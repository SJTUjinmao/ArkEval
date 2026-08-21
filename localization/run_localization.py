#!/usr/bin/env python3
from __future__ import annotations

import argparse
from email.header import decode_header
import hashlib
import json
import os
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from filelock import FileLock, Timeout as FileLockTimeout


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "dataset" / "arkeval_dataset.jsonl"
DEFAULT_REPO_POOL = ROOT / "depend" / "repair_repo" / "run01"
GIT_MUTATION_TIMEOUT_SECONDS = 600
GIT_READ_TIMEOUT_SECONDS = 120
_GIT_CONFIGURED_REPOS: set[str] = set()
MILVUS_RETRY_TIMEOUT_SECONDS = 1800
MILVUS_RETRY_INTERVAL_SECONDS = 5
DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parent / "outputs"
OUTPUT_STAGE_EMBEDDING = "01_embedding_localization"
OUTPUT_STAGE_LLM1 = "02_llm1_filter"
OUTPUT_STAGE_LLM2 = "03_llm2_dependency_expansion"
DEFAULT_LOCALIZATION_ENGINE_ROOT = Path(
    os.environ.get(
        "LOCALIZATION_ENGINE_ROOT",
        os.environ.get("CODEPHOENIX_ROOT", Path(__file__).resolve().parent),
    )
)
DEFAULT_ENV_FILE = ROOT / ".env"


def output_stage_name(*, no_llm_filter: bool, no_dep_expansion: bool) -> str:
    if no_llm_filter:
        return OUTPUT_STAGE_EMBEDDING
    if no_dep_expansion:
        return OUTPUT_STAGE_LLM1
    return OUTPUT_STAGE_LLM2


def load_dotenv_if_present(path: Path = DEFAULT_ENV_FILE) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


@dataclass(frozen=True)
class DatasetRow:
    row: int
    data: dict[str, Any]


@dataclass(frozen=True)
class LocalizationResult:
    row: int
    instance_id: str
    repo: str
    base_sha: str
    repo_root: str
    query: str
    absolute_paths: list[str]
    relative_paths: list[str]
    status: str
    error: str = ""
    repo_head_after_reset: str = ""
    index_progress_path: str = ""
    index_state_path: str = ""
    chunks_manifest_path: str = ""
    embedding_candidates_path: str = ""
    llm_core_files_path: str = ""
    llm_dep_files_path: str = ""
    llm_model: str = ""
    collection_name: str = ""
    collection_hostname: str = ""
    collection_namespace_hash: str = ""
    collection_repo_root: str = ""


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


def load_jsonl(path: Path) -> list[DatasetRow]:
    rows: list[DatasetRow] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for index, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line:
                continue
            rows.append(DatasetRow(index, json.loads(line)))
    if not rows:
        raise ValueError(f"dataset has no records: {path}")
    return rows


def repo_name_from_record(record: dict[str, Any]) -> str:
    repo = str(record.get("repo") or "").strip()
    if repo:
        return repo
    instance_id = str(record.get("instance_id") or "")
    if "__" in instance_id and "+" in instance_id:
        return instance_id.split("+", 1)[0].split("__", 1)[-1]
    raise ValueError("record has neither repo nor parseable instance_id")


def instance_id_from_record(row: int, record: dict[str, Any]) -> str:
    instance_id = str(record.get("instance_id") or "").strip()
    if instance_id:
        return instance_id
    org = str(record.get("org") or "unknown").strip() or "unknown"
    repo = repo_name_from_record(record)
    sha = str((record.get("base") or {}).get("sha") or "")[:8] or "unknown"
    number = str(record.get("number") or row)
    return f"{org}__{repo}+{sha}-{number}"


def base_sha_from_record(record: dict[str, Any]) -> str:
    base = record.get("base")
    if isinstance(base, dict):
        sha = str(base.get("sha") or "").strip()
        if sha:
            return sha
    sha = str(record.get("base_commit") or "").strip()
    if sha:
        return sha
    raise ValueError("record has neither base.sha nor base_commit")


def build_query(record: dict[str, Any]) -> str:
    if record.get("problem_statement"):
        return str(record["problem_statement"]).strip()

    parts: list[str] = []
    title = str(record.get("title") or "").strip()
    body = str(record.get("body") or "").strip()
    hints = str(record.get("hints") or "").strip()
    if title:
        parts.append(f"Title:\n{title}")
    if body:
        parts.append(f"Body:\n{body}")
    resolved = record.get("resolved_issues")
    if isinstance(resolved, list) and resolved:
        rendered: list[str] = []
        for item in resolved:
            if not isinstance(item, dict):
                continue
            issue_title = str(item.get("title") or "").strip()
            issue_body = str(item.get("body") or "").strip()
            if issue_title or issue_body:
                rendered.append("\n".join(x for x in (issue_title, issue_body) if x))
        if rendered:
            parts.append("Resolved issues:\n" + "\n\n".join(rendered))
    if hints:
        parts.append(f"Hints:\n{hints}")
    query = "\n\n".join(parts).strip()
    if query:
        return query

    patch_subject = patch_subject_from_record(record)
    if patch_subject:
        return f"Patch subject:\n{patch_subject}"
    return ""


def patch_subject_from_record(record: dict[str, Any]) -> str:
    patch = str(record.get("fix_patch") or "").strip()
    if not patch:
        return ""
    subject_lines: list[str] = []
    collecting = False
    for raw in patch.splitlines():
        if raw.startswith("Subject:"):
            collecting = True
            subject_lines.append(raw.partition(":")[2].strip())
            continue
        if collecting and (raw.startswith(" ") or raw.startswith("\t")):
            subject_lines.append(raw.strip())
            continue
        if collecting:
            break
    subject = " ".join(x for x in subject_lines if x).strip()
    if not subject:
        return ""
    subject = decode_header_text(subject)
    return " ".join(subject.split())


def decode_header_text(value: str) -> str:
    decoded: list[str] = []
    for part, charset in decode_header(value):
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def resolve_repo_root(record: dict[str, Any], *, repo_root: Path | None, repo_pool: Path) -> Path:
    if repo_root is not None:
        return repo_root.resolve()
    repo_name = repo_name_from_record(record)
    name_path = Path(repo_name)
    if name_path.is_absolute() or len(name_path.parts) != 1 or repo_name in {".", ".."}:
        raise ValueError(f"invalid dataset repo name: {repo_name}")
    pool = repo_pool.resolve()
    candidate = (pool / repo_name).resolve()
    try:
        candidate.relative_to(pool)
    except ValueError as exc:
        raise ValueError(f"dataset repo escapes assigned repo pool: {repo_name}") from exc
    if not candidate.is_dir():
        raise FileNotFoundError(f"repo not found for {repo_name}: {candidate}")
    return candidate


