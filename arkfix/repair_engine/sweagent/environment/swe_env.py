from __future__ import annotations

import datetime
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import traceback
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import gymnasium as gym
import yaml
from ghapi.all import GhApi
from git import Repo
from simple_parsing.helpers.serialization.serializable import FrozenSerializable
from simple_parsing.helpers.fields import field
from multi_swe_bench.harness.instance import Instance, Record, Image
from multi_swe_bench.harness.build_dataset import build_image, CliArgs
import docker
import docker.errors
import docker.models.containers
from sweagent import REPO_ROOT
from sweagent.environment.utils import (
    arkts_container_huawei_cli_env_bash,
    resolve_command_line_tools_host_path,
    PROCESS_DONE_MARKER_END,
    PROCESS_DONE_MARKER_START,
    InvalidGithubURL,
    copy_anything_to_container,
    copy_file_to_container,
    format_trajectory_markdown,
    get_container,
    get_gh_issue_data,
    get_instances,
    image_exists,
    native_build_permit,
    remove_image,
    terminate_process_tree,
    parse_gh_issue_url,
    read_with_timeout,
    read_with_timeout_experimental,
    action_hacking,
)
from sweagent.utils.config import keys_config
from sweagent.utils.log import default_logger, get_logger
from sweagent.utils.patch_utils import (
    defect_tree_sha256,
    find_arkts_forbidden_added_syntax,
    filter_submission_to_defect_files,
    scope_ranked_defect_files_to_harmony_project,
)
from sweagent.utils.native_repo import reset_repo_to_commit
from sweagent.utils.repair_status import (
    changed_files_from_patch,
    compute_repair_status,
    format_repair_status,
)

LONG_TIMEOUT = float(keys_config.get("SWE_AGENT_ENV_LONG_TIMEOUT", 600))
AGENT_ACTION_TIMEOUT = float(keys_config.get("SWE_AGENT_ACTION_TIMEOUT", 300))
AGENT_ACTION_NO_OUTPUT_TIMEOUT = float(keys_config.get("SWE_AGENT_ACTION_NO_OUTPUT_TIMEOUT", AGENT_ACTION_TIMEOUT))
BUILD_ACTION_TIMEOUT = float(keys_config.get("SWE_AGENT_BUILD_ACTION_TIMEOUT", max(LONG_TIMEOUT, 900)))
BUILD_ACTION_NO_OUTPUT_TIMEOUT = float(
    keys_config.get("SWE_AGENT_BUILD_ACTION_NO_OUTPUT_TIMEOUT", max(AGENT_ACTION_NO_OUTPUT_TIMEOUT, 600))
)
PATH_TO_REQS = "/root/requirements.txt"
PATH_TO_ENV_YML = "/root/environment.yml"
UNICODE_REPLACEMENT_CHAR = "\ufffd"


def _read_utf8_submission_patch(patch_path: Path) -> str:
    return _decode_utf8_submission_patch(patch_path.read_bytes(), str(patch_path))


def _decode_utf8_submission_patch(data: bytes, label: str) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = f"{label} is not valid UTF-8: {exc}"
        raise ValueError(msg) from exc
    if UNICODE_REPLACEMENT_CHAR in text:
        msg = f"{label} contains Unicode replacement characters; re-export the patch from clean source bytes"
        raise ValueError(msg)
    if "\x00" in text:
        msg = f"{label} contains NUL bytes; refusing to treat it as a text patch"
        raise ValueError(msg)
    return text


def _git_bash_path(path_value: str | os.PathLike[str]) -> str:
    """Convert a Windows path to the form Git Bash understands in shell exports."""

    path = Path(path_value).expanduser().resolve()
    text = str(path).replace("\\", "/")
    if len(text) >= 2 and text[1] == ":":
        return f"/{text[0].lower()}{text[2:]}"
    return text


def _quote_git_bash_path(path_value: str | os.PathLike[str]) -> str:
    return shlex.quote(_git_bash_path(path_value))


_COMMAND_USAGE: dict[str, tuple[str, str]] = {
    "open": ("open <file> [<line_number>]", "open entry/src/main/ets/pages/Index.ets 120"),
    "goto": ("goto <line_number>", "goto 120"),
    "create": ("create <filename>", "create entry/src/main/ets/pages/NewPage.ets"),
    "find_file": ("find_file <file_name> [<dir>]", "find_file RequestOption.ets library/src/main/ets"),
    "search_file": (
        "search_file <search_term> [<file>]",
        "search_file 'generateDataKey(' library/src/main/ets/components/imageknife/RequestOption.ets",
    ),
    "search_dir": ("search_dir <search_term> [<dir>]", "search_dir 'generateDataKey(' library/src/main/ets"),
    "scroll_up": ("scroll_up", "scroll_up"),
    "scroll_down": ("scroll_down", "scroll_down"),
    "repair_status": ("repair_status", "repair_status"),
    "submit": ("submit [ignored-path ...]", "submit"),
    "ohpm": ("ohpm <args>", "ohpm ls"),
    "set_cursors": ("set_cursors <start_line> <end_line>", "set_cursors 120 150"),
    "edit": (
        "edit <start_line>:<end_line> followed by replacement text and end_of_edit",
        "edit 120:126\n<replacement text>\nend_of_edit",
    ),
    "edit_file": (
        "edit_file <path> <start_line>:<end_line> followed by replacement text and end_of_edit_file",
        "edit_file library/src/main/ets/Foo.ets 120:126\n<replacement text>\nend_of_edit_file",
    ),
    "str_replace": (
        "str_replace <path> followed by <<<<<<< OLD / ======= / >>>>>>> NEW / end_of_str_replace",
        "str_replace library/src/main/ets/Foo.ets\n<<<<<<< OLD\n<exact old text>\n=======\n<new text>\n>>>>>>> NEW\nend_of_str_replace",
    ),
}

_EDIT_FILE_SUFFIX_RE = re.compile(r".*\.(?:ets|ts|tsx|js|jsx|json5|json|py|java|kt|cpp|cc|c|h|hpp)$", re.I)
_LINE_RANGE_RE = re.compile(r"^[0-9]+:[0-9]+$")
_XML_TOOL_RE = re.compile(r"</?\s*(?:minimax:tool_call|invoke|tool|command|args|parameters?|path)\b", re.I)
_SCRIPT_FILE_WRITE_RE = re.compile(
    r"\b(?:fs\.writeFileSync|writeFileSync|open\s*\([^)]*['\"][wax]\+?|Path\s*\([^)]*\)\.write_(?:text|bytes)|Set-Content|Add-Content|Out-File)\b",
    re.I | re.S,
)
_SHELL_REDIRECT_RE = re.compile(r"(?:^|\s)(?:>|>>|1>|2>)\s*(?:['\"])?([^\s'\";|&]+)", re.I)
_REPO_EDIT_TARGET_RE = re.compile(r".*\.(?:ets|ts|tsx|js|jsx|json5|json|py|java|kt|cpp|cc|c|h|hpp)$", re.I)


def _command_format_error(
    raw: str,
    problem: str,
    usage: str,
    example: str | None = None,
    retry: str | None = None,
) -> str:
    feedback = (
        "COMMAND_FORMAT_ERROR: command was not executed.\n"
        f"You wrote: {raw}\n"
        f"Problem: {problem}\n"
        f"Correct syntax: {usage}"
    )
    if example:
        feedback += f"\nExample:\n{example}"
    if retry:
        feedback += f"\nRetry exactly with this corrected command:\n{retry}"
    return feedback


def _usage_for(command: str) -> tuple[str, str]:
    return _COMMAND_USAGE.get(command, (f"{command} <args>", f"{command} <args>"))


def _strip_heredoc_tokens(parts: list[str]) -> list[str]:
    if "<<" in parts:
        return parts[: parts.index("<<")]
    return parts


def _looks_like_path_bound_edit(parts: list[str]) -> bool:
    if len(parts) < 2:
        return False
    first = parts[0].replace("\\", "/")
    return bool(_EDIT_FILE_SUFFIX_RE.match(first) and _LINE_RANGE_RE.match(parts[1]))


def _looks_like_missing_str_replace_command(parts: list[str], raw: str) -> bool:
    if not parts:
        return False
    first = parts[0].replace("\\", "/")
    if not _EDIT_FILE_SUFFIX_RE.match(first):
        return False
    return bool(re.search(r"(?m)^\s*<{7,}\s*OLD\s*$", raw) or "=======" in raw or ">>>>>>> NEW" in raw)


def _normalize_str_replace_marker_line(line: str) -> str:
    stripped = line.strip()
    if re.fullmatch(r"<{7,}\s*OLD", stripped):
        return "<<<<<<< OLD"
    if re.fullmatch(r"={7,}", stripped):
        return "======="
    if re.fullmatch(r">{7,}\s*NEW", stripped):
        return ">>>>>>> NEW"
    if stripped == "end_of_str_replace":
        return "end_of_str_replace"
    return line


def _corrected_missing_str_replace_command(raw: str, parts: list[str]) -> str | None:
    """Build a copy-pasteable retry when the model wrote only the path."""

    if not parts:
        return None
    path = parts[0].strip("'\"")
    if not path:
        return None

    lines = raw.splitlines()
    if not lines:
        return None

    first_line_match = re.match(r"^\s*(?:'[^']+'|\"[^\"]+\"|\S+)\s*(.*)$", lines[0])
    first_line_tail = first_line_match.group(1).strip() if first_line_match else ""
    block_lines = []
    if first_line_tail:
        block_lines.append(first_line_tail)
    block_lines.extend(lines[1:])
    if not any(re.fullmatch(r"\s*<{7,}\s*OLD\s*", line) for line in block_lines):
        return None

    normalized_block = [_normalize_str_replace_marker_line(line.rstrip("\r")) for line in block_lines]
    return f"str_replace {shlex.quote(path)}\n" + "\n".join(normalized_block)


def _is_repo_edit_target(value: str) -> bool:
    value = (value or "").strip().strip("'\"").replace("\\", "/")
    if not value:
        return False
    # Allow running helper scripts such as command_line_tools_test/tools/build_app.py.
    # This helper is only for write targets, so source-like paths are suspicious.
    return bool(_REPO_EDIT_TARGET_RE.match(value))


def _is_forbidden_search_target(value: str) -> bool:
    normalized = (value or "").strip().strip("'\"").replace("\\", "/").lower()
    if not normalized:
        return False
    padded = f"/{normalized.strip('/')}/"
    blocked_segments = (
        "/env_overlays/",
        "/oh_modules/",
        "/node_modules/",
        "/.hvigor/",
        "/build/",
        "/.git/",
    )
    if any(segment in padded for segment in blocked_segments):
        return True
    return any(
        marker in normalized
        for marker in (
            "devecoapi",
            "/deveco studio/",
            "/command-line-tools/",
            "/sdk/",
        )
    ) or normalized.endswith("/sdk")


def _has_shell_redirection_to_repo_file(raw: str) -> bool:
    return any(_is_repo_edit_target(match.group(1)) for match in _SHELL_REDIRECT_RE.finditer(raw or ""))


def _has_tee_write_to_repo_file(parts: list[str], raw: str) -> bool:
    if not parts:
        return False
    for index, part in enumerate(parts):
        if part != "tee":
            continue
        for target in parts[index + 1 :]:
            if target.startswith("-"):
                continue
            if _is_repo_edit_target(target):
                return True
            break
    return bool(re.search(r"(?:^|\|)\s*tee\s+(?:-[A-Za-z]+\s+)*[\"']?[^\"'\s]+\.(?:ets|ts|tsx|js|jsx|json5|json|py|java|kt|cpp|cc|c|h|hpp)\b", raw or "", re.I))


