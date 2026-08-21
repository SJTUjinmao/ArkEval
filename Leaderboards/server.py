#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import leaderboards


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
WEB_RUNS_DIR = leaderboards.RESULTS_DIR / "web_runs"
MODEL_PATCH_ROOT = ROOT / "model_patch"
TOTAL_ROWS = leaderboards.TOTAL_ROWS
MAX_JSON_BODY = 128 * 1024 * 1024
RUN_PROCESSES: dict[str, list[subprocess.Popen[str]]] = {}
START_RUN_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def _jsonl_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _safe_run_id(value: str | None) -> str:
    if value:
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
        if cleaned:
            return cleaned[:80]
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _unique_model_patch_archive_dir() -> Path:
    MODEL_PATCH_ROOT.mkdir(parents=True, exist_ok=True)
    stem = f"model_patch_{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    candidate = MODEL_PATCH_ROOT / stem
    if not candidate.exists():
        return candidate
    for index in range(2, 1000):
        candidate = MODEL_PATCH_ROOT / f"{stem}_{index:02d}"
        if not candidate.exists():
            return candidate
    raise ValueError(f"cannot allocate model patch archive directory under {MODEL_PATCH_ROOT}")


def _status_from_result(result: dict | None) -> str:
    if not result:
        return "pending"
    if result.get("source_contract"):
        return "source_contract"
    if result.get("resolved") is True and result.get("status") == "resolved":
        return "pass"
    if result.get("status") in {"fix_patch_apply_error", "model_patch_encoding_error"}:
        return "apply_error"
    return "fail"


def _load_rows_meta() -> list[dict]:
    return _jsonl_rows(leaderboards.ROWS_METADATA_PATH)


def _load_run(run_id: str) -> dict:
    run_dir = WEB_RUNS_DIR / run_id
    manifest_path = run_dir / "manifest.json"
    manifest = _read_json(manifest_path) if manifest_path.is_file() else {"run_id": run_id}
    raw_path = run_dir / "raw_results.json"
    raw = _read_json(raw_path) if raw_path.is_file() else _combined_worker_raw(manifest)
    scorecard_path = run_dir / "scorecard.json"
    scorecard = _read_json(scorecard_path) if scorecard_path.is_file() else None
    state_path = run_dir / "state.json"
    state = _read_json(state_path) if state_path.is_file() else {}

    processes = RUN_PROCESSES.get(run_id)
    if processes is not None:
        polls = [process.poll() for process in processes]
        still_running = any(poll is None for poll in polls)
        if state.get("status") != "canceled":
            state["status"] = "running" if still_running else ("completed" if all(poll == 0 for poll in polls) else "failed")
            state["exit_code"] = None if still_running else max(int(poll or 0) for poll in polls)

    results = raw.get("results") if isinstance(raw, dict) else []
    results_by_iid = {
        str(item.get("instance_id")): item
        for item in results
        if isinstance(item, dict) and item.get("instance_id")
    }
    selected_rows = [int(row) for row in manifest.get("selected_rows", [])]
    if not selected_rows:
        selected_rows = list(range(1, TOTAL_ROWS + 1))
    selected_set = set(selected_rows)
    rows = []
    for meta in _load_rows_meta():
        if int(meta.get("row") or 0) not in selected_set:
            continue
        result = results_by_iid.get(str(meta.get("instance_id")))
        rows.append(
            {
                "row": meta.get("row"),
                "instance_id": meta.get("instance_id"),
                "repo": meta.get("repo"),
                "title": meta.get("title"),
                "stored_set": meta.get("stored_set"),
                "status": _status_from_result(result),
                "verdict": result.get("status") if result else "pending",
                "resolved": bool(result and result.get("resolved")),
                "build_exit_code": result.get("build_exit_code") if result else None,
                "install_exit_code": result.get("install_exit_code") if result else None,
                "local_test_exit_code": result.get("local_test_exit_code") if result else None,
                "instrument_test_exit_code": result.get("instrument_test_exit_code") if result else None,
            }
        )

    completed = sum(1 for row in rows if row["status"] != "pending")
    passed = sum(1 for row in rows if row["status"] == "pass")
    failed = sum(1 for row in rows if row["status"] in {"fail", "apply_error", "source_contract"})
    total = len(selected_rows)
    log_tail = "" if state.get("status") == "canceled" else _run_log_tail(run_dir, manifest)

    return {
        "run_id": run_id,
        "manifest": manifest,
        "state": state,
        "summary": {
            "total": total,
            "completed": completed,
            "passed": passed,
            "failed": failed,
            "progress": completed / total if total else 0,
            "pass_rate": passed / total if total else 0,
            "scorecard": scorecard.get("summary") if scorecard else None,
        },
        "rows": rows,
        "log_tail": log_tail,
    }


