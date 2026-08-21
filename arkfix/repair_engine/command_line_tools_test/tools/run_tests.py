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


def _discover_test_package_name(legacy_config_file: Path) -> str | None:
    if not legacy_config_file.is_file():
        return None
    legacy_text = read_text_file(legacy_config_file, "legacy_ohos_test_config_file")
    return _extract_first_value(legacy_text, PACKAGE_NAME_MARKER)


def _collect_test_files(source_dir: Path) -> tuple[Path, ...]:
    if not source_dir.is_dir():
        return ()
    return tuple(sorted(path.resolve() for path in source_dir.rglob("*.test.ets") if path.is_file()))


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
        _append_log_block(
            log_path,
            "PRECHECK",
            "RESULT=no_test_targets_discovered\nNo src/ohosTest or src/test directory was found under the repo.",
        )
        return 1, str(log_path)

    if not runnable_targets:
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
            command = _build_instrument_command(
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

        command_title = f"COMMAND_{index}"
        command_intro = "\n".join(
            [
                f"TARGET={test_target.display_name(repo_dir)}",
                f"COMMAND={_format_command_for_log(command)}",
                f"CLASS_FILTER={','.join(class_filters) if class_filters else '<none>'}",
                f"TIMEOUT_SEC={int(max(float(timeout_sec), 1.0))}",
                f"AA_WAIT_TIME_MS={instrument_timeout_ms}",
                f"HYPIUM_TIMEOUT_MS={instrument_timeout_ms}",
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
            if aggregate_exit_code == 0 and effective_exit_code != 0:
                aggregate_exit_code = effective_exit_code
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
