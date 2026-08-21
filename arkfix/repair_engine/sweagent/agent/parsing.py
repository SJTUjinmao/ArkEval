from __future__ import annotations

import html
import json
import re
import shlex
import string
import textwrap
from abc import abstractmethod
from dataclasses import dataclass

from sweagent.agent.commands import Command


class FormatError(Exception):
    pass


def normalize_thought_action_minimax(model_response: str) -> str:
    """
    MiniMax (and some OpenAI-compatible gateways) may wrap reasoning in ``<think>`` /
    ``<redacted_thinking>`` and emit tool calls as ``<minimax:tool_call> ... </instruction>``
    or ``<invoke name="bash"><parameter name="command">...</parameter></invoke>`` instead of
    a fenced ``` block. `ThoughtActionParser` requires a ``` ... ``` command; this maps the
    common native XML shapes into that format and strips reasoning wrappers so a single turn
    can be parsed without burning format retries.
    """
    if not model_response or not model_response.strip():
        return model_response

    s = model_response
    # Reasoning / chain-of-thought wrappers (must not appear inside the final command fence).
    s = re.sub(
        r"<(think|thinking|redacted_thinking)>.*?</\1>",
        "",
        s,
        flags=re.DOTALL | re.IGNORECASE,
    )

    _one_arg_cmds = {"open", "goto", "create", "find_file", "search_file", "search_dir"}
    _no_arg_cmds = {"repair_status", "scroll_down", "scroll_up", "submit"}
    _shell_like_cmds = {"bash", "shell", "run", "execute"}

    def _collapse_inner(inner: str, *, known_cmd: str | None = None) -> str:
        inner = _strip_xmlish_argument(inner)
        if not inner:
            if known_cmd and known_cmd.lower() in _no_arg_cmds:
                return known_cmd
            return ""
        if known_cmd and known_cmd.lower() in _no_arg_cmds:
            inner_without_closing_tags = re.sub(r"</(?:invoke|tool_call|minimax:tool_call|instruction)>\s*", "", inner, flags=re.I)
            if not inner_without_closing_tags.strip():
                return known_cmd
        lines = [ln.strip() for ln in inner.splitlines() if ln.strip()]
        if len(lines) == 2 and lines[0] in _one_arg_cmds:
            return f"{lines[0]} {lines[1]}"
        if known_cmd and len(lines) == 1 and lines[0] and not lines[0].startswith(known_cmd):
            return f"{known_cmd} {lines[0]}"
        return inner

    def _to_fence(command: str) -> str:
        command = (command or "").strip()
        if not command:
            return "\n"
        return f"\n```\n{command}\n```\n"

    invoke_pat = re.compile(
        r"<invoke\b[^>]*\bname\s*=\s*(['\"])(.*?)\1[^>]*>\s*(.*?)\s*</invoke>",
        flags=re.DOTALL | re.IGNORECASE,
    )
    self_closing_invoke_pat = re.compile(
        r"<invoke\b[^>]*\bname\s*=\s*(['\"])(.*?)\1[^>]*/>",
        flags=re.DOTALL | re.IGNORECASE,
    )
    invoke_start_pat = re.compile(
        r"<invoke\b[^>]*\bname\s*=\s*(['\"])(.*?)\1[^>]*>",
        flags=re.DOTALL | re.IGNORECASE,
    )
    parameter_pat = re.compile(
        r"<parameter\b[^>]*\bname\s*=\s*(['\"])(.*?)\1[^>]*>\s*(.*?)\s*</parameter>",
        flags=re.DOTALL | re.IGNORECASE,
    )
    simple_param_pat = re.compile(
        r"<([A-Za-z_][\w:-]*)>\s*(.*?)\s*</\1>",
        flags=re.DOTALL | re.IGNORECASE,
    )
    command_tag_pat = re.compile(
        r"<command\b[^>]*>\s*(.*?)\s*</command>",
        flags=re.DOTALL | re.IGNORECASE,
    )

    def _strip_xmlish_argument(value: str) -> str:
        value = html.unescape((value or "").strip())
        if not value:
            return ""
        simple = re.fullmatch(r"<([A-Za-z_][\w:-]*)>\s*(.*?)\s*</\1>", value, flags=re.DOTALL | re.IGNORECASE)
        if simple:
            return _strip_xmlish_argument(simple.group(2))
        pseudo_path = re.fullmatch(
            r"<([^<>\s][^<>]*?)(?:</[A-Za-z_][\w:-]*)?\s*>",
            value,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if pseudo_path:
            return pseudo_path.group(1).strip()
        return value

    def _invoke_to_command(name: str, body: str) -> str:
        name = html.unescape((name or "").strip())
        params = [
            (html.unescape(param_name.strip()), _strip_xmlish_argument(value))
            for _, param_name, value in parameter_pat.findall(body or "")
        ]
        existing_param_names = {param_name.lower() for param_name, _ in params}
        for param_name, value in simple_param_pat.findall(body or ""):
            normalized_name = html.unescape(param_name.strip())
            if normalized_name.lower() not in existing_param_names:
                params.append((normalized_name, _strip_xmlish_argument(value)))
                existing_param_names.add(normalized_name.lower())
        command_from_named_params = _named_params_to_command(name, params)
        if command_from_named_params is not None:
            return command_from_named_params
        if name.lower() == "str_replace" and params:
            param_map = {param_name.lower(): value for param_name, value in params}
            path = next((param_map.get(key, "").strip() for key in ("path", "file") if param_map.get(key, "").strip()), "")
            replacement_block = _extract_replacement_block_from_body(body)
            if path and replacement_block:
                return f"str_replace {_quote_arg(path)}\n{replacement_block}"
        command_param = next((value for param_name, value in params if param_name.lower() == "command"), None)
        if command_param is not None:
            if name.lower() in _shell_like_cmds:
                return command_param.strip()
            return _collapse_inner(command_param, known_cmd=name)

        command_tag = command_tag_pat.search(body or "")
        if command_tag:
            command = html.unescape(command_tag.group(1).strip())
            if name.lower() in _shell_like_cmds:
                return command
            return _collapse_inner(command, known_cmd=name)

        if params:
            collapsed = " ".join(value for _, value in params if value)
            if name.lower() in _shell_like_cmds:
                return collapsed.strip()
            return _collapse_inner(collapsed, known_cmd=name)

        return _collapse_inner(body, known_cmd=name)

    def _quote_arg(value: str) -> str:
        return shlex.quote((value or "").strip())

    def _normalize_replacement_block(block: str, end_marker: str) -> str:
        block = html.unescape((block or "").strip())
        if not block:
            return ""
        block = re.sub(r"(?im)^\s*</?parameter\b[^>]*>\s*$", "", block)
        block = re.sub(r"(?im)^\s*</?invoke\b[^>]*>\s*$", "", block)
        block = re.sub(r"(?im)^\s*</?minimax:tool_call\b[^>]*>\s*$", "", block)
        block = re.sub(r"(?m)^<{7,}\s*OLD\s*$", "<<<<<<< OLD", block)
        block = re.sub(r"(?m)^>{7,}\s*NEW\s*$", ">>>>>>> NEW", block)
        if not block.endswith(end_marker):
            block = block.rstrip() + f"\n{end_marker}"
        return block

    def _extract_replacement_block_from_body(body: str) -> str:
        body = html.unescape(body or "")
        marker = re.search(r"(?im)^\s*<{7,}\s*OLD\s*$", body)
        if not marker:
            return ""
        block = body[marker.start() :]
        block = re.split(r"</(?:minimax:tool_call|tool_call|instruction)>", block, maxsplit=1, flags=re.I)[0]
        return _normalize_replacement_block(block, "end_of_str_replace")

    def _named_params_to_command(name: str, params: list[tuple[str, str]]) -> str | None:
        if not params:
            return None
        command = name.lower()
        param_map = {param_name.lower(): value for param_name, value in params}

        def pick(*names: str) -> str:
            for candidate in names:
                value = param_map.get(candidate)
                if value is not None and value.strip():
                    return value.strip()
            return ""

        if command in _shell_like_cmds:
            return None
        if command == "submit":
            return "submit"
        if command == "open":
            path = pick("path", "file", "filename")
            line_number = pick("line_number", "line", "line_number_start")
            if path:
                return " ".join(part for part in ["open", _quote_arg(path), line_number] if part)
        if command == "goto":
            line_number = pick("line_number", "line", "target_line")
            if line_number:
                return f"goto {line_number}"
        if command == "create":
            path = pick("filename", "file", "path")
            if path:
                return f"create {_quote_arg(path)}"
        if command == "find_file":
            file_name = pick("file_name", "filename", "name", "pattern")
            directory = pick("dir", "directory", "path")
            if file_name:
                return " ".join(part for part in ["find_file", _quote_arg(file_name), _quote_arg(directory) if directory else ""] if part)
        if command in {"search_file", "search_dir"}:
            search_term = pick("search_term", "term", "query", "pattern", "text")
            target = pick("file", "path") if command == "search_file" else pick("dir", "directory", "path")
            if search_term:
                return " ".join(part for part in [command, _quote_arg(search_term), _quote_arg(target) if target else ""] if part)
        if command == "ohpm":
            args = pick("args", "command", "cmd")
            if args:
                return f"ohpm {args}"
        if command == "edit_file":
            path = pick("path", "file")
            line_range = pick("range", "line_range")
            if not line_range:
                start_line = pick("start_line", "start")
                end_line = pick("end_line", "end")
                if start_line and end_line:
                    line_range = f"{start_line}:{end_line}"
            replacement = pick("replacement_text", "replacement", "new_text", "content", "body")
            if path and line_range and replacement:
                return f"edit_file {_quote_arg(path)} {line_range}\n{replacement}\nend_of_edit_file"
        if command == "str_replace":
            path = pick("path", "file")
            replacement_block = pick("replacement_block", "block", "body")
            if path and replacement_block:
                block = _normalize_replacement_block(replacement_block, "end_of_str_replace")
                if block.startswith("str_replace "):
                    return block
                return f"str_replace {_quote_arg(path)}\n{block}"
            old_text = pick("old", "old_text", "find", "before")
            new_text = pick("new", "new_text", "replacement", "after")
            if path and old_text and new_text:
                return (
                    f"str_replace {_quote_arg(path)}\n"
                    "<<<<<<< OLD\n"
                    f"{old_text}\n"
                    "=======\n"
                    f"{new_text}\n"
                    ">>>>>>> NEW\n"
                    "end_of_str_replace"
                )
        return None

    def _extract_last_invoke_command(inner: str) -> str:
        for invoke in reversed(list(invoke_pat.finditer(inner or ""))):
            command = _invoke_to_command(invoke.group(2), invoke.group(3))
            if command.strip():
                return command
        for invoke in reversed(list(self_closing_invoke_pat.finditer(inner or ""))):
            command = _invoke_to_command(invoke.group(2), "")
            if command.strip():
                return command
        for invoke in reversed(list(invoke_start_pat.finditer(inner or ""))):
            body = (inner or "")[invoke.end() :]
            command = _invoke_to_command(invoke.group(2), body)
            if command.strip():
                return command
        return ""

    def _tool_to_fence(m: re.Match) -> str:
        inner = m.group(1) or ""
        command = _extract_last_invoke_command(inner) or _collapse_inner(inner)
        return _to_fence(command)

    # 1) <minimax:tool_call> ... </instruction|minimax:tool_call>
    s = re.sub(
        r"<minimax:tool_call\b[^>]*>\s*(.*?)\s*</(?:instruction|minimax:tool_call)>",
        _tool_to_fence,
        s,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # 2) <invoke name="NAME"> ... </invoke> — Anthropic-flavoured tool call
    def _invoke_to_fence(m: re.Match) -> str:
        return _to_fence(_invoke_to_command(m.group(2), m.group(3)))

    s = re.sub(invoke_pat, _invoke_to_fence, s)

    def _self_closing_invoke_to_fence(m: re.Match) -> str:
        return _to_fence(_invoke_to_command(m.group(2), ""))

    s = re.sub(self_closing_invoke_pat, _self_closing_invoke_to_fence, s)

    # 3) Generic <tool_call> ... </tool_call> (OpenAI-style native tool payload)
    s = re.sub(
        r"<tool_call\b[^>]*>\s*(.*?)\s*</tool_call>",
        _tool_to_fence,
        s,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return s


# ABSTRACT BASE CLASSES


class ParseFunctionMeta(type):
    """
    Registry maps all inherited classes to their names.
    """

    _registry = {}

    def __new__(cls, name, bases, attrs):
        new_cls = super().__new__(cls, name, bases, attrs)
        if name != "ParseFunction":
            cls._registry[name] = new_cls
        return new_cls


@dataclass
class ParseFunction(metaclass=ParseFunctionMeta):
    """
    Abstract class for parsing functions.
    We use get to generate the right parser based on the name of the parser.
    """

    _error_message = None

    @abstractmethod
    def __call__(self, model_response, commands: list[Command], strict=False):
        raise NotImplementedError

    @property
    def format_error_template(self):
        if self._error_message is None:
            msg = "You must define an error message for your parser."
            raise NotImplementedError(msg)
        return textwrap.dedent(self._error_message)

    @classmethod
    def get(cls, name):
        try:
            return cls._registry[name]()
        except KeyError:
            msg = f"Model output parser ({name}) not found."
            raise ValueError(msg)


# DEFINE NEW PARSING FUNCTIONS BELOW THIS LINE


class ActionParser(ParseFunction):
    """
    Expects the model response to be a single command.
    Example: "ls -l"
    """

    _error_message = """\
    The command you provided was not recognized. Please specify one of the commands (+ any necessary arguments) from the following list in your response. Do not include any other text.

    COMMANDS:
    {command_docs}
    """

    def __call__(self, model_response, commands: list[Command], strict=False):
        if model_response.split():
            action = model_response.strip().split()[0]
            if action in {command.name for command in commands}:
                return model_response, model_response
        msg = "First word in model response is not a valid command."
        raise FormatError(msg)


class ThoughtActionParser(ParseFunction):
    """
    Expects the model response to be a discussion followed by a command wrapped in backticks.
    Example:
    Let's look at the files in the current directory.
    ```
    ls -l
    ```
    """

    _error_message = """\
    Your output was not formatted correctly. You must always include one discussion and one command as part of your response. Make sure you do not have multiple discussion/command tags.
    Please make sure your output precisely matches the following format:
    DISCUSSION
    Discuss here with yourself about what your planning and what you're going to do in this step.

    ```
    command(s) that you're going to run
    ```
    """

    def __call__(self, model_response, commands: list[Command], strict=False):
        """
        Parses the action from the output of the API call.
        We assume that the action is the last code block in the model_response.
        We also assume that the action is not nested within another code block.
        This is problematic if the model_response includes many unnamed ``` blocks.
        For instance:
        ```
        This is a code block.
        ```
        ```
        This is another code block.
        ```

        In this case, only the second code block will be parsed as the action.
        """
        model_response = normalize_thought_action_minimax(model_response)
        fence_line_pat = re.compile(r"^```[^\r\n]*\r?$", re.MULTILINE)
        open_fence = None
        valid_blocks = []
        for match in fence_line_pat.finditer(model_response):
            if open_fence is None:
                open_fence = match
                continue

            content_start = open_fence.end()
            if model_response.startswith("\r\n", content_start):
                content_start += 2
            elif model_response.startswith("\n", content_start):
                content_start += 1

            action = model_response[content_start : match.start()]
            if action.strip():
                valid_blocks.append((open_fence, match, action))
            open_fence = None

        if valid_blocks:
            start, end, action = valid_blocks[-1]
            thought = model_response[: start.start()] + model_response[end.end() :]
            return thought, action
        msg = "No non-empty action found in model response."
        raise FormatError(msg)


class XMLThoughtActionParser(ParseFunction):
    """
    Expects the model response to be a discussion followed by a command wrapped in XML tags.
    Example:
    Let's look at the files in the current directory.
    <command>
    ls -l
    </command>
    """

    _error_message = """\
    Your output was not formatted correctly. You must always include one discussion and one command as part of your response. Make sure you do not have multiple discussion/command tags.
    Please make sure your output precisely matches the following format:
    """

    def __call__(self, model_response, commands: list[Command], strict=False):
        """
        Parses the action from the output of the API call.
        We assume that the action is the last code block in the model_response.
        We also assume that the action is not nested within another code block.
        This is problematic if the model_response includes many unnamed ``` blocks.
        For instance:
        <command>
        This is a code block.
        </command>
        <command>
        This is another code block.
        </command>

        In this case, only the second code block will be parsed as the action.
        """
        if "<command>" not in model_response or "</command>" not in model_response:
            msg = "No action found in model response."
            raise FormatError(msg)
        # `action` is everything between the last <command> and </command> tags
        start_action = model_response.rfind("<command>") + len("<command>")  # start after the last <command> tag
        end_thought = model_response.rfind("<command>")  # end before the last <command> tag
        end_action = model_response.rfind("</command>")  # end before the last </command> tag
        restart_thought = model_response.rfind("</command>") + len("</command>")  # start after the last </command> tag
        # `thought` is everything not in between <command> and </command> tags (includes after the last </command> tag)
        action = model_response[start_action:end_action]
        thought = model_response[:end_thought] + model_response[restart_thought:]

        return thought.strip(), action.strip()


class EditFormat(ThoughtActionParser):
    """
    Expects the model response to be a discussion followed by a command wrapped in backticks.
    Example:
    We'll replace the contents of the current window with the following:
    ```
    import os
    os.listdir()
    ```
    """

    _error_message = """\
    Your output was not formatted correctly. You must wrap the replacement text in backticks (```).
    Please make sure your output precisely matches the following format:
    COMMENTS
    You can write comments here about what you're going to do if you want.

    ```
    New window contents.
    Make sure you copy the entire contents of the window here, with the required indentation.
    Make the changes to the window above directly in this window.
    Remember that all of the window's contents will be replaced with the contents of this window.
    Don't include line numbers in your response.
    ```
    """


class Identity(ParseFunction):
    """
    This parser does not do any parsing. It just returns the model response as both the thought and action.
    """

    _error_message = """\
    It seems like something went wrong with your output. Please try again.
    """

    def __call__(self, model_response, commands: list[Command], strict=False):
        """
        This doesn't do any parsing. It just returns the model response as the thought and action.
        """
        return model_response, model_response


class JsonParser(ParseFunction):
    """
    Expects the model response to be a JSON object.
    """

    _error_message = """\
    Your output could not be parsed as JSON. Please make sure your output 1) is valid JSON and
    2) Includes the "thought" and "command" fields.

    """

    def __call__(self, model_response, commands: list[Command], strict=False):
        """
        Parses the action from the output of the API call.
        We assume that model output is a JSON object with the following fields:
        {
            "thought": "discussion text here.",
            "command": {
                "arguments": {
                    "arg1": "value1",
                    "arg2": "value2",
                    ...
                },
                "name": "command_name"
            }
        }
        """
        try:
            data = json.loads(model_response)
            if not isinstance(data, dict):
                msg = "Model output is not a JSON object."
                raise FormatError(msg)

            # Check if required keys are present
            required_keys = ["thought", "command"]
            for key in required_keys:
                if key not in data:
                    msg = f"Key '{key}' is missing from model output."
                    raise FormatError(msg)

            # Check structure of 'command' key
            data_command = data["command"]
            if not isinstance(data_command, dict):
                msg = "Value of 'command' key is not a JSON object."
                raise FormatError(msg)

            # Check if required keys are present in 'command' object
            command_keys = ["name"]
            for key in command_keys:
                if key not in data_command:
                    msg = f"Key '{key}' is missing from 'command' object."
                    raise FormatError(msg)

            thought = data["thought"]

            # Generate action
            commands_dict = {c.name: c for c in commands}
            command = commands_dict.get(data_command["name"])
            if command is None:
                action = data_command["name"]
                if "arguments" in data_command:
                    action += " " + " ".join(data_command["arguments"].values())
            else:
                signature = command.signature
                signature = signature.replace("[", "").replace("]", "").replace("<", "{").replace(">", "}")
                signature_args = extract_keys(signature)
                command_args = {k: "" for k in signature_args}

                if "arguments" in data_command:
                    for arg in signature_args:
                        if arg in data_command["arguments"]:
                            value = data_command["arguments"][arg]
                            if should_quote(value, command):
                                value = shlex.quote(value)
                            command_args[arg] = value
                action = signature.format(**command_args)
            action = action.strip()
            return thought, action
        except json.JSONDecodeError:
            msg = "Model output is not valid JSON."
            raise FormatError(msg)


def extract_keys(format_string):
    """
    Given a format string, returns a set of all the keys in the format string.
    """
    formatter = string.Formatter()
    keys = set()
    for _, field_name, _, _ in formatter.parse(format_string):
        if field_name is not None:
            keys.add(field_name)
    return keys


def should_quote(value, command):
    """
    Returns True if the value should be quoted, False otherwise.
    """
    return isinstance(value, str) and command.end_name is None
