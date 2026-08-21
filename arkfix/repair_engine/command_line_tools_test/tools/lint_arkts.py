from __future__ import annotations

import _load_env  # noqa: F401
from _load_env import ensure_command_line_tools_env

import argparse
import json
import os
import sys
from pathlib import Path

try:
    from .common import (
        command_output,
        format_command,
        format_path_for_display,
        resolve_directory,
        run_command,
        timestamp_slug,
        tool_logs_dir,
        write_tool_log,
    )
except ImportError:
    from common import (  # type: ignore
        command_output,
        format_command,
        format_path_for_display,
        resolve_directory,
        run_command,
        timestamp_slug,
        tool_logs_dir,
        write_tool_log,
    )


DEFAULT_PRODUCT = "default"
DEFAULT_FORMAT = "json"
DEFAULT_TIMEOUT_SEC = 300.0
DEFAULT_CODELINTER_BAT = Path(
    r"E:\WorkApp\command-line-tools-all\commandline-tools-windows-x64-5.0.3.906\command-line-tools\bin\codelinter.bat"
)


def _resolve_codelinter_path(raw_path: str | None) -> Path:
    candidates: list[Path] = []
    if raw_path:
        candidates.append(Path(raw_path))
    env_override = os.environ.get("CODELINTER_PATH", "").strip()
    if env_override:
        candidates.append(Path(env_override))
    candidates.append(DEFAULT_CODELINTER_BAT)

    checked: list[str] = []
    for candidate in candidates:
        expanded = candidate.expanduser()
        if expanded.is_file():
            return expanded.resolve()
        if expanded.is_dir():
            for nested in (
                expanded / "bin" / "codelinter.bat",
                expanded / "codelinter" / "bin" / "codelinter.bat",
                expanded / "codelinter.bat",
            ):
                checked.append(str(nested))
                if nested.is_file():
                    return nested.resolve()
        checked.append(str(expanded))

    checked_text = "\n".join(f"  - {entry}" for entry in checked)
    raise FileNotFoundError(
        "Unable to locate codelinter.bat. Checked:\n"
        f"{checked_text}"
    )


def _resolve_targets(repo_dir: Path, requested: list[str]) -> list[Path]:
    if not requested:
        return [repo_dir]

    resolved_targets: list[Path] = []
    seen: set[str] = set()
    for raw in requested:
        candidate = Path(raw).expanduser()
        target = candidate.resolve() if candidate.is_absolute() else (repo_dir / candidate).resolve()
        if not target.exists():
            raise FileNotFoundError(f"Lint target does not exist: {target}")
        try:
            target.relative_to(repo_dir)
        except ValueError as exc:
            raise ValueError(f"Lint target is outside repo_path: {target}") from exc
        key = str(target).lower()
        if key in seen:
            continue
        seen.add(key)
        resolved_targets.append(target)
    return resolved_targets


def _resolve_output_path(repo_dir: Path, raw_output: str | None, output_format: str) -> Path:
    suffix = {
        "json": ".json",
        "xml": ".xml",
        "html": ".html",
        "default": ".txt",
    }.get(output_format, ".txt")
    if raw_output:
        candidate = Path(raw_output).expanduser()
        return candidate.resolve() if candidate.is_absolute() else (repo_dir / candidate).resolve()
    return (tool_logs_dir() / f"lint_arkts-report-{timestamp_slug()}{suffix}").resolve()


def _load_issue_count(report_path: Path, output_format: str) -> int | None:
    if output_format != "json" or not report_path.is_file():
        return None
    text = report_path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return 0
    payload = json.loads(text)
    if isinstance(payload, list):
        return len(payload)
    return None


def _build_log_text(
    *,
    repo_dir: Path,
    codelinter_path: Path,
    command: list[str],
    result_output: str,
    report_path: Path,
    issue_count: int | None,
) -> str:
    parts = [
        f"REPO_PATH={repo_dir}",
        f"CODELINTER_PATH={codelinter_path}",
        f"COMMAND={format_command(command)}",
        f"REPORT_PATH={report_path}",
    ]
    if issue_count is not None:
        parts.append(f"ISSUE_COUNT={issue_count}")
    parts.extend(
        [
            "",
            "OUTPUT:",
            result_output.rstrip(),
        ]
    )
    if report_path.is_file():
        report_text = report_path.read_text(encoding="utf-8", errors="replace").rstrip()
        parts.extend(
            [
                "",
                "REPORT:",
                report_text,
            ]
        )
    return "\n".join(parts).rstrip() + "\n"


