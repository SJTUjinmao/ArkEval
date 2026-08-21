from __future__ import annotations

import _load_env  # noqa: F401 — apply command_line_tools_test/.env before any other imports
from _load_env import ensure_command_line_tools_env

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

try:
    from .common import (
        StructuredToolError,
        command_output,
        error_to_dict,
        error_to_json,
        find_command_path,
        format_command,
        format_path_for_display,
        get_ordered_sdk_roots_for_repo,
        print_build_profile_sdk_resolution,
        read_build_profile_sdk_versions,
        resolve_directory,
        resolve_existing_path,
        run_command,
        strip_ansi,
        tail_text,
        write_tool_log,
    )
    from .ensure_emulator import get_hdc_targets
except ImportError:
    from common import (  # type: ignore
        StructuredToolError,
        command_output,
        error_to_dict,
        error_to_json,
        find_command_path,
        format_command,
        format_path_for_display,
        get_ordered_sdk_roots_for_repo,
        print_build_profile_sdk_resolution,
        read_build_profile_sdk_versions,
        resolve_directory,
        resolve_existing_path,
        run_command,
        strip_ansi,
        tail_text,
        write_tool_log,
    )
    from ensure_emulator import get_hdc_targets  # type: ignore


INSTALL_TIMEOUT_SEC = 180.0
INSTALLABLE_SUFFIXES = (".hap", ".hsp")
HDC_FAILURE_MARKERS = (
    "[fail]",
    "failed to install bundle",
    "install sign info inconsistent",
    "install failed",
    "error: failed to install",
)
RELEASE_TYPE_MISMATCH_MARKERS = (
    "9568282",
    "9568283",
    "releaseType target not same",
    "releaseType compatible not same",
)
UNINSTALL_ALREADY_CLEAN_MARKERS = (
    "not exist",
    "not exists",
    "not found",
    "not installed",
    "bundle not exist",
    "bundle does not exist",
    "does not exist",
    "no such",
)


def _hdc_command() -> list[str]:
    hdc_path = find_command_path("hdc")
    if not hdc_path:
        raise StructuredToolError(
            code="hdc_not_found",
            message="Unable to find 'hdc' in PATH.",
            details={"hint": "Make sure hdc is available from the current shell environment."},
        )
    return [str(hdc_path)]


def _resolve_package_path(package_path: str | Path) -> Path:
    try:
        resolved = resolve_existing_path(package_path, "package_path")
    except FileNotFoundError as exc:
        raise StructuredToolError(
            code="package_not_found",
            message="The provided HAP/HSP package file does not exist.",
            details={"package_path": Path(package_path).expanduser()},
        ) from exc

    if not resolved.is_file():
        raise StructuredToolError(
            code="package_not_file",
            message="The provided package_path is not a file.",
            details={"package_path": resolved},
        )
    if resolved.suffix.lower() not in INSTALLABLE_SUFFIXES:
        raise StructuredToolError(
            code="invalid_package_suffix",
            message="The provided file is not a .hap or .hsp package.",
            details={"package_path": resolved},
        )
    return resolved


def _resolve_hap_path(hap_path: str | Path) -> Path:
    return _resolve_package_path(hap_path)


def _resolve_target(target: str | None) -> str:
    target = target or os.environ.get("HDC_TARGET")
    try:
        online_targets = get_hdc_targets()
    except FileNotFoundError as exc:
        raise StructuredToolError(
            code="hdc_not_found",
            message=str(exc),
            details={},
        ) from exc
    except RuntimeError as exc:
        raise StructuredToolError(
            code="hdc_query_failed",
            message="Failed to query online Harmony targets before installation.",
            details={"reason": str(exc)},
        ) from exc

    if target:
        if target not in online_targets:
            raise StructuredToolError(
                code="target_not_online",
                message="The requested target is not currently online.",
                details={
                    "requested_target": target,
                    "online_targets": online_targets,
                },
            )
        return target

    if not online_targets:
        raise StructuredToolError(
            code="no_online_target",
            message="No online Harmony target is available for installation.",
            details={"online_targets": online_targets},
        )

    if len(online_targets) > 1:
        raise StructuredToolError(
            code="multiple_online_targets",
            message="Multiple online Harmony targets were found. Pass target explicitly.",
            details={"online_targets": online_targets},
        )

    return online_targets[0]


