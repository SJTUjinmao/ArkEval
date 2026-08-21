"""
启动 HarmonyOS 本地模拟器（原子操作，供 agent / CI 调用）。

与 ``run-harmony-tests.ps1`` 的 ``Start-EmulatorInstance`` **一致**：在 Windows 上 **直接** 启动 ``Emulator.exe`` 并传入 ``-hvd / -path / -imageRoot``（``subprocess`` 参数列表或 PowerShell ``Start-Process -ArgumentList``），**不再**经 ``cmd /c start``。
原因：``start`` 的引号/标题在部分中文 Windows 上会被误解析（曾出现 ``\\``、``\\HarmonyEmulator\\`` 等假路径）。工作目录设为 ``Emulator.exe`` 所在目录；**不要**对子进程加 ``DETACHED_PROCESS``（与旧笔记一致，避免与设备管理器行为不一致）。

``-imageRoot`` 须与 Device Manager / 本仓库 ``command_line_tools_test/.env`` 中配置一致，**不要**误用 ``DevEco\\sdk\\default`` 当作镜像根。

前置：``main()`` 首行 ``ensure_command_line_tools_env()``（与全部 ``tools/*.py`` 一致）。

以下键 **必须** 在 ``command_line_tools_test/.env`` 中配置并由 ``ensure_command_line_tools_env()`` 以 ``override=True`` 注入本进程，**不**再从其它环境变量推断或提供 CLI 覆盖：

``EMULATOR_PATH``、``DEVECO_PATH``、``EMULATOR_INSTANCE_PATH``、``EMULATOR_DEPLOYED_PATH``、
``EMULATOR_INSTANCE``、``EMULATOR_IMAGE_ROOT``。

当前仅在 **Windows** 下执行实际启动（与 DevEco 自带 ``Emulator.exe`` 布局一致）。

成功时 stdout 打印 ``KEY=value`` 行，便于 agent 解析。

启动前会按 ``deployed`` 下 ``lists.json`` / ``config.ini`` 等解析实例名；若 ``EMULATOR_INSTANCE`` 不在列表中则直接报错并提示 ``--list-instances``，减少 Emulator「搜索不到该项目」的盲目重试。
"""
from __future__ import annotations

import _load_env  # noqa: F401
from _load_env import ensure_command_line_tools_env

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    from .common import format_command, resolve_directory, run_command
    from .ensure_emulator import (
        get_hdc_targets,
        list_deployed_emulator_instance_names,
        wait_for_target,
    )
except ImportError:
    from common import format_command, resolve_directory, run_command  # type: ignore
    from ensure_emulator import (  # type: ignore
        get_hdc_targets,
        list_deployed_emulator_instance_names,
        wait_for_target,
    )


def _pick_target(targets: list[str]) -> str:
    for t in targets:
        if t.startswith("127.0.0.1:"):
            return t
    return targets[0]


def _emulator_argv(
    emulator_exe: Path,
    instance_name: str,
    deployed: Path,
    image_root: Path,
) -> list[str]:
    return [
        str(emulator_exe),
        "-hvd",
        instance_name,
        "-path",
        str(deployed),
        "-imageRoot",
        str(image_root),
    ]