def main() -> int:
    ensure_command_line_tools_env()

    parser = argparse.ArgumentParser(
        description="Run the 5.0.3.906 HarmonyOS codelinter for static ArkTS checks.",
    )
    parser.add_argument(
        "--repo-path",
        required=True,
        help="HarmonyOS project root. Use an absolute path.",
    )
    parser.add_argument(
        "--path",
        dest="paths",
        action="append",
        default=[],
        help=(
            "File or directory to lint, relative to --repo-path or absolute. "
            "Repeat this option to lint multiple paths. Default: lint the whole repo."
        ),
    )
    parser.add_argument(
        "--product",
        default=DEFAULT_PRODUCT,
        help=f"Active product name passed to codelinter. Default: {DEFAULT_PRODUCT}.",
    )
    parser.add_argument(
        "--format",
        choices=("default", "json", "xml", "html"),
        default=DEFAULT_FORMAT,
        help=f"Report format passed to codelinter. Default: {DEFAULT_FORMAT}.",
    )
    parser.add_argument(
        "--config",
        help="Optional codelinter .json/.json5 config file.",
    )
    parser.add_argument(
        "--output",
        help="Optional report file path. Default: a file under dev_sessions/05_test/logs.",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="Enable codelinter incremental mode.",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Allow codelinter auto-fix mode.",
    )
    parser.add_argument(
        "--timeout-sec",
        type=float,
        default=DEFAULT_TIMEOUT_SEC,
        help=f"Timeout in seconds. Default: {DEFAULT_TIMEOUT_SEC}.",
    )
    parser.add_argument(
        "--codelinter-path",
        help=(
            "Optional codelinter.bat path or command-line-tools root. "
            f"Default: {DEFAULT_CODELINTER_BAT}."
        ),
    )
    args = parser.parse_args()

    repo_dir = resolve_directory(args.repo_path, "repo_path")
    codelinter_path = _resolve_codelinter_path(args.codelinter_path)
    lint_targets = _resolve_targets(repo_dir, args.paths)
    report_path = _resolve_output_path(repo_dir, args.output, args.format)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    command = [str(codelinter_path)]
    if args.config:
        command.extend(["-c", args.config])
    if args.fix:
        command.append("--fix")
    if args.output or args.format == "json":
        command.extend(["-o", str(report_path)])
    if args.product:
        command.extend(["-p", args.product])
    if args.format:
        command.extend(["-f", args.format])
    if args.incremental:
        command.append("-i")
    command.extend([str(path) for path in lint_targets])

    result = run_command(command, cwd=repo_dir, timeout_sec=args.timeout_sec)
    combined_output = command_output(result)
    issue_count: int | None = None
    parse_error: str | None = None
    try:
        issue_count = _load_issue_count(report_path, args.format)
    except Exception as exc:  # pragma: no cover - defensive surface for tool logging
        parse_error = str(exc)

    log_path = write_tool_log(
        "lint_arkts",
        _build_log_text(
            repo_dir=repo_dir,
            codelinter_path=codelinter_path,
            command=command,
            result_output=combined_output,
            report_path=report_path,
            issue_count=issue_count,
        ),
    )

    print(f"REPO_PATH={repo_dir}")
    print(f"CODELINTER_PATH={codelinter_path}")
    print(f"TARGET_COUNT={len(lint_targets)}")
    for index, target in enumerate(lint_targets, start=1):
        print(f"TARGET_{index}={format_path_for_display(target, start=repo_dir)}")
    print(f"PRODUCT={args.product}")
    print(f"FORMAT={args.format}")
    print(f"REPORT_PATH={report_path}")
    print(f"LOG_PATH={format_path_for_display(log_path)}")

    if result.returncode != 0:
        print(f"ISSUE_COUNT={issue_count if issue_count is not None else ''}")
        print("LINT_STATUS=FAILED")
        print("LINT_REASON=command_failed")
        print(combined_output or f"codelinter exited with code {result.returncode}.", file=sys.stderr)
        return 2

    if parse_error:
        print("LINT_STATUS=FAILED")
        print("LINT_REASON=report_parse_failed")
        print(f"Unable to parse codelinter report: {parse_error}", file=sys.stderr)
        return 2

    print(f"ISSUE_COUNT={issue_count if issue_count is not None else ''}")
    if issue_count is not None and issue_count > 0:
        print("LINT_STATUS=FAILED")
        print("LINT_REASON=issues_found")
        return 1

    print("LINT_STATUS=SUCCESS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