def _looks_like_hdc_failure(output_text: str) -> bool:
    normalized = strip_ansi(output_text).lower()
    return any(marker in normalized for marker in HDC_FAILURE_MARKERS)


def _looks_like_release_type_mismatch(output_text: str) -> bool:
    normalized = strip_ansi(output_text).lower()
    return any(marker.lower() in normalized for marker in RELEASE_TYPE_MISMATCH_MARKERS)


def _looks_like_already_uninstalled(output_text: str) -> bool:
    normalized = strip_ansi(output_text).lower()
    return any(marker in normalized for marker in UNINSTALL_ALREADY_CLEAN_MARKERS)


def _extract_first_json_string(path: Path, key: str) -> str:
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"([^"]+)"', text)
    return match.group(1).strip() if match else ""


def _extract_bundle_name(repo_dir: Path | None) -> str:
    if repo_dir is None:
        return ""
    app_scope = repo_dir / "AppScope" / "app.json5"
    bundle_name = _extract_first_json_string(app_scope, "bundleName")
    if bundle_name:
        return bundle_name
    for legacy_config in sorted(repo_dir.glob("*/src/main/config.json")):
        bundle_name = _extract_first_json_string(legacy_config, "bundleName")
        if bundle_name:
            return bundle_name
    return ""


def _extract_cleanup_bundle_names(repo_dir: Path | None) -> list[str]:
    if repo_dir is None:
        return []

    names: list[str] = []

    def add(value: str) -> None:
        value = value.strip()
        if value and value not in names:
            names.append(value)

    add(_extract_bundle_name(repo_dir))
    for legacy_ohos_test_config in sorted(repo_dir.rglob("src/ohosTest/config.json")):
        if any(part in {"build", ".test", ".hvigor", "node_modules"} for part in legacy_ohos_test_config.parts):
            continue
        add(_extract_first_json_string(legacy_ohos_test_config, "package"))
    return names


def _uninstall_bundle_from_target(bundle_name: str, resolved_target: str) -> str:
    command = [*_hdc_command(), "-t", resolved_target, "uninstall", bundle_name]
    try:
        result = run_command(command, cwd=Path.cwd(), timeout_sec=60.0)
    except subprocess.TimeoutExpired as exc:
        raise StructuredToolError(
            code="hdc_uninstall_timeout",
            message="Timed out while uninstalling stale bundle before retrying install.",
            details={
                "bundle_name": bundle_name,
                "target": resolved_target,
                "command": format_command(command),
            },
        ) from exc
    output_text = command_output(result)
    if result.returncode != 0:
        raise StructuredToolError(
            code="hdc_uninstall_failed",
            message="Failed to uninstall stale bundle before retrying install.",
            details={
                "bundle_name": bundle_name,
                "target": resolved_target,
                "command": format_command(command),
                "exit_code": result.returncode,
                "output_tail": tail_text(output_text),
            },
        )
    return output_text


def _cleanup_bundles_before_install(repo_dir: Path | None, resolved_target: str, log_lines: list[str]) -> None:
    bundle_names = _extract_cleanup_bundle_names(repo_dir)
    if not bundle_names:
        print("PREINSTALL_CLEANUP_STATUS=SKIPPED")
        print("PREINSTALL_CLEANUP_REASON=no_bundle_name_found")
        log_lines.append("PREINSTALL_CLEANUP_STATUS=SKIPPED")
        log_lines.append("PREINSTALL_CLEANUP_REASON=no_bundle_name_found")
        return

    print("PREINSTALL_CLEANUP_STATUS=START")
    print(f"PREINSTALL_CLEANUP_TARGET={resolved_target}")
    print(f"PREINSTALL_CLEANUP_BUNDLES={','.join(bundle_names)}")
    log_lines.append("PREINSTALL_CLEANUP_STATUS=START")
    log_lines.append(f"PREINSTALL_CLEANUP_TARGET={resolved_target}")
    log_lines.append(f"PREINSTALL_CLEANUP_BUNDLES={','.join(bundle_names)}")

    for bundle_name in bundle_names:
        command = [*_hdc_command(), "-t", resolved_target, "uninstall", bundle_name]
        try:
            result = run_command(command, cwd=Path.cwd(), timeout_sec=60.0)
            output_text = command_output(result)
            output_tail = tail_text(output_text, limit=8)
            if result.returncode == 0:
                status = "SUCCESS"
            elif _looks_like_already_uninstalled(output_text):
                status = "ALREADY_CLEAN"
            else:
                status = f"IGNORED_FAILURE_EXIT_{result.returncode}"
        except subprocess.TimeoutExpired:
            output_tail = "Timed out while uninstalling stale bundle before install."
            status = "IGNORED_TIMEOUT"
        except Exception as exc:
            output_tail = str(exc)
            status = "IGNORED_ERROR"
        print(f"PREINSTALL_CLEANUP_BUNDLE={bundle_name}")
        print(f"PREINSTALL_CLEANUP_RESULT={status}")
        if output_tail:
            print(f"PREINSTALL_CLEANUP_OUTPUT={output_tail}")
        log_lines.append(f"PREINSTALL_CLEANUP_BUNDLE={bundle_name}")
        log_lines.append(f"PREINSTALL_CLEANUP_RESULT={status}")
        if output_tail:
            log_lines.append(f"PREINSTALL_CLEANUP_OUTPUT={output_tail}")

    print("PREINSTALL_CLEANUP_STATUS=DONE")
    log_lines.append("PREINSTALL_CLEANUP_STATUS=DONE")