def run_git(repo_root: Path, args: list[str], *, timeout: int = 120) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        detail = stderr or stdout or f"exit code {completed.returncode}"
        raise RuntimeError(f"git {' '.join(args)} failed in {repo_root}: {detail}")
    return (completed.stdout or "").rstrip("\r\n")


def status_without_codephoenix(repo_root: Path) -> str:
    args = [
        "status",
        "--porcelain",
        "--untracked-files=normal",
        "--",
        ".",
        ":(top,exclude).codephoenix",
        ":(top,exclude).codephoenix/**",
    ]
    for attempt in range(2):
        try:
            dirty = run_git(repo_root, args, timeout=GIT_READ_TIMEOUT_SECONDS)
            break
        except subprocess.TimeoutExpired:
            if attempt == 1:
                raise
            time.sleep(2)
    kept: list[str] = []
    for raw in dirty.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        path = line[2:].strip().replace("\\", "/") if len(line) > 2 else ""
        if path == ".codephoenix" or path.startswith(".codephoenix/"):
            continue
        status = line[:2]
        if status == " M" and is_case_collision_false_dirty(repo_root, path):
            continue
        kept.append(line)
    return "\n".join(kept)


def is_case_collision_false_dirty(repo_root: Path, path: str) -> bool:
    try:
        worktree_hash = run_git(repo_root, ["hash-object", "--no-filters", "--", path], timeout=30)
        tracked = run_git(repo_root, ["ls-files"], timeout=30).splitlines()
        path_lower = path.lower()
        for tracked_path in tracked:
            if tracked_path.lower() != path_lower:
                continue
            try:
                head_hash = run_git(repo_root, ["rev-parse", f"HEAD:{tracked_path}"], timeout=30)
                index_hash = run_git(repo_root, ["rev-parse", f":{tracked_path}"], timeout=30)
            except Exception:
                continue
            if head_hash == index_hash == worktree_hash:
                return True
    except Exception:
        return False
    return False


def reset_repo_to_base(repo_root: Path, base_sha: str) -> str:
    base = (base_sha or "").strip()
    if not base:
        raise RuntimeError("base reset failed: base.sha is empty")
    repo = repo_root.resolve()
    if not repo.is_dir():
        raise RuntimeError(f"base reset failed: repository directory does not exist: {repo}")
    try:
        inside_work_tree = run_git(repo, ["rev-parse", "--is-inside-work-tree"], timeout=GIT_READ_TIMEOUT_SECONDS)
        if inside_work_tree.strip().lower() != "true":
            raise RuntimeError(f"not a git work tree: {repo}")
        repo_key = str(repo).casefold()
        if repo_key not in _GIT_CONFIGURED_REPOS:
            run_git(repo, ["config", "core.autocrlf", "false"], timeout=GIT_READ_TIMEOUT_SECONDS)
            _GIT_CONFIGURED_REPOS.add(repo_key)
        resolved_base = run_git(
            repo,
            ["rev-parse", "--verify", f"{base}^{{commit}}"],
            timeout=GIT_READ_TIMEOUT_SECONDS,
        )
        try:
            run_git(repo, ["reset", "--hard", resolved_base], timeout=GIT_MUTATION_TIMEOUT_SECONDS)
        except RuntimeError as exc:
            reset_head = run_git(repo, ["rev-parse", "HEAD"], timeout=GIT_READ_TIMEOUT_SECONDS)
            if reset_head.lower() == resolved_base.lower() and not status_without_codephoenix(repo):
                pass
            else:
                reset_error = str(exc).lower()
                if not (
                    "unable to create file" in reset_error
                    and "invalid argument" in reset_error
                    and "could not reset index file" in reset_error
                ):
                    raise
                time.sleep(2)
                run_git(repo, ["reset", "--hard", resolved_base], timeout=GIT_MUTATION_TIMEOUT_SECONDS)
        run_git(repo, ["update-index", "-q", "--refresh"], timeout=GIT_MUTATION_TIMEOUT_SECONDS)
        head = run_git(repo, ["rev-parse", "HEAD"], timeout=GIT_READ_TIMEOUT_SECONDS)
        if head.lower() != resolved_base.lower():
            raise RuntimeError(f"reset verification failed: expected {resolved_base}, got {head}")
        dirty = status_without_codephoenix(repo)
        if dirty:
            run_git(repo, ["checkout", "--", "."], timeout=GIT_MUTATION_TIMEOUT_SECONDS)
            run_git(repo, ["checkout-index", "-f", "-a"], timeout=GIT_MUTATION_TIMEOUT_SECONDS)
            run_git(repo, ["clean", "-ffdxq", "-e", ".codephoenix/"], timeout=GIT_MUTATION_TIMEOUT_SECONDS)
            run_git(repo, ["update-index", "-q", "--refresh"], timeout=GIT_MUTATION_TIMEOUT_SECONDS)
            dirty = status_without_codephoenix(repo)
        if dirty:
            import time

            time.sleep(1)
            run_git(repo, ["update-index", "-q", "--really-refresh"], timeout=GIT_MUTATION_TIMEOUT_SECONDS)
            dirty = status_without_codephoenix(repo)
        if dirty:
            raise RuntimeError(f"reset verification failed: working tree is not clean after reset:\n{dirty}")
        return head
    except Exception as exc:
        if str(exc).startswith("base reset failed:"):
            raise
        raise RuntimeError(f"base reset failed: {exc}") from exc


def normalize_located_paths(paths: Iterable[str], repo_root: Path) -> tuple[list[str], list[str]]:
    from localization_engine.locate_flow import _current_repo_path

    absolute_paths: list[str] = []
    relative_paths: list[str] = []
    seen_relative: set[str] = set()
    repo_root = repo_root.resolve()
    for raw in paths:
        text = str(raw or "").strip()
        if not text:
            continue
        absolute, relative, key = _current_repo_path(repo_root, text)
        if key in seen_relative:
            continue
        seen_relative.add(key)
        absolute_paths.append(absolute)
        relative_paths.append(relative)
    return absolute_paths, relative_paths


def _load_jsonl_artifact(path: Path, label: str) -> list[dict[str, Any]]:
    if not path.is_file():
        raise RuntimeError(f"{label} artifact is missing: {path}")
    records: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{label} artifact has invalid JSON at line {line_number}: {path}") from exc
        if not isinstance(item, dict):
            raise RuntimeError(f"{label} artifact has a non-object at line {line_number}: {path}")
        records.append(item)
    return records


