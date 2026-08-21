#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


MODULE_DIR = Path(__file__).resolve().parent
ARKEVAL_ROOT = MODULE_DIR.parent
TOTAL_ROWS = 502
DEFAULT_DATASET = ARKEVAL_ROOT / "dataset" / "arkeval_dataset.jsonl"
DEFAULT_ROW_RUNS = ARKEVAL_ROOT / "dataset" / "test_out"
DEFAULT_REPO_ROOT = ARKEVAL_ROOT / "depend" / "repair_repo" / "run01"
DEFAULT_DEVECO_PATH = ARKEVAL_ROOT / "depend" / "harmony_env" / "deveco"
EVAL_SCRIPT = ARKEVAL_ROOT / "evaluation" / "run_llm_patch_eval.py"
EVAL_TOOLS_DIR = ARKEVAL_ROOT / "evaluation" / "command_line_tools_test" / "tools"

BENCHMARK_DIR = MODULE_DIR / "benchmark"
LOCK_DIR = MODULE_DIR / "locks"
METADATA_DIR = MODULE_DIR / "metadata"
MODEL_PATCH_DIR = MODULE_DIR / "model_patch" / "default"
PROTECTED_TEST_PATCH_DIR = MODULE_DIR / "test_patch" / "protected"
RESULTS_DIR = MODULE_DIR / "results"

BENCHMARK_PATH = BENCHMARK_DIR / "leaderboard_502.jsonl"
LOCK_PATH = LOCK_DIR / "test_patch_lock.json"
ROWS_METADATA_PATH = METADATA_DIR / "rows.jsonl"
PASSWORD_SHA256 = "8ca42f8e22ffaa39e86a49d10ddeb8c9af8cfee7afae78010b39def36ca37d32"
PATCH_TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "cp936")
UNICODE_REPLACEMENT_CHAR = "\ufffd"
FULL_REGRESSION_NEW_TEST_ONLY_ROWS: set[int] = set()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _decode_patch_bytes(data: bytes, source: str) -> tuple[str, str]:
    errors: list[str] = []
    for encoding in PATCH_TEXT_ENCODINGS:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError as exc:
            errors.append(f"{encoding}: {exc}")
            continue
        if UNICODE_REPLACEMENT_CHAR in text:
            raise ValueError(
                f"{source} contains Unicode replacement characters; re-export or upload the original patch bytes"
            )
        return text, encoding
    raise ValueError(f"{source} is not decodable as UTF-8 or GBK/CP936: {'; '.join(errors)}")


def _read_patch_text(path: Path) -> tuple[str, str]:
    text, encoding = _decode_patch_bytes(path.read_bytes(), str(path))
    return _normalize_patch_text(text), encoding


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _read_json(path: Path) -> Any:
    return json.loads(_read_text(path))


def _write_json(path: Path, data: Any) -> None:
    _write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _normalize_patch_text(text: str) -> str:
    if not text:
        return ""
    text = text.lstrip("\ufeff")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text if text.endswith("\n") else text + "\n"


def _row_name(row: int) -> str:
    return f"row{row:02d}"


def _model_patch_path(row: int) -> Path:
    return MODEL_PATCH_DIR / f"model_patch_{row}.patch"


def _model_meta_path(row: int) -> Path:
    return MODEL_PATCH_DIR / f"model_patch_{row}.meta.json"


def _test_patch_path(row: int) -> Path:
    return PROTECTED_TEST_PATCH_DIR / f"test_patch_{row}.patch"


def _workspace_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ARKEVAL_ROOT / path


def _portable_path(path: Path) -> str:
    return path.resolve().relative_to(ARKEVAL_ROOT.resolve()).as_posix()


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in _read_text(path).splitlines():
        line = raw.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    _write_text(path, payload)


def _load_dataset(path: Path) -> list[dict[str, Any]]:
    rows = _load_jsonl(path)
    if len(rows) != TOTAL_ROWS:
        raise SystemExit(f"expected {TOTAL_ROWS} dataset rows, got {len(rows)} from {path}")
    return rows


def _require_password(password: str | None) -> None:
    if password is None:
        password = getpass.getpass("Leaderboard test patch password: ")
    if _sha256_text(password) != PASSWORD_SHA256:
        raise SystemExit("invalid password; test patch assets were not modified")