def _install_package_to_target(resolved_package: Path, resolved_target: str) -> None:
    command = [*_hdc_command(), "-t", resolved_target, "install", "-r", str(resolved_package)]
    try:
        result = run_command(command, cwd=resolved_package.parent, timeout_sec=INSTALL_TIMEOUT_SEC)
    except subprocess.TimeoutExpired as exc:
        raise StructuredToolError(
            code="hdc_install_timeout",
            message="Timed out while waiting for hdc install to finish.",
            details={
                "package_path": resolved_package,
                "target": resolved_target,
                "command": format_command(command),
                "timeout_sec": INSTALL_TIMEOUT_SEC,
            },
        ) from exc

    output_text = command_output(result)

    if result.returncode != 0 or _looks_like_hdc_failure(output_text):
        raise StructuredToolError(
            code="hdc_install_failed",
            message="Failed to install the HAP/HSP package on the target.",
            details={
                "package_path": resolved_package,
                "target": resolved_target,
                "command": format_command(command),
                "exit_code": result.returncode,
                "output_tail": tail_text(output_text),
            },
        )


def _install_packages_to_target(resolved_packages: list[Path], resolved_target: str) -> None:
    command = [*_hdc_command(), "-t", resolved_target, "install", "-r", *[str(package) for package in resolved_packages]]
    try:
        result = run_command(command, cwd=resolved_packages[0].parent, timeout_sec=INSTALL_TIMEOUT_SEC)
    except subprocess.TimeoutExpired as exc:
        raise StructuredToolError(
            code="hdc_install_timeout",
            message="Timed out while waiting for hdc install to finish.",
            details={
                "package_paths": resolved_packages,
                "target": resolved_target,
                "command": format_command(command),
                "timeout_sec": INSTALL_TIMEOUT_SEC,
            },
        ) from exc

    output_text = command_output(result)

    if result.returncode != 0 or _looks_like_hdc_failure(output_text):
        raise StructuredToolError(
            code="hdc_install_failed",
            message="Failed to install the HAP/HSP package set on the target.",
            details={
                "package_paths": resolved_packages,
                "target": resolved_target,
                "command": format_command(command),
                "exit_code": result.returncode,
                "output_tail": tail_text(output_text),
            },
        )


def _install_resolved_packages(resolved_packages: list[Path], resolved_target: str) -> None:
    if len(resolved_packages) > 1:
        _install_packages_to_target(resolved_packages, resolved_target)
        return
    for package in resolved_packages:
        _install_package_to_target(package, resolved_target)


def _install_resolved_packages_with_retry(
    resolved_packages: list[Path],
    resolved_target: str,
    repo_dir: Path | None,
    log_lines: list[str],
) -> None:
    try:
        _install_resolved_packages(resolved_packages, resolved_target)
        return
    except StructuredToolError as exc:
        output_tail = str(exc.details.get("output_tail", ""))
        if exc.code != "hdc_install_failed" or not _looks_like_release_type_mismatch(output_tail):
            raise
        bundle_name = _extract_bundle_name(repo_dir)
        if not bundle_name:
            raise
        print("INSTALL_RETRY_REASON=9568282_releaseType_target_not_same")
        print(f"INSTALL_RETRY_UNINSTALL_BUNDLE={bundle_name}")
        log_lines.append("INSTALL_RETRY_REASON=9568282_releaseType_target_not_same")
        log_lines.append(f"INSTALL_RETRY_UNINSTALL_BUNDLE={bundle_name}")
        uninstall_output = _uninstall_bundle_from_target(bundle_name, resolved_target)
        uninstall_tail = tail_text(uninstall_output, limit=8)
        print(f"INSTALL_RETRY_UNINSTALL_OUTPUT={uninstall_tail}")
        log_lines.append(f"INSTALL_RETRY_UNINSTALL_OUTPUT={uninstall_tail}")
        _install_resolved_packages(resolved_packages, resolved_target)