def _validate_path_artifact(
    path: Path,
    *,
    label: str,
    repo_root: Path,
    expected_model: str = "",
) -> tuple[list[str], list[str]]:
    records = _load_jsonl_artifact(path, label)
    absolute_paths: list[str] = []
    relative_paths: list[str] = []
    for rank, item in enumerate(records, start=1):
        if int(item.get("rank") or 0) != rank:
            raise RuntimeError(f"{label} ranks are not contiguous at rank {rank}: {path}")
        absolute, relative = normalize_located_paths([str(item.get("file_path") or "")], repo_root)
        if len(absolute) != 1:
            raise RuntimeError(f"{label} contains an empty path at rank {rank}: {path}")
        declared_relative = str(item.get("relative_path") or "").replace("\\", "/")
        if declared_relative != relative[0]:
            raise RuntimeError(f"{label} relative_path mismatch at rank {rank}: {path}")
        if expected_model and str(item.get("model") or "") != expected_model:
            raise RuntimeError(f"{label} model mismatch at rank {rank}: {path}")
        absolute_paths.append(absolute[0])
        relative_paths.append(relative[0])
    if len({value.casefold() for value in relative_paths}) != len(relative_paths):
        raise RuntimeError(f"{label} contains duplicate paths: {path}")
    return absolute_paths, relative_paths


def validate_success_artifacts(
    *,
    repo_root: Path,
    top_k_files: int,
    no_llm_filter: bool,
    no_dep_expansion: bool,
    llm_model: str,
    embedding_candidates_path: Path,
    llm_core_files_path: Path,
    llm_dep_files_path: Path,
    row_trace_path: Path,
    llm_trace_path: Path,
    absolute_paths: list[str],
    relative_paths: list[str],
) -> None:
    candidate_abs, candidate_rel = _validate_path_artifact(
        embedding_candidates_path,
        label="embedding candidates",
        repo_root=repo_root,
    )
    if len(candidate_abs) != top_k_files:
        raise RuntimeError(
            f"embedding candidate count mismatch: expected={top_k_files} actual={len(candidate_abs)}"
        )

    trace = _load_jsonl_artifact(row_trace_path, "row trace")
    stages = [str(item.get("stage") or "") for item in trace]
    llm_trace = _load_jsonl_artifact(llm_trace_path, "LLM trace") if llm_trace_path.is_file() else []
    llm_stages = [str(item.get("stage") or "") for item in llm_trace]

    base_abs = candidate_abs
    base_rel = candidate_rel
    if not no_llm_filter:
        core_abs, core_rel = _validate_path_artifact(
            llm_core_files_path,
            label="LLM1 core files",
            repo_root=repo_root,
            expected_model=llm_model,
        )
        if not core_abs:
            raise RuntimeError("LLM1 core files artifact is empty")
        candidate_keys = {value.casefold() for value in candidate_rel}
        if any(value.casefold() not in candidate_keys for value in core_rel):
            raise RuntimeError("LLM1 selected a path outside the embedding candidates")
        if stages.count("llm_filter_done") != 1 or llm_stages.count("llm_filter") != 1:
            raise RuntimeError("LLM1 success trace is incomplete")
        base_abs, base_rel = core_abs, core_rel

    dep_abs: list[str] = []
    dep_rel: list[str] = []
    if not no_dep_expansion:
        dep_abs, dep_rel = _validate_path_artifact(
            llm_dep_files_path,
            label="LLM2 dependency files",
            repo_root=repo_root,
            expected_model=llm_model,
        )
        if stages.count("ast_dependency_analysis_done") != 1:
            raise RuntimeError("AST dependency analysis success trace is incomplete")
        dep_done = stages.count("llm_dep_expansion_done")
        dep_skipped = stages.count("llm_dep_expansion_skipped")
        if dep_done + dep_skipped != 1:
            raise RuntimeError("LLM2 success/skip trace is incomplete")
        if dep_done == 1 and llm_stages.count("llm_dep_expansion") != 1:
            raise RuntimeError("LLM2 response trace is incomplete")
        if dep_skipped == 1 and dep_abs:
            raise RuntimeError("LLM2 produced files despite a skipped dependency expansion")
        base_keys = {value.casefold() for value in base_rel}
        if any(value.casefold() in base_keys for value in dep_rel):
            raise RuntimeError("LLM2 dependency files overlap the base localization files")

    expected_abs, expected_rel = normalize_located_paths(base_abs + dep_abs, repo_root)
    if not expected_abs:
        raise RuntimeError("localization produced no files")
    if absolute_paths != expected_abs or relative_paths != expected_rel:
        raise RuntimeError("final localization files do not equal the validated LLM1/LLM2 artifacts")


def validate_reuse_candidate_source(
    dataset_record: dict[str, Any],
    *,
    row: int,
    candidates_path: Path,
) -> None:
    result_path = candidates_path.parent / "result.json"
    if not result_path.is_file():
        raise FileNotFoundError(f"reuse source result not found: {result_path}")
    source = json.loads(result_path.read_text(encoding="utf-8"))
    expected_instance = instance_id_from_record(row, dataset_record)
    expected_base = base_sha_from_record(dataset_record)
    expected_repo = repo_name_from_record(dataset_record)
    if source.get("status") != "ok":
        raise RuntimeError(f"reuse source row is not successful: row={row}")
    if int(source.get("row") or 0) != row:
        raise RuntimeError(f"reuse source row mismatch: expected={row} actual={source.get('row')}")
    if str(source.get("instance_id") or "") != expected_instance:
        raise RuntimeError(f"reuse source instance_id mismatch: row={row}")
    if str(source.get("base_sha") or "").casefold() != expected_base.casefold():
        raise RuntimeError(f"reuse source base_sha mismatch: row={row}")
    if str(source.get("repo") or "") != expected_repo:
        raise RuntimeError(f"reuse source repo mismatch: row={row}")


def acquire_repo_worker_locks(repo_root: Path) -> list[FileLock]:
    resolved_repo = repo_root.resolve()
    lock_paths = [resolved_repo.parent / ".arkfix.worker.lock"]
    legacy_lock_dir = resolved_repo / ".codephoenix"
    legacy_lock = legacy_lock_dir / "localization.lock"
    if legacy_lock.is_file():
        lock_paths.append(legacy_lock)

    locks: list[FileLock] = []
    try:
        for lock_path in lock_paths:
            lock = FileLock(str(lock_path))
            lock.acquire(timeout=0)
            locks.append(lock)
    except FileLockTimeout as exc:
        for lock in reversed(locks):
            lock.release()
        raise RuntimeError(f"repo worker is already active; assign this row to another repo pool: {repo_root}") from exc
    except Exception:
        for lock in reversed(locks):
            lock.release()
        raise
    return locks


def import_localization_engine(engine_root: Path) -> None:
    root = engine_root.resolve()
    if not (root / "localization_engine").is_dir():
        raise FileNotFoundError(f"localization_engine package not found: {root}")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def is_milvus_unavailable(exc: Exception) -> bool:
    text = str(exc).casefold()
    return any(
        marker in text
        for marker in (
            "proxy not healthy",
            "fail connecting to server",
            "failed to connect to server",
            "connection refused",
            "statuscode.unavailable",
            "grpc_status:14",
            "server unavailable",
            "find no available rootcoord",
            "find no available datacoord",
            "find no available querycoord",
            "find no available indexcoord",
            "is not serving, reason: sate code: abnormal",
            "is not serving, reason: state code: abnormal",
            "etcdserver: request timed out",
        )
    )