def init_assets(args: argparse.Namespace) -> int:
    _require_password(args.password)
    dataset_path = Path(args.dataset).resolve()
    dataset_rows = _load_dataset(dataset_path)

    benchmark_rows: list[dict[str, Any]] = []
    metadata_rows: list[dict[str, Any]] = []
    lock_rows: list[dict[str, Any]] = []

    for row, source_entry in enumerate(dataset_rows, start=1):
        entry = dict(source_entry)
        instance_id = str(entry.get("instance_id") or "")
        if not instance_id:
            raise SystemExit(f"row {row} has no instance_id")

        test_patch = _normalize_patch_text(str(entry.get("test_patch") or ""))
        if not test_patch:
            raise SystemExit(f"row {row} has no test_patch")
        test_patch_path = _test_patch_path(row)
        _write_text(test_patch_path, test_patch)
        test_sha = _sha256_text(test_patch)
        portable_test_path = _portable_path(test_patch_path)
        portable_model_path = _portable_path(_model_patch_path(row))
        entry_source = _portable_path(dataset_path)

        leaderboard_entry = dict(entry)
        leaderboard_entry["test_patch"] = test_patch
        leaderboard_entry["_leaderboard_row"] = row
        leaderboard_entry["_leaderboard_test_patch_sha256"] = test_sha
        leaderboard_entry["_leaderboard_test_patch_file"] = portable_test_path
        leaderboard_entry["_leaderboard_entry_source"] = entry_source
        benchmark_rows.append(leaderboard_entry)

        repo = entry.get("repo") or ""
        if not repo and "__" in instance_id:
            repo = instance_id.split("+", 1)[0].split("__", 1)[-1]
        metadata_rows.append(
            {
                "row": row,
                "instance_id": instance_id,
                "repo": repo,
                "title": entry.get("title", ""),
                "base_sha": (entry.get("base") or {}).get("sha", ""),
                "entry_source": entry_source,
                "test_patch_file": portable_test_path,
                "test_patch_sha256": test_sha,
                "test_patch_bytes": len(test_patch.encode("utf-8")),
                "model_patch_file": portable_model_path,
                "missing_prediction": not _model_patch_path(row).is_file(),
            }
        )
        lock_rows.append(
            {
                "row": row,
                "instance_id": instance_id,
                "test_patch_file": portable_test_path,
                "test_patch_sha256": test_sha,
                "test_patch_bytes": len(test_patch.encode("utf-8")),
                "entry_source": entry_source,
            }
        )

    _write_jsonl(BENCHMARK_PATH, benchmark_rows)
    _write_jsonl(ROWS_METADATA_PATH, metadata_rows)
    _write_json(
        LOCK_PATH,
        {
            "schema_version": 1,
            "created_at": _now(),
            "updated_at": _now(),
            "owner": "Leaderboards",
            "password_sha256": PASSWORD_SHA256,
            "dataset": _portable_path(dataset_path),
            "benchmark": _portable_path(BENCHMARK_PATH),
            "policy": "Evaluation must verify these hashes before scoring. Modify test patches only through update-test-patch with the password.",
            "rows": lock_rows,
        },
    )
    print(f"[init] wrote {TOTAL_ROWS} benchmark rows to {BENCHMARK_PATH}")
    print(f"[init] wrote locked test patches to {PROTECTED_TEST_PATCH_DIR}")
    return 0


def _load_rows_metadata() -> list[dict[str, Any]]:
    if not ROWS_METADATA_PATH.is_file():
        raise SystemExit(f"missing {ROWS_METADATA_PATH}; run init first")
    rows = _load_jsonl(ROWS_METADATA_PATH)
    if len(rows) != TOTAL_ROWS:
        raise SystemExit(f"expected {TOTAL_ROWS} rows in {ROWS_METADATA_PATH}, got {len(rows)}")
    return rows


def _new_test_only_instance_ids_for_full_regression(selected_rows: list[int], full_regression: bool) -> list[str]:
    if not full_regression:
        return []
    selected = set(selected_rows)
    ids: list[str] = []
    for row in _load_rows_metadata():
        row_number = int(row.get("row") or 0)
        if row_number in selected and row_number in FULL_REGRESSION_NEW_TEST_ONLY_ROWS:
            instance_id = str(row.get("instance_id") or "").strip()
            if instance_id:
                ids.append(instance_id)
    return ids


def verify_lock(args: argparse.Namespace | None = None, *, quiet: bool = False) -> bool:
    if not LOCK_PATH.is_file():
        raise SystemExit(f"missing {LOCK_PATH}; run init first")
    if not BENCHMARK_PATH.is_file():
        raise SystemExit(f"missing {BENCHMARK_PATH}; run init first")
    lock = _read_json(LOCK_PATH)
    benchmark_rows = _load_jsonl(BENCHMARK_PATH)
    if len(benchmark_rows) != TOTAL_ROWS or len(lock.get("rows", [])) != TOTAL_ROWS:
        if not quiet:
            print(
                f"[lock] expected {TOTAL_ROWS} rows, got benchmark={len(benchmark_rows)} "
                f"lock={len(lock.get('rows', []))}"
            )
        return False
    benchmark_by_row = {int(row.get("_leaderboard_row") or idx + 1): row for idx, row in enumerate(benchmark_rows)}

    errors: list[str] = []
    for row_lock in lock.get("rows", []):
        row = int(row_lock["row"])
        patch_path = _workspace_path(row_lock["test_patch_file"])
        if not patch_path.is_file():
            errors.append(f"row{row:02d}: missing {patch_path}")
            continue
        patch_text, _encoding = _read_patch_text(patch_path)
        actual_sha = _sha256_text(patch_text)
        expected_sha = row_lock.get("test_patch_sha256")
        if actual_sha != expected_sha:
            errors.append(f"row{row:02d}: protected test patch hash mismatch")
        benchmark_entry = benchmark_by_row.get(row)
        if benchmark_entry is None:
            errors.append(f"row{row:02d}: missing benchmark row")
            continue
        bench_sha = _sha256_text(_normalize_patch_text(str(benchmark_entry.get("test_patch") or "")))
        if bench_sha != expected_sha:
            errors.append(f"row{row:02d}: benchmark test_patch hash mismatch")

    if errors:
        for error in errors:
            print(f"[lock] {error}", file=sys.stderr)
        return False
    if not quiet:
        print("[lock] OK: protected test patches and benchmark hashes match")
    return True