def _has_inplace_edit_to_repo_file(parts: list[str]) -> bool:
    if not parts:
        return False
    command = parts[0]
    if command == "sed" and any(part == "-i" or part.startswith("-i") for part in parts[1:]):
        return any(_is_repo_edit_target(part) for part in parts[1:])
    if command == "perl" and any("i" in part and part.startswith("-") for part in parts[1:]):
        return any(_is_repo_edit_target(part) for part in parts[1:])
    return False


def _is_long_running_build_action(action: str) -> bool:
    if re.search(r"\|\||(?<!\|)\|(?!\|)|;|(?<![&<>])&(?![&>])", action):
        return False
    last_command: tuple[str, list[str]] | None = None
    for segment in re.split(r"(?:&&|\n)", action):
        try:
            parts = shlex.split(segment.strip().replace("\\", "/"), posix=True)
        except ValueError:
            return False
        while parts and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", parts[0]):
            parts.pop(0)
        if not parts or parts[0] == "cd":
            continue
        command = parts[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
        args = [part.replace("\\", "/").lower() for part in parts[1:]]
        last_command = (command, args)
    if last_command is None:
        return False
    command, args = last_command
    if command in {"python", "python3", "python.exe", "py", "py.exe"}:
        return any(arg.rsplit("/", 1)[-1] == "build_app.py" for arg in args)
    if command == "build_app.py":
        return True
    if command in {"hvigor", "hvigorw", "hvigor.bat", "hvigorw.bat", "hvigor.cmd", "hvigorw.cmd"}:
        return any(re.fullmatch(r"assemble(?:hap|har|hsp)", arg, re.IGNORECASE) for arg in args)
    return False


def _looks_like_forbidden_file_write(raw: str, parts: list[str]) -> bool:
    if not raw or not parts:
        return False
    command = parts[0]
    if command in {"cp", "mv", "install", "rsync", "patch"}:
        return True
    if command == "git":
        if any(part in {"apply", "am", "checkout", "restore", "reset", "clean"} for part in parts[1:]):
            return True
    if re.search(r"(?:^|(?:&&|\|\||;|\||\n)\s*)(?:command\s+)?(?:cp|mv|install|rsync|patch)\b", raw):
        return True
    if re.search(
        r"(?:^|(?:&&|\|\||;|\||\n)\s*)(?:command\s+)?git\b[^;&|\n]*\b(?:apply|am|checkout|restore|reset|clean)\b",
        raw,
    ):
        return True
    script_commands = {"bash", "sh", "zsh", "node", "python", "python3", "py", "powershell", "pwsh"}
    if command in script_commands and _SCRIPT_FILE_WRITE_RE.search(raw):
        return True
    if command in {"cat", "echo", "printf", "bash", "sh", "zsh"} and _has_shell_redirection_to_repo_file(raw):
        return True
    if _has_tee_write_to_repo_file(parts, raw):
        return True
    if _has_inplace_edit_to_repo_file(parts):
        return True
    if command in {"powershell", "pwsh"} and _has_shell_redirection_to_repo_file(raw):
        return True
    return False


def _agent_command_format_feedback(action: str) -> str | None:
    """Return actionable feedback for common malformed interactive commands.

    Some agent-facing commands are shell functions. If the model writes a
    malformed command such as ``search_file build() path``, bash fails before the
    function can print its own usage. Catching the common shapes here gives the
    model a precise recovery log instead of a generic shell parse error.
    """

    raw = (action or "").strip()
    if not raw:
        return None

    first_line = raw.splitlines()[0].strip()
    command_match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)", first_line)
    command_name = command_match.group(1) if command_match else ""

    if _XML_TOOL_RE.search(raw) and (command_name in _COMMAND_USAGE or raw.lstrip().startswith("<")):
        usage, example = _usage_for(command_name or "str_replace")
        if command_name == "ohpm":
            example = "ohpm ls"
        problem = "MiniMax XML tool_call was not normalized; command was not executed. Use the direct tool syntax below."
        if command_name:
            problem = "XML-style tool calls are not supported in this shell. Write the command directly."
        return _command_format_error(
            raw,
            problem,
            usage,
            example,
        )

    try:
        first_parts = shlex.split(first_line, posix=True)
    except ValueError:
        if command_name in _COMMAND_USAGE:
            usage, example = _usage_for(command_name)
            return _command_format_error(
                raw,
                "The command line has unbalanced quotes or invalid shell quoting.",
                usage,
                example,
            )
        first_parts = []

    if _looks_like_forbidden_file_write(raw, first_parts):
        _, edit_example = _usage_for("edit_file")
        _, replace_example = _usage_for("str_replace")
        return _command_format_error(
            raw,
            "Do not write repository files with shell, copy/move/patch/git mutation commands, Node, Python, PowerShell, redirection, tee, heredocs, sed -i, or perl -pi. Use edit_file, str_replace, or create so the repair tool can validate the diff.",
            "edit_file <path> <start_line>:<end_line> or str_replace <path>",
            f"{edit_example}\n\n{replace_example}",
        )
    first_parts = _strip_heredoc_tokens(first_parts)
    if first_parts and _looks_like_path_bound_edit(first_parts):
        usage, example = _usage_for("edit_file")
        str_usage, str_example = _usage_for("str_replace")
        return _command_format_error(
            raw,
            "This looks like a path plus line range, but no edit tool was named.",
            f"{usage}\nAlternative for block edits: {str_usage}",
            f"{example}\n\n{str_example}",
        )

    if first_parts and _looks_like_missing_str_replace_command(first_parts, raw):
        usage, example = _usage_for("str_replace")
        retry = _corrected_missing_str_replace_command(raw, first_parts)
        return _command_format_error(
            raw,
            'This looks like a str_replace replacement block, but the str_replace command name is missing. You forgot the tool name. Do not retry the path alone. The first line must start with "str_replace ".',
            usage,
            example,
            retry,
        )

    if first_parts and first_parts[0] == "str_replace":
        usage, example = _usage_for("str_replace")
        if len(first_parts) != 2:
            return _command_format_error(
                raw,
                "str_replace takes exactly one path argument on its first line.",
                usage,
                example,
            )
        required_markers = ("<<<<<<< OLD", "=======", ">>>>>>> NEW", "end_of_str_replace")
        if any(marker not in raw for marker in required_markers):
            return _command_format_error(
                first_line,
                "str_replace is missing the complete replacement block. Do not call it with only a path.",
                usage,
                example,
            )
        return None

    if first_parts and first_parts[0] == "edit_file":
        usage, example = _usage_for("edit_file")
        if len(first_parts) != 3:
            return _command_format_error(
                raw,
                "edit_file needs a path and a <start_line>:<end_line> range on the first line.",
                usage,
                example,
            )
        if not _LINE_RANGE_RE.match(first_parts[2]):
            return _command_format_error(
                raw,
                "edit_file range must be written as <start_line>:<end_line>, for example 120:126.",
                usage,
                example,
            )
        if "end_of_edit_file" not in raw:
            return _command_format_error(
                first_line,
                "edit_file is missing replacement text terminated by end_of_edit_file.",
                usage,
                example,
            )
        return None

    if first_parts and first_parts[0] == "edit":
        usage, example = _usage_for("edit")
        if len(first_parts) != 2:
            return _command_format_error(
                raw,
                "edit needs exactly one <start_line>:<end_line> range on the first line.",
                usage,
                example,
            )
        if not _LINE_RANGE_RE.match(first_parts[1]):
            return _command_format_error(raw, "edit range must look like 120:126.", usage, example)
        if "end_of_edit" not in raw:
            return _command_format_error(
                first_line,
                "edit is missing replacement text terminated by end_of_edit.",
                usage,
                example,
            )
        return None

    if "\n" in raw:
        if command_name in _COMMAND_USAGE:
            usage, example = _usage_for(command_name)
            return _command_format_error(
                raw,
                "This command does not take a multi-line body. Put only the command line in the action.",
                usage,
                example,
            )
        return None

    try:
        parts = shlex.split(raw, posix=True)
    except ValueError:
        return None
    if not parts:
        return None

    command = parts[0]
    usage, example = _usage_for(command)

    if command == "open":
        if len(parts) not in (2, 3):
            return _command_format_error(raw, "open needs one file path and optional line number.", usage, example)
        if len(parts) >= 3 and parts[1].isdigit() and not parts[2].isdigit():
            corrected = " ".join(["open", shlex.quote(parts[2]), parts[1], *[shlex.quote(p) for p in parts[3:]]])
            return _command_format_error(
                raw,
                "The file path must come first and the line number second.",
                usage,
                corrected,
            )
        if len(parts) == 3 and not parts[2].isdigit():
            return _command_format_error(raw, "open line_number must be numeric.", usage, example)

    if command == "goto":
        if len(parts) != 2 or not parts[1].isdigit():
            return _command_format_error(raw, "goto takes exactly one numeric line number.", usage, example)

    if command == "create":
        if len(parts) != 2:
            return _command_format_error(raw, "create takes exactly one filename.", usage, example)

    if command in {"scroll_up", "scroll_down", "repair_status"}:
        if len(parts) != 1:
            return _command_format_error(raw, f"{command} takes no arguments.", usage, example)

    if command == "set_cursors":
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            return _command_format_error(
                raw,
                "set_cursors takes exactly two numeric line numbers.",
                usage,
                example,
            )

    if command in {"find_file", "search_file", "search_dir"}:
        if len(parts) not in (2, 3):
            return _command_format_error(raw, f"{command} takes one required argument and one optional target.", usage, example)
        if len(parts) == 3 and _is_forbidden_search_target(parts[2]):
            return _command_format_error(
                raw,
                "Do not search SDK, DevEco, env_overlays, dependency, build, .hvigor, or .git directories. These targets are too large/generated and can kill the shell; search the repo/project source tree or KNOWN DEFECT FILES instead.",
                usage,
                example,
            )

    if command == "ohpm" and len(parts) < 2:
        return _command_format_error(raw, "ohpm needs the arguments to pass through to ohpm.bat.", usage, example)

    def _has_unescaped_shell_metachar(token: str) -> bool:
        escaped = False
        for ch in token:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch in "(){};&|<>":
                return True
        return False

    search_match = re.match(r"^\s*(search_file|search_dir)\s+([^'\"\s][^\s]*)", raw)
    if search_match:
        search_command = search_match.group(1)
        search_term = search_match.group(2)
        if _has_unescaped_shell_metachar(search_term):
            rest = raw[search_match.end() :].strip()
            corrected = f"{search_command} {shlex.quote(search_term)}"
            if rest:
                corrected += f" {rest}"
            return (
                "COMMAND_FORMAT_ERROR: command was not executed.\n"
                f"You wrote: {raw}\n"
                f"Correct syntax: {search_command} <search_term> "
                + ("[<file>]" if search_command == "search_file" else "[<dir>]")
                + "\n"
                "The search term contains shell metacharacters and must be quoted before bash sees it.\n"
                f"Retry exactly like:\n{corrected}"
            )

    return None


def _is_build_success_observation(observation: str) -> bool:
    return any(
        marker in observation
        for marker in ("BUILD_STATUS=SUCCESS", "COMPILE RESULT:SUCCESS", "BUILD SUCCESSFUL")
    )


