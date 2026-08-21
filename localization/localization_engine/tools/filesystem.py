from __future__ import annotations

"""Filesystem tools.

Tool interfaces:
- list_dir(path: str) -> list[str]
- file_search(repo_root: str, query: str) -> list[str]
- glob_file_search(repo_root: str, pattern: str) -> list[str]
- read_file(file_path: str, start_line: int | None, end_line: int | None) -> str
"""

import fnmatch
from pathlib import Path


def list_dir(*, path: str) -> list[str]:
    p = Path(path).resolve()
    if not p.is_dir():
        return []
    children: list[str] = []
    for child in sorted(p.iterdir(), key=lambda c: c.name):
        suffix = "/" if child.is_dir() else ""
        children.append(child.name + suffix)
    return children


def file_search(*, repo_root: str, query: str) -> list[str]:
    """文件名模糊搜索：返回路径中包含 query 的文件（相对 repo_root）。"""
    root = Path(repo_root).resolve()
    if not root.is_dir():
        return []
    query_lower = query.lower()
    out: list[str] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if query_lower in p.name.lower():
            out.append(str(rel))
    return sorted(out)[:200]


def glob_file_search(*, repo_root: str, pattern: str) -> list[str]:
    """按模式查找文件，如 *.ts、**/api/*.ts。返回相对 repo_root 的路径。"""
    root = Path(repo_root).resolve()
    if not root.is_dir():
        return []
    out: list[str] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if fnmatch.fnmatch(str(rel), pattern) or fnmatch.fnmatch(rel.as_posix(), pattern):
            out.append(rel.as_posix())
    return sorted(out)[:500]


def read_file(
    *,
    file_path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """读取文件内容；不传 start_line/end_line 时读整个文件。"""
    path = Path(file_path)
    text = path.read_text(encoding="utf-8", errors="ignore")
    if start_line is None and end_line is None:
        return text
    if start_line is not None and end_line is not None:
        if start_line < 1 or end_line < start_line:
            raise ValueError("Invalid line range")
    lines = text.splitlines()
    if start_line is None:
        start_line = 1
    if end_line is None:
        end_line = len(lines)
    start_idx = max(0, start_line - 1)
    end_idx = min(end_line, len(lines))
    return "\n".join(lines[start_idx:end_idx])