def update_test_patch(args: argparse.Namespace) -> int:
    _require_password(args.password)
    row = int(args.row)
    if row < 1 or row > TOTAL_ROWS:
        raise SystemExit(f"--row must be in 1..{TOTAL_ROWS}")
    patch_text, _encoding = _read_patch_text(Path(args.patch))
    patch_path = _test_patch_path(row)
    _write_text(patch_path, patch_text)
    patch_sha = _sha256_text(patch_text)

    benchmark_rows = _load_jsonl(BENCHMARK_PATH)
    if len(benchmark_rows) != TOTAL_ROWS:
        raise SystemExit(f"expected {TOTAL_ROWS} benchmark rows in {BENCHMARK_PATH}")
    benchmark_rows[row - 1]["test_patch"] = patch_text
    benchmark_rows[row - 1]["_leaderboard_test_patch_sha256"] = patch_sha
    portable_patch_path = _portable_path(patch_path)
    benchmark_rows[row - 1]["_leaderboard_test_patch_file"] = portable_patch_path
    _write_jsonl(BENCHMARK_PATH, benchmark_rows)

    lock = _read_json(LOCK_PATH)
    for lock_row in lock["rows"]:
        if int(lock_row["row"]) == row:
            lock_row["test_patch_file"] = portable_patch_path
            lock_row["test_patch_sha256"] = patch_sha
            lock_row["test_patch_bytes"] = len(patch_text.encode("utf-8"))
            lock_row["updated_at"] = _now()
            break
    lock["updated_at"] = _now()
    _write_json(LOCK_PATH, lock)

    metadata_rows = _load_rows_metadata()
    metadata_rows[row - 1]["test_patch_file"] = portable_patch_path
    metadata_rows[row - 1]["test_patch_sha256"] = patch_sha
    metadata_rows[row - 1]["test_patch_bytes"] = len(patch_text.encode("utf-8"))
    _write_jsonl(ROWS_METADATA_PATH, metadata_rows)

    print(f"[update-test-patch] row{row:02d} updated and lock refreshed")
    return 0


def _parse_rows(rows_arg: str) -> list[int]:
    if not rows_arg:
        return list(range(1, TOTAL_ROWS + 1))
    rows: set[int] = set()
    for chunk in rows_arg.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            left, right = chunk.split("-", 1)
            rows.update(range(int(left), int(right) + 1))
        else:
            rows.add(int(chunk))
    bad = sorted(row for row in rows if row < 1 or row > TOTAL_ROWS)
    if bad:
        raise SystemExit(f"rows out of range: {bad}")
    return sorted(rows)


def _copy_run_inputs(run_dir: Path, selected_rows: list[int]) -> tuple[Path, Path]:
    benchmark_rows = _load_jsonl(BENCHMARK_PATH)
    input_dir = run_dir / "input"
    patch_dir = input_dir / "model_patches"
    benchmark_path = input_dir / "benchmark.jsonl"
    patch_dir.mkdir(parents=True, exist_ok=True)

    selected_set = set(selected_rows)
    _write_jsonl(benchmark_path, [row for idx, row in enumerate(benchmark_rows, start=1) if idx in selected_set])
    for row in selected_rows:
        shutil.copyfile(_model_patch_path(row), patch_dir / f"model_patch_{row}.patch")
        shutil.copyfile(_model_meta_path(row), patch_dir / f"model_patch_{row}.meta.json")
    return benchmark_path, patch_dir


