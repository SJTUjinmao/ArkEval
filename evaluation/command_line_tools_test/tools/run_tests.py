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
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from PIL import Image

try:
    from .common import (
        append_text_file,
        command_output,
        find_command_path,
        format_command,
        format_path_for_display,
        get_ordered_sdk_roots_for_repo,
        print_build_profile_sdk_resolution,
        read_text_file,
        resolve_directory,
        run_command,
        write_text_file,
    )
    from .ensure_emulator import get_hdc_targets
except ImportError:
    from common import (  # type: ignore
        append_text_file,
        command_output,
        find_command_path,
        format_command,
        format_path_for_display,
        get_ordered_sdk_roots_for_repo,
        print_build_profile_sdk_resolution,
        read_text_file,
        resolve_directory,
        run_command,
        write_text_file,
    )
    from ensure_emulator import get_hdc_targets  # type: ignore


DEFAULT_TIMEOUT_SEC = 1800.0
DEFAULT_WAIT_TIME_MS = 30000
DEFAULT_HYPIUM_TIMEOUT_MS = 30000
DEFAULT_TEST_RUNNER = "OpenHarmonyTestRunner"
HOST_UI_SCENARIO_FILE = "host_ui_scenarios.json"
BUNDLE_NAME_MARKER = '"bundleName": "'
MODULE_NAME_MARKER = '"name": "'
PACKAGE_NAME_MARKER = '"package": "'
TEST_EXECUTION_FAILURE_MARKERS = (
    "failed to start user test",
    "get bundle info failed",
    "the specified module name is not found",
    "specified module name is not found",
    "module name is not found",
    "error: failed",
    "error: get ",
)
TEST_FINISHED_RESULT_CODE_RE = re.compile(r"TestFinished-ResultCode:\s*(-?\d+)", re.IGNORECASE)
OHOS_REPORT_CODE_RE = re.compile(r"OHOS_REPORT_CODE:\s*(-?\d+)", re.IGNORECASE)
OHOS_REPORT_STATUS_CODE_RE = re.compile(r"OHOS_REPORT_STATUS_CODE:\s*(-?\d+)", re.IGNORECASE)
OHOS_REPORT_RESULT_RE = re.compile(
    r"OHOS_REPORT_RESULT:\s*stream=.*?Failure:\s*(\d+),\s*Error:\s*(\d+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TestTarget:
    kind: str
    source_dir: Path
    module_root: Path
    bundle_name: str | None
    module_name: str | None
    test_files: tuple[Path, ...]
    package_name: str | None = None
    module_name_inferred: bool = False

    def display_name(self, repo_dir: Path) -> str:
        parts = [f"kind={self.kind}"]
        if self.bundle_name:
            parts.append(f"bundle={self.bundle_name}")
        if self.package_name:
            parts.append(f"package={self.package_name}")
        if self.module_name:
            parts.append(f"module={self.module_name}")
        parts.append(f"source={_safe_relative_text(self.source_dir, repo_dir)}")
        parts.append(f"tests={len(self.test_files)}")
        if self.module_name_inferred:
            parts.append("module_name_inferred=true")
        return ";".join(parts)


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _logs_dir() -> Path:
    return _workspace_root() / "dev_sessions" / "05_test" / "logs"


def _format_command_for_log(command: list[str]) -> str:
    if not command:
        return ""

    first_part = Path(command[0]).name
    if first_part.lower() == "hdc.exe":
        first_part = "hdc"
    return format_command([first_part, *command[1:]])


def _tail_text(text: str, limit: int = 80) -> str:
    lines = text.splitlines()
    if len(lines) <= limit:
        return text
    return "\n".join(lines[-limit:])


def _focused_window_name(window_dump: str) -> str | None:
    names_by_id: dict[str, str] = {}
    focus_id: str | None = None
    for raw_line in window_dump.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        focus_match = re.match(r"Focus window:\s*(\S+)", line, flags=re.I)
        if focus_match:
            focus_id = focus_match.group(1)
            continue
        parts = line.split()
        if (
            len(parts) >= 4
            and ":" not in parts[0]
            and parts[1].isdigit()
            and parts[3].isdigit()
        ):
            names_by_id[parts[3]] = parts[0]
    if focus_id is None:
        return None
    return names_by_id.get(focus_id)


def _timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _safe_relative_text(path: Path, start: Path) -> str:
    try:
        return path.resolve().relative_to(start.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _extract_first_value(text: str, marker: str) -> str | None:
    marker_index = text.find(marker)
    if marker_index < 0:
        return None

    start_index = marker_index + len(marker)
    end_index = text.find('"', start_index)
    if end_index < 0:
        return None

    value = text[start_index:end_index].strip()
    return value or None


def _discover_bundle_name(repo_dir: Path) -> str | None:
    app_scope_file = repo_dir / "AppScope" / "app.json5"
    if app_scope_file.is_file():
        app_scope_text = read_text_file(app_scope_file, "app_scope_file")
        bundle_name = _extract_first_value(app_scope_text, BUNDLE_NAME_MARKER)
        if bundle_name:
            return bundle_name

    for legacy_config in sorted(repo_dir.glob("*/src/main/config.json")):
        legacy_text = read_text_file(legacy_config, "legacy_config_file")
        bundle_name = _extract_first_value(legacy_text, BUNDLE_NAME_MARKER)
        if bundle_name:
            return bundle_name

    return None


def _discover_module_name(module_json_file: Path, module_root: Path) -> tuple[str | None, bool]:
    if module_json_file.is_file():
        module_text = read_text_file(module_json_file, "module_json_file")
        module_name = _extract_first_value(module_text, MODULE_NAME_MARKER)
        if module_name:
            return module_name, False

    guessed_module_name = f"{module_root.name}_test"
    return guessed_module_name, True


def _discover_legacy_test_module_name(legacy_config_file: Path) -> str | None:
    if not legacy_config_file.is_file():
        return None
    try:
        data = json.loads(read_text_file(legacy_config_file, "legacy_ohos_test_config_file"))
    except Exception:
        return None
    module = data.get("module") if isinstance(data, dict) else None
    if not isinstance(module, dict):
        return None
    distro = module.get("distro")
    if isinstance(distro, dict):
        distro_module_name = str(distro.get("moduleName") or "").strip()
        if distro_module_name:
            return distro_module_name
    module_name = str(module.get("name") or "").strip().lstrip(".")
    return module_name or None


def _discover_test_package_name(legacy_config_file: Path) -> str | None:
    if not legacy_config_file.is_file():
        return None
    legacy_text = read_text_file(legacy_config_file, "legacy_ohos_test_config_file")
    return _extract_first_value(legacy_text, PACKAGE_NAME_MARKER)


def _collect_test_files(source_dir: Path) -> tuple[Path, ...]:
    if not source_dir.is_dir():
        return ()
    test_files = [path.resolve() for path in source_dir.rglob("*.test.ets") if path.is_file()]
    scenario_file = source_dir / HOST_UI_SCENARIO_FILE
    if scenario_file.is_file():
        test_files.append(scenario_file.resolve())
    return tuple(sorted(test_files))


def _is_generated_source_dir(source_dir: Path, repo_dir: Path) -> bool:
    try:
        relative_parts = source_dir.resolve().relative_to(repo_dir.resolve()).parts
    except ValueError:
        relative_parts = source_dir.resolve().parts
    blocked_parts = {"build", ".test", ".hvigor", "node_modules"}
    return any(part in blocked_parts for part in relative_parts)


def _discover_instrument_targets(repo_dir: Path, bundle_name: str | None) -> list[TestTarget]:
    targets: list[TestTarget] = []
    for source_dir in sorted(path.resolve() for path in repo_dir.rglob("src/ohosTest") if path.is_dir()):
        if _is_generated_source_dir(source_dir, repo_dir):
            continue
        module_root = source_dir.parent.parent
        module_name, inferred = _discover_module_name(source_dir / "module.json5", module_root)
        legacy_module_name = _discover_legacy_test_module_name(source_dir / "config.json")
        if legacy_module_name:
            module_name = legacy_module_name
            inferred = False
        package_name = _discover_test_package_name(source_dir / "config.json")
        targets.append(
            TestTarget(
                kind="instrument",
                source_dir=source_dir,
                module_root=module_root,
                bundle_name=bundle_name,
                module_name=module_name,
                package_name=package_name,
                test_files=_collect_test_files(source_dir),
                module_name_inferred=inferred,
            )
        )
    return targets


def _discover_local_targets(repo_dir: Path, bundle_name: str | None) -> list[TestTarget]:
    targets: list[TestTarget] = []
    for source_dir in sorted(path.resolve() for path in repo_dir.rglob("src/test") if path.is_dir()):
        if _is_generated_source_dir(source_dir, repo_dir):
            continue
        module_root = source_dir.parent.parent
        targets.append(
            TestTarget(
                kind="local",
                source_dir=source_dir,
                module_root=module_root,
                bundle_name=bundle_name,
                module_name=module_root.name,
                package_name=None,
                test_files=_collect_test_files(source_dir),
            )
        )
    return targets


def _discover_targets(repo_dir: Path) -> list[TestTarget]:
    bundle_name = _discover_bundle_name(repo_dir)
    instrument_targets = _discover_instrument_targets(repo_dir, bundle_name)
    local_targets = _discover_local_targets(repo_dir, bundle_name)
    return [*instrument_targets, *local_targets]


def discover_test_targets(repo_path: str) -> list[str]:
    repo_dir = resolve_directory(repo_path, "repo_path")
    return [target.display_name(repo_dir) for target in _discover_targets(repo_dir)]


def _hdc_command() -> list[str]:
    hdc_path = find_command_path("hdc")
    if not hdc_path:
        raise FileNotFoundError("Unable to find 'hdc' in PATH.")
    return [str(hdc_path)]


def _resolve_single_target() -> str:
    online_targets = get_hdc_targets()
    requested_target = os.environ.get("HDC_TARGET")
    if requested_target:
      if requested_target not in online_targets:
          raise RuntimeError(
              f"The HDC_TARGET value is not online: {requested_target}. "
              f"Online targets: {', '.join(online_targets)}"
          )
      return requested_target
    if not online_targets:
        raise RuntimeError("No online Harmony target is available for running tests.")
    if len(online_targets) > 1:
        raise RuntimeError(
            "Multiple online Harmony targets were found. This step requires exactly one online target."
        )
    return online_targets[0]


def _extract_json_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _query_installed_module_names(device_target: str, bundle_name: str | None, repo_dir: Path) -> set[str]:
    if not bundle_name:
        return set()
    command = [
        *_hdc_command(),
        "-t",
        device_target,
        "shell",
        "bm",
        "dump",
        "-n",
        bundle_name,
    ]
    result = run_command(command, cwd=repo_dir, timeout_sec=30.0)
    if result.returncode != 0:
        return set()
    data = _extract_json_object(command_output(result))
    if not data:
        return set()

    names: set[str] = set()
    for key in ("hapModuleNames", "moduleNames"):
        value = data.get(key)
        if isinstance(value, list):
            names.update(str(item) for item in value if item)
    application_info = data.get("applicationInfo")
    if isinstance(application_info, dict):
        for module_info in application_info.get("moduleInfos") or []:
            if isinstance(module_info, dict) and module_info.get("moduleName"):
                names.add(str(module_info["moduleName"]))
    return names


def _build_instrument_command(
    device_target: str,
    test_target: TestTarget,
    class_filters: tuple[str, ...] = (),
    *,
    hypium_timeout_ms: int = DEFAULT_HYPIUM_TIMEOUT_MS,
    wait_time_ms: int = DEFAULT_WAIT_TIME_MS,
) -> list[str]:
    if not test_target.bundle_name:
        raise RuntimeError("Unable to build an instrument test command because the app bundle name was not found.")
    if not test_target.module_name and not test_target.package_name:
        raise RuntimeError("Unable to build an instrument test command because the test module name was not found.")

    command = [
        *_hdc_command(),
        "-t",
        device_target,
        "shell",
        "aa",
        "test",
        "-b",
        test_target.bundle_name,
        "-s",
        "unittest",
        DEFAULT_TEST_RUNNER,
    ]
    if test_target.package_name:
        command.extend(["-p", test_target.package_name])
    else:
        command.extend(["-m", str(test_target.module_name)])
    command.extend([
        "-s",
        "timeout",
        str(hypium_timeout_ms),
        "-w",
        str(wait_time_ms),
    ])
    if class_filters:
        command.extend(["-s", "class", ",".join(class_filters)])
    return command


def _build_instrument_command_variants(
    device_target: str,
    test_target: TestTarget,
    class_filters: tuple[str, ...] = (),
    *,
    hypium_timeout_ms: int = DEFAULT_HYPIUM_TIMEOUT_MS,
    wait_time_ms: int = DEFAULT_WAIT_TIME_MS,
) -> list[list[str]]:
    primary = _build_instrument_command(
        device_target,
        test_target,
        class_filters,
        hypium_timeout_ms=hypium_timeout_ms,
        wait_time_ms=wait_time_ms,
    )
    variants = [primary]
    if test_target.package_name and test_target.module_name:
        module_variant = list(primary)
        package_index = module_variant.index("-p")
        del module_variant[package_index:package_index + 2]
        insert_at = package_index
        module_variant[insert_at:insert_at] = ["-m", str(test_target.module_name)]
        variants.append(module_variant)

        qualified_package = f"{test_target.package_name}.{test_target.module_name}"
        qualified_variant = list(primary)
        qualified_variant[qualified_variant.index("-p") + 1] = qualified_package
        variants.append(qualified_variant)

    unique: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for variant in variants:
        key = tuple(variant)
        if key not in seen:
            seen.add(key)
            unique.append(variant)
    return unique


def _should_try_instrument_fallback(output_text: str) -> bool:
    normalized = output_text.lower()
    return (
        "openharmonytestrunner.abc" in normalized
        and "not found" in normalized
    )


def _format_target_block(title: str, targets: Iterable[TestTarget], repo_dir: Path) -> str:
    target_list = list(targets)
    if not target_list:
        return f"{title}=<none>\n"
    lines = [f"{title}_COUNT={len(target_list)}"]
    lines.extend(f"{title}_{index}={target.display_name(repo_dir)}" for index, target in enumerate(target_list, start=1))
    return "\n".join(lines) + "\n"


def _write_header(
    log_path: Path,
    repo_dir: Path,
    discovered_targets: list[TestTarget],
    runnable_targets: list[TestTarget],
    skipped_targets: list[TestTarget],
    *,
    sdk_meta: dict[str, Any] | None = None,
) -> None:
    workspace_root = _workspace_root()
    lines = [
        f"RUN_TESTS_TIMESTAMP={datetime.now().isoformat()}",
        "WORKSPACE_ROOT=.",
        f"REPO_PATH={format_path_for_display(repo_dir, start=workspace_root)}",
        "DEVECO_PATH=<deveco_path>",
        f"DISCOVERY_STRATEGY=scan src/ohosTest first, then src/test; prefer instrument targets during execution",
        f"DEFAULT_TEST_RUNNER={DEFAULT_TEST_RUNNER}",
        f"DEFAULT_WAIT_TIME_MS={DEFAULT_WAIT_TIME_MS}",
    ]
    if sdk_meta:
        c = sdk_meta.get("compileSdkVersion")
        v = sdk_meta.get("compatibleSdkVersion")
        sel = sdk_meta.get("sdk_selection_api_level")
        lines.extend(
            [
                f"BUILD_PROFILE_PATH={sdk_meta.get('build_profile_path', '')}",
                f"BUILD_PROFILE_PRODUCT={sdk_meta.get('product', '')}",
                f"BUILD_PROFILE_COMPILE_SDK_VERSION={c if c is not None else ''}",
                f"BUILD_PROFILE_COMPATIBLE_SDK_VERSION={v if v is not None else ''}",
                f"SDK_SELECTION_API_LEVEL={sel if sel is not None else ''}",
            ]
        )
        if sdk_meta.get("sdk_selection_note"):
            lines.append(f"SDK_SELECTION_NOTE={sdk_meta['sdk_selection_note']}")
    lines.append("")
    content = "\n".join(lines)
    content += _format_target_block("DISCOVERED_TARGET", discovered_targets, repo_dir)
    content += _format_target_block("RUNNABLE_INSTRUMENT_TARGET", runnable_targets, repo_dir)
    content += _format_target_block("SKIPPED_TARGET", skipped_targets, repo_dir)
    content += "\n"
    write_text_file(log_path, content)


def _append_log_block(log_path: Path, title: str, body: str) -> None:
    normalized_body = body.rstrip()
    content = f"## {title}\n"
    if normalized_body:
        content += normalized_body
    content += "\n\n"
    append_text_file(log_path, content)


def _load_host_ui_scenarios(repo_dir: Path) -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    for scenario_file in sorted(repo_dir.rglob(HOST_UI_SCENARIO_FILE)):
        if any(part in {"build", ".test", ".hvigor", "node_modules"} for part in scenario_file.parts):
            continue
        try:
            data = json.loads(scenario_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Invalid host UI scenario JSON: {scenario_file}: {exc}") from exc
        raw_scenarios = data.get("scenarios", data)
        if isinstance(raw_scenarios, dict):
            raw_scenarios = [raw_scenarios]
        if not isinstance(raw_scenarios, list):
            raise RuntimeError(f"Host UI scenario file must contain a scenario object or scenarios list: {scenario_file}")
        for raw in raw_scenarios:
            if not isinstance(raw, dict):
                raise RuntimeError(f"Host UI scenario entry is not an object: {scenario_file}")
            item = dict(raw)
            item["_scenario_file"] = scenario_file
            scenarios.append(item)
    return scenarios


def _hdc_shell(device_target: str, *args: str, timeout_sec: float = 30.0) -> str:
    command = [*_hdc_command(), "-t", device_target, "shell", *args]
    result = run_command(command, cwd=Path.cwd(), timeout_sec=timeout_sec)
    return command_output(result)


def _window_manager_dump(device_target: str) -> str:
    return _hdc_shell(
        device_target,
        "hidumper",
        "-s",
        "WindowManagerService",
        "-a",
        "-a",
        timeout_sec=20.0,
    )


def _display_manager_dump(device_target: str) -> str:
    return _hdc_shell(
        device_target,
        "hidumper",
        "-s",
        "DisplayManagerService",
        "-a",
        "-a",
        timeout_sec=20.0,
    )


def _parse_window_bounds(output: str, pattern: str) -> tuple[str, tuple[int, int, int, int]]:
    compiled = re.compile(pattern, flags=re.I)
    for raw_line in output.splitlines():
        if not compiled.search(raw_line):
            continue
        match = re.search(r"\[\s*(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s*\]", raw_line)
        if not match:
            continue
        left, top, width, height = (int(value) for value in match.groups())
        return raw_line.strip(), (left, top, width, height)
    raise RuntimeError(
        f"Unable to find WindowManagerService bounds matching {pattern!r}.\n"
        + _tail_text(output, limit=80)
    )


def _display_orientation(device_target: str) -> tuple[int | None, str]:
    output = _display_manager_dump(device_target)
    match = re.search(r"DisplayOrientation:\s*(-?\d+)", output)
    if not match:
        match = re.search(r"Rotation:\s*(-?\d+)", output)
    value = int(match.group(1)) if match else None
    return value, output


def _ensure_kika_input_current(device_target: str) -> str:
    lines: list[str] = []
    output = _hdc_shell(device_target, "hidumper", "-s", "InputMethodService", "-a", "-a", timeout_sec=20.0)
    if re.search(r"com\.example\.kikakeyboard/ServiceExtAbility[^}]+isCurrentIme\": \"true", output):
        return "ENSURE_KIKA_INPUT_CURRENT=already_current"

    def click(x: int, y: int, delay: float = 0.8) -> None:
        _hdc_shell(device_target, "uitest", "uiInput", "click", str(x), str(y), timeout_sec=10.0)
        time.sleep(delay)

    def click_attr(element_id: str, fallback: tuple[int, int], delay: float = 0.8) -> None:
        try:
            layout, _ = _dump_layout_json(device_target, Path.cwd(), "ensure_kika_input", element_id)
            bounds = _find_attr_bounds(layout, element_id)
        except Exception:
            bounds = None
        if bounds is None:
            lines.append(f"ENSURE_CLICK_ATTR_FALLBACK={element_id} at={fallback[0]},{fallback[1]}")
            click(fallback[0], fallback[1], delay)
            return
        left, top, right, bottom = bounds
        lines.append(f"ENSURE_CLICK_ATTR={element_id} bounds={bounds}")
        click((left + right) // 2, (top + bottom) // 2, delay)

    def click_attr_regex(pattern: str, fallback: tuple[int, int], delay: float = 0.8) -> None:
        try:
            layout, _ = _dump_layout_json(device_target, Path.cwd(), "ensure_kika_input", re.sub(r"[^A-Za-z0-9_.-]+", "_", pattern))
            compiled = re.compile(pattern, flags=re.I)
            bounds = None
            if layout:
                for node in _walk_json_nodes(layout):
                    text = " ".join(
                        _node_attr(node, attr_name)
                        for attr_name in ("id", "key", "accessibilityId", "description", "text", "hint", "type")
                    )
                    if compiled.search(text):
                        bounds = _parse_bounds_rect(_node_attr(node, "bounds"))
                        if bounds:
                            break
        except Exception:
            bounds = None
        if bounds is None:
            lines.append(f"ENSURE_CLICK_REGEX_FALLBACK={pattern} at={fallback[0]},{fallback[1]}")
            click(fallback[0], fallback[1], delay)
            return
        left, top, right, bottom = bounds
        lines.append(f"ENSURE_CLICK_REGEX={pattern} bounds={bounds}")
        click((left + right) // 2, (top + bottom) // 2, delay)

    def log_layout(label: str) -> None:
        try:
            layout, _ = _dump_layout_json(device_target, Path.cwd(), "ensure_kika_input", label)
            texts: list[str] = []
            if layout:
                for node in _walk_json_nodes(layout):
                    text = _node_attr(node, "text")
                    node_id = _node_attr(node, "id")
                    if text or node_id:
                        texts.append((text or node_id)[:80])
                    if len(texts) >= 12:
                        break
            lines.append(f"ENSURE_LAYOUT_{label}=" + " | ".join(texts))
        except Exception as exc:
            lines.append(f"ENSURE_LAYOUT_{label}_ERROR={exc}")

    _hdc_shell(device_target, "uitest", "uiInput", "keyEvent", "Home", timeout_sec=10.0)
    time.sleep(0.3)
    _hdc_shell(device_target, "aa", "force-stop", "com.huawei.hmos.settings", timeout_sec=10.0)
    time.sleep(0.3)
    _hdc_shell(device_target, "aa", "start", "-b", "com.huawei.hmos.settings", "-a", "com.huawei.hmos.settings.MainAbility", timeout_sec=15.0)
    time.sleep(1.0)
    log_layout("settings_home")
    layout, _ = _dump_layout_json(device_target, Path.cwd(), "ensure_kika_input", "settings_home_probe")
    for attempt in range(3):
        if _find_attr_bounds(layout, "system_and_updates") is not None:
            break
        lines.append(f"ENSURE_SETTINGS_HOME_RECOVER_BACK={attempt + 1}")
        _hdc_shell(device_target, "uitest", "uiInput", "keyEvent", "Back", timeout_sec=10.0)
        time.sleep(0.4)
        _hdc_shell(device_target, "aa", "start", "-b", "com.huawei.hmos.settings", "-a", "com.huawei.hmos.settings.MainAbility", timeout_sec=15.0)
        time.sleep(0.8)
        layout, _ = _dump_layout_json(device_target, Path.cwd(), "ensure_kika_input", f"settings_home_recover_{attempt + 1}")
    if _find_attr_bounds(layout, "system_and_updates") is None:
        log_layout("settings_home_unrecovered")
    click_attr("system_and_updates", (630, 2121))
    log_layout("system_page")
    click_attr("Setting.System.time_and_language.set_input", (600, 760))
    log_layout("input_page")
    click_attr_regex(r"entry_(title|text|image)_com\.example\.kikakeyboard|kikaInput", (1030, 958))
    time.sleep(0.5)
    log_layout("kika_page")
    output = _hdc_shell(device_target, "hidumper", "-s", "InputMethodService", "-a", "-a", timeout_sec=20.0)
    if re.search(r"com\.example\.kikakeyboard/ServiceExtAbility[^}]+isCurrentIme\": \"true", output):
        return "ENSURE_KIKA_INPUT_CURRENT=enabled_and_current"

    layout, _ = _dump_layout_json(device_target, Path.cwd(), "ensure_kika_input", "after_enable")
    if _find_attr_bounds(layout, "entry_toggle_input_switch_pc") is not None:
        click_attr("entry_toggle_input_switch_pc", (1110, 452))
        _hdc_shell(device_target, "uitest", "uiInput", "keyEvent", "Back", timeout_sec=10.0)
        time.sleep(0.8)
    elif _layout_contains_text(layout, "kikaInput"):
        _hdc_shell(device_target, "uitest", "uiInput", "keyEvent", "Back", timeout_sec=10.0)
        time.sleep(0.8)

    click_attr("entry_title_other_input_settings", (600, 580))
    time.sleep(0.4)
    click_attr_regex(r"SingleChoiceMenuItem_other_input_settings_input_select_1|kikaInput", (850, 935))
    time.sleep(0.8)
    output = _hdc_shell(device_target, "hidumper", "-s", "InputMethodService", "-a", "-a", timeout_sec=20.0)
    lines.append(_tail_text(output, limit=20))
    if not re.search(r"com\.example\.kikakeyboard/ServiceExtAbility[^}]+isCurrentIme\": \"true", output):
        raise RuntimeError("Unable to switch KikaInput to current IME.\n" + "\n".join(lines))
    return "ENSURE_KIKA_INPUT_CURRENT=switched"


def _append_aa_start_params(command: list[str], params: dict[str, Any]) -> None:
    for key, value in params.items():
        if isinstance(value, bool):
            command.extend(["--pb", str(key), "true" if value else "false"])
        elif isinstance(value, int) and not isinstance(value, bool):
            command.extend(["--pi", str(key), str(value)])
        else:
            command.extend(["--ps", str(key), str(value)])


def _capture_screen(
    device_target: str,
    repo_dir: Path,
    scenario_name: str,
    suffix: str,
) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", scenario_name).strip("_") or "host_ui"
    local_path = repo_dir / "entry" / "build" / "default" / "outputs" / "host_ui" / f"{safe_name}_{suffix}.jpeg"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    remote_path = f"/data/local/tmp/{safe_name}_{suffix}.jpeg"
    _hdc_shell(device_target, "snapshot_display", "-f", remote_path, timeout_sec=20.0)
    recv_command = [*_hdc_command(), "-t", device_target, "file", "recv", remote_path, str(local_path)]
    recv_result = run_command(recv_command, cwd=repo_dir, timeout_sec=20.0)
    if recv_result.returncode != 0:
        raise RuntimeError(command_output(recv_result))
    return local_path


def _dark_region_ratio(image_path: Path, region: dict[str, Any]) -> float:
    with Image.open(image_path) as image:
        rgb = image.convert("RGB")
        width, height = rgb.size
        left = int(float(region.get("left", 0.0)) * width)
        top = int(float(region.get("top", 0.0)) * height)
        right = int(float(region.get("right", 1.0)) * width)
        bottom = int(float(region.get("bottom", 1.0)) * height)
        left = max(0, min(left, width - 1))
        top = max(0, min(top, height - 1))
        right = max(left + 1, min(right, width))
        bottom = max(top + 1, min(bottom, height))
        pixels = list(rgb.crop((left, top, right, bottom)).getdata())
    if not pixels:
        return 0.0
    dark_count = 0
    for red, green, blue in pixels:
        if red < 120 and green < 120 and blue < 120 and abs(red - green) < 45 and abs(green - blue) < 45:
            dark_count += 1
    return dark_count / len(pixels)


def _color_region_ratio(image_path: Path, region: dict[str, Any], color: str) -> float:
    with Image.open(image_path) as image:
        rgb = image.convert("RGB")
        width, height = rgb.size
        left = int(float(region.get("left", 0.0)) * width)
        top = int(float(region.get("top", 0.0)) * height)
        right = int(float(region.get("right", 1.0)) * width)
        bottom = int(float(region.get("bottom", 1.0)) * height)
        left = max(0, min(left, width - 1))
        top = max(0, min(top, height - 1))
        right = max(left + 1, min(right, width))
        bottom = max(top + 1, min(bottom, height))
        pixels = list(rgb.crop((left, top, right, bottom)).getdata())
    if not pixels:
        return 0.0
    color_name = color.lower()
    match_count = 0
    for red, green, blue in pixels:
        if color_name == "blue":
            if blue >= 150 and red <= 80 and green <= 140:
                match_count += 1
        elif color_name == "white":
            if red >= 220 and green >= 220 and blue >= 220:
                match_count += 1
        elif color_name == "gray":
            if abs(red - green) <= 20 and abs(green - blue) <= 20 and 120 <= red <= 220:
                match_count += 1
        elif color_name == "black":
            if red < 80 and green < 80 and blue < 80:
                match_count += 1
        else:
            raise RuntimeError(f"Unsupported captureColorRegion color: {color}")
    return match_count / len(pixels)


def _find_latest_remote_file(
    device_target: str,
    patterns: list[str],
    *,
    roots: list[str] | None = None,
    timeout_sec: float = 20.0,
) -> str:
    search_roots = roots or ["/storage", "/data/storage/el2/base/files", "/data/app/el2"]
    find_parts = ["find", *search_roots]
    for index, pattern in enumerate(patterns):
        if index == 0:
            find_parts.extend(["-name", pattern])
        else:
            find_parts.extend(["-o", "-name", pattern])
    find_parts.extend(["2>/dev/null"])
    output = _hdc_shell(device_target, "sh", "-c", " ".join(find_parts), timeout_sec=timeout_sec)
    candidates = [line.strip() for line in output.splitlines() if line.strip()]
    if not candidates:
        raise RuntimeError(f"No remote files matched patterns {patterns!r} under {search_roots!r}.")

    newest_path = ""
    newest_mtime = -1
    for path in candidates:
        stat_output = _hdc_shell(device_target, "stat", "-c", "%Y", path, timeout_sec=10.0).strip()
        try:
            mtime = int(stat_output.splitlines()[-1])
        except (ValueError, IndexError):
            mtime = 0
        if mtime >= newest_mtime:
            newest_path = path
            newest_mtime = mtime
    return newest_path


def _pull_remote_file(device_target: str, repo_dir: Path, remote_path: str, scenario_name: str, suffix: str) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", scenario_name).strip("_") or "host_ui"
    remote_name = Path(remote_path.replace("\\", "/")).name or f"{suffix}.jpg"
    local_path = repo_dir / "entry" / "build" / "default" / "outputs" / "host_ui" / f"{safe_name}_{suffix}_{remote_name}"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    recv_command = [*_hdc_command(), "-t", device_target, "file", "recv", remote_path, str(local_path)]
    recv_result = run_command(recv_command, cwd=repo_dir, timeout_sec=20.0)
    if recv_result.returncode != 0:
        raise RuntimeError(command_output(recv_result))
    return local_path


def _qr_dark_bounds(image_path: Path) -> tuple[int, int, float]:
    with Image.open(image_path) as img:
        rgb = img.convert("RGB")
        width, height = rgb.size
        min_x = width
        min_y = height
        max_x = -1
        max_y = -1
        for y in range(height):
            for x in range(width):
                red, green, blue = rgb.getpixel((x, y))
                if red < 80 and green < 80 and blue < 80:
                    min_x = min(min_x, x)
                    min_y = min(min_y, y)
                    max_x = max(max_x, x)
                    max_y = max(max_y, y)
        if max_x < 0:
            return width, height, 0.0
        qr_width = max_x - min_x + 1
        qr_height = max_y - min_y + 1
        fill_ratio = min(qr_width, qr_height) / max(min(width, height), 1)
        return width, height, fill_ratio


def _assert_latest_saved_qr_crop(
    device_target: str,
    repo_dir: Path,
    scenario_name: str,
    action: dict[str, Any],
    step_index: int,
) -> str:
    delay_ms = int(action.get("delayMs", 0))
    if delay_ms > 0:
        time.sleep(delay_ms / 1000.0)
    patterns = [str(item) for item in action.get("patterns") or ["IMG_*.jpg", "IMG_*.jpeg"]]
    roots = [str(item) for item in action.get("roots") or ["/storage", "/data/storage/el2/base/files", "/data/app/el2"]]
    remote_path = _find_latest_remote_file(device_target, patterns, roots=roots)
    local_path = _pull_remote_file(device_target, repo_dir, remote_path, scenario_name, f"saved_qr_{step_index}")
    width, height, fill_ratio = _qr_dark_bounds(local_path)
    min_width = int(action.get("minWidth", 300))
    max_width = int(action.get("maxWidth", 360))
    min_height = int(action.get("minHeight", 300))
    max_height = int(action.get("maxHeight", 360))
    min_fill_ratio = float(action.get("minFillRatio", 0.80))
    passed = (
        width >= min_width
        and width <= max_width
        and height >= min_height
        and height <= max_height
        and fill_ratio >= min_fill_ratio
    )
    visual_name = str(action.get("visualName") or "saved_qr_crop")
    return "\n".join(
        [
            f"SAVED_QR_REMOTE_PATH={remote_path}",
            f"SAVED_QR_LOCAL_PATH={format_path_for_display(local_path, start=repo_dir)}",
            f"SAVED_QR_WIDTH={width}",
            f"SAVED_QR_HEIGHT={height}",
            f"SAVED_QR_FILL_RATIO={fill_ratio:.6f}",
            f"SAVED_QR_MIN_WIDTH={min_width}",
            f"SAVED_QR_MAX_WIDTH={max_width}",
            f"SAVED_QR_MIN_HEIGHT={min_height}",
            f"SAVED_QR_MAX_HEIGHT={max_height}",
            f"SAVED_QR_MIN_FILL_RATIO={min_fill_ratio:.6f}",
            f"SAVED_QR_VISUAL_NAME={visual_name}",
            f"SAVED_QR_RESULT={'PASS' if passed else 'FAIL'}",
        ]
    )


def _parse_bounds(bounds: str | None) -> tuple[int, int] | None:
    if not bounds:
        return None
    match = re.search(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", bounds)
    if not match:
        return None
    left, top, right, bottom = (int(value) for value in match.groups())
    return ((left + right) // 2, (top + bottom) // 2)


def _parse_bounds_rect(bounds: str | None) -> tuple[int, int, int, int] | None:
    if not bounds:
        return None
    match = re.search(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", bounds)
    if not match:
        return None
    return tuple(int(value) for value in match.groups())  # type: ignore[return-value]


def _walk_json_nodes(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json_nodes(child)


def _node_attr(node: dict[str, Any], key: str) -> str:
    value = node.get(key)
    if value is None and isinstance(node.get("attributes"), dict):
        value = node["attributes"].get(key)
    return str(value or "")


def _dump_layout_json(device_target: str, repo_dir: Path, scenario_name: str, suffix: str) -> tuple[dict[str, Any] | None, str]:
    output = _hdc_shell(device_target, "uitest", "dumpLayout", timeout_sec=20.0)
    match = re.search(r"(/data/local/tmp/layout_[0-9]+\.json)", output)
    if not match:
        return None, f"dumpLayout did not return a layout path: {output.strip()}"
    remote_path = match.group(1)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", scenario_name).strip("_") or "host_ui"
    local_path = repo_dir / "entry" / "build" / "default" / "outputs" / "host_ui" / f"{safe_name}_{suffix}.json"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    recv_command = [*_hdc_command(), "-t", device_target, "file", "recv", remote_path, str(local_path)]
    recv_result = run_command(recv_command, cwd=repo_dir, timeout_sec=20.0)
    if recv_result.returncode != 0:
        return None, command_output(recv_result)
    try:
        return json.loads(local_path.read_text(encoding="utf-8")), f"LAYOUT={format_path_for_display(local_path, start=repo_dir)}"
    except Exception as exc:
        return None, f"Failed to parse layout {local_path}: {exc}"


def _find_text_center(layout: dict[str, Any] | None, text: str) -> tuple[int, int] | None:
    if not layout:
        return None
    for node in _walk_json_nodes(layout):
        node_text = _node_attr(node, "text") or _node_attr(node, "description")
        if node_text == text:
            center = _parse_bounds(_node_attr(node, "bounds"))
            if center:
                return center
    return None


def _find_attr_bounds(layout: dict[str, Any] | None, value: str) -> tuple[int, int, int, int] | None:
    if not layout:
        return None
    for node in _walk_json_nodes(layout):
        for attr_name in ("id", "key", "accessibilityId", "description", "text", "hint"):
            if _node_attr(node, attr_name) == value:
                bounds = _parse_bounds_rect(_node_attr(node, "bounds"))
                if bounds:
                    return bounds
    return None


def _layout_viewport_bounds(layout: dict[str, Any] | None) -> tuple[int, int, int, int] | None:
    if not layout:
        return None
    root_bounds = _parse_bounds_rect(_node_attr(layout, "bounds"))
    if root_bounds:
        return root_bounds
    best: tuple[int, int, int, int] | None = None
    best_area = -1
    for node in _walk_json_nodes(layout):
        bounds = _parse_bounds_rect(_node_attr(node, "bounds"))
        if not bounds:
            continue
        area = max(bounds[2] - bounds[0], 0) * max(bounds[3] - bounds[1], 0)
        if area > best_area:
            best = bounds
            best_area = area
    return best


def _layout_contains_text(layout: dict[str, Any] | None, text: str) -> bool:
    if not layout:
        return False
    for node in _walk_json_nodes(layout):
        node_text = _node_attr(node, "text") or _node_attr(node, "description")
        if node_text == text:
            return True
    return False


def _find_node_by_attr(layout: dict[str, Any] | None, value: str) -> dict[str, Any] | None:
    if not layout:
        return None
    for node in _walk_json_nodes(layout):
        for attr_name in ("id", "key", "accessibilityId", "description", "text", "hint"):
            if _node_attr(node, attr_name) == value:
                return node
    return None


def _node_visible_text(node: dict[str, Any] | None) -> str:
    if not node:
        return ""
    for attr_name in ("text", "description", "hint"):
        text = _node_attr(node, attr_name)
        if text:
            return text
    return ""


def _wait_for_layout_text(
    device_target: str,
    repo_dir: Path,
    scenario_name: str,
    text: str,
    suffix: str,
    *,
    timeout_sec: float,
    interval_sec: float = 0.5,
) -> tuple[dict[str, Any] | None, str]:
    deadline = time.monotonic() + max(timeout_sec, 0.0)
    last_layout: dict[str, Any] | None = None
    last_info = ""
    attempt = 0
    while True:
        attempt += 1
        layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"{suffix}_{attempt}")
        last_layout = layout
        last_info = info
        if _layout_contains_text(layout, text):
            return layout, f"{info}\nWAIT_FOR_TEXT_ATTEMPTS={attempt}"
        if time.monotonic() >= deadline:
            break
        time.sleep(max(interval_sec, 0.1))
    return last_layout, f"{last_info}\nWAIT_FOR_TEXT_ATTEMPTS={attempt}"


def _layout_text_regex_count(layout: dict[str, Any] | None, pattern: str) -> int:
    if not layout or not pattern:
        return 0
    try:
        compiled = re.compile(pattern, re.MULTILINE)
    except re.error as exc:
        raise RuntimeError(f"Invalid layout text regex {pattern!r}: {exc}") from exc

    count = 0
    for node in _walk_json_nodes(layout):
        for attr_name in ("text", "description"):
            node_text = _node_attr(node, attr_name)
            if node_text:
                count += len(compiled.findall(node_text))
    return count


def _layout_contains_attr(layout: dict[str, Any] | None, attr_name: str, attr_value: str) -> bool:
    if not layout:
        return False
    for node in _walk_json_nodes(layout):
        if _node_attr(node, attr_name) == attr_value:
            return True
    return False


def _walk_layout_component_nodes(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        if isinstance(value.get("attributes"), dict):
            yield value
            children = value.get("children")
            if isinstance(children, list):
                for child in children:
                    yield from _walk_layout_component_nodes(child)
        else:
            for child in value.values():
                yield from _walk_layout_component_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_layout_component_nodes(child)


def _layout_component_index(layout: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not layout:
        return {}
    indexed: dict[str, dict[str, Any]] = {}
    for node in _walk_layout_component_nodes(layout):
        hierarchy = _node_attr(node, "hierarchy")
        if hierarchy and hierarchy not in indexed:
            indexed[hierarchy] = node
    return indexed


def _parent_hierarchy(hierarchy: str) -> str:
    parts = hierarchy.split(",")
    if len(parts) <= 1:
        return ""
    return ",".join(parts[:-1])


def _find_text_node(layout: dict[str, Any] | None, text: str) -> dict[str, Any] | None:
    if not layout:
        return None
    for node in _walk_layout_component_nodes(layout):
        node_text = _node_attr(node, "text") or _node_attr(node, "description")
        if node_text == text:
            return node
    return None


def _ancestor_hierarchies(hierarchy: str) -> list[str]:
    if not hierarchy:
        return []
    parts = hierarchy.split(",")
    return [",".join(parts[:index]) for index in range(len(parts), 0, -1)]


def _assert_text_group_vertical_position(
    layout: dict[str, Any] | None,
    *,
    top_text: str,
    bottom_text: str,
    min_center_ratio: float | None,
    max_center_ratio: float | None,
    min_top_margin_ratio: float,
    min_bottom_margin_ratio: float,
) -> tuple[dict[str, float], str]:
    viewport_bounds = _layout_viewport_bounds(layout)
    if not viewport_bounds:
        raise RuntimeError("No viewport bounds were available in the dumped layout.")
    nodes_by_hierarchy = _layout_component_index(layout)
    if not nodes_by_hierarchy:
        raise RuntimeError("No component nodes were available in the dumped layout.")

    top_node = _find_text_node(layout, top_text)
    bottom_node = _find_text_node(layout, bottom_text)
    if top_node is None:
        raise RuntimeError(f"Unable to find top dialog text {top_text!r}.")
    if bottom_node is None:
        raise RuntimeError(f"Unable to find bottom dialog text {bottom_text!r}.")

    top_hierarchy = _node_attr(top_node, "hierarchy")
    bottom_hierarchy = _node_attr(bottom_node, "hierarchy")
    top_ancestors = _ancestor_hierarchies(top_hierarchy)
    bottom_ancestors = set(_ancestor_hierarchies(bottom_hierarchy))
    common_hierarchy = next((item for item in top_ancestors if item in bottom_ancestors), "")
    common_node = nodes_by_hierarchy.get(common_hierarchy)
    if common_node is None:
        raise RuntimeError(
            f"Unable to find a common visible parent for {top_text!r} and {bottom_text!r}."
        )

    group_bounds = _parse_bounds_rect(_node_attr(common_node, "bounds"))
    if not group_bounds:
        raise RuntimeError(
            f"Common parent for {top_text!r} and {bottom_text!r} has no bounds."
        )
    view_left, view_top, view_right, view_bottom = viewport_bounds
    _left, top, _right, bottom = group_bounds
    viewport_height = max(view_bottom - view_top, 1)
    center_y = (top + bottom) / 2.0
    center_ratio = (center_y - view_top) / viewport_height
    top_margin_ratio = (top - view_top) / viewport_height
    bottom_margin_ratio = (view_bottom - bottom) / viewport_height

    failures: list[str] = []
    if min_center_ratio is not None and center_ratio < min_center_ratio:
        failures.append(f"center ratio {center_ratio:.3f} < {min_center_ratio:.3f}")
    if max_center_ratio is not None and center_ratio > max_center_ratio:
        failures.append(f"center ratio {center_ratio:.3f} > {max_center_ratio:.3f}")
    if top_margin_ratio < min_top_margin_ratio:
        failures.append(f"top margin ratio {top_margin_ratio:.3f} < {min_top_margin_ratio:.3f}")
    if bottom_margin_ratio < min_bottom_margin_ratio:
        failures.append(
            f"bottom margin ratio {bottom_margin_ratio:.3f} < {min_bottom_margin_ratio:.3f}"
        )

    detail = (
        f"ASSERT_TEXT_GROUP_VERTICAL_POSITION topText={top_text} bottomText={bottom_text} "
        f"groupBounds={group_bounds} viewportBounds={viewport_bounds} "
        f"centerRatio={center_ratio:.3f} "
        f"minCenterRatio={min_center_ratio:.3f}" if min_center_ratio is not None else
        f"ASSERT_TEXT_GROUP_VERTICAL_POSITION topText={top_text} bottomText={bottom_text} "
        f"groupBounds={group_bounds} viewportBounds={viewport_bounds} "
        f"centerRatio={center_ratio:.3f} minCenterRatio=<none>"
    )
    detail += (
        f" maxCenterRatio={max_center_ratio:.3f}" if max_center_ratio is not None else
        " maxCenterRatio=<none>"
    )
    detail += (
        f" topMarginRatio={top_margin_ratio:.3f} bottomMarginRatio={bottom_margin_ratio:.3f} "
        f"minTopMarginRatio={min_top_margin_ratio:.3f} "
        f"minBottomMarginRatio={min_bottom_margin_ratio:.3f} "
        f"commonHierarchy={common_hierarchy}"
    )
    if failures:
        raise RuntimeError(f"Text group is not in the expected vertical position: {'; '.join(failures)}. {detail}")
    return {
        "centerRatio": center_ratio,
        "topMarginRatio": top_margin_ratio,
        "bottomMarginRatio": bottom_margin_ratio,
    }, detail


def _assert_dialog_panel_vertical_position(
    layout: dict[str, Any] | None,
    *,
    min_center_ratio: float | None,
    max_center_ratio: float | None,
    min_top_margin_ratio: float,
    min_bottom_margin_ratio: float,
) -> tuple[dict[str, float], str]:
    viewport_bounds = _layout_viewport_bounds(layout)
    if not viewport_bounds:
        raise RuntimeError("No viewport bounds were available in the dumped layout.")
    nodes_by_hierarchy = _layout_component_index(layout)
    if not nodes_by_hierarchy:
        raise RuntimeError("No component nodes were available in the dumped layout.")

    dialog_node = next(
        (node for node in nodes_by_hierarchy.values() if _node_attr(node, "type") == "Dialog"),
        None,
    )
    if dialog_node is None:
        raise RuntimeError("Unable to find a visible Dialog component.")

    dialog_hierarchy = _node_attr(dialog_node, "hierarchy")
    candidates: list[tuple[int, str, tuple[int, int, int, int]]] = []
    view_left, view_top, view_right, view_bottom = viewport_bounds
    viewport_width = max(view_right - view_left, 1)
    viewport_height = max(view_bottom - view_top, 1)
    viewport_area = viewport_width * viewport_height

    for hierarchy, node in nodes_by_hierarchy.items():
        if hierarchy == dialog_hierarchy or not hierarchy.startswith(f"{dialog_hierarchy},"):
            continue
        bounds = _parse_bounds_rect(_node_attr(node, "bounds"))
        if not bounds:
            continue
        left, top, right, bottom = bounds
        width = max(right - left, 0)
        height = max(bottom - top, 0)
        area = width * height
        if area <= 0:
            continue
        if area >= viewport_area * 0.85:
            continue
        if width < viewport_width * 0.25 or height < viewport_height * 0.10:
            continue
        depth = hierarchy.count(",")
        candidates.append((depth, hierarchy, bounds))

    if not candidates:
        raise RuntimeError("Unable to find a dialog panel inside the Dialog component.")

    candidates.sort(key=lambda item: (item[0], -((item[2][2] - item[2][0]) * (item[2][3] - item[2][1]))))
    panel_hierarchy, panel_bounds = candidates[0][1], candidates[0][2]
    _left, top, _right, bottom = panel_bounds
    center_y = (top + bottom) / 2.0
    center_ratio = (center_y - view_top) / viewport_height
    top_margin_ratio = (top - view_top) / viewport_height
    bottom_margin_ratio = (view_bottom - bottom) / viewport_height

    failures: list[str] = []
    if min_center_ratio is not None and center_ratio < min_center_ratio:
        failures.append(f"center ratio {center_ratio:.3f} < {min_center_ratio:.3f}")
    if max_center_ratio is not None and center_ratio > max_center_ratio:
        failures.append(f"center ratio {center_ratio:.3f} > {max_center_ratio:.3f}")
    if top_margin_ratio < min_top_margin_ratio:
        failures.append(f"top margin ratio {top_margin_ratio:.3f} < {min_top_margin_ratio:.3f}")
    if bottom_margin_ratio < min_bottom_margin_ratio:
        failures.append(
            f"bottom margin ratio {bottom_margin_ratio:.3f} < {min_bottom_margin_ratio:.3f}"
        )

    detail = (
        "ASSERT_DIALOG_PANEL_VERTICAL_POSITION "
        f"panelBounds={panel_bounds} viewportBounds={viewport_bounds} "
        f"centerRatio={center_ratio:.3f} "
        f"minCenterRatio={min_center_ratio:.3f}" if min_center_ratio is not None else
        "ASSERT_DIALOG_PANEL_VERTICAL_POSITION "
        f"panelBounds={panel_bounds} viewportBounds={viewport_bounds} "
        f"centerRatio={center_ratio:.3f} minCenterRatio=<none>"
    )
    detail += (
        f" maxCenterRatio={max_center_ratio:.3f}" if max_center_ratio is not None else
        " maxCenterRatio=<none>"
    )
    detail += (
        f" topMarginRatio={top_margin_ratio:.3f} bottomMarginRatio={bottom_margin_ratio:.3f} "
        f"minTopMarginRatio={min_top_margin_ratio:.3f} "
        f"minBottomMarginRatio={min_bottom_margin_ratio:.3f} "
        f"panelHierarchy={panel_hierarchy}"
    )
    if failures:
        raise RuntimeError(f"Dialog panel is not in the expected vertical position: {'; '.join(failures)}. {detail}")
    return {
        "centerRatio": center_ratio,
        "topMarginRatio": top_margin_ratio,
        "bottomMarginRatio": bottom_margin_ratio,
    }, detail


def _assert_text_right_sibling_gap(
    layout: dict[str, Any] | None,
    *,
    text: str,
    sibling_type: str,
    min_gap_px: int,
    max_gap_px: int | None = None,
) -> tuple[int, str]:
    nodes_by_hierarchy = _layout_component_index(layout)
    if not nodes_by_hierarchy:
        raise RuntimeError("No component nodes were available in the dumped layout.")

    candidates: list[tuple[int, tuple[int, int, int, int], tuple[int, int, int, int], str]] = []
    for node in nodes_by_hierarchy.values():
        node_text = _node_attr(node, "text") or _node_attr(node, "description")
        if node_text != text:
            continue
        node_bounds = _parse_bounds_rect(_node_attr(node, "bounds"))
        hierarchy = _node_attr(node, "hierarchy")
        parent = nodes_by_hierarchy.get(_parent_hierarchy(hierarchy))
        if not node_bounds or not parent:
            continue
        for child in parent.get("children") or []:
            if not isinstance(child, dict) or _node_attr(child, "type") != sibling_type:
                continue
            sibling_bounds = _parse_bounds_rect(_node_attr(child, "bounds"))
            if not sibling_bounds or sibling_bounds[0] < node_bounds[2]:
                continue
            gap = sibling_bounds[0] - node_bounds[2]
            candidates.append((gap, node_bounds, sibling_bounds, _node_attr(parent, "bounds")))

    if not candidates:
        raise RuntimeError(
            f"Unable to find text {text!r} with a right-side {sibling_type!r} sibling in the same parent row."
        )

    gap, text_bounds, sibling_bounds, parent_bounds = sorted(candidates, key=lambda item: item[0], reverse=True)[0]
    if gap < min_gap_px:
        raise RuntimeError(
            f"Gap from {text!r} to right-side {sibling_type!r} is too small: "
            f"{gap}px < {min_gap_px}px; text_bounds={text_bounds}; "
            f"sibling_bounds={sibling_bounds}; parent_bounds={parent_bounds}"
        )
    if max_gap_px is not None and gap > max_gap_px:
        raise RuntimeError(
            f"Gap from {text!r} to right-side {sibling_type!r} is too large: "
            f"{gap}px > {max_gap_px}px; text_bounds={text_bounds}; "
            f"sibling_bounds={sibling_bounds}; parent_bounds={parent_bounds}"
        )
    detail = (
        f"ASSERT_TEXT_RIGHT_SIBLING_GAP text={text} siblingType={sibling_type} "
        f"gapPx={gap} minGapPx={min_gap_px} "
        f"maxGapPx={max_gap_px if max_gap_px is not None else '<none>'} "
        f"textBounds={text_bounds} siblingBounds={sibling_bounds} parentBounds={parent_bounds}"
    )
    return gap, detail


def _assert_text_inside_parent(
    layout: dict[str, Any] | None,
    *,
    text: str,
    parent_type: str,
    min_left_px: int,
    min_right_px: int,
    min_top_px: int,
    min_bottom_px: int,
) -> tuple[tuple[int, int, int, int], str]:
    nodes_by_hierarchy = _layout_component_index(layout)
    if not nodes_by_hierarchy:
        raise RuntimeError("No component nodes were available in the dumped layout.")

    candidates: list[tuple[tuple[int, int, int, int], tuple[int, int, int, int]]] = []
    for node in nodes_by_hierarchy.values():
        node_text = _node_attr(node, "text") or _node_attr(node, "description")
        if node_text != text:
            continue
        text_bounds = _parse_bounds_rect(_node_attr(node, "bounds"))
        hierarchy = _node_attr(node, "hierarchy")
        parent = nodes_by_hierarchy.get(_parent_hierarchy(hierarchy))
        if not text_bounds or not parent or _node_attr(parent, "type") != parent_type:
            continue
        parent_bounds = _parse_bounds_rect(_node_attr(parent, "bounds"))
        if parent_bounds:
            candidates.append((text_bounds, parent_bounds))

    if not candidates:
        raise RuntimeError(f"Unable to find text {text!r} inside parent type {parent_type!r}.")

    text_bounds, parent_bounds = sorted(
        candidates,
        key=lambda item: (
            item[0][0] - item[1][0],
            item[1][2] - item[0][2],
            item[0][1] - item[1][1],
            item[1][3] - item[0][3],
        ),
    )[0]
    left_gap = text_bounds[0] - parent_bounds[0]
    right_gap = parent_bounds[2] - text_bounds[2]
    top_gap = text_bounds[1] - parent_bounds[1]
    bottom_gap = parent_bounds[3] - text_bounds[3]
    failures: list[str] = []
    if left_gap < min_left_px:
        failures.append(f"left {left_gap}px < {min_left_px}px")
    if right_gap < min_right_px:
        failures.append(f"right {right_gap}px < {min_right_px}px")
    if top_gap < min_top_px:
        failures.append(f"top {top_gap}px < {min_top_px}px")
    if bottom_gap < min_bottom_px:
        failures.append(f"bottom {bottom_gap}px < {min_bottom_px}px")
    detail = (
        f"ASSERT_TEXT_INSIDE_PARENT text={text} parentType={parent_type} "
        f"leftGapPx={left_gap} rightGapPx={right_gap} topGapPx={top_gap} bottomGapPx={bottom_gap} "
        f"minLeftPx={min_left_px} minRightPx={min_right_px} minTopPx={min_top_px} minBottomPx={min_bottom_px} "
        f"textBounds={text_bounds} parentBounds={parent_bounds}"
    )
    if failures:
        raise RuntimeError("Text is clipped or too close to its parent edge: " + "; ".join(failures) + "; " + detail)
    return text_bounds, detail


def _assert_button_pair_spread_across_anchor(
    layout: dict[str, Any] | None,
    *,
    left_text: str,
    right_text: str,
    anchor_type: str,
    min_gap_ratio: float,
    left_center_max_ratio: float,
    right_center_min_ratio: float,
) -> tuple[dict[str, float], str]:
    if not layout:
        raise RuntimeError("No layout was available for button pair spread assertion.")

    anchors: list[tuple[int, tuple[int, int, int, int]]] = []
    left_buttons: list[tuple[int, tuple[int, int, int, int]]] = []
    right_buttons: list[tuple[int, tuple[int, int, int, int]]] = []
    for node in _walk_layout_component_nodes(layout):
        bounds = _parse_bounds_rect(_node_attr(node, "bounds"))
        if not bounds:
            continue
        node_type = _node_attr(node, "type")
        node_text = _node_attr(node, "text") or _node_attr(node, "description")
        area = max(bounds[2] - bounds[0], 0) * max(bounds[3] - bounds[1], 0)
        if node_type == anchor_type:
            anchors.append((area, bounds))
        if node_type == "Button" and node_text == left_text:
            left_buttons.append((area, bounds))
        if node_type == "Button" and node_text == right_text:
            right_buttons.append((area, bounds))

    if not anchors:
        raise RuntimeError(f"Unable to find anchor component type {anchor_type!r}.")
    if not left_buttons:
        raise RuntimeError(f"Unable to find left button text {left_text!r}.")
    if not right_buttons:
        raise RuntimeError(f"Unable to find right button text {right_text!r}.")

    anchor_bounds = sorted(anchors, key=lambda item: item[0], reverse=True)[0][1]
    left_bounds = sorted(left_buttons, key=lambda item: item[0], reverse=True)[0][1]
    right_bounds = sorted(right_buttons, key=lambda item: item[0], reverse=True)[0][1]
    anchor_width = max(anchor_bounds[2] - anchor_bounds[0], 1)
    left_center = (left_bounds[0] + left_bounds[2]) / 2.0
    right_center = (right_bounds[0] + right_bounds[2]) / 2.0
    gap = right_bounds[0] - left_bounds[2]
    gap_ratio = gap / anchor_width
    left_ratio = (left_center - anchor_bounds[0]) / anchor_width
    right_ratio = (right_center - anchor_bounds[0]) / anchor_width

    failures: list[str] = []
    if right_center <= left_center:
        failures.append(f"right center {right_center:.1f} <= left center {left_center:.1f}")
    if gap_ratio < min_gap_ratio:
        failures.append(f"gap ratio {gap_ratio:.3f} < {min_gap_ratio:.3f}")
    if left_ratio > left_center_max_ratio:
        failures.append(f"left center ratio {left_ratio:.3f} > {left_center_max_ratio:.3f}")
    if right_ratio < right_center_min_ratio:
        failures.append(f"right center ratio {right_ratio:.3f} < {right_center_min_ratio:.3f}")

    detail = (
        f"ASSERT_BUTTON_PAIR_SPREAD leftText={left_text} rightText={right_text} anchorType={anchor_type} "
        f"gapPx={gap} gapRatio={gap_ratio:.3f} minGapRatio={min_gap_ratio:.3f} "
        f"leftCenterRatio={left_ratio:.3f} leftCenterMaxRatio={left_center_max_ratio:.3f} "
        f"rightCenterRatio={right_ratio:.3f} rightCenterMinRatio={right_center_min_ratio:.3f} "
        f"leftBounds={left_bounds} rightBounds={right_bounds} anchorBounds={anchor_bounds}"
    )
    if failures:
        raise RuntimeError(f"Button pair is not spread across the runtime anchor: {'; '.join(failures)}. {detail}")
    return {
        "gapPx": float(gap),
        "gapRatio": gap_ratio,
        "leftCenterRatio": left_ratio,
        "rightCenterRatio": right_ratio,
    }, detail


def _assert_attr_bounds_inside_viewport(
    layout: dict[str, Any] | None,
    *,
    element_id: str,
    min_width_px: int,
    min_height_px: int,
    min_left_margin_px: int,
    min_right_margin_px: int,
    min_top_margin_px: int,
    min_bottom_margin_px: int,
) -> tuple[tuple[int, int, int, int], str]:
    viewport_bounds = _layout_viewport_bounds(layout)
    if not viewport_bounds:
        raise RuntimeError("No viewport bounds were available in the dumped layout.")
    bounds = _find_attr_bounds(layout, element_id)
    if bounds is None:
        raise RuntimeError(f"Unable to find visible id/key/text {element_id!r} in the dumped layout.")

    left, top, right, bottom = bounds
    view_left, view_top, view_right, view_bottom = viewport_bounds
    width = right - left
    height = bottom - top
    left_gap = left - view_left
    right_gap = view_right - right
    top_gap = top - view_top
    bottom_gap = view_bottom - bottom
    failures: list[str] = []
    if width < min_width_px:
        failures.append(f"width {width}px < {min_width_px}px")
    if height < min_height_px:
        failures.append(f"height {height}px < {min_height_px}px")
    if left_gap < min_left_margin_px:
        failures.append(f"left margin {left_gap}px < {min_left_margin_px}px")
    if right_gap < min_right_margin_px:
        failures.append(f"right margin {right_gap}px < {min_right_margin_px}px")
    if top_gap < min_top_margin_px:
        failures.append(f"top margin {top_gap}px < {min_top_margin_px}px")
    if bottom_gap < min_bottom_margin_px:
        failures.append(f"bottom margin {bottom_gap}px < {min_bottom_margin_px}px")

    detail = (
        f"ASSERT_ID_INSIDE_VIEWPORT id={element_id} bounds={bounds} viewportBounds={viewport_bounds} "
        f"widthPx={width} heightPx={height} "
        f"leftMarginPx={left_gap} rightMarginPx={right_gap} topMarginPx={top_gap} bottomMarginPx={bottom_gap} "
        f"minWidthPx={min_width_px} minHeightPx={min_height_px} "
        f"minLeftMarginPx={min_left_margin_px} minRightMarginPx={min_right_margin_px} "
        f"minTopMarginPx={min_top_margin_px} minBottomMarginPx={min_bottom_margin_px}"
    )
    if failures:
        raise RuntimeError(f"Component is clipped or outside the viewport: {'; '.join(failures)}. {detail}")
    return bounds, detail


def _assert_attr_horizontal_margins(
    layout: dict[str, Any] | None,
    *,
    element_id: str,
    min_left_margin_px: int,
    min_right_margin_px: int,
    min_left_margin_ratio: float,
    min_right_margin_ratio: float,
) -> tuple[dict[str, float], str]:
    viewport_bounds = _layout_viewport_bounds(layout)
    if not viewport_bounds:
        raise RuntimeError("No viewport bounds were available in the dumped layout.")
    bounds = _find_attr_bounds(layout, element_id)
    if bounds is None:
        raise RuntimeError(f"Unable to find visible id/key/text {element_id!r} in the dumped layout.")

    left, _top, right, _bottom = bounds
    view_left, _view_top, view_right, _view_bottom = viewport_bounds
    viewport_width = max(view_right - view_left, 1)
    left_gap = left - view_left
    right_gap = view_right - right
    left_ratio = left_gap / viewport_width
    right_ratio = right_gap / viewport_width
    failures: list[str] = []
    if left_gap < min_left_margin_px:
        failures.append(f"left margin {left_gap}px < {min_left_margin_px}px")
    if right_gap < min_right_margin_px:
        failures.append(f"right margin {right_gap}px < {min_right_margin_px}px")
    if left_ratio < min_left_margin_ratio:
        failures.append(f"left margin ratio {left_ratio:.3f} < {min_left_margin_ratio:.3f}")
    if right_ratio < min_right_margin_ratio:
        failures.append(f"right margin ratio {right_ratio:.3f} < {min_right_margin_ratio:.3f}")

    detail = (
        f"ASSERT_ID_HORIZONTAL_MARGINS id={element_id} bounds={bounds} viewportBounds={viewport_bounds} "
        f"leftMarginPx={left_gap} rightMarginPx={right_gap} "
        f"leftMarginRatio={left_ratio:.3f} rightMarginRatio={right_ratio:.3f} "
        f"minLeftMarginPx={min_left_margin_px} minRightMarginPx={min_right_margin_px} "
        f"minLeftMarginRatio={min_left_margin_ratio:.3f} minRightMarginRatio={min_right_margin_ratio:.3f}"
    )
    if failures:
        raise RuntimeError(f"Component is too wide or horizontally clipped: {'; '.join(failures)}. {detail}")
    return {
        "leftMarginPx": float(left_gap),
        "rightMarginPx": float(right_gap),
        "leftMarginRatio": left_ratio,
        "rightMarginRatio": right_ratio,
    }, detail


def _assert_attr_ancestor_horizontal_margins(
    layout: dict[str, Any] | None,
    *,
    element_id: str,
    ancestor_type: str,
    max_left_margin_px: int | None,
    max_right_margin_px: int | None,
    max_left_margin_ratio: float | None,
    max_right_margin_ratio: float | None,
) -> tuple[dict[str, float], str]:
    viewport_bounds = _layout_viewport_bounds(layout)
    if not viewport_bounds:
        raise RuntimeError("No viewport bounds were available in the dumped layout.")
    nodes_by_hierarchy = _layout_component_index(layout)
    target_node = _find_node_by_attr(layout, element_id)
    if target_node is None:
        raise RuntimeError(f"Unable to find visible id/key/text {element_id!r} in the dumped layout.")

    ancestor_node: dict[str, Any] | None = None
    for hierarchy in _ancestor_hierarchies(_node_attr(target_node, "hierarchy"))[1:]:
        candidate = nodes_by_hierarchy.get(hierarchy)
        if candidate and _node_attr(candidate, "type") == ancestor_type:
            ancestor_node = candidate
            break
    if ancestor_node is None:
        raise RuntimeError(f"Unable to find ancestor type {ancestor_type!r} for id/key/text {element_id!r}.")

    bounds = _parse_bounds_rect(_node_attr(ancestor_node, "bounds"))
    if bounds is None:
        raise RuntimeError(f"Ancestor type {ancestor_type!r} for {element_id!r} has no bounds.")

    left, _top, right, _bottom = bounds
    view_left, _view_top, view_right, _view_bottom = viewport_bounds
    viewport_width = max(view_right - view_left, 1)
    left_gap = left - view_left
    right_gap = view_right - right
    left_ratio = left_gap / viewport_width
    right_ratio = right_gap / viewport_width
    failures: list[str] = []
    if max_left_margin_px is not None and left_gap > max_left_margin_px:
        failures.append(f"left margin {left_gap}px > {max_left_margin_px}px")
    if max_right_margin_px is not None and right_gap > max_right_margin_px:
        failures.append(f"right margin {right_gap}px > {max_right_margin_px}px")
    if max_left_margin_ratio is not None and left_ratio > max_left_margin_ratio:
        failures.append(f"left margin ratio {left_ratio:.3f} > {max_left_margin_ratio:.3f}")
    if max_right_margin_ratio is not None and right_ratio > max_right_margin_ratio:
        failures.append(f"right margin ratio {right_ratio:.3f} > {max_right_margin_ratio:.3f}")

    detail = (
        f"ASSERT_ID_ANCESTOR_HORIZONTAL_MARGINS id={element_id} ancestorType={ancestor_type} "
        f"bounds={bounds} viewportBounds={viewport_bounds} "
        f"leftMarginPx={left_gap} rightMarginPx={right_gap} "
        f"leftMarginRatio={left_ratio:.3f} rightMarginRatio={right_ratio:.3f} "
        f"maxLeftMarginPx={max_left_margin_px if max_left_margin_px is not None else '<none>'} "
        f"maxRightMarginPx={max_right_margin_px if max_right_margin_px is not None else '<none>'} "
        f"maxLeftMarginRatio={max_left_margin_ratio:.3f}" if max_left_margin_ratio is not None else
        f"ASSERT_ID_ANCESTOR_HORIZONTAL_MARGINS id={element_id} ancestorType={ancestor_type} "
        f"bounds={bounds} viewportBounds={viewport_bounds} "
        f"leftMarginPx={left_gap} rightMarginPx={right_gap} "
        f"leftMarginRatio={left_ratio:.3f} rightMarginRatio={right_ratio:.3f} "
        f"maxLeftMarginPx={max_left_margin_px if max_left_margin_px is not None else '<none>'} "
        f"maxRightMarginPx={max_right_margin_px if max_right_margin_px is not None else '<none>'} "
        f"maxLeftMarginRatio=<none>"
    )
    detail += (
        f" maxRightMarginRatio={max_right_margin_ratio:.3f}" if max_right_margin_ratio is not None else
        " maxRightMarginRatio=<none>"
    )
    if failures:
        raise RuntimeError(f"Ancestor component is not horizontally expanded: {'; '.join(failures)}. {detail}")
    return {
        "leftMarginPx": float(left_gap),
        "rightMarginPx": float(right_gap),
        "leftMarginRatio": left_ratio,
        "rightMarginRatio": right_ratio,
    }, detail


def _assert_slider_left_aligned_with_text(
    layout: dict[str, Any] | None,
    *,
    text: str,
    slider_type: str,
    max_left_delta_px: int,
) -> tuple[int, str]:
    nodes_by_hierarchy = _layout_component_index(layout)
    if not nodes_by_hierarchy:
        raise RuntimeError("No component nodes were available in the dumped layout.")

    text_bounds: tuple[int, int, int, int] | None = None
    for node in nodes_by_hierarchy.values():
        node_text = _node_attr(node, "text") or _node_attr(node, "description")
        if node_text != text:
            continue
        candidate = _parse_bounds_rect(_node_attr(node, "bounds"))
        if candidate:
            text_bounds = candidate
            break
    if text_bounds is None:
        raise RuntimeError(f"Unable to find text {text!r} in the dumped layout.")

    slider_candidates: list[tuple[int, int, tuple[int, int, int, int]]] = []
    for node in nodes_by_hierarchy.values():
        if _node_attr(node, "type") != slider_type:
            continue
        slider_bounds = _parse_bounds_rect(_node_attr(node, "bounds"))
        if not slider_bounds:
            continue
        vertical_distance = abs(((slider_bounds[1] + slider_bounds[3]) // 2) - ((text_bounds[1] + text_bounds[3]) // 2))
        left_delta = abs(slider_bounds[0] - text_bounds[0])
        slider_candidates.append((vertical_distance, left_delta, slider_bounds))

    if not slider_candidates:
        raise RuntimeError(f"Unable to find a visible {slider_type!r} component in the dumped layout.")

    _vertical_distance, left_delta, slider_bounds = sorted(slider_candidates, key=lambda item: (item[0], item[1]))[0]
    if left_delta > max_left_delta_px:
        raise RuntimeError(
            f"{slider_type!r} left edge is not aligned with text {text!r}: "
            f"delta {left_delta}px > {max_left_delta_px}px; text_bounds={text_bounds}; slider_bounds={slider_bounds}"
        )
    detail = (
        f"ASSERT_SLIDER_LEFT_ALIGNED_WITH_TEXT text={text} sliderType={slider_type} "
        f"leftDeltaPx={left_delta} maxLeftDeltaPx={max_left_delta_px} "
        f"textBounds={text_bounds} sliderBounds={slider_bounds}"
    )
    return left_delta, detail


def _assert_component_numeric_text_range(
    layout: dict[str, Any] | None,
    *,
    component_type: str,
    min_value: float,
    max_value: float,
) -> tuple[float, str]:
    if not layout:
        raise RuntimeError("No layout was available for numeric component assertion.")

    candidates: list[tuple[float, tuple[int, int, int, int] | None]] = []
    for node in _walk_json_nodes(layout):
        if _node_attr(node, "type") != component_type:
            continue
        raw_text = _node_attr(node, "text")
        if not raw_text:
            continue
        try:
            value = float(raw_text)
        except ValueError:
            continue
        candidates.append((value, _parse_bounds_rect(_node_attr(node, "bounds"))))

    if not candidates:
        raise RuntimeError(f"Unable to find {component_type!r} with numeric text in the dumped layout.")

    value, bounds = candidates[0]
    if value < min_value or value > max_value:
        raise RuntimeError(
            f"{component_type!r} numeric value is out of range: "
            f"{value} not in [{min_value}, {max_value}]; bounds={bounds}"
        )
    detail = (
        f"ASSERT_COMPONENT_NUMERIC_TEXT_RANGE type={component_type} "
        f"value={value:.6f} min={min_value:.6f} max={max_value:.6f} bounds={bounds}"
    )
    return value, detail


def _assert_component_count(
    layout: dict[str, Any] | None,
    *,
    component_type: str,
    expected_count: int,
) -> tuple[int, str]:
    if not layout:
        raise RuntimeError("No layout was available for component count assertion.")

    matches = [
        node
        for node in _walk_layout_component_nodes(layout)
        if _node_attr(node, "type") == component_type
    ]
    actual_count = len(matches)
    if actual_count != expected_count:
        raise RuntimeError(
            f"{component_type!r} count mismatch: {actual_count} != {expected_count}"
        )
    return actual_count, f"ASSERT_COMPONENT_COUNT type={component_type} count={actual_count}"


def _device_hour_candidates(device_target: str) -> set[int]:
    output = _hdc_shell(device_target, "date", "+%H", timeout_sec=10.0).strip()
    match = re.search(r"\b([01]?\d|2[0-3])\b", output)
    if not match:
        raise RuntimeError(f"Unable to read device hour from date output: {output!r}")
    current = int(match.group(1))
    return {current, (current - 1) % 24}


def _assert_flipclock_hour_matches_device_time(
    device_target: str,
    layout: dict[str, Any] | None,
) -> tuple[int, str]:
    digit_columns: dict[int, dict[str, Any]] = {}
    for node in _walk_json_nodes(layout):
        if _node_attr(node, "type") != "Text":
            continue
        text = _node_attr(node, "text")
        if not re.fullmatch(r"\d", text):
            continue
        bounds = _parse_bounds_rect(_node_attr(node, "bounds"))
        if not bounds:
            continue
        left, top, right, bottom = bounds
        width = right - left
        height = bottom - top
        if width < 80 or height < 150:
            continue
        if top < 400 or bottom > 2300:
            continue
        center_x = (left + right) // 2
        key = round(center_x / 25) * 25
        current = digit_columns.get(key)
        if current is None or height > int(current["height"]):
            digit_columns[key] = {
                "text": text,
                "bounds": bounds,
                "height": height,
                "center_x": center_x,
            }
    ordered = sorted(digit_columns.values(), key=lambda item: int(item["center_x"]))
    if len(ordered) < 2:
        raise RuntimeError(f"Unable to find two visible FlipClock hour digit columns; candidates={ordered!r}")
    hour_digits = ordered[:2]
    displayed = int(str(hour_digits[0]["text"]) + str(hour_digits[1]["text"]))
    expected_hours = _device_hour_candidates(device_target)
    if displayed not in expected_hours:
        raise RuntimeError(
            f"FlipClock displayed hour {displayed:02d}, expected device hour in "
            f"{sorted(expected_hours)}; digit_columns={hour_digits!r}"
        )
    detail = (
        f"ASSERT_FLIPCLOCK_HOUR_MATCHES_DEVICE_TIME displayedHour={displayed:02d} "
        f"expectedHours={sorted(expected_hours)} digitColumns={hour_digits!r}"
    )
    return displayed, detail


def _assert_stepper_numeric_text_range(
    layout: dict[str, Any] | None,
    *,
    min_value: float,
    max_value: float,
) -> tuple[float, str]:
    nodes_by_hierarchy = _layout_component_index(layout)
    if not nodes_by_hierarchy:
        raise RuntimeError("No component nodes were available in the dumped layout.")

    button_bounds: list[tuple[int, int, int, int]] = []
    for node in nodes_by_hierarchy.values():
        if _node_attr(node, "type") != "Button":
            continue
        bounds = _parse_bounds_rect(_node_attr(node, "bounds"))
        if bounds:
            button_bounds.append(bounds)

    candidates: list[tuple[float, tuple[int, int, int, int], tuple[int, int, int, int], tuple[int, int, int, int]]] = []
    for node in nodes_by_hierarchy.values():
        if _node_attr(node, "type") != "Text":
            continue
        raw_text = _node_attr(node, "text")
        if not raw_text:
            continue
        try:
            value = float(raw_text)
        except ValueError:
            continue
        text_bounds = _parse_bounds_rect(_node_attr(node, "bounds"))
        if not text_bounds:
            continue
        text_mid_y = (text_bounds[1] + text_bounds[3]) // 2
        left_buttons = []
        right_buttons = []
        for bounds in button_bounds:
            button_mid_y = (bounds[1] + bounds[3]) // 2
            if abs(button_mid_y - text_mid_y) > max(bounds[3] - bounds[1], text_bounds[3] - text_bounds[1], 1):
                continue
            if bounds[2] <= text_bounds[0]:
                left_buttons.append(bounds)
            elif bounds[0] >= text_bounds[2]:
                right_buttons.append(bounds)
        if left_buttons and right_buttons:
            left_button = sorted(left_buttons, key=lambda item: item[2], reverse=True)[0]
            right_button = sorted(right_buttons, key=lambda item: item[0])[0]
            candidates.append((value, text_bounds, left_button, right_button))

    if not candidates:
        raise RuntimeError("Unable to find a numeric stepper text between decrement and increment buttons.")

    value, text_bounds, left_button, right_button = sorted(candidates, key=lambda item: item[1][1], reverse=True)[0]
    if value < min_value or value > max_value:
        raise RuntimeError(
            f"Stepper numeric value is out of range: {value} not in [{min_value}, {max_value}]; "
            f"text_bounds={text_bounds}; left_button={left_button}; right_button={right_button}"
        )
    detail = (
        f"ASSERT_STEPPER_NUMERIC_TEXT_RANGE value={value:.6f} "
        f"min={min_value:.6f} max={max_value:.6f} "
        f"textBounds={text_bounds} leftButton={left_button} rightButton={right_button}"
    )
    return value, detail


def _assert_button_row_inside_id(
    layout: dict[str, Any] | None,
    *,
    anchor_id: str,
    expected_count: int,
    min_width_px: int,
    min_height_px: int,
    horizontal_slop_px: int = 0,
    use_orig_bounds: bool = False,
) -> tuple[list[tuple[int, int, int, int]], str]:
    anchor_bounds = _find_attr_bounds(layout, anchor_id)
    if anchor_bounds is None:
        raise RuntimeError(f"Unable to find visible id/key/text {anchor_id!r} in the dumped layout.")

    anchor_left, anchor_top, anchor_right, anchor_bottom = anchor_bounds
    anchor_mid_y = (anchor_top + anchor_bottom) // 2
    candidates: list[tuple[int, tuple[int, int, int, int]]] = []
    seen_bounds: set[tuple[int, int, int, int]] = set()
    for node in _walk_json_nodes(layout):
        if _node_attr(node, "type") != "Button":
            continue
        bounds_attr = "origBounds" if use_orig_bounds else "bounds"
        bounds = _parse_bounds_rect(_node_attr(node, bounds_attr))
        if not bounds:
            continue
        if bounds in seen_bounds:
            continue
        seen_bounds.add(bounds)
        left, top, right, bottom = bounds
        if bottom <= anchor_top or top >= anchor_bottom:
            continue
        mid_y = (top + bottom) // 2
        if abs(mid_y - anchor_mid_y) > max(anchor_bottom - anchor_top, 1):
            continue
        candidates.append((left, bounds))

    ordered = [bounds for _, bounds in sorted(candidates, key=lambda item: item[0])]
    if len(ordered) < expected_count:
        raise RuntimeError(
            f"Expected at least {expected_count} visible Button nodes near {anchor_id!r}, "
            f"found {len(ordered)}; anchor_bounds={anchor_bounds}; button_bounds={ordered}"
        )
    selected = ordered[:expected_count]
    for bounds in selected:
        left, top, right, bottom = bounds
        width = right - left
        height = bottom - top
        if width < min_width_px or height < min_height_px:
            raise RuntimeError(
                f"Button row member is too small: bounds={bounds}; "
                f"width={width}px height={height}px min={min_width_px}x{min_height_px}; "
                f"anchor_bounds={anchor_bounds}; button_bounds={selected}"
            )
    leftmost = selected[0]
    rightmost = selected[-1]
    if leftmost[0] < anchor_left - horizontal_slop_px:
        raise RuntimeError(
            f"Left button is clipped outside {anchor_id!r}: left={leftmost[0]} "
            f"< anchorLeft={anchor_left} - slop={horizontal_slop_px}; "
            f"anchor_bounds={anchor_bounds}; button_bounds={selected}"
        )
    if rightmost[2] > anchor_right + horizontal_slop_px:
        raise RuntimeError(
            f"Right button is clipped outside {anchor_id!r}: right={rightmost[2]} "
            f"> anchorRight={anchor_right} + slop={horizontal_slop_px}; "
            f"anchor_bounds={anchor_bounds}; button_bounds={selected}"
        )

    detail = (
        f"ASSERT_BUTTON_ROW_INSIDE_ID id={anchor_id} expectedCount={expected_count} "
        f"anchorBounds={anchor_bounds} buttonBounds={selected} "
        f"minSize={min_width_px}x{min_height_px} horizontalSlopPx={horizontal_slop_px} "
        f"useOrigBounds={str(use_orig_bounds).lower()}"
    )
    return selected, detail
def _assert_visible_button_near_id(
    layout: dict[str, Any] | None,
    *,
    anchor_id: str,
    min_width_px: int,
    min_height_px: int,
    max_right_px: int | None = None,
    click_index: int = 0,
) -> tuple[tuple[int, int, int, int], str]:
    anchor_bounds = _find_attr_bounds(layout, anchor_id)
    if anchor_bounds is None:
        raise RuntimeError(f"Unable to find visible id/key/text {anchor_id!r} in the dumped layout.")

    anchor_left, anchor_top, anchor_right, anchor_bottom = anchor_bounds
    anchor_mid_y = (anchor_top + anchor_bottom) // 2
    candidates: list[tuple[int, tuple[int, int, int, int]]] = []
    for node in _walk_json_nodes(layout):
        if _node_attr(node, "type") != "Button":
            continue
        bounds = _parse_bounds_rect(_node_attr(node, "bounds"))
        if not bounds:
            continue
        left, top, right, bottom = bounds
        if bottom <= anchor_top or top >= anchor_bottom:
            continue
        mid_y = (top + bottom) // 2
        if abs(mid_y - anchor_mid_y) > max(anchor_bottom - anchor_top, 1):
            continue
        if right < anchor_left or left > anchor_right + 200:
            continue
        candidates.append((left, bounds))

    if not candidates:
        raise RuntimeError(f"Unable to find a visible Button near {anchor_id!r}; anchor_bounds={anchor_bounds}")

    ordered = [bounds for _, bounds in sorted(candidates, key=lambda item: item[0])]
    if click_index < 0 or click_index >= len(ordered):
        raise RuntimeError(
            f"Button index {click_index} is out of range near {anchor_id!r}; "
            f"candidate_count={len(ordered)} anchor_bounds={anchor_bounds}"
        )

    bounds = ordered[click_index]
    left, top, right, bottom = bounds
    width = right - left
    height = bottom - top
    if width < min_width_px:
        raise RuntimeError(
            f"Button near {anchor_id!r} is too narrow: "
            f"{width}px < {min_width_px}px; button_bounds={bounds}; anchor_bounds={anchor_bounds}"
        )
    if height < min_height_px:
        raise RuntimeError(
            f"Button near {anchor_id!r} is too short: "
            f"{height}px < {min_height_px}px; button_bounds={bounds}; anchor_bounds={anchor_bounds}"
        )
    if max_right_px is not None and right > max_right_px:
        raise RuntimeError(
            f"Button near {anchor_id!r} extends beyond allowed right edge: "
            f"{right}px > {max_right_px}px; button_bounds={bounds}; anchor_bounds={anchor_bounds}"
        )

    detail = (
        f"ASSERT_VISIBLE_BUTTON_NEAR_ID id={anchor_id} index={click_index} "
        f"buttonBounds={bounds} anchorBounds={anchor_bounds} "
        f"widthPx={width} minWidthPx={min_width_px} "
        f"heightPx={height} minHeightPx={min_height_px} "
        f"maxRightPx={max_right_px if max_right_px is not None else '<none>'}"
    )
    return bounds, detail


def _click_text(device_target: str, repo_dir: Path, scenario_name: str, text: str, step_index: int) -> str:
    layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"before_click_{step_index}")
    center = _find_text_center(layout, text)
    if center is None:
        raise RuntimeError(f"Unable to find visible text {text!r} before click. {info}")
    _hdc_shell(device_target, "uitest", "uiInput", "click", str(center[0]), str(center[1]), timeout_sec=10.0)
    return f"CLICK_TEXT={text} CENTER={center[0]},{center[1]}\n{info}"


def _click_id(device_target: str, repo_dir: Path, scenario_name: str, element_id: str, step_index: int) -> str:
    layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"before_click_id_{step_index}")
    bounds = _find_attr_bounds(layout, element_id)
    if bounds is None:
        raise RuntimeError(f"Unable to find visible id/key/text {element_id!r} before click. {info}")
    left, top, right, bottom = bounds
    center = ((left + right) // 2, (top + bottom) // 2)
    _hdc_shell(device_target, "uitest", "uiInput", "click", str(center[0]), str(center[1]), timeout_sec=10.0)
    return f"CLICK_ID={element_id} CENTER={center[0]},{center[1]}\n{info}"


def _click_relative_to_id(
    device_target: str,
    repo_dir: Path,
    scenario_name: str,
    element_id: str,
    step_index: int,
    *,
    x_offset: int = 0,
    y_offset: int = 0,
    x_ratio: float = 0.5,
    y_ratio: float = 0.5,
) -> str:
    layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"before_click_relative_{step_index}")
    bounds = _find_attr_bounds(layout, element_id)
    if bounds is None:
        raise RuntimeError(f"Unable to find visible id/key/text {element_id!r} before relative click. {info}")
    left, top, right, bottom = bounds
    width = max(right - left, 1)
    height = max(bottom - top, 1)
    x = left + int(width * x_ratio) + x_offset
    y = top + int(height * y_ratio) + y_offset
    _hdc_shell(device_target, "uitest", "uiInput", "click", str(x), str(y), timeout_sec=10.0)
    return f"CLICK_RELATIVE_ID={element_id} CENTER={x},{y}\n{info}"


def _click_relative_to_text(
    device_target: str,
    repo_dir: Path,
    scenario_name: str,
    text: str,
    step_index: int,
    *,
    x_offset: int = 0,
    y_offset: int = 0,
    x_ratio: float = 0.5,
    y_ratio: float = 0.5,
) -> str:
    layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"before_click_relative_text_{step_index}")
    bounds = _find_attr_bounds(layout, text)
    if bounds is None:
        raise RuntimeError(f"Unable to find visible text {text!r} before relative click. {info}")
    left, top, right, bottom = bounds
    width = max(right - left, 1)
    height = max(bottom - top, 1)
    x = left + int(width * x_ratio) + x_offset
    y = top + int(height * y_ratio) + y_offset
    _hdc_shell(device_target, "uitest", "uiInput", "click", str(x), str(y), timeout_sec=10.0)
    return f"CLICK_RELATIVE_TEXT={text} CENTER={x},{y}\n{info}"


def _long_click_id(device_target: str, repo_dir: Path, scenario_name: str, element_id: str, step_index: int) -> str:
    layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"before_long_click_id_{step_index}")
    bounds = _find_attr_bounds(layout, element_id)
    if bounds is None:
        raise RuntimeError(f"Unable to find visible id/key/text {element_id!r} before long click. {info}")
    left, top, right, bottom = bounds
    center = ((left + right) // 2, (top + bottom) // 2)
    _hdc_shell(device_target, "uitest", "uiInput", "longClick", str(center[0]), str(center[1]), timeout_sec=10.0)
    return f"LONG_CLICK_ID={element_id} CENTER={center[0]},{center[1]}\n{info}"


def _long_click_text(device_target: str, repo_dir: Path, scenario_name: str, text: str, step_index: int) -> str:
    layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"before_long_click_text_{step_index}")
    bounds = _find_attr_bounds(layout, text)
    if bounds is None:
        raise RuntimeError(f"Unable to find visible text {text!r} before long click. {info}")
    left, top, right, bottom = bounds
    center = ((left + right) // 2, (top + bottom) // 2)
    _hdc_shell(device_target, "uitest", "uiInput", "longClick", str(center[0]), str(center[1]), timeout_sec=10.0)
    return f"LONG_CLICK_TEXT={text} CENTER={center[0]},{center[1]}\n{info}"


def _long_click_relative_to_text(
    device_target: str,
    repo_dir: Path,
    scenario_name: str,
    text: str,
    step_index: int,
    *,
    x_offset: int = 0,
    y_offset: int = 0,
    x_ratio: float = 0.5,
    y_ratio: float = 0.5,
) -> str:
    layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"before_long_click_relative_text_{step_index}")
    bounds = _find_attr_bounds(layout, text)
    if bounds is None:
        raise RuntimeError(f"Unable to find visible text {text!r} before relative long click. {info}")
    left, top, right, bottom = bounds
    width = max(right - left, 1)
    height = max(bottom - top, 1)
    x = left + int(width * x_ratio) + x_offset
    y = top + int(height * y_ratio) + y_offset
    _hdc_shell(device_target, "uitest", "uiInput", "longClick", str(x), str(y), timeout_sec=10.0)
    return f"LONG_CLICK_RELATIVE_TEXT={text} CENTER={x},{y}\n{info}"


def _input_text(
    device_target: str,
    repo_dir: Path,
    scenario_name: str,
    element_id: str,
    text: str,
    step_index: int,
    *,
    x_ratio: float = 0.5,
    y_ratio: float = 0.5,
) -> str:
    layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"before_input_text_{step_index}")
    bounds = _find_attr_bounds(layout, element_id)
    if bounds is None:
        raise RuntimeError(f"Unable to find visible id/key/text {element_id!r} before text input. {info}")
    left, top, right, bottom = bounds
    width = max(right - left, 1)
    height = max(bottom - top, 1)
    x = left + int(width * x_ratio)
    y = top + int(height * y_ratio)
    _hdc_shell(device_target, "uitest", "uiInput", "inputText", str(x), str(y), text, timeout_sec=10.0)
    return f"INPUT_TEXT={element_id} VALUE={text} CENTER={x},{y}\n{info}"


def _input_text_at_point(device_target: str, x: int, y: int, text: str) -> str:
    _hdc_shell(device_target, "uitest", "uiInput", "inputText", str(x), str(y), text, timeout_sec=10.0)
    return f"INPUT_TEXT_AT_POINT VALUE={text} CENTER={x},{y}"


def _swipe_id(
    device_target: str,
    repo_dir: Path,
    scenario_name: str,
    element_id: str,
    step_index: int,
    *,
    start_x_ratio: float = 0.05,
    end_x_ratio: float = 0.95,
    y_ratio: float = 0.5,
    duration_ms: int = 800,
) -> str:
    layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"before_swipe_id_{step_index}")
    bounds = _find_attr_bounds(layout, element_id)
    if bounds is None:
        raise RuntimeError(f"Unable to find visible id/key/text {element_id!r} before swipe. {info}")
    left, top, right, bottom = bounds
    width = max(right - left, 1)
    height = max(bottom - top, 1)
    start_x = left + int(width * start_x_ratio)
    end_x = left + int(width * end_x_ratio)
    y = top + int(height * y_ratio)
    _hdc_shell(
        device_target,
        "uitest",
        "uiInput",
        "swipe",
        str(start_x),
        str(y),
        str(end_x),
        str(y),
        str(duration_ms),
        timeout_sec=10.0,
    )
    return f"SWIPE_ID={element_id} FROM={start_x},{y} TO={end_x},{y} DURATION_MS={duration_ms}\n{info}"


def _run_host_ui_action(
    device_target: str,
    repo_dir: Path,
    scenario_name: str,
    action: dict[str, Any],
    step_index: int,
) -> str:
    action_type = str(action.get("type") or action.get("action") or "")
    if action_type == "clickText":
        return _click_text(device_target, repo_dir, scenario_name, str(action["text"]), step_index)
    if action_type == "clickPoint":
        x = int(action["x"])
        y = int(action["y"])
        _hdc_shell(device_target, "uitest", "uiInput", "click", str(x), str(y), timeout_sec=10.0)
        return f"CLICK_POINT={x},{y}"
    if action_type == "swipePoint":
        start_x = int(action["startX"])
        start_y = int(action["startY"])
        end_x = int(action["endX"])
        end_y = int(action["endY"])
        duration_ms = int(action.get("durationMs", 800))
        _hdc_shell(
            device_target,
            "uitest",
            "uiInput",
            "swipe",
            str(start_x),
            str(start_y),
            str(end_x),
            str(end_y),
            str(duration_ms),
            timeout_sec=10.0,
        )
        return f"SWIPE_POINT={start_x},{start_y}->{end_x},{end_y} DURATION_MS={duration_ms}"
    if action_type == "clickId":
        return _click_id(device_target, repo_dir, scenario_name, str(action["id"]), step_index)
    if action_type == "clickRelativeToId":
        return _click_relative_to_id(
            device_target,
            repo_dir,
            scenario_name,
            str(action["id"]),
            step_index,
            x_offset=int(action.get("xOffset", 0)),
            y_offset=int(action.get("yOffset", 0)),
            x_ratio=float(action.get("xRatio", 0.5)),
            y_ratio=float(action.get("yRatio", 0.5)),
        )
    if action_type == "clickRelativeToText":
        return _click_relative_to_text(
            device_target,
            repo_dir,
            scenario_name,
            str(action["text"]),
            step_index,
            x_offset=int(action.get("xOffset", 0)),
            y_offset=int(action.get("yOffset", 0)),
            x_ratio=float(action.get("xRatio", 0.5)),
            y_ratio=float(action.get("yRatio", 0.5)),
        )
    if action_type == "longClickId":
        return _long_click_id(device_target, repo_dir, scenario_name, str(action["id"]), step_index)
    if action_type == "longClickText":
        return _long_click_text(device_target, repo_dir, scenario_name, str(action["text"]), step_index)
    if action_type == "longClickRelativeToText":
        return _long_click_relative_to_text(
            device_target,
            repo_dir,
            scenario_name,
            str(action["text"]),
            step_index,
            x_offset=int(action.get("xOffset", 0)),
            y_offset=int(action.get("yOffset", 0)),
            x_ratio=float(action.get("xRatio", 0.5)),
            y_ratio=float(action.get("yRatio", 0.5)),
        )
    if action_type == "inputText":
        return _input_text(
            device_target,
            repo_dir,
            scenario_name,
            str(action["id"]),
            str(action["text"]),
            step_index,
            x_ratio=float(action.get("xRatio", 0.5)),
            y_ratio=float(action.get("yRatio", 0.5)),
        )
    if action_type == "inputTextAtPoint":
        return _input_text_at_point(
            device_target,
            int(action["x"]),
            int(action["y"]),
            str(action["text"]),
        )
    if action_type == "swipeId":
        return _swipe_id(
            device_target,
            repo_dir,
            scenario_name,
            str(action["id"]),
            step_index,
            start_x_ratio=float(action.get("startXRatio", 0.05)),
            end_x_ratio=float(action.get("endXRatio", 0.95)),
            y_ratio=float(action.get("yRatio", 0.5)),
            duration_ms=int(action.get("durationMs", 800)),
        )
    if action_type == "wait":
        delay_sec = float(action.get("seconds", 1.0))
        time.sleep(delay_sec)
        return f"WAIT_SECONDS={delay_sec}"
    if action_type == "startAbility":
        command = [
            "aa",
            "start",
            "-d",
            str(action.get("displayId", 0)),
            "-b",
            str(action["bundle"]),
            "-a",
            str(action["ability"]),
        ]
        module = str(action.get("module") or "")
        if module:
            command.extend(["-m", module])
        params = action.get("params") or {}
        if isinstance(params, dict):
            _append_aa_start_params(command, params)
        output = _hdc_shell(device_target, *command, timeout_sec=15.0)
        delay_sec = float(action.get("delayAfterSec", 1.0))
        if delay_sec > 0:
            time.sleep(delay_sec)
        return "START_ABILITY_COMMAND=" + " ".join(command) + "\nSTART_ABILITY_OUTPUT=" + output.strip()
    if action_type == "ensureKikaInputCurrent":
        return _ensure_kika_input_current(device_target)
    if action_type == "recordWindowBounds":
        pattern = str(action["pattern"])
        output = _window_manager_dump(device_target)
        line, bounds = _parse_window_bounds(output, pattern)
        left, top, width, height = bounds
        label = str(action.get("label") or pattern)
        return "\n".join(
            [
                f"RECORD_WINDOW_BOUNDS={label}",
                f"WINDOW_MATCH={line}",
                f"WINDOW_LEFT={left}",
                f"WINDOW_TOP={top}",
                f"WINDOW_WIDTH={width}",
                f"WINDOW_HEIGHT={height}",
            ]
        )
    if action_type == "assertWindowHeight":
        pattern = str(action["pattern"])
        min_height_raw = action.get("minHeight")
        max_height_raw = action.get("maxHeight")
        output = _window_manager_dump(device_target)
        line, bounds = _parse_window_bounds(output, pattern)
        height = bounds[3]
        min_height = int(min_height_raw) if min_height_raw is not None else None
        max_height = int(max_height_raw) if max_height_raw is not None else None
        if min_height is not None and height < min_height:
            raise RuntimeError(
                f"Window {pattern!r} height {height} is below minimum {min_height}.\n{line}"
            )
        if max_height is not None and height > max_height:
            raise RuntimeError(
                f"Window {pattern!r} height {height} exceeds maximum {max_height}.\n{line}"
            )
        return "\n".join(
            [
                f"ASSERT_WINDOW_HEIGHT={pattern}",
                f"WINDOW_MATCH={line}",
                f"WINDOW_HEIGHT={height}",
                f"WINDOW_MIN_HEIGHT={min_height if min_height is not None else '<none>'}",
                f"WINDOW_MAX_HEIGHT={max_height if max_height is not None else '<none>'}",
            ]
        )
    if action_type == "waitForDisplayOrientation":
        expected = int(action["orientation"])
        timeout = float(action.get("timeoutSec", 8.0))
        interval = float(action.get("intervalSec", 0.5))
        deadline = time.monotonic() + timeout
        last_value: int | None = None
        while True:
            last_value, output = _display_orientation(device_target)
            if last_value == expected:
                return f"WAIT_FOR_DISPLAY_ORIENTATION={expected} ACTUAL={last_value}"
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Timed out waiting for display orientation {expected}; last={last_value}.\n"
                    + _tail_text(output, limit=80)
                )
            time.sleep(interval)
    if action_type == "assertWindowNameAbsent":
        pattern = str(action["pattern"])
        wait_sec = float(action.get("waitSec") or 0.0)
        if wait_sec > 0:
            time.sleep(wait_sec)
        output = _window_manager_dump(device_target)
        if re.search(pattern, output, flags=re.I):
            raise RuntimeError(
                f"Unexpected window name matching {pattern!r}.\n"
                + _tail_text(output, limit=80)
            )
        return f"ASSERT_WINDOW_NAME_ABSENT={pattern}"
    if action_type == "assertWindowNamePresent":
        pattern = str(action["pattern"])
        wait_sec = float(action.get("waitSec") or 0.0)
        if wait_sec > 0:
            time.sleep(wait_sec)
        output = _window_manager_dump(device_target)
        if re.search(pattern, output, flags=re.I) is None:
            raise RuntimeError(
                f"Missing window name matching {pattern!r}.\n"
                + _tail_text(output, limit=80)
            )
        return f"ASSERT_WINDOW_NAME_PRESENT={pattern}"
    if action_type == "assertFocusedWindowNameNotMatching":
        pattern = str(action["pattern"])
        wait_sec = float(action.get("waitSec") or 0.0)
        if wait_sec > 0:
            time.sleep(wait_sec)
        output = _window_manager_dump(device_target)
        focused_name = _focused_window_name(output)
        if focused_name is None:
            raise RuntimeError(
                "Unable to resolve focused window name.\n" + _tail_text(output, limit=80)
            )
        if re.search(pattern, focused_name, flags=re.I):
            raise RuntimeError(
                f"Focused window {focused_name!r} unexpectedly matches {pattern!r}.\n"
                + _tail_text(output, limit=80)
            )
        return f"ASSERT_FOCUSED_WINDOW_NAME_NOT_MATCHING={pattern} FOCUSED_WINDOW={focused_name}"
    if action_type == "waitForTextVisible":
        text = str(action["text"])
        timeout = float(action.get("timeoutSec", 10.0))
        interval = float(action.get("intervalSec", 0.5))
        layout, info = _wait_for_layout_text(
            device_target,
            repo_dir,
            scenario_name,
            text,
            f"wait_text_{step_index}",
            timeout_sec=timeout,
            interval_sec=interval,
        )
        if not _layout_contains_text(layout, text):
            raise RuntimeError(f"Timed out waiting for visible text {text!r}. {info}")
        return f"WAIT_FOR_TEXT_VISIBLE={text}\n{info}"
    if action_type == "keyEvent":
        key = str(action["key"])
        _hdc_shell(device_target, "uitest", "uiInput", "keyEvent", key, timeout_sec=10.0)
        return f"KEY_EVENT={key}"
    if action_type == "dumpLayout":
        _, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"action_dump_{step_index}")
        return info
    if action_type == "captureToastRegion":
        delay_ms = int(action.get("delayMs", 100))
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)
        image_path = _capture_screen(device_target, repo_dir, scenario_name, f"toast_{step_index}")
        region = action.get("region")
        if not isinstance(region, dict):
            region = {"left": 0.18, "top": 0.78, "right": 0.82, "bottom": 0.92}
        ratio = _dark_region_ratio(image_path, region)
        min_ratio = float(action.get("minDarkRatio", 0.015))
        passed = ratio >= min_ratio
        return "\n".join(
            [
                f"CAPTURE_TOAST_REGION={format_path_for_display(image_path, start=repo_dir)}",
                f"CAPTURE_TOAST_DARK_RATIO={ratio:.6f}",
                f"CAPTURE_TOAST_MIN_DARK_RATIO={min_ratio:.6f}",
                f"CAPTURE_TOAST_RESULT={'PASS' if passed else 'FAIL'}",
            ]
        )
    if action_type == "captureColorRegion":
        delay_ms = int(action.get("delayMs", 100))
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)
        image_path = _capture_screen(device_target, repo_dir, scenario_name, f"color_{step_index}")
        region = action.get("region")
        if not isinstance(region, dict):
            raise RuntimeError("captureColorRegion requires a region object.")
        color = str(action.get("color") or "blue")
        ratio = _color_region_ratio(image_path, region, color)
        min_ratio = float(action.get("minRatio", 0.1))
        max_ratio_value = action.get("maxRatio")
        max_ratio = float(max_ratio_value) if max_ratio_value is not None else None
        visual_name = str(action.get("visualName") or f"{color}_region")
        passed = ratio >= min_ratio
        if max_ratio is not None:
            passed = passed and ratio <= max_ratio
        return "\n".join(
            [
                f"CAPTURE_COLOR_REGION={format_path_for_display(image_path, start=repo_dir)}",
                f"CAPTURE_COLOR_NAME={color}",
                f"CAPTURE_COLOR_RATIO={ratio:.6f}",
                f"CAPTURE_COLOR_MIN_RATIO={min_ratio:.6f}",
                f"CAPTURE_COLOR_MAX_RATIO={max_ratio:.6f}" if max_ratio is not None else "CAPTURE_COLOR_MAX_RATIO=<none>",
                f"CAPTURE_COLOR_VISUAL_NAME={visual_name}",
                f"CAPTURE_COLOR_RESULT={'PASS' if passed else 'FAIL'}",
            ]
        )
    if action_type == "assertLatestSavedQrCrop":
        return _assert_latest_saved_qr_crop(device_target, repo_dir, scenario_name, action, step_index)
    if action_type == "assertTextVisible":
        text = str(action["text"])
        timeout = float(action.get("timeoutSec") or 0.0)
        if timeout > 0:
            layout, info = _wait_for_layout_text(
                device_target,
                repo_dir,
                scenario_name,
                text,
                f"assert_text_{step_index}",
                timeout_sec=timeout,
                interval_sec=float(action.get("intervalSec", 0.5)),
            )
        else:
            layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"assert_text_{step_index}")
        if not _layout_contains_text(layout, text):
            raise RuntimeError(f"Unable to find visible text {text!r}. {info}")
        return f"ASSERT_TEXT_VISIBLE={text}\n{info}"
    if action_type == "assertTextNotVisible":
        text = str(action["text"])
        wait_sec = float(action.get("waitSec") or 0.0)
        if wait_sec > 0:
            time.sleep(wait_sec)
        layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"assert_text_absent_{step_index}")
        if _layout_contains_text(layout, text):
            raise RuntimeError(f"Unexpected visible text {text!r}. {info}")
        return f"ASSERT_TEXT_NOT_VISIBLE={text}\n{info}"
    if action_type == "assertIdVisible":
        element_id = str(action["id"])
        layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"assert_id_{step_index}")
        if _find_attr_bounds(layout, element_id) is None:
            raise RuntimeError(f"Unable to find visible id/key/text {element_id!r}. {info}")
        return f"ASSERT_ID_VISIBLE={element_id}\n{info}"
    if action_type == "assertIdTextEquals":
        element_id = str(action["id"])
        expected_text = str(action["text"])
        layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"assert_id_text_{step_index}")
        node = _find_node_by_attr(layout, element_id)
        if node is None:
            raise RuntimeError(f"Unable to find visible id/key/text {element_id!r}. {info}")
        actual_text = _node_visible_text(node)
        if actual_text != expected_text:
            raise RuntimeError(
                f"Expected id/key {element_id!r} text {expected_text!r}, got {actual_text!r}. {info}"
            )
        return f"ASSERT_ID_TEXT_EQUALS={element_id} TEXT={expected_text}\n{info}"
    if action_type == "assertTextRightSiblingGap":
        text = str(action["text"])
        sibling_type = str(action.get("siblingType") or "Toggle")
        min_gap_px = int(action.get("minGapPx") or 1)
        max_gap_raw = action.get("maxGapPx")
        max_gap_px = int(max_gap_raw) if max_gap_raw is not None else None
        layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"assert_gap_{step_index}")
        _, detail = _assert_text_right_sibling_gap(
            layout,
            text=text,
            sibling_type=sibling_type,
            min_gap_px=min_gap_px,
            max_gap_px=max_gap_px,
        )
        return f"{detail}\n{info}"
    if action_type == "assertTextInsideParent":
        text = str(action["text"])
        parent_type = str(action.get("parentType") or "Button")
        layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"assert_text_inside_parent_{step_index}")
        _, detail = _assert_text_inside_parent(
            layout,
            text=text,
            parent_type=parent_type,
            min_left_px=int(action.get("minLeftPx") or 0),
            min_right_px=int(action.get("minRightPx") or 0),
            min_top_px=int(action.get("minTopPx") or 0),
            min_bottom_px=int(action.get("minBottomPx") or 0),
        )
        return f"{detail}\n{info}"
    if action_type == "assertButtonPairSpreadAcrossAnchor":
        layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"assert_button_pair_spread_{step_index}")
        _, detail = _assert_button_pair_spread_across_anchor(
            layout,
            left_text=str(action["leftText"]),
            right_text=str(action["rightText"]),
            anchor_type=str(action.get("anchorType") or "Canvas"),
            min_gap_ratio=float(action.get("minGapRatio") or 0.25),
            left_center_max_ratio=float(action.get("leftCenterMaxRatio") or 0.4),
            right_center_min_ratio=float(action.get("rightCenterMinRatio") or 0.6),
        )
        return f"{detail}\n{info}"
    if action_type == "assertTextGroupVerticalPosition":
        layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"assert_text_group_vertical_{step_index}")
        min_center_raw = action.get("minCenterRatio")
        max_center_raw = action.get("maxCenterRatio")
        _, detail = _assert_text_group_vertical_position(
            layout,
            top_text=str(action["topText"]),
            bottom_text=str(action["bottomText"]),
            min_center_ratio=float(min_center_raw) if min_center_raw is not None else None,
            max_center_ratio=float(max_center_raw) if max_center_raw is not None else None,
            min_top_margin_ratio=float(action.get("minTopMarginRatio") or 0.0),
            min_bottom_margin_ratio=float(action.get("minBottomMarginRatio") or 0.0),
        )
        return f"{detail}\n{info}"
    if action_type == "assertDialogPanelVerticalPosition":
        layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"assert_dialog_panel_vertical_{step_index}")
        min_center_raw = action.get("minCenterRatio")
        max_center_raw = action.get("maxCenterRatio")
        _, detail = _assert_dialog_panel_vertical_position(
            layout,
            min_center_ratio=float(min_center_raw) if min_center_raw is not None else None,
            max_center_ratio=float(max_center_raw) if max_center_raw is not None else None,
            min_top_margin_ratio=float(action.get("minTopMarginRatio") or 0.0),
            min_bottom_margin_ratio=float(action.get("minBottomMarginRatio") or 0.0),
        )
        return f"{detail}\n{info}"
    if action_type == "assertIdInsideViewport":
        layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"assert_id_inside_viewport_{step_index}")
        _, detail = _assert_attr_bounds_inside_viewport(
            layout,
            element_id=str(action["id"]),
            min_width_px=int(action.get("minWidthPx") or 1),
            min_height_px=int(action.get("minHeightPx") or 1),
            min_left_margin_px=int(action.get("minLeftMarginPx") or 0),
            min_right_margin_px=int(action.get("minRightMarginPx") or 0),
            min_top_margin_px=int(action.get("minTopMarginPx") or 0),
            min_bottom_margin_px=int(action.get("minBottomMarginPx") or 0),
        )
        return f"{detail}\n{info}"
    if action_type == "assertIdHorizontalMargins":
        layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"assert_id_horizontal_margins_{step_index}")
        _, detail = _assert_attr_horizontal_margins(
            layout,
            element_id=str(action["id"]),
            min_left_margin_px=int(action.get("minLeftMarginPx") or 0),
            min_right_margin_px=int(action.get("minRightMarginPx") or 0),
            min_left_margin_ratio=float(action.get("minLeftMarginRatio") or 0.0),
            min_right_margin_ratio=float(action.get("minRightMarginRatio") or 0.0),
        )
        return f"{detail}\n{info}"
    if action_type == "assertIdAncestorHorizontalMargins":
        layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"assert_id_ancestor_horizontal_margins_{step_index}")
        max_left_margin_px_raw = action.get("maxLeftMarginPx")
        max_right_margin_px_raw = action.get("maxRightMarginPx")
        max_left_margin_ratio_raw = action.get("maxLeftMarginRatio")
        max_right_margin_ratio_raw = action.get("maxRightMarginRatio")
        _, detail = _assert_attr_ancestor_horizontal_margins(
            layout,
            element_id=str(action["id"]),
            ancestor_type=str(action.get("ancestorType") or "Column"),
            max_left_margin_px=int(max_left_margin_px_raw) if max_left_margin_px_raw is not None else None,
            max_right_margin_px=int(max_right_margin_px_raw) if max_right_margin_px_raw is not None else None,
            max_left_margin_ratio=float(max_left_margin_ratio_raw) if max_left_margin_ratio_raw is not None else None,
            max_right_margin_ratio=float(max_right_margin_ratio_raw) if max_right_margin_ratio_raw is not None else None,
        )
        return f"{detail}\n{info}"
    if action_type == "assertSliderLeftAlignedWithText":
        text = str(action["text"])
        slider_type = str(action.get("sliderType") or "Slider")
        max_left_delta_px = int(action.get("maxLeftDeltaPx") or 20)
        layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"assert_slider_left_{step_index}")
        _, detail = _assert_slider_left_aligned_with_text(
            layout,
            text=text,
            slider_type=slider_type,
            max_left_delta_px=max_left_delta_px,
        )
        return f"{detail}\n{info}"
    if action_type == "assertComponentNumericTextRange":
        component_type = str(action["componentType"])
        min_value = float(action["min"])
        max_value = float(action["max"])
        layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"assert_numeric_{step_index}")
        _, detail = _assert_component_numeric_text_range(
            layout,
            component_type=component_type,
            min_value=min_value,
            max_value=max_value,
        )
        return f"{detail}\n{info}"
    if action_type == "assertComponentCount":
        layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"assert_count_{step_index}")
        _, detail = _assert_component_count(
            layout,
            component_type=str(action["componentType"]),
            expected_count=int(action["expectedCount"]),
        )
        return f"{detail}\n{info}"
    if action_type == "assertFlipClockHourMatchesDeviceTime":
        layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"assert_flipclock_hour_{step_index}")
        _, detail = _assert_flipclock_hour_matches_device_time(device_target, layout)
        return f"{detail}\n{info}"
    if action_type == "assertStepperNumericTextRange":
        min_value = float(action["min"])
        max_value = float(action["max"])
        layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"assert_stepper_{step_index}")
        _, detail = _assert_stepper_numeric_text_range(
            layout,
            min_value=min_value,
            max_value=max_value,
        )
        return f"{detail}\n{info}"
    if action_type == "assertVisibleButtonNearId":
        layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"assert_button_near_{step_index}")
        _, detail = _assert_visible_button_near_id(
            layout,
            anchor_id=str(action["id"]),
            min_width_px=int(action.get("minWidthPx") or 1),
            min_height_px=int(action.get("minHeightPx") or 1),
            max_right_px=int(action["maxRightPx"]) if action.get("maxRightPx") is not None else None,
            click_index=int(action.get("index") or 0),
        )
        return f"{detail}\n{info}"
    if action_type == "assertButtonRowInsideId":
        layout, info = _dump_layout_json(device_target, repo_dir, scenario_name, f"assert_button_row_inside_{step_index}")
        _, detail = _assert_button_row_inside_id(
            layout,
            anchor_id=str(action["id"]),
            expected_count=int(action.get("expectedCount") or 3),
            min_width_px=int(action.get("minWidthPx") or 1),
            min_height_px=int(action.get("minHeightPx") or 1),
            horizontal_slop_px=int(action.get("horizontalSlopPx") or 0),
            use_orig_bounds=bool(action.get("useOrigBounds")),
        )
        return f"{detail}\n{info}"
    raise RuntimeError(f"Unknown host UI action type: {action_type}")


