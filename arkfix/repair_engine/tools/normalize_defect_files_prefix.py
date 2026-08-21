"""Strip leading ``<repo>+<hash>/`` (or ``<repo>/``) from each row's ``defect_files``.

The agent starts its shell in ``<--repo_dir>/<repo>`` (see
``sweagent/environment/swe_env.py::_reset_native``), so ``defect_files`` should be
**relative to that repo root** (e.g. ``entry/src/.../Foo.ets``). Earlier benchmark
generation left a stray ``<repo>+<hash>/`` or ``<repo>/`` prefix that points at a
non-existent sibling directory and causes ``open`` lookups to fail.

Idempotent: rows already relative to repo root are left unchanged.

Usage::

    python tools/normalize_defect_files_prefix.py -i tests/arkts_benchmark.jsonl
    # writes sibling ``.bak`` before overwriting; pass -o to send output elsewhere.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def _strip_prefix(repo: str, path: str) -> str:
    norm = path.replace("\\", "/")
    # Try exact repo prefix first (no +hash), then any ``repo+...`` variant.
    for prefix in (f"{repo}/",):
        if norm.startswith(prefix):
            return norm[len(prefix) :]
    head, _, rest = norm.partition("/")
    if "+" in head and head.split("+", 1)[0] == repo:
        return rest
    return norm


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-i", "--input", type=Path, required=True)
    ap.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Defaults to overwriting --input after creating ``<input>.bak``.",
    )
    args = ap.parse_args()

    lines_out: list[str] = []
    changed = 0
    total = 0
    for raw in args.input.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not raw.strip():
            continue
        row = json.loads(raw)
        repo = (row.get("repo") or "").strip()
        files = row.get("defect_files") or []
        if isinstance(files, list) and repo:
            new_files = [_strip_prefix(repo, str(p)) for p in files]
            if new_files != list(files):
                row["defect_files"] = new_files
                changed += 1
        lines_out.append(json.dumps(row, ensure_ascii=False))
        total += 1

    if args.output is None:
        backup = args.input.with_suffix(args.input.suffix + ".bak")
        if not backup.exists():
            shutil.copy2(args.input, backup)
        target = args.input
    else:
        target = args.output
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines_out) + "\n", encoding="utf-8")
    print(f"wrote {total} row(s), rewrote defect_files on {changed} row(s) → {target}")


if __name__ == "__main__":
    main()
