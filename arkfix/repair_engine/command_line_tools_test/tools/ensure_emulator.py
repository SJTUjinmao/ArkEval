from __future__ import annotations

import _load_env  # noqa: F401 — apply command_line_tools_test/.env before any other imports
from _load_env import ensure_command_line_tools_env

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

try:
    from .common import (
        command_output,
        find_command_path,
        format_command,
        format_path_for_display,
        get_ordered_sdk_roots_for_repo,
        print_build_profile_sdk_resolution,
        read_build_profile_sdk_versions,
        resolve_directory,
        resolve_existing_path,
        run_command,
        start_detached_process,
        strip_ansi,
        tail_text,
        write_tool_log,
    )
except ImportError:
    from common import (  # type: ignore
        command_output,
        find_command_path,
        format_command,
        format_path_for_display,
        get_ordered_sdk_roots_for_repo,
        print_build_profile_sdk_resolution,
        read_build_profile_sdk_versions,
        resolve_directory,
        resolve_existing_path,
        run_command,
        start_detached_process,
        strip_ansi,
        tail_text,
        write_tool_log,
    )


DEFAULT_TIMEOUT_SEC = 120
POLL_INTERVAL_SEC = 2.0
HDC_QUERY_TIMEOUT_SEC = 10.0
PROCESS_QUERY_TIMEOUT_SEC = 10.0
LAUNCH_GRACE_SEC = 3.0
EMPTY_TARGET_MARKERS = {
    "",
    "empty",
    "[empty]",
    "no target",
    "no targets",
    "no connected target",
    "no connected targets",
}
COMMON_EMULATOR_EXECUTABLE_NAMES = (
    "Emulator.exe",
    "emulator.exe",
    "aemu.exe",
    "aemu64.exe",
)
DEVICE_MANAGER_OPTION_RE = re.compile(r'<option\s+name="([^"]+)"\s+value="([^"]*)"')


@dataclass(frozen=True)
class HarmonyEmulatorInstance:
    name: str
    emulator_root: Path
    instance_path: Path
    image_subpath: str | None
    sdk_path: Path | None


@dataclass(frozen=True)
class EmulatorLaunchAttempt:
    launcher: Path
    command: tuple[str, ...]
    description: str


def _hdc_command() -> list[str]:
    hdc_path = find_command_path("hdc")
    if not hdc_path:
        raise FileNotFoundError(
            "Unable to find 'hdc' in PATH. This step requires hdc to be available from the current environment."
        )
    return [str(hdc_path)]


def _parse_hdc_targets(output_text: str) -> list[str]:
    targets: list[str] = []
    seen: set[str] = set()

    for raw_line in strip_ansi(output_text).splitlines():
        line = raw_line.strip()
        normalized = line.lower()
        if not line or normalized in EMPTY_TARGET_MARKERS:
            continue
        if normalized.startswith("list of devices attached"):
            continue
        if normalized.startswith("daemon ") and " successfully " in normalized:
            continue
        target = line.split()[0]
        if target.lower() in EMPTY_TARGET_MARKERS or target in seen:
            continue
        seen.add(target)
        targets.append(target)

    return targets


def _query_hdc_targets() -> subprocess.CompletedProcess[str]:
    command = [*_hdc_command(), "list", "targets"]
    cwd = Path.cwd()
    try:
        return run_command(command, cwd=cwd, timeout_sec=HDC_QUERY_TIMEOUT_SEC)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"`{format_command(command)}` timed out after {HDC_QUERY_TIMEOUT_SEC:.0f} seconds."
        ) from exc


def get_hdc_targets() -> list[str]:
    result = _query_hdc_targets()
    output_text = command_output(result)
    if result.returncode != 0:
        detail = [
            f"Failed to query online Harmony targets with exit code {result.returncode}.",
            f"Command: {format_command([*_hdc_command(), 'list', 'targets'])}",
        ]
        output_tail = tail_text(output_text)
        if output_tail:
            detail.append("hdc output:")
            detail.append(output_tail)
        raise RuntimeError("\n".join(detail))
    return _parse_hdc_targets(output_text)


