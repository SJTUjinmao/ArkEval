from __future__ import annotations

"""Ignore rules.

External interface (MVP):
- `build_ignore_matcher(repo_root, use_gitignore, builtin_ignore_file) -> IgnoreMatcher`

Dependencies:
- `pathspec` for gitignore-style matching.
"""

from dataclasses import dataclass
from pathlib import Path

import pathspec


@dataclass(frozen=True)
class IgnoreMatcher:
    repo_root: Path
    spec: pathspec.PathSpec

    def is_ignored(self, path: Path) -> bool:
        try:
            rel = path.resolve().relative_to(self.repo_root)
        except Exception:
            return False
        rel_str = rel.as_posix()
        return self.spec.match_file(rel_str)


def _load_ignore_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
    return lines


def build_ignore_matcher(
    repo_root: str | Path,
    *,
    use_gitignore: bool,
    use_builtin_ignore: bool,
    builtin_ignore_file: str,
) -> IgnoreMatcher:
    root = Path(repo_root).resolve()
    patterns: list[str] = []
    if use_gitignore:
        patterns.extend(_load_ignore_lines(root / ".gitignore"))
    if use_builtin_ignore:
        patterns.extend(_load_ignore_lines(root / builtin_ignore_file))
    spec = pathspec.PathSpec.from_lines("gitwildmatch", patterns)
    return IgnoreMatcher(repo_root=root, spec=spec)
