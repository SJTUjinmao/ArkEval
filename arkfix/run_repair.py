#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPAIR_ENGINE_ROOT = Path(
    os.environ.get(
        "ARKEVAL_REPAIR_ENGINE_ROOT",
        Path(__file__).resolve().parent / "repair_engine",
    )
)
DEFAULT_REPO_POOL = ROOT / "depend" / "repair_repo"
DEFAULT_SCOPED_DATASET_DIR = ROOT / "dataset" / "test_out"
DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parent / "outputs"
DEFAULT_BENCHMARK_DATASET = ROOT / "dataset" / "arkeval_dataset.jsonl"

_TEST_ISSUE_FORWARD = re.compile(
    r"(?:修复|修改|整改|适配|新增|增加|添加|覆盖).{0,24}(?:测试|用例|自测试|自动化|流水线|xts)",
    re.IGNORECASE,
)
_TEST_ISSUE_REVERSE = re.compile(
    r"(?:测试|用例|自测试|自动化|流水线|xts).{0,24}(?:修复|修改|整改|适配|新增|增加|添加|覆盖|问题|报错|失败|不通过|框架)",
    re.IGNORECASE,
)
_TEST_ISSUE_ENGLISH_FORWARD = re.compile(
    r"\b(?:fix|modify|update|add|adapt|rectif\w*|cover)\w*\b.{0,80}\b(?:test|tests|testing|case|cases|pipeline|xts)\b",
    re.IGNORECASE,
)
_TEST_ISSUE_ENGLISH_REVERSE = re.compile(
    r"\b(?:test|tests|testing|case|cases|pipeline|xts)\b.{0,80}\b(?:fix|modify|update|add|adapt|rectif\w*|error|fail|bug|framework)\w*\b",
    re.IGNORECASE,
)
_TEST_BODY_ENGLISH_FORWARD = re.compile(
    r"\b(?:fix|modify|update|add|adapt|rectif\w*|cover)\w*\s+(?:the\s+)?(?:test cases?|automated tests?|self-tests?|test framework|testing pipeline|xts)\b",
    re.IGNORECASE,
)
_TEST_BODY_ENGLISH_REVERSE = re.compile(
    r"\b(?:test cases?|automated tests?|self-tests?|test framework|testing pipeline|xts)\b.{0,80}\b(?:need|needs|must|should|require|requires)\b.{0,40}\b(?:be\s+)?(?:fix|modify|update|add|adapt|rectif|cover)\w*\b",
    re.IGNORECASE,
)
_NON_TEST_BODY_MARKERS = (
    "测试用例本身存在问题暂不处理",
    "测试用例测试结果：评估不涉及",
    "lacks specific test case validation",
    "lacks test case validation",
    "compilation test passed",
)


def default_repair_python() -> str:
    current = Path(sys.executable).resolve()
    prefix = Path(sys.prefix).resolve()
    if prefix.parent.name.casefold() == "envs":
        base_python = prefix.parent.parent / current.name
        if base_python.is_file():
            return str(base_python)
    return str(current)


def claim_run_directory(run_dir: Path) -> None:
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise FileExistsError(f"repair run already exists; choose a new --timestamp: {run_dir}") from exc


def parse_row_spec(spec: str, *, max_row: int) -> list[int]:
    if not spec.strip():
        return list(range(1, max_row + 1))
    rows: set[int] = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            left, right = chunk.split("-", 1)
            start = int(left)
            end = int(right)
            if start > end:
                raise ValueError(f"bad row range: {chunk}")
            rows.update(range(start, end + 1))
        else:
            rows.add(int(chunk))
    bad = [row for row in sorted(rows) if row < 1 or row > max_row]
    if bad:
        raise ValueError(f"rows out of range 1..{max_row}: {bad}")
    return sorted(rows)