def wait_for_target(timeout_sec: float = DEFAULT_TIMEOUT_SEC) -> str | None:
    deadline = time.monotonic() + max(float(timeout_sec), 0.0)

    while True:
        targets = get_hdc_targets()
        if targets:
            return targets[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(min(POLL_INTERVAL_SEC, max(deadline - time.monotonic(), 0.0)))


def _normalize_path_text(path_text: str | None) -> Path | None:
    if not path_text:
        return None
    return Path(path_text.replace("\\", os.sep).replace("/", os.sep)).expanduser().resolve()


def _read_key_value_file(file_path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return data

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def _load_instance_from_config_dir(instance_dir: Path) -> HarmonyEmulatorInstance | None:
    config_file = instance_dir / "config.ini"
    if not config_file.is_file():
        return None

    config = _read_key_value_file(config_file)
    name = config.get("name") or instance_dir.name
    image_subpath = config.get("imageSubPath") or None
    sdk_path = _normalize_path_text(config.get("sdkPath"))

    return HarmonyEmulatorInstance(
        name=name,
        emulator_root=instance_dir.parent.resolve(),
        instance_path=instance_dir.resolve(),
        image_subpath=image_subpath,
        sdk_path=sdk_path,
    )


def _load_instances_from_lists(emulator_root: Path) -> list[HarmonyEmulatorInstance]:
    lists_file = emulator_root / "lists.json"
    if not lists_file.is_file():
        return []

    try:
        items = json.loads(lists_file.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(items, list):
        return []

    instances: list[HarmonyEmulatorInstance] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        instance_path = _normalize_path_text(item.get("path")) or (emulator_root / name).resolve()
        sdk_path = _normalize_path_text(item.get("harmonyos.sdk.path") or item.get("sdkPath"))
        image_subpath = str(item.get("imageDir", "")).strip() or None
        instances.append(
            HarmonyEmulatorInstance(
                name=name,
                emulator_root=emulator_root.resolve(),
                instance_path=instance_path,
                image_subpath=image_subpath,
                sdk_path=sdk_path,
            )
        )
    return instances


def _load_instances_from_sidecar_inis(emulator_root: Path) -> list[HarmonyEmulatorInstance]:
    instances: list[HarmonyEmulatorInstance] = []
    for sidecar_file in sorted(emulator_root.glob("*.ini")):
        if sidecar_file.name.lower() == "config.ini":
            continue
        sidecar_data = _read_key_value_file(sidecar_file)
        instance_path = _normalize_path_text(sidecar_data.get("path"))
        if not instance_path:
            continue
        instance = _load_instance_from_config_dir(instance_path)
        if instance:
            instances.append(instance)
            continue
        instances.append(
            HarmonyEmulatorInstance(
                name=sidecar_file.stem,
                emulator_root=emulator_root.resolve(),
                instance_path=instance_path,
                image_subpath=None,
                sdk_path=None,
            )
        )
    return instances


def _load_harmony_instances(emulator_path: Path) -> list[HarmonyEmulatorInstance]:
    if emulator_path.is_dir() and (emulator_path / "config.ini").is_file():
        instance = _load_instance_from_config_dir(emulator_path)
        return [instance] if instance else []

    if not emulator_path.is_dir():
        return []

    instances = _load_instances_from_lists(emulator_path)
    if not instances:
        instances = _load_instances_from_sidecar_inis(emulator_path)
    if not instances:
        for child in sorted(emulator_path.iterdir()):
            if not child.is_dir():
                continue
            instance = _load_instance_from_config_dir(child)
            if instance:
                instances.append(instance)

    unique_instances: list[HarmonyEmulatorInstance] = []
    seen: set[tuple[str, str]] = set()
    for instance in instances:
        key = (instance.name.lower(), str(instance.instance_path).lower())
        if key in seen:
            continue
        seen.add(key)
        unique_instances.append(instance)
    return unique_instances


def list_deployed_emulator_instance_names(emulator_deployed_root: str | os.PathLike[str]) -> list[str]:
    """
    列出 ``deployed`` 目录下可识别的模拟器实例名（与 ``lists.json`` / ``config.ini`` / 子目录扫描一致）。

    用于在启动前校验 ``EMULATOR_INSTANCE``，避免 Emulator 报「搜索不到该项目」。
    """
    root = Path(emulator_deployed_root).expanduser().resolve()
    if not root.is_dir():
        return []
    instances = _load_harmony_instances(root)
    return [i.name for i in instances]


def _read_device_manager_settings() -> dict[str, str]:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return {}

    huawei_dir = Path(appdata) / "Huawei"
    if not huawei_dir.is_dir():
        return {}

    option_files = sorted(huawei_dir.glob("DevEcoStudio*/options/deviceManager.xml"), reverse=True)
    for option_file in option_files:
        try:
            content = option_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        matches = DEVICE_MANAGER_OPTION_RE.findall(content)
        if matches:
            return {name: value for name, value in matches}
    return {}


def _resolve_image_root(instance: HarmonyEmulatorInstance) -> Path | None:
    if not instance.image_subpath:
        return None

    relative_image_path = Path(instance.image_subpath.replace("\\", os.sep).replace("/", os.sep))
    device_manager_settings = _read_device_manager_settings()

    raw_candidates: list[Path] = []
    for setting_name in ("imageDeployPath",):
        setting_value = device_manager_settings.get(setting_name, "").strip()
        candidate = _normalize_path_text(setting_value)
        if candidate:
            raw_candidates.append(candidate)

    if instance.sdk_path:
        raw_candidates.append(instance.sdk_path)
        raw_candidates.append(instance.sdk_path.parent / "huawei_sdk")

    raw_candidates.append(instance.emulator_root.parent / "huawei_sdk")

    seen: set[Path] = set()
    for candidate in raw_candidates:
        resolved = candidate.resolve()
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)
        if (resolved / relative_image_path).exists():
            return resolved
    return None


def _is_launchable_file(path: Path) -> bool:
    if not path.is_file():
        return False
    if os.name == "nt":
        return path.suffix.lower() in {".exe", ".bat", ".cmd", ".com"}
    return os.access(path, os.X_OK)


def _find_launcher_candidates(
    emulator_path: Path,
    instances: list[HarmonyEmulatorInstance],
) -> list[Path]:
    raw_candidates: list[Path] = []

    if emulator_path.is_file() and _is_launchable_file(emulator_path):
        raw_candidates.append(emulator_path.resolve())

    if emulator_path.is_dir():
        for name in COMMON_EMULATOR_EXECUTABLE_NAMES:
            candidate = emulator_path / name
            if candidate.exists():
                raw_candidates.append(candidate.resolve())

    for instance in instances:
        if not instance.sdk_path:
            continue
        sdk_path = instance.sdk_path.resolve()
        if sdk_path.name.lower() == "sdk":
            studio_root = sdk_path.parent
            raw_candidates.append(studio_root / "tools" / "emulator" / "Emulator.exe")
            raw_candidates.append(studio_root / "tools" / "emulator" / "emulator.exe")
        raw_candidates.append(sdk_path / "tools" / "emulator" / "Emulator.exe")
        raw_candidates.append(sdk_path / "tools" / "emulator" / "emulator.exe")

    for command_name in ("Emulator.exe", "Emulator", "emulator.exe", "emulator"):
        found = find_command_path(command_name)
        if found:
            raw_candidates.append(found)

    candidates: list[Path] = []
    seen: set[Path] = set()
    for candidate in raw_candidates:
        resolved = candidate.resolve()
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)
        candidates.append(resolved)
    return candidates


def _build_launch_attempts(path: Path) -> list[EmulatorLaunchAttempt]:
    instances = _load_harmony_instances(path)
    launcher_candidates = _find_launcher_candidates(path, instances)
    attempts: list[EmulatorLaunchAttempt] = []
    seen_commands: set[tuple[str, ...]] = set()

    for launcher in launcher_candidates:
        launcher_name = launcher.name.lower()
        if launcher_name == "emulator.exe" and instances:
            for instance in instances:
                image_root = _resolve_image_root(instance)
                command = [str(launcher), "-hvd", instance.name, "-path", str(instance.emulator_root)]
                description = f"{instance.name} via {launcher.name} and emulator root {instance.emulator_root}"
                if image_root:
                    command.extend(["-imageRoot", str(image_root)])
                    description += f" with image root {image_root}"
                key = tuple(command)
                if key not in seen_commands:
                    seen_commands.add(key)
                    attempts.append(EmulatorLaunchAttempt(launcher=launcher, command=key, description=description))

            if attempts:
                continue

        command = (str(launcher),)
        if command not in seen_commands:
            seen_commands.add(command)
            attempts.append(
                EmulatorLaunchAttempt(
                    launcher=launcher,
                    command=command,
                    description=f"direct launcher {launcher}",
                )
            )

    return attempts


def _is_process_running(process_name: str) -> bool:
    cwd = Path.cwd()
    if os.name == "nt":
        try:
            result = run_command(
                ["tasklist", "/FI", f"IMAGENAME eq {process_name}", "/FO", "CSV", "/NH"],
                cwd=cwd,
                timeout_sec=PROCESS_QUERY_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            return False
        if result.returncode != 0:
            return False
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return any(not line.startswith("INFO:") for line in lines)

    try:
        result = run_command(["ps", "-A", "-o", "comm="], cwd=cwd, timeout_sec=PROCESS_QUERY_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return False
    if result.returncode != 0:
        return False
    target_names = {process_name, Path(process_name).stem}
    for line in result.stdout.splitlines():
        current_name = Path(line.strip()).name
        if current_name in target_names:
            return True
    return False


def _start_emulator_if_possible(emulator_path: str | os.PathLike[str]) -> EmulatorLaunchAttempt:
    path = resolve_existing_path(emulator_path, "emulator_path")
    attempts = _build_launch_attempts(path)
    if not attempts:
        raise RuntimeError(
            "No online hdc target was found, and the provided emulator_path could not be resolved into a launchable "
            "emulator command. If this is a Harmony emulator root, make sure it contains device metadata such as "
            "`lists.json` or `config.ini`."
        )

    failure_messages: list[str] = []
    for attempt in attempts:
        if _is_process_running(attempt.launcher.name):
            return attempt

        try:
            process = start_detached_process(list(attempt.command), cwd=attempt.launcher.parent)
        except OSError as exc:
            failure_messages.append(f"{attempt.description}: {exc}")
            continue

        time.sleep(LAUNCH_GRACE_SEC)
        if process.poll() is None or _is_process_running(attempt.launcher.name):
            return attempt

        failure_messages.append(f"{attempt.description}: launcher exited immediately with code {process.returncode}")

    attempt_lines = "\n".join(f"- {message}" for message in failure_messages)
    raise RuntimeError(
        "No online hdc target was found, and all emulator launch attempts failed.\n"
        "Tried:\n"
        f"{attempt_lines}"
    )


def _build_no_target_error(
    timeout_sec: float,
    emulator_path: str | os.PathLike[str] | None,
    launch_attempt: EmulatorLaunchAttempt | None,
) -> str:
    if launch_attempt:
        return (
            "The emulator launch command was issued, but no online hdc target became available in time.\n"
            f"Launch attempt: {launch_attempt.description}\n"
            f"Command: {format_command(list(launch_attempt.command))}\n"
            f"Waited: {timeout_sec:.0f} seconds"
        )
    if emulator_path:
        return (
            "No online hdc target was found.\n"
            f"emulator_path was provided: {Path(emulator_path).expanduser()}\n"
            "The script could not resolve a valid auto-launch command from that path."
        )
    return (
        "No online hdc target was found.\n"
        "Pass emulator_path so the script can auto-launch a configured emulator, or start the emulator manually first."
    )


def _ensure_emulator_running(
    emulator_path: str | os.PathLike[str] | None,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    *,
    raise_on_failure: bool,
) -> str | None:
    target = wait_for_target(timeout_sec=0)
    if target:
        return target

    launch_attempt: EmulatorLaunchAttempt | None = None
    if emulator_path:
        launch_attempt = _start_emulator_if_possible(emulator_path)

    target = wait_for_target(timeout_sec=timeout_sec)
    if target:
        return target

    if raise_on_failure:
        raise RuntimeError(_build_no_target_error(timeout_sec, emulator_path, launch_attempt))
    return None


def ensure_emulator_running(
    emulator_path: str | os.PathLike[str] | None,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
) -> str | None:
    return _ensure_emulator_running(emulator_path=emulator_path, timeout_sec=timeout_sec, raise_on_failure=False)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ensure a HarmonyOS emulator target is online via hdc.")
    parser.add_argument(
        "--emulator-path",
        help="Path to a Harmony emulator root directory, emulator instance directory, or launcher executable.",
    )
    parser.add_argument(
        "--timeout-sec",
        type=float,
        default=DEFAULT_TIMEOUT_SEC,
        help=f"Seconds to wait for a target to appear. Default: {DEFAULT_TIMEOUT_SEC}",
    )
    parser.add_argument(
        "--repo-path",
        help="Optional Harmony project root. With --deveco-path, prints build-profile SDK lines before hdc checks.",
    )
    parser.add_argument(
        "--deveco-path",
        help="Optional DevEco Studio install path. Used together with --repo-path.",
    )
    return parser.parse_args()


def main() -> int:
    ensure_command_line_tools_env()
    args = _parse_args()
    log_lines: list[str] = [
        "TOOL=ensure_emulator.py",
        f"ARGV={' '.join(sys.argv[1:])}",
        f"EMULATOR_PATH_ARG={args.emulator_path or ''}",
        f"TIMEOUT_SEC={args.timeout_sec}",
        f"REPO_PATH={args.repo_path or ''}",
        f"DEVECO_PATH={args.deveco_path or ''}",
    ]
    try:
        if args.repo_path and args.deveco_path:
            repo_dir = resolve_directory(args.repo_path, "repo_path")
            deveco_dir = resolve_directory(args.deveco_path, "deveco_path")
            _, sdk_meta = get_ordered_sdk_roots_for_repo(repo_dir, deveco_dir, product_name="default")
            print_build_profile_sdk_resolution(sdk_meta)
            log_lines.append(f"SDK_META={sdk_meta}")
        elif args.repo_path:
            repo_dir = resolve_directory(args.repo_path, "repo_path")
            compile_v, compatible_v, prod = read_build_profile_sdk_versions(repo_dir)
            print_build_profile_sdk_resolution(
                {
                    "build_profile_path": str(repo_dir / "build-profile.json5"),
                    "product": prod,
                    "compileSdkVersion": compile_v,
                    "compatibleSdkVersion": compatible_v,
                    "sdk_selection_api_level": None,
                }
            )
            log_lines.append(f"BUILD_PROFILE_PATH={repo_dir / 'build-profile.json5'}")

        target = _ensure_emulator_running(
            emulator_path=args.emulator_path,
            timeout_sec=args.timeout_sec,
            raise_on_failure=True,
        )
        print("EMULATOR_STATUS=SUCCESS")
        print(f"TARGET={target}")
        log_lines.append("EMULATOR_STATUS=SUCCESS")
        log_lines.append(f"TARGET={target}")
        log_path = write_tool_log("ensure_emulator", "\n".join(log_lines))
        print(f"LOG_PATH={format_path_for_display(log_path)}")
        return 0
    except Exception as exc:
        log_lines.append("EMULATOR_STATUS=FAILED")
        log_lines.append(f"ERROR={exc}")
        log_path = write_tool_log("ensure_emulator", "\n".join(log_lines))
        print(f"LOG_PATH={format_path_for_display(log_path)}", file=sys.stderr)
        print("EMULATOR_STATUS=FAILED", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
