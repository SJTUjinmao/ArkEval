from __future__ import annotations

import _load_env  # noqa: F401
from _load_env import ensure_command_line_tools_env

import argparse
import gzip
import json
import os
import re
import sys
import tarfile
import time
import zipfile
import io
from pathlib import Path
from typing import Any

try:
    from .common import (
        build_harmony_command_env,
        command_output,
        ensure_local_properties,
        find_hvigor_wrapper,
        find_java_home,
        find_node_home,
        format_command,
        format_path_for_display,
        parse_json5_text,
        prepare_native_repair_environment,
        print_build_profile_sdk_resolution,
        require_sdk_roots_for_repo,
        resolve_sdk_api_slice_for_api,
        resolve_directory,
        run_command,
        run_ohpm_install,
        tail_text,
        write_tool_log,
    )
except ImportError:
    from common import (  # type: ignore
        build_harmony_command_env,
        command_output,
        ensure_local_properties,
        find_hvigor_wrapper,
        find_java_home,
        find_node_home,
        format_command,
        format_path_for_display,
        parse_json5_text,
        prepare_native_repair_environment,
        print_build_profile_sdk_resolution,
        require_sdk_roots_for_repo,
        resolve_sdk_api_slice_for_api,
        resolve_directory,
        run_command,
        run_ohpm_install,
        tail_text,
        write_tool_log,
    )


DEFAULT_TASK = "assembleHap"
DEFAULT_MODE = "module"
DEFAULT_MODULE = "entry"
DEFAULT_PRODUCT = "default"
DEFAULT_TARGET: str | None = None
SDK_RETRY_MARKERS = (
    "SDK component missing",
    "Invalid value of 'DEVECO_SDK_HOME'",
    "00303168",
    "00303217",
)


def _resolve_agent_repo_path_arg(repo_path: str) -> str:
    raw = (repo_path or "").strip()
    if not raw:
        return raw
    if Path(raw).is_dir():
        return raw

    normalized = raw.replace("\\", "/").lstrip("./")
    project_path = os.environ.get("MSWE_PROJECT_PATH", "").strip().replace("\\", "/").lstrip("./")
    if project_path and project_path != ".":
        if normalized == project_path and Path(".").is_dir():
            return "."
        prefix = project_path.rstrip("/") + "/"
        if normalized.startswith(prefix):
            stripped = normalized[len(prefix) :]
            if Path(stripped or ".").is_dir():
                return stripped or "."

    native_root = os.environ.get("MSWE_NATIVE_REPO_ROOT", "").strip()
    if native_root:
        rooted = Path(native_root) / normalized
        if rooted.is_dir():
            return str(rooted)
    return raw


def _default_build_command(task: str, mode: str, module: str, product: str, target: str | None) -> list[str]:
    command = ["--debug", "--no-daemon", "--mode", mode, "-p", f"module={module}", "-p", f"product={product}"]
    if target:
        command.extend(["-p", f"target={target}"])
    command.append(task)
    return command


def _ensure_project_ready(repo_dir: Path) -> None:
    build_profile = repo_dir / "build-profile.json5"
    if build_profile.exists():
        return

    backup_profile = repo_dir / "build-profile.json5.bak"
    if backup_profile.exists():
        raise FileNotFoundError(
            "Missing required project file: "
            f"{build_profile}. A backup file exists at {backup_profile}, "
            "but this step does not rename or modify project build-profile files."
        )

    raise FileNotFoundError(f"Missing required project file: {build_profile}")


def _should_retry_with_next_sdk(build_output: str) -> bool:
    normalized_output = build_output or ""
    return any(marker in normalized_output for marker in SDK_RETRY_MARKERS)


def _hvigor_output_reports_failure(build_output: str) -> bool:
    normalized_output = build_output or ""
    failure_markers = (
        "BUILD FAILED",
        "COMPILE RESULT:FAIL",
        "COMPILE RESULT: FAIL",
    )
    return any(marker in normalized_output for marker in failure_markers)


def _format_failure(
    result_exit_code: int,
    command: list[str],
    sdk_root: Path | None,
    node_home: Path | None,
    output_text: str,
    *,
    sdk_meta: dict | None = None,
    java_home: Path | None = None,
) -> str:
    detail = [
        f"Build failed with exit code {result_exit_code}.",
        f"Command: {format_command(command)}",
    ]
    if sdk_root:
        detail.append(f"DEVECO_SDK_HOME={sdk_root}")
        detail.append(f"OHOS_BASE_SDK_HOME={sdk_root}")
    if node_home:
        detail.append(f"NODE_HOME={node_home}")
    if java_home:
        detail.append(f"JAVA_HOME={java_home}")
    if sdk_meta:
        detail.append(
            "Build profile SDK: "
            f"compile={sdk_meta.get('compileSdkVersion')}, "
            f"compatible={sdk_meta.get('compatibleSdkVersion')}, "
            f"selection_api={sdk_meta.get('sdk_selection_api_level')}"
        )
    error_markers = (
        "ERROR",
        "ArkTS:",
        "Module parse failed",
        "COMPILE RESULT",
        "BUILD FAILED",
        "BUILDERROR",
        "Cannot ",
        "Can not ",
    )
    output_lines = output_text.splitlines()
    interesting_indexes = {
        index
        for index, line in enumerate(output_lines)
        if not line.lstrip().startswith("<w>")
        and "webpack.cache.PackFileCacheStrategy" not in line
        and any(marker.lower() in line.lower() for marker in error_markers)
    }
    expanded_indexes = set()
    for index in interesting_indexes:
        expanded_indexes.update(range(index, min(len(output_lines), index + 3)))
    interesting_lines = [
        output_lines[index]
        for index in sorted(expanded_indexes)
        if not output_lines[index].lstrip().startswith("<w>")
        and "webpack.cache.PackFileCacheStrategy" not in output_lines[index]
    ]
    interesting_tail = tail_text("\n".join(interesting_lines), limit=1000)
    if interesting_tail:
        detail.append("Build error lines:")
        detail.append(interesting_tail)
    linter_paths = sorted(
        {
            Path(match.group(0).strip().rstrip(".,;)'\""))
            for match in re.finditer(r"[A-Za-z]:[^\s]+ArkTSLinter_output\.json", output_text)
        }
    )
    for linter_path in linter_paths[:3]:
        if not linter_path.is_file():
            continue
        linter_tail = tail_text(linter_path.read_text(encoding="utf-8", errors="replace"), limit=1000)
        if linter_tail:
            detail.append(f"ArkTS linter output: {linter_path}")
            detail.append(linter_tail)
    output_tail = tail_text(output_text, limit=1000)
    if output_tail:
        detail.append("Build output:")
        detail.append(output_tail)
    return "\n".join(detail)


def build_project(repo_path: str, deveco_path: str) -> str:
    return build_project_with_options(
        repo_path=repo_path,
        deveco_path=deveco_path,
        task=DEFAULT_TASK,
        mode=DEFAULT_MODE,
        module=DEFAULT_MODULE,
        product=DEFAULT_PRODUCT,
        target=DEFAULT_TARGET,
    )