def latest_arkfix_input() -> Path:
    outputs = ROOT / "localization" / "outputs"
    candidates = sorted(
        outputs.glob("*/*/arkfix_input.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(
            "no localization arkfix_input.jsonl found; pass --dataset or run localization/run_localization.py first"
        )

    valid: list[tuple[int, int, Path]] = []
    newest_error = ""
    for path in candidates:
        try:
            records = load_jsonl(path)
            manifest_path = path.with_name("manifest.json")
            with manifest_path.open("r", encoding="utf-8") as handle:
                manifest = json.load(handle)
            if not isinstance(manifest, dict):
                raise ValueError("manifest root is not an object")
            declared_rows = manifest.get("rows")
            if not isinstance(declared_rows, list) or any(type(row) is not int for row in declared_rows):
                raise ValueError("manifest rows must be an integer list")
            if len(set(declared_rows)) != len(declared_rows):
                raise ValueError("manifest rows contain duplicates")
            actual_rows = [
                row_number_for_record(line_number, record)
                for line_number, record in enumerate(records, 1)
            ]
            if declared_rows != actual_rows:
                raise ValueError(
                    f"manifest rows do not match JSONL rows ({len(declared_rows)} != {len(actual_rows)})"
                )
            valid.append((len(actual_rows), path.stat().st_mtime_ns, path))
        except (OSError, UnicodeError, ValueError) as exc:
            if not newest_error:
                newest_error = f"{path}: {exc}"

    if not valid:
        raise FileNotFoundError(
            f"no complete UTF-8/JSON localization input with matching manifest rows; newest failure: {newest_error}"
        )
    return max(valid, key=lambda item: (item[0], item[1]))[2]


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line:
                raise ValueError(f"blank JSONL line {line_number}: {path}")
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL line {line_number}: {path}: {exc}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"JSONL line {line_number} is not an object: {path}")
            rows.append(record)
    if not rows:
        raise ValueError(f"dataset has no records: {path}")
    return rows


def normalize_repo_relative_path(path: str) -> str:
    text = str(path or "").replace("\\", "/").strip()
    if not text or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return ""
    while text.startswith("./"):
        text = text[2:]
    parts: list[str] = []
    for part in text.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            return ""
        parts.append(part)
    return "/".join(parts)


def is_test_patch_path(path: str) -> bool:
    normalized = normalize_repo_relative_path(path).lower()
    scoped = f"/{normalized}"
    return (
        "/src/test/" in scoped
        or "/src/ohostest/" in scoped
        or normalized.endswith(".test.ets")
    )


def issue_explicitly_targets_tests(record: dict[str, Any]) -> bool:
    def matches_title(text: str) -> bool:
        return any(
            pattern.search(text)
            for pattern in (
                _TEST_ISSUE_FORWARD,
                _TEST_ISSUE_REVERSE,
                _TEST_ISSUE_ENGLISH_FORWARD,
                _TEST_ISSUE_ENGLISH_REVERSE,
            )
        )

    title = str(record.get("title") or "").strip()
    if matches_title(title):
        return True
    body = str(record.get("body") or "").strip()
    body_lower = body.lower()
    if any(marker.lower() in body_lower for marker in _NON_TEST_BODY_MARKERS):
        return False
    return any(
        pattern.search(body)
        for pattern in (
            _TEST_ISSUE_FORWARD,
            _TEST_ISSUE_REVERSE,
            _TEST_BODY_ENGLISH_FORWARD,
            _TEST_BODY_ENGLISH_REVERSE,
        )
    )