def _split_arg_list(value: str | None) -> list[str]:
    if not value:
        return []
    normalized = value.replace(";", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def _partition_rows(rows: list[int], slots: int) -> list[list[int]]:
    active_slots = max(1, min(slots, len(rows)))
    chunks = [[] for _ in range(active_slots)]
    for index, row in enumerate(rows):
        chunks[index % active_slots].append(row)
    return [chunk for chunk in chunks if chunk]


def _auto_repo_roots(repo_root: Path, slots: int) -> list[Path]:
    repo_root = repo_root.resolve()
    explicit = [repo_root]
    name = repo_root.name.lower()
    if not name.startswith("run") or not name[3:].isdigit():
        return explicit
    parent = repo_root.parent
    required_repos = {str(row.get("repo") or "").strip() for row in _load_rows_metadata()}
    required_repos.discard("")
    roots: list[Path] = []
    for candidate in sorted(parent.glob("run*")):
        if candidate.is_dir() and all((candidate / repo).is_dir() for repo in required_repos):
            roots.append(candidate.resolve())
        if len(roots) >= slots:
            break
    return roots or explicit


def _planned_repo_roots(repo_root: str | Path, repo_roots: str | None, slots: int) -> list[Path]:
    explicit_roots = [Path(item).resolve() for item in _split_arg_list(repo_roots)]
    roots = explicit_roots or _auto_repo_roots(Path(repo_root), slots)
    local_pool = (ARKEVAL_ROOT / "depend" / "repair_repo").resolve()
    for root in roots:
        try:
            root.relative_to(local_pool)
        except ValueError as exc:
            raise ValueError(f"repo root must stay inside {local_pool}: {root}") from exc
    return roots


def _discover_hdc_targets(deveco_path: str | Path | None = None) -> list[str]:
    if deveco_path:
        _hdc, targets, _output = _list_hdc_targets(deveco_path)
        return targets

    hdc = shutil.which("hdc") or shutil.which("hdc.exe")
    if hdc:
        try:
            result = subprocess.run(
                [hdc, "list", "targets"],
                cwd=str(ARKEVAL_ROOT),
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=20,
            )
            targets: list[str] = []
            seen: set[str] = set()
            for raw in result.stdout.splitlines():
                line = raw.strip()
                if not line or line.lower().startswith("list of devices"):
                    continue
                target = line.split()[0]
                if target.lower() in {"no target", "no targets", "[empty]"} or target in seen:
                    continue
                seen.add(target)
                targets.append(target)
            if targets:
                return targets
        except Exception:
            pass

    probe = (
        "import json, sys; "
        f"sys.path.insert(0, {str(EVAL_TOOLS_DIR)!r}); "
        "from ensure_emulator import get_hdc_targets; "
        "print(json.dumps(get_hdc_targets(), ensure_ascii=False))"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(ARKEVAL_ROOT),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        parsed = json.loads(result.stdout.strip() or "[]")
        return [str(item) for item in parsed if str(item).strip()]
    except Exception:
        return []


def _resolve_parallel_workers(
    *,
    parallel_slots: int,
    repo_root: str | Path,
    repo_roots: str | None = "",
    hdc_targets: str | None = "",
    deveco_path: str | Path | None = None,
) -> list[dict[str, str]]:
    slots = max(1, int(parallel_slots or 1))
    roots = _planned_repo_roots(repo_root, repo_roots, slots)
    if len(roots) < slots:
        raise SystemExit(
            f"parallel slots={slots} needs {slots} repo roots; found {len(roots)}. "
            "Pass --repo-roots or use depend/repair_repo/run01..runNN."
        )
    missing_roots = [str(path) for path in roots[:slots] if not path.is_dir()]
    if missing_roots:
        raise SystemExit(f"parallel repo roots do not exist: {missing_roots}")

    targets = _split_arg_list(hdc_targets)
    if not targets:
        targets = _discover_hdc_targets(deveco_path)
    if slots > 1 and len(targets) < slots:
        raise SystemExit(
            f"parallel slots={slots} needs {slots} HDC targets; found {len(targets)}. "
            "Start more emulators or pass --hdc-targets target1,target2,..."
        )

    workers: list[dict[str, str]] = []
    for index in range(slots):
        workers.append(
            {
                "worker": f"worker{index + 1:02d}",
                "repo_root": str(roots[index]),
                "hdc_target": targets[index] if index < len(targets) else "",
            }
        )
    return workers


def _copy_subset_inputs(source_benchmark: Path, source_patch_dir: Path, input_dir: Path, rows: list[int]) -> tuple[Path, Path]:
    row_set = set(rows)
    benchmark_rows = []
    for index, entry in enumerate(_load_jsonl(source_benchmark), start=1):
        row = int(entry.get("_leaderboard_row") or index)
        if row in row_set:
            benchmark_rows.append(entry)

    patch_dir = input_dir / "model_patches"
    benchmark_path = input_dir / "benchmark.jsonl"
    patch_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(benchmark_path, benchmark_rows)
    for row in rows:
        for suffix in (".patch", ".meta.json"):
            source = source_patch_dir / f"model_patch_{row}{suffix}"
            if source.is_file():
                shutil.copyfile(source, patch_dir / source.name)
    return benchmark_path, patch_dir


def _build_eval_command(
    *,
    benchmark_path: Path,
    patch_dir: Path,
    output_path: Path,
    repo_root: str | Path,
    deveco_path: str,
    build_timeout: float,
    test_timeout: float,
    skip_existing: bool,
    full_regression: bool,
    new_test_only_instance_ids: list[str] | None = None,
) -> list[str]:
    command = [
        sys.executable,
        str(EVAL_SCRIPT),
        "--benchmark",
        str(benchmark_path),
        "--patches-dir",
        str(patch_dir),
        "--output",
        str(output_path),
        "--repo-root",
        str(Path(repo_root)),
        "--build-timeout",
        str(build_timeout),
        "--test-timeout",
        str(test_timeout),
    ]
    if deveco_path:
        command.extend(["--deveco-path", deveco_path])
    if skip_existing:
        command.append("--skip-existing")
    if not full_regression:
        command.append("--new-test-only")
    for instance_id in new_test_only_instance_ids or []:
        command.extend(["--new-test-only-instance-id", instance_id])
    return command


def _hdc_toolchain_dir(deveco_path: str | Path | None) -> Path | None:
    if not deveco_path:
        return None
    sdk_dir = Path(deveco_path) / "sdk"
    candidates = [sdk_dir / "default" / "openharmony" / "toolchains"]
    if sdk_dir.is_dir():
        candidates.extend(path.parent for path in sdk_dir.glob("**/hdc.exe"))
    for candidate in candidates:
        if (candidate / "hdc.exe").is_file():
            return candidate
    return None


def _worker_env(hdc_target: str | None, deveco_path: str | Path | None = None) -> dict[str, str] | None:
    toolchain_dir = _hdc_toolchain_dir(deveco_path)
    if not hdc_target and toolchain_dir is None:
        return None
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONSAFEPATH"] = "1"
    if hdc_target:
        env["HDC_TARGET"] = hdc_target
    if toolchain_dir is not None:
        path_key = "Path" if "Path" in env else "PATH"
        env[path_key] = str(toolchain_dir) + os.pathsep + env.get(path_key, "")
    return env


def _hdc_executable(deveco_path: str | Path | None) -> Path | None:
    toolchain_dir = _hdc_toolchain_dir(deveco_path)
    if toolchain_dir is not None:
        return toolchain_dir / "hdc.exe"
    if deveco_path:
        return None
    resolved = shutil.which("hdc") or shutil.which("hdc.exe")
    return Path(resolved) if resolved else None


def _parse_hdc_targets_output(output: str) -> list[str]:
    targets: list[str] = []
    seen: set[str] = set()
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("list of devices"):
            continue
        target = line.split()[0]
        if target.lower() in {"no", "no target", "no targets", "[empty]", "empty"} or target in seen:
            continue
        seen.add(target)
        targets.append(target)
    return targets


def _list_hdc_targets(deveco_path: str | Path | None) -> tuple[Path | None, list[str], str]:
    hdc = _hdc_executable(deveco_path)
    if hdc is None:
        return None, [], ""
    try:
        result = subprocess.run(
            [str(hdc), "list", "targets"],
            cwd=str(ARKEVAL_ROOT),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=_worker_env("", deveco_path),
            timeout=20,
        )
        return hdc, _parse_hdc_targets_output(result.stdout), result.stdout.strip()
    except Exception as exc:
        return hdc, [], str(exc)


def preflight_environment(
    *,
    repo_root: str | Path,
    repo_roots: str | None,
    hdc_targets: str | None,
    parallel_slots: int,
    deveco_path: str | Path | None,
    full_regression: bool = True,
) -> dict[str, Any]:
    slots = max(1, int(parallel_slots or 1))
    errors: list[str] = []
    checks: list[str] = []

    deveco = Path(deveco_path) if deveco_path else None
    if not deveco:
        errors.append("DevEco path is empty; set DevEco to the installed DevEco Studio directory")
    elif not deveco.is_dir():
        errors.append(f"DevEco path does not exist: {deveco}")
    else:
        resolved_deveco = deveco.resolve()
        local_harmony_env = (ARKEVAL_ROOT / "depend" / "harmony_env").resolve()
        try:
            resolved_deveco.relative_to(local_harmony_env)
        except ValueError:
            errors.append(f"DevEco path must stay inside {local_harmony_env}: {resolved_deveco}")
        else:
            checks.append(f"DevEco path OK: {resolved_deveco}")

    roots = _planned_repo_roots(repo_root, repo_roots, slots)
    if len(roots) < slots:
        errors.append(
            f"parallel slots={slots} needs {slots} repo roots; found {len(roots)} "
            "(set Repo roots explicitly or prepare depend/repair_repo/run01..runNN)"
        )
    for root in roots[:slots]:
        if not root.is_dir():
            errors.append(f"repo root does not exist: {root}")
            continue
        checks.append(f"repo root OK: {root}")
        lock_candidates = [
            root / ".git" / "index.lock",
            root / "ImageKnife" / ".git" / "index.lock",
            root / "applications_app_samples" / ".git" / "index.lock",
        ]
        existing_locks = [path for path in lock_candidates if path.is_file()]
        if existing_locks:
            errors.append("git index.lock exists: " + ", ".join(str(path) for path in existing_locks))

    if not shutil.which("git"):
        errors.append("git executable is not available in PATH")
    else:
        checks.append("git executable OK")

    hdc_exe, available_targets, hdc_output = _list_hdc_targets(deveco)
    if hdc_exe is None:
        errors.append(
            "hdc executable was not found; install DevEco/OpenHarmony SDK or set DevEco to a directory "
            "containing sdk\\default\\openharmony\\toolchains\\hdc.exe"
        )
    else:
        checks.append(f"hdc executable OK: {hdc_exe}")
        if not available_targets:
            detail = f" output: {hdc_output}" if hdc_output else ""
            errors.append(f"no online HDC target found via `{hdc_exe} list targets`.{detail}")
        else:
            checks.append("HDC targets OK: " + ", ".join(available_targets))

    requested_targets = _split_arg_list(hdc_targets)
    if requested_targets:
        if len(requested_targets) < slots:
            errors.append(f"parallel slots={slots} needs {slots} HDC targets; configured {len(requested_targets)}")
        if available_targets:
            missing_targets = [target for target in requested_targets[:slots] if target not in available_targets]
            if missing_targets:
                errors.append(
                    "configured HDC target is not online: "
                    + ", ".join(missing_targets)
                    + f"; online targets: {', '.join(available_targets)}"
                )
    elif len(available_targets) < slots:
        errors.append(f"parallel slots={slots} needs {slots} online HDC targets; found {len(available_targets)}")

    if errors:
        raise ValueError("environment preflight failed:\n- " + "\n- ".join(errors))
    return {
        "ok": True,
        "checked_at": _now(),
        "full_regression": bool(full_regression),
        "parallel_slots": slots,
        "repo_roots": [str(path) for path in roots[:slots]],
        "hdc_executable": str(hdc_exe) if hdc_exe else "",
        "available_hdc_targets": available_targets,
        "configured_hdc_targets": requested_targets,
        "checks": checks,
    }


def _combine_worker_outputs(output_paths: list[Path], output_path: Path | None = None) -> dict[str, Any]:
    all_results: list[dict[str, Any]] = []
    for path in output_paths:
        if not path.is_file():
            continue
        try:
            data = _read_json(path)
        except Exception:
            continue
        for result in data.get("results", []):
            if isinstance(result, dict):
                all_results.append(result)

    row_order = {str(row["instance_id"]): int(row["row"]) for row in _load_rows_metadata()}
    all_results.sort(key=lambda item: row_order.get(str(item.get("instance_id")), 9999))
    resolved = sum(1 for item in all_results if item.get("status") == "resolved")
    error_statuses = {
        "fix_patch_apply_error",
        "model_patch_encoding_error",
        "reset_failed",
        "repo_not_found",
        "not_in_benchmark",
    }
    error = sum(1 for item in all_results if item.get("status") in error_statuses)
    unresolved = len(all_results) - resolved - error
    summary = {
        "total": len(all_results),
        "resolved": resolved,
        "unresolved": max(0, unresolved),
        "error": error,
        "skipped": 0,
        "generated_at": _now(),
        "parallel_workers": len(output_paths),
    }
    combined = {"summary": summary, "results": all_results}
    if output_path is not None:
        _write_json(output_path, combined)
    return combined


def _run_evaluator_parallel(
    *,
    run_dir: Path,
    benchmark_path: Path,
    patch_dir: Path,
    selected_rows: list[int],
    repo_root: str | Path,
    repo_roots: str,
    hdc_targets: str,
    parallel_slots: int,
    deveco_path: str,
    build_timeout: float,
    test_timeout: float,
    skip_existing: bool,
    full_regression: bool,
    new_test_only_instance_ids: list[str] | None = None,
) -> int:
    chunks = _partition_rows(selected_rows, parallel_slots)
    workers = _resolve_parallel_workers(
        parallel_slots=len(chunks),
        repo_root=repo_root,
        repo_roots=repo_roots,
        hdc_targets=hdc_targets,
        deveco_path=deveco_path,
    )

    processes: list[tuple[subprocess.Popen[str], Any, Path, Path, dict[str, str], list[int]]] = []
    worker_manifest: list[dict[str, Any]] = []
    for index, rows in enumerate(chunks):
        worker = workers[index]
        worker_dir = run_dir / worker["worker"]
        worker_benchmark, worker_patch_dir = _copy_subset_inputs(
            benchmark_path,
            patch_dir,
            worker_dir / "input",
            rows,
        )
        output_path = worker_dir / "raw_results.json"
        log_path = worker_dir / "worker.log"
        worker_instance_ids = {str(row.get("instance_id")) for row in _load_jsonl(worker_benchmark)}
        worker_new_test_only_instance_ids = [
            instance_id
            for instance_id in (new_test_only_instance_ids or [])
            if instance_id in worker_instance_ids
        ]
        command = _build_eval_command(
            benchmark_path=worker_benchmark,
            patch_dir=worker_patch_dir,
            output_path=output_path,
            repo_root=worker["repo_root"],
            deveco_path=deveco_path,
            build_timeout=build_timeout,
            test_timeout=test_timeout,
            skip_existing=skip_existing,
            full_regression=full_regression,
            new_test_only_instance_ids=worker_new_test_only_instance_ids,
        )
        log = log_path.open("w", encoding="utf-8", newline="\n")
        print(
            f"[parallel] {worker['worker']} rows={','.join(str(row) for row in rows)} "
            f"repo={worker['repo_root']} hdc={worker['hdc_target'] or '<default>'}"
        )
        process = subprocess.Popen(
            command,
            cwd=str(ARKEVAL_ROOT),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=log,
            stderr=subprocess.STDOUT,
            env=_worker_env(worker["hdc_target"], deveco_path),
        )
        processes.append((process, log, output_path, log_path, worker, rows))
        worker_manifest.append(
            {
                **worker,
                "rows": rows,
                "pid": process.pid,
                "output": str(output_path),
                "log": str(log_path),
                "command": command,
            }
        )

    _write_json(run_dir / "workers.json", {"workers": worker_manifest})
    exit_codes: list[int] = []
    for process, log, _output_path, _log_path, worker, _rows in processes:
        exit_code = process.wait()
        log.close()
        exit_codes.append(exit_code)
        print(f"[parallel] {worker['worker']} exit={exit_code}")

    worker_outputs = [item[2] for item in processes]
    _combine_worker_outputs(worker_outputs, run_dir / "raw_results.json")
    with (run_dir / "run.log").open("w", encoding="utf-8", newline="\n") as combined_log:
        for _process, _log, _output_path, log_path, worker, rows in processes:
            combined_log.write(
                f"\n===== {worker['worker']} rows={','.join(str(row) for row in rows)} "
                f"repo={worker['repo_root']} hdc={worker['hdc_target'] or '<default>'} =====\n"
            )
            if log_path.is_file():
                combined_log.write(_read_text(log_path))
                combined_log.write("\n")
    return 0 if all(code == 0 for code in exit_codes) else 1


def run_eval(args: argparse.Namespace) -> int:
    if not verify_lock(quiet=True):
        raise SystemExit("test patch lock verification failed; refusing to score")

    selected_rows = _parse_rows(args.rows)
    effective_slots = max(1, min(int(args.parallel_slots or 1), len(selected_rows)))
    try:
        preflight = preflight_environment(
            repo_root=args.repo_root,
            repo_roots=args.repo_roots,
            hdc_targets=args.hdc_targets,
            parallel_slots=effective_slots,
            deveco_path=args.deveco_path,
            full_regression=args.full_regression,
        )
        print("[preflight] OK: " + "; ".join(preflight.get("checks", [])))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = RESULTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    benchmark_path, patch_dir = _copy_run_inputs(run_dir, selected_rows)
    output_path = run_dir / "raw_results.json"
    new_test_only_instance_ids = _new_test_only_instance_ids_for_full_regression(
        selected_rows,
        args.full_regression,
    )
    if new_test_only_instance_ids:
        print(
            "[eval-policy] full regression with new-test-only override for: "
            + ", ".join(new_test_only_instance_ids)
        )

    if int(args.parallel_slots or 1) > 1:
        returncode = _run_evaluator_parallel(
            run_dir=run_dir,
            benchmark_path=benchmark_path,
            patch_dir=patch_dir,
            selected_rows=selected_rows,
            repo_root=args.repo_root,
            repo_roots=args.repo_roots,
            hdc_targets=args.hdc_targets,
            parallel_slots=int(args.parallel_slots),
            deveco_path=args.deveco_path,
            build_timeout=args.build_timeout,
            test_timeout=args.test_timeout,
            skip_existing=args.skip_existing,
            full_regression=args.full_regression,
            new_test_only_instance_ids=new_test_only_instance_ids,
        )
    else:
        log_path = run_dir / "run.log"
        hdc_target = _split_arg_list(args.hdc_targets)[0] if _split_arg_list(args.hdc_targets) else ""
        command = _build_eval_command(
            benchmark_path=benchmark_path,
            patch_dir=patch_dir,
            output_path=output_path,
            repo_root=args.repo_root,
            deveco_path=args.deveco_path,
            build_timeout=args.build_timeout,
            test_timeout=args.test_timeout,
            skip_existing=args.skip_existing,
            full_regression=args.full_regression,
            new_test_only_instance_ids=new_test_only_instance_ids,
        )
        print("[run] " + " ".join(f'"{part}"' if " " in part else part for part in command))
        with log_path.open("w", encoding="utf-8", newline="\n") as log:
            process = subprocess.run(
                command,
                cwd=str(ARKEVAL_ROOT),
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=log,
                stderr=subprocess.STDOUT,
                env=_worker_env(hdc_target, args.deveco_path),
            )
        returncode = process.returncode
        print(f"[run] evaluator exit={returncode}, log={log_path}")
    if output_path.is_file():
        summarize_result_file(output_path, run_dir / "scorecard")
    return returncode


def _load_eval_result(result_path: Path) -> dict[str, Any] | None:
    try:
        data = _read_json(result_path)
    except Exception:
        return None
    results = data.get("results")
    if isinstance(results, list) and results:
        return results[0]
    return None


def _score_eval_result(result: dict[str, Any] | None, *, missing_prediction: bool) -> tuple[bool, str, str]:
    if missing_prediction:
        return False, "missing_prediction", "model patch file is empty"
    if result is None:
        return False, "not_run", "no evaluation result"
    if result.get("source_contract"):
        return False, "source_contract_rejected", "source-contract fallback is not a runtime pass"
    if result.get("resolved") is True and result.get("status") == "resolved":
        return True, "PASS", "resolved"
    status = str(result.get("status") or "unknown")
    error = str(result.get("error") or "")
    reason = error[:240] if error else status
    return False, status, reason


def _row_score_record(
    row_meta: dict[str, Any],
    result: dict[str, Any] | None,
    result_path: Path | None,
) -> dict[str, Any]:
    passed, verdict, reason = _score_eval_result(
        result,
        missing_prediction=bool(row_meta.get("missing_prediction")),
    )
    return {
        "row": row_meta["row"],
        "instance_id": row_meta["instance_id"],
        "repo": row_meta.get("repo", ""),
        "title": row_meta.get("title", ""),
        "passed": passed,
        "verdict": verdict,
        "reason": reason,
        "status": result.get("status") if result else None,
        "resolved": result.get("resolved") if result else False,
        "fix_patch_applied": result.get("fix_patch_applied") if result else None,
        "test_patch_applied": result.get("test_patch_applied") if result else None,
        "build_exit_code": result.get("build_exit_code") if result else None,
        "install_exit_code": result.get("install_exit_code") if result else None,
        "local_test_exit_code": result.get("local_test_exit_code") if result else None,
        "instrument_test_exit_code": result.get("instrument_test_exit_code") if result else None,
        "source_contract_rejected": bool(result and result.get("source_contract")),
        "model_source_version": row_meta.get("model_source_version", ""),
        "model_source_encoding": row_meta.get("model_source_encoding", ""),
        "model_patch_sha256": row_meta.get("model_patch_sha256", ""),
        "model_patch_bytes": row_meta.get("model_patch_bytes", 0),
        "stored_set": row_meta.get("stored_set", ""),
        "effective_contract": row_meta.get("effective_contract", ""),
        "effective_contract_source": row_meta.get("effective_contract_source", ""),
        "result_path": str(result_path) if result_path else "",
    }


def _write_scorecard(records: list[dict[str, Any]], output_prefix: Path, source: str) -> None:
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    total = len(records)
    passed = sum(1 for row in records if row["passed"])
    missing = sum(1 for row in records if row["verdict"] == "missing_prediction")
    not_run = sum(1 for row in records if row["verdict"] in {"not_run", "patch_mismatch"})
    source_contract = sum(1 for row in records if row.get("source_contract_rejected"))
    by_effective_contract: dict[str, dict[str, Any]] = {}
    for row in records:
        name = str(row.get("effective_contract") or row.get("stored_set") or "unknown")
        group = by_effective_contract.setdefault(
            name,
            {
                "total": 0,
                "passed": 0,
                "failed": 0,
                "pass_rate": 0.0,
                "missing_prediction": 0,
                "not_run_or_unusable_artifact": 0,
                "source_contract_rejected": 0,
            },
        )
        group["total"] += 1
        group["passed"] += 1 if row["passed"] else 0
        group["missing_prediction"] += 1 if row["verdict"] == "missing_prediction" else 0
        group["not_run_or_unusable_artifact"] += 1 if row["verdict"] in {"not_run", "patch_mismatch"} else 0
        group["source_contract_rejected"] += 1 if row.get("source_contract_rejected") else 0
    for group in by_effective_contract.values():
        group["failed"] = group["total"] - group["passed"]
        group["pass_rate"] = group["passed"] / group["total"] if group["total"] else 0.0
    summary = {
        "generated_at": _now(),
        "source": source,
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": passed / total if total else 0.0,
        "missing_prediction": missing,
        "not_run_or_unusable_artifact": not_run,
        "source_contract_rejected": source_contract,
        "by_effective_contract": by_effective_contract,
    }
    _write_json(output_prefix.with_suffix(".json"), {"summary": summary, "rows": records})

    lines = [
        "# Leaderboards scorecard",
        "",
        f"- source: `{source}`",
        f"- generated_at: `{summary['generated_at']}`",
        f"- pass rate: `{passed} / {total}` = `{summary['pass_rate']:.2%}`",
        *[
            f"- effective {name} pass rate: `{group['passed']} / {group['total']}` = `{group['pass_rate']:.2%}`"
            for name, group in sorted(by_effective_contract.items())
        ],
        f"- missing prediction: `{missing}`",
        f"- not run or unusable artifact: `{not_run}`",
        f"- source-contract rejected: `{source_contract}`",
        "",
        "| Row | Effective | Stored | Verdict | Status | Model source | Result |",
        "|---:|---|---|---|---|---|---|",
    ]
    for row in records:
        result_label = row["result_path"] or row["reason"]
        lines.append(
            f"| {row['row']} | {row.get('effective_contract') or ''} | {row.get('stored_set') or ''} | "
            f"{row['verdict']} | {row.get('status') or ''} | "
            f"{row.get('model_source_version') or ''} | `{result_label}` |"
        )
    _write_text(output_prefix.with_suffix(".md"), "\n".join(lines) + "\n")
    print(f"[scorecard] {passed}/{total} = {summary['pass_rate']:.2%}")
    print(f"[scorecard] wrote {output_prefix.with_suffix('.json')}")


def summarize_result_file(result_path: Path, output_prefix: Path | None = None) -> int:
    rows_meta = _load_rows_metadata()
    result_data = _read_json(result_path)
    results_by_iid = {
        str(result.get("instance_id")): result
        for result in result_data.get("results", [])
        if isinstance(result, dict) and result.get("instance_id")
    }
    records = []
    for row_meta in rows_meta:
        result = results_by_iid.get(str(row_meta["instance_id"]))
        records.append(_row_score_record(row_meta, result, result_path if result else None))
    _write_scorecard(records, output_prefix or RESULTS_DIR / "scorecard", str(result_path))
    return 0


def summarize(args: argparse.Namespace) -> int:
    return summarize_result_file(Path(args.result), Path(args.output_prefix) if args.output_prefix else None)


def _latest_model_result(row_dir: Path) -> tuple[dict[str, Any] | None, Path | None]:
    raw_dir = row_dir / "results" / "raw"
    if not raw_dir.is_dir():
        return None, None
    candidates = sorted(raw_dir.glob(f"{row_dir.name}_model*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    parsed: list[tuple[dict[str, Any], Path]] = []
    for candidate in candidates:
        result = _load_eval_result(candidate)
        if result is not None:
            parsed.append((result, candidate))
    return parsed[0] if parsed else (None, None)


def _row_run_model_patch_sha(row: int, row_runs: Path) -> str | None:
    patch_dir = row_runs / _row_name(row) / "patches" / "model"
    if not patch_dir.is_dir():
        return None
    patches = sorted(patch_dir.glob("*.patch"))
    if not patches:
        return _sha256_text("")
    if len(patches) > 1:
        patches = sorted(patches, key=lambda path: path.stat().st_mtime, reverse=True)
    return _sha256_bytes(patches[0].read_bytes())


def score_from_row_runs(args: argparse.Namespace) -> int:
    if not verify_lock(quiet=True):
        raise SystemExit("test patch lock verification failed; refusing to score")
    rows_meta = _load_rows_metadata()
    row_runs = Path(args.row_runs)
    records: list[dict[str, Any]] = []
    for row_meta in rows_meta:
        row = int(row_meta["row"])
        expected_sha = str(row_meta.get("model_patch_sha256") or "")
        actual_sha = _row_run_model_patch_sha(row, row_runs)
        result: dict[str, Any] | None = None
        result_path: Path | None = None
        if actual_sha is None:
            row_record = _row_score_record(row_meta, None, None)
            if not row_meta.get("missing_prediction"):
                row_record["verdict"] = "not_run"
                row_record["reason"] = "no row_runs model patch/result"
            records.append(row_record)
            continue
        if actual_sha != expected_sha:
            row_record = _row_score_record(row_meta, None, None)
            row_record["verdict"] = "patch_mismatch"
            row_record["reason"] = "row_runs model patch hash does not match Leaderboards/model_patch/default"
            records.append(row_record)
            continue
        result, result_path = _latest_model_result(row_runs / _row_name(row))
        records.append(_row_score_record(row_meta, result, result_path))
    _write_scorecard(records, Path(args.output_prefix), f"row_runs:{row_runs}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ArkTS Leaderboards wrapper around evaluation/run_llm_patch_eval.py")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help=f"Create the {TOTAL_ROWS}-row leaderboard assets")
    init.add_argument("--password", default=None)
    init.add_argument("--dataset", default=str(DEFAULT_DATASET))
    init.set_defaults(func=init_assets)

    verify = sub.add_parser("verify-lock", help="Verify locked test patches before scoring")
    verify.set_defaults(func=lambda _args: 0 if verify_lock() else 1)

    update = sub.add_parser("update-test-patch", help="Replace one locked test patch; requires password")
    update.add_argument("--row", type=int, required=True)
    update.add_argument("--patch", required=True)
    update.add_argument("--password", default=None)
    update.set_defaults(func=update_test_patch)

    run = sub.add_parser("run", help="Run the leaderboard evaluator and summarize the score")
    run.add_argument("--rows", default="", help=f"Rows to run, e.g. 1,4,8-12. Default: all {TOTAL_ROWS}")
    run.add_argument("--run-id", default="")
    run.add_argument("--repo-root", default=str(DEFAULT_REPO_ROOT))
    run.add_argument("--deveco-path", default=str(DEFAULT_DEVECO_PATH))
    run.add_argument("--build-timeout", type=float, default=1800.0)
    run.add_argument("--test-timeout", type=float, default=1800.0)
    run.add_argument("--parallel-slots", type=int, default=1, help="Run rows across N repo/device workers")
    run.add_argument(
        "--repo-roots",
        default="",
        help="Comma/semicolon separated repo roots for parallel workers. Defaults to run01..runNN siblings.",
    )
    run.add_argument(
        "--hdc-targets",
        default="",
        help="Comma/semicolon separated HDC targets. In parallel mode, one target is assigned to each worker.",
    )
    run.add_argument("--skip-existing", action="store_true")
    run.add_argument(
        "--full-regression",
        dest="full_regression",
        action="store_true",
        default=True,
        help="Run the full existing regression suite plus the submitted test_patch additions.",
    )
    run.add_argument(
        "--new-test-only",
        dest="full_regression",
        action="store_false",
        help="Isolation/debug mode: pass --new-test-only to the evaluator.",
    )
    run.set_defaults(func=run_eval)

    summarize_cmd = sub.add_parser("summarize", help="Summarize an existing run_llm_patch_eval JSON result")
    summarize_cmd.add_argument("--result", required=True)
    summarize_cmd.add_argument("--output-prefix", default="")
    summarize_cmd.set_defaults(func=summarize)

    existing = sub.add_parser("score-from-row-runs", help="Score matching existing row_runs model artifacts")
    existing.add_argument("--row-runs", default=str(DEFAULT_ROW_RUNS))
    existing.add_argument("--output-prefix", default=str(RESULTS_DIR / "current_row_runs_scorecard"))
    existing.set_defaults(func=score_from_row_runs)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
