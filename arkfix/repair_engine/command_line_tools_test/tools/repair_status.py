from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sweagent.utils.repair_status import compute_repair_status, format_repair_status  # noqa: E402


def _git_lines(repo_path: Path, args: list[str]) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise SystemExit(
            f"git {' '.join(args)} failed with exit code {result.returncode}\n{result.stderr[-2000:]}"
        )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _changed_files(repo_path: Path) -> list[str]:
    return (
        _git_lines(repo_path, ["diff", "--name-only", "--"])
        + _git_lines(repo_path, ["diff", "--cached", "--name-only", "--"])
        + _git_lines(repo_path, ["ls-files", "--others", "--exclude-standard"])
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Show repair progress against benchmark defect_files.")
    parser.add_argument("--repo-path", default=".", help="Repository root or any path inside the git repo.")
    parser.add_argument("--defect-files-json", default="", help="JSON array of defect file paths.")
    parser.add_argument("--defect-file", action="append", default=[], help="Defect file path. May be repeated.")
    parser.add_argument("--max-files", type=int, default=20)
    args = parser.parse_args()

    repo_input = Path(args.repo_path).resolve()
    root = subprocess.run(
        ["git", "-C", str(repo_input), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if root.returncode != 0:
        raise SystemExit(f"{repo_input} is not inside a git repository")
    repo_path = Path(root.stdout.strip())

    defect_files = list(args.defect_file)
    if args.defect_files_json.strip():
        loaded = json.loads(args.defect_files_json)
        if not isinstance(loaded, list):
            raise SystemExit("--defect-files-json must be a JSON array")
        defect_files.extend(str(item) for item in loaded)

    status = compute_repair_status(defect_files, _changed_files(repo_path))
    print(format_repair_status(status, max_files=args.max_files))


if __name__ == "__main__":
    main()
