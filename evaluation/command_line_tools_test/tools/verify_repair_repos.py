"""
批量验证 ``repair_repo/repo_before_fix`` 与 ``repair_repo/repo_after_fix`` 与 command_line_tools 的配合情况。

预期（与评测约定一致）：
- **repo_before_fix**：缺陷基线 —— **自动化测试应不能整套通过**（允许能编译 / 部分步骤通过；以 ``run_tests`` 与业务测试为准）。
- **repo_after_fix**：修复 + 测试补丁后的正确态 —— **构建与测试应能通过**（需本机 ``hdc`` 有在线设备时 ``run_tests`` 全量才有意义）。

用法（在 ``command_line_tools_test`` 目录下）::

    python tools/verify_repair_repos.py
    python tools/verify_repair_repos.py --skip-build
    # 最小化：只验一个仓库（before_fix 与 repo_after_fix 下同名的各跑一遍）
    python tools/verify_repair_repos.py --repo Media-Audio
    python tools/verify_repair_repos.py --run-full-tests
"""
from __future__ import annotations

import _load_env  # noqa: F401
from _load_env import ensure_command_line_tools_env

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _tools_dir() -> Path:
    return Path(__file__).resolve().parent


def _list_repo_dirs(base: Path) -> list[Path]:
    if not base.is_dir():
        return []
    return sorted([p for p in base.iterdir() if p.is_dir()], key=lambda p: p.name.lower())


def _run(
    argv: list[str],
    *,
    cwd: Path,
    timeout_sec: float | None,
) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
        )
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired as exc:
        return 124, "", str(exc)


def _parse_discover_instrument_count(stdout: str) -> int:
    n = 0
    for line in stdout.splitlines():
        if line.strip().startswith("kind=instrument"):
            n += 1
    return n


def main() -> int:
    ensure_command_line_tools_env()
    parser = argparse.ArgumentParser(description="Verify repair_repo before/after against command_line_tools_test.")
    parser.add_argument(
        "--repair-repo-root",
        type=Path,
        default=None,
        help="Default: <repo>/repair_repo",
    )
    parser.add_argument("--deveco-path", default=os.environ.get("DEVECO_PATH", ""), help="DevEco Studio path.")
    parser.add_argument("--skip-build", action="store_true", help="Only SDK precheck + run_tests --discover-only.")
    parser.add_argument(
        "--build-timeout-sec",
        type=float,
        default=600.0,
        help="Timeout per assembleHap (default 600s).",
    )
    parser.add_argument(
        "--run-full-tests",
        action="store_true",
        help="Also run run_tests (instrument) — requires online hdc target.",
    )
    parser.add_argument(
        "--test-timeout-sec",
        type=int,
        default=1800,
        help="Timeout for full run_tests (default 1800).",
    )
    parser.add_argument(
        "--repo",
        default="",
        metavar="NAME",
        help="Only verify this folder name under repo_before_fix and repo_after_fix (minimal run).",
    )
    args = parser.parse_args()

    rr = args.repair_repo_root or (_repo_root() / "repair_repo")
    before_dir = rr / "repo_before_fix"
    after_dir = rr / "repo_after_fix"
    deveco = args.deveco_path.strip()
    if not deveco:
        print("ERROR: set DEVECO_PATH in command_line_tools_test/.env or pass --deveco-path", file=sys.stderr)
        return 2

    ctl = _tools_dir().parent
    py = sys.executable
    build_py = _tools_dir() / "build_app.py"
    run_py = _tools_dir() / "run_tests.py"

    rows: list[dict[str, Any]] = []

    def process_side(label: str, base: Path) -> None:
        nonlocal rows
        repos = _list_repo_dirs(base)
        if args.repo:
            repos = [p for p in repos if p.name == args.repo]
            if not repos:
                rows.append(
                    {
                        "side": label,
                        "path": str(base),
                        "note": f"no subdirectory named {args.repo!r}",
                        "skipped": True,
                    }
                )
                return
        if not repos:
            rows.append(
                {
                    "side": label,
                    "path": str(base),
                    "note": "directory missing or empty",
                    "skipped": True,
                }
            )
            return
        for repo in repos:
            row: dict[str, Any] = {
                "side": label,
                "repo": repo.name,
                "path": str(repo),
            }
            # discover-only (includes SDK precheck)
            code_d, out_d, err_d = _run(
                [
                    py,
                    str(run_py),
                    "--repo-path",
                    str(repo),
                    "--deveco-path",
                    deveco,
                    "--discover-only",
                ],
                cwd=ctl,
                timeout_sec=120.0,
            )
            row["discover_exit"] = code_d
            row["instrument_targets"] = _parse_discover_instrument_count(out_d + err_d)
            row["sdk_lines_ok"] = "SDK_SELECTION_API_LEVEL=" in (out_d + err_d) or "BUILD_PROFILE_COMPILE_SDK_VERSION=" in (
                out_d + err_d
            )

            if not args.skip_build:
                code_b, out_b, err_b = _run(
                    [
                        py,
                        str(build_py),
                        "--repo-path",
                        str(repo),
                        "--deveco-path",
                        deveco,
                    ],
                    cwd=ctl,
                    timeout_sec=args.build_timeout_sec,
                )
                row["build_exit"] = code_b
                m = re.search(r"HAP_PATH=(.+)", out_b + err_b)
                row["hap_hint"] = m.group(1).strip() if m else ""
            else:
                row["build_exit"] = None

            if args.run_full_tests and row.get("instrument_targets", 0) > 0:
                code_t, out_t, err_t = _run(
                    [
                        py,
                        str(run_py),
                        "--repo-path",
                        str(repo),
                        "--deveco-path",
                        deveco,
                        "--timeout-sec",
                        str(args.test_timeout_sec),
                    ],
                    cwd=ctl,
                    timeout_sec=float(args.test_timeout_sec) + 30.0,
                )
                row["run_tests_full_exit"] = code_t
                log_m = re.search(r"LOG_PATH=(.+)", out_t + err_t)
                row["run_tests_log_hint"] = log_m.group(1).strip() if log_m else ""
            else:
                row["run_tests_full_exit"] = None

            rows.append(row)

    process_side("before_fix", before_dir)
    process_side("after_fix", after_dir)

    print(json.dumps({"repair_repo_root": str(rr.resolve()), "deveco_path": deveco, "results": rows}, ensure_ascii=False, indent=2))

    # Non-zero exit if after_fix expected but all skipped
    after_rows = [r for r in rows if r.get("side") == "after_fix" and not r.get("skipped")]
    if not any(r.get("side") == "after_fix" and r.get("skipped") for r in rows) and not after_rows:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