def _is_stage_process_thread_project(repo_dir: Path | None) -> bool:
    if repo_dir is None:
        return False
    normalized = repo_dir.as_posix().replace("\\", "/")
    return normalized.endswith("/code/DocsSample/ApplicationModels/StageProcessThread")


def _is_system_signed_validation_project(repo_dir: Path | None) -> bool:
    if repo_dir is None:
        return False
    normalized = repo_dir.as_posix().replace("\\", "/")
    return (
        normalized.endswith("/code/DocsSample/ApplicationModels/StageProcessThread")
        or normalized.endswith("/code/BasicFeature/Telephony/Call")
        or normalized.endswith("/code/SystemFeature/Media/Screenshot")
        or normalized.endswith("/code/SystemFeature/Media/ScreenRecorder")
        or normalized.endswith("/code/BasicFeature/TaskManagement/WorkScheduler")
        or normalized.endswith("/ability/FormLauncher")
        or normalized.endswith("/CompleteApps/KikaInput")
    )


def _find_app_samples_root(repo_dir: Path) -> Path | None:
    for parent in [repo_dir, *repo_dir.parents]:
        if parent.name == "applications_app_samples":
            return parent
    return None


def _stage_process_thread_sign_tool_dir(repo_dir: Path) -> Path | None:
    app_samples_root = _find_app_samples_root(repo_dir)
    if app_samples_root is None:
        return None
    pool_candidates: list[Path] = []
    for parent in [repo_dir, *repo_dir.parents]:
        if parent.name != "arkeval":
            continue
        repair_pool = parent / "depend" / "repair_repo"
        for run_dir in sorted(repair_pool.glob("run*")):
            pool_root = run_dir / "applications_app_samples"
            pool_candidates.extend(
                [
                    pool_root / "code" / "Project" / "HapBuild" / "compile-tool" / "tool" / "sign_tool",
                    pool_root / "code" / "BasicFeature" / "TaskManagement" / "WorkScheduler" / "signTool",
                ]
            )
        break
    candidates = [
        app_samples_root / "code" / "Project" / "HapBuild" / "compile-tool" / "tool" / "sign_tool",
        app_samples_root / "code" / "SystemFeature" / "TaskManagement" / "WorkScheduler" / "signTool",
        app_samples_root / "code" / "BasicFeature" / "TaskManagement" / "WorkScheduler" / "signTool",
        *pool_candidates,
    ]
    for sign_tool_dir in candidates:
        jar_dir = sign_tool_dir
        if sign_tool_dir.name == "sign_tool":
            jar_dir = sign_tool_dir.parent
        required = [
            jar_dir / "hap-sign-tool.jar",
            sign_tool_dir / "OpenHarmony.p12",
            sign_tool_dir / "OpenHarmonyApplication.pem",
            sign_tool_dir / "OpenHarmonyProfileRelease.pem",
        ]
        if all(path.is_file() for path in required):
            return sign_tool_dir
    return None


def _run_sign_tool(command: list[str], cwd: Path) -> str:
    result = run_command(command, cwd=cwd, timeout_sec=120.0)
    output_text = command_output(result)
    if result.returncode != 0:
        raise StructuredToolError(
            code="stage_process_thread_signing_failed",
            message="Failed to run hap-sign-tool for StageProcessThread system signing.",
            details={
                "command": format_command(command),
                "exit_code": result.returncode,
                "output_tail": tail_text(output_text),
            },
        )
    return output_text


