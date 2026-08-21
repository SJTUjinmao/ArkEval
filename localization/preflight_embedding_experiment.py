#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable

from pymilvus import Collection, connections, utility

from localization_engine.config import load_config
from localization_engine.indexer import collection_identity
from run_localization import base_sha_from_record, parse_row_spec, repo_name_from_record, run_git, status_without_codephoenix


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "dataset" / "arkeval_dataset.jsonl"
DEFAULT_ARTIFACT_ROOT = Path(__file__).resolve().parent / "outputs" / "99_experiment_artifacts"
DEFAULT_ENDPOINTS = [f"http://127.0.0.1:{port}" for port in [*range(8108, 8118), 8208, 8209, 8210]]
LEGACY_COLLECTIONS = [
    "codephoenix_applications_app_samples",
    "codephoenix_ImageKnife",
]
CORE_FILES = [
    ROOT / "dataset" / "arkeval_dataset.jsonl",
    ROOT / "start_embedding_cluster.ps1",
    ROOT / "run_embedding_iso10.ps1",
    Path(__file__).resolve(),
    Path(__file__).resolve().parent / "run_localization.py",
    Path(__file__).resolve().parent / "merge_localization_outputs.py",
    Path(__file__).resolve().parent / "validate_embedding_isolation.py",
    Path(__file__).resolve().parent / "localization_engine" / "indexer.py",
    Path(__file__).resolve().parent / "localization_engine" / "locate_flow.py",
    Path(__file__).resolve().parent / "localization_engine" / "milvus" / "client.py",
    Path(__file__).resolve().parent / "localization_engine" / "embedding" / "cache.py",
    Path(__file__).resolve().parent / "localization_engine" / "embedding" / "clients.py",
]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="strict") as handle:
        for line_no, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line:
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"expected JSON object at {path}:{line_no}")
            records.append(value)
    return records


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_assignments(
    values: Iterable[str], *, max_row: int, allow_partial_coverage: bool = False
) -> dict[str, list[int]]:
    assignments: dict[str, list[int]] = {}
    for value in values:
        worker, separator, spec = value.partition("=")
        worker = worker.strip()
        if not separator or not worker or worker in assignments:
            raise ValueError(f"invalid or duplicate assignment: {value}")
        assignments[worker] = parse_row_spec(spec, max_row=max_row)
    expected_workers = [f"run{index:02d}" for index in range(1, 11)]
    if list(assignments) != expected_workers:
        raise ValueError(f"workers must be ordered run01-run10: {list(assignments)}")
    flattened = [row for rows in assignments.values() for row in rows]
    expected_rows = list(range(1, max_row + 1))
    has_duplicates = len(flattened) != len(set(flattened))
    has_missing = sorted(flattened) != expected_rows
    if has_duplicates or (has_missing and not allow_partial_coverage):
        missing = sorted(set(expected_rows) - set(flattened))
        duplicates = sorted({row for row in flattened if flattened.count(row) > 1})
        raise ValueError(f"assignment coverage mismatch: missing={missing} duplicates={duplicates}")
    return assignments


def check_commit_objects(repo_root: Path, shas: Iterable[str]) -> list[str]:
    unique = sorted(set(shas))
    payload = "".join(f"{sha}^{{commit}}\n" for sha in unique)
    completed = subprocess.run(
        ["git", "cat-file", "--batch-check=%(objectname) %(objecttype)"],
        cwd=str(repo_root),
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "git cat-file failed").strip())
    lines = completed.stdout.splitlines()
    return [sha for sha, line in zip(unique, lines) if not line.endswith(" commit")]


def endpoint_urls() -> list[str]:
    raw = os.environ.get("LOCALIZATION_ENGINE_DGX_EMBEDDING_BASE_URLS", "").strip()
    return [part.strip().rstrip("/") for part in raw.split(",") if part.strip()] or DEFAULT_ENDPOINTS


def request_json(url: str, *, data: dict[str, Any] | None = None, timeout: int = 120) -> dict[str, Any]:
    body = None if data is None else json.dumps(data).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object from {url}")
    return value


