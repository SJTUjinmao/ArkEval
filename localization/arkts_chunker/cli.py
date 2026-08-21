from __future__ import annotations

"""
ArkTS 分块实验 CLI。

用法示例：

    python -m arkts_chunker chunk /path/to/repo entry/src/main/ets/pages/V2/Index.ets

仅打印分块信息（行号范围与部分内容），不会写索引。
"""

import argparse
from pathlib import Path

from .chunker import chunk_file, DEFAULT_MAX_CHARS


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="arkts_chunker", description="ArkTS .ets chunking playground")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_chunk = sub.add_parser("chunk", help="Preview ArkTS .ets chunks")
    p_chunk.add_argument("repo_root", type=Path, help="Path to ArkTS repo root")
    p_chunk.add_argument("rel_path", type=Path, help="Relative path to .ets file from repo root")
    p_chunk.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="Max chars per chunk")
    p_chunk.add_argument(
        "--show-text",
        action="store_true",
        help="Print first few lines of each chunk content as preview",
    )

    args = parser.parse_args(argv)

    if args.cmd == "chunk":
        repo_root: Path = args.repo_root
        rel_path: Path = args.rel_path
        max_chars: int = args.max_chars
        chunks = chunk_file(repo_root, rel_path, max_chars=max_chars)
        print(f"File: {(repo_root / rel_path).resolve()}")
        print(f"Total chunks: {len(chunks)}")
        for i, ch in enumerate(chunks, 1):
            print(f"  chunk {i}: lines {ch.line_start}-{ch.line_end}")
            if args.show_text:
                lines = ch.text.splitlines()
                preview = "\n".join(lines[: min(5, len(lines))])
                print("    --- preview ---")
                print(preview)
                print("    ---------------")

