"""
检查 ``command_line_tools_test/tools`` 下各 CLI，并可对固定示例仓做 **端到端** 实测（非 discover-only 冒烟）。

用法（在 ``command_line_tools_test`` 目录）::

    python tools/verify_tools.py
    python tools/verify_tools.py --deep

``--deep`` 会调用 ``integration_test.py``，默认工程为仓库内::

    <MSWE-agent 根>/repair_repo/repo_after_fix/Media-Audio

流程：**assemble 打包 → run_local_tests（hvigor test）→ ensure_emulator（hdc）→ install_app → run_tests（设备 instrument）→ extract_benchmark_patches**。
需本机已配置 ``command_line_tools_test/.env``（含 ``DEVECO_PATH`` 等），且模拟器/真机可被 hdc 识别（与日常跑集成一致）。

退出码：``--help``/导入失败为 1；``--deep`` 时以 ``integration_test.py`` 进程退出码为准（构建或 extract 失败则为 1）。
"""
from __future__ import annotations

import _load_env  # noqa: F401
from _load_env import ensure_command_line_tools_env

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def _tools_dir() -> Path:
    return Path(__file__).resolve().parent


def _ctl_root() -> Path:
    return _tools_dir().parent


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _run(argv: list[str], *, cwd: Path, timeout: float | None = None) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired as exc:
        return 124, "", str(exc)


def main() -> int:
    ensure_command_line_tools_env()
    parser = argparse.ArgumentParser(description="Verify command_line_tools_test tools.")
    parser.add_argument(
        "--deep",
        action="store_true",
        help=(
            "Run full E2E via integration_test.py: build → run_local_tests → hdc/emulator → install → "
            "run_tests (instrument) → extract. Default: repair_repo/repo_after_fix/Media-Audio."
        ),
    )
    parser.add_argument(
        "--repo-subdir",
        default="Media-Audio",
        help="Project folder name under repair_repo/<side>/ for --deep (default: Media-Audio).",
    )
    parser.add_argument(
        "--repo-fix-side",
        choices=("after_fix", "before_fix"),
        default="after_fix",
        help="Use repo_after_fix (修复后) or repo_before_fix (缺陷基线). Default: after_fix.",
    )
    parser.add_argument(
        "--e2e-max-seconds",
        type=float,
        default=7200.0,
        help="Wall-clock cap for integration_test subprocess (default 7200 = 2h).",
    )
    args = parser.parse_args()

    ctl = _ctl_root()
    td = _tools_dir()
    py = sys.executable
    rr = _repo_root()

    scripts_help = [
        "build_app.py",
        "extract_benchmark_patches.py",
        "lint_arkts.py",
        "run_tests.py",
        "run_local_tests.py",
        "install_app.py",
        "ensure_emulator.py",
        "start_emulator.py",
        "integration_test.py",
        "verify_repair_repos.py",
        "verify_tools.py",
    ]

    report: dict[str, object] = {"phase": "help", "ok": True, "failures": []}

    for name in scripts_help:
        code, out, err = _run([py, str(td / name), "--help"], cwd=ctl)
        if code != 0:
            report["ok"] = False
            report["failures"].append(  # type: ignore[union-attr]
                {"tool": name, "step": "--help", "exit": code, "stderr": err[:2000]}
            )

    code_imp, out_imp, err_imp = _run(
        [
            py,
            "-c",
            "import sys; p=sys.argv[1]; sys.path.insert(0,p); import common; import _load_env; print('IMPORT_OK')",
            str(td),
        ],
        cwd=ctl,
    )
    imp_combined = (out_imp or "") + (err_imp or "")
    if code_imp != 0 or "IMPORT_OK" not in imp_combined:
        report["ok"] = False
        report["failures"].append(  # type: ignore[union-attr]
            {
                "tool": "common/_load_env",
                "step": "import",
                "exit": code_imp,
                "output": imp_combined[:2000],
            }
        )

    if not args.deep:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1

    deveco = os.environ.get("DEVECO_PATH", "").strip()
    _fix_side_dir = {"after_fix": "repo_after_fix", "before_fix": "repo_before_fix"}[args.repo_fix_side]
    default_e2e_repo = rr / "repair_repo" / _fix_side_dir / args.repo_subdir
    # Intentionally ignore REPO_PATH in .env:
    # it is reserved for the agent's repair workflow and should not affect tool E2E defaults.
    repo_path = default_e2e_repo

    if not deveco:
        report["ok"] = False
        report["e2e_skipped"] = "DEVECO_PATH missing"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    if not repo_path.is_dir():
        report["ok"] = False
        report["e2e_skipped"] = f"repo path not found: {repo_path}"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    integration_argv = [
        py,
        str(td / "integration_test.py"),
        "--repo-path",
        str(repo_path.resolve()),
        "--deveco-path",
        deveco,
        "--build-timeout-sec",
        "1200",
        "--run-tests-timeout-sec",
        "1800",
        "--emulator-timeout-sec",
        "120",
    ]

    help_ok = bool(report.get("ok"))
    code, out, err = _run(integration_argv, cwd=ctl, timeout=args.e2e_max_seconds)
    combined = (out or "") + (err or "")
    tail = combined[-12000:] if len(combined) > 12000 else combined

    report["phase"] = "e2e_integration"
    report["e2e_repo"] = str(repo_path.resolve())
    report["deveco_path"] = deveco
    report["integration_test"] = {
        "argv": integration_argv[2:],
        "exit_code": code,
        "tail": tail,
    }
    report["ok"] = help_ok and code == 0

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ok"]:
        return code if code != 0 else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