def patch_changed_paths(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        values: list[str] = []
        if line.startswith("diff --git "):
            try:
                parts = shlex.split(line)
            except ValueError:
                parts = line.split()
            values = parts[2:4]
        elif line.startswith("--- ") or line.startswith("+++ "):
            value = line[4:].split("\t", 1)[0].strip()
            if value != "/dev/null":
                values = [value]
        for value in values:
            normalized = normalize_repo_relative_path(re.sub(r"^[ab]/", "", value))
            if normalized and normalized not in paths:
                paths.append(normalized)
    return paths


def derive_test_patch_policy(record: dict[str, Any]) -> tuple[bool, str]:
    issue = issue_explicitly_targets_tests(record)
    gold_fix = any(is_test_patch_path(path) for path in patch_changed_paths(str(record.get("fix_patch") or "")))
    if issue and gold_fix:
        return True, "issue+gold_fix"
    if issue:
        return True, "issue"
    if gold_fix:
        return True, "gold_fix"
    return False, "none"


def load_test_patch_policies(
    path: Path = DEFAULT_BENCHMARK_DATASET,
) -> dict[str, tuple[bool, str]]:
    policies: dict[str, tuple[bool, str]] = {}
    for line_number, record in enumerate(load_jsonl(path), 1):
        instance_id = str(record.get("instance_id") or "").strip()
        if not instance_id:
            raise ValueError(f"gold dataset line {line_number} has no instance_id: {path}")
        if instance_id in policies:
            raise ValueError(f"duplicate instance_id in gold dataset: {instance_id}")
        policies[instance_id] = derive_test_patch_policy(record)
    return policies


def absolute_paths_to_relative(record: dict[str, Any]) -> list[str]:
    localization = record.get("_localization")
    repo_root_raw = ""
    if isinstance(localization, dict):
        repo_root_raw = str(localization.get("repo_root") or "")
    if not repo_root_raw:
        return []
    repo_root = Path(repo_root_raw).resolve()
    out: list[str] = []
    for raw in record.get("localized_file_abs_paths") or []:
        try:
            rel = Path(str(raw)).resolve().relative_to(repo_root).as_posix()
        except (OSError, ValueError):
            continue
        normalized = normalize_repo_relative_path(rel)
        if normalized and normalized not in out:
            out.append(normalized)
    return out


def localized_defect_files(record: dict[str, Any]) -> list[str]:
    raw = record.get("localized_defect_files")
    out: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            normalized = normalize_repo_relative_path(str(item))
            if normalized and normalized not in out:
                out.append(normalized)
    if out:
        return out
    return absolute_paths_to_relative(record)


def row_number_for_record(line_number: int, record: dict[str, Any]) -> int:
    value = record.get("row")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    localization = record.get("_localization")
    if isinstance(localization, dict):
        value = localization.get("row")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return line_number


def parse_selected_rows(spec: str, *, available_rows: list[int]) -> list[int]:
    available = sorted(set(available_rows))
    if not available:
        return []
    if not spec.strip():
        return available
    selected: set[int] = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            left, right = chunk.split("-", 1)
            start = int(left)
            end = int(right)
            if start > end:
                raise ValueError(f"bad row range: {chunk}")
            selected.update(range(start, end + 1))
        else:
            selected.add(int(chunk))
    missing = sorted(selected.difference(available))
    if missing:
        raise ValueError(f"selected rows are not present in localization input: {missing}")
    return sorted(selected)


def parse_instance_id(instance_id: str) -> tuple[str, str, int]:
    left, _, tail = instance_id.partition("+")
    org, sep, repo = left.partition("__")
    if not sep or not org or not repo:
        return "unknown", "", 0
    number = 0
    if "-" in tail:
        maybe_number = tail.rsplit("-", 1)[-1]
        if maybe_number.isdigit():
            number = int(maybe_number)
    return org, repo, number


def problem_to_title_body(problem: str) -> tuple[str, str]:
    text = (problem or "").strip()
    if not text:
        return "Localized ArkTS repair task", ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and stripped.lower() != "title:":
            return stripped[:200], text
    return text[:200], text


def localized_paths_for_repair(record: dict[str, Any]) -> tuple[list[str], str]:
    raw = record.get("localized_file_rel_paths")
    out: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            normalized = normalize_repo_relative_path(str(item))
            if normalized and normalized not in out:
                out.append(normalized)
    if out:
        return out, "localized_file_rel_paths"
    fallback = localized_defect_files(record)
    if fallback:
        return fallback, "localized_defect_files"
    return [], ""


def scoped_repair_record(
    record: dict[str, Any],
    *,
    line_number: int,
) -> tuple[dict[str, Any], str]:
    instance_id = str(record.get("instance_id") or "").strip()
    if not instance_id:
        raise ValueError(f"input line {line_number} has no instance_id")

    org, repo_from_id, number_from_id = parse_instance_id(instance_id)
    repo = str(record.get("repo") or repo_from_id).strip()
    if not repo or repo in {".", ".."} or Path(repo).name != repo or "/" in repo or "\\" in repo:
        raise ValueError(f"input line {line_number} ({instance_id}) has unsafe repo name: {repo!r}")
    number_raw = record.get("number")
    number = number_raw if isinstance(number_raw, int) else number_from_id
    if not isinstance(number, int) or number <= 0:
        number = line_number

    base = record.get("base")
    if isinstance(base, dict):
        base_sha = str(base.get("sha") or "").strip()
        base_label = str(base.get("label") or f"{org}:{base_sha[:8]}").strip()
        base_ref = str(base.get("ref") or base_sha).strip()
    else:
        base_sha = str(record.get("base_sha") or record.get("base_commit") or "").strip()
        base_label = f"{org}:{base_sha[:8]}" if base_sha else f"{org}:unknown"
        base_ref = base_sha or "unknown"
    if not base_sha:
        raise ValueError(f"input line {line_number} ({instance_id}) has no base sha")

    problem = str(record.get("problem") or record.get("problem_statement") or "").strip()
    title = str(record.get("title") or "").strip()
    body = str(record.get("body") or "").strip()
    if problem and not (title or body):
        title, body = problem_to_title_body(problem)
    if not title:
        title = "Localized ArkTS repair task"

    resolved = record.get("resolved_issues")
    if not isinstance(resolved, list) or not resolved:
        resolved = [{"number": number, "title": title, "body": body or problem}]

    defect_files, source = localized_paths_for_repair(record)
    if not defect_files:
        raise ValueError(f"input line {line_number} ({instance_id}) has no localized repair scope")

    scoped = {
        "org": str(record.get("org") or org),
        "repo": repo,
        "number": number,
        "state": str(record.get("state") or "closed"),
        "title": title,
        "body": body,
        "base": {
            "label": base_label,
            "ref": base_ref,
            "sha": base_sha,
        },
        "resolved_issues": resolved,
        "fix_patch": "",
        "test_patch": "",
        "fixed_tests": {},
        "p2p_tests": {},
        "f2p_tests": {},
        "s2p_tests": {},
        "n2p_tests": {},
        "run_result": {"passed_count": 0, "failed_count": 0, "skipped_count": 0, "passed_tests": [], "failed_tests": [], "skipped_tests": []},
        "test_patch_result": {"passed_count": 0, "failed_count": 0, "skipped_count": 0, "passed_tests": [], "failed_tests": [], "skipped_tests": []},
        "fix_patch_result": {"passed_count": 0, "failed_count": 0, "skipped_count": 0, "passed_tests": [], "failed_tests": [], "skipped_tests": []},
        "instance_id": instance_id,
        "hints": str(record.get("hints") or ""),
        "defect_files": defect_files,
        "language": str(record.get("language") or "arkts"),
    }
    project_path = ""
    raw_project_path = record.get("project_path")
    if isinstance(raw_project_path, str) and raw_project_path.strip():
        project_path = normalize_repo_relative_path(raw_project_path)
        if not project_path:
            raise ValueError(
                f"input line {line_number} ({instance_id}) has unsafe project_path: {raw_project_path!r}"
            )
    if project_path:
        scoped["project_path"] = project_path
    return scoped, source


def build_scoped_dataset(
    records: list[dict[str, Any]],
    *,
    selected_rows: list[int],
    allow_original_defect_files: bool,
    test_patch_policies: dict[str, tuple[bool, str]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    policies = test_patch_policies or load_test_patch_policies()
    selected_set = set(selected_rows)
    scoped: list[dict[str, Any]] = []
    mapping: list[dict[str, Any]] = []

    for line_number, record in enumerate(records, 1):
        original_row = row_number_for_record(line_number, record)
        if original_row not in selected_set:
            continue
        try:
            next_record, source = scoped_repair_record(
                record,
                line_number=line_number,
            )
        except ValueError:
            if not allow_original_defect_files:
                raise
            next_record = dict(record)
            existing = [
                normalize_repo_relative_path(str(item))
                for item in record.get("defect_files") or []
                if normalize_repo_relative_path(str(item))
            ]
            next_record["defect_files"] = existing
            source = "original_defect_files"
        instance_id = str(next_record.get("instance_id") or "").strip()
        policy = policies.get(instance_id)
        if policy is None:
            raise ValueError(f"no gold test-patch policy for instance_id: {instance_id}")
        next_record["fix_patch"] = ""
        next_record["test_patch"] = ""
        next_record["allow_test_patch"] = policy[0]
        next_record["allow_test_patch_reason"] = policy[1]
        next_record["_arkeval_original_row"] = original_row
        scoped.append(next_record)
        mapping.append(
            {
                "scoped_line": len(scoped),
                "original_row": original_row,
                "instance_id": next_record.get("instance_id", ""),
                "repo": next_record.get("repo", ""),
                "defect_file_source": source,
                "defect_files": next_record.get("defect_files", []),
                "allow_test_patch": policy[0],
                "allow_test_patch_reason": policy[1],
            }
        )

    if not scoped:
        raise ValueError(f"no selected rows were written: {selected_rows}")
    return scoped, mapping


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
        newline="\n",
    )


def split_multi_path_arg(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[;,]", value) if part.strip()]


def format_command(command: list[str]) -> str:
    return " ".join(f'"{part}"' if " " in part else part for part in command)


def safe_file_stem(value: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    stem = stem.strip("._-")
    return stem or datetime.now().strftime("%Y%m%d_%H%M%S")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run ArkEval repair using localized file scopes.")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=None,
        help="Localization arkfix_input.jsonl. Defaults to latest localization/outputs/*/arkfix_input.jsonl.",
    )
    parser.add_argument("--rows", default="", help="Original dataset row numbers to repair, e.g. 1,4,8-12.")
    parser.add_argument(
        "--repair-engine-root",
        "--arkagent-root",
        dest="repair_engine_root",
        type=Path,
        default=DEFAULT_REPAIR_ENGINE_ROOT,
        help="Directory containing the migrated repair engine. --arkagent-root is a compatibility alias.",
    )
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument(
        "--scoped-dataset-dir",
        type=Path,
        default=DEFAULT_SCOPED_DATASET_DIR,
        help="Directory for the generated arkts scoped benchmark JSONL.",
    )
    parser.add_argument("--timestamp", default="", help="Override repair_<timestamp> and model_<timestamp> stamp.")
    parser.add_argument("--model-name", default="MiniMax-M2.5")
    parser.add_argument("--config-file", type=Path, default=None)
    parser.add_argument("--repo-pool", type=Path, default=DEFAULT_REPO_POOL)
    parser.add_argument("--repo-pools", default="")
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--worker-timeout-seconds", type=float, default=14400.0)
    parser.add_argument("--worker-start-interval-seconds", type=float, default=0.25)
    parser.add_argument("--worker-task-batch-size", type=int, default=3)
    parser.add_argument("--build-concurrency", type=int, default=8)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--max-steps-per-instance", type=int, default=50)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--top-p", type=float, default=1.0)
    parser.add_argument("--python-exe", default=default_repair_python())
    apply_check_group = parser.add_mutually_exclusive_group()
    apply_check_group.add_argument(
        "--serial-apply-check",
        dest="serial_apply_check",
        action="store_true",
    )
    apply_check_group.add_argument(
        "--no-serial-apply-check",
        dest="serial_apply_check",
        action="store_false",
    )
    parser.set_defaults(serial_apply_check=True)
    parser.add_argument("--apply-check-repo-root", type=Path, default=None)
    parser.add_argument("--deveco-path", default="")
    parser.add_argument("--rag-mode", default="off", choices=["off", "on"])
    parser.add_argument(
        "--rag-docs-roots",
        default="",
        help="Semicolon/comma separated local HarmonyOS official documentation roots for RAG indexing.",
    )
    parser.add_argument(
        "--rag-samples-roots",
        default="",
        help="Semicolon/comma separated local official ArkTS sample roots for RAG indexing.",
    )
    parser.add_argument("--rag-index-name", default="arkfix_default")
    parser.add_argument("--rag-top-k-docs", type=int, default=4)
    parser.add_argument("--rag-top-k-code", type=int, default=4)
    parser.add_argument("--rag-max-context-chars", type=int, default=12000)
    parser.add_argument("--rag-storage-dir", default="")
    parser.add_argument("--rag-build-index", action="store_true")
    parser.add_argument("--allow-original-defect-files", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="Do not print arkfix wrapper details to stdout.")
    parser.add_argument("--skip-preflight", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repair_engine_root = args.repair_engine_root.resolve()
    batch_script = repair_engine_root / "scripts" / "run_arkts_model_patch_batch.py"
    if not batch_script.is_file():
        raise FileNotFoundError(f"repair engine batch script not found: {batch_script}")

    dataset = (args.dataset.resolve() if args.dataset else latest_arkfix_input())
    records = load_jsonl(dataset)
    available_rows = [row_number_for_record(index, record) for index, record in enumerate(records, 1)]
    selected_rows = parse_selected_rows(args.rows, available_rows=available_rows)

    stamp = args.timestamp or datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    run_dir = args.output_root.resolve() / f"repair_{stamp}"
    claim_run_directory(run_dir)
    scoped_dataset_dir = args.scoped_dataset_dir.resolve()
    scoped_dataset_dir.mkdir(parents=True, exist_ok=True)
    scoped_dataset = scoped_dataset_dir / f"arkts_repair_scoped_{safe_file_stem(stamp)}.jsonl"
    mapping_path = run_dir / "row_mapping.json"
    model_output_root = run_dir / "model_patch"

    scoped_records, row_mapping = build_scoped_dataset(
        records,
        selected_rows=selected_rows,
        allow_original_defect_files=args.allow_original_defect_files,
    )
    write_jsonl(scoped_dataset, scoped_records)
    mapping_path.write_text(
        json.dumps(row_mapping, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    config_file = (
        args.config_file.resolve()
        if args.config_file
        else repair_engine_root / "config" / "arkts_system_prompt.yaml"
    )
    repo_pool = args.repo_pool.resolve()
    apply_check_repo_root = (
        args.apply_check_repo_root.resolve()
        if args.apply_check_repo_root
        else repo_pool / "run01"
    )

    command = [
        args.python_exe,
        str(batch_script),
        "--dataset",
        str(scoped_dataset),
        "--output-root",
        str(model_output_root),
        "--timestamp",
        stamp,
        "--model-name",
        args.model_name,
        "--config-file",
        str(config_file),
        "--repo-pool",
        str(repo_pool),
        "--rows",
        ",".join(str(row) for row in range(1, len(scoped_records) + 1)),
        "--workers",
        str(args.workers),
        "--worker-timeout-seconds",
        str(args.worker_timeout_seconds),
        "--worker-start-interval-seconds",
        str(args.worker_start_interval_seconds),
        "--worker-task-batch-size",
        str(args.worker_task_batch_size),
        "--build-concurrency",
        str(args.build_concurrency),
        "--max-retries",
        str(args.max_retries),
        "--max-steps-per-instance",
        str(args.max_steps_per_instance),
        "--temperature",
        str(args.temperature),
        "--top-p",
        str(args.top_p),
        "--apply-check-repo-root",
        str(apply_check_repo_root),
    ]
    if args.repo_pools.strip():
        command.extend(["--repo-pools", ";".join(split_multi_path_arg(args.repo_pools))])
    if args.serial_apply_check:
        command.append("--serial-apply-check")
    else:
        command.append("--no-serial-apply-check")
    if args.deveco_path.strip():
        command.extend(["--deveco-path", args.deveco_path.strip()])
    if args.rag_mode.strip().lower() != "off":
        command.extend(
            [
                "--rag-mode",
                args.rag_mode,
                "--rag-docs-roots",
                args.rag_docs_roots,
                "--rag-samples-roots",
                args.rag_samples_roots,
                "--rag-index-name",
                args.rag_index_name,
                "--rag-top-k-docs",
                str(args.rag_top_k_docs),
                "--rag-top-k-code",
                str(args.rag_top_k_code),
                "--rag-max-context-chars",
                str(args.rag_max_context_chars),
            ]
        )
        if args.rag_storage_dir.strip():
            command.extend(["--rag-storage-dir", args.rag_storage_dir.strip()])
        if args.rag_build_index:
            command.append("--rag-build-index")
    if args.dry_run:
        command.append("--dry-run")
    if args.skip_preflight:
        command.append("--skip-preflight")

    manifest = {
        "dataset": str(dataset),
        "scoped_dataset": str(scoped_dataset),
        "row_mapping": str(mapping_path),
        "repair_engine_root": str(repair_engine_root),
        "batch_script": str(batch_script),
        "model_output_root": str(model_output_root),
        "timestamp": stamp,
        "selected_original_rows": selected_rows,
        "scoped_rows": list(range(1, len(scoped_records) + 1)),
        "rag": {
            "mode": args.rag_mode,
            "docs_roots": args.rag_docs_roots,
            "samples_roots": args.rag_samples_roots,
            "index_name": args.rag_index_name,
            "top_k_docs": args.rag_top_k_docs,
            "top_k_code": args.rag_top_k_code,
            "max_context_chars": args.rag_max_context_chars,
            "storage_dir": args.rag_storage_dir,
            "build_index": bool(args.rag_build_index),
        },
        "command": command,
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (run_dir / "command.txt").write_text(format_command(command) + "\n", encoding="utf-8")

    if not args.quiet:
        print(f"[arkfix] scoped_dataset={scoped_dataset}", flush=True)
        print(f"[arkfix] row_mapping={mapping_path}", flush=True)
        print(f"[arkfix] command={format_command(command)}", flush=True)

    if args.dry_run:
        if not args.quiet:
            print("[arkfix] dry-run: command was written but not executed", flush=True)
        return 0

    completed = subprocess.run(
        command,
        cwd=str(repair_engine_root),
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
