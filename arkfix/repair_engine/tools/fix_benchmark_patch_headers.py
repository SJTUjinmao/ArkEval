"""Repair malformed ``diff --git`` headers and ensure trailing newline on
``fix_patch`` / ``test_patch`` fields in arkts_benchmark.jsonl.

All observed rows in this dataset are simple modifications (no renames / new /
deleted files), and every bad header is of the form
``diff --git a/<path>`` (missing the ``b/<path>`` destination). This script
rewrites each such line to ``diff --git a/<path> b/<path>`` so ``git apply``
and regex-based diff parsers accept it.

Idempotent: rows that already have proper headers + trailing newline are
left untouched.

Usage::

    python tools/fix_benchmark_patch_headers.py -i tests/arkts_benchmark.jsonl
    # creates <input>.bak once, then overwrites in place; pass -o to redirect.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path


_BAD_HEAD_RE = re.compile(r"^diff --git a/(\S+)\s*$", re.MULTILINE)


def repair_diff_headers(text: str) -> tuple[str, int]:
    """Rewrite ``diff --git a/<p>`` (no dest) to ``diff --git a/<p> b/<p>``.

    Returns ``(new_text, n_replacements)``.
    """
    if not text:
        return text, 0
    n = 0

    def _repl(m: "re.Match[str]") -> str:
        nonlocal n
        n += 1
        p = m.group(1)
        return f"diff --git a/{p} b/{p}"

    new_text = _BAD_HEAD_RE.sub(_repl, text)
    return new_text, n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-i", "--input", type=Path, required=True)
    ap.add_argument("-o", "--output", type=Path, default=None)
    args = ap.parse_args()

    rows: list[str] = []
    total_rows = 0
    changed_rows = 0
    total_fix_hdrs = 0
    total_test_hdrs = 0
    newline_fixed = 0
    for raw in args.input.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        row = json.loads(raw)
        total_rows += 1
        row_changed = False

        for key in ("fix_patch", "test_patch"):
            val = row.get(key)
            if not isinstance(val, str) or not val:
                continue
            new_val, n = repair_diff_headers(val)
            if key == "fix_patch":
                total_fix_hdrs += n
            else:
                total_test_hdrs += n
            if not new_val.endswith("\n"):
                new_val = new_val + "\n"
                newline_fixed += 1
            if new_val != val:
                row[key] = new_val
                row_changed = True

        if row_changed:
            changed_rows += 1
        rows.append(json.dumps(row, ensure_ascii=False))

    if args.output is None:
        backup = args.input.with_suffix(args.input.suffix + ".bak")
        if not backup.exists():
            shutil.copy2(args.input, backup)
        target = args.input
    else:
        target = args.output
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(
        f"rows: {total_rows}, changed: {changed_rows}, "
        f"fix_patch header fixes: {total_fix_hdrs}, test_patch header fixes: {total_test_hdrs}, "
        f"trailing-newline fixes: {newline_fixed}\n-> {target}"
    )


if __name__ == "__main__":
    main()
