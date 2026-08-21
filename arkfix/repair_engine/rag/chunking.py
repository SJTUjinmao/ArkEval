from __future__ import annotations

import bisect
import html
import re
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

from .deps import ensure_arkeval_on_path


DOC_SUFFIXES = {".html", ".htm", ".md", ".markdown", ".txt", ".rst"}
CODE_SUFFIXES = {".ets", ".ts"}


def _sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


@dataclass(frozen=True)
class RagChunk:
    source_type: str
    source_path: str
    title: str
    line_start: int
    line_end: int
    chunk_hash: str
    text: str

    def to_json(self) -> dict:
        return asdict(self)


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._skip_depth += 1
            return
        if tag.lower() in {"p", "br", "div", "section", "article", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag.lower() in {"p", "div", "section", "article", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        raw = html.unescape("".join(self.parts))
        return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", raw)).strip()


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _clean_doc_text(path: Path, raw: str) -> tuple[str, str]:
    title = path.stem
    if path.suffix.lower() in {".html", ".htm"}:
        title_match = re.search(r"<title[^>]*>(.*?)</title>", raw, flags=re.IGNORECASE | re.DOTALL)
        if title_match:
            title = re.sub(r"\s+", " ", html.unescape(title_match.group(1))).strip() or title
        parser = _HTMLTextExtractor()
        parser.feed(raw)
        return parser.text(), title

    heading = re.search(r"^\s*#\s+(.+)$", raw, flags=re.MULTILINE)
    if heading:
        title = heading.group(1).strip()
    return raw.strip(), title


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for match in re.finditer(r"\n", text):
        starts.append(match.end())
    return starts


def _line_for_offset(starts: list[int], offset: int) -> int:
    return max(1, bisect.bisect_right(starts, offset))


def _token_spans(text: str) -> list[tuple[int, int]]:
    # Treat CJK characters as individual tokens and Latin identifiers as grouped tokens.
    pattern = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9_.$@/-]+|[^\s]", re.UNICODE)
    return [(m.start(), m.end()) for m in pattern.finditer(text)]


def _doc_windows(text: str, *, window_tokens: int = 512, overlap_tokens: int = 80) -> Iterable[tuple[int, int]]:
    spans = _token_spans(text)
    if not spans:
        return
    step = max(1, window_tokens - overlap_tokens)
    index = 0
    while index < len(spans):
        end_index = min(len(spans), index + window_tokens)
        yield spans[index][0], spans[end_index - 1][1]
        if end_index == len(spans):
            break
        index += step


def iter_doc_chunks(roots: Iterable[Path]) -> list[RagChunk]:
    chunks: list[RagChunk] = []
    for root in roots:
        if not root.exists():
            continue
        files = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
        for path in files:
            if path.suffix.lower() not in DOC_SUFFIXES:
                continue
            raw = _read_text(path)
            text, title = _clean_doc_text(path, raw)
            if not text.strip():
                continue
            starts = _line_starts(text)
            for start_offset, end_offset in _doc_windows(text):
                chunk_text = text[start_offset:end_offset].strip()
                if not chunk_text:
                    continue
                line_start = _line_for_offset(starts, start_offset)
                line_end = _line_for_offset(starts, end_offset)
                chunk_hash = _sha256_text(f"docs:{path}:{line_start}:{line_end}:{chunk_text}")
                chunks.append(
                    RagChunk(
                        source_type="docs",
                        source_path=str(path),
                        title=title,
                        line_start=line_start,
                        line_end=line_end,
                        chunk_hash=chunk_hash,
                        text=chunk_text,
                    )
                )
    return chunks


def _chunk_code_by_ranges(path: Path, *, node_executable: str, max_chunk_chars: int) -> list[RagChunk]:
    ensure_arkeval_on_path()
    from localization.localization_engine.ast.extractor import extract_function_ranges

    text = _read_text(path)
    lines = text.splitlines()
    chunks: list[RagChunk] = []
    for fn in extract_function_ranges(ts_file=path, node_executable=node_executable):
        start = max(1, int(fn.get("line_start", 1)))
        end = min(len(lines), int(fn.get("line_end", start)))
        if start > end:
            continue
        chunk_lines = lines[start - 1 : end]
        window: list[str] = []
        win_start = start
        cur_line = start
        for line in chunk_lines:
            if sum(len(x) for x in window) + len(line) + 1 > max_chunk_chars and window:
                win_text = "\n".join(window)
                chunk_hash = _sha256_text(f"code:{path}:{win_start}:{cur_line - 1}:{win_text}")
                chunks.append(RagChunk("code", str(path), path.name, win_start, cur_line - 1, chunk_hash, win_text))
                window = []
                win_start = cur_line
            window.append(line)
            cur_line += 1
        if window:
            win_text = "\n".join(window)
            chunk_hash = _sha256_text(f"code:{path}:{win_start}:{cur_line - 1}:{win_text}")
            chunks.append(RagChunk("code", str(path), path.name, win_start, cur_line - 1, chunk_hash, win_text))
    return chunks


def _chunk_code_fallback(path: Path, *, max_chunk_chars: int) -> list[RagChunk]:
    text = _read_text(path)
    lines = text.splitlines()
    chunks: list[RagChunk] = []
    start = 1
    buf: list[str] = []
    for line_no, line in enumerate(lines, 1):
        if sum(len(x) for x in buf) + len(line) + 1 > max_chunk_chars and buf:
            chunk_text = "\n".join(buf)
            chunk_hash = _sha256_text(f"code:{path}:{start}:{line_no - 1}:{chunk_text}")
            chunks.append(RagChunk("code", str(path), path.name, start, line_no - 1, chunk_hash, chunk_text))
            buf = []
            start = line_no
        buf.append(line)
    if buf:
        chunk_text = "\n".join(buf)
        chunk_hash = _sha256_text(f"code:{path}:{start}:{len(lines)}:{chunk_text}")
        chunks.append(RagChunk("code", str(path), path.name, start, len(lines), chunk_hash, chunk_text))
    return chunks


def iter_code_chunks(roots: Iterable[Path], *, node_executable: str = "node", max_chunk_chars: int = 2048) -> list[RagChunk]:
    chunks: list[RagChunk] = []
    for root in roots:
        if not root.exists():
            continue
        files = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
        for path in files:
            if path.suffix.lower() not in CODE_SUFFIXES:
                continue
            try:
                file_chunks = _chunk_code_by_ranges(path, node_executable=node_executable, max_chunk_chars=max_chunk_chars)
            except Exception:
                file_chunks = _chunk_code_fallback(path, max_chunk_chars=max_chunk_chars)
            chunks.extend(file_chunks)
    return chunks

