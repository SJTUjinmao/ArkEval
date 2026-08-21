from __future__ import annotations

"""Internal tool registry.

What is a registry?
- It is a mapping from `tool_name -> tool_handler` with metadata.
- The agent/LLM selects a tool by name; the runtime resolves it from the registry.

External interfaces:
- `ToolRegistry.register(...)`
- `ToolRegistry.invoke(name, **kwargs)`
"""

from dataclasses import dataclass
from typing import Any, Callable


ToolHandler = Callable[..., Any]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    handler: ToolHandler


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, name: str, description: str, handler: ToolHandler) -> None:
        if name in self._tools:
            raise ValueError(f"Tool already registered: {name}")
        self._tools[name] = ToolSpec(name=name, description=description, handler=handler)

    def list_tools(self) -> list[ToolSpec]:
        return sorted(self._tools.values(), key=lambda t: t.name)

    def invoke(self, name: str, /, **kwargs: Any) -> Any:
        if name not in self._tools:
            raise KeyError(f"Unknown tool: {name}")
        return self._tools[name].handler(**kwargs)


def build_default_registry() -> ToolRegistry:
    """Construct the default tool registry (stage B+C tools)."""

    from .tools.ask_user import ask_user
    from .tools.edit import apply_diff, edit_file, write_file
    from .tools.filesystem import file_search, glob_file_search, list_dir, read_file
    from .tools.run import terminal
    from .tools.search import codebase_search, grep

    registry = ToolRegistry()
    registry.register("ask_user", "Ask user a question with optional A/B/C options; returns user input.", ask_user)
    registry.register("list_dir", "List directory contents (names with / for dirs).", list_dir)
    registry.register("file_search", "Fuzzy filename search under repo_root.", file_search)
    registry.register("glob_file_search", "Find files by glob pattern (e.g. **/*.ts).", glob_file_search)
    registry.register("read_file", "Read file content; optional start_line/end_line for a range.", read_file)
    registry.register("codebase_search", "Semantic code search by natural language query (vector search).", codebase_search)
    registry.register("grep", "Text/regex search in repo files.", grep)
    registry.register(
        "edit_file",
        "Edit existing file: by (file_path, start_line, end_line, new_content) or (file_path, old_string, new_string). Optional repo_root for path check.",
        edit_file,
    )
    registry.register(
        "write_file",
        "Create or overwrite file at file_path with content; creates parent dirs if needed. Optional repo_root, overwrite (default True).",
        write_file,
    )
    registry.register(
        "apply_diff",
        "Apply unified diff: repo_root + diff_text or diff_path; uses system patch first, fallback to Python. strip (default 1) for -p.",
        apply_diff,
    )
    registry.register(
        "terminal",
        "Run shell command: command, optional cwd (default current), timeout_seconds (default 60), allowed_roots for safety.",
        terminal,
    )
    return registry