def _host_ui_hilog_command(scenario: dict[str, Any]) -> list[str]:
    hilog_command = ["hilog", "-x"]
    if scenario.get("hilogAll"):
        return hilog_command
    if scenario.get("hilogTag"):
        hilog_command.extend(["-T", str(scenario["hilogTag"])])
    elif not scenario.get("hilogDomain"):
        hilog_command.extend(["-T", "TaskManager"])
    hilog_command.extend(["-D", str(scenario.get("hilogDomain") or "0xFF00")])
    return hilog_command


def _host_ui_success_log_numeric_seen(scenario: dict[str, Any], text: str, lines: list[str]) -> bool:
    numeric_regex = str(scenario.get("successLogNumericRegex") or "")
    if not numeric_regex:
        return False
    group_index = int(scenario.get("successLogNumericGroup") or 1)
    matches = list(re.finditer(numeric_regex, text))
    values: list[float] = []
    for match in matches:
        try:
            values.append(float(match.group(group_index)))
        except (IndexError, ValueError):
            continue
    min_raw = scenario.get("successLogNumericMin")
    max_raw = scenario.get("successLogNumericMax")
    min_value = float(min_raw) if min_raw is not None else None
    max_value = float(max_raw) if max_raw is not None else None
    last_value = values[-1] if values else None
    passed = last_value is not None
    if min_value is not None and last_value is not None:
        passed = passed and last_value >= min_value
    if max_value is not None and last_value is not None:
        passed = passed and last_value <= max_value
    lines.append(f"SUCCESS_LOG_NUMERIC_REGEX={numeric_regex}")
    lines.append(f"SUCCESS_LOG_NUMERIC_COUNT={len(values)}")
    lines.append(f"SUCCESS_LOG_NUMERIC_GROUP={group_index}")
    lines.append(f"SUCCESS_LOG_NUMERIC_LAST={last_value if last_value is not None else '<none>'}")
    lines.append(f"SUCCESS_LOG_NUMERIC_MIN={min_value if min_value is not None else '<none>'}")
    lines.append(f"SUCCESS_LOG_NUMERIC_MAX={max_value if max_value is not None else '<none>'}")
    lines.append(f"SUCCESS_LOG_NUMERIC_RESULT={'PASS' if passed else 'FAIL'}")
    return passed


