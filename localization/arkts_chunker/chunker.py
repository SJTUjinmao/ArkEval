from __future__ import annotations

"""
ArkTS (.ets) experimental chunker.

用途：
- 在不改动现有 indexer 的前提下，对指定 .ets 文件做「ArkTS-aware」分块；
- 优先调用 Node 侧 arkts-extractor.mjs 获取结构范围；
- 失败时退回 ArkTS 专用 fallback（正则 + 大括号计数）；
- 最后兜底为简单的行窗口分块。

不连接 Milvus，不写索引，仅输出分块信息（包含行号与文本）。
"""

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List


@dataclass
class ChunkRefLike:
    file_path: str
    line_start: int
    line_end: int
    text: str


DEFAULT_MAX_CHARS = 2048


def _run_arkts_extractor(repo_root: Path, file_path: Path) -> list[dict]:
    """调用 Node 侧 arkts-extractor.mjs，返回 ranges 列表。"""
    # 默认脚本位置：localization_engine/ast/node/arkts-extractor.mjs
    # 这里通过 repo_root 向上两级找到定位引擎项目根。
    # 也允许通过环境变量 OVERRIDE_ARKTS_EXTRACTOR 指定绝对路径。
    override = os.environ.get("OVERRIDE_ARKTS_EXTRACTOR")
    if override:
        extractor = Path(override)
    else:
        # /home/xiebang/CodePhoenix/arkts_chunker/.. -> 定位引擎根
        # 然后拼 localization_engine/ast/node/arkts-extractor.mjs
        this_file = Path(__file__).resolve()
        project_root = this_file.parents[1]
        extractor = project_root / "localization_engine" / "ast" / "node" / "arkts-extractor.mjs"

    if not extractor.is_file():
        return []

    cmd = [
        os.environ.get("NODE_EXECUTABLE", "node"),
        str(extractor),
        "--file",
        str(file_path),
        "--root",
        str(repo_root),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return []

    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return []
    ranges = data.get("ranges") or []
    out: list[dict] = []
    for r in ranges:
        try:
            ls = int(r.get("line_start"))
            le = int(r.get("line_end"))
        except (TypeError, ValueError):
            continue
        if ls <= 0 or le < ls:
            continue
        out.append({"line_start": ls, "line_end": le, "kind": r.get("kind"), "name": r.get("name")})
    return out


def _split_by_max_chars(
    file_path: Path, lines: list[str], line_start: int, line_end: int, max_chars: int
) -> list[ChunkRefLike]:
    """在给定 [line_start, line_end] 范围内，按 max_chars 做二次分块。"""
    # 将 1-based 行号区间转换为 0-based 切片
    start_idx = max(line_start - 1, 0)
    end_idx = min(line_end, len(lines))
    window: list[str] = []
    window_start_line = start_idx + 1
    chunks: list[ChunkRefLike] = []

    cur_line = start_idx + 1
    for idx in range(start_idx, end_idx):
        line = lines[idx]
        text_len = sum(len(x) for x in window) + len(line) + 1
        if window and text_len > max_chars:
            text = "\n".join(window)
            chunks.append(
                ChunkRefLike(
                    file_path=str(file_path),
                    line_start=window_start_line,
                    line_end=cur_line - 1,
                    text=text,
                )
            )
            window = []
            window_start_line = cur_line
        window.append(line)
        cur_line += 1

    if window:
        text = "\n".join(window)
        chunks.append(
            ChunkRefLike(
                file_path=str(file_path),
                line_start=window_start_line,
                line_end=end_idx,
                text=text,
            )
        )
    return chunks


def _normalize_ranges(ranges: list[dict]) -> list[dict]:
    """
    将可能重叠的 ranges 规范化为不重叠的区间，并优先保留更细粒度的块（如 build）。
    这样按字符数二次切分时不会在方法中间截断（例如不会在 build() 内部把 struct 切成 6-66 和 67-79）。
    """
    if not ranges:
        return []
    # 按区间长度升序，短区间（如 build）优先
    sorted_r = sorted(ranges, key=lambda x: (x["line_end"] - x["line_start"], x["line_start"]))
    segments: list[dict] = []  # (line_start, line_end, kind, name)
    covered: list[tuple[int, int]] = []  # 已覆盖的 [s,e] 区间

    def merge_covered(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
        if not intervals:
            return []
        intervals = sorted(intervals, key=lambda x: x[0])
        out = [intervals[0]]
        for s, e in intervals[1:]:
            if s <= out[-1][1] + 1:
                out[-1] = (out[-1][0], max(out[-1][1], e))
            else:
                out.append((s, e))
        return out

    for r in sorted_r:
        a, b = int(r["line_start"]), int(r["line_end"])
        remaining = [(a, b)]
        for cs, ce in covered:
            new_remaining = []
            for rs, re in remaining:
                if re < cs or rs > ce:
                    new_remaining.append((rs, re))
                else:
                    if rs < cs:
                        new_remaining.append((rs, cs - 1))
                    if re > ce:
                        new_remaining.append((ce + 1, re))
            remaining = new_remaining
        for rs, re in remaining:
            if rs <= re:
                segments.append({
                    "line_start": rs,
                    "line_end": re,
                    "kind": r.get("kind"),
                    "name": r.get("name"),
                })
        covered = merge_covered(covered + [(a, b)])

    return sorted(segments, key=lambda x: x["line_start"])


def chunk_with_arkts_ast(repo_root: Path, rel_path: Path, max_chars: int = DEFAULT_MAX_CHARS) -> List[ChunkRefLike]:
    """基于 ArkTS AST 的分块：Node 子进程给出 ranges，规范化为不重叠区间后再按 max_chars 切分。"""
    repo_root = repo_root.resolve()
    file_path = (repo_root / rel_path).resolve()
    if not file_path.is_file():
        raise FileNotFoundError(file_path)

    text = file_path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    raw_ranges = _run_arkts_extractor(repo_root, file_path)
    ranges = _normalize_ranges(raw_ranges)
    chunks: list[ChunkRefLike] = []
    for r in ranges:
        ls = int(r["line_start"])
        le = int(r["line_end"])
        chunks.extend(_split_by_max_chars(file_path, lines, ls, le, max_chars))
    return chunks


def chunk_arkts_fallback(text_lines: list[str], file_path: Path, max_chars: int = DEFAULT_MAX_CHARS) -> List[ChunkRefLike]:
    """ArkTS 专用 fallback：基于 struct/build 正则 + 大括号计数的启发式分块。"""
    import re

    struct_re = re.compile(r"^\s*(?:@Entry\s+)?export\s+struct\s+(\w+)")
    plain_struct_re = re.compile(r"^\s*struct\s+(\w+)")
    build_re = re.compile(r"^\s*(\w+)?\s*build\s*\(\s*\)\s*{")

    n = len(text_lines)
    used = [False] * n

    def find_block_end(start_idx: int) -> int:
        depth = 0
        started = False
        for i in range(start_idx, n):
            line = text_lines[i]
            for ch in line:
                if ch == "{":
                    depth += 1
                    started = True
                elif ch == "}":
                    depth -= 1
            if started and depth <= 0:
                return i + 1
        return start_idx + 1

    ranges: list[tuple[int, int]] = []
    for i, line in enumerate(text_lines):
        m = struct_re.match(line) or plain_struct_re.match(line)
        if m:
            end = find_block_end(i)
            ranges.append((i + 1, end))
            for j in range(i, end):
                if 0 <= j < n:
                    used[j] = True
            continue
        b = build_re.match(line)
        if b:
            end = find_block_end(i)
            ranges.append((i + 1, end))
            for j in range(i, end):
                if 0 <= j < n:
                    used[j] = True

    # 对未覆盖的区域，用简单行块补齐
    start = None
    for i, flag in enumerate(used):
        if not flag and start is None and text_lines[i].strip():
            start = i
        elif (flag or not text_lines[i].strip()) and start is not None:
            ranges.append((start + 1, i + 1))
            start = None
    if start is not None:
        ranges.append((start + 1, n))

    # 合并并按行号排序
    ranges = sorted(ranges, key=lambda x: x[0])

    chunks: list[ChunkRefLike] = []
    for ls, le in ranges:
        chunks.extend(_split_by_max_chars(file_path, text_lines, ls, le, max_chars))
    return chunks


def chunk_file(repo_root: Path, rel_path: Path, max_chars: int = DEFAULT_MAX_CHARS) -> List[ChunkRefLike]:
    """高层入口：先尝试 ArkTS AST，失败则 ArkTS fallback，再失败则简单行窗口。"""
    repo_root = repo_root.resolve()
    file_path = (repo_root / rel_path).resolve()
    text = file_path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()

    chunks: list[ChunkRefLike] = []
    try:
        chunks = chunk_with_arkts_ast(repo_root, rel_path, max_chars=max_chars)
    except Exception:
        chunks = []

    if not chunks:
        chunks = chunk_arkts_fallback(lines, file_path, max_chars=max_chars)

    if not chunks:
        # 最后兜底：简单行窗口
        chunks = _split_by_max_chars(file_path, lines, 1, len(lines), max_chars=max_chars)

    return chunks
