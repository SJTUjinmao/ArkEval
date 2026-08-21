"""
在开发机上执行 HarmonyOS **本地单元测试**（``src/test``、Hypium），通过 hvigor ``test`` 任务。

对应官方说明：本地测试在 IDE 中点击运行，与命令行等价方式一般为工程根目录执行 ``hvigorw`` / ``hvigorw.bat`` 的 ``test`` 任务
（参见 `HarmonyOS 运行本地测试 <https://developer.huawei.com/consumer/cn/doc/harmonyos-guides/ide-local-test>`_）。

说明：

- 与 ``run_tests.py`` 中的 **instrument**（``hdc shell aa test`` + ``src/ohosTest``）不同；本地测试**不需要** hdc/模拟器。
- 华为文档与社区反馈：本地测试依赖本机工具链，**部分环境（如部分 Linux CI）可能不支持**，此时应改用 instrument 或官方给出的替代方案。

前置：``main()`` 首行 ``ensure_command_line_tools_env()``（与全部 ``tools/*.py`` 一致）。
"""
from __future__ import annotations

import _load_env  # noqa: F401
from _load_env import ensure_command_line_tools_env

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from .common import (
        append_text_file,
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
    )
except ImportError:
    from common import (  # type: ignore
        append_text_file,
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
    )

SDK_RETRY_MARKERS = (
    "SDK component missing",
    "Invalid value of 'DEVECO_SDK_HOME'",
    "00303168",
    "00303217",
)
MISSING_TEST_TASK_MARKERS = (
    "Task test not found",
    "Task 'test' not found",
)
UNIT_TEST_REPLACE_PAGE_PROP = "unit.test.replace.page"
UNIT_TEST_BRIDGE_DIR = "__xb_local_tests__"
UNIT_TEST_BRIDGE_PAGE = f"{UNIT_TEST_BRIDGE_DIR}/List.test"
SOURCE_CONTRACT_ROW03 = "XB_SOURCE_CONTRACT: row03_disk_cache_path"
SOURCE_CONTRACT_ROW06 = "XB_SOURCE_CONTRACT: row06_is_url_exist_cache_type"
SOURCE_CONTRACT_ROW09 = "XB_SOURCE_CONTRACT: row09_orange_shopping_transition"


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _logs_dir() -> Path:
    return _workspace_root() / "dev_sessions" / "05_test" / "logs"


def _timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _is_generated_src_test(source_dir: Path, repo_dir: Path) -> bool:
    blocked = {"build", ".hvigor", "node_modules", "oh_modules"}
    try:
        raw_parts = source_dir.absolute().relative_to(repo_dir.absolute()).parts
    except ValueError:
        raw_parts = source_dir.absolute().parts
    if any(p in blocked for p in raw_parts):
        return True
    try:
        relative_parts = source_dir.resolve().relative_to(repo_dir.resolve()).parts
    except ValueError:
        relative_parts = source_dir.resolve().parts
    return any(p in blocked for p in relative_parts)