def _list_runs() -> list[dict]:
    if not WEB_RUNS_DIR.is_dir():
        return []
    runs = []
    for run_dir in sorted(WEB_RUNS_DIR.iterdir(), key=lambda path: path.stat().st_mtime, reverse=True):
        if run_dir.is_dir() and (run_dir / "manifest.json").is_file():
            state = _load_run(run_dir.name)
            runs.append(
                {
                    "run_id": run_dir.name,
                    "status": state["state"].get("status", "unknown"),
                    "created_at": state["manifest"].get("created_at", ""),
                    "progress": state["summary"]["progress"],
                    "passed": state["summary"]["passed"],
                    "completed": state["summary"]["completed"],
                }
            )
    return runs


def _monitor(
    run_id: str,
    processes: list[subprocess.Popen[str]],
    raw_results: Path,
    score_prefix: Path,
    log_handles,
) -> None:
    state_path = WEB_RUNS_DIR / run_id / "state.json"
    exit_codes = [process.wait() for process in processes]
    for handle in log_handles:
        handle.close()
    state = _read_json(state_path) if state_path.is_file() else {}
    manifest = _read_json(WEB_RUNS_DIR / run_id / "manifest.json")
    was_canceled = state.get("status") == "canceled"
    if not was_canceled:
        leaderboards._combine_worker_outputs(
            [Path(worker["output"]) for worker in manifest.get("workers", [])],
            raw_results,
        )
    state["status"] = "canceled" if was_canceled else ("completed" if all(code == 0 for code in exit_codes) else "failed")
    state["exit_code"] = max(exit_codes) if exit_codes else 1
    state["finished_at"] = state.get("canceled_at") or _now()
    if raw_results.is_file() and not was_canceled:
        try:
            leaderboards.summarize_result_file(raw_results, score_prefix)
            state["scorecard"] = str(score_prefix.with_suffix(".json"))
        except Exception as exc:
            state["scorecard_error"] = str(exc)
    _write_json(state_path, state)
    RUN_PROCESSES.pop(run_id, None)


def _combined_worker_raw(manifest: dict) -> dict:
    workers = manifest.get("workers") if isinstance(manifest, dict) else None
    if not isinstance(workers, list):
        return {"summary": {}, "results": []}
    return leaderboards._combine_worker_outputs([Path(worker["output"]) for worker in workers if worker.get("output")])


