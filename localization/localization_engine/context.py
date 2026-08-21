# localization_engine/context.py
from __future__ import annotations

"""阶段三上下文收集：在 file_list + focus_chunks 基础上，用 read_file/glob/grep 产出供 LLM 使用的上下文包。"""

import re
from pathlib import Path
from typing import Any


def build_fix_context(
    repo_root: str | Path,
    query: str,
    file_list: list[str],
    focus_chunks: list[dict],
    *,
    read_file_padding: int = 2,
    include_glob_patterns: list[str] | None = None,
    grep_symbols_from_chunks: bool = True,
    max_grep_results_per_symbol: int = 20,
) -> dict[str, Any]:
    """在给定问题文件列表与焦点 chunk 上，用 ⑦ read_file 精读、⑧ glob 发现相关文件、⑨ grep 查调用链，产出结构化上下文。

    返回结构：
    - snippets: list[dict]，每项含 file_path, line_start, line_end, content（带 padding 的代码片段）
    - related_files: list[str]，可选扩展的相关文件路径（相对或绝对依实现）
    - grep_hits: list[dict]，可选，每项含 file_path, line, text（grep 命中行）
    - query: 原 query（便于拼 prompt）
    """
    repo = Path(repo_root).resolve()
    out: dict[str, Any] = {
        "query": query,
        "snippets": [],
        "related_files": [],
        "grep_hits": [],
    }
    file_set = {Path(f).resolve() for f in file_list}
    seen_snippet_key: set[tuple[str, int, int]] = set()

    from .tools.filesystem import read_file

    for c in focus_chunks:
        fp = c.get("file_path")
        start = int(c.get("line_start", 0))
        end = int(c.get("line_end", 0))
        if not fp or start < 1 or end < start:
            continue
        key = (str(Path(fp).resolve()), start, end)
        if key in seen_snippet_key:
            continue
        seen_snippet_key.add(key)
        s1 = max(1, start - read_file_padding)
        s2 = end + read_file_padding
        try:
            content = read_file(file_path=fp, start_line=s1, end_line=s2)
        except Exception:
            content = "(read_file failed)"
        out["snippets"].append({
            "file_path": fp,
            "line_start": s1,
            "line_end": s2,
            "content": content,
        })

    if include_glob_patterns:
        from .tools.filesystem import glob_file_search
        for pattern in include_glob_patterns:
            for rel in glob_file_search(repo_root=str(repo), pattern=pattern):
                abs_p = (repo / rel).resolve()
                if abs_p.is_file() and abs_p not in file_set:
                    out["related_files"].append(str(abs_p))
        out["related_files"] = list(dict.fromkeys(out["related_files"]))[:100]

    if grep_symbols_from_chunks and focus_chunks:
        symbols = _extract_symbols_from_chunks(repo, focus_chunks)
        if symbols:
            from .tools.search import grep
            for sym in symbols[:15]:
                try:
                    hits = grep(repo_root=str(repo), query=re.escape(sym), is_regexp=False, include_glob=None)
                    for h in hits[:max_grep_results_per_symbol]:
                        out["grep_hits"].append({
                            "file_path": h["file_path"],
                            "line": h["line"],
                            "text": h["text"],
                        })
                except Exception:
                    continue

    return out


def _extract_symbols_from_chunks(repo: Path, focus_chunks: list[dict]) -> list[str]:
    """从 focus_chunks 对应内容中简单抽取疑似函数/类名（用于 grep 调用链）。"""
    from .tools.filesystem import read_file

    candidates: set[str] = set()
    for c in focus_chunks:
        fp = c.get("file_path")
        start = int(c.get("line_start", 1))
        end = int(c.get("line_end", 1))
        try:
            text = read_file(file_path=fp, start_line=start, end_line=end)
        except Exception:
            continue
        for m in re.finditer(r"\b([A-Z][a-zA-Z0-9_]*)\b", text):
            if len(m.group(1)) > 2:
                candidates.add(m.group(1))
        for m in re.finditer(r"\b([a-z][a-zA-Z0-9_]*)\s*\(", text):
            if len(m.group(1)) > 2 and m.group(1) not in ("function", "return", "if", "for", "while"):
                candidates.add(m.group(1))
    return sorted(candidates)[:20]


def context_to_markdown(ctx: dict[str, Any]) -> str:
    """将 build_fix_context 的返回转为可拼进 LLM prompt 的 Markdown 字符串。"""
    parts = [f"## Query\n{ctx.get('query', '')}\n"]
    parts.append("## Focus code snippets\n")
    for s in ctx.get("snippets", []):
        parts.append(f"### {s['file_path']} (L{s['line_start']}-{s['line_end']})\n```\n{s.get('content', '')}\n```\n")
    if ctx.get("related_files"):
        parts.append("## Related files\n")
        for f in ctx["related_files"][:30]:
            parts.append(f"- {f}\n")
    if ctx.get("grep_hits"):
        parts.append("## Grep hits (references)\n")
        for h in ctx["grep_hits"][:50]:
            parts.append(f"- {h['file_path']}:{h['line']} {h.get('text', '')[:120]}\n")
    return "\n".join(parts)