def check_endpoint(base_url: str) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        health = request_json(f"{base_url}/health", timeout=30)
        response = request_json(
            f"{base_url}/embed",
            data={"texts": ["ArkEval iso10 preflight"], "include_embeddings": True, "max_length": 1024},
        )
        embeddings = response.get("embeddings")
        if embeddings is None and isinstance(response.get("data"), list) and response["data"]:
            embeddings = [response["data"][0].get("embedding")]
        vector = embeddings[0] if isinstance(embeddings, list) and embeddings else []
        errors: list[str] = []
        if not (health.get("ok") or health.get("status") == "ok"):
            errors.append("health not ok")
        if "Qwen3-Embedding-8B" not in str(health.get("model") or ""):
            errors.append(f"unexpected model={health.get('model')}")
        if int(health.get("dim") or 0) != 4096 or int(response.get("dim") or 0) != 4096:
            errors.append("dim is not 4096")
        if int(health.get("max_allowed_length") or 0) != 1024 or int(response.get("max_length") or 0) != 1024:
            errors.append("max_length contract mismatch")
        if len(vector) != 4096:
            errors.append(f"vector length={len(vector)}")
        return {
            "base_url": base_url,
            "ok": not errors,
            "model": health.get("model", ""),
            "dim": response.get("dim", 0),
            "vector_length": len(vector),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "errors": errors,
        }
    except Exception as exc:
        return {
            "base_url": base_url,
            "ok": False,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "errors": [f"{type(exc).__name__}: {exc}"],
        }