def _kill_process_tree(pid: int) -> None:
    if sys.platform.startswith("win"):
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    else:
        subprocess.run(["kill", "-TERM", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def _cleanup_canceled_run(run_dir: Path, manifest: dict) -> None:
    for worker in manifest.get("workers", []):
        log_path = Path(worker["log"]) if worker.get("log") else None
        output_path = Path(worker["output"]) if worker.get("output") else None
        if log_path and log_path.is_file():
            try:
                log_path.write_text("", encoding="utf-8", newline="\n")
            except OSError:
                pass
        if output_path and output_path.is_file():
            try:
                output_path.unlink()
            except OSError:
                pass
    for path in [
        run_dir / "raw_results.json",
        run_dir / "scorecard.json",
        run_dir / "scorecard.md",
        run_dir / "scorecard.csv",
    ]:
        if path.is_file():
            try:
                path.unlink()
            except OSError:
                pass


def _cancel_run(run_id: str) -> dict:
    run_id = _safe_run_id(run_id)
    run_dir = WEB_RUNS_DIR / run_id
    if not run_dir.is_dir():
        raise ValueError(f"run not found: {run_id}")
    state_path = run_dir / "state.json"
    manifest = _read_json(run_dir / "manifest.json")
    state = _read_json(state_path) if state_path.is_file() else {}
    killed_pids: list[int] = []

    processes = RUN_PROCESSES.get(run_id)
    if processes:
        for process in processes:
            if process.poll() is None:
                _kill_process_tree(process.pid)
                killed_pids.append(process.pid)
    else:
        for worker in manifest.get("workers", []):
            pid = worker.get("pid")
            if isinstance(pid, int):
                _kill_process_tree(pid)
                killed_pids.append(pid)

    state["status"] = "canceled"
    state["canceled_at"] = _now()
    state["exit_code"] = -1
    state["killed_pids"] = killed_pids
    state.pop("scorecard", None)
    state.pop("scorecard_error", None)
    _write_json(state_path, state)
    _cleanup_canceled_run(run_dir, manifest)
    return _load_run(run_id)


def _run_log_tail(run_dir: Path, manifest: dict) -> str:
    workers = manifest.get("workers") if isinstance(manifest, dict) else None
    if isinstance(workers, list) and workers:
        lines: list[str] = []
        for worker in workers:
            log_path = Path(worker.get("log", ""))
            if not log_path.is_file():
                continue
            text = log_path.read_text(encoding="utf-8", errors="replace")
            lines.append(f"===== {worker.get('worker')} rows={','.join(str(row) for row in worker.get('rows', []))} =====")
            lines.extend(text.splitlines()[-24:])
        return "\n".join(lines[-100:])

    log_path = run_dir / "run.log"
    if log_path.is_file():
        text = log_path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-80:])
    return ""


def _parse_patch_row_from_filename(filename: str) -> int | None:
    base_name = Path(filename).name
    match = re.search(r"_([^_.]+)\.(?:patch|diff|txt)$", base_name, re.I)
    if not match:
        return None
    row_match = re.search(r"(\d+)$", match.group(1))
    if not row_match:
        return None
    row = int(row_match.group(1))
    return row if 1 <= row <= TOTAL_ROWS else None


def _extract_row_patch(item: dict) -> tuple[int, str]:
    row = item.get("row")
    filename = str(item.get("filename") or "")
    if row is None:
        row = _parse_patch_row_from_filename(filename)
    try:
        row_int = int(row)
    except Exception as exc:
        raise ValueError(f"cannot determine row for {filename}") from exc
    if row_int < 1 or row_int > TOTAL_ROWS:
        raise ValueError(f"row out of range for {filename}: {row_int}")
    content = str(item.get("content") or "").lstrip("\ufeff")
    if "\x00" in content:
        raise ValueError(f"row{row_int:02d} patch contains NUL bytes")
    return row_int, leaderboards._normalize_patch_text(content)


def _archive_model_patch_submission(
    patches: dict[int, dict[str, str]],
    rows_meta: list[dict],
    run_id: str,
    created_at: str,
) -> Path:
    archive_dir = _unique_model_patch_archive_dir()
    archive_dir.mkdir(parents=True, exist_ok=False)
    manifest_rows: list[dict] = []
    for meta in rows_meta:
        row = int(meta["row"])
        if row not in patches:
            continue
        patch_item = patches[row]
        patch_text = patch_item["content"]
        patch_path = archive_dir / f"model_patch_{row}.patch"
        patch_path.write_text(patch_text, encoding="utf-8", newline="\n")
        manifest_rows.append(
            {
                "row": row,
                "instance_id": meta.get("instance_id", ""),
                "repo": meta.get("repo", ""),
                "title": meta.get("title", ""),
                "filename": patch_path.name,
                "source_filename": patch_item["filename"],
                "source_encoding": patch_item["source_encoding"],
                "model_patch_sha256": leaderboards._sha256_text(patch_text),
                "model_patch_bytes": len(patch_text.encode("utf-8")),
            }
        )
    patch_files = sorted(archive_dir.glob("model_patch_*.patch"))
    if len(patch_files) != len(patches):
        raise ValueError(
            f"expected to archive {len(patches)} model patches, wrote {len(patch_files)} to {archive_dir}"
        )
    _write_json(
        archive_dir / "manifest.json",
        {
            "run_id": run_id,
            "created_at": created_at,
            "archive_dir": str(archive_dir),
            "patch_count": len(patch_files),
            "rows": manifest_rows,
        },
    )
    return archive_dir