def _host_ui_failure_log_regex_seen(scenario: dict[str, Any], text: str, lines: list[str]) -> bool:
    failure_log_regex = str(scenario.get("failureLogRegex") or "")
    if not failure_log_regex:
        return False
    count = len(re.findall(failure_log_regex, text, flags=re.S))
    lines.append(f"FAILURE_LOG_REGEX={failure_log_regex}")
    lines.append(f"FAILURE_LOG_REGEX_COUNT={count}")
    return count > 0


def _host_ui_failure_log_numeric_count_seen(scenario: dict[str, Any], text: str, lines: list[str]) -> bool:
    numeric_regex = str(scenario.get("failureLogNumericRegex") or "")
    if not numeric_regex:
        return False
    window_start_regex = str(scenario.get("failureLogNumericWindowStartRegex") or "")
    window_end_regex = str(scenario.get("failureLogNumericWindowEndRegex") or "")
    window_text = text
    if window_start_regex:
        starts = list(re.finditer(window_start_regex, text, flags=re.S))
        if starts:
            window_text = text[starts[-1].start():]
        else:
            window_text = ""
    if window_end_regex and window_text:
        end_match = re.search(window_end_regex, window_text, flags=re.S)
        if end_match:
            window_text = window_text[:end_match.end()]
    group_index = int(scenario.get("failureLogNumericGroup") or 1)
    min_raw = scenario.get("failureLogNumericMin")
    max_raw = scenario.get("failureLogNumericMax")
    min_value = float(min_raw) if min_raw is not None else None
    max_value = float(max_raw) if max_raw is not None else None
    values: list[float] = []
    for match in re.finditer(numeric_regex, window_text, flags=re.S):
        try:
            value = float(match.group(group_index))
        except (IndexError, ValueError):
            continue
        if min_value is not None and value < min_value:
            continue
        if max_value is not None and value > max_value:
            continue
        values.append(value)
    min_count = int(scenario.get("failureLogNumericMinCount") or 1)
    passed = len(values) >= min_count
    lines.append(f"FAILURE_LOG_NUMERIC_WINDOW_START_REGEX={window_start_regex if window_start_regex else '<none>'}")
    lines.append(f"FAILURE_LOG_NUMERIC_WINDOW_END_REGEX={window_end_regex if window_end_regex else '<none>'}")
    lines.append(f"FAILURE_LOG_NUMERIC_REGEX={numeric_regex}")
    lines.append(f"FAILURE_LOG_NUMERIC_GROUP={group_index}")
    lines.append(f"FAILURE_LOG_NUMERIC_MIN={min_value if min_value is not None else '<none>'}")
    lines.append(f"FAILURE_LOG_NUMERIC_MAX={max_value if max_value is not None else '<none>'}")
    lines.append(f"FAILURE_LOG_NUMERIC_MIN_COUNT={min_count}")
    lines.append(f"FAILURE_LOG_NUMERIC_VALUES={','.join(str(value) for value in values)}")
    lines.append(f"FAILURE_LOG_NUMERIC_COUNT={len(values)}")
    lines.append(f"FAILURE_LOG_NUMERIC_RESULT={'FAILURE_SEEN' if passed else 'FAILURE_ABSENT'}")
    return passed