def require_stable_milvus_healthz(*, attempts: int = 3) -> None:
    for attempt in range(attempts):
        with urllib.request.urlopen("http://127.0.0.1:9091/healthz", timeout=5) as response:
            if response.status != 200:
                raise RuntimeError(f"Milvus healthz returned HTTP {response.status}")
        if attempt + 1 < attempts:
            time.sleep(2)


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight the fixed ArkEval 502-row iso10 embedding experiment.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--repo-pool-root", type=Path, default=ROOT / "depend" / "repair_repo")
    parser.add_argument("--assignment", action="append", required=True)
    parser.add_argument("--run-prefix", required=True)
    parser.add_argument("--run-stamp", required=True)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--python-exe", default=sys.executable)
    parser.add_argument("--embedding-batch-size", type=int, default=32)
    parser.add_argument("--embedding-parallel-requests", type=int, default=13)
    parser.add_argument("--embedding-max-length", type=int, default=1024)
    parser.add_argument("--min-free-gb", type=float, default=20.0)
    parser.add_argument("--allow-existing-expected-collections", action="store_true")
    parser.add_argument("--allow-partial-coverage", action="store_true")
    args = parser.parse_args()

    dataset_path = args.dataset.resolve()
    dataset = read_jsonl(dataset_path)
    assignments = parse_assignments(
        args.assignment,
        max_row=len(dataset),
        allow_partial_coverage=args.allow_partial_coverage,
    )
    repo_pool_root = args.repo_pool_root.resolve()
    artifact = (args.artifact or DEFAULT_ARTIFACT_ROOT / f"{args.run_prefix}_preflight_{args.run_stamp}.json").resolve()
    if not artifact.parent.is_dir():
        raise FileNotFoundError(f"artifact directory does not exist: {artifact.parent}")

    failures: list[str] = []
    file_hashes: dict[str, str] = {}
    for path in CORE_FILES:
        if path.is_file():
            file_hashes[str(path.resolve())] = sha256_file(path)
        else:
            failures.append(f"core file missing: {path}")

    repo_groups: dict[tuple[str, str], dict[str, Any]] = {}
    for worker, rows in assignments.items():
        pool = repo_pool_root / worker
        for row in rows:
            record = dataset[row - 1]
            repo_name = repo_name_from_record(record)
            repo_path = Path(repo_name)
            if repo_path.is_absolute() or len(repo_path.parts) != 1 or repo_name in {".", ".."}:
                failures.append(f"{worker}/row{row}: invalid dataset repo name: {repo_name}")
                continue
            resolved_repo = (pool / repo_name).resolve()
            try:
                resolved_repo.relative_to(pool.resolve())
            except ValueError:
                failures.append(f"{worker}/row{row}: repo escapes assigned pool: {repo_name}")
                continue
            key = (worker, repo_name)
            group = repo_groups.setdefault(
                key,
                {"worker": worker, "repo": repo_name, "repo_root": str(resolved_repo), "rows": [], "base_shas": []},
            )
            group["rows"].append(row)
            group["base_shas"].append(base_sha_from_record(record))

    expected_collections: dict[str, dict[str, Any]] = {}
    repo_checks: list[dict[str, Any]] = []
    for group in repo_groups.values():
        repo_root = Path(group["repo_root"])
        errors: list[str] = []
        head = ""
        dirty = ""
        missing_commits: list[str] = []
        identity = None
        try:
            if not repo_root.is_dir():
                raise FileNotFoundError(f"repo directory missing: {repo_root}")
            if run_git(repo_root, ["rev-parse", "--is-inside-work-tree"], timeout=30).casefold() != "true":
                raise RuntimeError("not a git worktree")
            head = run_git(repo_root, ["rev-parse", "HEAD"], timeout=30)
            dirty = status_without_codephoenix(repo_root)
            if dirty:
                errors.append(f"tracked worktree dirty: {dirty}")
            missing_commits = check_commit_objects(repo_root, group["base_shas"])
            if missing_commits:
                errors.append(f"missing base commits: {missing_commits}")
            cfg = load_config(repo_root)
            identity = collection_identity(cfg.milvus.collection_prefix, cfg.repo_root)
            expected_collections.setdefault(
                identity.collection_name,
                {**identity.to_dict(), "worker": group["worker"], "repo": group["repo"]},
            )
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        if errors:
            failures.extend(f"{group['worker']}/{group['repo']}: {error}" for error in errors)
        repo_checks.append(
            {
                **group,
                "head": head,
                "dirty": dirty,
                "missing_commits": missing_commits,
                "collection": identity.to_dict() if identity else {},
                "ok": not errors,
                "errors": errors,
            }
        )

    if len(expected_collections) != len(repo_groups):
        failures.append(f"collection identity collision: repos={len(repo_groups)} collections={len(expected_collections)}")

    urls = endpoint_urls()
    endpoint_checks: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=len(urls)) as executor:
        futures = {executor.submit(check_endpoint, url): url for url in urls}
        for future in as_completed(futures):
            endpoint_checks.append(future.result())
    endpoint_checks.sort(key=lambda item: item["base_url"])
    if len(endpoint_checks) != 13:
        failures.append(f"expected 13 embedding endpoints, got {len(endpoint_checks)}")
    failures.extend(
        f"embedding endpoint {item['base_url']}: {'; '.join(item['errors'])}"
        for item in endpoint_checks
        if not item["ok"]
    )

    milvus_report: dict[str, Any] = {"ok": False, "host": "127.0.0.1", "port": 19530}
    try:
        require_stable_milvus_healthz()
        connections.connect("iso10_preflight", host="127.0.0.1", port="19530")
        existing = set(utility.list_collections(using="iso10_preflight"))
        unexpected = sorted(existing.intersection(expected_collections))
        legacy_counts = {
            name: Collection(name, using="iso10_preflight").num_entities
            for name in LEGACY_COLLECTIONS
            if name in existing
        }
        milvus_report.update(
            {
                "ok": not unexpected or args.allow_existing_expected_collections,
                "healthz_consecutive_successes": 3,
                "existing_collection_count": len(existing),
                "unexpected_expected_collections": unexpected,
                "legacy_collection_counts": legacy_counts,
            }
        )
        milvus_report["allow_existing_expected_collections"] = args.allow_existing_expected_collections
        if unexpected and not args.allow_existing_expected_collections:
            failures.append(f"expected new collections already exist: {unexpected}")
    except Exception as exc:
        milvus_report["error"] = f"{type(exc).__name__}: {exc}"
        failures.append(f"Milvus preflight failed: {exc}")

    disk = shutil.disk_usage(ROOT.anchor or ROOT)
    free_gb = disk.free / (1024**3)
    if free_gb < args.min_free_gb:
        failures.append(f"free disk below threshold: {free_gb:.2f} GB < {args.min_free_gb:.2f} GB")

    expected_run_ids = [f"{args.run_prefix}_p{index}_{args.run_stamp}" for index in range(1, 11)]
    output_stage = Path(__file__).resolve().parent / "outputs" / "01_embedding_localization"
    existing_outputs = [run_id for run_id in expected_run_ids if (output_stage / run_id).exists()]
    merged_run_id = f"{args.run_prefix}_merged_{args.run_stamp}"
    if existing_outputs:
        failures.append(f"partial output directories already exist: {existing_outputs}")
    if (output_stage / merged_run_id).exists():
        failures.append(f"merged output directory already exists: {output_stage / merged_run_id}")

    report = {
        "ok": not failures,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "hostname": socket.gethostname().casefold(),
        "python_exe": str(Path(args.python_exe).resolve()),
        "dataset": str(dataset_path),
        "dataset_rows": len(dataset),
        "run_prefix": args.run_prefix,
        "run_stamp": args.run_stamp,
        "expected_run_ids": expected_run_ids,
        "merged_run_id": merged_run_id,
        "embedding": {
            "model": "Qwen/Qwen3-Embedding-8B",
            "batch_size": args.embedding_batch_size,
            "parallel_requests": args.embedding_parallel_requests,
            "max_length": args.embedding_max_length,
            "endpoints": endpoint_checks,
        },
        "assignments": assignments,
        "assignment_counts": {worker: len(rows) for worker, rows in assignments.items()},
        "allow_partial_coverage": args.allow_partial_coverage,
        "repo_checks": repo_checks,
        "expected_collections": list(expected_collections.values()),
        "milvus": milvus_report,
        "disk": {"root": ROOT.anchor, "free_gb": round(free_gb, 3), "minimum_free_gb": args.min_free_gb},
        "file_sha256": file_hashes,
        "failures": failures,
    }
    artifact.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "artifact": str(artifact), "failures": len(failures)}, ensure_ascii=False))
    for failure in failures:
        print(f"[FAIL] {failure}", file=sys.stderr)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