def record_milvus_retry(*, operation: str, attempt: int, error: Exception) -> None:
    trace = os.environ.get("LOCALIZATION_ENGINE_ROW_TRACE_PATH", "").strip()
    if trace:
        append_jsonl(
            Path(trace),
            {
                "stage": "milvus_unavailable_retry",
                "operation": operation,
                "attempt": attempt,
                "retry_interval_seconds": MILVUS_RETRY_INTERVAL_SECONDS,
                "error": f"{type(error).__name__}: {error}",
            },
        )


def record_collection_integrity_rebuild(*, error: Exception) -> None:
    trace = os.environ.get("LOCALIZATION_ENGINE_ROW_TRACE_PATH", "").strip()
    if trace:
        append_jsonl(
            Path(trace),
            {
                "stage": "collection_integrity_full_rebuild",
                "error": f"{type(error).__name__}: {error}",
            },
        )


def ensure_indexed(repo_root: Path, *, force_index: bool) -> None:
    from localization_engine.indexer import CollectionIntegrityError, index_repo

    unavailable_deadline: float | None = None
    attempt = 0
    full = force_index
    while True:
        try:
            index_repo(str(repo_root), dry_run=False, full=full)
            return
        except CollectionIntegrityError as exc:
            if full:
                raise RuntimeError(f"index full rebuild failed: {exc}") from exc
            record_collection_integrity_rebuild(error=exc)
            full = True
        except Exception as exc:
            if not is_milvus_unavailable(exc):
                mode = "full rebuild" if full else "incremental sync"
                raise RuntimeError(f"index {mode} failed: {exc}") from exc
            if unavailable_deadline is None:
                unavailable_deadline = time.monotonic() + MILVUS_RETRY_TIMEOUT_SECONDS
            if time.monotonic() >= unavailable_deadline:
                mode = "full rebuild" if full else "incremental sync"
                raise RuntimeError(f"index {mode} failed: {exc}") from exc
            attempt += 1
            record_milvus_retry(operation="index", attempt=attempt, error=exc)
            time.sleep(MILVUS_RETRY_INTERVAL_SECONDS)
            full = True


