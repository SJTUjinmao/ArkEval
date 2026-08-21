from __future__ import annotations

import _load_env  # noqa: F401
from _load_env import ensure_command_line_tools_env

import argparse
import json
import os
import re
import sys
import zipfile
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

    for sdk_root in sdk_candidates:
        env = build_harmony_command_env(
            sdk_root=sdk_root,
            sdk_api_level=selected_api_level,
            deveco_path=deveco_dir,
            base_env=os.environ.copy(),
        )
        result = run_command(command, cwd=repo_dir, env=env)
        output_text = command_output(result)
        if result.returncode == 0:
            return output_text

        if "BUILD SUCCESSFUL" in (output_text or ""):
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

    for module_info in modules:
        if not module_info.get("has_ohos_test"):
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
    with zipfile.ZipFile(path, "a") as archive:
        names = set(archive.namelist())
        lower_entries = [
            name for name in sorted(names)
            if name.startswith(lower_prefix) and name.rsplit("/", 1)[-1]
        ]
        for lower_name in lower_entries:
            upper_name = upper_prefix + lower_name[len(lower_prefix):]
            if upper_name in names:
                continue
            archive.writestr(upper_name, archive.read(lower_name))
            names.add(upper_name)
            notes.append(f"PACKAGE_PATCH_OHOSTEST_RUNNER_CASE={lower_name}->{upper_name}")
    return notes


def _patch_test_package_artifacts(package_artifacts: list[dict[str, str]]) -> list[str]:
    notes: list[str] = []
    for artifact in package_artifacts:
        if artifact.get("kind") != "ohosTest":
            continue
        notes.extend(_patch_legacy_fa_test_runner_case(str(artifact.get("path", ""))))
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
        log_path = write_tool_log("build_app", "\n".join(log_lines))
        print(f"LOG_PATH={format_path_for_display(log_path)}", file=sys.stderr)
        print("BUILD_STATUS=FAILED", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
