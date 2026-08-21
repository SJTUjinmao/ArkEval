from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

import yaml
from simple_parsing.helpers.fields import field
from simple_parsing.helpers.flatten import FlattenedAccess
from simple_parsing.helpers.serialization.serializable import FrozenSerializable
from tenacity import RetryError
import time

from sweagent.agent.commands import Command, ParseCommand
from sweagent.agent.history_processors import HistoryProcessor
from sweagent.agent.models import (
    APIStats,
    ContextWindowExceededError,
    CostLimitExceededError,
    ModelArguments,
    get_model,
)
from sweagent.agent.parsing import FormatError, ParseFunction
from sweagent.environment.swe_env import SWEEnv
from sweagent.utils.config import convert_paths_to_abspath
from sweagent.utils.log import get_logger


def _read_text_stable(path: str | Path) -> str:
    """Read text with a stable encoding on Windows (avoid GBK default)."""
    p = Path(path)
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return p.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            return p.read_text()


def _is_command_format_error_observation(obs: str | None) -> bool:
    return bool(obs and obs.lstrip().startswith("COMMAND_FORMAT_ERROR:"))


def _has_no_modified_defect_files(obs: str | None) -> bool:
    return bool(obs and re.search(r"modified_defect_code_files:\s*0/\d+", obs))


def _recorded_action_exit_code(obs: str | None, fallback: int | None) -> int | None:
    matches = re.findall(r"^BUILD_ACTION_EXIT_CODE=(-?\d+)$", obs or "", re.MULTILINE)
    return int(matches[-1]) if matches else fallback


@dataclass(frozen=True)
class Subroutine(FrozenSerializable):
    name: str
    agent_file: str
    # one of "action", "observation", "response", "state", "thought"
    return_type: str = None  # type: ignore
    init_observation: str | None = None
    end_name: str | None = None
    signature: str | None = None
    docstring: str | None = None
    model: ModelArguments | None = None
    agent_args: Any | None = None


@dataclass(frozen=True)
class AgentConfig(FrozenSerializable):
    system_template: str
    instance_template: str
    patch_only_generation: bool = False
    next_step_template: str | None = None  # defaults to instance_template
    next_step_no_output_template: str | None = None  # defaults to next_step_template
    strategy_template: str | None = None
    demonstration_template: str | None = None
    # Paths to demonstrations. If path is not absolute, it is assumed to be
    # relative to the SWE_AGENT_CONFIG_ROOT (if set) or the SWE-agent repository root
    demonstrations: list[str | Path] = field(default_factory=list)
    put_demos_in_history: bool = False  # if True, add demonstration to history instead of as a single message
    # defaults to format_error_template in ParseFunction
    format_error_template: str = None  # type: ignore
    # Paths to command files. If path is not absolute, it is assumed to be
    # relative to the SWE_AGENT_CONFIG_ROOT (if set) or the SWE-agent repository root
    command_files: list[str | Path] = field(default_factory=list)
    env_variables: dict[str, str] = field(default_factory=dict)
    util_functions: list[str] = field(default_factory=list)
    submit_command: str = "submit"
    parse_function: str = "ThoughtActionParser"
    parse_command: str = "ParseCommandBash"
    history_processor: str = "DefaultHistoryProcessor"
    history_processor_args: dict[str, Any] = field(default_factory=dict)
    command_docs: str = None  # type: ignore
    blocklist_error_template: str = "Interactive operation '{name}' is not supported by this environment"
    blocklist: tuple[str, ...] = (
        "vim",
        "vi",
        "emacs",
        "nano",
        "nohup",
        "git",
    )
    blocklist_standalone: tuple[str, ...] = (
        "python",
        "python3",
        "ipython",
        "bash",
        "sh",
        "exit",
        "/bin/bash",
        "/bin/sh",
        "nohup",
        "vi",
        "vim",
        "emacs",
        "nano",
    )
    language_specified_demo: dict[str, str] = field(default_factory=dict)
    language_specified_tools: dict[str, str] = field(default_factory=dict)
    # Should extract environment state in a json readable form
    state_command: Command = Command(
        name="state",
        code="""state() {
            echo '{"working_dir": "'$(realpath --relative-to=$ROOT/.. $PWD)'"}';
        };""",
    )
    _commands: list[Command] = field(default_factory=list)
    _subroutines: dict[str, Subroutine] = field(default_factory=dict)
    subroutine_types: list[Subroutine] = field(default_factory=list)

    @classmethod
    def load_yaml_utf8(cls, path: str | Path) -> "AgentConfig":
        """Load config YAML with a stable encoding on Windows."""
        data = yaml.safe_load(_read_text_stable(path)) or {}
        return cls.from_dict(data)

    def __post_init__(self):
        object.__setattr__(self, "command_files", convert_paths_to_abspath(self.command_files))
        object.__setattr__(self, "demonstrations", convert_paths_to_abspath(self.demonstrations))

        if self.next_step_template is None:
            object.__setattr__(self, "next_step_template", self.instance_template)
        if self.next_step_no_output_template is None:
            object.__setattr__(self, "next_step_no_output_template", self.next_step_template)

        object.__setattr__(self, "parse_command", ParseCommand.get(self.parse_command))
        for file in self.command_files:
            commands = self.parse_command.parse_command_file(file)

            util_functions = [command for command in commands if command.name.startswith("_")]
            commands = [command for command in commands if not command.name.startswith("_")]

            object.__setattr__(self, "util_functions", self.util_functions + util_functions)
            object.__setattr__(self, "_commands", self._commands + commands)

        for subroutine in self.subroutine_types:
            if subroutine.name == "submit":
                msg = "Cannot use 'submit' as a subroutine name"
                raise ValueError(msg)
            agent_args = AgentArguments(
                model=subroutine.model,
                config_file=subroutine.agent_file,
            )
            object.__setattr__(subroutine, "agent_args", agent_args)
            object.__setattr__(self, "_subroutines", {**self._subroutines, subroutine.name: subroutine})

        multi_line_command_endings = {
            command.name: command.end_name
            for command in [*self._commands, *self._subroutines.values()]
            if command.end_name is not None
        }
        object.__setattr__(self, "multi_line_command_endings", multi_line_command_endings)
        object.__setattr__(
            self,
            "command_docs",
            self.parse_command.generate_command_docs(
                self._commands,
                self.subroutine_types,
                **self.env_variables,
            ),
        )
        object.__setattr__(self, "parse_function", ParseFunction.get(self.parse_function))
        if self.format_error_template is None:
            object.__setattr__(
                self,
                "format_error_template",
                self.parse_function.format_error_template,
            )
        object.__setattr__(
            self,
            "format_error_template",
            self.format_error_template.format(**self.__dict__),
        )
        for command in self._commands:
            if command.name == self.submit_command:
                object.__setattr__(self, "submit_command_end_name", command.end_name)
                break
        object.__setattr__(
            self,
            "history_processor",
            HistoryProcessor.get(self.history_processor, **self.history_processor_args),
        )