def _run_build_with_sdk_roots(
    repo_dir: Path,
    deveco_dir: Path,
    *,
    task: str,
    mode: str,
    module: str,
    product: str,
    target: str | None,
    sdk_roots: list[Path],
    sdk_meta: dict,
) -> str:
    output_text = _run_hvigor_task_with_sdk_roots(
        repo_dir,
        deveco_dir,
        task=task,
        mode=mode,
        module=module,
        product=product,
        target=target,
        sdk_roots=sdk_roots,
        sdk_meta=sdk_meta,
    )
    return _hap_path_after_success(repo_dir, output_text)


def _run_hvigor_task_with_sdk_roots(
    repo_dir: Path,
    deveco_dir: Path,
    *,
    task: str,
    mode: str,
    module: str,
    product: str,
    target: str | None,
    sdk_roots: list[Path],
    sdk_meta: dict,
) -> str:
    hvigor_path = find_hvigor_wrapper(repo_dir, deveco_dir)
    node_home = find_node_home(deveco_dir)
    java_home = find_java_home(deveco_dir)
    command = [str(hvigor_path), *_default_build_command(task, mode, module, product, target)]
    sdk_candidates: list[Path | None] = sdk_roots or [None]
    failures: list[str] = []
    selected_api_level = sdk_meta.get("sdk_selection_api_level")
    openssl_legacy_provider = True
    hvigor_config = repo_dir / "hvigor" / "hvigor-config.json5"
    if hvigor_config.is_file():
        hvigor_config_text = hvigor_config.read_text(encoding="utf-8", errors="replace")
        if (
            re.search(r'"hvigorVersion"\s*:\s*"[45]\.', hvigor_config_text)
            or re.search(r'"modelVersion"\s*:\s*"5\.', hvigor_config_text)
        ):
            openssl_legacy_provider = False

    for sdk_root in sdk_candidates:
        env = build_harmony_command_env(
            sdk_root=sdk_root,
            sdk_api_level=selected_api_level,
            deveco_path=deveco_dir,
            base_env=os.environ.copy(),
            openssl_legacy_provider=openssl_legacy_provider,
        )
        result = run_command(command, cwd=repo_dir, env=env)
        output_text = command_output(result)
        if (
            result.returncode != 0
            and "EPERM: operation not permitted" in output_text
            and re.search(r"\.hvigor[\\/]project_caches[\\/]", output_text, re.IGNORECASE)
        ):
            time.sleep(1.0)
            result = run_command(command, cwd=repo_dir, env=env)
            output_text = command_output(result)
        if result.returncode == 0 and not _hvigor_output_reports_failure(output_text):
            return output_text

        failure_message = _format_failure(
            result.returncode,
            command,
            sdk_root,
            node_home,
            output_text,
            sdk_meta=sdk_meta,
            java_home=java_home,
        )
        failures.append(failure_message)
        if _should_retry_with_next_sdk(output_text):
            continue
        raise RuntimeError(failure_message)

    if len(failures) == 1:
        raise RuntimeError(failures[0])

    attempted_roots = [str(root) for root in sdk_roots]
    combined = [
        "Build failed after trying multiple SDK roots.",
        f"Attempted SDK roots: {attempted_roots}",
        failures[-1],
    ]
    raise RuntimeError("\n".join(combined))


def build_project_with_options(
    repo_path: str,
    deveco_path: str,
    task: str = DEFAULT_TASK,
    mode: str = DEFAULT_MODE,
    module: str = DEFAULT_MODULE,
    product: str = DEFAULT_PRODUCT,
    target: str | None = DEFAULT_TARGET,
    *,
    sdk_roots: list[Path] | None = None,
    sdk_meta: dict | None = None,
) -> str:
    repo_dir = resolve_directory(repo_path, "repo_path")
    deveco_dir = resolve_directory(deveco_path, "deveco_path")
    module = _resolve_module_name_case(repo_dir, module)
    _ensure_project_ready(repo_dir)
    if sdk_roots is None or sdk_meta is None:
        sdk_roots, sdk_meta = require_sdk_roots_for_repo(
            repo_dir,
            deveco_dir,
            product_name=product,
        )
    return _run_build_with_sdk_roots(
        repo_dir,
        deveco_dir,
        task=task,
        mode=mode,
        module=module,
        product=product,
        target=target,
        sdk_roots=sdk_roots,
        sdk_meta=sdk_meta,
    )


def _read_json5_file(path: Path) -> Any:
    return parse_json5_text(path.read_text(encoding="utf-8", errors="replace"))


def _read_module_type(module_dir: Path) -> str:
    module_json = module_dir / "src" / "main" / "module.json5"
    legacy_config = module_dir / "src" / "main" / "config.json"
    if module_json.is_file():
        raw = module_json.read_text(encoding="utf-8", errors="replace")
        module_type_key = "type"
    elif legacy_config.is_file():
        raw = legacy_config.read_text(encoding="utf-8", errors="replace")
        module_type_key = "moduleType"
    else:
        return ""
    try:
        module_data = parse_json5_text(raw)
        module_section = module_data.get("module") if isinstance(module_data, dict) else {}
        if isinstance(module_section, dict):
            if module_type_key == "moduleType":
                distro = module_section.get("distro")
                if isinstance(distro, dict):
                    return str(distro.get("moduleType") or "").strip()
            return str(module_section.get(module_type_key) or "").strip()
    except Exception:
        pass
    match = __import__("re").search(rf'["\']{module_type_key}["\']\s*:\s*["\']([^"\']+)["\']', raw)
    return match.group(1).strip() if match else ""


def _project_modules(repo_dir: Path) -> list[dict[str, Any]]:
    profile = _read_json5_file(repo_dir / "build-profile.json5")
    raw_modules = profile.get("modules") if isinstance(profile, dict) else []
    modules: list[dict[str, Any]] = []
    if not isinstance(raw_modules, list):
        return modules
    for item in raw_modules:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        src_path = str(item.get("srcPath") or "").strip()
        if not name or not src_path:
            continue
        module_dir = (repo_dir / src_path).resolve()
        module_type = _read_module_type(module_dir)
        modules.append(
            {
                "name": name,
                "srcPath": src_path,
                "dir": module_dir,
                "type": module_type,
                "has_ohos_test": (module_dir / "src" / "ohosTest").is_dir(),
            }
        )
    return modules


def _resolve_module_name_case(repo_dir: Path, module: str) -> str:
    """Map case-only module typos to the exact build-profile module name."""
    requested = str(module or "").strip()
    if not requested:
        return requested

    suffix = ""
    base_name = requested
    if requested.endswith("@ohosTest"):
        suffix = "@ohosTest"
        base_name = requested[: -len(suffix)]

    module_names = [str(item.get("name") or "").strip() for item in _project_modules(repo_dir)]
    module_names = [name for name in module_names if name]
    if base_name in module_names:
        return requested

    matches = [name for name in module_names if name.casefold() == base_name.casefold()]
    if len(matches) == 1:
        return matches[0] + suffix

    return requested