def _run_host_ui_scenario(device_target: str, repo_dir: Path, scenario: dict[str, Any], timeout_sec: int) -> tuple[int, str]:
    name = str(scenario.get("name") or "host_ui_scenario")
    target_bundle = str(scenario.get("targetBundle") or "com.samples.stageprocessthread")
    target_ability = str(scenario.get("targetAbility") or "EntryAbility")
    target_module = str(scenario.get("targetModule") or "")
    setup_bundle = str(scenario.get("setupBundle") or "")
    setup_ability = str(scenario.get("setupAbility") or "")
    setup_module = str(scenario.get("setupModule") or "")
    success_log = str(scenario.get("successLog") or "getMissionInfos.find etsclock")
    failure_log = str(scenario.get("failureLog") or "getMissionInfos.find etsclock")
    expect = str(scenario.get("expect") or "success_log")
    actions = list(scenario.get("actions") or [])
    for text in scenario.get("clickTexts") or []:
        actions.append({"type": "clickText", "text": str(text)})
    if not actions and not scenario.get("runInstrumentClass"):
        raise RuntimeError(f"Host UI scenario {name} has no actions or clickTexts.")

    lines = [
        f"HOST_UI_SCENARIO_NAME={name}",
        f"HOST_UI_SCENARIO_FILE={format_path_for_display(scenario.get('_scenario_file', ''), start=repo_dir)}",
        f"EXPECT={expect}",
    ]
    _hdc_shell(device_target, "hilog", "-r", timeout_sec=10.0)

    for bundle in scenario.get("forceStopBundles") or []:
        bundle_name = str(bundle).strip()
        if bundle_name:
            _hdc_shell(device_target, "aa", "force-stop", bundle_name, timeout_sec=10.0)
            lines.append(f"FORCE_STOP_BUNDLE={bundle_name}")

    for index, setup_item in enumerate(scenario.get("setupAbilities") or [], start=1):
        if not isinstance(setup_item, dict):
            raise RuntimeError(f"setupAbilities entry must be an object: {setup_item!r}")
        setup_command = [
            "aa",
            "start",
            "-b",
            str(setup_item["bundleName"]),
            "-a",
            str(setup_item["abilityName"]),
        ]
        setup_module = str(setup_item.get("moduleName") or "")
        if setup_module:
            setup_command.extend(["-m", setup_module])
        _hdc_shell(device_target, *setup_command, timeout_sec=15.0)
        lines.append(f"SETUP_ABILITY_{index}_COMMAND=" + " ".join(setup_command))
        time.sleep(float(setup_item.get("delaySec") or scenario.get("setupDelaySec") or 0.8))
        _hdc_shell(device_target, "uitest", "uiInput", "keyEvent", "Home", timeout_sec=10.0)
        time.sleep(float(scenario.get("setupHomeDelaySec") or 0.3))

    start_command = ["aa", "start", "-d", "0", "-b", target_bundle, "-a", target_ability]
    if target_module:
        start_command.extend(["-m", target_module])
    start_params = scenario.get("startParams")
    if isinstance(start_params, dict):
        _append_aa_start_params(start_command, start_params)
    start_output = _hdc_shell(device_target, *start_command, timeout_sec=15.0)
    lines.append("START_COMMAND=" + " ".join(start_command))
    lines.append("START_OUTPUT=" + start_output.strip())
    time.sleep(float(scenario.get("startDelaySec") or 1.5))

    run_instrument_class = str(scenario.get("runInstrumentClass") or "")
    if run_instrument_class:
        discovered_targets = _discover_targets(repo_dir)
        instrument_targets = [target for target in discovered_targets if target.kind == "instrument"]
        if not instrument_targets:
            raise RuntimeError(f"Host UI scenario {name} requested runInstrumentClass but no instrument target exists.")
        command = _build_instrument_command(
            device_target,
            instrument_targets[0],
            (run_instrument_class,),
            hypium_timeout_ms=max(DEFAULT_WAIT_TIME_MS, int(max(float(timeout_sec), 1.0) * 1000)),
            wait_time_ms=max(DEFAULT_WAIT_TIME_MS, int(max(float(timeout_sec), 1.0) * 1000)),
        )
        result = run_command(command, cwd=repo_dir, timeout_sec=max(float(timeout_sec), 1.0))
        output_text = command_output(result)
        instrument_failed = result.returncode != 0 or _looks_like_test_execution_failure(output_text)
        lines.extend(
            [
                "INSTRUMENT_COMMAND_BEGIN",
                _format_command_for_log(command),
                "INSTRUMENT_COMMAND_END",
                f"INSTRUMENT_EXIT_CODE={result.returncode}",
                f"INSTRUMENT_EFFECTIVE_FAILED={str(instrument_failed).lower()}",
                "INSTRUMENT_OUTPUT_BEGIN",
                output_text.rstrip(),
                "INSTRUMENT_OUTPUT_END",
            ]
        )
        if instrument_failed:
            return 1, "\n".join(lines)

    if not actions:
        hilog_command = _host_ui_hilog_command(scenario)
        hilog = _hdc_shell(device_target, *hilog_command, timeout_sec=10.0)
        lines.extend(["HILOG_BEGIN", hilog.rstrip(), "HILOG_END"])
        final_layout, layout_info = _dump_layout_json(device_target, repo_dir, name, "final")
        lines.append(layout_info)

        success_seen = success_log in hilog
        failure_seen = failure_log in hilog if failure_log else False
        success_log_regex = str(scenario.get("successLogRegex") or "")
        failure_log_regex = str(scenario.get("failureLogRegex") or "")
        if success_log_regex:
            success_seen = success_seen or re.search(success_log_regex, hilog) is not None
        if failure_log_regex:
            failure_seen = failure_seen or _host_ui_failure_log_regex_seen(scenario, hilog, lines)
        failure_seen = failure_seen or _host_ui_failure_log_numeric_count_seen(scenario, hilog, lines)
        success_seen = success_seen or _host_ui_success_log_numeric_seen(scenario, hilog, lines)
        success_text = str(scenario.get("successText") or "")
        failure_text = str(scenario.get("failureText") or "")
        if success_text:
            success_seen = success_seen or _layout_contains_text(final_layout, success_text)
        if failure_text:
            failure_seen = failure_seen or _layout_contains_text(final_layout, failure_text)
        success_text_regex = str(scenario.get("successTextRegex") or "")
        if success_text_regex:
            min_count = int(scenario.get("successTextRegexMinCount") or 1)
            match_count = _layout_text_regex_count(final_layout, success_text_regex)
            lines.append(f"SUCCESS_TEXT_REGEX={success_text_regex}")
            lines.append(f"SUCCESS_TEXT_REGEX_COUNT={match_count}")
            lines.append(f"SUCCESS_TEXT_REGEX_MIN_COUNT={min_count}")
            success_seen = success_seen or match_count >= min_count
        failure_text_regex = str(scenario.get("failureTextRegex") or "")
        if failure_text_regex:
            min_count = int(scenario.get("failureTextRegexMinCount") or 1)
            match_count = _layout_text_regex_count(final_layout, failure_text_regex)
            lines.append(f"FAILURE_TEXT_REGEX={failure_text_regex}")
            lines.append(f"FAILURE_TEXT_REGEX_COUNT={match_count}")
            lines.append(f"FAILURE_TEXT_REGEX_MIN_COUNT={min_count}")
            failure_seen = failure_seen or match_count >= min_count
        success_page_path = str(scenario.get("successPagePath") or "")
        failure_page_path = str(scenario.get("failurePagePath") or "")
        success_bundle = str(scenario.get("successBundle") or "")
        failure_bundle = str(scenario.get("failureBundle") or "")
        if success_page_path:
            success_seen = success_seen or _layout_contains_attr(final_layout, "pagePath", success_page_path)
        if failure_page_path:
            failure_seen = failure_seen or _layout_contains_attr(final_layout, "pagePath", failure_page_path)
        if success_bundle:
            success_seen = success_seen or _layout_contains_attr(final_layout, "bundleName", success_bundle)
        if failure_bundle:
            failure_seen = failure_seen or _layout_contains_attr(final_layout, "bundleName", failure_bundle)
        if expect == "success_without_failure":
            passed = success_seen and not failure_seen
            reason = "success log/text found without failure" if passed else "missing success or failure log/text found"
        elif expect == "success_log":
            passed = success_seen
            reason = "success log/text found" if passed else "missing expected success log/text"
        elif expect == "no_success_log":
            passed = not success_seen
            reason = "success log/text absent" if passed else "unexpected success log/text found"
        elif expect == "failure_log":
            passed = failure_seen and not success_seen
            reason = "failure log/text found without success" if passed else "missing failure or unexpected success"
        else:
            raise RuntimeError(f"Unknown host UI scenario expectation: {expect}")

        lines.append(f"HOST_UI_SCENARIO_SUCCESS_SEEN={str(success_seen).lower()}")
        lines.append(f"HOST_UI_SCENARIO_FAILURE_SEEN={str(failure_seen).lower()}")
        lines.append(f"HOST_UI_SCENARIO_RESULT={'PASS' if passed else 'FAIL'}")
        lines.append(f"HOST_UI_SCENARIO_REASON={reason}")
        return (0 if passed else 1), "\n".join(lines)

    _hdc_shell(device_target, "aa", "force-stop", target_bundle, timeout_sec=10.0)
    start_command = ["aa", "start", "-d", "0", "-b", target_bundle, "-a", target_ability]
    if target_module:
        start_command.extend(["-m", target_module])
    start_params = scenario.get("startParams")
    if isinstance(start_params, dict):
        _append_aa_start_params(start_command, start_params)
    start_output = _hdc_shell(device_target, *start_command, timeout_sec=15.0)
    lines.append("ACTION_START_COMMAND=" + " ".join(start_command))
    lines.append("ACTION_START_OUTPUT=" + start_output.strip())
    time.sleep(float(scenario.get("startDelaySec") or 1.5))
    if scenario.get("clearMissions", True):
        for swipe_index in range(2):
            _hdc_shell(device_target, "uitest", "uiInput", "swipe", "900", "2400", "900", "600", "800", timeout_sec=10.0)
            time.sleep(0.3)
        try:
            lines.append(_click_text(device_target, repo_dir, name, str(scenario.get("clearMissionsText") or "删除全部任务"), 0))
            time.sleep(1.5)
        except Exception as exc:
            lines.append(f"CLEAR_MISSIONS_WARNING={exc}")

    if setup_bundle and setup_ability:
        setup_command = ["aa", "start", "-b", setup_bundle, "-a", setup_ability]
        if setup_module:
            setup_command.extend(["-m", setup_module])
        _hdc_shell(device_target, *setup_command, timeout_sec=15.0)
        time.sleep(1.0)
        _hdc_shell(device_target, "uitest", "uiInput", "keyEvent", "Home", timeout_sec=10.0)
        time.sleep(0.5)
        _hdc_shell(device_target, *start_command, timeout_sec=15.0)
        time.sleep(1.5)
        mission_list = _hdc_shell(device_target, "aa", "dump", "-l", timeout_sec=10.0)
        lines.extend(["MISSION_LIST_BEGIN", mission_list.rstrip(), "MISSION_LIST_END"])

    for index, raw_action in enumerate(actions, start=1):
        if not isinstance(raw_action, dict):
            raise RuntimeError(f"Host UI action must be an object: {raw_action!r}")
        try:
            action_result = _run_host_ui_action(device_target, repo_dir, name, raw_action, index)
            lines.append(action_result)
            if "CAPTURE_TOAST_RESULT=PASS" in action_result:
                lines.append("HOST_UI_VISUAL_SUCCESS=toast_dark_region")
            if "CAPTURE_TOAST_RESULT=FAIL" in action_result:
                lines.append("HOST_UI_VISUAL_FAILURE=toast_dark_region_missing")
            color_visual_match = re.search(r"CAPTURE_COLOR_VISUAL_NAME=([^\n\r]+)", action_result)
            if color_visual_match:
                color_visual = color_visual_match.group(1).strip()
                if "CAPTURE_COLOR_RESULT=PASS" in action_result:
                    lines.append(f"HOST_UI_VISUAL_SUCCESS={color_visual}")
                if "CAPTURE_COLOR_RESULT=FAIL" in action_result:
                    lines.append(f"HOST_UI_VISUAL_FAILURE={color_visual}_missing")
            saved_qr_visual_match = re.search(r"SAVED_QR_VISUAL_NAME=([^\n\r]+)", action_result)
            if saved_qr_visual_match:
                saved_qr_visual = saved_qr_visual_match.group(1).strip()
                if "SAVED_QR_RESULT=PASS" in action_result:
                    lines.append(f"HOST_UI_VISUAL_SUCCESS={saved_qr_visual}")
                if "SAVED_QR_RESULT=FAIL" in action_result:
                    lines.append(f"HOST_UI_VISUAL_FAILURE={saved_qr_visual}_mismatch")
        except Exception as exc:
            if raw_action.get("optional"):
                lines.append(f"OPTIONAL_ACTION_WARNING={exc}")
            else:
                lines.append(f"ACTION_ERROR={exc}")
                try:
                    hilog_command = _host_ui_hilog_command(scenario)
                    hilog = _hdc_shell(device_target, *hilog_command, timeout_sec=10.0)
                    lines.extend(["HILOG_BEGIN", hilog.rstrip(), "HILOG_END"])
                except Exception as log_exc:
                    lines.append(f"HILOG_CAPTURE_ERROR={log_exc}")
                try:
                    final_layout, layout_info = _dump_layout_json(device_target, repo_dir, name, "error")
                    lines.append(layout_info)
                except Exception as layout_exc:
                    lines.append(f"LAYOUT_CAPTURE_ERROR={layout_exc}")
                lines.append("HOST_UI_SCENARIO_RESULT=ERROR")
                return 1, "\n".join(lines)
        delay_after = raw_action.get("delayAfterSec")
        if delay_after is None:
            delay_after = scenario.get("delayAfterClickSec")
        if delay_after is None:
            delay_after = 1.0
        time.sleep(float(delay_after))

    hilog_command = _host_ui_hilog_command(scenario)
    hilog = _hdc_shell(device_target, *hilog_command, timeout_sec=10.0)
    lines.extend(["HILOG_BEGIN", hilog.rstrip(), "HILOG_END"])
    final_layout, layout_info = _dump_layout_json(device_target, repo_dir, name, "final")
    lines.append(layout_info)

    success_seen = success_log in hilog
    log_success_seen = success_seen
    failure_seen = failure_log in hilog if failure_log else False
    success_log_regex = str(scenario.get("successLogRegex") or "")
    failure_log_regex = str(scenario.get("failureLogRegex") or "")
    action_and_hilog = "\n".join(lines) + "\n" + hilog
    if success_log_regex:
        regex_seen = re.search(success_log_regex, action_and_hilog) is not None
        success_seen = success_seen or regex_seen
        log_success_seen = log_success_seen or regex_seen
    if failure_log_regex:
        failure_seen = failure_seen or _host_ui_failure_log_regex_seen(scenario, action_and_hilog, lines)
    failure_seen = failure_seen or _host_ui_failure_log_numeric_count_seen(scenario, action_and_hilog, lines)
    numeric_seen = _host_ui_success_log_numeric_seen(scenario, action_and_hilog, lines)
    success_seen = success_seen or numeric_seen
    log_success_seen = log_success_seen or numeric_seen
    success_text = str(scenario.get("successText") or "")
    failure_text = str(scenario.get("failureText") or "")
    if success_text:
        success_seen = success_seen or _layout_contains_text(final_layout, success_text)
    if failure_text:
        failure_seen = failure_seen or _layout_contains_text(final_layout, failure_text)
    success_text_regex = str(scenario.get("successTextRegex") or "")
    if success_text_regex:
        min_count = int(scenario.get("successTextRegexMinCount") or 1)
        match_count = _layout_text_regex_count(final_layout, success_text_regex)
        lines.append(f"SUCCESS_TEXT_REGEX={success_text_regex}")
        lines.append(f"SUCCESS_TEXT_REGEX_COUNT={match_count}")
        lines.append(f"SUCCESS_TEXT_REGEX_MIN_COUNT={min_count}")
        success_seen = success_seen or match_count >= min_count
    failure_text_regex = str(scenario.get("failureTextRegex") or "")
    if failure_text_regex:
        min_count = int(scenario.get("failureTextRegexMinCount") or 1)
        match_count = _layout_text_regex_count(final_layout, failure_text_regex)
        lines.append(f"FAILURE_TEXT_REGEX={failure_text_regex}")
        lines.append(f"FAILURE_TEXT_REGEX_COUNT={match_count}")
        lines.append(f"FAILURE_TEXT_REGEX_MIN_COUNT={min_count}")
        failure_seen = failure_seen or match_count >= min_count
    success_page_path = str(scenario.get("successPagePath") or "")
    failure_page_path = str(scenario.get("failurePagePath") or "")
    success_bundle = str(scenario.get("successBundle") or "")
    failure_bundle = str(scenario.get("failureBundle") or "")
    if success_page_path:
        success_seen = success_seen or _layout_contains_attr(final_layout, "pagePath", success_page_path)
    if failure_page_path:
        failure_seen = failure_seen or _layout_contains_attr(final_layout, "pagePath", failure_page_path)
    if success_bundle:
        success_seen = success_seen or _layout_contains_attr(final_layout, "bundleName", success_bundle)
    if failure_bundle:
        failure_seen = failure_seen or _layout_contains_attr(final_layout, "bundleName", failure_bundle)
    success_visual = str(scenario.get("successVisual") or "")
    if success_visual:
        visual_success_seen = any(
            line.strip() == f"HOST_UI_VISUAL_SUCCESS={success_visual}"
            for line in lines
        )
        success_seen = success_seen or visual_success_seen
    else:
        visual_success_seen = False
    failure_visual = str(scenario.get("failureVisual") or "")
    if failure_visual:
        failure_seen = failure_seen or any(line == f"HOST_UI_VISUAL_FAILURE={failure_visual}" for line in lines)
    lines.append(f"HOST_UI_VISUAL_SUCCESS_SEEN={str(visual_success_seen).lower()}")

    if expect == "success_log":
        passed = success_seen
        reason = "success log/text found" if passed else "missing expected success log/text"
    elif expect == "success_visual":
        passed = visual_success_seen
        reason = "visual signal found" if passed else "missing visual signal"
    elif expect == "success_log_and_visual":
        passed = log_success_seen and visual_success_seen
        reason = (
            "success log and visual signal found"
            if passed
            else "missing success log or visual signal"
        )
    elif expect == "no_success_log":
        passed = not success_seen
        reason = "success log/text absent" if passed else "unexpected success log/text found"
    elif expect == "failure_log":
        passed = failure_seen and not success_seen
        reason = "failure log/text found without success" if passed else "missing failure or unexpected success"
    elif expect == "success_without_failure":
        passed = success_seen and not failure_seen
        reason = "success log/text found without failure" if passed else "missing success or failure log/text found"
    else:
        raise RuntimeError(f"Unknown host UI scenario expectation: {expect}")

    lines.append(f"HOST_UI_SCENARIO_SUCCESS_SEEN={str(success_seen).lower()}")
    lines.append(f"HOST_UI_SCENARIO_FAILURE_SEEN={str(failure_seen).lower()}")
    lines.append(f"HOST_UI_SCENARIO_RESULT={'PASS' if passed else 'FAIL'}")
    lines.append(f"HOST_UI_SCENARIO_REASON={reason}")
    return (0 if passed else 1), "\n".join(lines)