def _make_system_core_profile_json(
    profile_path: Path,
    *,
    bundle_name: str,
    restricted_permissions: list[str] | None = None,
) -> None:
    distribution_certificate = (
        "-----BEGIN CERTIFICATE-----\n"
        "MIICMzCCAbegAwIBAgIEaOC/zDAMBggqhkjOPQQDAwUAMGMxCzAJBgNVBAYTAkNO\n"
        "MRQwEgYDVQQKEwtPcGVuSGFybW9ueTEZMBcGA1UECxMQT3Blbkhhcm1vbnkgVGVh\n"
        "bTEjMCEGA1UEAxMaT3Blbkhhcm1vbnkgQXBwbGljYXRpb24gQ0EwHhcNMjEwMjAy\n"
        "MTIxOTMxWhcNNDkxMjMxMTIxOTMxWjBoMQswCQYDVQQGEwJDTjEUMBIGA1UEChML\n"
        "T3Blbkhhcm1vbnkxGTAXBgNVBAsTEE9wZW5IYXJtb255IFRlYW0xKDAmBgNVBAMT\n"
        "H09wZW5IYXJtb255IEFwcGxpY2F0aW9uIFJlbGVhc2UwWTATBgcqhkjOPQIBBggq\n"
        "hkjOPQMBBwNCAATbYOCQQpW5fdkYHN45v0X3AHax12jPBdEDosFRIZ1eXmxOYzSG\n"
        "JwMfsHhUU90E8lI0TXYZnNmgM1sovubeQqATo1IwUDAfBgNVHSMEGDAWgBTbhrci\n"
        "FtULoUu33SV7ufEFfaItRzAOBgNVHQ8BAf8EBAMCB4AwHQYDVR0OBBYEFPtxruhl\n"
        "cRBQsJdwcZqLu9oNUVgaMAwGCCqGSM49BAMDBQADaAAwZQIxAJta0PQ2p4DIu/ps\n"
        "LMdLCDgQ5UH1l0B4PGhBlMgdi2zf8nk9spazEQI/0XNwpft8QAIwHSuA2WelVi/o\n"
        "zAlF08DnbJrOOtOnQq5wHOPlDYB4OtUzOYJk9scotrEnJxJzGsh/\n"
        "-----END CERTIFICATE-----\n"
    )
    profile = {
        "version-name": "2.0.0",
        "version-code": 2,
        "app-distribution-type": "os_integration",
        "uuid": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{bundle_name}.system_core")),
        "validity": {"not-before": 1594865258, "not-after": 2524579200},
        "type": "release",
        "bundle-info": {
            "developer-id": "OpenHarmony",
            "distribution-certificate": distribution_certificate,
            "bundle-name": bundle_name,
            "apl": "system_core",
            "app-feature": "hos_system_app",
        },
        "acls": {"allowed-acls": [""]},
        "permissions": {"restricted-permissions": restricted_permissions or []},
        "issuer": "pki_internal",
    }
    profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _make_stage_process_thread_profile_json(profile_path: Path) -> None:
    _make_system_core_profile_json(profile_path, bundle_name="com.samples.stageprocessthread")


def _system_signed_bundle_name(repo_dir: Path) -> str:
    bundle_name = _extract_bundle_name(repo_dir)
    if bundle_name:
        return bundle_name
    if _is_stage_process_thread_project(repo_dir):
        return "com.samples.stageprocessthread"
    return ""


def _system_signed_restricted_permissions(repo_dir: Path) -> list[str]:
    normalized = repo_dir.as_posix().replace("\\", "/")
    if normalized.endswith("/ability/FormLauncher"):
        return [
            "ohos.permission.GET_BUNDLE_INFO_PRIVILEGED",
            "ohos.permission.REQUIRE_FORM",
        ]
    if normalized.endswith("/code/BasicFeature/Telephony/Call"):
        return ["ohos.permission.PLACE_CALL"]
    if normalized.endswith("/code/SystemFeature/Media/Screenshot"):
        return [
            "ohos.permission.CAPTURE_SCREEN",
            "ohos.permission.PRIVACY_WINDOW",
        ]
    if normalized.endswith("/code/SystemFeature/Media/ScreenRecorder"):
        return [
            "ohos.permission.CAPTURE_SCREEN",
            "ohos.permission.SYSTEM_FLOAT_WINDOW",
        ]
    if normalized.endswith("/code/BasicFeature/TaskManagement/WorkScheduler"):
        return [
            "ohos.permission.INSTALL_BUNDLE",
            "ohos.permission.NOTIFICATION_CONTROLLER",
        ]
    return []