def _ohos_test_build_plan(module_info: dict[str, Any]) -> dict[str, str]:
    name = str(module_info["name"])
    module_type = str(module_info.get("type") or "").strip()
    if module_type == "entry":
        return {
            "module": f"{name}@ohosTest",
            "task": DEFAULT_TASK,
            "target": "",
            "artifact_hint": "ohosTest",
        }
    return {
        "module": name,
        "task": "genOnDeviceTestHap",
        "target": "ohosTest",
        "artifact_hint": "default",
    }


def _module_declares_data_proxy(module_info: dict[str, Any]) -> bool:
    module_dir = Path(module_info["dir"])
    module_json = module_dir / "src" / "main" / "module.json5"
    if not module_json.is_file():
        return False
    raw = module_json.read_text(encoding="utf-8", errors="replace")
    if '"proxyData"' in raw or '"proxyDatas"' in raw or "proxyData" in raw or "proxyDatas" in raw:
        return True
    try:
        data = parse_json5_text(raw)
    except Exception:
        return False
    module = data.get("module") if isinstance(data, dict) else None
    return isinstance(module, dict) and ("proxyData" in module or "proxyDatas" in module)


def _find_newest_artifact(
    module_dir: Path,
    suffix: str,
    *,
    target_hint: str | None = None,
) -> Path:
    candidates = [path for path in module_dir.rglob(f"*{suffix}") if path.is_file()]
    if target_hint:
        hinted = [
            path for path in candidates
            if f"/outputs/{target_hint}/" in path.as_posix().replace("\\", "/")
        ]
        if hinted:
            candidates = hinted
    if not candidates:
        raise FileNotFoundError(f"No {suffix} artifact found under {module_dir}")
    return sorted(candidates, key=lambda path: (path.stat().st_mtime, str(path)), reverse=True)[0].resolve()


def build_test_packages(
    repo_path: str,
    deveco_path: str,
    *,
    product: str = DEFAULT_PRODUCT,
    sdk_roots: list[Path] | None = None,
    sdk_meta: dict | None = None,
) -> list[dict[str, str]]:
    repo_dir = resolve_directory(repo_path, "repo_path")
    deveco_dir = resolve_directory(deveco_path, "deveco_path")
    _ensure_project_ready(repo_dir)
    if sdk_roots is None or sdk_meta is None:
        sdk_roots, sdk_meta = require_sdk_roots_for_repo(
            repo_dir,
            deveco_dir,
            product_name=product,
        )

    modules = _project_modules(repo_dir)
    artifacts: list[dict[str, str]] = []

    for module_info in modules:
        if module_info.get("type") != "shared":
            continue
        name = str(module_info["name"])
        module_dir = Path(module_info["dir"])
        _run_hvigor_task_with_sdk_roots(
            repo_dir,
            deveco_dir,
            task="assembleHsp",
            mode=DEFAULT_MODE,
            module=name,
            product=product,
            target=None,
            sdk_roots=sdk_roots,
            sdk_meta=sdk_meta,
        )
        artifact = _find_newest_artifact(module_dir, ".hsp", target_hint="default")
        artifacts.append({"kind": "hsp", "module": name, "path": str(artifact)})

    entry_modules = [m for m in modules if m.get("type") == "entry"]
    for module_info in entry_modules:
        name = str(module_info["name"])
        module_dir = Path(module_info["dir"])
        _run_hvigor_task_with_sdk_roots(
            repo_dir,
            deveco_dir,
            task=DEFAULT_TASK,
            mode=DEFAULT_MODE,
            module=name,
            product=product,
            target=None,
            sdk_roots=sdk_roots,
            sdk_meta=sdk_meta,
        )
        artifact = _find_newest_artifact(module_dir, ".hap", target_hint="default")
        artifacts.append({"kind": "hap", "module": name, "path": str(artifact)})

    feature_modules = [m for m in modules if m.get("type") == "feature"]
    feature_modules = sorted(
        feature_modules,
        key=lambda item: (0 if _module_declares_data_proxy(item) else 1, str(item["name"])),
    )


    for module_info in feature_modules:
        name = str(module_info["name"])
        module_dir = Path(module_info["dir"])
        _run_hvigor_task_with_sdk_roots(
            repo_dir,
            deveco_dir,
            task=DEFAULT_TASK,
            mode=DEFAULT_MODE,
            module=name,
            product=product,
            target=None,
            sdk_roots=sdk_roots,
            sdk_meta=sdk_meta,
        )
        artifact = _find_newest_artifact(module_dir, ".hap", target_hint="default")
        artifacts.append({"kind": "hap", "module": name, "path": str(artifact)})

    for module_info in modules:
        if not module_info.get("has_ohos_test"):
            continue
        ohos_test_dir = Path(module_info["dir"]) / "src" / "ohosTest"
        has_test_sources = any(ohos_test_dir.rglob("*.test.ets")) or any(ohos_test_dir.rglob("*.test.ts"))
        has_test_module = (ohos_test_dir / "module.json5").is_file() or (ohos_test_dir / "config.json").is_file()
        has_metadata_checks = (ohos_test_dir / "package_metadata_checks.json").is_file()
        if (
            ((ohos_test_dir / "host_ui_scenarios.json").is_file() or has_metadata_checks)
            and not has_test_sources
            and not has_test_module
        ):
            print(
                "BUILD_TEST_PACKAGE_SKIP="
                + str(module_info["name"])
                + " reason=test_metadata_or_host_ui_only"
            )
            continue
        if module_info.get("type") == "feature" and not _module_declares_data_proxy(module_info):
            print(
                "BUILD_TEST_PACKAGE_SKIP="
                + str(module_info["name"])
                + " reason=feature_ohosTest_task_unavailable"
            )
            continue
        module_dir = Path(module_info["dir"])
        test_plan = _ohos_test_build_plan(module_info)
        _run_hvigor_task_with_sdk_roots(
            repo_dir,
            deveco_dir,
            task=test_plan["task"],
            mode=DEFAULT_MODE,
            module=test_plan["module"],
            product=product,
            target=test_plan["target"] or None,
            sdk_roots=sdk_roots,
            sdk_meta=sdk_meta,
        )
        test_artifact = _find_newest_artifact(module_dir, ".hap", target_hint=test_plan["artifact_hint"])
        artifacts.append({"kind": "ohosTest", "module": test_plan["module"], "path": str(test_artifact)})

    if not artifacts:
        raise FileNotFoundError(f"No installable HAP/HSP artifacts were built under repo: {repo_dir}")
    return artifacts


def _recover_hap_if_success_log(repo_dir: Path, hvigor_combined_output: str) -> str | None:
    if "BUILD SUCCESSFUL" not in (hvigor_combined_output or ""):
        return None
    try:
        return find_built_hap(str(repo_dir))
    except FileNotFoundError:
        return None