def _locate_files_once(
    repo_root: Path,
    query: str,
    *,
    top_k_files: int,
    top_k_hits: int | None,
    no_llm_filter: bool,
    no_dep_expansion: bool,
    raw_scores: bool,
    embedding_candidates_path: Path,
    llm_core_files_path: Path,
    llm_dep_files_path: Path,
    reuse_embedding_candidates_path: Path | None,
) -> list[str]:
    try:
        for path in (embedding_candidates_path, llm_core_files_path, llm_dep_files_path):
            path.parent.mkdir(parents=True, exist_ok=True)
        previous_candidates_path = os.environ.get("LOCALIZATION_ENGINE_EMBEDDING_CANDIDATES_PATH")
        previous_core_path = os.environ.get("LOCALIZATION_ENGINE_LLM_CORE_FILES_PATH")
        previous_dep_path = os.environ.get("LOCALIZATION_ENGINE_LLM_DEP_FILES_PATH")
        previous_reuse_path = os.environ.get("LOCALIZATION_ENGINE_REUSE_EMBEDDING_CANDIDATES")
        os.environ["LOCALIZATION_ENGINE_EMBEDDING_CANDIDATES_PATH"] = str(embedding_candidates_path)
        os.environ["LOCALIZATION_ENGINE_LLM_CORE_FILES_PATH"] = str(llm_core_files_path)
        os.environ["LOCALIZATION_ENGINE_LLM_DEP_FILES_PATH"] = str(llm_dep_files_path)
        if reuse_embedding_candidates_path is not None:
            os.environ["LOCALIZATION_ENGINE_REUSE_EMBEDDING_CANDIDATES"] = str(reuse_embedding_candidates_path)
        else:
            os.environ.pop("LOCALIZATION_ENGINE_REUSE_EMBEDDING_CANDIDATES", None)
        if raw_scores:
            if reuse_embedding_candidates_path is not None:
                if not reuse_embedding_candidates_path.is_file():
                    raise FileNotFoundError(f"embedding candidates reuse file not found: {reuse_embedding_candidates_path}")
                ranking = []
                seen: set[str] = set()
                with reuse_embedding_candidates_path.open("r", encoding="utf-8", errors="replace") as handle:
                    for raw in handle:
                        line = raw.strip()
                        if not line:
                            continue
                        item = json.loads(line)
                        raw_path = str(item.get("relative_path") or item.get("file_path") or "").strip()
                        if raw_path:
                            candidate = Path(raw_path)
                            candidate = (repo_root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
                            try:
                                relative = candidate.relative_to(repo_root).as_posix()
                            except ValueError as exc:
                                raise RuntimeError(f"reused candidate is outside current repo: {candidate}") from exc
                            if not candidate.is_file():
                                raise RuntimeError(f"reused candidate does not exist in current checkout: {candidate}")
                            key = relative.casefold()
                            if key in seen:
                                continue
                            seen.add(key)
                            ranking.append((str(candidate), float(item.get("score", 0.0))))
                        if len(ranking) >= top_k_files:
                            break
            else:
                from localization_engine.locate_flow import get_file_ranking_by_score

                ranking = get_file_ranking_by_score(
                    str(repo_root),
                    query,
                    top_k_files=top_k_files,
                    top_k_hits=top_k_hits,
                )
            if len(ranking) != top_k_files:
                raise RuntimeError(
                    f"embedding candidate count mismatch after reuse validation: expected={top_k_files} actual={len(ranking)}"
                )
            embedding_candidates_path.parent.mkdir(parents=True, exist_ok=True)
            with embedding_candidates_path.open("w", encoding="utf-8", newline="\n") as handle:
                for rank, (path, score) in enumerate(ranking, start=1):
                    try:
                        rel = Path(path).resolve().relative_to(repo_root).as_posix()
                    except ValueError:
                        rel = path
                    handle.write(
                        json.dumps(
                            {
                                "rank": rank,
                                "file_path": path,
                                "relative_path": rel,
                                "source": "embedding_raw_scores",
                                "score": float(score),
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            return [path for path, _score in ranking]

        from localization_engine.locate_flow import get_files_to_modify

        return get_files_to_modify(
            str(repo_root),
            query,
            top_k_files=top_k_files,
            top_k_hits=top_k_hits,
            ask=False,
            use_llm_filter=not no_llm_filter,
            use_llm_dep_expansion=not no_dep_expansion,
        )
    except Exception as exc:
        from localization_engine.locate_flow import LocalizationRetrievalError

        if isinstance(exc, LocalizationRetrievalError):
            raise
        raise RuntimeError(f"locate failed: {exc}") from exc
    finally:
        if previous_candidates_path is None:
            os.environ.pop("LOCALIZATION_ENGINE_EMBEDDING_CANDIDATES_PATH", None)
        else:
            os.environ["LOCALIZATION_ENGINE_EMBEDDING_CANDIDATES_PATH"] = previous_candidates_path
        if previous_core_path is None:
            os.environ.pop("LOCALIZATION_ENGINE_LLM_CORE_FILES_PATH", None)
        else:
            os.environ["LOCALIZATION_ENGINE_LLM_CORE_FILES_PATH"] = previous_core_path
        if previous_dep_path is None:
            os.environ.pop("LOCALIZATION_ENGINE_LLM_DEP_FILES_PATH", None)
        else:
            os.environ["LOCALIZATION_ENGINE_LLM_DEP_FILES_PATH"] = previous_dep_path
        if previous_reuse_path is None:
            os.environ.pop("LOCALIZATION_ENGINE_REUSE_EMBEDDING_CANDIDATES", None)
        else:
            os.environ["LOCALIZATION_ENGINE_REUSE_EMBEDDING_CANDIDATES"] = previous_reuse_path


def locate_files(
    repo_root: Path,
    query: str,
    *,
    top_k_files: int,
    top_k_hits: int | None,
    no_llm_filter: bool,
    no_dep_expansion: bool,
    raw_scores: bool,
    embedding_candidates_path: Path,
    llm_core_files_path: Path,
    llm_dep_files_path: Path,
    reuse_embedding_candidates_path: Path | None,
) -> list[str]:
    from localization_engine.locate_flow import LocalizationRetrievalError

    deadline = time.monotonic() + MILVUS_RETRY_TIMEOUT_SECONDS
    attempt = 0
    while True:
        try:
            return _locate_files_once(
                repo_root,
                query,
                top_k_files=top_k_files,
                top_k_hits=top_k_hits,
                no_llm_filter=no_llm_filter,
                no_dep_expansion=no_dep_expansion,
                raw_scores=raw_scores,
                embedding_candidates_path=embedding_candidates_path,
                llm_core_files_path=llm_core_files_path,
                llm_dep_files_path=llm_dep_files_path,
                reuse_embedding_candidates_path=reuse_embedding_candidates_path,
            )
        except LocalizationRetrievalError as exc:
            if not is_milvus_unavailable(exc) or time.monotonic() >= deadline:
                raise
            attempt += 1
            record_milvus_retry(operation="search", attempt=attempt, error=exc)
            embedding_candidates_path.unlink(missing_ok=True)
            time.sleep(MILVUS_RETRY_INTERVAL_SECONDS)


def write_scope_file(repo_root: Path, relative_paths: list[str]) -> Path:
    scope_dir = repo_root / ".codephoenix"
    scope_dir.mkdir(parents=True, exist_ok=True)
    scope_file = scope_dir / "fix_scope_files.txt"
    scope_file.write_text("\n".join(relative_paths) + ("\n" if relative_paths else ""), encoding="utf-8")
    return scope_file


def write_row_files(rows_dir: Path, result: LocalizationResult) -> None:
    row_dir = rows_dir / f"row_{result.row:06d}"
    row_dir.mkdir(parents=True, exist_ok=True)
    (row_dir / "localized_files_abs.txt").write_text(
        "\n".join(result.absolute_paths) + ("\n" if result.absolute_paths else ""),
        encoding="utf-8",
    )
    (row_dir / "localized_files_rel.txt").write_text(
        "\n".join(result.relative_paths) + ("\n" if result.relative_paths else ""),
        encoding="utf-8",
    )
    (row_dir / "query.txt").write_text(result.query + ("\n" if result.query else ""), encoding="utf-8")
    (row_dir / "result.json").write_text(
        json.dumps(result_to_json(result), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_row_error(rows_dir: Path, row: int, trace: str) -> None:
    row_dir = rows_dir / f"row_{row:06d}"
    row_dir.mkdir(parents=True, exist_ok=True)
    (row_dir / "error.log").write_text(trace, encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"created_at": datetime.now().isoformat(timespec="seconds"), **payload}, ensure_ascii=False) + "\n")


def write_index_snapshot(
    row_dir: Path,
    *,
    index_progress_path: str,
    index_state_path: str,
    chunks_manifest_path: str,
    embedding_candidates_path: str = "",
    llm_core_files_path: str = "",
    llm_dep_files_path: str = "",
    skipped_reason: str = "",
) -> None:
    snapshot: dict[str, Any] = {
        "index_progress_path": index_progress_path,
        "index_state_path": index_state_path,
        "chunks_manifest_path": chunks_manifest_path,
        "embedding_candidates_path": embedding_candidates_path,
        "llm_core_files_path": llm_core_files_path,
        "llm_dep_files_path": llm_dep_files_path,
    }
    if skipped_reason:
        snapshot["skipped_reason"] = skipped_reason
    for key, raw_path in (
        ("index_progress", index_progress_path),
        ("index_state", index_state_path),
    ):
        path = Path(raw_path) if raw_path else None
        if path and path.is_file():
            try:
                snapshot[key] = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                snapshot[f"{key}_error"] = str(exc)
    manifest_path = Path(chunks_manifest_path) if chunks_manifest_path else None
    if manifest_path and manifest_path.is_file():
        digest = hashlib.sha256()
        with manifest_path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        snapshot["chunks_manifest_sha256"] = digest.hexdigest()
    if not skipped_reason:
        state = snapshot.get("index_state")
        audit = state.get("collection_audit") if isinstance(state, dict) else None
        if not isinstance(state, dict) or state.get("status") != "done":
            raise RuntimeError("index snapshot requires index_state.status=done")
        if not isinstance(audit, dict) or audit.get("ok") is not True:
            raise RuntimeError("index snapshot requires a successful collection audit")
    row_dir.mkdir(parents=True, exist_ok=True)
    (row_dir / "index_snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def result_to_json(result: LocalizationResult) -> dict[str, Any]:
    return {
        "row": result.row,
        "instance_id": result.instance_id,
        "repo": result.repo,
        "base_sha": result.base_sha,
        "repo_root": result.repo_root,
        "query": result.query,
        "absolute_paths": result.absolute_paths,
        "relative_paths": result.relative_paths,
        "status": result.status,
        "error": result.error,
        "repo_head_after_reset": result.repo_head_after_reset,
        "index_progress_path": result.index_progress_path,
        "index_state_path": result.index_state_path,
        "chunks_manifest_path": result.chunks_manifest_path,
        "embedding_candidates_path": result.embedding_candidates_path,
        "llm_core_files_path": result.llm_core_files_path,
        "llm_dep_files_path": result.llm_dep_files_path,
        "llm_model": result.llm_model,
        "collection_name": result.collection_name,
        "collection_hostname": result.collection_hostname,
        "collection_namespace_hash": result.collection_namespace_hash,
        "collection_repo_root": result.collection_repo_root,
    }


def arkfix_input_record(result: LocalizationResult) -> dict[str, Any]:
    return {
        "row": result.row,
        "instance_id": result.instance_id,
        "repo": result.repo,
        "repo_root": result.repo_root,
        "base_sha": result.base_sha,
        "problem": result.query,
        "localized_file_abs_paths": result.absolute_paths,
        "localized_file_rel_paths": result.relative_paths,
        "localization_status": result.status,
        "localization_error": result.error,
        "repo_head_after_reset": result.repo_head_after_reset,
        "embedding_candidates_path": result.embedding_candidates_path,
        "llm_core_files_path": result.llm_core_files_path,
        "llm_dep_files_path": result.llm_dep_files_path,
        "localization_llm_model": result.llm_model,
        "collection_name": result.collection_name,
        "collection_hostname": result.collection_hostname,
        "collection_namespace_hash": result.collection_namespace_hash,
        "collection_repo_root": result.collection_repo_root,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the migrated localization engine for ArkEval JSONL rows.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--rows", default="", help="Rows to localize, e.g. 1,4,8-12. Default: all rows.")
    parser.add_argument("--repo-root", type=Path, default=None, help="Use one repo root for every selected row.")
    parser.add_argument("--repo-pool", type=Path, default=DEFAULT_REPO_POOL, help="Directory containing repos by name.")
    parser.add_argument(
        "--localization-engine-root",
        "--codephoenix-root",
        dest="localization_engine_root",
        type=Path,
        default=DEFAULT_LOCALIZATION_ENGINE_ROOT,
        help="Directory containing the localization_engine package.",
    )
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--run-id", default="", help="Override output run id.")
    parser.add_argument("--timestamp", default="", help="Deprecated alias for --run-id.")
    parser.add_argument("--top-k-files", type=int, default=10)
    parser.add_argument("--top-k-hits", type=int, default=None)
    parser.add_argument("--no-llm-filter", action="store_true", help="Use embedding file ranking without LLM filtering.")
    parser.add_argument("--no-dep-expansion", action="store_true", help="Disable dependency expansion.")
    parser.add_argument("--raw-scores", action="store_true", help="Use raw embedding file ranking only.")
    parser.add_argument(
        "--reuse-embedding-candidates-root",
        type=Path,
        default=None,
        help="Reuse rows/row_xxxxxx/embedding_candidates.jsonl from a previous localization output root.",
    )
    parser.add_argument("--force-index", action="store_true", help="Rebuild the localization index before locating.")
    parser.add_argument("--no-write-scope", action="store_true", help="Do not write repo/.codephoenix/fix_scope_files.txt.")
    parser.add_argument("--keep-going", action="store_true", help="Continue after per-row localization failures.")
    parser.add_argument("--chunk-workers", type=int, default=0)
    parser.add_argument("--embedding-batch-size", type=int, default=0)
    parser.add_argument("--embedding-parallel-requests", type=int, default=0)
    parser.add_argument("--milvus-upsert-batch-size", type=int, default=0)
    parser.add_argument("--milvus-upsert-workers", type=int, default=0)
    parser.add_argument("--index-queue-size", type=int, default=0)
    parser.add_argument("--progress-interval-seconds", type=float, default=0.0)
    return parser.parse_args(argv)


def apply_indexing_env(args: argparse.Namespace) -> dict[str, Any]:
    mapping = {
        "chunk_workers": ("LOCALIZATION_ENGINE_CHUNK_WORKERS", args.chunk_workers),
        "embedding_batch_size": ("LOCALIZATION_ENGINE_EMBEDDING_BATCH_SIZE", args.embedding_batch_size),
        "embedding_parallel_requests": ("LOCALIZATION_ENGINE_EMBEDDING_PARALLEL_REQUESTS", args.embedding_parallel_requests),
        "milvus_upsert_batch_size": ("LOCALIZATION_ENGINE_MILVUS_UPSERT_BATCH_SIZE", args.milvus_upsert_batch_size),
        "milvus_upsert_workers": ("LOCALIZATION_ENGINE_MILVUS_UPSERT_WORKERS", args.milvus_upsert_workers),
        "index_queue_size": ("LOCALIZATION_ENGINE_INDEX_QUEUE_SIZE", args.index_queue_size),
        "progress_interval_seconds": ("LOCALIZATION_ENGINE_PROGRESS_INTERVAL_SECONDS", args.progress_interval_seconds),
    }
    out: dict[str, Any] = {}
    for key, (env_name, value) in mapping.items():
        if value and value > 0:
            os.environ[env_name] = str(value)
            out[key] = value
        else:
            out[key] = None
    return out


def main(argv: list[str] | None = None) -> int:
    load_dotenv_if_present()
    args = parse_args(argv)
    indexing_config = apply_indexing_env(args)
    dataset = args.dataset.resolve()
    repo_pool = args.repo_pool.resolve()
    import_localization_engine(args.localization_engine_root)
    llm_required = not (args.no_llm_filter and args.no_dep_expansion)
    if args.raw_scores and llm_required:
        raise RuntimeError("--raw-scores cannot be combined with LLM localization stages")
    if llm_required:
        from localization_engine.config import load_config

        llm_config = load_config(repo_pool).llm
        missing = [
            name
            for name, value in (
                ("api_key", llm_config.api_key),
                ("base_url", llm_config.base_url),
                ("model", llm_config.model_name),
            )
            if not str(value or "").strip()
        ]
        if missing:
            raise RuntimeError(f"localization LLM configuration is incomplete: missing {', '.join(missing)}")
        llm_model = llm_config.model_name
    else:
        llm_model = ""
    run_id = args.run_id or args.timestamp or f"loc_{dataset.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    output_stage = output_stage_name(
        no_llm_filter=bool(args.no_llm_filter),
        no_dep_expansion=bool(args.no_dep_expansion),
    )
    stage_dir = args.output_root.resolve() / output_stage
    stage_dir.mkdir(parents=True, exist_ok=True)
    out_dir = stage_dir / run_id
    rows_dir = out_dir / "rows"
    try:
        out_dir.mkdir()
    except FileExistsError as exc:
        raise RuntimeError(f"localization run_id already exists; use a new run_id: {run_id}") from exc
    rows_dir.mkdir()

    rows = load_jsonl(dataset)
    selected = set(parse_row_spec(args.rows, max_row=max(row.row for row in rows)))
    selected_rows = [row for row in rows if row.row in selected]
    reuse_embedding_candidates = args.reuse_embedding_candidates_root is not None

    from localization_engine.indexer import COLLECTION_IDENTITY_VERSION

    preflight_artifact = os.environ.get("ARKEVAL_EMBEDDING_PREFLIGHT_ARTIFACT", "").strip()
    preflight_sha256 = ""
    if preflight_artifact:
        preflight_path = Path(preflight_artifact).resolve()
        if not preflight_path.is_file():
            raise FileNotFoundError(f"preflight artifact not found: {preflight_path}")
        preflight_sha256 = hashlib.sha256(preflight_path.read_bytes()).hexdigest()
        preflight_artifact = str(preflight_path)

    manifest = {
        "dataset": str(dataset),
        "repo_root": str(args.repo_root.resolve()) if args.repo_root else "",
        "repo_pool": str(repo_pool),
        "localization_engine_root": str(args.localization_engine_root.resolve()),
        "rows": sorted(selected),
        "top_k_files": args.top_k_files,
        "top_k_hits": args.top_k_hits,
        "no_llm_filter": bool(args.no_llm_filter),
        "no_dep_expansion": bool(args.no_dep_expansion),
        "raw_scores": bool(args.raw_scores),
        "reuse_embedding_candidates_root": str(args.reuse_embedding_candidates_root.resolve()) if args.reuse_embedding_candidates_root else "",
        "force_index_requested": bool(args.force_index),
        "force_index_effective": bool(args.force_index and not reuse_embedding_candidates),
        "index_sync_effective": not reuse_embedding_candidates,
        "index_mode": "reused_embedding_candidates" if reuse_embedding_candidates else ("full" if args.force_index else "incremental"),
        "indexing": indexing_config,
        "collection_identity_version": COLLECTION_IDENTITY_VERSION,
        "preflight_artifact": preflight_artifact,
        "preflight_sha256": preflight_sha256,
        "run_id": run_id,
        "output_stage": output_stage,
        "llm_model": llm_model,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "schema": {
            "localization_results": "localization_results.v1",
            "arkfix_input": "arkfix_input.v1",
        },
        "artifacts": {
            "localization_results": "localization_results.jsonl",
            "arkfix_input": "arkfix_input.jsonl",
            "rows_dir": "rows",
        },
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    localized_records: list[dict[str, Any]] = []
    results: list[LocalizationResult] = []
    with (
        (out_dir / "localization_results.jsonl").open("w", encoding="utf-8", newline="\n") as result_handle,
        (out_dir / "arkfix_input.jsonl").open("w", encoding="utf-8", newline="\n") as arkfix_handle,
    ):
        for item in selected_rows:
            record = item.data
            instance_id = instance_id_from_record(item.row, record)
            repo = repo_name_from_record(record)
            row_dir = rows_dir / f"row_{item.row:06d}"
            row_trace_path = row_dir / "row_trace.jsonl"
            llm_trace_path = row_dir / "llm_trace.jsonl"
            embedding_candidates_path = row_dir / "embedding_candidates.jsonl"
            llm_core_files_path = row_dir / "llm_core_files.jsonl"
            llm_dep_files_path = row_dir / "llm_dep_expansion_files.jsonl"
            reuse_embedding_candidates_path = (
                args.reuse_embedding_candidates_root.resolve() / "rows" / f"row_{item.row:06d}" / "embedding_candidates.jsonl"
                if args.reuse_embedding_candidates_root
                else None
            )
            previous_row_trace = os.environ.get("LOCALIZATION_ENGINE_ROW_TRACE_PATH")
            previous_llm_trace = os.environ.get("LOCALIZATION_ENGINE_LLM_TRACE_PATH")
            os.environ["LOCALIZATION_ENGINE_ROW_TRACE_PATH"] = str(row_trace_path)
            os.environ["LOCALIZATION_ENGINE_LLM_TRACE_PATH"] = str(llm_trace_path)
            base_sha = ""
            query = ""
            repo_head_after_reset = ""
            index_progress_path = ""
            index_state_path = ""
            chunks_manifest_path = ""
            current_repo_root_text = ""
            collection_name = ""
            collection_hostname = ""
            collection_namespace_hash = ""
            collection_repo_root = ""
            repo_locks: list[FileLock] = []
            try:
                append_jsonl(row_trace_path, {"stage": "row_start", "row": item.row, "instance_id": instance_id, "repo": repo})
                base_sha = base_sha_from_record(record)
                query = build_query(record)
                if not query:
                    raise ValueError("record produced an empty localization query")
                current_repo_root = resolve_repo_root(record, repo_root=args.repo_root, repo_pool=repo_pool)
                current_repo_root_text = str(current_repo_root)
                if reuse_embedding_candidates_path is not None:
                    validate_reuse_candidate_source(
                        record,
                        row=item.row,
                        candidates_path=reuse_embedding_candidates_path,
                    )
                codephoenix_dir = current_repo_root / ".codephoenix"
                repo_locks = acquire_repo_worker_locks(current_repo_root)
                from localization_engine.indexer import get_collection_identity

                identity = get_collection_identity(current_repo_root)
                collection_name = identity.collection_name
                collection_hostname = identity.collection_hostname
                collection_namespace_hash = identity.collection_namespace_hash
                collection_repo_root = identity.collection_repo_root
                index_progress_path = str(codephoenix_dir / "index_progress.json")
                index_state_path = str(codephoenix_dir / "index_state.json")
                chunks_manifest_path = str(codephoenix_dir / "chunks_manifest.jsonl")
                append_jsonl(
                    row_trace_path,
                    {
                        "stage": "base_reset_start",
                        "base_sha": base_sha,
                        "repo_root": str(current_repo_root),
                        "collection_name": collection_name,
                        "collection_hostname": collection_hostname,
                        "collection_namespace_hash": collection_namespace_hash,
                    },
                )
                repo_head_after_reset = reset_repo_to_base(current_repo_root, base_sha)
                from localization_engine.locate_flow import clear_git_tracked_path_cache

                clear_git_tracked_path_cache()
                append_jsonl(
                    row_trace_path,
                    {
                        "stage": "base_reset_done",
                        "repo_head_after_reset": repo_head_after_reset,
                        "collection_name": collection_name,
                        "collection_repo_root": collection_repo_root,
                    },
                )
                if reuse_embedding_candidates_path is None:
                    append_jsonl(
                        row_trace_path,
                        {
                            "stage": "index_sync_start",
                            "force_index": bool(args.force_index),
                            "index_progress_path": index_progress_path,
                            "collection_name": collection_name,
                            "collection_namespace_hash": collection_namespace_hash,
                        },
                    )
                    ensure_indexed(current_repo_root, force_index=args.force_index)
                    write_index_snapshot(
                        row_dir,
                        index_progress_path=index_progress_path,
                        index_state_path=index_state_path,
                        chunks_manifest_path=chunks_manifest_path,
                        embedding_candidates_path=str(embedding_candidates_path),
                        llm_core_files_path=str(llm_core_files_path),
                        llm_dep_files_path=str(llm_dep_files_path),
                    )
                    append_jsonl(
                        row_trace_path,
                        {
                            "stage": "index_sync_done",
                            "index_snapshot_path": str(row_dir / "index_snapshot.json"),
                            "collection_name": collection_name,
                        },
                    )
                else:
                    append_jsonl(
                        row_trace_path,
                        {
                            "stage": "index_sync_skipped",
                            "reason": "reuse_embedding_candidates",
                            "reuse_embedding_candidates_path": str(reuse_embedding_candidates_path),
                        },
                    )
                    write_index_snapshot(
                        row_dir,
                        index_progress_path=index_progress_path,
                        index_state_path=index_state_path,
                        chunks_manifest_path=chunks_manifest_path,
                        embedding_candidates_path=str(embedding_candidates_path),
                        llm_core_files_path=str(llm_core_files_path),
                        llm_dep_files_path=str(llm_dep_files_path),
                        skipped_reason="reuse_embedding_candidates",
                    )
                append_jsonl(
                    row_trace_path,
                    {
                        "stage": "locate_start",
                        "top_k_files": args.top_k_files,
                        "top_k_hits": args.top_k_hits,
                        "llm_filter": not args.no_llm_filter,
                        "dep_expansion": not args.no_dep_expansion,
                        "raw_scores": bool(args.raw_scores),
                        "embedding_candidates_path": str(embedding_candidates_path),
                        "llm_core_files_path": str(llm_core_files_path),
                        "llm_dep_files_path": str(llm_dep_files_path),
                        "reuse_embedding_candidates_path": str(reuse_embedding_candidates_path) if reuse_embedding_candidates_path else "",
                        "collection_name": collection_name,
                        "collection_namespace_hash": collection_namespace_hash,
                    },
                )
                located = locate_files(
                    current_repo_root,
                    query,
                    top_k_files=args.top_k_files,
                    top_k_hits=args.top_k_hits,
                    no_llm_filter=args.no_llm_filter,
                    no_dep_expansion=args.no_dep_expansion,
                    raw_scores=args.raw_scores,
                    embedding_candidates_path=embedding_candidates_path,
                    llm_core_files_path=llm_core_files_path,
                    llm_dep_files_path=llm_dep_files_path,
                    reuse_embedding_candidates_path=reuse_embedding_candidates_path,
                )
                append_jsonl(row_trace_path, {"stage": "locate_done", "located_count": len(located)})
                absolute_paths, relative_paths = normalize_located_paths(located, current_repo_root)
                validate_success_artifacts(
                    repo_root=current_repo_root,
                    top_k_files=args.top_k_files,
                    no_llm_filter=args.no_llm_filter,
                    no_dep_expansion=args.no_dep_expansion,
                    llm_model=llm_model,
                    embedding_candidates_path=embedding_candidates_path,
                    llm_core_files_path=llm_core_files_path,
                    llm_dep_files_path=llm_dep_files_path,
                    row_trace_path=row_trace_path,
                    llm_trace_path=llm_trace_path,
                    absolute_paths=absolute_paths,
                    relative_paths=relative_paths,
                )
                if not args.no_write_scope:
                    write_scope_file(current_repo_root, relative_paths)
                result = LocalizationResult(
                    row=item.row,
                    instance_id=instance_id,
                    repo=repo,
                    base_sha=base_sha,
                    repo_root=str(current_repo_root),
                    query=query,
                    absolute_paths=absolute_paths,
                    relative_paths=relative_paths,
                    status="ok",
                    repo_head_after_reset=repo_head_after_reset,
                    index_progress_path=index_progress_path,
                    index_state_path=index_state_path,
                    chunks_manifest_path=chunks_manifest_path,
                    embedding_candidates_path=str(embedding_candidates_path),
                    llm_core_files_path=str(llm_core_files_path),
                    llm_dep_files_path=str(llm_dep_files_path),
                    llm_model=llm_model,
                    collection_name=collection_name,
                    collection_hostname=collection_hostname,
                    collection_namespace_hash=collection_namespace_hash,
                    collection_repo_root=collection_repo_root,
                )
                append_jsonl(
                    row_trace_path,
                    {
                        "stage": "row_done",
                        "status": "ok",
                        "relative_paths_count": len(relative_paths),
                        "collection_name": collection_name,
                    },
                )
            except Exception as exc:
                append_jsonl(row_trace_path, {"stage": "row_error", "error_type": type(exc).__name__, "error": str(exc)})
                result = LocalizationResult(
                    row=item.row,
                    instance_id=instance_id,
                    repo=repo,
                    base_sha=base_sha,
                    repo_root=current_repo_root_text,
                    query=query,
                    absolute_paths=[],
                    relative_paths=[],
                    status="error",
                    error=f"{type(exc).__name__}: {exc}",
                    repo_head_after_reset=repo_head_after_reset,
                    index_progress_path=index_progress_path,
                    index_state_path=index_state_path,
                    chunks_manifest_path=chunks_manifest_path,
                    embedding_candidates_path=str(embedding_candidates_path),
                    llm_core_files_path=str(llm_core_files_path),
                    llm_dep_files_path=str(llm_dep_files_path),
                    llm_model=llm_model,
                    collection_name=collection_name,
                    collection_hostname=collection_hostname,
                    collection_namespace_hash=collection_namespace_hash,
                    collection_repo_root=collection_repo_root,
                )
                write_row_error(rows_dir, item.row, traceback.format_exc())
                if not args.keep_going:
                    result_handle.write(json.dumps(result_to_json(result), ensure_ascii=False) + "\n")
                    arkfix_handle.write(json.dumps(arkfix_input_record(result), ensure_ascii=False) + "\n")
                    raise
            finally:
                for repo_lock in reversed(repo_locks):
                    if repo_lock.is_locked:
                        repo_lock.release()
                if previous_row_trace is None:
                    os.environ.pop("LOCALIZATION_ENGINE_ROW_TRACE_PATH", None)
                else:
                    os.environ["LOCALIZATION_ENGINE_ROW_TRACE_PATH"] = previous_row_trace
                if previous_llm_trace is None:
                    os.environ.pop("LOCALIZATION_ENGINE_LLM_TRACE_PATH", None)
                else:
                    os.environ["LOCALIZATION_ENGINE_LLM_TRACE_PATH"] = previous_llm_trace

            results.append(result)
            write_row_files(rows_dir, result)
            result_handle.write(json.dumps(result_to_json(result), ensure_ascii=False) + "\n")
            arkfix_handle.write(json.dumps(arkfix_input_record(result), ensure_ascii=False) + "\n")
            result_handle.flush()
            arkfix_handle.flush()

            enriched = dict(record)
            enriched["localized_file_abs_paths"] = result.absolute_paths
            enriched["localized_file_rel_paths"] = result.relative_paths
            enriched["_localization"] = result_to_json(result)
            localized_records.append(enriched)
            print(
                f"[localization] row={item.row} instance={instance_id} status={result.status} "
                f"files={len(result.relative_paths)} model={result.llm_model or 'none'}",
                flush=True,
            )

    (out_dir / "enriched_dataset.jsonl").write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in localized_records),
        encoding="utf-8",
        newline="\n",
    )
    ok_count = sum(1 for result in results if result.status == "ok")
    print(f"[done] localized {ok_count}/{len(results)} rows; output={out_dir}")
    return 0 if ok_count == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