def _launch_emulator_direct(
    emulator_exe: Path,
    instance_name: str,
    deployed: Path,
    image_root: Path,
) -> tuple[subprocess.Popen[str], list[str]]:
    """直接 ``Emulator.exe -hvd … -path … -imageRoot …``，不经 ``cmd /c start``。"""
    emulator_dir = emulator_exe.parent
    argv = _emulator_argv(emulator_exe, instance_name, deployed, image_root)
    proc = subprocess.Popen(
        argv,
        cwd=str(emulator_dir),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    return proc, argv


DEVECO_MANUAL_PREREQ = (
    "若 DevEco 弹窗提示「模拟器启动失败」：请在 DevEco Studio 登录华为账号，并在 "
    "设备管理器中至少成功启动一次该模拟器；部分环境仅支持从 Device Manager 首次拉起。"
)


def _stop_stale_emulator_processes_windows() -> None:
    if os.name != "nt":
        return
    cwd = Path.cwd()
    for image in ("Emulator.exe", "emulator-crash-service.exe"):
        run_command(
            ["taskkill", "/F", "/IM", image, "/T"],
            cwd=cwd,
            env=os.environ.copy(),
            timeout_sec=15.0,
        )
    time.sleep(3)


def main() -> int:
    ensure_command_line_tools_env()
    parser = argparse.ArgumentParser(
        description=(
            "Start Harmony local emulator (Emulator.exe argv, no cmd start). "
            "Paths come only from command_line_tools_test/.env (EMULATOR_PATH, DEVECO_PATH, EMULATOR_*, etc.)."
        ),
    )
    parser.add_argument(
        "--wait-hdc",
        action="store_true",
        help="After launch, poll hdc until a target appears or timeout.",
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=180.0,
        help="Used with --wait-hdc (default 180).",
    )
    parser.add_argument(
        "--stop-stale",
        action="store_true",
        help="If no hdc target yet, taskkill Emulator / emulator-crash-service before starting (Windows).",
    )
    parser.add_argument(
        "--show-command-only",
        action="store_true",
        help="Print the resolved command and exit without starting.",
    )
    parser.add_argument(
        "--list-instances",
        action="store_true",
        help="Print EMULATOR_DEPLOYED_PATH and instance names under deployed (lists/config/subdirs), then exit.",
    )
    parser.add_argument(
        "--instance-path",
        default="",
        help=(
            "Absolute path to one deployed emulator instance directory, e.g. "
            "C:\\Users\\xb\\AppData\\Local\\Huawei\\Emulator\\deployed\\Huawei_Phone_4. "
            "Overrides EMULATOR_INSTANCE_PATH / EMULATOR_DEPLOYED_PATH / EMULATOR_INSTANCE."
        ),
    )
    args = parser.parse_args()

    if args.list_instances:
        deployed_s = os.environ.get("EMULATOR_DEPLOYED_PATH", "").strip()
        if not deployed_s:
            print("ERROR: EMULATOR_DEPLOYED_PATH must be set in command_line_tools_test/.env", file=sys.stderr)
            return 2
        deployed = resolve_directory(deployed_s, "EMULATOR_DEPLOYED_PATH")
        names = list_deployed_emulator_instance_names(str(deployed))
        print(f"EMULATOR_DEPLOYED_PATH={deployed}")
        print(f"INSTANCE_COUNT={len(names)}")
        for i, n in enumerate(names, 1):
            print(f"INSTANCE_{i}={n}")
        if not names:
            print(
                "HINT=No instances parsed; confirm path is .../Huawei/Emulator/deployed and lists.json exists.",
                file=sys.stderr,
            )
        return 0

    emulator_path_s = os.environ.get("EMULATOR_PATH", "").strip()
    deveco_s = os.environ.get("DEVECO_PATH", "").strip()
    instance_path_s = args.instance_path.strip() or os.environ.get("EMULATOR_INSTANCE_PATH", "").strip()
    deployed_s = os.environ.get("EMULATOR_DEPLOYED_PATH", "").strip()
    instance_name = os.environ.get("EMULATOR_INSTANCE", "").strip()
    image_root_s = os.environ.get("EMULATOR_IMAGE_ROOT", "").strip()

    if os.name != "nt":
        print("EMULATOR_STATUS=UNSUPPORTED_OS", file=sys.stderr)
        print(
            "This script expects Windows DevEco Emulator.exe layout. Use a manual emulator on other platforms.",
            file=sys.stderr,
        )
        return 2

    if emulator_path_s:
        emulator_exe = Path(emulator_path_s).expanduser().resolve()
    else:
        if not deveco_s:
            print(
                "ERROR: EMULATOR_PATH or DEVECO_PATH must be set in command_line_tools_test/.env",
                file=sys.stderr,
            )
            return 2
        deveco = resolve_directory(deveco_s, "DEVECO_PATH")
        emulator_exe = deveco / "tools" / "emulator" / "Emulator.exe"
    if not emulator_exe.is_file():
        print(f"ERROR: Emulator.exe not found: {emulator_exe}", file=sys.stderr)
        return 2

    if instance_path_s:
        instance_path = resolve_directory(instance_path_s, "EMULATOR_INSTANCE_PATH")
        deployed = instance_path.parent
        instance_name = instance_path.name
    else:
        try:
            online = get_hdc_targets()
        except RuntimeError as exc:
            print(f"EMULATOR_WARN=hdc_query_failed: {exc}", file=sys.stderr)
            online = []

        if online:
            print("EMULATOR_STATUS=ALREADY_ONLINE")
            print(f"HDC_TARGETS={','.join(online)}")
            if args.wait_hdc:
                sel = _pick_target(online)
                print(f"HDC_SELECTED_TARGET={sel}")
            return 0

        if not deployed_s:
            print("ERROR: EMULATOR_DEPLOYED_PATH must be set in command_line_tools_test/.env", file=sys.stderr)
            return 2
        deployed = resolve_directory(deployed_s, "EMULATOR_DEPLOYED_PATH")

    if not instance_name:
        print(
            "ERROR: EMULATOR_INSTANCE_PATH or EMULATOR_INSTANCE must be set in command_line_tools_test/.env",
            file=sys.stderr,
        )
        return 2

    known = list_deployed_emulator_instance_names(str(deployed))

    def _norm_name(s: str) -> str:
        return " ".join(s.split()).strip().casefold()

    want = _norm_name(instance_name)
    if known:
        if not any(_norm_name(k) == want for k in known):
            print(
                f"ERROR: EMULATOR_INSTANCE={instance_name!r} not found under deployed root. "
                f"Found instance names: {known}. Update command_line_tools_test/.env to match Device Manager.",
                file=sys.stderr,
            )
            print("HINT=python tools/start_emulator.py --list-instances", file=sys.stderr)
            return 2
    else:
        print(
            "EMULATOR_WARN=no_instances_parsed_under_EMULATOR_DEPLOYED_PATH; "
            "launch may fail (e.g. 搜索不到该项目).",
            file=sys.stderr,
        )

    if not image_root_s:
        print("ERROR: EMULATOR_IMAGE_ROOT must be set in command_line_tools_test/.env", file=sys.stderr)
        return 2
    try:
        image_root = resolve_directory(image_root_s, "EMULATOR_IMAGE_ROOT")
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.stop_stale:
        _stop_stale_emulator_processes_windows()

    command = _emulator_argv(emulator_exe, instance_name, deployed, image_root)
    print(f"COMMAND={format_command(command)}")

    if args.show_command_only:
        print("EMULATOR_LAUNCH=direct_argv_no_cmd")
        print("EMULATOR_STATUS=COMMAND_ONLY")
        return 0

    try:
        proc, argv = _launch_emulator_direct(emulator_exe, instance_name, deployed, image_root)
    except OSError as exc:
        print("EMULATOR_STATUS=FAILED", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 1

    print(f"EMULATOR_LAUNCH=direct_argv={format_command(argv)}")

    time.sleep(3)
    if proc.poll() is not None and proc.returncode not in (0, None):
        print(f"EMULATOR_NOTE=process_exit_code={proc.returncode}", file=sys.stderr)

    print("EMULATOR_STATUS=LAUNCHED")
    print(f"EMULATOR_HINT={DEVECO_MANUAL_PREREQ}", file=sys.stderr)

    if args.wait_hdc:
        target = wait_for_target(timeout_sec=args.wait_seconds)
        if not target:
            print("EMULATOR_STATUS=WAIT_HDC_TIMEOUT", file=sys.stderr)
            print(f"EMULATOR_HINT={DEVECO_MANUAL_PREREQ}", file=sys.stderr)
            return 1
        print(f"HDC_SELECTED_TARGET={target}")
        print("EMULATOR_STATUS=SUCCESS")
        return 0

    print("EMULATOR_STATUS=DONE_NO_WAIT")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