def _patch_legacy_fa_test_runner_case(hap_path: str) -> list[str]:
    path = Path(hap_path)
    if not path.is_file() or path.suffix.lower() != ".hap":
        return []

    lower_prefix = "assets/js/testrunner/"
    upper_prefix = "assets/js/TestRunner/"
    notes: list[str] = []

    def runner_alias_payload(name: str, payload: bytes) -> bytes:
        if "/OpenHarmonyTestRunner." not in name:
            return payload
        return payload.replace(b"testrunner", b"TestRunner")

    def legacy_package_prefix(archive: zipfile.ZipFile) -> str:
        if "config.json" not in archive.namelist():
            return ""
        try:
            config = json.loads(archive.read("config.json").decode("utf-8", errors="replace"))
        except Exception:
            return ""
        module = config.get("module") if isinstance(config, dict) else None
        package_name = str(module.get("package") or "").strip() if isinstance(module, dict) else ""
        if not package_name or "/" in package_name or "\\" in package_name:
            return ""
        return package_name.rstrip("/") + "/"

    with zipfile.ZipFile(path, "a") as archive:
        names = set(archive.namelist())
        if "module.json" in names and "ets/modules.abc" in names:
            try:
                module_json = json.loads(archive.read("module.json").decode("utf-8", errors="replace"))
            except Exception:
                module_json = {}
            app = module_json.get("app") if isinstance(module_json, dict) else None
            module = module_json.get("module") if isinstance(module_json, dict) else None
            bundle_name = str(app.get("bundleName") or "").strip() if isinstance(app, dict) else ""
            module_name = str(module.get("name") or "").strip() if isinstance(module, dict) else ""
            src_entrance = str(
                (module.get("srcEntrance") or module.get("srcEntry") or "")
            ).strip() if isinstance(module, dict) else ""
            if module_name and src_entrance:
                runner_stem = src_entrance.replace("\\", "/").removeprefix("./")
                if runner_stem.startswith("ets/"):
                    runner_stem = runner_stem[len("ets/"):]
                runner_stem = re.sub(r"\.(ets|ts|js)$", ".abc", runner_stem, flags=re.IGNORECASE)
                runner_stem_lower = runner_stem.replace("/TestRunner/", "/testrunner/")
                runner_names = [
                    f"ets/{runner_stem}",
                    f"ets/{runner_stem_lower}",
                    f"{module_name}/ets/{runner_stem}",
                    f"{module_name}/ets/{runner_stem_lower}",
                ]
                if module_name.endswith("_test"):
                    runner_names.extend(
                        [
                            "ets/testrunner/OpenHarmonyTestRunner.abc",
                            "ets/TestRunner/OpenHarmonyTestRunner.abc",
                            f"{module_name}/ets/testrunner/OpenHarmonyTestRunner.abc",
                            f"{module_name}/ets/TestRunner/OpenHarmonyTestRunner.abc",
                        ]
                    )
                    if bundle_name:
                        runner_names.extend(
                            [
                                f"{bundle_name}/{module_name}/ets/testrunner/OpenHarmonyTestRunner.abc",
                                f"{bundle_name}/{module_name}/ets/TestRunner/OpenHarmonyTestRunner.abc",
                            ]
                        )
                for target_name in runner_names:
                    if target_name in names:
                        continue
                    archive.writestr(target_name, archive.read("ets/modules.abc"))
                    names.add(target_name)
                    notes.append(f"PACKAGE_PATCH_STAGE_OHOSTEST_RUNNER_ALIAS=ets/modules.abc->{target_name}")
        lower_entries = [
            name for name in sorted(names)
            if name.startswith(lower_prefix) and name.rsplit("/", 1)[-1]
        ]
        for lower_name in lower_entries:
            upper_name = upper_prefix + lower_name[len(lower_prefix):]
            if upper_name in names:
                continue
            archive.writestr(upper_name, runner_alias_payload(upper_name, archive.read(lower_name)))
            names.add(upper_name)
            notes.append(f"PACKAGE_PATCH_OHOSTEST_RUNNER_CASE={lower_name}->{upper_name}")

        runner_js = upper_prefix + "OpenHarmonyTestRunner.js"
        if runner_js in names:
            runner_text = archive.read(runner_js).decode("utf-8", errors="replace")
            for chunk_name in ("vendors", "commons"):
                if f'"{chunk_name}"' not in runner_text and f"'{chunk_name}'" not in runner_text:
                    continue
                for suffix in (".abc", ".js", ".js.map"):
                    source_name = f"assets/js/TestAbility/{chunk_name}{suffix}"
                    target_name = f"{upper_prefix}{chunk_name}{suffix}"
                    if source_name not in names or target_name in names:
                        continue
                    archive.writestr(target_name, archive.read(source_name))
                    names.add(target_name)
                    notes.append(f"PACKAGE_PATCH_OHOSTEST_RUNNER_CHUNK={source_name}->{target_name}")

        package_prefix = legacy_package_prefix(archive)
        if package_prefix:
            for source_name in sorted(name for name in list(names) if name.startswith("assets/js/")):
                if not source_name.rsplit("/", 1)[-1]:
                    continue
                target_names = [
                    package_prefix + source_name,
                    "assets/" + package_prefix + source_name,
                ]
                if package_prefix == "com.example.entry_test/":
                    target_names.append("entry_test/" + source_name)
                    target_names.append(package_prefix + "entry_test/" + source_name)
                    target_names.append("assets/entry_test/" + package_prefix + source_name)
                    target_names.append(package_prefix + "assets/entry_test/" + source_name)
                for target_name in target_names:
                    if target_name in names:
                        continue
                    archive.writestr(target_name, archive.read(source_name))
                    names.add(target_name)
                    notes.append(f"PACKAGE_PATCH_OHOSTEST_PACKAGE_ASSET={source_name}->{target_name}")
    return notes


def _patch_test_package_artifacts(package_artifacts: list[dict[str, str]]) -> list[str]:
    notes: list[str] = []
    for artifact in package_artifacts:
        if artifact.get("kind") != "ohosTest":
            continue
        notes.extend(_patch_legacy_fa_test_runner_case(str(artifact.get("path", ""))))
    return notes


