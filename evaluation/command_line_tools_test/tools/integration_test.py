"""
真实集成测试（非冒烟）：依次调用 ``tools`` 下各 CLI，使用真实仓库路径与真实子进程。

前置：``main()`` 内 ``ensure_command_line_tools_env()`` + ``import _load_env``，保证 ``command_line_tools_test/.env`` 已加载。

步骤（instrument 依赖 hdc 在线；本地测试不依赖 hdc）：
1. ``build_app.py`` — 完整 ``assembleHap``
2. ``run_local_tests.py`` — ``src/test`` 本地单元测试（``hvigorw`` ``test``）；默认执行，可用 ``--skip-local-tests`` 跳过
3. ``ensure_emulator.py`` — 等待/拉起模拟器，使 hdc 有 **online** 目标（除非 ``--skip-emulator``；跳过则须已有真机/已开模拟器）
4. ``install_app.py`` — 有 ``.hap`` 且未 ``--skip-install`` 时安装到目标设备
5. ``run_tests.py`` — **完整** instrument（非 ``--discover-only``），此时设备上应已装包
6. ``extract_benchmark_patches.py`` — 从仓库根 ``tests/arkts_benchmark.jsonl`` 抽取一条 patch 到临时目录

用法（在 ``command_line_tools_test`` 目录）::

    python tools/integration_test.py   # 默认：local test + emulator + install + instrument + extract
    python tools/integration_test.py --repo-path "E:\\...\\Media-Audio"
    python tools/integration_test.py --skip-local-tests  # 跳过 hvigor 本地测试
    python tools/integration_test.py --skip-emulator --skip-install  # 无设备：build + local + extract（instrument 可能失败）

退出码：``build_app``、``run_local_tests``（未跳过时）、``extract_benchmark_patches`` 任一失败则为 1；``run_tests`` / 模拟器 / 安装 仍不因非 0 单独判死（无设备场景）。
"""
from __future__ import annotations

import _load_env  # noqa: F401
from _load_env import ensure_command_line_tools_env

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _tools_dir() -> Path:
    return Path(__file__).resolve().parent


def _ctl_root() -> Path:
    return _tools_dir().parent


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


def _parse_hap_path(combined: str) -> str:
    m = re.search(r"^HAP_PATH=(.*)$", combined, re.MULTILINE)
    if not m:
        return ""
    return m.group(1).strip()


def _parse_package_paths(combined: str) -> list[str]:
    m = re.search(r"^PACKAGE_PATHS_JSON=(.*)$", combined, re.MULTILINE)
    if not m:
        hap_path = _parse_hap_path(combined)
        return [hap_path] if hap_path else []
    try:
        data = json.loads(m.group(1).strip())
    except json.JSONDecodeError:
        return []
    paths: list[str] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("path"):
                paths.append(str(item["path"]))
    return paths