@dataclass(frozen=True)
class AgentArguments(FlattenedAccess, FrozenSerializable):
    """Configure the agent's behaviour (templates, parse functions, blocklists, ...)."""

    model: ModelArguments = None

    # Policy can only be set via config yaml file from command line
    config_file: Path | None = None
    config: AgentConfig | None = field(default=None, cmd=False)

    def __post_init__(self):
        if self.config is None and self.config_file is not None:
            # If unassigned, we load the config from the file to store its contents with the overall arguments
            # Avoid FrozenSerializable.load_yaml using platform default encoding (GBK on some Windows setups).
            config = AgentConfig.load_yaml_utf8(self.config_file)
            object.__setattr__(self, "config", config)
        assert self.config is not None  # mypy
        for subroutine in getattr(self.config, "subroutines", {}).values():
            model_args = subroutine.model
            object.__setattr__(
                model_args,
                "per_instance_cost_limit",
                self.model.per_instance_cost_limit,
            )
            object.__setattr__(model_args, "total_cost_limit", self.model.total_cost_limit)
            object.__setattr__(
                model_args,
                "max_steps_per_instance",
                self.model.max_steps_per_instance,
            )


class TrajectoryStep(TypedDict):
    action: str
    observation: str
    response: str
    state: str | None
    thought: str
    command_results: list[dict[str, Any]]


class AgentHook:
    def on_init(self): ...

    def on_run_start(
        self,
    ): ...

    def on_step_start(self): ...

    def on_actions_generated(self, *, thought: str, action: str, output: str): ...

    def on_sub_action_started(self, *, sub_action: str): ...

    def on_sub_action_executed(self, *, obs: str, done: bool): ...

    def on_step_done(self, *, trajectory_step: TrajectoryStep, model_stats: APIStats): ...

    def on_run_done(self): ...

    def on_model_query(self, *, query: str, agent: str):
        """Actually query the model with the complete history."""

    def on_query_message_added(
        self,
        *,
        role: str,
        content: str,
        agent: str,
        is_demo: bool = False,
        thought: str = "",
        action: str = "",
    ): ...


class RepairWallClock:
    """Wall time that advances only during *repair* work; pauses during test-only ``env.step`` blocks."""

    __slots__ = ("_base_s", "_segment_start", "_in_repair")

    def __init__(self) -> None:
        self._base_s = 0.0
        self._segment_start = time.perf_counter()
        self._in_repair = True

    def pause(self) -> None:
        if self._in_repair and self._segment_start is not None:
            self._base_s += time.perf_counter() - self._segment_start
            self._in_repair = False
            self._segment_start = None

    def resume(self) -> None:
        if not self._in_repair:
            self._segment_start = time.perf_counter()
            self._in_repair = True

    def elapsed_s(self) -> float:
        if self._in_repair and self._segment_start is not None:
            return self._base_s + (time.perf_counter() - self._segment_start)
        return self._base_s


_REPAIR_EXCLUDE_LINE_REGEXES: tuple[re.Pattern[str], ...] = (
    # Harmony / OpenHarmony — task name `test` (not paths like src/test in cd-only lines without hvigor test)
    re.compile(r"\b(?:hvigorw(?:\.bat)?|hvigor)\s+test\b", re.I),
    re.compile(r"\b(?:hvigorw(?:\.bat)?|hvigor)\s+\S+:\s*test\b", re.I),
    re.compile(r"\bohpm\s+test\b", re.I),
    re.compile(r"\b(?:npm|pnpm|yarn)\s+test\b", re.I),
    re.compile(r"\bpytest\b", re.I),
    re.compile(r"\bjest\b", re.I),
    re.compile(r"\bmocha\b", re.I),
    re.compile(r"\bhypium\b", re.I),
    re.compile(r"run_(?:local_)?tests\.py", re.I),
    re.compile(r"\bvitest\b", re.I),
    re.compile(r"\bcargo\s+test\b", re.I),
    re.compile(r"\bgo\s+test\b", re.I),
    re.compile(r"\bpython(?:3)?(?:\.exe)?\s+(?:-m\s+)?unittest\b", re.I),
    re.compile(r"\bhdc\b.*\baa\s+test\b", re.I),
    re.compile(r"\bgradle\w*\s+test\b", re.I),
)


def _line_looks_like_test_command(line: str) -> bool:
    t = line.strip()
    if not t or t.startswith("#"):
        return False
    for rx in _REPAIR_EXCLUDE_LINE_REGEXES:
        if rx.search(t):
            return True
    return False


def _action_excludes_repair_wall_clock(action: str) -> bool:
    """True when this bash block is running automated tests — excluded from repair_elapsed (e.g. 120s repair budget)."""
    s = action.strip()
    if not s:
        return False
    for line in s.splitlines():
        if _line_looks_like_test_command(line):
            return True
    return _line_looks_like_test_command(s.replace("\n", " "))


_EDIT_ACTION_NAMES = {"create", "edit", "edit_file", "str_replace"}


