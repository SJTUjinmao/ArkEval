#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from getpass import getuser
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout as FileLockTimeout
import yaml


ROOT = Path(__file__).resolve().parents[1]
ARKEVAL_ROOT = ROOT.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from sweagent.utils.patch_utils import is_agent_self_test_patch_path
from sweagent.utils.native_repo import mask_windows_case_collisions, remove_untracked_reparse_points

DEFAULT_DATASET = ROOT / "tests" / "arkts_benchmark_v4.jsonl"
DEFAULT_CONFIG = ROOT / "config" / "arkts_system_prompt.yaml"
DEFAULT_REPO_POOL = ARKEVAL_ROOT / "depend" / "repair_repo"
DEFAULT_OUTPUT_ROOT = ROOT.parent / "outputs" / "model_patch"
DEFAULT_TRAJECTORIES_ROOT = ROOT / "trajectories" / getuser()
UNICODE_REPLACEMENT_CHAR = "\ufffd"


def default_worker_python() -> str:
    current = Path(sys.executable).resolve()
    prefix = Path(sys.prefix).resolve()
    if prefix.parent.name.casefold() == "envs":
        base_python = prefix.parent.parent / current.name
        if base_python.is_file():
            return str(base_python)
    return str(current)


@dataclass(frozen=True)
class DatasetRow:
    row: int
    instance_id: str
    repo: str
    base_sha: str
    defect_files: tuple[str, ...] = ()
    allow_test_patch: bool = False
    allow_test_patch_reason: str = "none"


@dataclass(frozen=True)
class WorkerSpec:
    attempt: int
    worker: int
    rows: list[int]
    repo_dir: Path
    suffix: str
    instance_filter: str
    command: list[str]
    log_path: Path
    batch_run_id: str
    started_at_epoch: float | None = None
    trajectory_dir: Path | None = None
    exit_code: int | None = None
    cleanup_error: str = ""
    compatible_slots: tuple[Path, ...] = ()


@dataclass(frozen=True)
class TimingInfo:
    repair_time_s: float | None
    edit_action_elapsed_s: float | None
    edit_action_count: int | None
    timing_status: str


@dataclass(frozen=True)
class PatchCandidate:
    row: int
    instance_id: str
    patch_path: Path
    meta_path: Path | None
    traj_path: Path | None
    trajectory_dir: Path
    attempt: int
    worker: int
    repo_dir: Path
    batch_run_id: str
    base_sha: str
    text: str
    bytes_len: int
    sha256: str
    source_patch_sha256: str
    trajectory_sha256: str
    source_meta: dict[str, Any]
    timing: TimingInfo


@dataclass(frozen=True)
class RowStatus:
    row: int
    instance_id: str
    ok: bool
    reason: str


class ProgressReporter:
    def __init__(self, *, total: int, label: str = "patch") -> None:
        self.total = max(total, 1)
        self.label = label
        self.done_rows: set[int] = set()
        self.detail_rows: set[int] = set()
        self.last_heartbeat_s = 0.0

    def seed_existing(self, rows: list[int], output_dir: Path) -> None:
        for row in rows:
            if (output_dir / f"model_patch_{row}.patch").is_file():
                self.done_rows.add(row)
        self.print_status(row=None, force=True)

    def mark_done(self, row: int, detail: str | None = None) -> None:
        if row in self.done_rows:
            if detail and row not in self.detail_rows:
                print(detail, flush=True)
                self.detail_rows.add(row)
            return
        self.done_rows.add(row)
        if detail:
            print(detail, flush=True)
            self.detail_rows.add(row)
        self.print_status(row=row, force=True)

    def print_status(self, *, row: int | None, force: bool = False) -> None:
        if not force:
            return
        done = min(len(self.done_rows), self.total)
        percent = int(round(done * 100 / self.total))
        filled = int(round(done * 50 / self.total))
        bar = "#" * filled + "." * (50 - filled)
        print(f"[progress] {self.label} [{bar}] {percent:3d}% ({done}/{self.total})", flush=True)

    def heartbeat(self, *, interval_s: float = 30.0) -> None:
        now = time.monotonic()
        if now - self.last_heartbeat_s < interval_s:
            return
        self.last_heartbeat_s = now
        done = min(len(self.done_rows), self.total)
        percent = int(round(done * 100 / self.total))
        filled = int(round(done * 50 / self.total))
        bar = "#" * filled + "." * (50 - filled)
        print(f"[progress] {self.label} [{bar}] {percent:3d}% ({done}/{self.total})", flush=True)


def load_dataset(path: Path) -> dict[int, DatasetRow]:
    rows: dict[int, DatasetRow] = {}
    with path.open("r", encoding="utf-8", errors="strict") as handle:
        for idx, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line:
                continue
            data = json.loads(line)
            instance_id = str(data.get("instance_id") or "")
            if not instance_id:
                raise ValueError(f"dataset row {idx} has no instance_id")
            repo = str(data.get("repo") or "")
            if not repo and "__" in instance_id:
                repo = instance_id.split("+", 1)[0].split("__", 1)[-1]
            base_sha = str((data.get("base") or {}).get("sha") or "").strip()
            if not base_sha:
                raise ValueError(f"dataset row {idx} has no base.sha")
            raw_defect_files = data.get("defect_files", [])
            if not isinstance(raw_defect_files, list):
                raise ValueError(f"dataset row {idx} defect_files is not a list")
            allow_test_patch = data.get("allow_test_patch", False)
            if type(allow_test_patch) is not bool:
                raise ValueError(f"dataset row {idx} allow_test_patch is not boolean")
            allow_test_patch_reason = str(data.get("allow_test_patch_reason") or "none")
            rows[idx] = DatasetRow(
                row=idx,
                instance_id=instance_id,
                repo=repo,
                base_sha=base_sha,
                defect_files=tuple(str(path) for path in raw_defect_files if str(path).strip()),
                allow_test_patch=allow_test_patch,
                allow_test_patch_reason=allow_test_patch_reason,
            )
    if not rows:
        raise ValueError(f"dataset has no rows: {path}")
    return rows