def _prepare_system_signed_validation_packages(
    resolved_packages: list[Path],
    repo_dir: Path | None,
    log_lines: list[str],
) -> list[Path]:
    if not _is_system_signed_validation_project(repo_dir):
        return resolved_packages
    assert repo_dir is not None
    sign_tool_dir = _stage_process_thread_sign_tool_dir(repo_dir)
    if sign_tool_dir is None:
        log_lines.append("ENV_PREPARE_SYSTEM_APP_SIGNING=missing_sign_tool")
        print("ENV_PREPARE_SYSTEM_APP_SIGNING=missing_sign_tool")
        return resolved_packages
    bundle_name = _system_signed_bundle_name(repo_dir)
    if not bundle_name:
        log_lines.append("ENV_PREPARE_SYSTEM_APP_SIGNING=missing_bundle_name")
        print("ENV_PREPARE_SYSTEM_APP_SIGNING=missing_bundle_name")
        return resolved_packages

    output_dir = repo_dir / "entry" / "build" / "default" / "outputs" / "systemSigned"
    output_dir.mkdir(parents=True, exist_ok=True)
    unsigned_profile = output_dir / "system_profile_unsigned.json"
    signed_profile = output_dir / "system_profile.p7b"
    _make_system_core_profile_json(
        unsigned_profile,
        bundle_name=bundle_name,
        restricted_permissions=_system_signed_restricted_permissions(repo_dir),
    )

    jar_dir = sign_tool_dir.parent if sign_tool_dir.name == "sign_tool" else sign_tool_dir
    java_command = ["java", "-jar", str(jar_dir / "hap-sign-tool.jar")]
    _run_sign_tool(
        [
            *java_command,
            "sign-profile",
            "-mode",
            "localSign",
            "-keyAlias",
            "openharmony application profile release",
            "-keyPwd",
            "123456",
            "-profileCertFile",
            str(sign_tool_dir / "OpenHarmonyProfileRelease.pem"),
            "-inFile",
            str(unsigned_profile),
            "-signAlg",
            "SHA256withECDSA",
            "-keystoreFile",
            str(sign_tool_dir / "OpenHarmony.p12"),
            "-keystorePwd",
            "123456",
            "-outFile",
            str(signed_profile),
        ],
        cwd=output_dir,
    )

    signed_packages: list[Path] = []
    for package in resolved_packages:
        if package.suffix.lower() != ".hap" or "entry-" not in package.name:
            signed_packages.append(package)
            continue
        signed_package = output_dir / package.name.replace("-unsigned.hap", "-system-signed.hap")
        if signed_package == package:
            signed_package = output_dir / f"{package.stem}-system-signed.hap"
        sign_command = [
            *java_command,
            "sign-app",
            "-keyAlias",
            "openharmony application release",
            "-signAlg",
            "SHA256withECDSA",
            "-mode",
            "localSign",
            "-appCertFile",
            str(sign_tool_dir / "OpenHarmonyApplication.pem"),
            "-profileFile",
            str(signed_profile),
            "-inFile",
            str(package),
            "-keystoreFile",
            str(sign_tool_dir / "OpenHarmony.p12"),
            "-outFile",
            str(signed_package),
            "-keyPwd",
            "123456",
            "-keystorePwd",
            "123456",
        ]
        output_text = _run_sign_tool(sign_command, cwd=output_dir)
        if "Not support command param:-compatibleVersion" not in output_text:
            pass
        signed_packages.append(signed_package)
        log_lines.append(
            "ENV_PREPARE_SYSTEM_APP_SIGNING="
            f"{format_path_for_display(package, start=repo_dir)}->{format_path_for_display(signed_package, start=repo_dir)}"
        )
        print(
            "ENV_PREPARE_SYSTEM_APP_SIGNING="
            f"{format_path_for_display(package, start=repo_dir)}->{format_path_for_display(signed_package, start=repo_dir)}"
        )
    return signed_packages


def _prepare_stage_process_thread_system_signed_packages(
    resolved_packages: list[Path],
    repo_dir: Path | None,
    log_lines: list[str],
) -> list[Path]:
    return _prepare_system_signed_validation_packages(resolved_packages, repo_dir, log_lines)


def _install_hap_to_target(resolved_hap: Path, resolved_target: str) -> None:
    _install_package_to_target(resolved_hap, resolved_target)