def _run_host_ui_scenarios(
    device_target: str,
    repo_dir: Path,
    scenarios: list[dict[str, Any]],
    timeout_sec: int,
    log_path: Path,
) -> int:
    aggregate_exit_code = 0
    for index, scenario in enumerate(scenarios, start=1):
        try:
            exit_code, body = _run_host_ui_scenario(device_target, repo_dir, scenario, timeout_sec)
        except Exception as exc:
            exit_code = 1
            body = f"HOST_UI_SCENARIO_RESULT=ERROR\nERROR={exc}"
        _append_log_block(log_path, f"HOST_UI_SCENARIO_{index}", body)
        if aggregate_exit_code == 0 and exit_code != 0:
            aggregate_exit_code = exit_code
    return aggregate_exit_code


def _looks_like_test_execution_failure(output_text: str) -> bool:
    normalized = output_text.lower()
    for match in OHOS_REPORT_CODE_RE.finditer(output_text):
        try:
            if int(match.group(1)) != 0:
                return True
        except ValueError:
            return True
    for match in OHOS_REPORT_RESULT_RE.finditer(output_text):
        try:
            if int(match.group(1)) != 0 or int(match.group(2)) != 0:
                return True
        except ValueError:
            return True
    for match in OHOS_REPORT_STATUS_CODE_RE.finditer(output_text):
        try:
            if int(match.group(1)) < 0:
                return True
        except ValueError:
            return True
    for match in TEST_FINISHED_RESULT_CODE_RE.finditer(output_text):
        try:
            if int(match.group(1)) != 0:
                return True
        except ValueError:
            return True
    return any(marker in normalized for marker in TEST_EXECUTION_FAILURE_MARKERS)