def load_benchmark_entries(path: Path) -> dict[int, dict[str, Any]]:
    entries: dict[int, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8", errors="strict") as handle:
        for idx, raw in enumerate(handle, 1):
            line = raw.strip()
            if line:
                entries[idx] = json.loads(line)
    return entries


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_patch_only_config(path: Path) -> bool:
    data = yaml.safe_load(path.read_text(encoding="utf-8", errors="strict")) or {}
    return isinstance(data, dict) and data.get("patch_only_generation") is True


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


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


def shard_rows(rows: list[int], workers: int) -> list[list[int]]:
    if workers < 1:
        raise ValueError("workers must be >= 1")
    if not rows:
        return []
    if workers == 7 and rows == list(range(1, 51)):
        return [
            list(range(1, 8)),
            list(range(8, 15)),
            list(range(15, 22)),
            list(range(22, 29)),
            list(range(29, 36)),
            list(range(36, 43)),
            list(range(43, 51)),
        ]
    active = min(workers, len(rows))
    shards = [[] for _ in range(active)]
    for index, row in enumerate(rows):
        shards[index % active].append(row)
    return [shard for shard in shards if shard]


def discover_repo_slots(repo_pools: list[Path]) -> list[Path]:
    slots: list[Path] = []
    seen: set[str] = set()
    for pool in repo_pools:
        if not pool.is_dir():
            raise SystemExit(f"repair repo pool does not exist: {pool}")
        pool_slots = sorted(
            (
                path
                for path in pool.iterdir()
                if path.is_dir() and re.fullmatch(r"run\d+", path.name, re.IGNORECASE)
            ),
            key=lambda path: int(path.name[3:]),
        )
        for slot in pool_slots:
            resolved = str(slot.resolve()).casefold()
            if resolved in seen:
                raise SystemExit(f"duplicate repair repo slot: {slot.resolve()}")
            seen.add(resolved)
            slots.append(slot.resolve())
    if not slots:
        raise SystemExit("no runNN repair repo slots found in --repo-pool/--repo-pools")
    return slots


def _safe_repo_name(repo: str) -> bool:
    return bool(repo) and repo not in {".", ".."} and Path(repo).name == repo and "/" not in repo and "\\" not in repo


def assign_rows_to_slots(
    rows: list[int],
    workers: int,
    dataset_rows: dict[int, DatasetRow],
    slots: list[Path],
) -> list[tuple[Path, list[int]]]:
    if workers < 1:
        raise ValueError("workers must be >= 1")
    if not rows:
        return []

    repos = sorted({dataset_rows[row].repo for row in rows})
    candidates: dict[str, list[Path]] = {}
    for repo in repos:
        if not _safe_repo_name(repo):
            raise SystemExit(f"unsafe or empty repository name in dataset: {repo!r}")
        candidates[repo] = [slot for slot in slots if (slot / repo / ".git").exists()]
        if not candidates[repo]:
            raise SystemExit(f"no repair repo slot contains dataset repository: {repo}")

    active_target = min(workers, len(rows), len(slots))
    active: list[Path] = []
    for repo in sorted(repos, key=lambda name: (len(candidates[name]), name)):
        if any(slot in candidates[repo] for slot in active):
            continue
        active.append(candidates[repo][0])
    if len(active) > active_target:
        raise SystemExit(
            f"{active_target} workers cannot cover all dataset repositories; "
            f"need at least {len(active)} compatible slots"
        )
    for slot in slots:
        if len(active) >= active_target:
            break
        if slot not in active:
            active.append(slot)

    assigned: dict[Path, list[int]] = {slot: [] for slot in active}
    for row in sorted(rows, key=lambda value: (len(candidates[dataset_rows[value].repo]), value)):
        compatible = [slot for slot in active if slot in candidates[dataset_rows[row].repo]]
        if not compatible:
            raise SystemExit(
                f"active repair slots do not contain row {row} repository {dataset_rows[row].repo}"
            )
        slot = min(compatible, key=lambda value: (len(assigned[value]), active.index(value)))
        assigned[slot].append(row)
    return [(slot, sorted(assigned[slot])) for slot in active if assigned[slot]]


def build_instance_filter(rows: list[int], dataset_rows: dict[int, DatasetRow]) -> str:
    missing = [row for row in rows if row not in dataset_rows]
    if missing:
        raise ValueError(f"dataset missing rows: {missing}")
    escaped = [re.escape(dataset_rows[row].instance_id) for row in rows]
    return "^(" + "|".join(escaped) + ")$"


def _as_cli_bool(value: bool) -> str:
    return "true" if value else "false"


def build_run_command(
    *,
    python_exe: str,
    config_file: Path,
    model_name: str,
    temperature: float,
    top_p: float,
    dataset: Path,
    repo_dir: Path,
    instance_filter: str,
    max_steps: int,
    suffix: str,
    rag_mode: str,
    rag_docs_roots: str,
    rag_samples_roots: str,
    rag_index_name: str,
    rag_top_k_docs: int,
    rag_top_k_code: int,
    rag_max_context_chars: int,
    rag_storage_dir: str,
) -> list[str]:
    command = [
        python_exe,
        str(ROOT / "run.py"),
        "--config_file",
        str(config_file),
        "--model_name",
        model_name,
        "--temperature",
        str(temperature),
        "--top_p",
        str(top_p),
        "--pr_file",
        str(dataset),
        "--repo_dir",
        str(repo_dir),
        "--instance_filter",
        instance_filter,
        "--max_steps_per_instance",
        str(max_steps),
        "--skip_existing",
        _as_cli_bool(False),
        "--print_config",
        _as_cli_bool(False),
        "--raise_exceptions",
        _as_cli_bool(False),
        "--skip_workdir_reset",
        _as_cli_bool(True),
        "--suffix",
        suffix,
    ]
    if rag_mode.strip().lower() not in {"", "off", "false", "0", "none"}:
        command.extend(
            [
                "--rag_mode",
                rag_mode,
                "--rag_docs_roots",
                rag_docs_roots,
                "--rag_samples_roots",
                rag_samples_roots,
                "--rag_index_name",
                rag_index_name,
                "--rag_top_k_docs",
                str(rag_top_k_docs),
                "--rag_top_k_code",
                str(rag_top_k_code),
                "--rag_max_context_chars",
                str(rag_max_context_chars),
            ]
        )
        if rag_storage_dir.strip():
            command.extend(["--rag_storage_dir", rag_storage_dir.strip()])
    return command


def build_worker_specs(
    *,
    rows: list[int],
    attempt: int,
    workers: int,
    dataset_rows: dict[int, DatasetRow],
    repo_pools: list[Path],
    output_dir: Path,
    batch_slug: str,
    batch_run_id: str,
    python_exe: str,
    config_file: Path,
    model_name: str,
    temperature: float,
    top_p: float,
    dataset: Path,
    max_steps: int,
    rag_mode: str,
    rag_docs_roots: str,
    rag_samples_roots: str,
    rag_index_name: str,
    rag_top_k_docs: int,
    rag_top_k_code: int,
    rag_max_context_chars: int,
    rag_storage_dir: str,
    task_batch_size: int = 3,
    precomputed_slots: list[Path] | None = None,
    precomputed_compatible: dict[int, tuple[Path, ...]] | None = None,
) -> list[WorkerSpec]:
    if task_batch_size < 1:
        raise ValueError("task_batch_size must be >= 1")
    specs: list[WorkerSpec] = []
    attempt_log_dir = output_dir / "run_logs" / f"attempt{attempt:02d}"
    slots = precomputed_slots or discover_repo_slots(repo_pools)
    all_compatible = (
        {row: precomputed_compatible[row] for row in rows}
        if precomputed_compatible is not None
        else compatible_slots_by_row(rows, dataset_rows, slots)
    )
    active_slots = select_base_aware_active_slots(rows, workers, slots, all_compatible)
    compatible_by_row = {
        row: tuple(slot for slot in active_slots if slot in all_compatible[row])
        for row in rows
    }
    groups: dict[tuple[str, tuple[Path, ...]], list[int]] = {}
    for row in sorted(rows, key=lambda value: (len(compatible_by_row[value]), value)):
        compatible_slots = compatible_by_row[row]
        if not compatible_slots:
            item = dataset_rows[row]
            raise SystemExit(
                f"no active repair slot contains row {row} repository {item.repo} at base {item.base_sha}"
            )
        groups.setdefault((dataset_rows[row].repo, compatible_slots), []).append(row)

    effective_batch_size = 1 if attempt > 1 else task_batch_size
    tasks: list[tuple[list[int], tuple[Path, ...]]] = []
    for (_repo, compatible_slots), group_rows in groups.items():
        for start in range(0, len(group_rows), effective_batch_size):
            tasks.append((group_rows[start : start + effective_batch_size], compatible_slots))
    desired_tasks = min(workers, len(rows))
    while len(tasks) < desired_tasks:
        split_index = max(range(len(tasks)), key=lambda index: len(tasks[index][0]))
        shard, compatible_slots = tasks[split_index]
        if len(shard) < 2:
            break
        midpoint = len(shard) // 2
        tasks[split_index : split_index + 1] = [
            (shard[:midpoint], compatible_slots),
            (shard[midpoint:], compatible_slots),
        ]

    for shard, compatible_slots in sorted(tasks, key=lambda item: (len(item[1]), item[0][0])):
        repo_dir = compatible_slots[0]
        index = active_slots.index(repo_dir) + 1
        row_label = (
            f"row_{shard[0]:04d}"
            if len(shard) == 1
            else f"rows_{shard[0]:04d}_{shard[-1]:04d}_{len(shard)}"
        )
        suffix = (
            f"modelpatch_v4_batch_{batch_slug}_{batch_run_id[:12]}_"
            f"attempt{attempt:02d}_w{index}_{row_label}"
        )
        instance_filter = build_instance_filter(shard, dataset_rows)
        command = build_run_command(
            python_exe=python_exe,
            config_file=config_file,
            model_name=model_name,
            temperature=temperature,
            top_p=top_p,
            dataset=dataset,
            repo_dir=repo_dir,
            instance_filter=instance_filter,
            max_steps=max_steps,
            suffix=suffix,
            rag_mode=rag_mode,
            rag_docs_roots=rag_docs_roots,
            rag_samples_roots=rag_samples_roots,
            rag_index_name=rag_index_name,
            rag_top_k_docs=rag_top_k_docs,
            rag_top_k_code=rag_top_k_code,
            rag_max_context_chars=rag_max_context_chars,
            rag_storage_dir=rag_storage_dir,
        )
        specs.append(
            WorkerSpec(
                attempt=attempt,
                worker=index,
                rows=shard,
                repo_dir=repo_dir,
                suffix=suffix,
                instance_filter=instance_filter,
                command=command,
                log_path=attempt_log_dir / f"{row_label}_worker{index:02d}.log",
                batch_run_id=batch_run_id,
                compatible_slots=compatible_slots,
            )
        )
    return specs


def select_base_aware_active_slots(
    rows: list[int],
    workers: int,
    slots: list[Path],
    compatible_by_row: dict[int, tuple[Path, ...]],
) -> list[Path]:
    for row in rows:
        if not compatible_by_row.get(row):
            raise SystemExit(f"no repair repo slot contains row {row} repository at its base commit")
    target = min(workers, len(rows), len(slots))
    ordered_slots = [slot.resolve() for slot in slots]
    covering: tuple[Path, ...] | None = None
    if len(ordered_slots) <= 16:
        for size in range(1, target + 1):
            for candidate in itertools.combinations(ordered_slots, size):
                if all(any(slot in compatible_by_row[row] for slot in candidate) for row in rows):
                    covering = candidate
                    break
            if covering is not None:
                break
    else:
        uncovered = set(rows)
        selected: list[Path] = []
        while uncovered and len(selected) < target:
            slot = max(
                (item for item in ordered_slots if item not in selected),
                key=lambda item: sum(item in compatible_by_row[row] for row in uncovered),
            )
            covered = {row for row in uncovered if slot in compatible_by_row[row]}
            if not covered:
                break
            selected.append(slot)
            uncovered -= covered
        if not uncovered:
            covering = tuple(selected)
    if covering is None:
        raise SystemExit(f"{target} workers cannot cover all requested repo/base pairs")

    active = list(covering)
    for slot in ordered_slots:
        if len(active) >= target:
            break
        if slot not in active and any(slot in compatible_by_row[row] for row in rows):
            active.append(slot)
    return active


def compatible_slots_by_row(
    rows: list[int],
    dataset_rows: dict[int, DatasetRow],
    slots: list[Path],
) -> dict[int, tuple[Path, ...]]:
    """Return only slot/base pairs proven usable by batched git object checks."""

    resolved_slots = [slot.resolve() for slot in slots]
    valid_bases: dict[tuple[Path, str], set[str]] = {}
    requested: dict[tuple[Path, str], set[str]] = {}
    for row in rows:
        item = dataset_rows[row]
        if not _safe_repo_name(item.repo) or not item.base_sha:
            continue
        for slot in resolved_slots:
            repo = slot / item.repo
            if (repo / ".git").exists():
                requested.setdefault((slot, item.repo), set()).add(item.base_sha)

    def inspect_repo_bases(item: tuple[tuple[Path, str], set[str]]) -> tuple[tuple[Path, str], set[str]]:
        (slot, repo_name), base_shas = item
        repo = (slot / repo_name).resolve()
        top = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        actual = top.stdout.strip().replace("\\", "/").rstrip("/")
        expected = str(repo).replace("\\", "/").rstrip("/")
        if top.returncode != 0 or actual.casefold() != expected.casefold():
            return (slot, repo_name), set()
        ordered = sorted(base_shas)
        result = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "--batch-check=%(objectname) %(objecttype)"],
            input="".join(f"{sha}^{{commit}}\n" for sha in ordered),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(30, len(ordered)),
            check=False,
        )
        if result.returncode != 0:
            return (slot, repo_name), set()
        lines = result.stdout.splitlines()
        return (slot, repo_name), {
            sha
            for sha, line in zip(ordered, lines)
            if len(line.split()) == 2 and line.split()[-1] == "commit"
        }

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(requested)))) as executor:
        for key, bases in executor.map(inspect_repo_bases, requested.items()):
            valid_bases[key] = bases

    return {
        row: tuple(
            slot
            for slot in resolved_slots
            if dataset_rows[row].base_sha in valid_bases.get((slot, dataset_rows[row].repo), set())
        )
        for row in rows
    }


def _format_command(command: list[str]) -> str:
    return " ".join(f'"{part}"' if " " in part else part for part in command)


def check_repo_slots(
    specs: list[WorkerSpec],
    dataset_rows: dict[int, DatasetRow],
    *,
    precomputed_compatible: dict[int, tuple[Path, ...]] | None = None,
) -> None:
    missing: list[str] = []
    requested_slots = sorted(
        {
            slot
            for spec in specs
            for slot in (spec.compatible_slots or (spec.repo_dir,))
        },
        key=lambda path: str(path).casefold(),
    )
    requested_rows = sorted({row for spec in specs for row in spec.rows})
    compatible = (
        {row: precomputed_compatible[row] for row in requested_rows}
        if precomputed_compatible is not None
        else compatible_slots_by_row(requested_rows, dataset_rows, requested_slots)
    )
    for spec in specs:
        if not spec.repo_dir.is_dir():
            missing.append(str(spec.repo_dir))
            continue
        for row in spec.rows:
            dataset_row = dataset_rows[row]
            for slot in spec.compatible_slots or (spec.repo_dir,):
                if slot.resolve() not in compatible.get(row, ()):
                    missing.append(
                        f"invalid slot/base row={row} slot={slot} repo={dataset_row.repo} base={dataset_row.base_sha}"
                    )
    if missing:
        raise SystemExit("invalid repair repo slots:\n" + "\n".join(missing))