def _package_metadata_check_files(repo_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    for path in repo_dir.rglob("package_metadata_checks.json"):
        normalized = path.as_posix().replace("\\", "/")
        if "/src/ohosTest/" in normalized or "/src/test/" in normalized:
            candidates.append(path.resolve())
    return sorted(candidates)


def _load_json_from_hap(hap_path: Path, inner_path: str) -> Any:
    with zipfile.ZipFile(hap_path) as archive:
        try:
            raw = archive.read(inner_path)
        except KeyError as exc:
            raise AssertionError(f"{hap_path.name} is missing {inner_path}") from exc
    return json.loads(raw.decode("utf-8"))


def _load_module_metadata_from_hap(hap_path: Path) -> dict[str, Any]:
    try:
        module_json = _load_json_from_hap(hap_path, "module.json")
    except AssertionError:
        module_json = _load_json_from_hap(hap_path, "config.json")
    module = module_json.get("module") if isinstance(module_json, dict) else None
    if not isinstance(module, dict):
        raise AssertionError(f"{hap_path.name} has no module metadata object")
    return module


def _matches_expected(actual: dict[str, Any], expected: dict[str, Any], *, context: str) -> bool:
    for key, expected_value in expected.items():
        if key in {"metadataName", "metadataResource", "profile", "contains", "containsForm"}:
            continue
        if actual.get(key) != expected_value:
            return False
    metadata_name = expected.get("metadataName")
    if metadata_name is not None:
        metadata = actual.get("metadata")
        metadata_entries = metadata if isinstance(metadata, list) else [metadata] if isinstance(metadata, dict) else []
        if not metadata_entries:
            return False
        if not any(isinstance(item, dict) and item.get("name") == metadata_name for item in metadata_entries):
            return False
    metadata_resource = expected.get("metadataResource")
    if metadata_resource is not None:
        metadata = actual.get("metadata")
        metadata_entries = metadata if isinstance(metadata, list) else [metadata] if isinstance(metadata, dict) else []
        if not metadata_entries:
            return False
        if not any(isinstance(item, dict) and item.get("resource") == metadata_resource for item in metadata_entries):
            return False
    return True


def _require_named_entries(entries: Any, expected_names: list[Any], *, context: str) -> None:
    if not isinstance(entries, list):
        raise AssertionError(f"{context} is not a list")
    actual_names = {
        item.get("name")
        for item in entries
        if isinstance(item, dict)
    }
    missing = [
        str(name)
        for name in expected_names
        if str(name) not in {str(actual) for actual in actual_names}
    ]
    if missing:
        raise AssertionError(f"{context} missing names: {', '.join(missing)}")


def _require_matching_entry(entries: Any, expected: dict[str, Any], *, context: str) -> dict[str, Any]:
    if not isinstance(entries, list):
        raise AssertionError(f"{context} is not a list")
    for entry in entries:
        if isinstance(entry, dict) and _matches_expected(entry, expected, context=context):
            return entry
    raise AssertionError(f"No {context} entry matches {expected}")


def _assert_profile(profile: Any, expected: dict[str, Any], *, context: str) -> None:
    contains = expected.get("contains")
    if isinstance(contains, dict):
        if not isinstance(profile, dict):
            raise AssertionError(f"{context} is not a JSON object")
        for key, value in contains.items():
            if profile.get(key) != value:
                raise AssertionError(f"{context}.{key} expected {value!r}, got {profile.get(key)!r}")

    contains_form = expected.get("containsForm")
    if isinstance(contains_form, dict):
        if not isinstance(profile, dict) or not isinstance(profile.get("forms"), list):
            raise AssertionError(f"{context} does not contain a forms list")
        for form in profile["forms"]:
            if isinstance(form, dict) and all(form.get(key) == value for key, value in contains_form.items()):
                return
        raise AssertionError(f"{context} has no form matching {contains_form}")


def _assert_pack_info(pack_info: Any, expected: dict[str, Any], *, context: str) -> None:
    if not isinstance(pack_info, dict):
        raise AssertionError(f"{context} is not a JSON object")

    module_name = str(expected.get("moduleName") or "").strip()
    if module_name:
        modules = pack_info.get("summary", {}).get("modules")
        if not isinstance(modules, list):
            raise AssertionError(f"{context}.summary.modules is not a list")
        matching_modules = [
            module for module in modules
            if isinstance(module, dict)
            and isinstance(module.get("distro"), dict)
            and module["distro"].get("moduleName") == module_name
        ]
        if not matching_modules:
            raise AssertionError(f"{context} has no summary module named {module_name!r}")

        module_type = expected.get("moduleType")
        if module_type is not None and not any(
            module["distro"].get("moduleType") == module_type for module in matching_modules
        ):
            raise AssertionError(f"{context} has no {module_name!r} summary module with type {module_type!r}")

    package_name = str(expected.get("packageName") or "").strip()
    if package_name:
        packages = pack_info.get("packages")
        if not isinstance(packages, list):
            raise AssertionError(f"{context}.packages is not a list")
        matching_packages = [
            package for package in packages
            if isinstance(package, dict) and package.get("name") == package_name
        ]
        if not matching_packages:
            raise AssertionError(f"{context} has no package named {package_name!r}")

        module_type = expected.get("moduleType")
        if module_type is not None and not any(
            package.get("moduleType") == module_type for package in matching_packages
        ):
            raise AssertionError(f"{context} has no {package_name!r} package with type {module_type!r}")


def _run_package_metadata_check_file(
    check_file: Path,
    package_artifacts: list[dict[str, str]],
) -> list[str]:
    spec = json.loads(check_file.read_text(encoding="utf-8"))
    checks = spec.get("checks") if isinstance(spec, dict) else None
    if not isinstance(checks, list):
        raise AssertionError(f"{check_file} must contain a checks array")

    artifacts_by_module = {str(item.get("module")): Path(str(item.get("path"))) for item in package_artifacts}
    notes = [f"PACKAGE_METADATA_CHECK_FILE={format_path_for_display(check_file)}"]
    for index, check in enumerate(checks, start=1):
        if not isinstance(check, dict):
            raise AssertionError(f"{check_file} check #{index} is not an object")
        module_name = str(check.get("module") or "").strip()
        if not module_name:
            raise AssertionError(f"{check_file} check #{index} missing module")
        hap_path = artifacts_by_module.get(module_name)
        if hap_path is None:
            raise AssertionError(f"No built package artifact found for module {module_name}")

        module = _load_module_metadata_from_hap(hap_path)

        expected_module_name = check.get("moduleName")
        if expected_module_name is not None and module.get("name") != expected_module_name:
            raise AssertionError(
                f"{hap_path.name} module.name expected {expected_module_name!r}, got {module.get('name')!r}"
            )

        expected_module_type = check.get("moduleType")
        if expected_module_type is not None and module.get("type") != expected_module_type:
            raise AssertionError(
                f"{hap_path.name} module.type expected {expected_module_type!r}, got {module.get('type')!r}"
            )

        for expected_extension in check.get("extensionAbilities", []) or []:
            if not isinstance(expected_extension, dict):
                raise AssertionError(f"{check_file} extensionAbilities entry is not an object")
            _require_matching_entry(
                module.get("extensionAbilities"),
                expected_extension,
                context=f"{module_name}.extensionAbilities",
            )

        for expected_ability in check.get("abilities", []) or []:
            if not isinstance(expected_ability, dict):
                raise AssertionError(f"{check_file} abilities entry is not an object")
            _require_matching_entry(
                module.get("abilities"),
                expected_ability,
                context=f"{module_name}.abilities",
            )

        expected_req_permissions = check.get("reqPermissions")
        if isinstance(expected_req_permissions, list):
            _require_named_entries(
                module.get("reqPermissions"),
                expected_req_permissions,
                context=f"{module_name}.reqPermissions",
            )

        data_proxy_property = str(check.get("dataProxyProperty") or "proxyData").strip()
        data_proxy_entries = module.get(data_proxy_property)
        for expected_proxy in check.get("dataProxyEntries", []) or []:
            if not isinstance(expected_proxy, dict):
                raise AssertionError(f"{check_file} dataProxyEntries entry is not an object")
            _require_matching_entry(
                data_proxy_entries,
                expected_proxy,
                context=f"{module_name}.dataProxyEntries",
            )

        for expected_profile in check.get("profiles", []) or []:
            if not isinstance(expected_profile, dict):
                raise AssertionError(f"{check_file} profiles entry is not an object")
            profile_path = str(expected_profile.get("path") or "").strip()
            if not profile_path:
                raise AssertionError(f"{check_file} profiles entry missing path")
            profile = _load_json_from_hap(hap_path, profile_path)
            _assert_profile(profile, expected_profile, context=f"{module_name}:{profile_path}")

        expected_pack_info = check.get("packInfo")
        if isinstance(expected_pack_info, dict):
            pack_info = _load_json_from_hap(hap_path, "pack.info")
            _assert_pack_info(pack_info, expected_pack_info, context=f"{module_name}:pack.info")

        notes.append(f"PACKAGE_METADATA_CHECK_PASSED={module_name}:{hap_path.name}")
    return notes


def _run_package_metadata_checks(repo_dir: Path, package_artifacts: list[dict[str, str]]) -> list[str]:
    notes: list[str] = []
    for check_file in _package_metadata_check_files(repo_dir):
        notes.extend(_run_package_metadata_check_file(check_file, package_artifacts))
    if notes:
        notes.append("PACKAGE_METADATA_CHECK_STATUS=SUCCESS")
    return notes


def _resource_compile_check_files(repo_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    for path in repo_dir.rglob("resource_compile_checks.json"):
        normalized = path.as_posix().replace("\\", "/")
        if any(part in {"build", ".hvigor", "node_modules", "oh_modules"} for part in path.relative_to(repo_dir).parts):
            continue
        if "/src/ohosTest/" in normalized or "/src/test/" in normalized:
            candidates.append(path.resolve())
    return sorted(candidates)


def _assert_text_checks(text: str, expected: dict[str, Any], *, context: str) -> None:
    for literal in expected.get("contains", []) or []:
        literal_text = str(literal)
        if literal_text not in text:
            raise AssertionError(f"{context} does not contain {literal_text!r}")

    for symbol_check in expected.get("symbols", []) or []:
        if not isinstance(symbol_check, dict):
            raise AssertionError(f"{context} symbols entry is not an object")
        symbol = str(symbol_check.get("name") or "").strip()
        if not symbol:
            raise AssertionError(f"{context} symbols entry missing name")
        actual_count = len(re.findall(rf"\b{re.escape(symbol)}\b", text))
        expected_count = symbol_check.get("count")
        if expected_count is not None and actual_count != expected_count:
            raise AssertionError(
                f"{context} symbol {symbol!r} expected count {expected_count}, got {actual_count}"
            )
        if expected_count is None and actual_count < 1:
            raise AssertionError(f"{context} symbol {symbol!r} was not found")


def _run_resource_compile_check_file(check_file: Path, repo_dir: Path) -> list[str]:
    spec = json.loads(check_file.read_text(encoding="utf-8"))
    checks = spec.get("checks") if isinstance(spec, dict) else None
    if not isinstance(checks, list):
        raise AssertionError(f"{check_file} must contain a checks array")

    notes = [f"RESOURCE_COMPILE_CHECK_FILE={format_path_for_display(check_file)}"]
    for index, check in enumerate(checks, start=1):
        if not isinstance(check, dict):
            raise AssertionError(f"{check_file} check #{index} is not an object")
        relative_path = str(check.get("path") or "").strip()
        if not relative_path:
            raise AssertionError(f"{check_file} check #{index} missing path")
        generated_path = (repo_dir / relative_path).resolve()
        try:
            generated_path.relative_to(repo_dir.resolve())
        except ValueError as exc:
            raise AssertionError(f"{check_file} check #{index} path escapes repo: {relative_path}") from exc
        if not generated_path.is_file():
            raise AssertionError(f"Generated resource check file missing: {generated_path}")

        text = generated_path.read_text(encoding="utf-8", errors="replace")
        _assert_text_checks(text, check, context=format_path_for_display(generated_path, start=repo_dir))
        label = str(check.get("label") or relative_path)
        notes.append(f"RESOURCE_COMPILE_CHECK_PASSED={label}")
    return notes


def _run_resource_compile_checks(repo_dir: Path) -> list[str]:
    notes: list[str] = []
    for check_file in _resource_compile_check_files(repo_dir):
        notes.extend(_run_resource_compile_check_file(check_file, repo_dir))
    if notes:
        notes.append("RESOURCE_COMPILE_CHECK_STATUS=SUCCESS")
    return notes


def _packaged_resource_check_files(repo_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    for path in repo_dir.rglob("packaged_resource_checks.json"):
        normalized = path.as_posix().replace("\\", "/")
        if any(part in {"build", ".hvigor", "node_modules", "oh_modules"} for part in path.relative_to(repo_dir).parts):
            continue
        if "/src/ohosTest/" in normalized or "/src/test/" in normalized:
            candidates.append(path.resolve())
    return sorted(candidates)


def _load_json_from_gzip_tar(artifact_path: Path, member_path: str) -> Any:
    try:
        raw = gzip.decompress(artifact_path.read_bytes())
    except OSError as exc:
        raise AssertionError(f"Packaged resource artifact is not gzip-compressed: {artifact_path}") from exc
    try:
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            extracted = archive.extractfile(member_path)
            if extracted is None:
                raise AssertionError(f"{artifact_path.name} does not contain {member_path!r}")
            return json.loads(extracted.read().decode("utf-8"))
    except tarfile.TarError as exc:
        raise AssertionError(f"Packaged resource artifact is not a tar archive: {artifact_path}") from exc


def _assert_packaged_resource_strings(resource_json: Any, expected: dict[str, Any], *, context: str) -> None:
    raw_strings = resource_json.get("string") if isinstance(resource_json, dict) else None
    if not isinstance(raw_strings, list):
        raise AssertionError(f"{context} has no string array")
    values: dict[str, str] = {}
    for item in raw_strings:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            values[item["name"]] = str(item.get("value") or "")

    for string_check in expected.get("strings", []) or []:
        if not isinstance(string_check, dict):
            raise AssertionError(f"{context} strings entry is not an object")
        name = str(string_check.get("name") or "").strip()
        if not name:
            raise AssertionError(f"{context} strings entry missing name")
        if name not in values:
            raise AssertionError(f"{context} missing string resource {name!r}")
        expected_value = string_check.get("value")
        if expected_value is not None and values[name] != str(expected_value):
            raise AssertionError(
                f"{context} string {name!r} expected {expected_value!r}, got {values[name]!r}"
            )
        for forbidden in string_check.get("notContains", []) or []:
            forbidden_text = str(forbidden)
            if forbidden_text in values[name]:
                raise AssertionError(
                    f"{context} string {name!r} contains forbidden text {forbidden_text!r}"
                )


def _run_packaged_resource_check_file(check_file: Path, repo_dir: Path) -> list[str]:
    spec = json.loads(check_file.read_text(encoding="utf-8"))
    checks = spec.get("checks") if isinstance(spec, dict) else None
    if not isinstance(checks, list):
        raise AssertionError(f"{check_file} must contain a checks array")

    notes = [f"PACKAGED_RESOURCE_CHECK_FILE={format_path_for_display(check_file)}"]
    for index, check in enumerate(checks, start=1):
        if not isinstance(check, dict):
            raise AssertionError(f"{check_file} check #{index} is not an object")
        artifact_relative = str(check.get("artifact") or "").strip()
        member_path = str(check.get("member") or "").strip()
        if not artifact_relative:
            raise AssertionError(f"{check_file} check #{index} missing artifact")
        if not member_path:
            raise AssertionError(f"{check_file} check #{index} missing member")
        artifact_path = (repo_dir / artifact_relative).resolve()
        try:
            artifact_path.relative_to(repo_dir.resolve())
        except ValueError as exc:
            raise AssertionError(f"{check_file} check #{index} artifact escapes repo: {artifact_relative}") from exc
        if not artifact_path.is_file():
            raise AssertionError(f"Packaged resource artifact missing: {artifact_path}")

        resource_json = _load_json_from_gzip_tar(artifact_path, member_path)
        context = f"{format_path_for_display(artifact_path, start=repo_dir)}:{member_path}"
        _assert_packaged_resource_strings(resource_json, check, context=context)
        label = str(check.get("label") or member_path)
        notes.append(f"PACKAGED_RESOURCE_CHECK_PASSED={label}")
    return notes


def _run_packaged_resource_checks(repo_dir: Path) -> list[str]:
    notes: list[str] = []
    for check_file in _packaged_resource_check_files(repo_dir):
        notes.extend(_run_packaged_resource_check_file(check_file, repo_dir))
    if notes:
        notes.append("PACKAGED_RESOURCE_CHECK_STATUS=SUCCESS")
    return notes


def _hap_path_after_success(repo_dir: Path, hvigor_combined_output: str) -> str:
    try:
        return find_built_hap(str(repo_dir))
    except FileNotFoundError:
        if "BUILD SUCCESSFUL" in (hvigor_combined_output or ""):
            return ""
        raise


def find_built_hap(repo_path: str) -> str:
    repo_dir = resolve_directory(repo_path, "repo_path")
    hap_files = sorted(
        repo_dir.rglob("*.hap"),
        key=lambda path: (path.stat().st_mtime, str(path)),
        reverse=True,
    )
    if not hap_files:
        raise FileNotFoundError(f"No .hap file found under repo: {repo_dir}")
    return str(hap_files[0].resolve())


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a HarmonyOS HAP with hvigorw.")
    raw_args = sys.argv[1:]
    module_explicit = any(arg == "--module" or arg.startswith("--module=") for arg in raw_args)
    task_explicit = any(arg == "--task" or arg.startswith("--task=") for arg in raw_args)
    parser.add_argument("--repo-path", required=True, help="Harmony repo path.")
    parser.add_argument(
        "--deveco-path",
        default=os.environ.get("DEVECO_PATH", "").strip(),
        help="DevEco Studio install path. Default: env DEVECO_PATH",
    )
    parser.add_argument("--task", default=DEFAULT_TASK, help=f"Hvigor task to run. Default: {DEFAULT_TASK}")
    parser.add_argument("--mode", default=DEFAULT_MODE, help=f"Hvigor mode. Default: {DEFAULT_MODE}")
    parser.add_argument("--module", default=DEFAULT_MODULE, help=f"Module name. Default: {DEFAULT_MODULE}")
    parser.add_argument("--product", default=DEFAULT_PRODUCT, help=f"Product name. Default: {DEFAULT_PRODUCT}")
    parser.add_argument("--target", default=DEFAULT_TARGET, help="Optional target name, for example ohosTest.")
    parser.add_argument(
        "--find-only",
        action="store_true",
        help="Only scan the repo for the newest built .hap file without running hvigor.",
    )
    parser.add_argument(
        "--build-test-packages",
        action="store_true",
        help="Build all packages needed before instrument tests: dependency HSPs, entry HAP, and ohosTest HAP.",
    )
    parser.add_argument(
        "--skip-ohpm-install",
        action="store_true",
        help="Skip running `ohpm install` before build (not recommended; wrapper/tool deps may be missing).",
    )
    parser.add_argument(
        "--ohpm-timeout-sec",
        type=float,
        default=600.0,
        help="Timeout for `ohpm install` (default 600s).",
    )
    args = parser.parse_args()
    args.module_explicit = module_explicit
    args.task_explicit = task_explicit
    return args


def main() -> int:
    ensure_command_line_tools_env()
    args = _parse_args()
    log_lines: list[str] = [
        "TOOL=build_app.py",
        f"ARGV={' '.join(sys.argv[1:])}",
        f"REPO_PATH={args.repo_path}",
        f"DEVECO_PATH={args.deveco_path}",
    ]
    try:
        if not args.deveco_path:
            raise ValueError("DEVECO_PATH missing (set command_line_tools_test/.env or pass --deveco-path)")
        if os.environ.get("ARKAGENT_REQUIRE_EXPLICIT_BUILD_TARGET", "").strip() == "1":
            missing = []
            if not args.module_explicit:
                missing.append("--module")
            if not args.task_explicit:
                missing.append("--task")
            if missing:
                raise ValueError(
                    "Explicit HarmonyOS build target required by "
                    "ARKAGENT_REQUIRE_EXPLICIT_BUILD_TARGET=1; missing "
                    + ", ".join(missing)
                    + ". Read KNOWN DEFECT FILES and build-profile.json5/module.json5, then rerun with "
                    "`--module <module> --task <assembleHap|assembleHar|assembleHsp>`. "
                    "For example, HAR/library fixes should use `--module library --task assembleHar`."
                )
        args.repo_path = _resolve_agent_repo_path_arg(args.repo_path)
        repo_dir = resolve_directory(args.repo_path, "repo_path")
        deveco_dir = resolve_directory(args.deveco_path, "deveco_path")
        resolved_module = _resolve_module_name_case(repo_dir, args.module)
        if resolved_module != args.module:
            log_lines.append(f"BUILD_MODULE_RESOLVED={resolved_module} (from {args.module})")
            print(f"BUILD_MODULE_RESOLVED={resolved_module} (from {args.module})")
            args.module = resolved_module

        package_artifacts: list[dict[str, str]] = []
        if args.find_only:
            sdk_roots, sdk_meta = require_sdk_roots_for_repo(
                repo_dir,
                deveco_dir,
                product_name=args.product,
            )
            local_properties_path = ensure_local_properties(
                repo_dir,
                sdk_root=sdk_roots[0] if sdk_roots else None,
                sdk_api_level=sdk_meta.get("sdk_selection_api_level"),
                base_env=os.environ.copy(),
            )
            print_build_profile_sdk_resolution(sdk_meta)
            log_lines.append(f"SDK_META={sdk_meta}")
            log_lines.append(f"SDK_ROOTS={[str(root) for root in sdk_roots]}")
            log_lines.append(f"SDK_HVIGOR_ROOT={sdk_roots[0]}")
            log_lines.append(
                "SDK_API_SLICE="
                + str(resolve_sdk_api_slice_for_api(sdk_roots[0], sdk_meta.get("sdk_selection_api_level")))
            )
            log_lines.append(f"LOCAL_PROPERTIES_PATH={local_properties_path}")
            hap_path = find_built_hap(args.repo_path)
        else:
            sdk_roots, sdk_meta = require_sdk_roots_for_repo(
                repo_dir,
                deveco_dir,
                product_name=args.product,
            )
            local_properties_path = ensure_local_properties(
                repo_dir,
                sdk_root=sdk_roots[0] if sdk_roots else None,
                sdk_api_level=sdk_meta.get("sdk_selection_api_level"),
                base_env=os.environ.copy(),
            )
            print_build_profile_sdk_resolution(sdk_meta)
            log_lines.append(f"SDK_META={sdk_meta}")
            log_lines.append(f"SDK_ROOTS={[str(root) for root in sdk_roots]}")
            log_lines.append(f"SDK_HVIGOR_ROOT={sdk_roots[0]}")
            log_lines.append(
                "SDK_API_SLICE="
                + str(resolve_sdk_api_slice_for_api(sdk_roots[0], sdk_meta.get("sdk_selection_api_level")))
            )
            log_lines.append(f"LOCAL_PROPERTIES_PATH={local_properties_path}")
            pre_ohpm_prepare_notes = prepare_native_repair_environment(
                repo_dir,
                deveco_dir,
                product_name=args.product,
                timeout_sec=900,
                sdk_roots=sdk_roots,
                sdk_meta=sdk_meta,
            )
            for note in pre_ohpm_prepare_notes:
                print(note)
                log_lines.append(note)
            if not args.skip_ohpm_install:
                has_oh_package = any((repo_dir / name).is_file() for name in ("oh-package.json5", "oh-package.json"))
                if has_oh_package:
                    ohpm_timeout = args.ohpm_timeout_sec if args.ohpm_timeout_sec > 0 else None
                    ohpm_code, ohpm_output = run_ohpm_install(
                        repo_dir,
                        deveco_dir,
                        timeout_sec=ohpm_timeout,
                        product_name=args.product,
                    )
                    log_lines.append(ohpm_output.strip())
                    if ohpm_code != 0:
                        raise RuntimeError(f"`ohpm install` failed before build.\n{tail_text(ohpm_output, limit=80)}")
                else:
                    log_lines.append("OHPM_STATUS=SKIPPED\nOHPM_REASON=no_oh_package_json5_legacy_hvigor_project")

            prepare_notes = prepare_native_repair_environment(
                repo_dir,
                deveco_dir,
                product_name=args.product,
                timeout_sec=900,
                sdk_roots=sdk_roots,
                sdk_meta=sdk_meta,
            )
            for note in prepare_notes:
                print(note)
                log_lines.append(note)
            hvigor_path = find_hvigor_wrapper(repo_dir, deveco_dir)
            hvigor_command = [str(hvigor_path), *_default_build_command(args.task, args.mode, args.module, args.product, args.target)]
            hvigor_equivalent = format_command(hvigor_command)
            print(f"HVIGOR_WRAPPER_PATH={format_path_for_display(hvigor_path)}")
            print(f"HVIGOR_EQUIVALENT_COMMAND={hvigor_equivalent}")
            log_lines.append(f"HVIGOR_WRAPPER_PATH={hvigor_path}")
            log_lines.append(f"HVIGOR_EQUIVALENT_COMMAND={hvigor_equivalent}")
            if args.build_test_packages:
                package_artifacts = build_test_packages(
                    repo_path=args.repo_path,
                    deveco_path=args.deveco_path,
                    product=args.product,
                    sdk_roots=sdk_roots,
                    sdk_meta=sdk_meta,
                )
                for patch_note in _patch_test_package_artifacts(package_artifacts):
                    print(patch_note)
                    log_lines.append(patch_note)
                for check_note in _run_package_metadata_checks(repo_dir, package_artifacts):
                    print(check_note)
                    log_lines.append(check_note)
                hap_candidates = [item["path"] for item in package_artifacts if item["kind"] == "hap"]
                hap_path = hap_candidates[-1] if hap_candidates else ""
            else:
                hap_path = build_project_with_options(
                    repo_path=args.repo_path,
                    deveco_path=args.deveco_path,
                    task=args.task,
                    mode=args.mode,
                    module=args.module,
                    product=args.product,
                    target=args.target,
                    sdk_roots=sdk_roots,
                    sdk_meta=sdk_meta,
                )

        for check_note in _run_resource_compile_checks(repo_dir):
            print(check_note)
            log_lines.append(check_note)
        for check_note in _run_packaged_resource_checks(repo_dir):
            print(check_note)
            log_lines.append(check_note)
        print("BUILD_STATUS=SUCCESS")
        log_lines.append("BUILD_STATUS=SUCCESS")
        if package_artifacts:
            package_json = json.dumps(package_artifacts, ensure_ascii=False)
            print(f"PACKAGE_PATHS_JSON={package_json}")
            log_lines.append(f"PACKAGE_PATHS_JSON={package_json}")
            for index, item in enumerate(package_artifacts, start=1):
                display_path = format_path_for_display(item["path"], start=repo_dir)
                print(f"PACKAGE_PATH_{index}={display_path}")
                print(f"PACKAGE_KIND_{index}={item['kind']}")
                print(f"PACKAGE_MODULE_{index}={item['module']}")
        if hap_path:
            print(f"HAP_PATH={format_path_for_display(hap_path, start=repo_dir)}")
        else:
            print("HAP_PATH=")
            print(
                "BUILD_NOTE=hvigor_success_but_no_hap_file "
                "(unsigned / skip sign - install may require configuring signingConfigs).",
                file=sys.stderr,
            )
        log_lines.append(f"HAP_PATH={hap_path}")
        log_path = write_tool_log("build_app", "\n".join(log_lines))
        print(f"LOG_PATH={format_path_for_display(log_path)}")
        return 0
    except Exception as exc:
        log_lines.append("BUILD_STATUS=FAILED")
        log_lines.append(f"ERROR={exc}")
        details = getattr(exc, "details", None)
        if isinstance(details, dict):
            detail_text = json.dumps(details, ensure_ascii=False, default=str)
            log_lines.append(f"ERROR_DETAILS={detail_text}")
        log_path = write_tool_log("build_app", "\n".join(log_lines))
        print(f"LOG_PATH={format_path_for_display(log_path)}", file=sys.stderr)
        print("BUILD_STATUS=FAILED", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        if isinstance(details, dict):
            print(f"ERROR_DETAILS={detail_text}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