def _remaining_timeout(deadline: float) -> float:
    return max(deadline - time.monotonic(), 1.0)


def _sdk_precheck_blocks_tests(sdk_roots: list[Path], sdk_meta: dict[str, Any]) -> str | None:
    """
    If SDK roots are missing or ``build-profile.json5`` API levels are not installed under any root,
    instrument tests cannot succeed (HAP must match the same API toolchain). Return ``None`` if OK.
    """
    if not sdk_roots:
        return sdk_meta.get("sdk_selection_note") or (
            "No OpenHarmony SDK root found; set DEVECO_SDK_HOME or pass a valid --deveco-path."
        )
    if sdk_meta.get("sdk_selection_note"):
        return str(sdk_meta["sdk_selection_note"])
    return None


def run_all_tests(
    repo_path: str,
    deveco_path: str,
    timeout_sec: int = 1800,
    *,
    product_name: str = "default",
    class_filters: tuple[str, ...] = (),
) -> tuple[int, str]:
    repo_dir = resolve_directory(repo_path, "repo_path")
    deveco_dir = resolve_directory(deveco_path, "deveco_path")

    sdk_roots, sdk_meta = get_ordered_sdk_roots_for_repo(
        repo_dir,
        deveco_dir,
        product_name=product_name,
    )

    log_path = (_logs_dir() / f"run_tests-{_timestamp_slug()}.log").resolve()

    sdk_fail = _sdk_precheck_blocks_tests(sdk_roots, sdk_meta)
    if sdk_fail:
        _write_header(
            log_path,
            repo_dir,
            [],
            [],
            [],
            sdk_meta=sdk_meta,
        )
        _append_log_block(
            log_path,
            "PRECHECK",
            f"RESULT=sdk_precheck_failed\n{sdk_fail}",
        )
        return 1, str(log_path)

    discovered_targets = _discover_targets(repo_dir)
    runnable_targets = [target for target in discovered_targets if target.kind == "instrument"]
    skipped_targets = [target for target in discovered_targets if target.kind != "instrument"]
    _write_header(
        log_path,
        repo_dir,
        discovered_targets,
        runnable_targets,
        skipped_targets,
        sdk_meta=sdk_meta,
    )

    if not discovered_targets:
        host_ui_scenarios = _load_host_ui_scenarios(repo_dir)
        if host_ui_scenarios:
            try:
                device_target = _resolve_single_target()
            except Exception as exc:
                _append_log_block(
                    log_path,
                    "PRECHECK",
                    f"RESULT=target_resolution_failed\nERROR={exc}",
                )
                return 1, str(log_path)
            _append_log_block(
                log_path,
                "PRECHECK",
                f"RESULT=ready_host_ui_only\nSELECTED_TARGET={device_target}",
            )
            _append_log_block(
                log_path,
                "HOST_UI_SCENARIO_DISCOVERY",
                f"RESULT=found\nCOUNT={len(host_ui_scenarios)}",
            )
            return _run_host_ui_scenarios(device_target, repo_dir, host_ui_scenarios, timeout_sec, log_path), str(log_path)
        else:
            _append_log_block(
                log_path,
                "PRECHECK",
                "RESULT=no_test_targets_discovered\nNo src/ohosTest or src/test directory was found under the repo.",
            )
            return 1, str(log_path)

    if not runnable_targets:
        host_ui_scenarios = _load_host_ui_scenarios(repo_dir)
        if host_ui_scenarios:
            try:
                device_target = _resolve_single_target()
            except Exception as exc:
                _append_log_block(
                    log_path,
                    "PRECHECK",
                    f"RESULT=target_resolution_failed\nERROR={exc}",
                )
                return 1, str(log_path)
            _append_log_block(
                log_path,
                "PRECHECK",
                f"RESULT=ready_host_ui_only\nSELECTED_TARGET={device_target}",
            )
            _append_log_block(
                log_path,
                "HOST_UI_SCENARIO_DISCOVERY",
                f"RESULT=found\nCOUNT={len(host_ui_scenarios)}",
            )
            return _run_host_ui_scenarios(device_target, repo_dir, host_ui_scenarios, timeout_sec, log_path), str(log_path)
        else:
            _append_log_block(
                log_path,
                "PRECHECK",
                "RESULT=no_instrument_target_discovered\n"
                "Instrument Test is the only execution mode implemented in this step. "
                "Discovered targets were logged above for follow-up extension.",
            )
            return 1, str(log_path)

    try:
        device_target = _resolve_single_target()
    except Exception as exc:
        _append_log_block(
            log_path,
            "PRECHECK",
            f"RESULT=target_resolution_failed\nERROR={exc}",
        )
        return 1, str(log_path)

    _append_log_block(
        log_path,
        "PRECHECK",
        f"RESULT=ready\nSELECTED_TARGET={device_target}",
    )

    host_ui_scenarios = _load_host_ui_scenarios(repo_dir)
    if host_ui_scenarios:
        _append_log_block(
            log_path,
            "HOST_UI_SCENARIO_DISCOVERY",
            f"RESULT=found\nCOUNT={len(host_ui_scenarios)}",
        )
        return _run_host_ui_scenarios(device_target, repo_dir, host_ui_scenarios, timeout_sec, log_path), str(log_path)

    bundle_name = runnable_targets[0].bundle_name if runnable_targets else None
    installed_module_names = _query_installed_module_names(device_target, bundle_name, repo_dir)
    if installed_module_names:
        missing_targets = [
            target
            for target in runnable_targets
            if target.module_name and target.module_name not in installed_module_names
        ]
        if missing_targets:
            runnable_targets = [
                target
                for target in runnable_targets
                if not target.module_name or target.module_name in installed_module_names
            ]
            skipped_targets.extend(missing_targets)
            _append_log_block(
                log_path,
                "INSTALLED_MODULE_FILTER",
                "\n".join(
                    [
                        "RESULT=filtered_non_installed_test_modules",
                        f"INSTALLED_MODULES={','.join(sorted(installed_module_names))}",
                        *[
                            f"SKIPPED={target.display_name(repo_dir)}"
                            for target in missing_targets
                        ],
                    ]
                ),
            )
    if not runnable_targets:
        _append_log_block(
            log_path,
            "PRECHECK",
            "RESULT=no_installed_instrument_target_discovered\n"
            "Instrument source directories were found, but no matching installed test modules were present.",
        )
        return 1, str(log_path)

    deadline = time.monotonic() + max(float(timeout_sec), 1.0)
    instrument_timeout_ms = max(DEFAULT_WAIT_TIME_MS, int(max(float(timeout_sec), 1.0) * 1000))
    aggregate_exit_code = 0

    for index, test_target in enumerate(runnable_targets, start=1):
        try:
            commands = _build_instrument_command_variants(
                device_target,
                test_target,
                class_filters,
                hypium_timeout_ms=instrument_timeout_ms,
                wait_time_ms=instrument_timeout_ms,
            )
        except Exception as exc:
            _append_log_block(
                log_path,
                f"COMMAND_{index}_RESULT",
                f"EXIT_CODE=1\nERROR={exc}",
            )
            return 1, str(log_path)

        for variant_index, command in enumerate(commands, start=1):
            command_title = f"COMMAND_{index}" if len(commands) == 1 else f"COMMAND_{index}_{variant_index}"
            command_intro = "\n".join(
                [
                    f"TARGET={test_target.display_name(repo_dir)}",
                    f"COMMAND={_format_command_for_log(command)}",
                    f"CLASS_FILTER={','.join(class_filters) if class_filters else '<none>'}",
                    f"TIMEOUT_SEC={int(max(float(timeout_sec), 1.0))}",
                    f"AA_WAIT_TIME_MS={instrument_timeout_ms}",
                    f"HYPIUM_TIMEOUT_MS={instrument_timeout_ms}",
                    f"VARIANT={variant_index}/{len(commands)}",
                ]
            )
            _append_log_block(log_path, command_title, command_intro)

            try:
                result = run_command(
                    command,
                    cwd=repo_dir,
                    timeout_sec=_remaining_timeout(deadline),
                )
                output_text = command_output(result)
                effective_exit_code = result.returncode
                failure_reason = ""
                if effective_exit_code == 0 and _looks_like_test_execution_failure(output_text):
                    effective_exit_code = 1
                    failure_reason = "TEST_EXECUTION_FAILURE=detected_error_output"
                result_body = "\n".join(
                    [item for item in [
                        f"EXIT_CODE={result.returncode}",
                        f"EFFECTIVE_EXIT_CODE={effective_exit_code}",
                        failure_reason,
                        "OUTPUT_BEGIN",
                        output_text,
                        "OUTPUT_END",
                    ] if item]
                )
                _append_log_block(log_path, f"{command_title}_RESULT", result_body)
                if effective_exit_code != 0 and variant_index < len(commands) and _should_try_instrument_fallback(output_text):
                    _append_log_block(
                        log_path,
                        f"{command_title}_FALLBACK",
                        "RESULT=retrying_next_variant\nREASON=openharmony_test_runner_asset_not_found",
                    )
                    continue
                if aggregate_exit_code == 0 and effective_exit_code != 0:
                    aggregate_exit_code = effective_exit_code
                break
            except subprocess.TimeoutExpired as exc:
                timeout_output = "\n".join(
                    part
                    for part in (
                        (exc.stdout or "").strip(),
                        (exc.stderr or "").strip(),
                    )
                    if part
                )
                timeout_body = "\n".join(
                    [
                        "EXIT_CODE=124",
                        f"ERROR=Timed out after {max(float(timeout_sec), 1.0):.0f} seconds while waiting for the test command.",
                        "OUTPUT_BEGIN",
                        timeout_output,
                        "OUTPUT_END",
                    ]
                )
                _append_log_block(log_path, f"{command_title}_RESULT", timeout_body)
                return 124, str(log_path)

    return aggregate_exit_code, str(log_path)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover and run HarmonyOS tests without building or installing.")
    parser.add_argument("--repo-path", required=True, help="Harmony repo path.")
    parser.add_argument(
        "--deveco-path",
        default=os.environ.get("DEVECO_PATH", "").strip(),
        help="DevEco Studio install path. Default: env DEVECO_PATH",
    )
    parser.add_argument(
        "--timeout-sec",
        type=int,
        default=int(DEFAULT_TIMEOUT_SEC),
        help=f"Maximum time to allow the test command(s) to run. Default: {int(DEFAULT_TIMEOUT_SEC)}",
    )
    parser.add_argument(
        "--discover-only",
        action="store_true",
        help="Only print discovered test targets without executing commands.",
    )
    parser.add_argument(
        "--product",
        default="default",
        help="Product name in build-profile.json5 (used to read compileSdkVersion / compatibleSdkVersion). Default: default",
    )
    parser.add_argument(
        "--class-filter",
        action="append",
        default=[],
        help="Hypium suite/class filter to pass to aa test. Can be repeated.",
    )
    return parser.parse_args()