@dataclass(frozen=True)
class EnvironmentArguments(FrozenSerializable):
    """Configure data sources and setup instructions for the environment in which we solve the tasks."""
    # Specify the data meta info
    cli_args: CliArgs
    # Specify a branch name or a commit hash to checkout before running the task.
    # Only used when running over a single problem statement/issue.
    base_commit: str | None = None
    # Use a persistent container with this name. After every task, the container will be paused, but not removed.
    # This is useful for speedup when running multiple tasks from the same repositories in a row, as the repositories
    # will have already been cloned and the conda environments will have been installed.
    container_name: str | None = None
    # Try to install the environment before running the task.
    install_environment: bool = True
    # No effect, kept for backwards compatibility.
    timeout: int | None = None
    # Enable environment logger.
    verbose: bool = False
    # Do not use attempt to use a repository mirror from https://github.com/swe-bench.
    no_mirror: bool = True
    # Cache task images to speed up task initialization. This means that the environment will be saved as a
    # docker image for every repository, base commit, and setup combination. This uses quite a bit of disk space
    # but speeds up task initialization significantly when running over multiple issues from the same repository
    # (or using different models for the same issues).
    cache_task_images: bool = False
    # Custom environment setup. Currently only used when data_path points to a single issue.
    # This needs to be either a string pointing to a yaml file (with yaml, yml file extension)
    # or a shell script (with sh extension).
    # See https://princeton-nlp.github.io/SWE-agent/usage/cl_tutorial#environment-setup
    environment_setup: str | None = None
    # Only used when running on single issue. Path to local repository or github repository.
    repo_path: str = ""
     # whether to pre-build all images before running instances or build on the fly
    pre_build_all_images: bool = False
    # remove image after a instance is done
    remove_image: bool = False
    # Base directory to search for local repos in native mode. If empty, defaults to the parent directory of MSWE-agent.
    repos_base_dir: str = ""



    def __post_init__(self):
        if self.timeout is not None:
            default_logger.warning("The 'timeout' argument is deprecated and has no effect.")
        if self.cache_task_images and self.container_name:
            msg = (
                "Not allowed to use persistent container with caching task images "
                "(probably doesn't make sense and takes excessive space)."
            )
            raise ValueError(msg)
        if self.container_name is not None and self.container_name.strip() == "":
            msg = "Set container_name to None if you don't want to use a persistent container."
            raise ValueError(msg)


class EnvHook:
    """Hook to be used in `SWEEnv`.

    Subclass this class, add functionality and add it with `SWEEEnv.add_hook(hook)`.
    This allows to inject custom functionality at different stages of the environment
    lifecycle, in particular to connect SWE-agent to a new interface (like a GUI).
    """

    def on_init(self) -> None:
        """Gets called when the hook is added"""

    def on_copy_repo_started(self, *, repo_type: str, repo_path: str) -> None:
        """Gets called when the repository is being cloned to the container

        Args:
            repo_type: Type of repository. Either 'local' or 'github'
            repo_path: Path to the repository
        """

    def on_install_env_started(self) -> None:
        """Called when we start installing the environment"""

    def on_close(self):
        """Called when the environment is closed"""