def install_hap(hap_path: str | Path, target: str | None = None) -> None:
    resolved_hap = _resolve_hap_path(hap_path)
    resolved_target = _resolve_target(target)
    _install_hap_to_target(resolved_hap, resolved_target)


def install_packages(package_paths: list[str | Path], target: str | None = None) -> list[Path]:
    if not package_paths:
        raise StructuredToolError(
            code="no_package_paths",
            message="No HAP/HSP package path was provided for installation.",
            details={},
        )
    resolved_packages = [_resolve_package_path(path) for path in package_paths]
    resolved_target = _resolve_target(target)
    if len(resolved_packages) > 1:
        _install_packages_to_target(resolved_packages, resolved_target)
        return resolved_packages
    for package in resolved_packages:
        _install_package_to_target(package, resolved_target)
    return resolved_packages


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install HarmonyOS HAP/HSP packages onto an online target via hdc.")
    parser.add_argument("--hap-path", help="Backward-compatible single .hap file path to install.")
    parser.add_argument(
        "--package-path",
        action="append",
        default=[],
        help="HAP/HSP package path to install. Repeat to install multiple packages in order.",
    )
    parser.add_argument(
        "--target",
        help="Optional target connect key. If omitted, the script uses the only online target.",
    )
    parser.add_argument(
        "--repo-path",
        help="Optional Harmony project root. With --deveco-path, prints build-profile SDK lines before install.",
    )
    parser.add_argument(
        "--deveco-path",
        help="Optional DevEco Studio install path. Used together with --repo-path.",
    )
    return parser.parse_args()


def main() -> int:
    ensure_command_line_tools_env()
    args = _parse_args()
    repo_dir: Path | None = None
    log_lines: list[str] = [
        "TOOL=install_app.py",
        f"ARGV={' '.join(sys.argv[1:])}",
        f"HAP_PATH={args.hap_path or ''}",
        f"PACKAGE_PATHS={args.package_path}",
        f"TARGET_ARG={args.target or ''}",
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

        package_paths = list(args.package_path or [])
        if args.hap_path:
            package_paths.append(args.hap_path)
        resolved_packages = [_resolve_package_path(path) for path in package_paths]
        if not resolved_packages:
            raise StructuredToolError(
                code="no_package_paths",
                message="No --hap-path or --package-path was provided.",
                details={},
            )
        resolved_packages = _prepare_stage_process_thread_system_signed_packages(resolved_packages, repo_dir, log_lines)
        resolved_target = _resolve_target(args.target)
        _cleanup_bundles_before_install(repo_dir, resolved_target, log_lines)
        _install_resolved_packages_with_retry(resolved_packages, resolved_target, repo_dir, log_lines)
        for index, resolved_package in enumerate(resolved_packages, start=1):
            print(f"PACKAGE_INSTALLED_{index}={format_path_for_display(resolved_package)}")
            log_lines.append(f"PACKAGE_INSTALLED_{index}={resolved_package}")
        print("INSTALL_STATUS=SUCCESS")
        print(f"TARGET={resolved_target}")
        print(f"PACKAGE_COUNT={len(resolved_packages)}")
        if len(resolved_packages) == 1 and resolved_packages[0].suffix.lower() == ".hap":
            print(f"HAP_PATH={format_path_for_display(resolved_packages[0])}")
        log_lines.append("INSTALL_STATUS=SUCCESS")
        log_lines.append(f"TARGET={resolved_target}")
        log_lines.append(f"PACKAGE_COUNT={len(resolved_packages)}")
        log_path = write_tool_log("install_app", "\n".join(log_lines))
        print(f"LOG_PATH={format_path_for_display(log_path)}")
        return 0
    except Exception as exc:
        error = error_to_dict(exc)
        log_lines.append("INSTALL_STATUS=FAILED")
        log_lines.append(f"ERROR_CODE={error['code']}")
        log_lines.append(f"ERROR_MESSAGE={error['message']}")
        log_lines.append(f"ERROR_JSON={error_to_json(exc)}")
        log_path = write_tool_log("install_app", "\n".join(log_lines))
        print(f"LOG_PATH={format_path_for_display(log_path)}", file=sys.stderr)
        print("INSTALL_STATUS=FAILED", file=sys.stderr)
        print(f"ERROR_CODE={error['code']}", file=sys.stderr)
        print(f"ERROR_MESSAGE={error['message']}", file=sys.stderr)
        print(f"ERROR_JSON={error_to_json(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
