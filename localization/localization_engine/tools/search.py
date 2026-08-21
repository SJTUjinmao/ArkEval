from __future__ import annotations

"""Search tools.

Tool interfaces:
- codebase_search(repo_root, query, top_k) -> list[dict]
- codebase_search_in_files(repo_root, query, file_list, top_k_chunks, ...) -> list[dict]  # 阶段三细粒度
- grep(query, is_regexp, include_glob, repo_root) -> list[dict]
"""

import re
from dataclasses import asdict, dataclass
from pathlib import Path


def codebase_search_in_files(
    *,
    repo_root: str,
    query: str,
    file_list: list[str],
    top_k_chunks: int = 30,
    max_chunks_per_file: int = 5,
) -> list[dict]:
    """阶段三细粒度定位：在已定位的问题文件内做语义检索，返回需重点关注的 chunk 列表。"""
    from ..locate_flow import get_focus_chunks

    return get_focus_chunks(
        repo_root,
        query,
        file_list,
        top_k_chunks=top_k_chunks,
        max_chunks_per_file=max_chunks_per_file,
    )


def codebase_search(
    *,
    repo_root: str,
    query: str,
    top_k: int = 20,
) -> list[dict]:
    """语义代码搜索：用自然语言在向量库中检索相关代码块。"""
    from ..indexer import locate_hits

    hits = locate_hits(repo_root, query, top_k=top_k)
    return [
        {
            "file_path": h.file_path,
            "line_start": h.line_start,
            "line_end": h.line_end,
            "score": float(h.score),
        }
        for h in hits
    ]


@dataclass(frozen=True)
class GrepHit:
    file_path: str
    line: int
    text: str


def _iter_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for p in root.rglob("*"):
        if p.is_file():
            files.append(p)
    return files


def grep(*, query: str, is_regexp: bool = False, include_glob: str | None = None, repo_root: str) -> list[dict]:
    root = Path(repo_root).resolve()
    pattern = re.compile(query) if is_regexp else None

    results: list[GrepHit] = []
    for file_path in _iter_files(root):
        if include_glob and not file_path.match(include_glob):
            continue
        try:
            lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        for idx, line in enumerate(lines, start=1):
            matched = bool(pattern.search(line)) if pattern else (query in line)
            if matched:
                results.append(GrepHit(file_path=str(file_path), line=idx, text=line))
    return [asdict(r) for r in results]