def main() -> int:
    ensure_command_line_tools_env()
    parser = argparse.ArgumentParser(description="Real integration test for command_line_tools_test/tools.")
    parser.add_argument(
        "--repo-path",
        type=Path,
        default=None,
        help=(
            "Harmony project root. Default: <repo_root>/repair_repo/repo_after_fix/Media-Audio "
            "(REPO_PATH in .env is ignored by this tool; pass --repo-path to override)."
        ),
    )
    parser.add_argument("--deveco-path", default=os.environ.get("DEVECO_PATH", "").strip(), help="DevEco Studio path.")
    parser.add_argument(
        "--jsonl",
        type=Path,
        default=None,
        help="Benchmark jsonl. Default: <repo>/tests/arkts_benchmark.jsonl",
    )
    parser.add_argument(
        "--extract-repo",
        default="Media-Audio",
        help="Repo field in jsonl for extract_benchmark_patches.",
    )
    parser.add_argument("--extract-number", type=int, default=5963, help="Benchmark number for extract.")
    parser.add_argument(
        "--build-timeout-sec",
        type=float,
        default=900.0,
    )
    parser.add_argument(
        "--run-tests-timeout-sec",
        type=int,
        default=1800,
        help="Full run_tests timeout.",
    )
    parser.add_argument(
        "--emulator-timeout-sec",
        type=float,
        default=120.0,
    )
    parser.add_argument(
        "--skip-install",
        action="store_true",
        help="Do not run install_app (run_tests may fail if the app is not already on the device).",
    )
    parser.add_argument(
        "--skip-emulator",
        action="store_true",
        help="Skip ensure_emulator (e.g. headless CI).",
    )
    parser.add_argument(
        "--skip-local-tests",
        action="store_true",
        help="Do not run run_local_tests.py (hvigor test / src/test). Default is to run local tests after build.",
    )
    parser.add_argument(
        "--local-tests-timeout-sec",
        type=float,
        default=1800.0,
        help="Timeout for run_local_tests.py hvigor invocation (skipped when --skip-local-tests).",
    )
    parser.add_argument(
        "--local-debug",
        action="store_true",
        help="Enable verbose local test logs (passes --hvigor-stacktrace --hvigor-debug to run_local_tests.py).",
    )
    args = parser.parse_args()

    deveco = args.deveco_path
    if not deveco:
        print("ERROR: DEVECO_PATH missing (.env or --deveco-path)", file=sys.stderr)
        return 2

    rr = _repo_root()
    default_repo = rr / "repair_repo" / "repo_after_fix" / "Media-Audio"
    if args.repo_path is not None:
        repo_path = args.repo_path.expanduser().resolve()
    else:
        repo_path = default_repo

    if not repo_path.is_dir():
        print(f"ERROR: repo-path not a directory: {repo_path}", file=sys.stderr)
        return 2

    jsonl = args.jsonl or (rr / "tests" / "arkts_benchmark.jsonl")
    if not jsonl.is_file():
        print(f"ERROR: jsonl not found: {jsonl}", file=sys.stderr)
        return 2

    ctl = _ctl_root()
    py = sys.executable
    td = _tools_dir()
    build_py = td / "build_app.py"
    run_py = td / "run_tests.py"
    local_py = td / "run_local_tests.py"
    emu_py = td / "ensure_emulator.py"
    inst_py = td / "install_app.py"
    ext_py = td / "extract_benchmark_patches.py"

    emu_exe = os.environ.get("EMULATOR_PATH", "").strip() or str(Path(deveco) / "tools" / "emulator" / "Emulator.exe")

    steps: list[dict[str, Any]] = []
    hap_abs = ""
    package_paths: list[str] = []

    # --- 1. build ---
    code, out, err = _run(
        [py, str(build_py), "--repo-path", str(repo_path), "--deveco-path", deveco, "--build-test-packages"],
        cwd=ctl,
        timeout_sec=args.build_timeout_sec,
    )
    combined = out + err
    package_paths = _parse_package_paths(combined)
    hap_rel = _parse_hap_path(combined)
    if hap_rel and not Path(hap_rel).is_absolute():
        # build_app.py 输出为相对工程根；旧版曾为相对 command_line_tools_test 的 cwd
        cand = (repo_path / hap_rel).resolve()
        hap_abs = str(cand) if cand.is_file() else str((ctl / hap_rel).resolve())
    elif hap_rel:
        hap_abs = str(Path(hap_rel).resolve())
    steps.append(
        {
            "tool": "build_app.py",
            "argv": ["--repo-path", str(repo_path), "--deveco-path", deveco],
            "exit_code": code,
            "tail": (combined[-4000:] if len(combined) > 4000 else combined),
            "hap_path_parsed": hap_abs or hap_rel,
            "package_paths": package_paths,
        }
    )

    # --- 2. run_local_tests（默认开，不依赖 hdc）---
    if not args.skip_local_tests:
        local_extra: list[str] = []
        if args.local_debug:
            local_extra.extend(["--hvigor-stacktrace", "--hvigor-debug"])
        code_loc, out_loc, err_loc = _run(
            [
                py,
                str(local_py),
                "--repo-path",
                str(repo_path),
                "--deveco-path",
                deveco,
                "--timeout-sec",
                str(int(args.local_tests_timeout_sec)),
                *local_extra,
            ],
            cwd=ctl,
            timeout_sec=args.local_tests_timeout_sec + 60.0,
        )
        combined_loc = out_loc + err_loc
        steps.append(
            {
                "tool": "run_local_tests.py",
                "argv": ["hvigor test", str(repo_path)],
                "exit_code": code_loc,
                "tail": (combined_loc[-6000:] if len(combined_loc) > 6000 else combined_loc),
            }
        )
    else:
        steps.append(
            {
                "tool": "run_local_tests.py",
                "skipped": True,
                "reason": "--skip-local-tests",
            }
        )

    # --- 3. ensure_emulator（instrument 必须先有 hdc online 目标）---
    if args.skip_emulator:
        steps.append({"tool": "ensure_emulator.py", "skipped": True, "reason": "--skip-emulator"})
    else:
        code_emu, out_emu, err_emu = _run(
            [
                py,
                str(emu_py),
                "--emulator-path",
                emu_exe,
                "--timeout-sec",
                str(args.emulator_timeout_sec),
                "--repo-path",
                str(repo_path),
                "--deveco-path",
                deveco,
            ],
            cwd=ctl,
            timeout_sec=args.emulator_timeout_sec + 30.0,
        )
        combined_emu = out_emu + err_emu
        steps.append(
            {
                "tool": "ensure_emulator.py",
                "exit_code": code_emu,
                "tail": (combined_emu[-3000:] if len(combined_emu) > 3000 else combined_emu),
            }
        )

    # --- 4. install_app ---
    if args.skip_install:
        steps.append({"tool": "install_app.py", "skipped": True, "reason": "--skip-install"})
    elif not package_paths:
        steps.append(
            {
                "tool": "install_app.py",
                "skipped": True,
                "reason": "no_package_paths_after_build",
            }
        )
    else:
        install_args = [
            py,
            str(inst_py),
            "--repo-path",
            str(repo_path),
            "--deveco-path",
            deveco,
        ]
        for package_path in package_paths:
            install_args.extend(["--package-path", package_path])
        code_inst, out_inst, err_inst = _run(
            install_args,
            cwd=ctl,
            timeout_sec=300.0,
        )
        combined_inst = out_inst + err_inst
        steps.append(
            {
                "tool": "install_app.py",
                "exit_code": code_inst,
                "tail": (combined_inst[-3000:] if len(combined_inst) > 3000 else combined_inst),
            }
        )

    # --- 5. run_tests full（设备已就绪且尽量已装包后再跑 instrument）---
    code2, out2, err2 = _run(
        [
            py,
            str(run_py),
            "--repo-path",
            str(repo_path),
            "--deveco-path",
            deveco,
            "--timeout-sec",
            str(args.run_tests_timeout_sec),
        ],
        cwd=ctl,
        timeout_sec=float(args.run_tests_timeout_sec) + 60.0,
    )
    combined2 = out2 + err2
    steps.append(
        {
            "tool": "run_tests.py",
            "argv": ["full instrument run", str(repo_path)],
            "exit_code": code2,
            "tail": (combined2[-6000:] if len(combined2) > 6000 else combined2),
        }
    )

    # --- 6. extract_benchmark_patches ---
    out_dir = Path(tempfile.mkdtemp(prefix="integration_extract_"))
    try:
        code5, out5, err5 = _run(
            [
                py,
                str(ext_py),
                "--jsonl",
                str(jsonl),
                "--repo",
                args.extract_repo,
                "--number",
                str(args.extract_number),
                "--output-dir",
                str(out_dir),
                "--test-output",
                "itest_test.patch",
                "--fix-output",
                "itest_fix.patch",
            ],
            cwd=ctl,
            timeout_sec=120.0,
        )
        combined5 = out5 + err5
        test_p = out_dir / "itest_test.patch"
        fix_p = out_dir / "itest_fix.patch"
        steps.append(
            {
                "tool": "extract_benchmark_patches.py",
                "exit_code": code5,
                "output_dir": str(out_dir),
                "test_patch_bytes": test_p.stat().st_size if test_p.is_file() else 0,
                "fix_patch_bytes": fix_p.stat().st_size if fix_p.is_file() else 0,
                "tail": (combined5[-2000:] if len(combined5) > 2000 else combined5),
            }
        )
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)

    report = {
        "repo_path": str(repo_path),
        "deveco_path": deveco,
        "steps": steps,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    try:
        from common import format_path_for_display, write_tool_log  # type: ignore
    except ImportError:
        from .common import format_path_for_display, write_tool_log
    log_path = write_tool_log("integration_test", json.dumps(report, ensure_ascii=False, indent=2))
    print(f"LOG_PATH={format_path_for_display(log_path)}")

    # build、本地测试（未跳过）、extract 失败则整体失败；instrument / 模拟器 / 安装 依赖设备，非 0 不单独判死
    hard = [
        s
        for s in steps
        if not s.get("skipped")
        and s["tool"] in ("build_app.py", "run_local_tests.py", "extract_benchmark_patches.py")
    ]
    hard_fail = any(s.get("exit_code", 1) != 0 for s in hard)

    return 1 if hard_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