def _print_log_tail(log_path: str, max_lines: int = 80) -> None:
    path = Path(log_path)
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    print("LOG_TAIL_BEGIN")
    for line in lines[-max_lines:]:
        print(line)
    print("LOG_TAIL_END")


def main() -> int:
    ensure_command_line_tools_env()
    args = _parse_args()
    try:
        if not args.deveco_path:
            raise ValueError("DEVECO_PATH missing (set command_line_tools_test/.env or pass --deveco-path)")
        if args.discover_only:
            repo_dir = resolve_directory(args.repo_path, "repo_path")
            deveco_dir = resolve_directory(args.deveco_path, "deveco_path")
            sdk_roots, sdk_meta = get_ordered_sdk_roots_for_repo(
                repo_dir,
                deveco_dir,
                product_name=args.product,
            )
            print_build_profile_sdk_resolution(sdk_meta)
            sdk_fail = _sdk_precheck_blocks_tests(sdk_roots, sdk_meta)
            if sdk_fail:
                print(f"SDK_PRECHECK_FAILED={sdk_fail}", file=sys.stderr)
                return 1
            for item in discover_test_targets(args.repo_path):
                print(item)
            return 0

        exit_code, log_path = run_all_tests(
            repo_path=args.repo_path,
            deveco_path=args.deveco_path,
            timeout_sec=args.timeout_sec,
            product_name=args.product,
            class_filters=tuple(args.class_filter or ()),
        )
        print("TEST_RUN_STATUS=COMPLETED")
        print(f"EXIT_CODE={exit_code}")
        print(f"LOG_PATH={format_path_for_display(log_path)}")
        if exit_code != 0:
            _print_log_tail(log_path)
        return exit_code
    except Exception as exc:
        print("TEST_RUN_STATUS=FAILED", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

