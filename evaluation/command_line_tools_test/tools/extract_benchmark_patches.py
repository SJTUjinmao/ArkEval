from __future__ import annotations

import _load_env  # noqa: F401 — apply command_line_tools_test/.env before any other imports
from _load_env import ensure_command_line_tools_env

import argparse
import json
import sys
from pathlib import Path

try:
    from .common import format_path_for_display, write_text_file, write_tool_log
except ImportError:
    from common import format_path_for_display, write_text_file, write_tool_log  # type: ignore


DEFAULT_REPO = "TaskManagement-ReminderAgentManager"
DEFAULT_NUMBER = 5926


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract test_patch and fix_patch from arkts_benchmark.jsonl into patch files."
    )
    parser.add_argument(
        "--jsonl",
        default=str(_workspace_root() / "arkts_benchmark.jsonl"),
        help="Path to arkts_benchmark.jsonl. Defaults to the workspace root copy.",
    )
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        help=f"Repository name to match. Defaults to {DEFAULT_REPO}.",
    )
    parser.add_argument(
        "--number",
        type=int,
        default=DEFAULT_NUMBER,
        help=f"Benchmark number to match. Defaults to {DEFAULT_NUMBER}.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(_workspace_root()),
        help="Directory where patch files will be written. Defaults to the workspace root.",
    )
    parser.add_argument(
        "--test-output",
        default="test_patch.patch",
        help="Filename for the extracted test_patch. Defaults to test_patch.patch.",
    )
    parser.add_argument(
        "--fix-output",
        default="fix_patch.patch",
        help="Filename for the extracted fix_patch. Defaults to fix_patch.patch.",
    )
    return parser.parse_args()


def _load_target_record(jsonl_path: Path, repo: str, number: int) -> dict[str, object]:
    if not jsonl_path.is_file():
        raise FileNotFoundError(f"Benchmark file does not exist: {jsonl_path}")

    matches: list[dict[str, object]] = []
    with jsonl_path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            record = json.loads(line)
            if record.get("repo") == repo and record.get("number") == number:
                matches.append(record)

    if not matches:
        raise ValueError(f"No benchmark row found for repo={repo!r}, number={number}.")
    if len(matches) > 1:
        raise ValueError(f"Multiple benchmark rows found for repo={repo!r}, number={number}.")
    return matches[0]


def _require_patch(record: dict[str, object], field_name: str) -> str:
    value = record.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Field {field_name!r} is missing or empty in the matched benchmark row.")
    return value


def main() -> int:
    ensure_command_line_tools_env()
    args = _parse_args()

    jsonl_path = Path(args.jsonl).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    record = _load_target_record(jsonl_path, args.repo, args.number)
    test_patch = _require_patch(record, "test_patch")
    fix_patch = _require_patch(record, "fix_patch")

    test_output_path = write_text_file(output_dir / args.test_output, test_patch)
    fix_output_path = write_text_file(output_dir / args.fix_output, fix_patch)

    print(f"repo={args.repo}")
    print(f"number={args.number}")
    print(f"test_patch={test_output_path}")
    print(f"fix_patch={fix_output_path}")
    log_path = write_tool_log(
        "extract_benchmark_patches",
        "\n".join(
            [
                "TOOL=extract_benchmark_patches.py",
                f"ARGV={' '.join(sys.argv[1:])}",
                f"JSONL={jsonl_path}",
                f"REPO={args.repo}",
                f"NUMBER={args.number}",
                f"OUTPUT_DIR={output_dir}",
                f"TEST_PATCH_PATH={test_output_path}",
                f"FIX_PATCH_PATH={fix_output_path}",
            ]
        ),
    )
    print(f"LOG_PATH={format_path_for_display(log_path)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - CLI error path
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