def _primary_action_name(action: str) -> str:
    for line in action.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped.split(None, 1)[0]
    return ""


def _action_is_file_edit(action: str) -> bool:
    return _primary_action_name(action) in _EDIT_ACTION_NAMES


class Agent:
    """Agent handles the behaviour of the model and how it interacts with the environment."""

    def __init__(self, name: str, args: AgentArguments, log_dir: Path = None):
        self.name = name
        self.model = get_model(args.model, args.config._commands + args.config.subroutine_types)
        self.config = args.config
        assert self.config is not None  # mypy
        self.system_args = {
            "command_docs": self.config.command_docs,
            **self.config.env_variables,
        }
        self.instance_args = None
        self._parse_command_patterns()
        self.history = []
        self.last_container_id = None
        self.hooks = []
        self._last_prompt_hash = ""
        self.logger = get_logger("agent", log_dir)

    def add_hook(self, hook: AgentHook):
        """Add hook to agent"""
        hook.on_init()
        self.hooks.append(hook)

    def _append_history(self, item: dict):
        for hook in self.hooks:
            hook.on_query_message_added(**item)
        self.history.append(item)

    def setup(self, instance_args, init_model_stats=None) -> None:
        """Setup the agent for a new instance. This includes
        formatting the system message and adding demonstrations to the history.

        Args:
            instance_args: Arguments for the instance
        """
        assert self.config is not None  # mypy
        self.model.reset_stats(init_model_stats)
        self.instance_args = instance_args

        system_msg = self.config.system_template.format(
            **self.system_args,
            **self.instance_args,
        )
        self.logger.info(f"SYSTEM ({self.name})\n{system_msg}")

        self.history: list[dict[str, Any]] = []
        self._append_history({"role": "system", "content": system_msg, "agent": self.name})

        if "history_to_messages" in dir(self.model):
            for demonstration_path in self.config.demonstrations:
                if self.config.demonstration_template is None and not self.config.put_demos_in_history:
                    msg = "Cannot use demonstrations without a demonstration template or put_demos_in_history=True"
                    raise ValueError(msg)

                # Load history
                self.logger.info(f"DEMONSTRATION: {demonstration_path}")
                demo_history = json.loads(_read_text_stable(demonstration_path))["history"]
                demo_history = [
                    entry
                    for entry in demo_history
                    if ("agent" not in entry) or ("agent" in entry and entry["agent"] == self.name)
                ]

                if self.config.put_demos_in_history:
                    if self.config.demonstration_template is not None:
                        self.logger.warning("Demonstration template is ignored for put_demos_in_history=True")
                    # Add demonstration to history directly as separate messages
                    for entry in demo_history:
                        if entry["role"] != "system":
                            entry["is_demo"] = True
                            self._append_history(entry)
                else:
                    # Add demonstration as single message to history
                    demo_message = self.model.history_to_messages(
                        demo_history,
                        is_demonstration=True,
                    )
                    demonstration = self.config.demonstration_template.format(demonstration=demo_message)
                    self._append_history(
                        {
                            "agent": self.name,
                            "content": demonstration,
                            "is_demo": True,
                            "role": "user",
                        },
                    )

    @property
    def state_command(self) -> str:
        """Return the bash command that will be used to extract the environment state."""
        return self.config.state_command.name

    def _reinitialize_after_native_shell_restart(self, env: SWEEnv) -> None:
        if not getattr(env, "_native_shell_requires_agent_init", False):
            return
        self.logger.info("Reinitializing agent commands after native shell restart")
        self.init_environment_vars(env)
        env._native_shell_requires_agent_init = False

    @property
    def local_history(self) -> list[dict[str, str]]:
        """Return the history of the agent since the last reset."""
        return self.config.history_processor([entry for entry in self.history if entry["agent"] == self.name])

    def save_trajectory(
        self, trajectory: list[dict[str, Any]], log_path: Path, env_name: str, info: dict[str, Any]
    ) -> None:
        """Atomically save the trajectory."""
        log_dict = {
            "environment": env_name,
            "trajectory": trajectory,
            "history": self.history,
            "info": info,
        }
        fd, raw_temp = tempfile.mkstemp(
            prefix=f".{log_path.name}.",
            suffix=".tmp",
            dir=log_path.parent,
        )
        temp_path = Path(raw_temp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(log_dict, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, log_path)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    def _get_first_match(self, action: str, pattern_type: str) -> re.Match | None:
        """Return the first match of a command pattern in the action string."""
        assert self.config is not None  # mypy
        if pattern_type == "subroutine":
            patterns = {k: v for k, v in self.subroutine_patterns.items()}
        elif pattern_type == "multi_line":
            patterns = {
                k: v
                for k, v in self.command_patterns.items()
                if k in self.config.multi_line_command_endings or k == self.config.submit_command
            }
            patterns += {
                k: v for k, v in self.subroutine_patterns.items() if k in self.config.multi_line_command_endings
            }
        elif pattern_type == "multi_line_no_subroutines":
            patterns = {k: v for k, v in self.command_patterns.items() if k in self.config.multi_line_command_endings}
        else:
            msg = f"Unknown pattern type: {pattern_type}"
            raise ValueError(msg)
        matches = list()
        for _, pat in patterns.items():
            match = pat.search(action)
            if match:
                matches.append(match)
        if len(matches) == 0:
            return None
        matches = sorted(matches, key=lambda x: x.start())
        return matches[0]

    def _guard_multiline_input(self, action: str) -> str:
        """Split action by multiline commands, then append the first line in each multiline command with "<< '{end_name}'".
        Multiline commands (which are specified by an end_name) are commands that span multiple lines and are terminated by a specific end_name.

        Their multi-line argument is sent using a heredoc, which is a way to send a multi-line string to a command in bash.
        """
        parsed_action = list()
        rem_action = action
        while rem_action.strip():
            first_match = self._get_first_match(rem_action, "multi_line_no_subroutines")
            if first_match:
                pre_action = rem_action[: first_match.start()]
                match_action = rem_action[first_match.start() : first_match.end()]
                rem_action = rem_action[first_match.end() :]
                if pre_action.strip():
                    parsed_action.append(pre_action)
                if match_action.strip():
                    eof = first_match.group(3).strip()
                    if not match_action.split("\n")[0].strip().endswith(f"<< '{eof}'"):
                        guarded_command = match_action[first_match.start() :]
                        first_line = guarded_command.split("\n")[0]
                        guarded_command = guarded_command.replace(first_line, first_line + f" << '{eof}'", 1)
                        parsed_action.append(guarded_command)
                    else:
                        parsed_action.append(match_action)
            else:
                parsed_action.append(rem_action)
                rem_action = ""
        return "\n".join(parsed_action)

    def split_actions(self, action: str, pattern_type="subroutine") -> list[dict[str, Any]]:
        """Split an action into a list of actions in a greedy manner, each of which is a subroutine call or a single command."""
        parsed_action = list()
        rem_action = action
        while rem_action.strip():
            first_match = self._get_first_match(rem_action, pattern_type)
            if first_match:
                pre_action = rem_action[: first_match.start()]
                match_action = rem_action[first_match.start() : first_match.end()]
                rem_action = rem_action[first_match.end() :]
                if pre_action.strip():
                    parsed_action.append({"agent": self.name, "action": pre_action, "cmd_name": None})
                if match_action.strip():
                    if match_action.split()[0] == self.config.submit_command:
                        parsed_action.append(
                            {
                                "agent": self.name,
                                "action": match_action,
                                "cmd_name": first_match.group(1),
                            },
                        )  # submit command is not a subroutine
                    else:
                        parsed_action.append(
                            {
                                "agent": first_match.group(1),
                                "args": first_match.group(2),
                                "action": match_action,
                                "cmd_name": first_match.group(1),
                            },
                        )
            else:
                parsed_action.append({"agent": self.name, "action": rem_action, "cmd_name": None})
                rem_action = ""
        return parsed_action

    def _parse_command_patterns(self) -> None:
        assert self.config is not None  # mypy
        self.command_patterns = dict()
        for command in self.config._commands:
            if command.end_name is not None:
                pat = re.compile(
                    rf"^\s*({command.name})\s*(.*?)^({command.end_name})\s*$",
                    re.DOTALL | re.MULTILINE,
                )
                self.command_patterns[command.name] = pat
            else:
                pat = re.compile(rf"^\s*({command.name})\s*(.*?)$", re.MULTILINE)
                self.command_patterns[command.name] = pat
        self.subroutine_patterns = dict()
        for _, subroutine in self.config._subroutines.items():
            if subroutine.end_name is None:
                pat = re.compile(rf"^\s*({subroutine.name})\s*(.*?)$", re.MULTILINE)
                self.subroutine_patterns[subroutine.name,] = pat
            else:
                pat = re.compile(
                    rf"^\s*({subroutine.name})\s*(.*?)^({subroutine.end_name})\s*$",
                    re.DOTALL | re.MULTILINE,
                )
                self.subroutine_patterns[subroutine.name] = pat
        if hasattr(self.config, "submit_command_end_name"):
            submit_pat = re.compile(
                rf"^\s*({self.config.submit_command})\s*(.*?)^({self.config.submit_command_end_name})\s*$",
                re.DOTALL | re.MULTILINE,
            )
        else:
            submit_pat = re.compile(rf"^\s*({self.config.submit_command})(\s*)$", re.MULTILINE)  # group 2 is nothing
        self.subroutine_patterns[self.config.submit_command] = submit_pat
        self.command_patterns[self.config.submit_command] = submit_pat

    def _env_step_repair_aware(
        self,
        env: SWEEnv,
        action: str,
        repair_clock: RepairWallClock,
    ) -> tuple[str | None, int, bool, dict[str, Any]]:
        """Run ``env.step``; pause repair wall clock when the action runs automated tests."""
        if _action_excludes_repair_wall_clock(action):
            repair_clock.pause()
            try:
                return env.step(action)
            finally:
                repair_clock.resume()
        return env.step(action)

    def forward(self, observation: str, available_actions: list[str], state: str, lang: str) -> tuple[str, str, str]:
        """Forwards the model

        Args:
            observation: Observation
            available_actions: Currently not used
            state:
            language: Current case language

        Returns:
            thought: model reasoning
            action: action that the model proposes
            output: raw model output
        """
        thought, action, output = self.forward_with_error_check(observation, state, lang)

        self._append_history(
            {
                "role": "assistant",
                "content": output,
                "thought": thought,
                "action": action,
                "agent": self.name,
            },
        )

        self.logger.info(f" THOUGHT ({self.name})\n{thought}")
        self.logger.info(f" ACTION ({self.name})\n{action}")

        return thought, action, output

    def forward_model(self, observation: str, state: str, lang: str) -> str:
        """Query the model with the current state and observation with the appropriate template.

        Returns:
            output: raw model output
        """
        assert self.config is not None  # mypy

        state_vars = json.loads(state)

        templates: list[str] = []
        # Determine observation template based on what prior observation was
        if self.history[-1]["role"] == "system" or self.history[-1].get("is_demo", False):
            # Show instance template if prev. obs. was initial system message
            templates = [self.config.instance_template]
            if self.config.strategy_template is not None:
                templates.append(self.config.strategy_template)
        elif observation is None or observation.strip() == "":
            # Show no output template if observation content was empty
            templates = [self.config.next_step_no_output_template]
        else:
            # Show standard output template if there is observation content
            templates = [self.config.next_step_template]

        # Populate selected template(s) with information (e.g., issue, arguments, state)
        messages = []
        language_specified_script = self.config.language_specified_demo.get(
            lang,
            self.config.language_specified_demo.get("arkts", ""),
        )
        language_specified_tools = self.config.language_specified_tools.get(
            lang,
            self.config.language_specified_tools.get("arkts", ""),
        )
        for template in templates:
            messages.append(
                template.format(
                    **self.instance_args,
                    **self.system_args,
                    **state_vars,
                    observation=(observation if observation is not None else ""),
                    language=lang,
                    language_specified_script=language_specified_script,
                    language_specified_tools=language_specified_tools,
                ),
            )

        message = "\n".join(messages)

        self.logger.info(f" MODEL INPUT\n{message}")
        self._append_history({"role": "user", "content": message, "agent": self.name})
        query_history = self.local_history
        self._last_prompt_hash = hashlib.sha256(
            json.dumps(query_history, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

        for hook in self.hooks:
            hook.on_model_query(query=query_history, agent=self.name)
        return self.model.query(query_history)

    def retry_after_format_fail(self, output: str) -> str:
        """Ask the model to correct (without committing to persistent history) after a malformatted model output"""
        format_error_template = self.config.format_error_template

        self.logger.warning(f"MALFORMED OUTPUT\n{output}")
        self.logger.warning(f"FORMAT ERROR\n{format_error_template}")

        temp_history = self.local_history + [
            {"role": "assistant", "content": output, "agent": self.name},
            {"role": "user", "content": format_error_template, "agent": self.name},
        ]
        return self.model.query(temp_history)

    def retry_after_blocklist_fail(self, output: str, action: str) -> str:
        """Ask the model to correct (without committing to persistent history) after a disallowed command"""
        name = action.strip().split()[0]
        blocklist_error_message = self.config.blocklist_error_template.format(name=name)

        self.logger.warning(f"BLOCKLISTED OUTPUT\n{output}")
        self.logger.warning(f"BLOCKLIST ERROR\n{blocklist_error_message}")

        temp_history = self.local_history + [
            {"role": "assistant", "content": output, "agent": self.name},
            {"role": "user", "content": blocklist_error_message, "agent": self.name},
        ]
        return self.model.query(temp_history)

    def should_block_action(self, action: str) -> bool:
        """Check if the command should be blocked."""
        names = action.strip().split()
        if len(names) == 0:
            return False
        name = names[0]
        if name in self.config.blocklist:
            return True
        if name in self.config.blocklist_standalone and name == action.strip():
            return True
        return False

    def check_format_and_requery(
        self,
        output: str,
    ) -> tuple[str, str, str]:
        """Query the model with the current state and observation with the appropriate template.

        Try to parse the output into a thought and action. Retry if the output is malformatted or the action is blocked.

        Returns:
            thought: model reasoning
            action: action that the model proposes
            output: raw model output
        """
        # Condition for handling outputs with no thought (just action)
        if self.model.args.model_name == "human":
            return "", output, output
        elif self.model.args.model_name == "human_thought":
            thought, action = ParseFunction.get("ThoughtActionParser")(
                output,
                self.config._commands + self.config.subroutine_types,
                strict=False,
            )
            return thought, action, output

        format_fails = blocklist_fails = 0

        while format_fails + blocklist_fails <= 2:
            try:
                thought, action = self.config.parse_function(
                    output,
                    self.config._commands + self.config.subroutine_types,
                    strict=False,
                )
            except KeyboardInterrupt:
                raise
            except FormatError:
                format_fails += 1
                output = self.retry_after_format_fail(output)
                continue
            if self.should_block_action(action):
                blocklist_fails += 1
                output = self.retry_after_blocklist_fail(output, action)
            else:
                return thought, action, output
        try:
            thought, action = self.config.parse_function(
                output,
                self.config._commands + self.config.subroutine_types,
                strict=False,
            )
            if not self.should_block_action(action):
                self.logger.warning("Recovered a valid action from the final format retry.")
                return thought, action, output
        except FormatError:
            pass
        self.logger.warning(f"Malformat limit reached: \n{output}")
        return "Exit due to format error", "exit_format", output

    def forward_with_error_check(self, observation: str, state: str, lang: str) -> tuple[str, str, str]:
        """Wrapper around `self.forward_model` that handles errors and retries
        due to format errors or blocked actions.

        Returns:
            thought: model reasoning
            action: action that the model proposes
            output: raw model output
        """
        try:
            output = self.forward_model(observation, state, lang)
            return self.check_format_and_requery(output)
        except KeyboardInterrupt:
            raise
        except RuntimeError as e:
            self.logger.warning(f"Runtime error: {e}")
            return (
                f"Exit due to runtime error: {e}",
                "exit_error",
                f"exit due to runtime error: {e}",
            )
        except ContextWindowExceededError:
            self.logger.warning("Context window exceeded")
            return "Exit due to context window", "exit_context", "Exit due to context window"
        except CostLimitExceededError:
            self.logger.warning("Cost limit exceeded")
            return "Exit due to cost limit", "exit_cost", "Exit due to cost limit"
        except RetryError as e:
            self.logger.warning(f"Retry error: {e}")
            return (
                f"Exit due to retry error: {e}",
                "exit_api",
                f"exit due to retry error: {e}",
            )

    def init_environment_vars(self, env: SWEEnv):
        self.set_environment_vars(env, self.config.env_variables)

    def set_environment_vars(self, env: SWEEnv, env_variables: dict[str, Any]) -> None:
        assert self.config is not None  # mypy
        commands_to_execute = (
            [self.config.state_command.code]
            +
            # [code for code in self.config.util_functions] +
            # [command.code for command in self.config._commands] +
            [f"{k}={v}" for k, v in env_variables.items()]
        )
        commands = "\n".join(commands_to_execute)
        try:
            output = env.communicate(commands)
            if env.returncode != 0:
                msg = f"Nonzero return code: {env.returncode}\nOutput: {output}"
                raise RuntimeError(msg)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            self.logger.warning("Failed to set environment variables")
            raise e
        command_files = list()
        for file in self.config.command_files:
            datum = dict()
            with open(file, encoding="utf-8") as f:
                contents = f.read()
            datum["contents"] = contents
            filename = Path(file).name
            if not contents.strip().startswith("#!"):
                if filename.endswith(".sh"):
                    # files are sourced, so they are not executable
                    datum["name"] = Path(file).name
                    datum["type"] = "source_file"
                elif filename.startswith("_"):
                    # files are sourced, so they are not executable
                    datum["name"] = Path(file).name
                    datum["type"] = "utility"
                else:
                    msg = (
                        f"Non-shell script file {file} does not start with shebang.\n"
                        "Either add a shebang (#!) or change the file extension to .sh if you want to source it.\n"
                        "You can override this behavior by adding an underscore to the file name (e.g. _utils.py)."
                    )
                    raise ValueError(msg)
            else:
                # scripts are made executable
                datum["name"] = Path(file).name.rsplit(".", 1)[0]
                datum["type"] = "script"
            command_files.append(datum)
        env.add_commands(command_files)

    def get_environment_vars(self, env: SWEEnv) -> dict[str, Any]:
        """Get environment variables"""
        assert self.config is not None  # mypy
        env_vars = dict()
        for var in self.config.env_variables:
            env_vars[var] = env.communicate(f"echo ${var}").strip()
        return env_vars

    def call_subroutine(self, agent_name: str, sub_action, env: SWEEnv):
        """Call subroutine"""
        assert self.config is not None  # mypy
        env_vars = self.get_environment_vars(env)
        cwd = env.communicate("pwd -P").strip()
        init_observation = self.config._subroutines[agent_name].init_observation
        if init_observation is not None:
            obs, _, _, _ = env.step(init_observation.format(args=sub_action["args"]))
        else:
            obs = None
        if env.returncode != 0:
            self._append_history({"role": "user", "content": obs, "agent": agent_name})
            msg = f"Nonzero return code: {env.returncode} for init_observation in {agent_name}.\n{obs}"
            raise RuntimeError(msg)
        return_type = self.config._subroutines[agent_name].return_type
        sub_agent = Agent(agent_name, self.config._subroutines[agent_name].agent_args)
        sub_agent_output = sub_agent.run(
            {"issue": sub_action["args"]},
            env,
            observation=obs,
            return_type=return_type,
            init_model_stats=self.model.stats,
        )
        self.history += sub_agent.history
        self.set_environment_vars(env, env_vars)
        env.communicate(f"cd {cwd}")
        self.model.stats.replace(sub_agent.model.stats)
        return sub_agent_output

    def run(
        self,
        setup_args: dict[str, Any],
        env: SWEEnv,
        observation: str | None = None,
        traj_dir: Path | None = None,
        return_type: str | None = "info_trajectory",
        init_model_stats: APIStats | None = None,
    ):
        """
        Run the agent on an environment.
        Return the final value of the specified return type.

        Args:
            setup_args: Arguments to pass to the agent's setup method.
            env: The environment to run the agent on.
            observation: Output from environment setup
            traj_dir: Directory to save the trajectory to
            return_type: Controls what to return.
                This should be left at `info_trajectory`, the
                other values are for internal usage with subroutines.
            init_model_stats: Initial model stats to use for the run.

        Returns:
            If return_type is "info_trajectory", returns a tuple of
            the info dictionary and the trajectory (list of dictionaries).
        """
        done = False
        # mypy checks
        assert getattr(env, "native_mode", False) or env.container_obj is not None
        assert env.record is not None
        assert self.config is not None

        env_id = env.container_name if getattr(env, "native_mode", False) else env.container_obj.id
        if env_id != self.last_container_id:
            self.logger.info(f"Initializing agent settings for env {env_id}")
            self.init_environment_vars(env)
            self.last_container_id = env_id
        # Re-initialize primary
        self.setup(setup_args, init_model_stats)

        for hook in self.hooks:
            hook.on_run_start()

        # Run action/observation loop
        trajectory = []
        info = {}
        traj_log_path = traj_dir / (env.record.data['instance_id'] + ".traj")
        self.logger.info("Trajectory will be saved to %s", traj_log_path)
        step_count = 0
        command_format_error_streak = 0
        max_steps = int(getattr(self.model.args, "max_steps_per_instance", 0) or 0)
        if max_steps > 0:
            self.logger.info("max_steps_per_instance=%d (0=unlimited)", max_steps)

        instance_id = env.record.data.get("instance_id", "unknown")
        wall_t0 = time.perf_counter()
        repair_clock = RepairWallClock()
        edit_action_elapsed_s = 0.0
        edit_command_elapsed_s = 0.0
        edit_action_count = 0
        stop_wall_clock = threading.Event()
        tick_state: dict[str, Any] = {"step": 0, "max_steps": max_steps, "repair_clock": repair_clock}

        def _wall_clock_tick_loop() -> None:
            interval = 5.0
            rc: RepairWallClock = tick_state["repair_clock"]
            while not stop_wall_clock.wait(interval):
                repair_s = rc.elapsed_s()
                wall_s = time.perf_counter() - wall_t0
                mx = tick_state["max_steps"]
                cap = str(mx) if mx > 0 else "inf"
                self.logger.info(
                    "WALL_CLOCK instance_id=%s repair_elapsed_s=%.1f wall_elapsed_s=%.1f agent_steps_completed=%s/%s",
                    instance_id,
                    repair_s,
                    wall_s,
                    tick_state["step"],
                    cap,
                )

        ticker_thread: threading.Thread | None = None
        if traj_dir is not None:
            ticker_thread = threading.Thread(
                target=_wall_clock_tick_loop,
                name="agent_wall_clock",
                daemon=True,
            )
            ticker_thread.start()

        def _read_state_json_with_recovery(reason: str, prefix_command: str = "") -> str | None:
            self._reinitialize_after_native_shell_restart(env)
            if not self.state_command:
                if prefix_command:
                    env.communicate(prefix_command)
                return None
            initial_command = f"{prefix_command}; {self.state_command}" if prefix_command else self.state_command
            state_value = env.communicate(initial_command)
            for attempt in range(120):
                try:
                    json.loads(state_value)
                    return state_value
                except json.JSONDecodeError:
                    if attempt % 20 == 0:
                        self.logger.warning("last step output still in the pipe, retrying..")
                    time.sleep(0.05)
                    state_value = env.communicate(self.state_command)
            self.logger.warning(
                "State command did not return JSON after retries; attempting native shell recovery (%s).",
                reason,
            )
            restart = getattr(env, "_restart_native_shell_after_failure", None)
            if callable(restart) and restart(f"state_json_desync:{reason}"):
                self._reinitialize_after_native_shell_restart(env)
                state_value = env.communicate(initial_command)
                for attempt in range(40):
                    try:
                        json.loads(state_value)
                        return state_value
                    except json.JSONDecodeError:
                        if attempt % 10 == 0:
                            self.logger.warning("state command still not JSON after shell restart, retrying..")
                        time.sleep(0.05)
                        state_value = env.communicate(self.state_command)
            raise RuntimeError(f"state command did not return JSON after recovery: {reason}")

        try:
            while not done:
                tick_state["step"] = step_count
                for hook in self.hooks:
                    hook.on_step_start()

                # After max_steps LLM rounds, force submit so the run always ends with a patch attempt.
                if max_steps > 0 and step_count >= max_steps:
                    self.logger.warning(
                        "Reached max_steps_per_instance=%d; forcing `submit` with current workspace.",
                        max_steps,
                    )
                    state = _read_state_json_with_recovery("forced_submit")
                    thought = (
                        f"Stopped after {max_steps} agent steps (max_steps_per_instance). "
                        "Submitting current repository state."
                    )
                    action = "submit"
                    output = thought
                    observations: list[str | None] = []
                    command_results: list[dict[str, Any]] = []
                    for sub_action in self.split_actions(self._guard_multiline_input(action + "\n")):
                        if sub_action["agent"] == self.name or sub_action["cmd_name"] == self.config.submit_command:
                            for hook in self.hooks:
                                hook.on_sub_action_started(sub_action=sub_action)
                            obs, _, done, info = self._env_step_repair_aware(
                                env, sub_action["action"], repair_clock
                            )
                            command_results.append(
                                {
                                    "action": sub_action["action"],
                                    "command_name": sub_action["cmd_name"],
                                    "exit_code": _recorded_action_exit_code(
                                        obs, info.get("action_exit_code", env.returncode)
                                    ),
                                }
                            )
                            self._reinitialize_after_native_shell_restart(env)
                            for hook in self.hooks:
                                hook.on_sub_action_executed(obs=obs, done=done)
                            observations.append(obs)
                            if sub_action["cmd_name"] == self.config.submit_command:
                                done = True
                            if done:
                                break
                    observation = "\n".join([obs for obs in observations if obs is not None])
                    trajectory_step = TrajectoryStep(
                        {
                            "action": action,
                            "observation": observation,
                            "response": output,
                            "state": state,
                            "thought": thought,
                            "command_results": command_results,
                        },
                    )
                    trajectory.append(trajectory_step)
                    model_stats: APIStats = self.model.stats
                    info["model_stats"] = model_stats.to_dict()
                    info["edit_action_elapsed_s"] = edit_action_elapsed_s
                    info["edit_command_elapsed_s"] = edit_command_elapsed_s
                    info["edit_action_count"] = edit_action_count
                    info["max_steps_forced_submit"] = True
                    if traj_dir:
                        self.save_trajectory(trajectory, traj_log_path, env_name=env.name, info=info)
                    for hook in self.hooks:
                        hook.on_step_done(trajectory_step=trajectory_step, model_stats=model_stats)
                    break

                steps_remaining = max_steps - step_count if max_steps > 0 else float('inf')
                late_no_diff_guard = bool(max_steps > 0 and step_count >= 20 and _has_no_modified_defect_files(observation))
                guard_value = "1" if late_no_diff_guard else "0"
                state = _read_state_json_with_recovery(
                    "step_start",
                    f"export MSWE_LATE_NO_DIFF_GUARD={guard_value}",
                )
                # Inject deadline warning when only 5 steps remain.
                if late_no_diff_guard:
                    observation = (observation or "") + (
                        "\n\nLATE_NO_DIFF_GUARD: More than 20 agent steps have passed and "
                        "repair_status still shows modified_defect_code_files: 0/N. "
                        "Broad exploration is now blocked. The next useful action must be "
                        "open/search within a KNOWN DEFECT FILE or edit_file/str_replace a "
                        "KNOWN DEFECT FILE with the smallest issue-related repair.\n"
                    )
                if max_steps > 0 and steps_remaining <= 5:
                    deadline_warning = (
                        f"\n\nDEADLINE: Only {steps_remaining} steps remaining! "
                        "You MUST finalize now:\n"
                        "  1) If packaging/compile not run yet: one `hvigorw assembleHar --no-daemon` "
                        "(or module `assembleHap` / other documented **assemble*** from HVIGOR MODULE ROOT) - do NOT run `hvigorw test`. "
                        "Do **not** use `codelinter` unless that binary exists in your env (it is optional; many setups have no CodeLinter on PATH).\n"
                        "  2) Run `repair_status` or `git diff --name-only` to verify defect-file coverage. "
                        "If any `unmodified_defect_code_files` remain, edit them only when they are issue-related; do not make coverage-only edits.\n"
                        "  3) Run `submit` **only if** the latest **assemble*/packaging** step exited **0** and the diff has an issue-related change in KNOWN DEFECT FILES. If the build still fails, do NOT submit - fix errors or stop without submitting.\n"
                        "Do NOT start broad exploration. Prioritize a focused KNOWN DEFECT FILE repair, a green **package build**, then submit.\n"
                    )
                    observation = (observation or "") + deadline_warning

                step_started_at_epoch_s = time.time()
                step_t0 = time.perf_counter()
                thought, action, output = self.forward(observation, env.get_available_actions(), state, env.record.language)
                model_elapsed_s = time.perf_counter() - step_t0
                for hook in self.hooks:
                    hook.on_actions_generated(thought=thought, action=action, output=output)
                observations = list()
                run_action = self._guard_multiline_input(action)
                action_exec_elapsed_s = 0.0
                edit_exec_elapsed_s = 0.0
                edit_sub_actions: list[str] = []
                command_results: list[dict[str, Any]] = []
                for sub_action in self.split_actions(run_action):
                    if sub_action["agent"] == self.name or sub_action["cmd_name"] == self.config.submit_command:
                        for hook in self.hooks:
                            hook.on_sub_action_started(sub_action=sub_action)
                        sub_t0 = time.perf_counter()
                        obs, _, done, info = self._env_step_repair_aware(
                            env, sub_action["action"], repair_clock
                        )
                        command_results.append(
                            {
                                "action": sub_action["action"],
                                "command_name": sub_action["cmd_name"],
                                "exit_code": _recorded_action_exit_code(
                                    obs, info.get("action_exit_code", env.returncode)
                                ),
                            }
                        )
                        self._reinitialize_after_native_shell_restart(env)
                        sub_elapsed_s = time.perf_counter() - sub_t0
                        action_exec_elapsed_s += sub_elapsed_s
                        if _action_is_file_edit(sub_action["action"]):
                            edit_exec_elapsed_s += sub_elapsed_s
                            edit_sub_actions.append(_primary_action_name(sub_action["action"]))
                        for hook in self.hooks:
                            hook.on_sub_action_executed(obs=obs, done=done)
                        observations.append(obs)
                        if sub_action["cmd_name"] == self.config.submit_command:
                            done = True
                        if done:
                            break
                    else:
                        agent_name = sub_action["agent"]
                        sub_agent_output = self.call_subroutine(agent_name, sub_action, env)
                        observations.append(sub_agent_output)

                observation = "\n".join([obs for obs in observations if obs is not None])
                step_elapsed_s = time.perf_counter() - step_t0
                is_file_edit_step = bool(edit_sub_actions)
                if is_file_edit_step:
                    edit_action_elapsed_s += step_elapsed_s
                    edit_command_elapsed_s += edit_exec_elapsed_s
                    edit_action_count += 1

                trajectory_step = TrajectoryStep(
                    {
                        "action": action,
                        "observation": observation,
                        "response": output,
                        "state": state,
                        "thought": thought,
                        "command_results": command_results,
                        "prompt_hash": getattr(self, "_last_prompt_hash", ""),
                        "timing": {
                            "started_at_epoch_s": step_started_at_epoch_s,
                            "ended_at_epoch_s": time.time(),
                            "elapsed_s": step_elapsed_s,
                            "model_elapsed_s": model_elapsed_s,
                            "prompt_hash": getattr(self, "_last_prompt_hash", ""),
                            "command_elapsed_s": action_exec_elapsed_s,
                            "edit_command_elapsed_s": edit_exec_elapsed_s,
                            "is_file_edit_step": is_file_edit_step,
                            "edit_actions": edit_sub_actions,
                            "cumulative_edit_action_elapsed_s": edit_action_elapsed_s,
                            "cumulative_edit_command_elapsed_s": edit_command_elapsed_s,
                            "cumulative_edit_action_count": edit_action_count,
                        },
                    },
                )
                trajectory.append(trajectory_step)
                nonempty_observations = [obs for obs in observations if obs is not None and obs.strip()]
                only_command_format_errors = bool(nonempty_observations) and all(
                    _is_command_format_error_observation(obs) for obs in nonempty_observations
                )
                if only_command_format_errors and not done:
                    command_format_error_streak += 1
                    if command_format_error_streak < 3:
                        info["command_format_error_not_counted_step"] = True
                        info["command_format_error_streak"] = command_format_error_streak
                        self.logger.info(
                            "Command format error did not count against max_steps "
                            "(streak=%d/2 before counting).",
                            command_format_error_streak,
                        )
                    else:
                        step_count += 1
                        info["command_format_error_counted_step"] = True
                        info["command_format_error_streak"] = command_format_error_streak
                        observation += (
                            "\n\nRepeated COMMAND_FORMAT_ERROR responses are now counted against "
                            "max_steps. Use the exact syntax shown above."
                        )
                        trajectory_step["observation"] = observation
                        self.logger.warning(
                            "Repeated command format errors counted as one max_steps step "
                            "(streak=%d).",
                            command_format_error_streak,
                        )
                else:
                    command_format_error_streak = 0
                    step_count += 1
                model_stats: APIStats = self.model.stats
                info["model_stats"] = model_stats.to_dict()
                info["edit_action_elapsed_s"] = edit_action_elapsed_s
                info["edit_command_elapsed_s"] = edit_command_elapsed_s
                info["edit_action_count"] = edit_action_count
                if traj_dir:
                    self.save_trajectory(trajectory, traj_log_path, env_name=env.name, info=info)
                for hook in self.hooks:
                    hook.on_step_done(trajectory_step=trajectory_step, model_stats=model_stats)

        finally:
            if ticker_thread is not None:
                stop_wall_clock.set()
                ticker_thread.join(timeout=2.0)
            if traj_dir is not None:
                total_wall = time.perf_counter() - wall_t0
                total_repair = repair_clock.elapsed_s()
                self.logger.info(
                    "WALL_CLOCK instance_id=%s total_repair_elapsed_s=%.1f total_wall_elapsed_s=%.1f edit_action_elapsed_s=%.1f edit_command_elapsed_s=%.1f edit_action_count=%d final_agent_steps_completed=%d",
                    instance_id,
                    total_repair,
                    total_wall,
                    edit_action_elapsed_s,
                    edit_command_elapsed_s,
                    edit_action_count,
                    step_count,
                )

        for hook in self.hooks:
            hook.on_run_done()

        env.on_run_done()

        self.logger.info("Trajectory saved to %s", traj_log_path)

        if return_type == "info":
            return info
        if return_type == "info_trajectory":
            return info, trajectory
        return trajectory[-1][return_type]