def localization_preflight_failures(
    rows: list[int],
    dataset_rows: dict[int, DatasetRow],
) -> dict[int, RowStatus]:
    failures: dict[int, RowStatus] = {}
    allowed_reasons = {"issue", "gold_fix", "issue+gold_fix"}
    for row in rows:
        item = dataset_rows[row]
        test_paths = [path for path in item.defect_files if is_agent_self_test_patch_path(path)]
        policy_valid = (
            item.allow_test_patch_reason in allowed_reasons
            if item.allow_test_patch
            else item.allow_test_patch_reason == "none"
        )
        if not policy_valid:
            failures[row] = RowStatus(
                row,
                item.instance_id,
                False,
                f"localization_failure:invalid_allow_test_patch_reason={item.allow_test_patch_reason}",
            )
        elif test_paths and not item.allow_test_patch:
            failures[row] = RowStatus(
                row,
                item.instance_id,
                False,
                "localization_failure:unauthorized_test_defect_files=" + ",".join(test_paths),
            )
    return failures


def check_worker_python_runtime(python_exe: str) -> None:
    result = subprocess.run(
        [python_exe, "-B", "-c", "import run"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SystemExit(f"worker Python runtime cannot import repair engine: {python_exe}\n{detail}")


def scan_source_patch_progress(
    specs: list[WorkerSpec],
    *,
    dataset_rows: dict[int, DatasetRow],
    progress: ProgressReporter | None,
) -> None:
    if progress is None:
        return
    for spec in specs:
        trajectory_dir = spec.trajectory_dir or find_trajectory_dir(spec.suffix)
        if trajectory_dir is None:
            continue
        patch_dir = trajectory_dir / "patches"
        for row in spec.rows:
            if row in progress.done_rows and row in progress.detail_rows:
                continue
            instance_id = dataset_rows[row].instance_id
            patch_path = patch_dir / f"{instance_id}.patch"
            if patch_path.is_file() and patch_path.stat().st_size > 0:
                timing = extract_repair_timing(trajectory_dir / f"{instance_id}.traj")
                detail = (
                    format_patch_timing_detail(
                        row=row,
                        timing=timing,
                        state="generated",
                    )
                    if timing.timing_status == "ok"
                    else None
                )
                progress.mark_done(row, detail)


def run_workers(
    specs: list[WorkerSpec],
    *,
    dataset_rows: dict[int, DatasetRow],
    progress: ProgressReporter | None = None,
    worker_timeout_seconds: float = 14400.0,
    worker_start_interval_seconds: float = 0.25,
    build_concurrency: int = 8,
    defer_canonical_preprocess: bool = True,
) -> list[WorkerSpec]:
    from sweagent.environment.utils import WindowsKillOnCloseJob, terminate_process_tree

    if worker_timeout_seconds <= 0:
        raise ValueError("worker_timeout_seconds must be > 0")
    if worker_start_interval_seconds < 0:
        raise ValueError("worker_start_interval_seconds must be >= 0")
    if build_concurrency < 1:
        raise ValueError("build_concurrency must be >= 1")

    if not specs:
        return []
    env = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(key, None)
    env.pop("HDC_TARGET", None)
    env["ARKEVAL_OFFLINE_ENV_ROOT"] = str(
        (ARKEVAL_ROOT / "evaluation" / "command_line_tools_test" / "offline_env").resolve()
    )
    env["HVIGOR_USER_HOME"] = str(
        (ARKEVAL_ROOT / "evaluation" / "command_line_tools_test" / ".hvigor").resolve()
    )
    env["ARKFIX_BUILD_CONCURRENCY"] = str(build_concurrency)
    if defer_canonical_preprocess:
        env["ARKFIX_DEFER_CANONICAL_PREPROCESS"] = "1"
    else:
        env.pop("ARKFIX_DEFER_CANONICAL_PREPROCESS", None)
    batch_ids = {spec.batch_run_id for spec in specs}
    if len(batch_ids) != 1:
        raise ValueError("all workers in one launch must share exactly one batch_run_id")
    env["ARKFIX_BATCH_RUN_ID"] = next(iter(batch_ids))
    processes: list[tuple[WorkerSpec, subprocess.Popen[str], Any, float, float, WindowsKillOnCloseJob]] = []
    acquired_locks: list[FileLock] = []
    cleaned_workers: set[str] = set()
    slots_locked = False

    def slots_for(spec: WorkerSpec) -> tuple[Path, ...]:
        return spec.compatible_slots or (spec.repo_dir,)

    active_slots = sorted(
        {slot.resolve() for spec in specs for slot in slots_for(spec)},
        key=lambda path: str(path).casefold(),
    )
    slot_workers = {slot: index for index, slot in enumerate(active_slots, 1)}
    cleanup_executor = ThreadPoolExecutor(max_workers=min(2, len(active_slots)))

    def retarget_spec(spec: WorkerSpec, slot: Path) -> WorkerSpec:
        slot = slot.resolve()
        worker = slot_workers[slot]
        suffix = re.sub(r"_w\d+_", f"_w{worker}_", spec.suffix, count=1)
        command = list(spec.command)
        for flag, value in (("--repo_dir", str(slot)), ("--suffix", suffix)):
            try:
                command[command.index(flag) + 1] = value
            except (ValueError, IndexError) as exc:
                raise ValueError(f"worker command is missing {flag}: {spec.command}") from exc
        log_name = re.sub(r"_worker\d+\.log$", f"_worker{worker:02d}.log", spec.log_path.name)
        return replace(
            spec,
            worker=worker,
            repo_dir=slot,
            suffix=suffix,
            command=command,
            log_path=spec.log_path.parent / log_name,
        )

    def run_cleanup_command(command: list[str]) -> subprocess.CompletedProcess[str]:
        job = WindowsKillOnCloseJob()
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=(
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    if os.name == "nt"
                    else 0
                ),
            )
            job.assign(process)
            try:
                stdout, stderr = process.communicate(timeout=180)
            except subprocess.TimeoutExpired:
                terminate_process_tree(process)
                job.close()
                stdout, stderr = process.communicate()
                return subprocess.CompletedProcess(command, 124, stdout, stderr or "cleanup timed out")
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        finally:
            if process is not None and process.poll() is None:
                terminate_process_tree(process)
            job.close()

    def cleanup_repo_to_head(repo: Path) -> str:
        (repo / ".git" / "index.lock").unlink(missing_ok=True)
        reset_command = ["git", "-C", str(repo), "reset", "--hard", "HEAD"]
        result = run_cleanup_command(reset_command)
        output = (result.stderr or result.stdout).strip()
        if result.returncode != 0 and (
            "index file corrupt" in output.lower() or "bad signature" in output.lower()
        ):
            (repo / ".git" / "index").unlink(missing_ok=True)
            result = run_cleanup_command(reset_command)
            output = (result.stderr or result.stdout).strip()
        if result.returncode != 0:
            return f"{repo}:reset:{output or result.returncode}"
        try:
            remove_untracked_reparse_points(repo, preserve_paths=(".codephoenix",))
        except Exception as exc:
            return f"{repo}:reparse:{exc}"
        for operation, command in (
            ("clean", ["git", "-C", str(repo), "clean", "-ffdx", "-e", ".codephoenix/"]),
            ("submodule", ["git", "-C", str(repo), "submodule", "update", "--init", "--recursive", "--force"]),
        ):
            result = run_cleanup_command(command)
            output = (result.stderr or result.stdout).strip()
            if result.returncode != 0:
                return f"{repo}:{operation}:{output or result.returncode}"
        try:
            mask_windows_case_collisions(repo)
        except Exception as exc:
            return f"{repo}:case-collision:{exc}"
        result = run_cleanup_command(
            [
                "git",
                "-C",
                str(repo),
                "status",
                "--porcelain",
                "--",
                ".",
                ":(exclude).codephoenix/**",
            ]
        )
        output = (result.stderr or result.stdout).strip()
        if result.returncode != 0 or result.stdout.strip():
            return f"{repo}:status:{output or result.returncode}"
        return ""

    def cleanup_all_slot_repos_to_head(stage: str) -> str:
        repo_names = {
            dataset_rows[row].repo
            for spec in specs
            for row in spec.rows
        }
        repos = sorted(
            {
                (slot / repo_name).resolve()
                for slot in active_slots
                for repo_name in repo_names
                if (slot / repo_name / ".git").exists()
            },
            key=lambda path: str(path).casefold(),
        )
        if not repos:
            return "no dataset repository found in active slots"
        with ThreadPoolExecutor(max_workers=min(8, len(repos))) as executor:
            failures = [failure for failure in executor.map(cleanup_repo_to_head, repos) if failure]
        if failures:
            return "; ".join(failures)
        print(f"[repo-{stage}] slots={len(active_slots)} repos={len(repos)} clean_at_head", flush=True)
        return ""

    def cleanup_worker_repos(spec: WorkerSpec, *, allow_fast_path: bool = False) -> str:
        targets: dict[Path, str] = {}
        for row in spec.rows:
            dataset_row = dataset_rows[row]
            targets[(spec.repo_dir / dataset_row.repo).resolve()] = dataset_row.base_sha
        failures: list[str] = []
        for repo, base_sha in targets.items():
            (repo / ".git" / "index.lock").unlink(missing_ok=True)
            if allow_fast_path:
                status = run_cleanup_command(
                    [
                        "git",
                        "-C",
                        str(repo),
                        "status",
                        "--porcelain=v2",
                        "--branch",
                        "--ignored=matching",
                        "--untracked-files=all",
                        "--",
                        ".",
                        ":(exclude).codephoenix/**",
                    ]
                )
                status_lines = status.stdout.splitlines()
                branch_oid = next(
                    (line.removeprefix("# branch.oid ").strip() for line in status_lines if line.startswith("# branch.oid ")),
                    "",
                )
                changes = [line for line in status_lines if line and not line.startswith("# ")]
                if status.returncode == 0 and branch_oid.casefold() == base_sha.casefold() and not changes:
                    continue
            commands = [
                ["git", "-C", str(repo), "reset", "--hard", base_sha],
                ["git", "-C", str(repo), "clean", "-ffdx", "-e", ".codephoenix/"],
                ["git", "-C", str(repo), "submodule", "update", "--recursive", "--force"],
            ]
            for index, command in enumerate(commands):
                operation = command[3]
                try:
                    result = run_cleanup_command(command)
                except Exception as exc:
                    failures.append(f"{repo.name}:{operation}:{exc}")
                    break
                output = (result.stderr or result.stdout).strip()
                if (
                    index == 0
                    and result.returncode != 0
                    and ("index file corrupt" in output.lower() or "bad signature" in output.lower())
                ):
                    (repo / ".git" / "index").unlink(missing_ok=True)
                    result = run_cleanup_command(command)
                    output = (result.stderr or result.stdout).strip()
                if result.returncode != 0 or (operation == "status" and result.stdout.strip()):
                    failures.append(
                        f"{repo.name}:{operation}:{output or result.stdout.strip() or result.returncode}"
                    )
                    break
                if index == 0:
                    try:
                        remove_untracked_reparse_points(repo, preserve_paths=(".codephoenix",))
                    except Exception as exc:
                        failures.append(f"{repo.name}:reparse:{exc}")
                        break
            else:
                try:
                    mask_windows_case_collisions(repo)
                except Exception as exc:
                    failures.append(f"{repo.name}:case-collision:{exc}")
                    continue
                status_command = [
                    "git",
                    "-C",
                    str(repo),
                    "status",
                    "--porcelain",
                    "--",
                    ".",
                    ":(exclude).codephoenix/**",
                ]
                try:
                    result = run_cleanup_command(status_command)
                except Exception as exc:
                    failures.append(f"{repo.name}:status:{exc}")
                    continue
                output = (result.stderr or result.stdout).strip()
                if result.returncode != 0 or result.stdout.strip():
                    failures.append(f"{repo.name}:status:{output or result.returncode}")
        cleaned_workers.add(spec.suffix)
        return "; ".join(failures)

    lock_paths = {slot / ".arkfix.worker.lock" for slot in active_slots}
    for spec in specs:
        row = spec.rows[0]
        for slot in slots_for(spec):
            legacy_dir = slot / dataset_rows[row].repo / ".codephoenix"
            legacy_lock = legacy_dir / "localization.lock"
            if legacy_dir.is_dir():
                lock_paths.add(legacy_lock)

    try:
        for lock_path in sorted(lock_paths, key=lambda path: str(path).casefold()):
            lock = FileLock(str(lock_path))
            try:
                lock.acquire(timeout=0)
            except FileLockTimeout as exc:
                raise SystemExit(f"repair/localization repo slot is already in use: {lock_path}") from exc
            acquired_locks.append(lock)
        slots_locked = True
        pre_cleanup_error = cleanup_all_slot_repos_to_head("pre")
        if pre_cleanup_error:
            raise RuntimeError(f"repo pre-cleanup failed: {pre_cleanup_error}")

        pending_specs = sorted(specs, key=lambda spec: (len(slots_for(spec)), spec.rows[0]))
        available_slots = set(active_slots)
        running: list[
            tuple[WorkerSpec, subprocess.Popen[str], Any, float, float, WindowsKillOnCloseJob]
        ] = []
        cleaning: dict[Future[str], tuple[WorkerSpec, Any, float, int]] = {}
        completed: list[WorkerSpec] = []

        while pending_specs or running or cleaning:
            while available_slots:
                dispatchable = [
                    spec for spec in pending_specs if any(slot.resolve() in available_slots for slot in slots_for(spec))
                ]
                if not dispatchable:
                    break
                planned = min(dispatchable, key=lambda spec: (len(slots_for(spec)), spec.rows[0]))
                slot = min(
                    (slot.resolve() for slot in slots_for(planned) if slot.resolve() in available_slots),
                    key=lambda path: str(path).casefold(),
                )
                spec = retarget_spec(planned, slot)
                if processes and len(processes) < len(active_slots) and worker_start_interval_seconds:
                    time.sleep(worker_start_interval_seconds)
                spec.log_path.parent.mkdir(parents=True, exist_ok=True)
                log = spec.log_path.open("w", encoding="utf-8", newline="\n")
                log.write("[command] " + _format_command(spec.command) + "\n")
                log.flush()
                job = WindowsKillOnCloseJob()
                started_at_epoch = time.time()
                process: subprocess.Popen[str] | None = None
                process_env = env.copy()
                process_env["ARKFIX_WORKER_SLOT"] = slot.name
                process_env["ARKFIX_WORKER_SUFFIX"] = spec.suffix
                try:
                    process = subprocess.Popen(
                        spec.command,
                        cwd=str(ROOT),
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        env=process_env,
                        creationflags=(
                            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                            if os.name == "nt"
                            else 0
                        ),
                    )
                    job.assign(process)
                except Exception:
                    if process is not None and process.poll() is None:
                        terminate_process_tree(process)
                    job.close()
                    cleanup_error = cleanup_worker_repos(spec)
                    if cleanup_error:
                        log.write(f"[cleanup-error] {cleanup_error}\n")
                        log.flush()
                    log.close()
                    raise
                assert process is not None
                item = (spec, process, log, time.monotonic(), started_at_epoch, job)
                processes.append(item)
                running.append(item)
                pending_specs.remove(planned)
                available_slots.remove(slot)

            scan_source_patch_progress(
                [item[0] for item in running],
                dataset_rows=dataset_rows,
                progress=progress,
            )
            if progress is not None:
                progress.heartbeat()
            still_running: list[
                tuple[WorkerSpec, subprocess.Popen[str], Any, float, float, WindowsKillOnCloseJob]
            ] = []
            now = time.monotonic()
            for spec, process, log, started_at, started_at_epoch, job in running:
                exit_code = process.poll()
                if exit_code is None and now - started_at >= worker_timeout_seconds:
                    log.write(f"[timeout] worker exceeded {worker_timeout_seconds:g} seconds\n")
                    log.flush()
                    terminate_process_tree(process)
                    job.close()
                    exit_code = 124
                if exit_code is None:
                    still_running.append((spec, process, log, started_at, started_at_epoch, job))
                    continue
                job.close()
                future = cleanup_executor.submit(
                    cleanup_worker_repos,
                    spec,
                    allow_fast_path=(exit_code == 0),
                )
                cleaning[future] = (spec, log, started_at_epoch, exit_code)
            running = still_running

            for future in list(cleaning):
                if not future.done():
                    continue
                spec, log, started_at_epoch, exit_code = cleaning.pop(future)
                try:
                    cleanup_error = future.result()
                except Exception as exc:
                    cleanup_error = str(exc)
                if cleanup_error:
                    log.write(f"[cleanup-error] {cleanup_error}\n")
                    log.flush()
                log.close()
                completed.append(
                    replace(
                        spec,
                        started_at_epoch=started_at_epoch,
                        trajectory_dir=find_trajectory_dir(spec.suffix, not_before_epoch=started_at_epoch),
                        exit_code=exit_code,
                        cleanup_error=cleanup_error,
                    )
                )
                if not cleanup_error:
                    available_slots.add(spec.repo_dir.resolve())
            if pending_specs and not running and not cleaning and not any(
                any(slot.resolve() in available_slots for slot in slots_for(spec))
                for spec in pending_specs
            ):
                for blocked_spec in pending_specs:
                    completed.append(
                        replace(
                            blocked_spec,
                            exit_code=125,
                            cleanup_error="all compatible slots were quarantined after cleanup failure",
                        )
                    )
                pending_specs.clear()
            if running or cleaning:
                time.sleep(0.2)
        scan_source_patch_progress(completed, dataset_rows=dataset_rows, progress=progress)
        return completed
    finally:
        active_exception = sys.exc_info()[0] is not None
        post_cleanup_error = ""
        for _spec, process, log, _started_at, _started_at_epoch, job in processes:
            if process.poll() is None:
                terminate_process_tree(process)
            job.close()
        cleanup_executor.shutdown(wait=True)
        for _spec, process, log, _started_at, _started_at_epoch, job in processes:
            if _spec.suffix not in cleaned_workers:
                cleanup_error = cleanup_worker_repos(_spec)
                if cleanup_error and not log.closed:
                    log.write(f"[cleanup-error] {cleanup_error}\n")
                    log.flush()
            if not log.closed:
                log.close()
        if slots_locked:
            try:
                post_cleanup_error = cleanup_all_slot_repos_to_head("post")
            except Exception as exc:
                post_cleanup_error = str(exc)
        for lock in reversed(acquired_locks):
            lock.release()
        if post_cleanup_error:
            print(f"[repo-post-error] {post_cleanup_error}", file=sys.stderr, flush=True)
            if not active_exception:
                raise RuntimeError(f"repo post-cleanup failed: {post_cleanup_error}")


def find_trajectory_dir(
    suffix: str,
    trajectories_root: Path = DEFAULT_TRAJECTORIES_ROOT,
    *,
    not_before_epoch: float | None = None,
) -> Path | None:
    if not trajectories_root.is_dir():
        return None
    matches = [path for path in trajectories_root.glob(f"*__{suffix}") if path.is_dir()]
    if not_before_epoch is not None:
        matches = [path for path in matches if path.stat().st_mtime >= not_before_epoch - 1.0]
    if not matches:
        return None
    if len(matches) != 1:
        return None
    return matches[0]


def read_json_file(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except Exception:
        return {}


def read_source_meta(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    data = read_json_file(path)
    return data if isinstance(data, dict) else {}


def validate_patch_text(path: Path) -> tuple[bool, str, str, bytes]:
    data = path.read_bytes()
    if not data:
        return False, "", "empty_patch", data
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return False, "", f"invalid_utf8: {exc}", data
    if UNICODE_REPLACEMENT_CHAR in text:
        return False, text, "contains_unicode_replacement_char", data
    if "\x00" in text:
        return False, text, "contains_nul", data
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        return False, normalized, "empty_patch", data
    if not normalized.endswith("\n"):
        normalized += "\n"
    return True, normalized, "ok", data


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_repair_timing_from_data(data: dict[str, Any]) -> TimingInfo:
    info = data.get("info") if isinstance(data, dict) else None
    info = info if isinstance(info, dict) else {}
    repair_time = _float_or_none(info.get("edit_command_elapsed_s"))
    edit_action_elapsed = _float_or_none(info.get("edit_action_elapsed_s"))
    edit_action_count = _int_or_none(info.get("edit_action_count"))
    if repair_time is not None:
        return TimingInfo(repair_time, edit_action_elapsed, edit_action_count, "ok")

    # Compatibility fallback for partially written trajectories.
    cumulative: float | None = None
    count: int | None = edit_action_count
    trajectory = data.get("trajectory") if isinstance(data, dict) else None
    if isinstance(trajectory, list):
        for step in trajectory:
            if not isinstance(step, dict):
                continue
            timing = step.get("timing")
            if not isinstance(timing, dict):
                continue
            value = _float_or_none(timing.get("cumulative_edit_command_elapsed_s"))
            if value is not None:
                cumulative = value
            count_value = _int_or_none(timing.get("cumulative_edit_action_count"))
            if count_value is not None:
                count = count_value
    if cumulative is not None:
        return TimingInfo(cumulative, edit_action_elapsed, count, "ok")
    return TimingInfo(None, edit_action_elapsed, count, "missing_timing")


def extract_repair_timing(traj_path: Path | None) -> TimingInfo:
    if traj_path is None or not traj_path.is_file():
        return TimingInfo(None, None, None, "missing_timing")
    return _extract_repair_timing_from_data(read_json_file(traj_path))


def format_patch_timing_detail(
    *,
    row: int,
    timing: TimingInfo,
    state: str,
) -> str:
    repair_time = f"{timing.repair_time_s:.2f}s" if timing.repair_time_s is not None else "NA"
    edit_elapsed = (
        f"{timing.edit_action_elapsed_s:.2f}s"
        if timing.edit_action_elapsed_s is not None
        else "NA"
    )
    action_count = str(timing.edit_action_count) if timing.edit_action_count is not None else "NA"
    return (
        f"[patch] row{row:02d} {state} "
        f"repair_time={repair_time} "
        f"edit_action_elapsed={edit_elapsed} "
        f"edit_actions={action_count}"
    )


def format_patch_done_detail(candidate: PatchCandidate, timing: TimingInfo) -> str:
    return format_patch_timing_detail(
        row=candidate.row,
        timing=timing,
        state="written",
    )


def collect_candidates(
    completed_specs: list[WorkerSpec],
    dataset_rows: dict[int, DatasetRow],
) -> tuple[dict[int, PatchCandidate], dict[int, RowStatus]]:
    by_row: dict[int, PatchCandidate] = {}
    status: dict[int, RowStatus] = {}
    for spec in completed_specs:
        if spec.cleanup_error:
            for row in spec.rows:
                status[row] = RowStatus(
                    row,
                    dataset_rows[row].instance_id,
                    False,
                    _short_reason("repo_cleanup_failed", spec.cleanup_error),
                )
            continue
        if spec.trajectory_dir is None:
            for row in spec.rows:
                status[row] = RowStatus(row, dataset_rows[row].instance_id, False, "missing_trajectory")
            continue
        patch_dir = spec.trajectory_dir / "patches"
        for row in spec.rows:
            instance_id = dataset_rows[row].instance_id
            patch_path = patch_dir / f"{instance_id}.patch"
            meta_path = patch_dir / f"{instance_id}.meta.json"
            traj_path = spec.trajectory_dir / f"{instance_id}.traj"
            if not patch_path.is_file():
                status[row] = RowStatus(row, instance_id, False, "missing_patch")
                continue
            if not meta_path.is_file():
                status[row] = RowStatus(row, instance_id, False, "missing_patch_meta")
                continue
            if not traj_path.is_file():
                status[row] = RowStatus(row, instance_id, False, "missing_trajectory_file")
                continue
            ok, text, reason, data = validate_patch_text(patch_path)
            if not ok:
                status[row] = RowStatus(row, instance_id, False, reason)
                continue
            source_meta = read_source_meta(meta_path)
            if source_meta.get("instance_id") != instance_id:
                status[row] = RowStatus(row, instance_id, False, "patch_meta_instance_mismatch")
                continue
            if source_meta.get("batch_run_id") != spec.batch_run_id:
                status[row] = RowStatus(row, instance_id, False, "patch_meta_batch_mismatch")
                continue
            if source_meta.get("worker_slot") != spec.repo_dir.name:
                status[row] = RowStatus(row, instance_id, False, "patch_meta_slot_mismatch")
                continue
            if source_meta.get("worker_suffix") != spec.suffix:
                status[row] = RowStatus(row, instance_id, False, "patch_meta_suffix_mismatch")
                continue
            if str(source_meta.get("base_sha") or "").casefold() != dataset_rows[row].base_sha.casefold():
                status[row] = RowStatus(row, instance_id, False, "patch_meta_base_mismatch")
                continue
            if source_meta.get("allow_test_patch") is not dataset_rows[row].allow_test_patch:
                status[row] = RowStatus(row, instance_id, False, "patch_meta_test_policy_mismatch")
                continue
            if str(source_meta.get("allow_test_patch_reason") or "none") != dataset_rows[row].allow_test_patch_reason:
                status[row] = RowStatus(row, instance_id, False, "patch_meta_test_policy_reason_mismatch")
                continue
            runtime_project_path = str(source_meta.get("project_path") or "").replace("\\", "/").strip()
            runtime_defect_files = source_meta.get("defect_files")
            if not runtime_project_path or not isinstance(runtime_defect_files, list) or not runtime_defect_files:
                status[row] = RowStatus(row, instance_id, False, "missing_runtime_scope")
                continue
            runtime_project_key = runtime_project_path.casefold() if os.name == "nt" else runtime_project_path
            if runtime_project_path != "." and any(
                not (
                    str(path).replace("\\", "/").casefold()
                    if os.name == "nt"
                    else str(path).replace("\\", "/")
                ).startswith(runtime_project_key + "/")
                for path in runtime_defect_files
            ):
                status[row] = RowStatus(row, instance_id, False, "runtime_scope_mismatch")
                continue
            source_forced_submit = source_meta.get("max_steps_forced_submit")
            if type(source_forced_submit) is not bool:
                status[row] = RowStatus(row, instance_id, False, "invalid_forced_submit_provenance")
                continue
            source_patch_only = source_meta.get("patch_only_generation", False)
            if type(source_patch_only) is not bool:
                status[row] = RowStatus(row, instance_id, False, "invalid_patch_only_provenance")
                continue
            source_validation = source_meta.get("final_validation")
            if source_validation != "passed" and not (
                source_patch_only and source_validation == "patch_only_pending_serial_build"
            ):
                status[row] = RowStatus(row, instance_id, False, "final_validation_failed")
                continue
            if source_patch_only and source_meta.get("validation_status") != "patch_only_scope_apply_pending_serial_build":
                status[row] = RowStatus(row, instance_id, False, "invalid_patch_only_validation_status")
                continue
            if not str(source_meta.get("base_apply_check") or "").strip() or source_meta.get("base_apply_check") == "not_checked":
                status[row] = RowStatus(row, instance_id, False, "missing_base_apply_check")
                continue
            try:
                trajectory_bytes = traj_path.read_bytes()
                trajectory_data = json.loads(trajectory_bytes.decode("utf-8", errors="strict"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                status[row] = RowStatus(row, instance_id, False, "invalid_trajectory_json")
                continue
            if not isinstance(trajectory_data, dict) or not isinstance(trajectory_data.get("trajectory"), list):
                status[row] = RowStatus(row, instance_id, False, "invalid_trajectory_shape")
                continue
            final_info = trajectory_data.get("info")
            if not isinstance(final_info, dict) or final_info.get("exit_status") != "submitted":
                status[row] = RowStatus(row, instance_id, False, "trajectory_not_normal_submit")
                continue
            if final_info.get("max_steps_forced_submit", False) is not source_forced_submit:
                status[row] = RowStatus(row, instance_id, False, "forced_submit_provenance_mismatch")
                continue
            source_patch_sha256 = hashlib.sha256(data).hexdigest()
            if source_meta.get("patch_sha256") != source_patch_sha256:
                status[row] = RowStatus(row, instance_id, False, "patch_meta_source_sha256_mismatch")
                continue
            output_bytes = text.encode("utf-8")
            output_sha256 = hashlib.sha256(output_bytes).hexdigest()
            trajectory_sha256 = hashlib.sha256(trajectory_bytes).hexdigest()
            timing = _extract_repair_timing_from_data(trajectory_data)
            candidate = PatchCandidate(
                row=row,
                instance_id=instance_id,
                patch_path=patch_path,
                meta_path=meta_path if meta_path.is_file() else None,
                traj_path=traj_path if traj_path.is_file() else None,
                trajectory_dir=spec.trajectory_dir,
                attempt=spec.attempt,
                worker=spec.worker,
                repo_dir=spec.repo_dir,
                batch_run_id=spec.batch_run_id,
                base_sha=dataset_rows[row].base_sha,
                text=text,
                bytes_len=len(output_bytes),
                sha256=output_sha256,
                source_patch_sha256=source_patch_sha256,
                trajectory_sha256=trajectory_sha256,
                source_meta=source_meta,
                timing=timing,
            )
            by_row[row] = candidate
            status[row] = RowStatus(row, instance_id, True, "ok")
    return by_row, status


def write_outputs(
    output_dir: Path,
    dataset_rows: dict[int, DatasetRow],
    candidates: dict[int, PatchCandidate],
    target_rows: list[int],
    *,
    attempt: int,
    row_failures: dict[int, RowStatus] | None = None,
    dataset_path: Path = DEFAULT_DATASET,
    config_file: Path = DEFAULT_CONFIG,
    progress: ProgressReporter | None = None,
    publish_manifest: bool = False,
    persist: bool = True,
) -> list[RowStatus]:
    if not output_dir.is_dir():
        raise FileNotFoundError(f"batch output directory was not atomically claimed: {output_dir}")
    manifest_rows: list[dict[str, Any]] = []
    repair_rows: list[dict[str, Any]] = []
    statuses: list[RowStatus] = []
    for row in target_rows:
        dataset_row = dataset_rows[row]
        candidate = candidates.get(row)
        failure = row_failures.get(row) if row_failures else None
        if failure is not None and not failure.ok:
            if persist:
                for stale in (
                    output_dir / f"model_patch_{row}.patch",
                    output_dir / f"model_patch_{row}.meta.json",
                ):
                    try:
                        stale.unlink()
                    except FileNotFoundError:
                        pass
            statuses.append(RowStatus(row, dataset_row.instance_id, False, failure.reason))
            continue
        if candidate is None:
            statuses.append(
                RowStatus(
                    row,
                    dataset_row.instance_id,
                    False,
                    failure.reason if failure is not None else "missing_valid_patch",
                )
            )
            continue

        patch_target = output_dir / f"model_patch_{row}.patch"
        meta_target = output_dir / f"model_patch_{row}.meta.json"
        if persist:
            atomic_write_text(patch_target, candidate.text)

        timing = candidate.timing
        public_meta = {
            "row": row,
            "instance_id": candidate.instance_id,
            "attempt": candidate.attempt,
            "worker": candidate.worker,
            "slot": candidate.repo_dir.name,
            "repo_dir": str(candidate.repo_dir),
            "batch_run_id": candidate.batch_run_id,
            "base_sha": candidate.base_sha,
            "project_path": candidate.source_meta.get("project_path", ""),
            "defect_files": candidate.source_meta.get("defect_files", []),
            "allow_test_patch": candidate.source_meta.get("allow_test_patch", False),
            "allow_test_patch_reason": candidate.source_meta.get("allow_test_patch_reason", "none"),
            "max_steps_forced_submit": candidate.source_meta.get("max_steps_forced_submit", False),
            "source_trajectory": str(candidate.trajectory_dir),
            "source_patch": str(candidate.patch_path),
            "source_meta": str(candidate.meta_path) if candidate.meta_path else "",
            "source_traj": str(candidate.traj_path) if candidate.traj_path else "",
            "target_patch": str(patch_target),
            "bytes": candidate.bytes_len,
            "source_patch_sha256": candidate.source_patch_sha256,
            "output_patch_sha256": candidate.sha256,
            "trajectory_sha256": candidate.trajectory_sha256,
            "repair_time_s": timing.repair_time_s,
            "edit_action_count": timing.edit_action_count,
            "edit_action_elapsed_s": timing.edit_action_elapsed_s,
            "timing_status": timing.timing_status,
            "base_apply_check": candidate.source_meta.get("base_apply_check", ""),
            "validation_status": candidate.source_meta.get("validation_status", ""),
            "source_patch_meta": candidate.source_meta,
        }
        if persist:
            atomic_write_text(
                meta_target,
                json.dumps(public_meta, ensure_ascii=False, indent=2) + "\n",
            )
        if progress is not None:
            progress.mark_done(row, format_patch_done_detail(candidate, timing))
        manifest_rows.append(
            {
                "row": row,
                "instance_id": candidate.instance_id,
                "target_patch": str(patch_target),
                "target_meta": str(meta_target),
                "source_patch": str(candidate.patch_path),
                "source_meta": str(candidate.meta_path) if candidate.meta_path else "",
                "attempt": candidate.attempt,
                "worker": candidate.worker,
                "slot": candidate.repo_dir.name,
                "batch_run_id": candidate.batch_run_id,
                "base_sha": candidate.base_sha,
                "project_path": candidate.source_meta.get("project_path", ""),
                "max_steps_forced_submit": candidate.source_meta.get("max_steps_forced_submit", False),
                "bytes": candidate.bytes_len,
                "source_patch_sha256": candidate.source_patch_sha256,
                "output_patch_sha256": candidate.sha256,
                "trajectory_sha256": candidate.trajectory_sha256,
                "repair_time_s": timing.repair_time_s,
                "timing_status": timing.timing_status,
            }
        )
        repair_rows.append(
            {
                "row": row,
                "instance_id": candidate.instance_id,
                "repair_time_s": timing.repair_time_s,
                "edit_action_count": timing.edit_action_count,
                "edit_action_elapsed_s": timing.edit_action_elapsed_s,
                "timing_status": timing.timing_status,
                "attempt": candidate.attempt,
                "worker": candidate.worker,
                "patch": str(patch_target),
                "source_trajectory": str(candidate.trajectory_dir),
            }
        )
        statuses.append(RowStatus(row, candidate.instance_id, True, "ok"))

    if not persist:
        return statuses

    repair_summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "attempt": attempt,
        "dataset_sha256": file_sha256(dataset_path) if dataset_path.is_file() else "",
        "config_sha256": file_sha256(config_file) if config_file.is_file() else "",
        "total": len(repair_rows),
        "missing_timing": sum(1 for item in repair_rows if item["timing_status"] != "ok"),
        "rows": repair_rows,
    }
    atomic_write_text(
        output_dir / "repair_times.json",
        json.dumps(repair_summary, ensure_ascii=False, indent=2) + "\n",
    )
    fieldnames = [
        "row",
        "instance_id",
        "repair_time_s",
        "edit_action_count",
        "edit_action_elapsed_s",
        "timing_status",
        "attempt",
        "worker",
        "patch",
        "source_trajectory",
    ]
    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(repair_rows)
    atomic_write_text(output_dir / "repair_times.csv", csv_buffer.getvalue())
    write_time_log(output_dir, attempt=attempt, repair_rows=repair_rows)

    self_check = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "attempt": attempt,
        "total": len(target_rows),
        "valid": sum(1 for item in statuses if item.ok),
        "invalid": sum(1 for item in statuses if not item.ok),
        "rows": [item.__dict__ for item in statuses],
    }
    atomic_write_text(
        output_dir / "self_check.json",
        json.dumps(self_check, ensure_ascii=False, indent=2) + "\n",
    )
    if publish_manifest:
        if any(not item.ok for item in statuses):
            raise ValueError("cannot publish a completion manifest with invalid rows")
        atomic_write_text(
            output_dir / "manifest.jsonl",
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in manifest_rows),
        )
    return statuses


def merge_candidates(existing: dict[int, PatchCandidate], new: dict[int, PatchCandidate]) -> dict[int, PatchCandidate]:
    merged = dict(existing)
    merged.update(new)
    return merged


def _short_reason(prefix: str, message: str, limit: int = 500) -> str:
    compact = " ".join(str(message or "").split())
    if len(compact) > limit:
        compact = compact[:limit] + "..."
    return f"{prefix}: {compact}" if compact else prefix


def write_time_log(output_dir: Path, *, attempt: int, repair_rows: list[dict[str, Any]]) -> None:
    ok_times = [
        float(item["edit_action_elapsed_s"])
        for item in repair_rows
        if item.get("timing_status") == "ok" and item.get("edit_action_elapsed_s") is not None
    ]
    total_s = round(sum(ok_times), 2)
    avg_s = round(total_s / len(ok_times), 2) if ok_times else None
    missing_timing = sum(1 for item in repair_rows if item.get("timing_status") != "ok")
    lines = [
        f"generated_at: {datetime.now().isoformat(timespec='seconds')}",
        f"attempt: {attempt}",
        "time_scope: repair edit-action time (model wait for edit action + edit command execution)",
        f"patch_count: {len(repair_rows)}",
        f"timing_ok_count: {len(ok_times)}",
        f"missing_timing_count: {missing_timing}",
        f"total_repair_time_s: {total_s:.2f}",
        f"total_repair_time_min: {total_s / 60:.2f}",
        f"avg_repair_time_s: {avg_s:.2f}" if avg_s is not None else "avg_repair_time_s: NA",
        "",
        "row,instance_id,repair_time_s,edit_action_elapsed_s,edit_command_elapsed_s,edit_action_count,timing_status,attempt,worker",
    ]
    for item in sorted(repair_rows, key=lambda row: int(row["row"])):
        repair_time = item.get("repair_time_s")
        edit_elapsed = item.get("edit_action_elapsed_s")
        lines.append(
            ",".join(
                [
                    str(item.get("row", "")),
                    str(item.get("instance_id", "")),
                    f"{float(edit_elapsed):.2f}" if edit_elapsed is not None else "",
                    f"{float(edit_elapsed):.2f}" if edit_elapsed is not None else "",
                    f"{float(repair_time):.2f}" if repair_time is not None else "",
                    str(item.get("edit_action_count", "")),
                    str(item.get("timing_status", "")),
                    str(item.get("attempt", "")),
                    str(item.get("worker", "")),
                ]
            )
        )
    atomic_write_text(output_dir / "time.log", "\n".join(lines) + "\n")


def serial_eval_apply_check(
    *,
    output_dir: Path,
    dataset_entries: dict[int, dict[str, Any]],
    dataset_rows: dict[int, DatasetRow],
    candidates: dict[int, PatchCandidate],
    target_rows: list[int],
    repo_root: Path,
    deveco_path: str,
) -> dict[int, RowStatus]:
    from sweagent.environment.utils import native_build_permit
    from evaluation.run_llm_patch_eval import (
        _determine_evaluation_scope,
        apply_patch,
        reset_repo,
        run_build,
        run_environment_preprocess,
    )

    statuses: dict[int, RowStatus] = {}
    _ = repo_root  # Kept for CLI compatibility; normal flow uses each candidate's actual slot.
    acquired_locks: list[FileLock] = []
    lock_paths = {
        candidate.repo_dir / ".arkfix.worker.lock"
        for row in target_rows
        if (candidate := candidates.get(row)) is not None
    }
    for row in target_rows:
        candidate = candidates.get(row)
        if candidate is None:
            continue
        legacy_dir = candidate.repo_dir / dataset_rows[row].repo / ".codephoenix"
        legacy_lock = legacy_dir / "localization.lock"
        if legacy_dir.is_dir():
            lock_paths.add(legacy_lock)
    try:
        for lock_path in sorted(lock_paths, key=lambda path: str(path).casefold()):
            lock = FileLock(str(lock_path))
            try:
                lock.acquire(timeout=0)
            except FileLockTimeout as exc:
                raise SystemExit(f"serial apply-check slot is already in use: {lock_path}") from exc
            acquired_locks.append(lock)

        for row in target_rows:
            dataset_row = dataset_rows[row]
            candidate = candidates.get(row)
            if candidate is None:
                statuses[row] = RowStatus(row, dataset_row.instance_id, False, "missing_patch")
                continue
            patch_text = candidate.text
            if not patch_text.strip():
                statuses[row] = RowStatus(row, dataset_row.instance_id, False, "empty_patch")
                continue

            benchmark_entry = dataset_entries.get(row)
            if not benchmark_entry:
                statuses[row] = RowStatus(row, dataset_row.instance_id, False, "missing_benchmark_entry")
                continue
            repo_dir: Path | None = None
            base_sha = ""
            try:
                repo_dir = (candidate.repo_dir / dataset_row.repo).resolve()
                repo_dir.relative_to(candidate.repo_dir.resolve())
                if not (repo_dir / ".git").exists():
                    statuses[row] = RowStatus(
                        row, dataset_row.instance_id, False, f"repo_not_found:{dataset_row.repo}"
                    )
                    continue
                base_sha = str((benchmark_entry.get("base") or {}).get("sha") or "").strip()
                reset_ok, reset_msg = reset_repo(repo_dir, base_sha)
                if not reset_ok:
                    statuses[row] = RowStatus(
                        row,
                        dataset_row.instance_id,
                        False,
                        _short_reason("reset_failed", reset_msg),
                    )
                    continue
                project_path = str(candidate.source_meta.get("project_path") or "").replace("\\", "/").strip()
                project_dir = repo_dir if project_path == "." else (repo_dir / project_path).resolve()
                project_dir.relative_to(repo_dir.resolve())
                if not project_dir.is_dir() or not (project_dir / "build-profile.json5").is_file():
                    statuses[row] = RowStatus(
                        row,
                        dataset_row.instance_id,
                        False,
                        f"runtime_project_not_found:{project_path}",
                    )
                    continue
                apply_ok, apply_msg = apply_patch(repo_dir, patch_text, "model_patch_serial_check")
                if not apply_ok:
                    statuses[row] = RowStatus(
                        row,
                        dataset_row.instance_id,
                        False,
                        _short_reason("leaderboard_apply_error", apply_msg),
                    )
                    continue
                with native_build_permit():
                    preprocess_code, preprocess_out = run_environment_preprocess(project_dir, deveco_path)
                    build_code = -1
                    build_out = ""
                    scope: dict[str, Any] = {}
                    if preprocess_code == 0:
                        runtime_entry = dict(benchmark_entry)
                        runtime_entry["project_path"] = project_path
                        runtime_entry["defect_files"] = candidate.source_meta.get("defect_files", [])
                        scope = _determine_evaluation_scope(
                            project_dir,
                            runtime_entry,
                            patch_text,
                            "",
                        )
                        build_code, build_out = run_build(
                            project_dir,
                            deveco_path,
                            scope=scope,
                        )
                if preprocess_code != 0:
                    statuses[row] = RowStatus(
                        row,
                        dataset_row.instance_id,
                        False,
                        _short_reason("leaderboard_preprocess_failed", preprocess_out),
                    )
                    continue
                if build_code != 0:
                    statuses[row] = RowStatus(
                        row,
                        dataset_row.instance_id,
                        False,
                        _short_reason("serial_build_failed", build_out),
                    )
                    continue
                build_marker = next(
                    (
                        marker
                        for marker in ("BUILD_STATUS=SUCCESS", "COMPILE RESULT:SUCCESS", "BUILD SUCCESSFUL")
                        if marker in build_out
                    ),
                    "",
                )
                if not build_marker or "BUILD_STATUS=SKIPPED" in build_out:
                    statuses[row] = RowStatus(
                        row,
                        dataset_row.instance_id,
                        False,
                        "serial_build_missing_success_marker",
                    )
                    continue
                candidate.source_meta["serial_validation"] = {
                    "build_exit_code": build_code,
                    "build_success_marker": build_marker,
                    "build_output_sha256": hashlib.sha256(build_out.encode("utf-8")).hexdigest(),
                    "build_scope": scope,
                    "validated_at": datetime.now().isoformat(timespec="seconds"),
                }
                candidate.source_meta["validation_status"] = "validated_serial_build_scope_apply"
                statuses[row] = RowStatus(row, dataset_row.instance_id, True, "ok")
            except Exception as exc:
                statuses[row] = RowStatus(
                    row,
                    dataset_row.instance_id,
                    False,
                    _short_reason("serial_apply_check_exception", str(exc)),
                )
            finally:
                if repo_dir is not None and base_sha:
                    reset_ok, reset_msg = reset_repo(repo_dir, base_sha)
                    if not reset_ok:
                        statuses[row] = RowStatus(
                            row,
                            dataset_row.instance_id,
                            False,
                            _short_reason("serial_cleanup_failed", reset_msg),
                        )
    finally:
        for lock in reversed(acquired_locks):
            lock.release()
    return statuses


def copy_final_fail_report(output_dir: Path, statuses: list[RowStatus], worker_specs: list[WorkerSpec]) -> None:
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "missing_or_invalid_rows": [item.__dict__ for item in statuses if not item.ok],
        "workers": [
            {
                "attempt": spec.attempt,
                "worker": spec.worker,
                "rows": spec.rows,
                "repo_dir": str(spec.repo_dir),
                "slot": spec.repo_dir.name,
                "batch_run_id": spec.batch_run_id,
                "started_at_epoch": spec.started_at_epoch,
                "suffix": spec.suffix,
                "log_path": str(spec.log_path),
                "trajectory_dir": str(spec.trajectory_dir) if spec.trajectory_dir else "",
                "exit_code": spec.exit_code,
                "cleanup_error": spec.cleanup_error,
            }
            for spec in worker_specs
        ],
    }
    atomic_write_text(
        output_dir / "batch_failure_report.json",
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
    )


def write_batch_metadata(
    *,
    output_dir: Path,
    args: argparse.Namespace,
    dataset: Path,
    config_file: Path,
    specs: list[WorkerSpec],
) -> None:
    data = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "batch_run_id": specs[0].batch_run_id if specs else "",
        "model_name": args.model_name,
        "temperature": args.temperature,
        "top_p": args.top_p,
        "max_steps_per_instance": args.max_steps_per_instance,
        "workers": args.workers,
        "worker_timeout_seconds": args.worker_timeout_seconds,
        "worker_start_interval_seconds": args.worker_start_interval_seconds,
        "worker_task_batch_size": args.worker_task_batch_size,
        "build_concurrency": args.build_concurrency,
        "worker_plan": [
            {
                "worker": spec.worker,
                "rows": spec.rows,
                "repo_dir": str(spec.repo_dir),
                "slot": spec.repo_dir.name,
                "compatible_slots": [slot.name for slot in spec.compatible_slots],
                "batch_run_id": spec.batch_run_id,
                "suffix": spec.suffix,
                "started_at_epoch": spec.started_at_epoch,
                "exit_code": spec.exit_code,
                "cleanup_error": spec.cleanup_error,
            }
            for spec in specs
        ],
        "dataset": str(dataset),
        "dataset_sha256": file_sha256(dataset) if dataset.is_file() else "",
        "config_file": str(config_file),
        "config_sha256": file_sha256(config_file) if config_file.is_file() else "",
        "serial_apply_check": bool(args.serial_apply_check),
        "apply_check_repo_root": str(args.apply_check_repo_root),
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
    }
    atomic_write_text(
        output_dir / "batch_metadata.json",
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )


def load_resume_specs(output_dir: Path) -> tuple[str, list[WorkerSpec]]:
    metadata_path = output_dir / "batch_metadata.json"
    report_path = output_dir / "batch_failure_report.json"
    if not metadata_path.is_file():
        raise FileNotFoundError("--resume-existing requires batch_metadata.json")
    metadata = read_json_file(metadata_path)
    batch_run_id = str(metadata.get("batch_run_id") or "").strip()
    if report_path.is_file():
        workers = read_json_file(report_path).get("workers")
    else:
        workers = metadata.get("worker_plan")
    if not batch_run_id or not isinstance(workers, list):
        raise ValueError("invalid resume metadata")
    specs: list[WorkerSpec] = []
    for item in workers:
        if not isinstance(item, dict) or item.get("batch_run_id") != batch_run_id:
            raise ValueError("resume worker batch_run_id mismatch")
        suffix = str(item["suffix"])
        attempt_match = re.search(r"_attempt(\d+)_", suffix)
        attempt = int(item.get("attempt") or (attempt_match.group(1) if attempt_match else 1))
        worker = int(item["worker"])
        rows = [int(row) for row in item["rows"]]
        log_path = str(item.get("log_path") or "").strip()
        if not log_path:
            row_label = "row_" + "_".join(f"{row:04d}" for row in rows)
            log_path = str(output_dir / "run_logs" / f"attempt{attempt:02d}" / f"{row_label}_worker{worker:02d}.log")
        trajectory = str(item.get("trajectory_dir") or "").strip()
        if not trajectory:
            recovered = find_trajectory_dir(suffix, not_before_epoch=item.get("started_at_epoch"))
            trajectory = str(recovered) if recovered else ""
        specs.append(
            WorkerSpec(
                attempt=attempt,
                worker=worker,
                rows=rows,
                repo_dir=Path(item["repo_dir"]).resolve(),
                suffix=suffix,
                instance_filter="",
                command=[],
                log_path=Path(log_path).resolve(),
                batch_run_id=batch_run_id,
                started_at_epoch=item.get("started_at_epoch"),
                trajectory_dir=Path(trajectory).resolve() if trajectory else None,
                exit_code=item.get("exit_code"),
                cleanup_error=str(item.get("cleanup_error") or ""),
            )
        )
    return batch_run_id, specs