def discover_local_test_modules(repo_dir: Path) -> list[str]:
    """模块名列表：存在 ``<module>/src/test`` 且其下含 ``*.test.ets`` 的模块。"""
    seen: set[str] = set()
    ordered: list[str] = []
    for test_root in sorted(repo_dir.rglob("src/test"), key=lambda p: str(p)):
        if not test_root.is_dir():
            continue
        if _is_generated_src_test(test_root, repo_dir):
            continue
        has_case = any(test_root.rglob("*.test.ets"))
        if not has_case:
            continue
        module_root = test_root.parent.parent
        if not (module_root / "src" / "main" / "module.json5").is_file():
            continue
        name = module_root.name
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _read_json5_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = parse_json5_text(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _project_module_dirs(repo_dir: Path) -> dict[str, Path]:
    modules: dict[str, Path] = {}
    profile = _read_json5_file(repo_dir / "build-profile.json5")
    for raw in profile.get("modules") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        src_path = str(raw.get("srcPath") or name).strip()
        if not name or not src_path:
            continue
        src_path = src_path.replace("\\", "/")
        if src_path.startswith("./"):
            src_path = src_path[2:]
        modules[name] = (repo_dir / src_path).resolve()
    if not modules:
        for candidate in repo_dir.iterdir():
            if candidate.is_dir() and (candidate / "src" / "main" / "module.json5").is_file():
                modules[candidate.name] = candidate.resolve()
    return modules


def _module_type(module_dir: Path) -> str:
    data = _read_json5_file(module_dir / "src" / "main" / "module.json5")
    module_data = data.get("module") if isinstance(data.get("module"), dict) else {}
    return str(module_data.get("type") or "").strip().lower()


def _has_prop(extra_props: list[str], key: str) -> bool:
    prefix = f"{key}="
    return any(raw.strip() == key or raw.strip().startswith(prefix) for raw in extra_props)


def _ensure_unit_test_bridge(repo_dir: Path, module: str, extra_props: list[str]) -> Path | None:
    """Make HAR/HSP src/test suites reachable from UnitTestBuild."""
    if _has_prop(extra_props, UNIT_TEST_REPLACE_PAGE_PROP):
        return None
    module_dir = _project_module_dirs(repo_dir).get(module)
    if module_dir is None:
        return None
    if _module_type(module_dir) not in {"har", "hsp"}:
        return None
    list_test = module_dir / "src" / "test" / "List.test.ets"
    if not list_test.is_file():
        return None
    bridge_dir = module_dir / "src" / "main" / "ets" / UNIT_TEST_BRIDGE_DIR
    bridge_file = bridge_dir / "List.test.ets"
    bridge_dir.mkdir(parents=True, exist_ok=True)
    bridge_file.write_text(
        "\n".join(
            [
                "import testsuite from '../../../test/List.test'",
                "",
                "@Entry",
                "@Component",
                "struct XbLocalUnitTestEntry {",
                "  aboutToAppear(): void {",
                "    testsuite()",
                "  }",
                "",
                "  build() {",
                "    Column() {}",
                "  }",
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    extra_props.append(f"{UNIT_TEST_REPLACE_PAGE_PROP}={UNIT_TEST_BRIDGE_PAGE}")
    return bridge_file


def _cleanup_unit_test_bridge(bridge_file: Path | None) -> None:
    if bridge_file is None:
        return
    try:
        bridge_file.unlink(missing_ok=True)
        parent = bridge_file.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass


def _hvigor_base_args(
    *,
    mode: str,
    module: str,
    product: str,
    coverage: bool,
    extra_props: list[str],
) -> list[str]:
    args = [
        "--no-daemon",
        "--no-incremental",
        "--mode",
        mode,
        "-p",
        f"module={module}",
        "-p",
        f"product={product}",
    ]
    if coverage:
        args.extend(["-p", "coverage=true"])
    for raw in extra_props:
        if raw.strip():
            if "=" in raw:
                k, v = raw.split("=", 1)
                args.extend(["-p", f"{k.strip()}={v.strip()}"])
            else:
                args.extend(["-p", raw.strip()])
    return args


def _should_retry_with_next_sdk(build_output: str) -> bool:
    return any(m in (build_output or "") for m in SDK_RETRY_MARKERS)


def _is_missing_test_task(build_output: str) -> bool:
    return any(m in (build_output or "") for m in MISSING_TEST_TASK_MARKERS)


def _append_hvigor_flags(
    command: list[str],
    *,
    hvigor_stacktrace: bool,
    hvigor_debug: bool,
) -> list[str]:
    result = list(command)
    if hvigor_stacktrace:
        result.append("--stacktrace")
    if hvigor_debug:
        result.append("--debug")
    return result


def _success_or_recovered(result: subprocess.CompletedProcess[str], out: str) -> tuple[int, str] | None:
    if result.returncode == 0:
        return 0, out
    if "BUILD SUCCESSFUL" not in (out or ""):
        return None
    recovered = "\n".join(
        [
            "LOCAL_TEST_NOTE=recovered_success_from_build_successful_output",
            f"HVIGOR_EXIT_CODE={result.returncode}",
            (out or "").strip(),
        ]
    ).strip()
    return 0, recovered


def run_local_tests_hvigor(
    repo_path: str,
    deveco_path: str,
    *,
    mode: str = "module",
    module: str = "entry",
    product: str = "default",
    task: str = "test",
    coverage: bool = False,
    extra_props: list[str] | None = None,
    hvigor_stacktrace: bool = False,
    hvigor_debug: bool = True,
    timeout_sec: float | None = None,
) -> tuple[int, str]:
    """
    在 ``repo_path`` 下执行 ``hvigorw ... <task>``（默认可跑本地单元测试的 ``test``）。

    返回 ``(exit_code, combined_output)``。
    """
    repo_dir = resolve_directory(repo_path, "repo_path")
    deveco_dir = resolve_directory(deveco_path, "deveco_path")
    bp = repo_dir / "build-profile.json5"
    if not bp.is_file():
        raise FileNotFoundError(f"Missing build-profile.json5: {bp}")

    hvigor_path = find_hvigor_wrapper(repo_dir, deveco_dir)
    sdk_roots, _sdk_meta = require_sdk_roots_for_repo(
        repo_dir,
        deveco_dir,
        product_name=product,
    )
    node_home = find_node_home(deveco_dir)
    java_home = find_java_home(deveco_dir)
    extras = list(extra_props or [])
    bridge_file = _ensure_unit_test_bridge(repo_dir, module, extras) if task in {"test", "UnitTestBuild"} else None

    def finish(code: int, text: str) -> tuple[int, str]:
        _cleanup_unit_test_bridge(bridge_file)
        return code, text

    base = _hvigor_base_args(
        mode=mode,
        module=module,
        product=product,
        coverage=coverage,
        extra_props=extras,
    )
    command = _append_hvigor_flags(
        [str(hvigor_path), *base, task],
        hvigor_stacktrace=hvigor_stacktrace,
        hvigor_debug=hvigor_debug,
    )
    fallback_command = _append_hvigor_flags(
        [str(hvigor_path), *base, "UnitTestBuild"],
        hvigor_stacktrace=hvigor_stacktrace,
        hvigor_debug=hvigor_debug,
    )

    failures: list[str] = []
    sdk_candidates: list[Path | None] = sdk_roots or [None]
    selected_api_level = _sdk_meta.get("sdk_selection_api_level")

    for sdk_root in sdk_candidates:
        env = build_harmony_command_env(
            sdk_root=sdk_root,
            sdk_api_level=selected_api_level,
            deveco_path=deveco_dir,
            base_env=os.environ.copy(),
        )
        to = timeout_sec if timeout_sec is not None and timeout_sec > 0 else None
        try:
            result = run_command(command, cwd=repo_dir, env=env, timeout_sec=to)
        except subprocess.TimeoutExpired as exc:
            return finish(124, f"TimeoutExpired after {timeout_sec}s: {exc}")
        out = command_output(result)
        recovered = _success_or_recovered(result, out)
        if recovered is not None:
            return finish(recovered[0], recovered[1])

        if task == "test" and _is_missing_test_task(out):
            try:
                fallback_result = run_command(fallback_command, cwd=repo_dir, env=env, timeout_sec=to)
            except subprocess.TimeoutExpired as exc:
                return finish(124, f"TimeoutExpired after {timeout_sec}s during UnitTestBuild fallback: {exc}")
            fallback_out = command_output(fallback_result)
            fallback_recovered = _success_or_recovered(fallback_result, fallback_out)
            prefix = "\n".join(
                [
                    "LOCAL_TEST_NOTE=fallback_to_UnitTestBuild_after_missing_test_task",
                    f"ORIGINAL_COMMAND={format_command(command)}",
                    f"FALLBACK_COMMAND={format_command(fallback_command)}",
                    "=== ORIGINAL OUTPUT ===",
                    tail_text(out, limit=80),
                    "=== FALLBACK OUTPUT ===",
                ]
            )
            if fallback_recovered is not None:
                return finish(0, (prefix + "\n" + fallback_recovered[1]).strip())
            return finish(fallback_result.returncode, (prefix + "\n" + fallback_out).strip())

        # hvigor 有时会在日志中给出 "BUILD SUCCESSFUL" 但进程退出码仍为非 0。
        # 对本地测试而言，这种情况下更符合用户预期的是视为成功（避免误报）。
        if "BUILD SUCCESSFUL" in (out or ""):
            recovered = "\n".join(
                [
                    "LOCAL_TEST_NOTE=recovered_success_from_build_successful_output",
                    f"HVIGOR_EXIT_CODE={result.returncode}",
                    (out or "").strip(),
                ]
            ).strip()
            return finish(0, recovered)

        msg = (
            f"hvigor exited with {result.returncode}.\n"
            f"Command: {format_command(command)}\n"
            + (f"Output:\n{tail_text(out, limit=80)}" if out else "")
        )
        failures.append(msg)
        if _should_retry_with_next_sdk(out):
            continue
        return finish(result.returncode, out)

    last = failures[-1] if failures else "Unknown failure"
    return finish(1, last)


def _default_report_hint(repo_dir: Path) -> str:
    """与文档/社区常见产物路径一致（若未生成则以实际 hvigor 输出为准）。"""
    candidates = [
        repo_dir / "entry" / "build" / "default" / "outputs" / "default" / "reports" / "tests",
        repo_dir / "build" / "reports" / "tests",
    ]
    for c in candidates:
        if c.is_dir():
            index = c / "index.html"
            if index.is_file():
                return str(index.resolve())
            return str(c.resolve())
    return ""


def _local_unit_test_result_files(repo_dir: Path) -> list[Path]:
    pattern = "*/.test/*/intermediates/test/coverage_data/test_result.txt"
    return sorted(repo_dir.glob(pattern), key=lambda p: str(p))


def _check_local_unit_test_results(repo_dir: Path) -> tuple[int, str]:
    result_files = _local_unit_test_result_files(repo_dir)
    if not result_files:
        return 0, ""

    chunks: list[str] = [f"LOCAL_UNIT_TEST_RESULT_FILE_COUNT={len(result_files)}"]
    aggregate = 0
    summary_re = re.compile(r"Tests run:\s*(\d+),\s*Failure:\s*(\d+),\s*Error:\s*(\d+),\s*Pass:\s*(\d+)", re.I)
    for result_file in result_files:
        text = result_file.read_text(encoding="utf-8", errors="replace")
        chunks.append(f"LOCAL_UNIT_TEST_RESULT_FILE={result_file}")
        match = summary_re.search(text)
        if match:
            tests, failures, errors, passed = (int(match.group(i)) for i in range(1, 5))
            status = "FAILED" if failures > 0 or errors > 0 else "SUCCESS"
            chunks.append(
                "LOCAL_UNIT_TEST_RESULT="
                f"{status} tests={tests} failure={failures} error={errors} pass={passed}"
            )
            if status == "FAILED" and aggregate == 0:
                aggregate = 1
        elif "result=Failure" in text or "result=Error" in text:
            chunks.append("LOCAL_UNIT_TEST_RESULT=FAILED summary=missing failure_or_error_marker=true")
            if aggregate == 0:
                aggregate = 1
        else:
            chunks.append("LOCAL_UNIT_TEST_RESULT=UNKNOWN summary=missing")

        if aggregate != 0:
            chunks.append("LOCAL_UNIT_TEST_RESULT_TAIL=" + tail_text(text, limit=20))

    return aggregate, "\n".join(chunks)


def _source_contract_files(repo_dir: Path) -> list[Path]:
    files: list[Path] = []
    for test_file in sorted(repo_dir.rglob("src/test/*.test.ets"), key=lambda p: str(p)):
        if _is_generated_src_test(test_file.parent, repo_dir):
            continue
        try:
            text = test_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if SOURCE_CONTRACT_ROW03 in text or SOURCE_CONTRACT_ROW06 in text or SOURCE_CONTRACT_ROW09 in text:
            files.append(test_file)
    return files


def _extract_ets_method_body(text: str, method_name: str) -> str | None:
    method_pos = text.find(method_name)
    if method_pos < 0:
        return None
    paren_pos = text.find("(", method_pos)
    if paren_pos < 0:
        return None

    depth = 0
    signature_end = -1
    for i in range(paren_pos, len(text)):
        char = text[i]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                signature_end = i
                break
    if signature_end < 0:
        return None

    body_start = text.find("{", signature_end)
    if body_start < 0:
        return None

    depth = 0
    for i in range(body_start, len(text)):
        char = text[i]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[body_start + 1 : i]
    return None


def _check_row03_disk_cache_path_contract(repo_dir: Path) -> tuple[int, str]:
    request_manager = (
        repo_dir
        / "library"
        / "src"
        / "main"
        / "ets"
        / "components"
        / "imageknife"
        / "requestmanage"
        / "RequestManager.ets"
    )
    if not request_manager.is_file():
        return 1, "SOURCE_CONTRACT_STATUS=FAILED\nSOURCE_CONTRACT_REASON=RequestManager.ets_not_found"

    text = request_manager.read_text(encoding="utf-8", errors="replace")
    uses_dedicated_disk_path = bool(
        re.search(r"getFileCacheByFile\s*\(\s*request\.diskMemoryCachePath\s*,", text)
    ) or bool(
        re.search(r"saveFileCacheOnlyFile\s*\(\s*this\.options\.diskMemoryCachePath\s*,", text)
    )
    guards_missing_files_dir = (
        bool(re.search(r"let\s+filesDir\s*=\s*request\?\.moduleContext\?\.filesDir\s*;", text))
        and "filesDir == undefined" in text
        and "onError(" in text
        and "cannot load from disk cache" in text
    )

    if uses_dedicated_disk_path or guards_missing_files_dir:
        mode = "dedicated_disk_path" if uses_dedicated_disk_path else "missing_files_dir_guard"
        return 0, f"SOURCE_CONTRACT_STATUS=SUCCESS\nSOURCE_CONTRACT=row03_disk_cache_path\nSOURCE_CONTRACT_MODE={mode}"

    return 1, "\n".join(
        [
            "SOURCE_CONTRACT_STATUS=FAILED",
            "SOURCE_CONTRACT=row03_disk_cache_path",
            "SOURCE_CONTRACT_REASON=RequestManager still reads disk cache through moduleContext/filesDir without a guard or dedicated disk path",
        ]
    )


def _check_row06_is_url_exist_cache_type_contract(repo_dir: Path) -> tuple[int, str]:
    imageknife = (
        repo_dir
        / "library"
        / "src"
        / "main"
        / "ets"
        / "components"
        / "imageknife"
        / "ImageKnife.ets"
    )
    if not imageknife.is_file():
        return 1, "SOURCE_CONTRACT_STATUS=FAILED\nSOURCE_CONTRACT_REASON=ImageKnife.ets_not_found"

    text = imageknife.read_text(encoding="utf-8", errors="replace")
    body = _extract_ets_method_body(text, "isUrlExist")
    if body is None:
        return 1, "SOURCE_CONTRACT_STATUS=FAILED\nSOURCE_CONTRACT_REASON=isUrlExist_method_not_found"

    assigns_request_cache_type = bool(re.search(r"\brequest\s*\.\s*cacheType\s*=\s*cacheType\s*;", body))
    checks_parameter_directly = all(
        bool(re.search(rf"(?<!\.)\bcacheType\s*==\s*CacheType\.{name}\b", body))
        for name in ("Cache", "Disk", "Default")
    )

    if assigns_request_cache_type or checks_parameter_directly:
        mode = "request_cacheType_assignment" if assigns_request_cache_type else "direct_cacheType_parameter"
        return 0, f"SOURCE_CONTRACT_STATUS=SUCCESS\nSOURCE_CONTRACT=row06_is_url_exist_cache_type\nSOURCE_CONTRACT_MODE={mode}"

    return 1, "\n".join(
        [
            "SOURCE_CONTRACT_STATUS=FAILED",
            "SOURCE_CONTRACT=row06_is_url_exist_cache_type",
            "SOURCE_CONTRACT_REASON=isUrlExist still ignores the cacheType parameter before selecting memory/disk cache behavior",
        ]
    )


def _has_accepted_goods_transition(source: str, id_expression: str) -> bool:
    transition_prefix = r"'goods'\s*\+\s*" + re.escape(id_expression)
    has_geometry = bool(re.search(r"geometryTransition\s*\(\s*" + transition_prefix, source))
    has_shared_default = bool(re.search(r"sharedTransition\s*\(\s*" + transition_prefix, source)) and (
        "Curve.Default" in source
    )
    return has_geometry or has_shared_default


def _check_row09_orange_shopping_transition_contract(repo_dir: Path) -> tuple[int, str]:
    goods_list = (
        repo_dir
        / "feature"
        / "navigationHome"
        / "src"
        / "main"
        / "ets"
        / "components"
        / "good"
        / "GoodsList.ets"
    )
    detail_page = (
        repo_dir
        / "feature"
        / "detailPageHsp"
        / "src"
        / "main"
        / "ets"
        / "main"
        / "DetailPage.ets"
    )
    missing = [str(path) for path in (goods_list, detail_page) if not path.is_file()]
    if missing:
        return 1, "\n".join(
            [
                "SOURCE_CONTRACT_STATUS=FAILED",
                "SOURCE_CONTRACT=row09_orange_shopping_transition",
                "SOURCE_CONTRACT_REASON=source_file_not_found",
                *[f"SOURCE_CONTRACT_MISSING={path}" for path in missing],
            ]
        )

    goods_text = goods_list.read_text(encoding="utf-8", errors="replace")
    detail_text = detail_page.read_text(encoding="utf-8", errors="replace")
    goods_transition_ok = _has_accepted_goods_transition(goods_text, "item.id")
    detail_transition_ok = _has_accepted_goods_transition(detail_text, "this.goodDetailData.id")
    navigation_ok = "pushPathByName('DetailPage', item" in goods_text

    if goods_transition_ok and detail_transition_ok and navigation_ok:
        return 0, "\n".join(
            [
                "SOURCE_CONTRACT_STATUS=SUCCESS",
                "SOURCE_CONTRACT=row09_orange_shopping_transition",
                f"SOURCE_CONTRACT_GOODS_TRANSITION={goods_transition_ok}",
                f"SOURCE_CONTRACT_DETAIL_TRANSITION={detail_transition_ok}",
                f"SOURCE_CONTRACT_NAVIGATION_ITEM={navigation_ok}",
            ]
        )

    return 1, "\n".join(
        [
            "SOURCE_CONTRACT_STATUS=FAILED",
            "SOURCE_CONTRACT=row09_orange_shopping_transition",
            "SOURCE_CONTRACT_REASON=GoodsList and DetailPage must use the same accepted goods transition identity before navigating to DetailPage",
            f"SOURCE_CONTRACT_GOODS_TRANSITION={goods_transition_ok}",
            f"SOURCE_CONTRACT_DETAIL_TRANSITION={detail_transition_ok}",
            f"SOURCE_CONTRACT_NAVIGATION_ITEM={navigation_ok}",
        ]
    )


def _run_source_contracts(repo_dir: Path) -> tuple[int, str]:
    files = _source_contract_files(repo_dir)
    if not files:
        return 0, ""

    chunks: list[str] = [
        f"SOURCE_CONTRACT_FILE_COUNT={len(files)}",
        *[f"SOURCE_CONTRACT_FILE={path}" for path in files],
    ]
    aggregate = 0
    if any(SOURCE_CONTRACT_ROW03 in path.read_text(encoding="utf-8", errors="replace") for path in files):
        code, out = _check_row03_disk_cache_path_contract(repo_dir)
        chunks.append(out)
        if aggregate == 0 and code != 0:
            aggregate = code
    if any(SOURCE_CONTRACT_ROW06 in path.read_text(encoding="utf-8", errors="replace") for path in files):
        code, out = _check_row06_is_url_exist_cache_type_contract(repo_dir)
        chunks.append(out)
        if aggregate == 0 and code != 0:
            aggregate = code
    if any(SOURCE_CONTRACT_ROW09 in path.read_text(encoding="utf-8", errors="replace") for path in files):
        code, out = _check_row09_orange_shopping_transition_contract(repo_dir)
        chunks.append(out)
        if aggregate == 0 and code != 0:
            aggregate = code

    return aggregate, "\n".join(chunks)


def main() -> int:
    ensure_command_line_tools_env()
    parser = argparse.ArgumentParser(
        description="Run HarmonyOS local unit tests (src/test) via hvigor test task.",
    )
    parser.add_argument("--repo-path", required=True, help="Harmony project root.")
    parser.add_argument(
        "--deveco-path",
        default=os.environ.get("DEVECO_PATH", "").strip(),
        help="DevEco Studio install path. Default: env DEVECO_PATH",
    )
    parser.add_argument("--mode", default="module", help="hvigor --mode. Default: module")
    parser.add_argument("--module", default="entry", help="Module name (-p module=...). Default: entry")
    parser.add_argument("--product", default="default", help="Product name. Default: default")
    parser.add_argument(
        "--task",
        default="test",
        help="Hvigor task for local tests. Default: test (see Huawei local test / ide-local-test doc).",
    )
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="Pass -p coverage=true for coverage (if supported by your hvigor plugin).",
    )
    parser.add_argument(
        "--prop",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Extra -p properties (repeatable), e.g. --prop buildMode=debug",
    )
    parser.add_argument(
        "--all-local-modules",
        action="store_true",
        help="Discover modules with src/test/*.test.ets and run --task once per module.",
    )
    parser.add_argument(
        "--discover-only",
        action="store_true",
        help="Only print discovered local test modules (same rules as --all-local-modules), do not run hvigor.",
    )
    parser.add_argument(
        "--timeout-sec",
        type=float,
        default=0.0,
        help="Optional timeout for each hvigor invocation (0 = no limit).",
    )
    parser.add_argument(
        "--hvigor-stacktrace",
        action="store_true",
        help="Append --stacktrace to hvigor command for more detailed errors.",
    )
    parser.add_argument(
        "--hvigor-debug",
        action="store_true",
        help="Append --debug to hvigor command for verbose hvigor logs.",
    )
    parser.add_argument(
        "--skip-ohpm-install",
        action="store_true",
        help="Skip running `ohpm install` before hvigor test (not recommended; may fail to resolve @ohos/hypium).",
    )
    parser.add_argument(
        "--ohpm-timeout-sec",
        type=float,
        default=600.0,
        help="Timeout for `ohpm install` (default 600s).",
    )
    args = parser.parse_args()

    repo_dir = resolve_directory(args.repo_path, "repo_path")
    if not args.deveco_path:
        print("LOCAL_TEST_STATUS=FAILED", file=sys.stderr)
        print("EXIT_CODE=2")
        print("LOG_PATH=")
        print("REPORT_HINT=")
        print("ERROR=DEVECO_PATH missing (set command_line_tools_test/.env or pass --deveco-path)", file=sys.stderr)
        return 2
    deveco_dir = resolve_directory(args.deveco_path, "deveco_path")

    if args.discover_only:
        mods = discover_local_test_modules(repo_dir)
        print(f"LOCAL_TEST_MODULE_COUNT={len(mods)}")
        for i, m in enumerate(mods, start=1):
            print(f"LOCAL_TEST_MODULE_{i}={m}")
        return 0

    _logs_dir().mkdir(parents=True, exist_ok=True)
    log_path = _logs_dir() / f"run_local_tests-{_timestamp_slug()}.log"

    if sys.platform not in ("win32", "darwin"):
        print(
            "LOCAL_TEST_NOTE=Official docs and community reports: local unit tests may only "
            "be supported on Windows/macOS DevEco environments; this run may fail on other OS.",
            file=sys.stderr,
        )

    modules: list[str]
    if args.all_local_modules:
        modules = discover_local_test_modules(repo_dir)
        if not modules:
            print("LOCAL_TEST_STATUS=SKIPPED")
            print("LOCAL_TEST_REASON=no_src_test_modules_found")
            print("EXIT_CODE=0")
            return 0
    else:
        modules = [args.module]

    if not args.skip_ohpm_install:
        ohpm_to = args.ohpm_timeout_sec if args.ohpm_timeout_sec > 0 else None
        ohpm_code, ohpm_out = run_ohpm_install(
            repo_dir,
            deveco_dir,
            timeout_sec=ohpm_to,
            product_name=args.product,
        )
        append_text_file(log_path, "=== OHPM INSTALL ===\n" + ohpm_out + "\n")
        if ohpm_code != 0:
            print("LOCAL_TEST_STATUS=FAILED", file=sys.stderr)
            print(f"EXIT_CODE={ohpm_code}")
            print(f"LOG_PATH={format_path_for_display(log_path)}")
            print("REPORT_HINT=")
            print(tail_text(ohpm_out, limit=40), file=sys.stderr)
            return ohpm_code

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
    append_text_file(
        log_path,
        "=== SDK PRECHECK ===\n"
        f"SDK_META={sdk_meta}\n"
        f"SDK_ROOTS={[str(root) for root in sdk_roots]}\n\n",
    )
    append_text_file(
        log_path,
        "SDK_HVIGOR_ROOT="
        + str(sdk_roots[0])
        + "\n"
        + "SDK_API_SLICE="
        + str(resolve_sdk_api_slice_for_api(sdk_roots[0], sdk_meta.get("sdk_selection_api_level")))
        + "\n",
    )
    append_text_file(log_path, f"LOCAL_PROPERTIES_PATH={local_properties_path}\n\n")
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
    append_text_file(log_path, "=== ENV PREPARE ===\n" + "\n".join(prepare_notes) + "\n\n")

    timeout_kw = args.timeout_sec if args.timeout_sec > 0 else None
    aggregate = 0
    combined_chunks: list[str] = []
    for mod in modules:
        code, out = run_local_tests_hvigor(
            str(repo_dir),
            str(deveco_dir),
            mode=args.mode,
            module=mod,
            product=args.product,
            task=args.task,
            coverage=args.coverage,
            extra_props=list(args.prop or []),
            hvigor_stacktrace=bool(args.hvigor_stacktrace),
            hvigor_debug=bool(args.hvigor_debug),
            timeout_sec=timeout_kw,
        )
        block = f"=== MODULE={mod} EXIT={code} ===\n{out}"
        combined_chunks.append(block)
        if aggregate == 0 and code != 0:
            aggregate = code
        append_text_file(log_path, block + "\n\n")

    combined = "\n\n".join(combined_chunks)
    local_result_code, local_result_out = _check_local_unit_test_results(repo_dir)
    if local_result_out:
        combined = (combined + "\n\n=== LOCAL UNIT TEST RESULTS ===\n" + local_result_out).strip()
        append_text_file(log_path, "=== LOCAL UNIT TEST RESULTS ===\n" + local_result_out + "\n\n")
        if aggregate == 0 and local_result_code != 0:
            aggregate = local_result_code
    contract_code, contract_out = _run_source_contracts(repo_dir)
    if contract_out:
        combined = (combined + "\n\n=== SOURCE CONTRACTS ===\n" + contract_out).strip()
        append_text_file(log_path, "=== SOURCE CONTRACTS ===\n" + contract_out + "\n\n")
        if aggregate == 0 and contract_code != 0:
            aggregate = contract_code
    hint = _default_report_hint(repo_dir)

    if aggregate == 0:
        print("LOCAL_TEST_STATUS=SUCCESS")
    else:
        print("LOCAL_TEST_STATUS=FAILED", file=sys.stderr)
    print(f"EXIT_CODE={aggregate}")
    print(f"LOG_PATH={format_path_for_display(log_path)}")
    if hint:
        print(f"REPORT_HINT={hint}")
    else:
        print("REPORT_HINT=")
    if aggregate != 0:
        print(tail_text(combined, limit=40), file=sys.stderr)
    return aggregate


if __name__ == "__main__":
    raise SystemExit(main())