class SWEEnv(gym.Env):
    """Gym environment for SWE-bench. This class should handle all communication with the docker container."""

    name = "swe_main"
    # This prefix will be prepended to the image name when caching task images
    cached_image_prefix = "swe-agent-task-env-"

    def __init__(self, args: EnvironmentArguments, log_dir: Path = None):
        super().__init__()
        t0 = time.perf_counter()
        self.args = args
        self.prebuild = args.pre_build_all_images
        self.remove_image = args.remove_image
        self.base_commit: str | None = None
        self.communicate_output: str | None = None
        self.container_name: str | None = args.container_name
        self.install_environment = args.install_environment
        self.logger = get_logger("SWEEnv", log_dir)
        self.persistent = args.container_name is not None
        self.returncode: None | int = None
        self.native_mode: bool = False
        if not self.args.verbose:
            # fixme: This creates problems if we have multiple instances of this class
            self.logger.disabled = True

        #: The commit hash of the swe-agent repository
        self.commit_sha = None
        try:
            repo = Repo(REPO_ROOT, search_parent_directories=True)
            self.commit_sha = repo.head.object.hexsha
        except KeyboardInterrupt:
            raise
        except Exception as e:
            self.logger.exception("Failed to get commit hash for this repo: %s", str(e))

        self._github_token: str = keys_config.get("GITHUB_TOKEN", "")  # type: ignore

        # Load Task Instances
        self.data_path = self.args.cli_args.pr_file
        self.data = get_instances(
            self.data_path,
            cli_args=self.args.cli_args,
            prebuild=self.prebuild
        )
        #: Instance we're currently processing. Gets set in self.reset.
        self.record: Record | None = None
        # Avoid emoji in logs: Windows consoles may default to GBK and crash on Unicode output.
        self.logger.info(f"Loaded dataset from {self.data_path}")

        # Establish connection with execution container
        self.image_name = None
        self.container_obj: docker.models.containers.Container | None = None
        self.container: subprocess.Popen | None = None
        # self._reset_container()

        self.idx = 0
        self.clean_multi_line_functions = lambda x: x
        self.hooks: list[EnvHook] = []

        self.logger.debug("Environment initialization took %.2f seconds", time.perf_counter() - t0)

    def _get_cached_task_image_name(self) -> str:
        assert self.record is not None
        inputs: list[str] = [
            self.record.instance.pr.repo,
            self.record.instance.pr.base.sha,
            self.args.environment_setup or "no_setup",
        ]
        tag = hashlib.sha256("".join(inputs).encode()).hexdigest()[:50]
        return f"{self.cached_image_prefix}{tag}"

    def add_hook(self, hook: EnvHook):
        """Add `EnvHook` to the environment.

        This allows to inject custom functionality at different stages of the environment
        lifecycle, in particular to connect SWE-agent to a new interface (like a GUI).
        """
        hook.on_init()
        self.hooks.append(hook)

    @property
    def _repo_name(self) -> str:
        """Name of the local copy of the repository"""
        assert self.record is not None
        return self.record.instance.pr.repo

    def _copy_repo(self) -> str:
        """Clone/copy repository/codebase in container

        Returns:
            folder name of clone
        """
        assert self.container_obj is not None
        assert self.record is not None  # mypy
        if self._github_token:
            token_prefix = f"{self._github_token}@"
        # fixme: This if statement is brittle and should probably be replaced with better logic
        self.logger.info("Trying to clone from non-mirror...")
        clone_url = f"https://{token_prefix}github.com/{self.record.instance.pr.repo}.git"
        clone_method = keys_config.get("SWE_AGENT_CLONE_METHOD", default="sparse", choices=["sparse", "full"])
        if len(self.data) > 1 or self.persistent:
            msg = "Falling back to full cloning method due to multiple instances or persistent container"
            self.logger.debug(msg)
        if clone_method == "full":
            self.communicate_with_handling(
                input=f"git clone {clone_url} {self._repo_name}",
                error_msg="Failed to clone repository from conservative method",
                timeout_duration=LONG_TIMEOUT,
            )
        else:
            base_commit = self.record.instance.pr.base.sha
            self.communicate_with_handling(
                input="&&".join(
                    (
                        f"mkdir {self._repo_name}",
                        f"cd {self._repo_name}",
                        "git init",
                        f"git remote add origin {clone_url}",
                        f"git fetch --depth 1 origin {base_commit}",
                        "git checkout FETCH_HEAD",
                        "cd ..",
                    )
                ),
                error_msg="Failed to clone repository with fast method",
                timeout_duration=LONG_TIMEOUT,
            )
        return self._repo_name

    def reset(self, instance_id: str, apply_test_patch: bool = False) -> tuple[str | None, dict]:
        """
        Function to reset container between each task instance.

        * Clones instance's repository
        * Cleans repository of prior modifications
        * Resets environment variables
        * Check out base commit

        Args:
            instance_id

        Returns:
            observation: output from container
            info: additional information (e.g. debugging information)
        """
        info = {}
        info["commit_sha"] = self.commit_sha

        # Get task instance
        self.record = self.data[instance_id]

        # Set query, gold command
        self.base_commit = self.record.instance.pr.base.sha
        pr = self.record.instance.pr
        # Prefer linked issue text when present; ArkTS/Gitee defect jsonl often has placeholder
        # resolved_issues (short title + empty body) while the real spec is on the PR.
        if pr.resolved_issues:
            ri = pr.resolved_issues[0]
            ri_body = (ri.body or "").strip()
            if not ri_body and ((pr.title or "").strip() or (pr.body or "").strip()):
                self.query = "TITLE:\n" + (pr.title or "") + "\n CONTENT:\n" + (pr.body or "")
            else:
                self.query = "TITLE:\n" + (ri.title or "") + "\n CONTENT:\n" + (ri.body or "")
        else:
            self.query = "TITLE:\n" + (pr.title or "") + "\n CONTENT:\n" + (pr.body or "")
        self.reward = None

        cli_pr_file = str(getattr(getattr(self.args, "cli_args", None), "pr_file", "")).lower()
        self.native_mode = (getattr(self.record, "language", None) == "arkts") or ("arkts" in cli_pr_file)

        # build images
        if not self.prebuild:
            self._build_image()

        ### Reset Container ###
        self._reset_container(instance_id)

        # Clone repository if not already cloned
        if not self.native_mode:
            self.communicate(input="cd /home")
            folders = self.communicate(input="ls").split("\n")
            assert self._repo_name in folders

        # Clean repository of any modifications + Checkout base commit
        if self.native_mode:
            reset_repo_to_commit(
                self.native_workdir,
                self.base_commit,
                clean_args=["clean", "-ffdxq", "-e", ".codephoenix/"],
                verify_clean=True,
                preserve_paths=(".codephoenix",),
            )
            clean_cmds = [
                'echo -n > "${SWE_AGENT_COMMANDS_DIR}/files_to_edit.txt"',
                "export ROOT=$(pwd -P)",
                "git status",
            ]
        else:
            clean_cmds = [
                f"echo -n > /root/files_to_edit.txt",
                f"cd {self._repo_name}",
                "export ROOT=$(pwd -P)",
                "git status",
                "git restore .",
                f"git reset --hard {self.base_commit}",
                "git clean -ffdxq",
            ]
        for cmd in clean_cmds:
            self.communicate_with_handling(
                input=cmd,
                error_msg="Failed to clean repository",
                timeout_duration=LONG_TIMEOUT,
            )

        # Hard verification: ensure we're exactly at base_commit and the workspace is clean.
        expected_head = (self.base_commit or "").strip().lower()
        actual_head = self.communicate_with_handling(
            input="git rev-parse HEAD",
            error_msg="Failed to read git HEAD after reset",
            timeout_duration=LONG_TIMEOUT,
        ).strip().lower()
        if not expected_head or not actual_head or expected_head != actual_head:
            msg = (
                "Repository reset verification failed: "
                f"expected HEAD={expected_head or '<empty>'}, actual HEAD={actual_head or '<empty>'}."
            )
            self.logger.error(msg)
            raise RuntimeError(msg)

        dirty_status = self.communicate_with_handling(
            input="git status --porcelain -- . ':(exclude).codephoenix/**'",
            error_msg="Failed to inspect git working tree after reset",
            timeout_duration=LONG_TIMEOUT,
        )
        if dirty_status.strip():
            msg = (
                "Repository reset verification failed: working tree is not clean after reset. "
                f"git status --porcelain output:\n{dirty_status}"
            )
            self.logger.error(msg)
            raise RuntimeError(msg)

        self.logger.info("Reset verification passed: HEAD matches base_commit and working tree is clean.")

        if self.native_mode:
            raw_defect_files = self.record.data.get("defect_files", [])
            if not isinstance(raw_defect_files, list) or not raw_defect_files:
                raise RuntimeError(f"{self._repo_name} record has no ranked defect_files")
            project_path, scoped_defect_files = scope_ranked_defect_files_to_harmony_project(
                self.native_workdir,
                [str(item) for item in raw_defect_files],
            )
            self.record.data["project_path"] = project_path
            self.record.data["defect_files"] = scoped_defect_files
            raw_defect_files = scoped_defect_files
            self.logger.info(
                "ArkTS runtime scope: project=%s defect_files=%d",
                project_path,
                len(scoped_defect_files),
            )
            project_path_raw = self.record.data.get("project_path", "") or ""
            if isinstance(project_path_raw, str):
                project_path = project_path_raw.strip()
            else:
                project_path = str(project_path_raw).strip()
            self._native_project_path = project_path or "."
            if project_path and project_path != ".":
                self.communicate_with_handling(
                    input=f"cd {shlex.quote(project_path)}",
                    error_msg="Failed to switch to ArkTS app root inferred from defect files",
                    timeout_duration=LONG_TIMEOUT,
                )
                self.logger.info("ArkTS native mode: changed working directory to app root %s", project_path)
            else:
                self.logger.info("ArkTS native mode: using current directory as app root (%s)", project_path or ".")
            self.communicate_with_handling(
                input=(
                    f"export MSWE_PROJECT_PATH={shlex.quote(project_path or '.')}; "
                    "export MSWE_NATIVE_REPO_ROOT=\"$(git rev-parse --show-toplevel 2>/dev/null || pwd)\"; "
                    "export MSWE_PROJECT_ROOT=\"$(pwd)\""
                ),
                error_msg="Failed to export ArkTS project path metadata",
                timeout_duration=LONG_TIMEOUT,
            )

        raw_defect_files_for_env = self.record.data.get("defect_files", [])
        if isinstance(raw_defect_files_for_env, list):
            defect_files_json = json.dumps([str(x) for x in raw_defect_files_for_env])
        else:
            defect_files_json = "[]"
        self._native_defect_files_json = defect_files_json
        self.communicate_with_handling(
            input=f"export MSWE_DEFECT_FILES_JSON={shlex.quote(defect_files_json)}",
            error_msg="Failed to export defect_files JSON for repair_status",
            timeout_duration=LONG_TIMEOUT,
        )

        if not self.native_mode:
            # pre-install dependencies for swe-agent ACI tools
            for cmd in [
                "apt-get update",
                'apt-get install -y jq'
            ]:
                self.communicate_with_handling(
                    input=cmd,
                    error_msg="Failed to install",
                    timeout_duration=LONG_TIMEOUT,
                    no_output_timeout_duration=LONG_TIMEOUT
                )

        # update .gitignore files (language from specify_languages(); see env/utils.py LANGUAGE_MAP)
        lang = self.record.language
        if self.native_mode:
            lang = "arkts"
        elif lang is None:
            self.logger.warning(
                "record.language is None; using typescript gitignore script. "
                "Add your dataset filename pattern to specify_languages() if this is ArkTS or another stack.",
            )
            lang = "typescript"
            
        if not self.native_mode:
            source_file = Path(REPO_ROOT) / "multi_swe_bench" / "utils" / "gitignores" / f"{lang}.sh"
            copy_anything_to_container(self.container_obj, str(source_file), "/home/ignore.sh")
            self.communicate('chmod +x /home/ignore.sh')
            self.communicate_with_handling(
                input="bash ../ignore.sh",
                error_msg="Failed to source ignore files"
            )

        # Huawei CLI (bind-mounted at /opt/command-line-tools): OHPM_HOME, SDK, bundled node, PATH,
        # optional `ohpm config set registry` — see utils.arkts_container_huawei_cli_env_bash / keys.cfg.
        if lang == "arkts" and not self.native_mode:
            self.logger.info("ArkTS: applying Huawei CLI env (OHPM_HOME, DEVECO_*, PATH, ohpm registry)")
            self.communicate_with_handling(
                input=arkts_container_huawei_cli_env_bash(),
                error_msg="Failed to export HarmonyOS command-line-tools PATH / ohpm",
            )
            # Hard guardrail: fail fast if SDK path mapping is not the expected container path.
            self.communicate_with_handling(
                input=(
                    "test -d /opt/command-line-tools/sdk && "
                    "test \"${DEVECO_SDK_HOME:-}\" = \"/opt/command-line-tools/sdk\" && "
                    "test \"${OHOS_SDK_HOME:-}\" = \"/opt/command-line-tools/sdk\" && "
                    "test \"${HOS_SDK_HOME:-}\" = \"/opt/command-line-tools/sdk\""
                ),
                error_msg=(
                    "ArkTS SDK env/path mismatch. Expected DEVECO_SDK_HOME/OHOS_SDK_HOME/HOS_SDK_HOME="
                    "/opt/command-line-tools/sdk and existing /opt/command-line-tools/sdk."
                ),
            )
        elif lang == "arkts" and self.native_mode:
            self.logger.info("ArkTS: applying native Harmony SDK adapter")
            self._apply_native_harmony_sdk_adapter(project_path or ".")

        # Reset environment variables
        for cmd in [
            'export CURRENT_FILE=""',
            "export CURRENT_LINE=0",
            "export SEARCH_RESULTS=()",
            "export SEARCH_FILES=()",
            "export SEARCH_INDEX=0",
        ]:
            self.communicate_with_handling(
                input=cmd,
                error_msg="Failed to reset environment variables",
            )

        if not self.native_mode:
            system = self.communicate("uname -s").strip().lower()
            arch = self.communicate("uname -m").strip().lower()
            if system == "linux" and arch == "x86_64":
                self.communicate_with_handling(
                    "apt update --allow-insecure-repositories ; apt install build-essential -y --allow-unauthenticated",
                    error_msg="Failed to install build-essential",
                    timeout_duration=LONG_TIMEOUT,
                )
    
            # remove the fix.patch if it exists
            self.communicate('rm /home/fix.patch')
        # Write any metadata to info if necessary
        return None, info

    def _apply_test_patch(self):
        """
        Apply test patch for oracle setting
        """
        path_to_patch = "test.patch"
        with open(path_to_patch, "w") as f:
            f.write(self.record.instance.pr.test_patch)
        if not self.native_mode:
            subprocess.run(
                f"docker cp {path_to_patch} {self.container_name}:/root/test.patch",
                shell=True,
                check=False,
            )
            self.communicate_with_handling(
                input="git apply /root/test.patch",
                error_msg="Failed to apply test patch correctly",
            )
        else:
            # We are already in the correct directory in native bash
            # Copy patch to $HOME to avoid Git checking it in or ignoring it
            import shutil
            target_patch = Path("~").expanduser() / "test.patch"
            shutil.copy(path_to_patch, target_patch)
            self.communicate_with_handling(
                input=f"git apply '{target_patch.as_posix()}'",
                error_msg="Failed to apply test patch correctly",
            )
            # Cannot remove immediately if not needed here, but fine we copied it
            
        os.remove(path_to_patch)

    def step(self, action: str) -> tuple[str | None, int, bool, dict]:
        """
        Runs given action in environment and returns corresponding output

        Args:
            action: command to run in bash shell

        Returns:
            observation:  output from container
            reward: value between 0 and 1 quantifying correctness of output + environment state
            done: whether task is over
            info: additional information (e.g. debugging information)
        """
        info = {}

        observation = ""
        # Handle special actions
        if action.strip() == "skip":
            observation = "Skipped"
            info["exit_status"] = "skipped"
            return observation, 0, True, info
        if action == "exit_error":
            observation = "Exited (runtime error, no autosubmission)"
            info["exit_status"] = "exit_error_no_autosubmit"
            self.logger.warning("Exiting on runtime error without autosubmission")
            return observation, 0, True, info

        if action in {"exit_context", "exit_cost", "exit_format", "exit_api"}:
            try:
                observation = self.communicate(input="submit", timeout_duration=AGENT_ACTION_TIMEOUT)
                submission = self.get_submission(observation, action="submit")
                assert submission is not None and submission.strip() != "", AssertionError("No submission found.")
                self.logger.info(f"Found submission: {submission}")
                info["exit_status"] = f"submitted ({action})"
                info["submission"] = submission
                observation = "Exited (autosubmitted)"
                self.logger.info("Exiting with autosubmission")
                return observation, 0, True, info
            except ValueError as exc:
                observation = f"Exited ({action}; invalid autosubmission: {exc})"
                info["exit_status"] = action
                return observation, 0, True, info
            except KeyboardInterrupt:
                raise
            except:
                observation = "Exited"
                info["exit_status"] = action
                return observation, 0, True, info
            
        command_feedback = _agent_command_format_feedback(action)
        if command_feedback:
            info["command_format_error"] = True
            return command_feedback, 0, False, info

        # do action hacking
        action = action_hacking(action)
        is_build_action = self.native_mode and _is_long_running_build_action(action)
        build_exit_code: int | None = None
        action_timeout = LONG_TIMEOUT
        action_no_output_timeout = AGENT_ACTION_NO_OUTPUT_TIMEOUT
        if _is_long_running_build_action(action):
            action_timeout = max(LONG_TIMEOUT, BUILD_ACTION_TIMEOUT)
            action_no_output_timeout = max(AGENT_ACTION_NO_OUTPUT_TIMEOUT, BUILD_ACTION_NO_OUTPUT_TIMEOUT)

        # Attempt to run action in container
        observation = ""
        permit = native_build_permit() if is_build_action else nullcontext()
        try:
            with permit:
                try:
                    observation = self.communicate(
                        input=action,
                        timeout_duration=action_timeout,
                        no_output_timeout_duration=action_no_output_timeout,
                    )
                    build_exit_code = self.returncode
                    info["action_exit_code"] = self.returncode
                except TimeoutError as e:
                    try:
                        observation += e.args[1] if len(e.args) > 1 else ""
                        observation += self.interrupt()
                        observation += "\nEXECUTION TIMED OUT"
                        observation += (
                            f" BECAUSE NO OUTPUT WAS PRODUCED FOR MORE THAN {action_no_output_timeout:g} SECONDS.\nPLEASE REFINE YOUR RUNNING COMMAND SO IT WILL PRODUCE OUTPUT IN THE SPECIFIED TIME FRAME."
                        )
                        if self._restart_native_shell_after_failure("timeout"):
                            observation += "\nNative shell was restarted; continue with a narrower command."
                            self.returncode = 1
                            info["action_exit_code"] = 1
                            info["native_shell_restarted"] = True
                            return observation, 0, False, info
                    except RuntimeError as e:
                        observation += e.args[1] if len(e.args) > 1 else ""
                        observation += "\nEXECUTION TIMED OUT AND INTERRUPT FAILED."
                        if self._restart_native_shell_after_failure("timeout_interrupt_failed"):
                            observation += "\nNative shell was restarted; continue with a narrower command."
                            self.returncode = 1
                            info["action_exit_code"] = 1
                            info["native_shell_restarted"] = True
                            return observation, 0, False, info
                        info["action_exit_code"] = 1
                        info["exit_status"] = "early_exit"
                        self.logger.warning(f"Failed to interrupt container: {e}\n")
                        self.close()
                        return observation, 0, True, info
                    self.returncode = 1
                    info["action_exit_code"] = 1
                    info["exit_status"] = "early_exit"
                    self.close()
                    return observation, 0, True, info
                except RuntimeError as e:
                    observation += "\nCOMMAND FAILED TO EXECUTE."
                    if self._restart_native_shell_after_failure(str(e)):
                        observation += "\nNative shell was restarted after command failure; continue with the exact tool syntax and a narrower target."
                        self.returncode = 1
                        info["action_exit_code"] = 1
                        info["native_shell_restarted"] = True
                        return observation, 0, False, info
                    info["action_exit_code"] = 1
                    info["exit_status"] = "early_exit"
                    self.logger.warning(f"Failed to execute command: {e}\n.")
                    self.close()
                    return observation, 0, True, info
                except BrokenPipeError as e:
                    observation += "\nBROKEN PIPE ERROR."
                    if self._restart_native_shell_after_failure("broken_pipe"):
                        observation += "\nNative shell was restarted after broken pipe; continue with a narrower command."
                        self.returncode = 1
                        info["action_exit_code"] = 1
                        info["native_shell_restarted"] = True
                        return observation, 0, False, info
                    info["action_exit_code"] = 1
                    info["exit_status"] = "early_exit"
                    self.logger.error(f"Broken pipe error: {e}\n")
                    self.close()
                    return observation, 0, True, info
                except Exception:
                    observation += "\nEXECUTION FAILED OR COMMAND MALFORMED"
                    self.returncode = 1
                    info["action_exit_code"] = 1
                    self.logger.exception("Unknown exception")
                    return observation, 0, False, info
        except TimeoutError as e:
            self.returncode = 1
            info["action_exit_code"] = 1
            return f"BUILD PERMIT WAIT TIMED OUT: {e}", 0, False, info

        if is_build_action and build_exit_code is not None:
            observation = observation.rstrip() + f"\nBUILD_ACTION_EXIT_CODE={build_exit_code}"
            try:
                raw_defect_files = self.record.data.get("defect_files", [])
                if not isinstance(raw_defect_files, list):
                    raise ValueError("runtime defect_files is not a list")
                tree_sha256 = defect_tree_sha256(self.native_workdir, [str(path) for path in raw_defect_files])
                observation += f"\nBUILD_TREE_SHA256={tree_sha256}"
            except Exception as exc:
                observation += f"\nBUILD_TREE_HASH_ERROR={type(exc).__name__}: {exc}"

        # truncate observation, in case some test information is too large
        if len(observation) > 40000:
            observation = observation[:20000] + "..." + observation[-20000:]

        # Strip ANSI escape codes so the LLM receives clean text instead of
        # terminal color/style sequences (e.g. \x1b[31m) which appear as garbage.
        observation = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', observation)

        # Record submission and end episode if `submit` keyword found
        try:
            submission = self.get_submission(observation, action=action)
        except ValueError as exc:
            rejection = (
                "SUBMIT_REJECTED_INVALID_PATCH\n"
                "The submit command produced a model.patch that is not a clean UTF-8 text patch.\n"
                f"{exc}\n"
                "Regenerate the diff from the repository bytes instead of stdout or repair the contaminated source file."
            )
            return rejection, 0, False, info
        if submission is not None:
            raw_submission = submission
            submit_status = self._repair_status_from_patch(raw_submission)
            defect_files = self._defect_files_for_status()
            if defect_files:
                filtered_submission = filter_submission_to_defect_files(raw_submission, defect_files)
                filtered_status = self._repair_status_from_patch(filtered_submission)
                outside_dropped = submit_status.outside_defect_code_files if submit_status else []
                if filtered_submission != raw_submission:
                    info["outside_defect_code_files_dropped"] = outside_dropped
                    info["submission_filter"] = {
                        "mode": "defect_files_only",
                        "raw_chars": len(raw_submission),
                        "filtered_chars": len(filtered_submission),
                    }
                    self.logger.info(
                        "Filtered submission to KNOWN DEFECT FILES only (%d -> %d chars); dropped outside files: %s",
                        len(raw_submission),
                        len(filtered_submission),
                        outside_dropped,
                    )
                if filtered_status and filtered_status.has_defect_code_files and not filtered_submission.strip():
                    rejection_status = submit_status or filtered_status
                    rejection = (
                        "SUBMIT_REJECTED_NO_DEFECT_FILE_CHANGES\n"
                        "The submitted patch has no eligible changes in KNOWN DEFECT FILES after filtering outside diffs.\n"
                        "Edit every KNOWN DEFECT FILE, then run build/test gates and submit again.\n\n"
                        + format_repair_status(rejection_status)
                    )
                    return rejection, 0, False, info
                if filtered_status and filtered_status.has_unmodified_defect_code:
                    info["unmodified_defect_code_files"] = filtered_status.unmodified_defect_code_files
                    self.logger.info(
                        "Submission leaves %d KNOWN DEFECT FILES unchanged; accepting filtered patch.",
                        len(filtered_status.unmodified_defect_code_files),
                    )
                submission = filtered_submission
            elif submit_status and submit_status.has_outside_defect_code:
                rejection = (
                    "SUBMIT_REJECTED_OUTSIDE_DEFECT_CODE\n"
                    "The submitted patch modifies .ets/.ts files outside KNOWN DEFECT FILES.\n"
                    "Move the repair into KNOWN DEFECT FILES or revert outside-defect code edits, then run tests and submit again.\n\n"
                    + format_repair_status(submit_status)
                )
                return rejection, 0, False, info
            forbidden = find_arkts_forbidden_added_syntax(submission)
            if forbidden:
                rejection = (
                    "SUBMIT_REJECTED_ARKTS_SYNTAX\n"
                    f"The submitted .ets patch introduces forbidden ArkTS syntax: {forbidden}.\n"
                    "Replace that construct with ArkTS-compatible syntax, then submit again."
                )
                return rejection, 0, False, info
            self.logger.info(f"Found submission: {submission}")
            info["exit_status"] = "submitted"
            info["submission"] = submission if submission.strip() != "" else None
            observation = submission if submission.strip() != "" else None
            return observation, 0, True, info
        status_note = self._format_current_repair_status(
            build_success_note=_is_build_success_observation(observation)
        )
        if status_note:
            observation = (observation.rstrip() + "\n\n" + status_note).strip()
        return observation, 0, False, info

    def _defect_files_for_status(self) -> list[str]:
        raw = self.record.data.get("defect_files", [])
        if not isinstance(raw, list):
            return []
        return [str(x) for x in raw if str(x).strip()]

    def _allow_test_patch(self) -> bool:
        return self.record.data.get("allow_test_patch") is True

    def _repair_status_from_patch(self, patch_text: str):
        defect_files = self._defect_files_for_status()
        if not defect_files:
            return None
        return compute_repair_status(
            defect_files,
            changed_files_from_patch(patch_text),
            allow_test_patch=self._allow_test_patch(),
        )

    def _format_current_repair_status(self, *, build_success_note: bool = False) -> str:
        defect_files = self._defect_files_for_status()
        if not defect_files:
            return ""
        try:
            changed_output = self.communicate(
                input=(
                    "git diff --name-only -- && "
                    "git diff --cached --name-only -- && "
                    "git ls-files --others --exclude-standard"
                ),
                timeout_duration=AGENT_ACTION_TIMEOUT,
            )
        except Exception:
            self.logger.warning("Failed to collect git changed files for repair_status", exc_info=True)
            return ""
        changed_files = [line.strip() for line in changed_output.splitlines() if line.strip()]
        status = compute_repair_status(
            defect_files,
            changed_files,
            allow_test_patch=self._allow_test_patch(),
        )
        return format_repair_status(status, build_success_note=build_success_note)

    def close(self) -> None:
        """
        Handle environment shutdown
        """
        self.logger.info("Beginning environment shutdown...")
        if self.container is not None and getattr(self, "native_mode", False):
            try:
                terminate_process_tree(self.container)
            finally:
                self._cleanup_native_commands_dir(self.container)
        else:
            try:
                self.communicate(input="exit")
            except KeyboardInterrupt:
                raise
            except Exception:
                self.logger.warning("Errors when exiting container", exc_info=True)
            if self.container is not None:
                self.container.terminate()
        if self.container_obj is None:
            pass
        elif self.persistent:
            # stopping is Podman specific, but doesn't hurt to include
            # https://stackoverflow.com/a/32428199/
            # Sleeping to avoid https://github.com/princeton-nlp/SWE-agent/issues/496 ??
            time.sleep(0.1)
            if self.container_obj.status not in {"paused", "exited", "dead", "stopping"}:
                try:
                    self.container_obj.pause()
                except Exception:
                    self.logger.warning("Failed to pause container.", exc_info=True)
                except KeyboardInterrupt:
                    raise
                else:
                    self.logger.info("Agent container paused")
            else:
                self.logger.info(f"Agent container status: {self.container_obj.status}")
        else:
            try:
                self.container_obj.remove(force=True)
            except KeyboardInterrupt:
                raise
            except Exception:
                self.logger.warning("Failed to remove container", exc_info=True)
            else:
                self.logger.info("Agent container stopped")
        for hook in self.hooks:
            hook.on_close()

    @staticmethod
    def _cleanup_native_commands_dir(process) -> None:
        commands_dir = getattr(process, "arkfix_commands_dir", None)
        if commands_dir:
            shutil.rmtree(Path(commands_dir), ignore_errors=True)
            process.arkfix_commands_dir = None

    # MARK: Helper functions #

    def _build_image(self) -> None:
        if getattr(self, "native_mode", False):
            return
        instance = self.record.instance
        self.logger.info(f"Building image for {instance.name()}")
        build_image(
            instance.dependency(),
            cli=self.args.cli_args,
            logger=self.logger
        )
        

    def _reset_native(self, instance_id) -> None:
        from sweagent.environment.utils import get_native_shell

        self._native_shell_requires_agent_init = False
        self._native_harmony_export_command = ""
        
        # Determine paths
        from pathlib import Path
        repo_name = self._repo_name
        mswe_agent_root = Path(__file__).resolve().parents[2] # Root of MSWE-agent
        repos_base_dir = getattr(self.args, "repos_base_dir", "")
        if not repos_base_dir:
            # `--repo_dir` lands in cli_args.repo_dir; use it as the native source root.
            repos_base_dir = getattr(getattr(self.args, "cli_args", None), "repo_dir", "")

        if repos_base_dir:
            repo_dir = Path(repos_base_dir).resolve() / repo_name
        else:
            repo_dir = mswe_agent_root.parent / repo_name

        # Per local-repo workflow, native mode must start directly in <repo_dir>/<repo>.
        if repo_dir.is_dir():
            workdir = repo_dir
        else:
            msg = (
                f"Local repo source directory not found: {repo_dir}. "
                f"Expected path: <repo_dir>/<repo> where repo={repo_name}."
            )
            self.logger.error(msg)
            raise FileNotFoundError(msg)
            
        self.native_workdir = workdir
        self.container, self.parent_pids = get_native_shell(workdir)
        self.container_obj = None
        self.container_name = f"native-{instance_id}"
        self.logger.info("Native Environment Initialized")

    def _restore_native_shell_context(self) -> None:
        if not getattr(self, "native_mode", False):
            return

        project_path = getattr(self, "_native_project_path", ".") or "."
        raw_defect_files = getattr(self.record, "data", {}).get("defect_files", []) if self.record is not None else []
        if isinstance(raw_defect_files, list):
            defect_files_json = json.dumps([str(x) for x in raw_defect_files])
        else:
            defect_files_json = getattr(self, "_native_defect_files_json", "[]")
        self._native_defect_files_json = defect_files_json

        if project_path and project_path != ".":
            self.communicate_with_handling(
                input=f"cd {shlex.quote(project_path)}",
                error_msg="Failed to restore ArkTS app root after native shell restart",
                timeout_duration=LONG_TIMEOUT,
            )
        self.communicate_with_handling(
            input=(
                f"export MSWE_PROJECT_PATH={shlex.quote(project_path or '.')}; "
                "export MSWE_NATIVE_REPO_ROOT=\"$(git rev-parse --show-toplevel 2>/dev/null || pwd)\"; "
                "export MSWE_PROJECT_ROOT=\"$(pwd)\"; "
                f"export MSWE_DEFECT_FILES_JSON={shlex.quote(defect_files_json)}; "
                'export CURRENT_FILE=""; export CURRENT_LINE=0; '
                "export SEARCH_RESULTS=(); export SEARCH_FILES=(); export SEARCH_INDEX=0"
            ),
            error_msg="Failed to restore native repair shell metadata",
            timeout_duration=LONG_TIMEOUT,
        )
        self._restore_native_harmony_sdk_environment()

    def _restart_native_shell_after_failure(self, reason: str) -> bool:
        if not getattr(self, "native_mode", False):
            return False
        from sweagent.environment.utils import get_native_shell

        native_root = getattr(self, "native_workdir", None)
        if not isinstance(native_root, Path):
            return False
        old_process = self.container
        try:
            if old_process is not None:
                terminate_process_tree(old_process)
                if old_process.poll() is None:
                    raise RuntimeError("broken native shell is still running")
        except Exception:
            self.logger.warning("Failed to kill broken native shell before restart", exc_info=True)
            return False
        if old_process is not None:
            self._cleanup_native_commands_dir(old_process)
        try:
            self.logger.warning("Restarting native shell after command failure: %s", reason)
            self.container, self.parent_pids = get_native_shell(native_root)
            self._init_scripts()
            self._restore_native_shell_context()
            self._native_shell_requires_agent_init = True
            self.returncode = 1
            return True
        except Exception:
            replacement = self.container
            if replacement is not None and replacement is not old_process:
                try:
                    terminate_process_tree(replacement)
                finally:
                    self._cleanup_native_commands_dir(replacement)
            self.logger.warning("Failed to restart native shell after command failure", exc_info=True)
            return False






    def _exclude_native_environment_paths(self, project_dir: Path) -> None:
        """Hide generated native-preprocess artifacts from git status."""

        native_root = getattr(self, "native_workdir", None)
        if not isinstance(native_root, Path):
            return
        git_info = native_root / ".git" / "info"
        if not git_info.is_dir():
            return

        patterns: list[str] = []
        for path in (
            project_dir / "local.properties",
            project_dir / "oh_modules",
            project_dir / ".hvigor",
        ):
            try:
                patterns.append(path.resolve().relative_to(native_root.resolve()).as_posix())
            except ValueError:
                patterns.append(path.name)
        for pattern in ("build/", "*/build/", "oh_modules/", "*/oh_modules/", ".hvigor/", "*/.hvigor/"):
            patterns.append(pattern)

        exclude_path = git_info / "exclude"
        existing = exclude_path.read_text(encoding="utf-8", errors="replace") if exclude_path.is_file() else ""
        existing_lines = {line.strip() for line in existing.splitlines()}
        missing = [pattern for pattern in patterns if pattern and pattern not in existing_lines]
        if not missing:
            return
        with exclude_path.open("a", encoding="utf-8", newline="\n") as fp:
            if existing and not existing.endswith("\n"):
                fp.write("\n")
            for pattern in missing:
                fp.write(f"{pattern}\n")

    def _record_native_preprocess_baseline_tree(self, native_root: Path) -> str:
        """Record the post-preprocess tree so submit excludes environment shims."""

        def run_git(args: list[str]) -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(
                ["git", "-C", str(native_root), *args],
                capture_output=True,
                text=False,
                timeout=120,
                check=False,
            )

        pathspecs = []
        skipped: list[str] = []
        for pathspec in self._native_defect_file_pathspecs():
            tracked = run_git(["ls-files", "--error-unmatch", "--", pathspec])
            exists = (native_root / pathspec).exists()
            if tracked.returncode == 0 or exists:
                pathspecs.append(pathspec)
            else:
                skipped.append(pathspec)
        if skipped:
            self.logger.warning(
                "Leaving %d missing defect_files out of the preprocess baseline; they remain valid creation targets: %s",
                len(skipped),
                ", ".join(skipped),
            )
        if pathspecs:
            add = run_git(["add", "-A", "--", *pathspecs])
            if add.returncode != 0:
                message = (add.stderr or add.stdout or b"").decode("utf-8", errors="replace")
                raise RuntimeError(f"failed to stage native preprocess baseline: {message.strip()}")
        else:
            self.logger.warning("No defect_files pathspecs; native preprocess baseline uses current index only.")
        tree = run_git(["write-tree"])
        reset = run_git(["reset", "-q"])
        if reset.returncode != 0:
            message = (reset.stderr or reset.stdout or b"").decode("utf-8", errors="replace")
            raise RuntimeError(f"failed to unstage native preprocess baseline: {message.strip()}")
        if tree.returncode != 0:
            message = (tree.stderr or tree.stdout or b"").decode("utf-8", errors="replace")
            raise RuntimeError(f"failed to record native preprocess baseline tree: {message.strip()}")
        tree_hash = tree.stdout.decode("ascii", errors="replace").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", tree_hash):
            raise RuntimeError(f"invalid native preprocess baseline tree hash: {tree_hash!r}")
        self.logger.info("ArkTS native preprocess baseline tree: %s", tree_hash)
        return tree_hash

    def _apply_native_harmony_sdk_adapter(self, project_path: str) -> None:
        """Prepare DevEco/SDK env after checkout-to-base and before agent repair starts."""

        if not self.native_mode:
            return
        if os.environ.get("ARKFIX_PATCH_ONLY_GENERATION", "").strip() == "1":
            self.logger.info("ArkTS native preprocess deferred to serial apply-check for patch-only generation")
            return
        native_root = getattr(self, "native_workdir", None)
        if not isinstance(native_root, Path):
            self.logger.warning("ArkTS SDK adapter skipped: native workdir is unavailable.")
            return

        project_rel = (project_path or ".").strip() or "."
        project_dir = native_root if project_rel == "." else (native_root / project_rel)
        try:
            from tools.common import (
                apply_vpn_extension_api20_profile_adapter,
                build_deveco_tool_env,
                ensure_local_properties,
                find_deveco_sdk_roots,
                find_ohpm,
                find_hvigor_wrapper,
                prepare_native_repair_environment,
                require_sdk_roots_for_repo,
                resolve_deveco_path_setting,
                resolve_sdk_api_slice_for_api,
            )

            deveco_path = resolve_deveco_path_setting()
            if not deveco_path:
                raise RuntimeError("DEVECO_PATH missing; set tools/.env DEVECO_PATH.")

            profile_adapter_notes = apply_vpn_extension_api20_profile_adapter(
                project_dir,
                find_deveco_sdk_roots(deveco_path),
                product_name="default",
            )
            sdk_roots, sdk_meta = require_sdk_roots_for_repo(project_dir, deveco_path, product_name="default")
            sdk_root = sdk_roots[0]
            sdk_api_level = sdk_meta.get("sdk_selection_api_level")
            tool_env = build_deveco_tool_env(deveco_path, sdk_root=sdk_root)
            local_properties = ensure_local_properties(
                project_dir,
                sdk_root=sdk_root,
                sdk_api_level=sdk_api_level,
                base_env=tool_env,
            )
            self._exclude_native_environment_paths(project_dir)
            with native_build_permit():
                prepare_notes = prepare_native_repair_environment(
                    project_dir,
                    deveco_path,
                    product_name="default",
                    timeout_sec=900,
                    run_ohpm=True,
                )
            for note in [*profile_adapter_notes, *prepare_notes]:
                self.logger.info("ArkTS native preprocess: %s", note)
            native_env_base_tree = self._record_native_preprocess_baseline_tree(native_root)
            self._native_env_base_tree = native_env_base_tree

            deveco_dir = Path(deveco_path).expanduser().resolve()
            node_home = Path(tool_env["NODE_HOME"]).expanduser().resolve() if tool_env.get("NODE_HOME") else None
            java_home = deveco_dir / "jbr"
            ohpm = find_ohpm(deveco_dir)
            hvigor = find_hvigor_wrapper(project_dir, deveco_dir)
            path_prefixes = [
                path
                for path in (
                    hvigor.parent if hvigor else None,
                    ohpm.parent if ohpm else None,
                    node_home,
                    java_home / "bin" if java_home.is_dir() else None,
                )
                if isinstance(path, Path) and path.exists()
            ]

            sdk_api_slice = resolve_sdk_api_slice_for_api(sdk_root, sdk_api_level)
            exports = [
                f"export DEVECO_HOME={_quote_git_bash_path(deveco_dir)}",
                f"export DEVECO_PATH={_quote_git_bash_path(deveco_dir)}",
                f"export DEVECO_SDK_HOME={_quote_git_bash_path(sdk_root)}",
                f"export OHOS_BASE_SDK_HOME={_quote_git_bash_path(sdk_root)}",
                f"export OHOS_SDK_HOME={_quote_git_bash_path(sdk_root)}",
                f"export HOS_SDK_HOME={_quote_git_bash_path(sdk_root)}",
                f"export OPENHARMONY_SDK_PATH={_quote_git_bash_path(sdk_api_slice)}",
                f"export MSWE_NATIVE_ENV_BASE_TREE={shlex.quote(native_env_base_tree)}",
            ]
            if java_home.is_dir():
                exports.append(f"export JAVA_HOME={_quote_git_bash_path(java_home)}")
            if node_home:
                exports.append(f"export NODE_HOME={_quote_git_bash_path(node_home)}")
                exports.append("export DEVECO_NODE_HOME=\"$NODE_HOME\"")
            if ohpm:
                exports.append(f"export OHPM_HOME={_quote_git_bash_path(ohpm.parent.parent)}")
            if path_prefixes:
                path_value = ":".join(_git_bash_path(path) for path in path_prefixes)
                exports.append(f"export PATH={shlex.quote(path_value)}:$PATH")

            export_command = " && ".join(exports)
            self._native_harmony_export_command = export_command
            self.communicate_with_handling(
                input=export_command,
                error_msg="Failed to export native Harmony SDK adapter environment",
                timeout_duration=LONG_TIMEOUT,
            )
            self.logger.info(
                "ArkTS SDK adapter configured: project=%s compileSdk=%s compatibleSdk=%s selectedApi=%s sdkRoot=%s localProperties=%s",
                project_dir,
                sdk_meta.get("compileSdkVersion"),
                sdk_meta.get("compatibleSdkVersion"),
                sdk_api_level,
                sdk_root,
                local_properties,
            )
        except Exception as exc:
            self.logger.error("ArkTS SDK adapter failed for project %s: %s", project_dir, exc)
            raise

    def _restore_native_harmony_sdk_environment(self) -> None:
        export_command = getattr(self, "_native_harmony_export_command", "")
        if not export_command:
            raise RuntimeError("native Harmony SDK environment was not initialized before shell restart")
        self.communicate_with_handling(
            input=export_command,
            error_msg="Failed to restore native Harmony SDK environment",
            timeout_duration=LONG_TIMEOUT,
        )

    def _reset_container(self, instance_id) -> None:
        if self.container is not None:
            try:
                if getattr(self, "native_mode", False):
                    terminate_process_tree(self.container)
                else:
                    self.container.terminate()
            except KeyboardInterrupt:
                raise
            except:
                self.logger.warning("Failed to terminate container", exc_info=True)
            else:
                self.logger.debug("Terminated container")
            finally:
                if getattr(self, "native_mode", False):
                    self._cleanup_native_commands_dir(self.container)
                
        if getattr(self, "native_mode", False):
            self._reset_native(instance_id)
            self._init_scripts()
            return
            
        image_full_name = self.record.instance.name()
        self._init_container(image_full_name)
        self._init_scripts()

    def reset_container(self, instance_id) -> None:
        self.close()
        self.container = None
        self.container_obj = None
        self._reset_container(instance_id)

    @staticmethod
    def _get_container_name(image_name: str) -> str:
        """Return name of container"""
        process_id = str(os.getpid())
        current_time = str(datetime.datetime.now())
        unique_string = current_time + process_id
        hash_object = hashlib.sha256(unique_string.encode())
        image_name_sanitized = image_name.replace("/", "-")
        image_name_sanitized = image_name_sanitized.replace(":", "-")
        return f"{image_name_sanitized}-{hash_object.hexdigest()[:10]}"

    def _init_container(self, cached_image: str | None = None) -> None:
        """
        Handles container initialization. Defines container name and creates it.
        If cached_image is provided, it will use that image name instead of the default.
        """
        image_name = self.image_name
        if cached_image is not None:
            image_name = cached_image
            self.logger.info(f"Using cached image: {image_name}")
        if self.persistent:
            assert self.container_name is not None
        else:
            # Make sure that we get a new container name just in case removing didn't work.
            # Might be a fix for https://github.com/princeton-nlp/SWE-agent/issues/451
            self.container_name = self._get_container_name(image_name)
        host_cli = resolve_command_line_tools_host_path(
            repo_key=(
                f"{self.record.instance.pr.org}/{self.record.instance.pr.repo}"
                if self.record is not None
                else None
            ),
        )
        self.container, self.parent_pids = get_container(
            self.container_name,
            image_name,
            persistent=self.persistent,
            command_line_tools_host_path=host_cli,
        )
        try:
            client = docker.from_env(timeout=600)
        except docker.errors.DockerException as e:
            if "Error while fetching server API version" in str(e):
                msg = "Docker is not running. Please start Docker and try again."
            else:
                msg = "Unknown docker exception occurred. Are you sure docker is running?"
            raise RuntimeError(msg) from e
        t0 = time.time()
        self.container_obj = None
        while time.time() - t0 < 60:
            try:
                self.container_obj = client.containers.get(self.container_name)
            except docker.errors.NotFound:
                self.logger.debug("Couldn't find container. Let's wait and retry.")
                time.sleep(1)
            else:
                break
        else:
            print(f"{self.persistent=}")
            available_containers = client.containers.list(all=True)
            available_containers_info = json.dumps([str(c.attrs) for c in available_containers], indent=2)
            print(available_containers_info)
            msg = "Failed to get container object."
            raise RuntimeError(msg)
        self.logger.info(" Environment Initialized")

    def _init_scripts(self):
        """
        Initialize custom commands within container
        """
        if getattr(self, "native_mode", False):
            self.communicate_with_handling(
                'if [ -z "${SWE_AGENT_COMMANDS_DIR:-}" ]; then '
                'export SWE_AGENT_COMMANDS_DIR="$(mktemp -d "${TMPDIR:-/tmp}/swe-agent-commands.XXXXXX")"; fi',
                error_msg="Failed to create isolated native commands directory",
            )
            cmd_dir = '"$SWE_AGENT_COMMANDS_DIR"'
        else:
            cmd_dir = "/root/commands"
        self.communicate_with_handling(
            f"mkdir -p {cmd_dir}",
            error_msg="Failed to create commands directory",
        )
        self.communicate_with_handling(
            f"touch {cmd_dir}/__init__.py",
            error_msg="Failed to create __init__.py",
        )
        self.communicate_with_handling(
            f"export PATH={cmd_dir}:$PATH",
            error_msg="Failed to add commands directory to PATH",
        )

    def _communicate_experimental(
        self,
        input: str,
        timeout_duration=25,
        no_output_timeout_duration: int | float = 25,
    ) -> str:
        """Experimental version of `_communicate`"""
        assert self.container is not None
        command_suffix = f"echo {PROCESS_DONE_MARKER_START}$?{PROCESS_DONE_MARKER_END}\n"
        try:
            self.returncode = None
            cmd = input if input.endswith("\n") else input + "\n"
            cmd += command_suffix
            os.write(self.container.stdin.fileno(), cmd.encode())
            time.sleep(0.03)
            self.container.stdin.flush()
        except BrokenPipeError:
            traceback.print_exc()
            self.logger.error("Failed to communicate with container. Check docker logs for more information.")
            msg = "Failed to communicate with container"
            raise RuntimeError(msg)

        buffer, exit_code = read_with_timeout_experimental(self.container, timeout_duration, no_output_timeout_duration)
        self.returncode = int(exit_code)
        return buffer

    def _communicate(
        self,
        input: str,
        timeout_duration=25,
        no_output_timeout_duration: int | float = 25,
    ) -> str:
        assert self.container is not None
        communicate_method = keys_config.get(
            "SWE_AGENT_COMMUNICATE_METHOD", default="end-marker", choices=["end-marker", "processes"]
        )
        if communicate_method == "end-marker":
            return self._communicate_experimental(input, timeout_duration, no_output_timeout_duration)
        try:
            self.returncode = None
            cmd = input if input.endswith("\n") else input + "\n"
            os.write(self.container.stdin.fileno(), cmd.encode())
            time.sleep(0.1)
            self.container.stdin.flush()
        except BrokenPipeError:
            traceback.print_exc()
            self.logger.error("Failed to communicate with container. Check docker logs for more information.")
            msg = "Failed to communicate with container"
            raise RuntimeError(msg)
        try:
            buffer = read_with_timeout(self.container, self.get_pids, timeout_duration)
            self.container.stdin.write("echo $?\n")
            time.sleep(0.1)
            self.container.stdin.flush()
            exit_code = read_with_timeout(self.container, self.get_pids, 5).strip()
        except Exception as e:
            self.logger.error(f"Read with timeout failed on input:\n---\n{input}\n---")
            raise e
        if not exit_code.isdigit():
            msg = f"Container crashed. Failed to get exit code. Output:\n---\n{buffer}\n---"
            raise RuntimeError(msg)
        self.returncode = int(exit_code)
        return buffer

    def _check_syntax(self, input: str):
        """
        Saves environment variables to file
        """
        output = self._communicate(f"/bin/bash -n <<'EOF'\n{input}\nEOF\n")
        return output, self.returncode == 0

    def communicate(
        self,
        input: str,
        timeout_duration=25,
        no_output_timeout_duration: int | float | None = 25,
    ) -> str:
        """
        Sends input to container and returns output

        Args:
            input: input to send to container

        Returns:
            output: output from container
        """
        if input.strip() != "exit":
            output, valid = self._check_syntax(input)
            if not valid:
                return output  # shows syntax errors
            output = self._communicate(
                input,
                timeout_duration=timeout_duration,
                no_output_timeout_duration=no_output_timeout_duration
            )
            self.communicate_output = output
            return output
        else:
            if self.container is not None:
                if getattr(self, "native_mode", False):
                    terminate_process_tree(self.container)
                else:
                    self.container.terminate()
            self.returncode = 0
            self.communicate_output = ""
            return ""

    def communicate_with_handling(self, input: str, error_msg: str, timeout_duration=25, no_output_timeout_duration= 25, except_error_msgs = []) -> str:
        """
        Wrapper for communicate function that raises error if return code is non-zero

        Args:
            input: input to send to container
            error_msg: error message to raise if return code is non-zero
            timeout_duration: duration to wait for output

        Returns:
            output: output from container
        """
        logs = self.communicate(input, timeout_duration=timeout_duration, no_output_timeout_duration=no_output_timeout_duration)
        if self.returncode != 0:
            if any( caught_err in logs for caught_err in except_error_msgs):
                self.logger.warning(f'the error message is in exception, some adjustmens will be acted to the commands.')
                return logs
            self.logger.error(f"{error_msg}: {logs}")
            self.close()
            msg = f"{error_msg}: {logs}"
            raise RuntimeError(msg)
        return logs

    def get_available_actions(self) -> list[str]:
        """
        Returns list of available actions in current environment state

        Currently not in use.
        """
        return []

    def get_pids(self, all_pids=False) -> list[str]:
        """
        Gets list of processes running inside docker container

        Args:
            all_pids: whether to return all pids, or whether to exclude the main-process attached pid,
                and parent PIDs

        Returns:
            list of PIDs
        """
        pids = self.container_obj.exec_run("ps -eo pid,comm --no-headers").output.decode().split("\n")
        pids = [x.split() for x in pids if x]
        if not all_pids:
            pids = [x for x in pids if x[1] not in ["ps", 'npm', 'yarn', 'sh'] and x[0] not in self.parent_pids]
        return pids

    def _native_submission_patch_path(self) -> Path | None:
        if not getattr(self, "native_mode", False):
            return None
        native_root = getattr(self, "native_workdir", None)
        if not isinstance(native_root, Path):
            return None
        return native_root / "model.patch"

    def _native_cached_git_diff_submission(self) -> str | None:
        if not getattr(self, "native_mode", False):
            return None
        native_root = getattr(self, "native_workdir", None)
        if not isinstance(native_root, Path) or not native_root.is_dir():
            return None
        repo_check = subprocess.run(
            ["git", "-C", str(native_root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=False,
            timeout=30,
            check=False,
        )
        if repo_check.returncode != 0 or repo_check.stdout.strip() != b"true":
            return None
        diff_args = ["diff", "--cached", "--no-ext-diff", "--binary"]
        env_base_tree = str(getattr(self, "_native_env_base_tree", "") or "").strip()
        if env_base_tree:
            diff_args.append(env_base_tree)
        pathspecs = self._native_defect_file_pathspecs()
        if pathspecs:
            diff_args.extend(["--", *pathspecs])
        result = subprocess.run(
            ["git", "-C", str(native_root), *diff_args],
            capture_output=True,
            text=False,
            timeout=120,
            check=False,
        )
        if result.returncode != 0:
            message = _decode_utf8_submission_patch(
                result.stderr or result.stdout,
                f"git diff --cached stderr from {native_root}",
            )
            raise ValueError(f"failed to extract native staged git diff: {message.strip()}")
        if not result.stdout:
            return None
        try:
            return _decode_utf8_submission_patch(result.stdout, f"git diff --cached bytes from {native_root}")
        except ValueError:
            if not pathspecs:
                raise
            attributes_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    newline="\n",
                    prefix="arkfix_attributes_",
                    suffix=".txt",
                    delete=False,
                ) as handle:
                    attributes_path = Path(handle.name)
                    for path in pathspecs:
                        handle.write(f"{json.dumps(path, ensure_ascii=False)} binary\n")
                binary_result = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(native_root),
                        "-c",
                        f"core.attributesFile={attributes_path.as_posix()}",
                        *diff_args,
                    ],
                    capture_output=True,
                    text=False,
                    timeout=120,
                    check=False,
                )
                if binary_result.returncode != 0:
                    message = (binary_result.stderr or binary_result.stdout).decode("utf-8", errors="replace")
                    raise ValueError(f"failed to extract native binary git diff: {message.strip()}")
                return _decode_utf8_submission_patch(
                    binary_result.stdout,
                    f"binary git diff --cached bytes from {native_root}",
                )
            finally:
                if attributes_path is not None:
                    attributes_path.unlink(missing_ok=True)

    def _native_defect_file_pathspecs(self) -> list[str]:
        raw_files: list[str] = []
        if self.record is not None:
            raw = self.record.data.get("defect_files", [])
            if isinstance(raw, list):
                raw_files = [str(item) for item in raw if str(item).strip()]
        if not raw_files:
            return []

        def normalize(value: str) -> str:
            value = str(value or "").replace("\\", "/").strip()
            while value.startswith("./"):
                value = value[2:]
            parts: list[str] = []
            for part in value.split("/"):
                if not part or part == ".":
                    continue
                if part == "..":
                    if parts:
                        parts.pop()
                    continue
                parts.append(part)
            return "/".join(parts)

        out: list[str] = []
        seen: set[str] = set()
        for raw_path in raw_files:
            normalized = normalize(raw_path)
            if normalized and normalized not in seen:
                seen.add(normalized)
                out.append(normalized)
        return out

    def _should_read_native_submission_file(self, output: str, action: str | None = None) -> bool:
        if not getattr(self, "native_mode", False):
            return False
        if "<<SUBMISSION_FILE||" in output or "<<SUBMISSION||" in output:
            return True
        if action is None:
            return False
        first_line = (action or "").strip().splitlines()[0] if (action or "").strip() else ""
        try:
            parts = shlex.split(first_line, posix=True)
        except ValueError:
            return False
        return bool(parts and parts[0] == "submit")

    def get_submission(self, output: str, action: str | None = None) -> str | None:
        """
        Function for extracting diff patch submission at the end of an episode.

        Args:
            output: `submit` observation

        Returns:
            submission: diff patch submission
        """
        if self._should_read_native_submission_file(output, action=action):
            cached_diff = self._native_cached_git_diff_submission()
            if cached_diff is not None:
                return cached_diff
            patch_path = self._native_submission_patch_path()
            if patch_path is not None and patch_path.is_file():
                return _read_utf8_submission_patch(patch_path)

        file_pattern = r"\<\<SUBMISSION_FILE\|\|(.*?)\|\|SUBMISSION_FILE\>\>"
        if re.search(file_pattern, output, re.DOTALL) is not None:
            return None

        pattern = r"\<\<SUBMISSION\|\|(.*)\|\|SUBMISSION\>\>"
        match = re.search(pattern, output, re.DOTALL)
        if match is None:
            return None
        return match.group(1)

    def run_shell_script(self, script_path: Path, *, location: str) -> None:
        """Run custom script supplied by user at `script_path`

        Args:
            script_path: path to script file
            location: location of script file 'host' or 'container'
        """
        if location == "host":
            return self._run_shell_script_host(script_path)
        elif location == "container":
            raise NotImplementedError
        msg = f"Invalid 'location': {location}"
        raise ValueError(msg)

    def _run_shell_script_host(self, script_path: Path) -> None:
        """Run shell script file (located on host) in container"""
        if not script_path.is_file():
            msg = f"Script not found at {script_path}"
            raise FileNotFoundError(msg)
        shell_commands = Path(script_path).read_text().splitlines(keepends=True)
        for i, cmd in enumerate(shell_commands):
            self.communicate_with_handling(
                cmd,
                error_msg=f"Failed to execute line {i}.",
                timeout_duration=LONG_TIMEOUT,
            )

    def _get_install_configs(self) -> dict | None:
        """Return config for environment setup"""
        assert self.record is not None  # mypy
        if (
            self.record["problem_statement_source"] != "swe-bench" or self.record["repo_type"] == "local"
        ) and self.args.environment_setup is None:
            self.logger.warning(
                "install_environment is set to True, but the data path is a GitHub URL "
                "without an environment config file (environment_config key/flag). "
                "Skipping conda environment installation.",
            )
            return None
        if self.args.environment_setup is not None:
            assert isinstance(self.args.environment_setup, (str, os.PathLike))
            if Path(self.args.environment_setup).suffix in [".yml", ".yaml"]:
                try:
                    return yaml.safe_load(Path(self.args.environment_setup).read_text())
                except Exception as e:
                    msg = "Environment config file needs to be a yaml file"
                    raise ValueError(msg) from e
            elif Path(self.args.environment_setup).suffix == ".sh":
                return {
                    "shell_script_path": self.args.environment_setup,
                }
            else:
                msg = "Environment config file needs to be a yaml file or shell script"
                raise ValueError(msg)
        else:
            return None

    def _conda_environment_exists(self, env_name: str) -> bool:
        env_check = self.communicate(f"conda env list | grep {env_name}", timeout_duration=LONG_TIMEOUT)
        return env_check.strip() != ""

    def add_commands(self, commands: list[dict]) -> None:
        """
        Adds custom commands to container
        """
        for command in commands:
            name = command["name"]
            contents = command["contents"]
            cmd_dir = '"$SWE_AGENT_COMMANDS_DIR"' if getattr(self, "native_mode", False) else "/root/commands"
            
            if getattr(self, "native_mode", False):
                delimiter = "EOF_SWE_AGENT_SCRIPT_DELIMITER"
                self.communicate_with_handling(
                    f"cat << '{delimiter}' > {cmd_dir}/{name}\n{contents}\n{delimiter}",
                    error_msg=f"Failed to create {name}"
                )
            else:
                copy_file_to_container(self.container_obj, contents, f"{cmd_dir}/{name}")

            if command["type"] == "source_file":
                self.communicate_with_handling(
                    f"source {cmd_dir}/{name}",
                    error_msg=(
                        f"Failed to source {name}. If you meant to make a script,"
                        " start the file with a shebang (e.g. #!/usr/bin/env python)."
                    ),
                )
            elif command["type"] == "script":
                self.communicate_with_handling(
                    f"chmod +x {cmd_dir}/{name}",
                    error_msg=f"Failed to chmod {name}",
                )
            elif command["type"] == "utility":
                # nothing to do for utility scripts
                pass
            else:
                msg = f"Invalid command type: {command['type']}"
                raise ValueError(msg)

    def interrupt(self):
        """
        Send interrupt signal to container and exhaust stdout buffer with a communicate call
        """
        if getattr(self, "native_mode", False):
            if hasattr(self, "container") and self.container:
                terminate_process_tree(self.container)
            return "Interrupted."
            
        assert self.container is not None
        assert self.container_obj is not None
        pids = self.get_pids()
        for p in reversed(pids):
            # We need to avoid to kill the main process which is in the small pid
            pid = p[0]
            self.container_obj.exec_run(f"kill -9 {pid}")
        observation = ""
        try:
            observation += read_with_timeout(self.container, self.get_pids, 20)
        except TimeoutError:
            pass
        try:
            # This is a workaround because of bash behaviour
            # when sometimes we get the prints of Killed after we press some "Enter" in stdin
            self.communicate(input="echo 'interrupted'", timeout_duration=5)
            output = self.communicate(input="echo 'interrupted'", timeout_duration=5)
            assert output.strip().endswith("interrupted"), "container health check failed"
        except TimeoutError:
            msg = "Failed to interrupt container"
            raise RuntimeError(msg)
        return observation
        
    def on_run_done(self):
        self.close()
        if self.remove_image:
            image_name: str = self.record.instance.dependency().image_full_name()
            self.logger.info(f"Removing image of {image_name}")
            remove_image(image_name)


    def open_pr(self, *, trajectory, _dry_run: bool = False):
        """Create PR to repository

        Args:
            trajectory: Trajectory of actions taken by the agent
            _dry_run: Whether to actually push anything or just simulate it
        """
        self.logger.info("Opening PR")
        # TODO: have better way of handling this
        # Adding random string suffix to avoid name conflicts if we had a previously failed run
        issue_url = self.args.data_path
        try:
            issue = get_gh_issue_data(issue_url, token=self._github_token)
        except InvalidGithubURL as e:
            msg = "Data path must be a github issue URL if --open_pr is set."
            raise ValueError(msg) from e
        branch_name = f"swe-agent-fix-#{issue.number}-" + str(random.random())[2:10]

        self.communicate_with_handling(
            input="rm -f model.patch",
            error_msg="Failed to remove model patch",
            timeout_duration=10,
        )
        self.communicate_with_handling(
            input=f"git checkout -b {branch_name}",
            error_msg="Failed to switch to new branch",
            timeout_duration=10,
        )
        self.communicate_with_handling(
            input="git add .",
            error_msg="Failed to add commits",
            timeout_duration=10,
        )
        dry_run_flag = "--allow-empty" if _dry_run else ""
        self.communicate_with_handling(
            input=f"git commit -m 'Fix: {issue.title}' -m 'Closes #{issue.number}' {dry_run_flag}",
            error_msg="Failed to commit changes",
            timeout_duration=10,
        )

        owner, repo, _ = parse_gh_issue_url(issue_url)
        # If `--repo_path` was specified with a different github URL, then the record will contain
        # the forking user
        assert self.record is not None
        if self.record["repo_type"] != "github":
            # We already validated that `--data_path` is a github issue URL
            # so this is the only case where we can reach here
            msg = "--repo_path must point to a github URL if --open_pr is set"
            raise ValueError(msg)
        forker, _ = self.record["repo"].split("/")
        head = branch_name
        remote = "origin"
        if forker != owner:
            head = f"{forker}:{branch_name}"
            token_prefix = ""
            if self._github_token:
                token_prefix = f"{self._github_token}@"
            fork_url = f"https://{token_prefix}github.com/{forker}/{repo}.git"
            self.logger.debug(f"Using fork: {fork_url}")
            self.communicate_with_handling(
                input=f"git remote add fork {fork_url}",
                error_msg="Failed to create new git remote",
                timeout_duration=10,
            )
            remote = "fork"
        dry_run_prefix = "echo " if _dry_run else ""
        self.communicate_with_handling(
            input=f"{dry_run_prefix} git push {remote} {branch_name}",
            error_msg=(
                "Failed to push branch to remote. Please check your token and permissions. "
                "You might want to push to a fork with the push_gh_repo_url option."
            ),
            timeout_duration=10,
        )
        body = (
            f"This is a PR opened by AI tool [SWE Agent](https://github.com/princeton-nlp/SWE-agent/) "
            f"to close [#{issue.number}]({issue_url}) ({issue.title}).\n\nCloses #{issue.number}."
        )
        body += "\n\n" + format_trajectory_markdown(trajectory)
        api = GhApi(token=self._github_token)
        if not _dry_run:
            pr_info = api.pulls.create(
                owner=owner,
                repo=repo,
                title=f"SWE-agent[bot] PR to fix: {issue.title}",
                head=head,
                base="main",
                body=body,
                draft=True,
            )
            self.logger.info(
                f"PR created as a draft at {pr_info.html_url}. Please review it carefully, push "
                "any required changes onto the branch and then click "
                "'Ready for Review' to bring it to the attention of the maintainers.",
            )