def all_workers_failed_before_trajectory(completed: list[WorkerSpec]) -> bool:
    return bool(completed) and all(
        spec.exit_code not in (0, None) and spec.trajectory_dir is None
        for spec in completed
    )


def maybe_build_rag_index(args: argparse.Namespace) -> None:
    if not args.rag_build_index:
        return
    if args.rag_mode.strip().lower() in {"", "off", "false", "0", "none"}:
        print("[rag] --rag-build-index ignored because --rag-mode is off", flush=True)
        return
    from rag.config import RagConfig
    from rag.index import build_index

    cfg = RagConfig.from_values(
        mode=args.rag_mode,
        docs_roots=args.rag_docs_roots,
        samples_roots=args.rag_samples_roots,
        index_name=args.rag_index_name,
        top_k_docs=args.rag_top_k_docs,
        top_k_code=args.rag_top_k_code,
        max_context_chars=args.rag_max_context_chars,
        storage_dir=args.rag_storage_dir or None,
    )
    stats = build_index(cfg, full=True)
    print(
        "[rag] built index "
        f"name={stats.index_name} docs_chunks={stats.docs_chunks} code_chunks={stats.code_chunks} "
        f"dim={stats.embedding_dim} sidecar={stats.sidecar_path}",
        flush=True,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run ArkTS v4 model-patch generation with retries.")
    parser.add_argument("--model-name", default="MiniMax-M2.5")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--config-file", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--repo-pool", type=Path, default=DEFAULT_REPO_POOL)
    parser.add_argument(
        "--repo-pools",
        default="",
        help="Optional comma/semicolon separated repo pools. Existing runNN slots are discovered dynamically.",
    )
    parser.add_argument("--repo-slot-start", type=int, default=0)
    parser.add_argument("--repo-slot-end", type=int, default=0)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--timestamp", default="", help="Override model_<timestamp> directory stamp.")
    parser.add_argument("--rows", default="", help="Rows to run, e.g. 1,4,8-12. Default: all dataset rows.")
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--worker-timeout-seconds", type=float, default=14400.0)
    parser.add_argument("--worker-start-interval-seconds", type=float, default=0.25)
    parser.add_argument("--worker-task-batch-size", type=int, default=3)
    parser.add_argument("--build-concurrency", type=int, default=8)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--max-steps-per-instance", type=int, default=50)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--top-p", type=float, default=1.0)
    parser.add_argument("--python-exe", default=default_worker_python())
    parser.add_argument("--serial-apply-check", dest="serial_apply_check", action="store_true", default=False)
    parser.add_argument("--no-serial-apply-check", dest="serial_apply_check", action="store_false")
    parser.add_argument("--apply-check-repo-root", type=Path, default=DEFAULT_REPO_POOL / "run01")
    parser.add_argument("--deveco-path", default="")
    parser.add_argument("--rag-mode", default="off", choices=["off", "on"])
    parser.add_argument("--rag-docs-roots", default="")
    parser.add_argument("--rag-samples-roots", default="")
    parser.add_argument("--rag-index-name", default="arkfix_default")
    parser.add_argument("--rag-top-k-docs", type=int, default=4)
    parser.add_argument("--rag-top-k-code", type=int, default=4)
    parser.add_argument("--rag-max-context-chars", type=int, default=12000)
    parser.add_argument("--rag-storage-dir", default="")
    parser.add_argument("--rag-build-index", action="store_true")
    parser.add_argument(
        "--resume-existing",
        action="store_true",
        help="Resume a failed batch in the existing model_<timestamp> directory.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-preflight", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        os.environ.pop(key, None)
    preferred_python = Path(default_worker_python()).resolve()
    current_python = Path(sys.executable).resolve()
    if current_python != preferred_python and os.environ.get("ARKFIX_BATCH_ORCHESTRATOR_REEXEC") != "1":
        child_env = os.environ.copy()
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            child_env.pop(key, None)
        child_env["ARKFIX_BATCH_ORCHESTRATOR_REEXEC"] = "1"
        child_args = list(argv) if argv is not None else sys.argv[1:]
        return subprocess.call(
            [str(preferred_python), str(Path(__file__).resolve()), *child_args],
            cwd=str(ROOT),
            env=child_env,
        )
    dataset = args.dataset.resolve()
    config_file = args.config_file.resolve()
    if is_patch_only_config(config_file) and not args.serial_apply_check:
        raise ValueError("patch-only generation requires --serial-apply-check for real build validation")
    repo_pool = args.repo_pool.resolve()
    if args.repo_pools.strip():
        repo_pools = [
            Path(part.strip()).resolve()
            for part in re.split(r"[;,]", args.repo_pools)
            if part.strip()
        ]
    else:
        repo_pools = [repo_pool]
    output_root = args.output_root.resolve()
    apply_check_repo_root = args.apply_check_repo_root.resolve()
    dataset_rows = load_dataset(dataset)
    dataset_entries = load_benchmark_entries(dataset)
    target_rows = parse_row_spec(args.rows, max_row=max(dataset_rows))
    localization_failures = localization_preflight_failures(target_rows, dataset_rows)
    eligible_rows = [row for row in target_rows if row not in localization_failures]
    repo_slots = discover_repo_slots(repo_pools)
    if args.repo_slot_start or args.repo_slot_end:
        slot_start = args.repo_slot_start or 1
        slot_end = args.repo_slot_end or 9999
        if slot_start > slot_end:
            raise ValueError("--repo-slot-start cannot exceed --repo-slot-end")
        repo_slots = [
            slot
            for slot in repo_slots
            if slot.name.startswith("run")
            and slot.name[3:].isdigit()
            and slot_start <= int(slot.name[3:]) <= slot_end
        ]
        if not repo_slots:
            raise ValueError(f"no repo slots found in requested range run{slot_start:02d}-run{slot_end:02d}")
    compatible_slots = compatible_slots_by_row(eligible_rows, dataset_rows, repo_slots)
    stamp = args.timestamp or datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    batch_slug = stamp.replace("-", "_")
    output_dir = output_root / f"model_{stamp}"
    progress = ProgressReporter(total=len(target_rows), label="model_patch")

    completed_specs: list[WorkerSpec] = []
    collected: dict[int, PatchCandidate] = {}
    rows_to_run = eligible_rows
    final_statuses: list[RowStatus] = list(localization_failures.values())
    metadata_written = False
    output_claimed = False
    first_attempt = 1
    quarantined_slots: set[Path] = set()
    if args.resume_existing:
        if args.dry_run:
            raise ValueError("--resume-existing cannot be combined with --dry-run")
        batch_run_id, completed_specs = load_resume_specs(output_dir)
        metadata = read_json_file(output_dir / "batch_metadata.json")
        expected = {
            "dataset_sha256": file_sha256(dataset),
            "config_sha256": file_sha256(config_file),
            "model_name": args.model_name,
        }
        for key, value in expected.items():
            if metadata.get(key) != value:
                raise ValueError(f"resume metadata mismatch: {key}")
        collected, _ = collect_candidates(completed_specs, dataset_rows)
        rows_to_run = [row for row in eligible_rows if row not in collected]
        quarantined_slots = set()
        first_attempt = max((spec.attempt for spec in completed_specs), default=0) + 1
        output_claimed = True
        metadata_written = True
        for row, candidate in sorted(collected.items()):
            progress.mark_done(row, format_patch_timing_detail(
                row=row,
                timing=extract_repair_timing(candidate.traj_path),
                state="reused",
            ))
        print(
            f"[resume] model_patch reused={len(collected)} pending={len(rows_to_run)} "
            f"quarantined_slots={len(quarantined_slots)} output={output_dir}",
            flush=True,
        )
    else:
        batch_run_id = uuid.uuid4().hex
    os.environ["ARKFIX_BUILD_CONCURRENCY"] = str(args.build_concurrency)

    if not args.dry_run and not args.skip_preflight:
        check_worker_python_runtime(args.python_exe)

    for failure in localization_failures.values():
        print(f"[localization-failure] row={failure.row} {failure.reason}", file=sys.stderr)
    if not eligible_rows:
        if args.dry_run:
            return 0
        output_dir.mkdir(parents=True, exist_ok=False)
        copy_final_fail_report(output_dir, final_statuses, completed_specs)
        print(f"[failed] report={output_dir / 'batch_failure_report.json'}", file=sys.stderr)
        return 1

    for attempt in range(first_attempt, args.max_retries + 2):
        available_slots = [
            slot for slot in repo_slots if slot.resolve() not in quarantined_slots
        ]
        available_compatible = {
            row: tuple(
                slot
                for slot in compatible_slots[row]
                if slot.resolve() not in quarantined_slots
            )
            for row in rows_to_run
        }
        unavailable_rows = [row for row, slots in available_compatible.items() if not slots]
        if unavailable_rows:
            final_statuses = [
                RowStatus(
                    row,
                    dataset_rows[row].instance_id,
                    False,
                    "all_compatible_slots_quarantined",
                )
                for row in unavailable_rows
            ]
            copy_final_fail_report(output_dir, final_statuses, completed_specs)
            print(f"[failed] all compatible slots quarantined for rows={unavailable_rows}", file=sys.stderr)
            return 2
        specs = build_worker_specs(
            rows=rows_to_run,
            attempt=attempt,
            workers=args.workers,
            dataset_rows=dataset_rows,
            repo_pools=repo_pools,
            output_dir=output_dir,
            batch_slug=batch_slug,
            batch_run_id=batch_run_id,
            python_exe=args.python_exe,
            config_file=config_file,
            model_name=args.model_name,
            temperature=args.temperature,
            top_p=args.top_p,
            dataset=dataset,
            max_steps=args.max_steps_per_instance,
            rag_mode=args.rag_mode,
            rag_docs_roots=args.rag_docs_roots,
            rag_samples_roots=args.rag_samples_roots,
            rag_index_name=args.rag_index_name,
            rag_top_k_docs=args.rag_top_k_docs,
            rag_top_k_code=args.rag_top_k_code,
            rag_max_context_chars=args.rag_max_context_chars,
            rag_storage_dir=args.rag_storage_dir,
            task_batch_size=args.worker_task_batch_size,
            precomputed_slots=available_slots,
            precomputed_compatible=available_compatible,
        )
        if args.dry_run:
            print(f"[attempt {attempt}] rows={','.join(str(row) for row in rows_to_run)}")
            for spec in specs:
                print(
                    f"[worker {spec.worker:02d}] rows={','.join(str(row) for row in spec.rows)} "
                    f"repo={spec.repo_dir} compatible={','.join(slot.name for slot in spec.compatible_slots)} "
                    f"suffix={spec.suffix}"
                )
                print("  " + _format_command(spec.command))
            print(f"[dry-run] output_dir={output_dir}")
            return 0

        if not output_claimed:
            output_dir.mkdir(parents=True, exist_ok=False)
            output_claimed = True
            print(f"[start] model_patch rows={len(target_rows)} output={output_dir}", flush=True)
            progress.print_status(row=None, force=True)
            maybe_build_rag_index(args)
        if not metadata_written:
            write_batch_metadata(
                output_dir=output_dir,
                args=args,
                dataset=dataset,
                config_file=config_file,
                specs=specs,
            )
            metadata_written = True
        completed = run_workers(
            specs,
            dataset_rows=dataset_rows,
            progress=progress,
            worker_timeout_seconds=args.worker_timeout_seconds,
            worker_start_interval_seconds=args.worker_start_interval_seconds,
            build_concurrency=args.build_concurrency,
            defer_canonical_preprocess=args.serial_apply_check,
        )
        completed_specs.extend(completed)
        quarantined_slots.update(
            spec.repo_dir.resolve() for spec in completed if spec.cleanup_error
        )
        write_batch_metadata(
            output_dir=output_dir,
            args=args,
            dataset=dataset,
            config_file=config_file,
            specs=completed_specs,
        )
        if all_workers_failed_before_trajectory(completed):
            final_statuses = list(localization_failures.values()) + [
                RowStatus(row, dataset_rows[row].instance_id, False, "worker_failed_before_trajectory")
                for row in rows_to_run
            ]
            copy_final_fail_report(output_dir, final_statuses, completed_specs)
            print(
                "[failed] all workers exited before creating trajectories; "
                "check run_logs for argument/import/environment errors",
                file=sys.stderr,
            )
            print(f"[failed] report={output_dir / 'batch_failure_report.json'}", file=sys.stderr)
            return 2
        new_candidates, candidate_status = collect_candidates(completed, dataset_rows)
        collected = merge_candidates(collected, new_candidates)
        row_failures = dict(localization_failures)
        row_failures.update(candidate_status)
        final_statuses = write_outputs(
            output_dir,
            dataset_rows,
            collected,
            target_rows,
            attempt=attempt,
            row_failures=row_failures,
            dataset_path=dataset,
            config_file=config_file,
            progress=progress,
            persist=False,
        )
        rows_to_run = [
            item.row for item in final_statuses if not item.ok and item.row not in localization_failures
        ]
        if rows_to_run:
            continue
        if args.serial_apply_check:
            try:
                from evaluation.run_llm_patch_eval import _find_deveco_path
            except Exception as exc:
                raise RuntimeError(f"cannot import leaderboard apply-check components: {exc}") from exc
            deveco_path = args.deveco_path.strip() or _find_deveco_path()
            if not deveco_path:
                raise RuntimeError("DEVECO_PATH is required for --serial-apply-check")
            serial_status = serial_eval_apply_check(
                output_dir=output_dir,
                dataset_entries=dataset_entries,
                dataset_rows=dataset_rows,
                candidates=collected,
                target_rows=eligible_rows,
                repo_root=apply_check_repo_root,
                deveco_path=deveco_path,
            )
            row_failures.update({row: status for row, status in serial_status.items() if not status.ok})
            final_statuses = write_outputs(
                output_dir,
                dataset_rows,
                collected,
                target_rows,
                attempt=attempt,
                row_failures=row_failures,
                dataset_path=dataset,
                config_file=config_file,
                progress=progress,
                persist=False,
            )
        rows_to_run = [
            item.row for item in final_statuses if not item.ok and item.row not in localization_failures
        ]
        if rows_to_run:
            continue
        if localization_failures:
            rows_to_run = sorted(localization_failures)
            break
        (output_dir / "batch_failure_report.json").unlink(missing_ok=True)
        final_statuses = write_outputs(
            output_dir,
            dataset_rows,
            collected,
            target_rows,
            attempt=attempt,
            dataset_path=dataset,
            config_file=config_file,
            progress=progress,
            publish_manifest=True,
        )
        print(f"[success] wrote model patches to {output_dir}")
        return 0

    if args.dry_run:
        print(f"[dry-run] output_dir={output_dir}")
        return 0

    copy_final_fail_report(output_dir, final_statuses, completed_specs)
    print(f"[failed] missing_or_invalid_rows={rows_to_run}", file=sys.stderr)
    print(f"[failed] report={output_dir / 'batch_failure_report.json'}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