def _active_process_runs() -> list[str]:
    active: list[str] = []
    for run_id, processes in list(RUN_PROCESSES.items()):
        if any(process.poll() is None for process in processes):
            active.append(run_id)
    return active


def _split_config_list(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[;,]", value) if part.strip()]


def _external_repo_processes(repo_roots: list[str]) -> list[str]:
    normalized_roots = [str(Path(root)).replace("\\", "/").lower() for root in repo_roots if root]
    if not normalized_roots:
        return []
    markers = (
        "run_llm_patch_eval.py",
        "build_app.py",
        "install_app.py",
        "run_tests.py",
        "run_local_tests.py",
        "hvigorw",
    )
    ps_command = (
        "Get-CimInstance Win32_Process | "
        f"Where-Object {{ $_.CommandLine -and $_.ProcessId -ne {os.getpid()} }} | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_command],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return []
        process_rows = json.loads(completed.stdout)
    except Exception:
        return []
    if isinstance(process_rows, dict):
        process_rows = [process_rows]
    busy: list[str] = []
    for row in process_rows if isinstance(process_rows, list) else []:
        command = str(row.get("CommandLine") or "")
        normalized_command = command.replace("\\", "/").lower()
        if not any(root in normalized_command for root in normalized_roots):
            continue
        if not any(marker in normalized_command for marker in markers):
            continue
        busy.append(f"pid {row.get('ProcessId')}: {command[:220]}")
    return busy


def _start_run(payload: dict) -> dict:
    with START_RUN_LOCK:
        return _start_run_locked(payload)


def _start_run_locked(payload: dict) -> dict:
    active_runs = _active_process_runs()
    if active_runs:
        raise ValueError(f"another run is still active: {', '.join(active_runs)}")
    if not leaderboards.verify_lock(quiet=True):
        raise ValueError("test patch lock verification failed")
    patch_items = payload.get("patches")
    if not isinstance(patch_items, list):
        raise ValueError("patches must be a list")
    patches: dict[int, dict[str, str]] = {}
    for item in patch_items:
        if not isinstance(item, dict):
            raise ValueError("patch item must be an object")
        row, content = _extract_row_patch(item)
        if row in patches:
            raise ValueError(f"duplicate row {row}")
        patches[row] = {
            "content": content,
            "filename": str(item.get("filename") or f"model_patch_{row}.patch"),
            "source_encoding": str(item.get("encoding") or ""),
        }
    if not patches:
        raise ValueError("at least one model patch is required")

    repo_root = str(leaderboards.DEFAULT_REPO_ROOT)
    repo_roots = ""
    hdc_targets = str(payload.get("hdc_targets") or "")
    parallel_slots = min(len(patches), max(1, int(payload.get("parallel_slots") or 1)))
    deveco_path = str(leaderboards.DEFAULT_DEVECO_PATH)
    client_version = str(payload.get("client_version") or "")
    full_regression = bool(payload.get("full_regression"))
    if not client_version and not full_regression:
        full_regression = True
    build_timeout = float(payload.get("build_timeout") or 1800)
    test_timeout = float(payload.get("test_timeout") or 1800)

    preflight = leaderboards.preflight_environment(
        repo_root=repo_root,
        repo_roots=repo_roots,
        hdc_targets=hdc_targets,
        parallel_slots=parallel_slots,
        deveco_path=deveco_path,
        full_regression=full_regression,
    )

    configured_roots = list(preflight.get("repo_roots") or (_split_config_list(repo_roots) or [repo_root]))
    busy_processes = _external_repo_processes(configured_roots)
    if busy_processes:
        raise ValueError("repo root is busy; wait for the current evaluator to finish: " + " | ".join(busy_processes[:3]))

    run_id = _safe_run_id(payload.get("run_id"))
    run_dir = WEB_RUNS_DIR / run_id
    if run_dir.exists():
        run_id = f"{run_id}-{datetime.now().strftime('%H%M%S')}"
        run_dir = WEB_RUNS_DIR / run_id
    created_at = _now()
    input_dir = run_dir / "input"
    patch_dir = input_dir / "model_patches"
    patch_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(leaderboards.BENCHMARK_PATH, input_dir / "benchmark.jsonl")

    selected_rows = sorted(patches)
    rows_meta = [meta for meta in _load_rows_meta() if int(meta.get("row") or 0) in patches]
    if len(rows_meta) != len(patches):
        found = {int(meta.get("row") or 0) for meta in rows_meta}
        raise ValueError(f"missing row metadata: {sorted(set(patches) - found)}")
    model_patch_archive_dir = _archive_model_patch_submission(patches, rows_meta, run_id, created_at)
    for meta in rows_meta:
        row = int(meta["row"])
        patch_path = patch_dir / f"model_patch_{row}.patch"
        meta_path = patch_dir / f"model_patch_{row}.meta.json"
        patch_item = patches[row]
        patch_text = patch_item["content"]
        patch_path.write_text(patch_text, encoding="utf-8", newline="\n")
        _write_json(
            meta_path,
            {
                "instance_id": meta["instance_id"],
                "row": row,
                "variant": "model",
                "uploaded_at": created_at,
                "filename": f"model_patch_{row}.patch",
                "source_filename": patch_item["filename"],
                "source_encoding": patch_item["source_encoding"],
                "model_patch_sha256": leaderboards._sha256_text(patch_text),
            },
        )

    new_test_only_instance_ids = leaderboards._new_test_only_instance_ids_for_full_regression(
        selected_rows,
        full_regression,
    )
    chunks = leaderboards._partition_rows(selected_rows, parallel_slots)
    workers = leaderboards._resolve_parallel_workers(
        parallel_slots=len(chunks),
        repo_root=repo_root,
        repo_roots=repo_roots,
        hdc_targets=hdc_targets,
        deveco_path=deveco_path,
    )

    worker_manifest: list[dict] = []
    processes: list[subprocess.Popen[str]] = []
    log_handles = []
    for index, rows in enumerate(chunks):
        worker = workers[index]
        worker_dir = run_dir / worker["worker"]
        worker_benchmark, worker_patch_dir = leaderboards._copy_subset_inputs(
            input_dir / "benchmark.jsonl",
            patch_dir,
            worker_dir / "input",
            rows,
        )
        worker_instance_ids = {
            str(row.get("instance_id"))
            for row in leaderboards._load_jsonl(worker_benchmark)
        }
        worker_new_test_only_instance_ids = [
            instance_id
            for instance_id in new_test_only_instance_ids
            if instance_id in worker_instance_ids
        ]
        output_path = worker_dir / "raw_results.json"
        log_path = worker_dir / "worker.log"
        command = leaderboards._build_eval_command(
            benchmark_path=worker_benchmark,
            patch_dir=worker_patch_dir,
            output_path=output_path,
            repo_root=worker["repo_root"],
            deveco_path=deveco_path,
            build_timeout=build_timeout,
            test_timeout=test_timeout,
            skip_existing=False,
            full_regression=full_regression,
            new_test_only_instance_ids=worker_new_test_only_instance_ids,
        )
        command.insert(1, "-u")
        log_handle = log_path.open("w", encoding="utf-8", newline="\n")
        process = subprocess.Popen(
            command,
            cwd=str(leaderboards.ARKEVAL_ROOT),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=leaderboards._worker_env(worker["hdc_target"], deveco_path),
        )
        processes.append(process)
        log_handles.append(log_handle)
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

    manifest = {
        "run_id": run_id,
        "created_at": created_at,
        "repo_root": repo_root,
        "repo_roots": repo_roots,
        "hdc_targets": hdc_targets,
        "parallel_slots": len(chunks),
        "deveco_path": deveco_path,
        "full_regression": full_regression,
        "selected_rows": selected_rows,
        "total_rows": TOTAL_ROWS,
        "client_version": client_version or "legacy-forced-full-regression",
        "model_patch_archive_dir": str(model_patch_archive_dir),
        "model_patch_archive_name": model_patch_archive_dir.name,
        "environment_preflight": preflight,
        "workers": worker_manifest,
    }
    _write_json(run_dir / "manifest.json", manifest)
    _write_json(run_dir / "state.json", {"status": "starting", "created_at": manifest["created_at"]})

    RUN_PROCESSES[run_id] = processes
    _write_json(
        run_dir / "state.json",
        {
            "status": "running",
            "created_at": manifest["created_at"],
            "pids": [process.pid for process in processes],
            "parallel_slots": len(chunks),
        },
    )
    thread = threading.Thread(
        target=_monitor,
        args=(run_id, processes, run_dir / "raw_results.json", run_dir / "scorecard", log_handles),
        daemon=True,
    )
    thread.start()
    return _load_run(run_id)


class Handler(BaseHTTPRequestHandler):
    server_version = "ArkTSLeaderboards/0.1"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[web] {self.address_string()} {fmt % args}")

    def _send_json(self, data: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_error_json(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        self._send_json({"error": message}, status)

    def _read_body_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or "0")
        if length > MAX_JSON_BODY:
            raise ValueError("request body is too large")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path == "/api/summary":
                scorecard = leaderboards.RESULTS_DIR / "current_row_runs_scorecard.json"
                score = _read_json(scorecard) if scorecard.is_file() else None
                self._send_json(
                    {
                        "lock_ok": leaderboards.verify_lock(quiet=True),
                        "arkeval_root": str(leaderboards.ARKEVAL_ROOT.resolve()),
                        "total_rows": TOTAL_ROWS,
                        "rows": _load_rows_meta(),
                        "scorecard": score,
                        "runs": _list_runs(),
                    }
                )
                return
            if path == "/api/runs":
                self._send_json({"runs": _list_runs()})
                return
            if path.startswith("/api/runs/"):
                run_id = path.rsplit("/", 1)[-1]
                self._send_json(_load_run(run_id))
                return
            self._serve_static(path)
        except Exception as exc:
            self._send_error_json(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/runs":
                payload = self._read_body_json()
                self._send_json(_start_run(payload), HTTPStatus.CREATED)
                return
            if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/cancel"):
                run_id = unquote(parsed.path).split("/")[-2]
                self._send_json(_cancel_run(run_id))
                return
            self._send_error_json("unknown endpoint", HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_error_json(str(exc))

    def _serve_static(self, path: str) -> None:
        if path in {"", "/"}:
            target = WEB_ROOT / "index.html"
        else:
            rel = path.lstrip("/")
            target = WEB_ROOT / rel
        target = target.resolve()
        web_root = WEB_ROOT.resolve()
        if web_root != target and web_root not in target.parents:
            self.send_error(HTTPStatus.FORBIDDEN.value)
            return
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND.value)
            return
        content_type = "text/plain; charset=utf-8"
        if target.suffix == ".html":
            content_type = "text/html; charset=utf-8"
        elif target.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        elif target.suffix == ".js":
            content_type = "application/javascript; charset=utf-8"
        elif target.suffix == ".svg":
            content_type = "image/svg+xml"
        payload = target.read_bytes()
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(payload)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Serve the ArkTS Leaderboards web UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    WEB_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Leaderboards UI: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
