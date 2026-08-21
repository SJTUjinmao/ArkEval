#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_DATASET = ROOT / "dataset" / "arkeval_dataset.jsonl"
DEFAULT_REPO_POOL = ROOT / "depend" / "repair_repo"
DEFAULT_LOCALIZATION_REPO_POOL = DEFAULT_REPO_POOL / "run01"
DEFAULT_OUTPUT_ROOT = ROOT / "arkfix" / "outputs"
DEFAULT_SCOPED_DATASET_DIR = ROOT / "dataset" / "test_out"
DEFAULT_LOCALIZATION_PYTHON = Path(sys.executable)
DEFAULT_LOCALIZATION_ENV_FILES = (
    ROOT / ".env",
)
OUTPUT_STAGE_EMBEDDING = "01_embedding_localization"
OUTPUT_STAGE_LLM1 = "02_llm1_filter"
OUTPUT_STAGE_LLM2 = "03_llm2_dependency_expansion"


def default_repair_python() -> str:
    current = Path(sys.executable).resolve()
    prefix = Path(sys.prefix).resolve()
    if prefix.parent.name.casefold() == "envs":
        base_python = prefix.parent.parent / current.name
        if base_python.is_file():
            return str(base_python)
    return str(current)


def localization_output_stage(args: argparse.Namespace) -> str:
    if args.no_llm_filter:
        return OUTPUT_STAGE_EMBEDDING
    if args.no_dep_expansion:
        return OUTPUT_STAGE_LLM1
    return OUTPUT_STAGE_LLM2


def safe_file_stem(value: str) -> str:
    out = []
    for char in value.strip():
        out.append(char if char.isalnum() or char in "._-" else "_")
    stem = "".join(out).strip("._-")
    return stem or datetime.now().strftime("%Y%m%d_%H%M%S")


def format_command(command: list[str]) -> str:
    return " ".join(f'"{part}"' if " " in part else part for part in command)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_dotenv_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
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
            values[key] = value
    return values


def resolve_default_localization_env_file() -> Path | None:
    for path in DEFAULT_LOCALIZATION_ENV_FILES:
        if path.is_file():
            return path
    return None


def first_env_value(env: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = env.get(key, "").strip()
        if value:
            return value
    return ""


def provider_env_keys(env: dict[str, str], *suffixes: str) -> list[str]:
    provider = first_env_value(env, "LLM_PROVIDER", "OPENAI_PROVIDER", "MODEL_PROVIDER")
    provider = "".join(char if char.isalnum() else "_" for char in provider).strip("_").upper()
    if not provider:
        return []
    return [f"{provider}_{suffix}" for suffix in suffixes]


def resolve_generative_model(env: dict[str, str], cli_model_name: str) -> str:
    return (
        first_env_value(
            env,
            "LOCALIZATION_ENGINE_LLM_MODEL",
            "LLM_MODEL",
            "GENERATIVE_MODEL",
            *provider_env_keys(env, "MODEL", "OPENAI_MODEL", "MODEL_NAME"),
            "MODEL",
            "OPENAI_MODEL",
            "MODEL_NAME",
        )
        or cli_model_name
    )


def resolve_generative_api_key(env: dict[str, str]) -> str:
    return first_env_value(
        env,
        "LOCALIZATION_ENGINE_LLM_API_KEY",
        *provider_env_keys(env, "OPENAI_API_KEY", "API_KEY"),
        "OPENAI_API_KEY",
        "API_KEY",
    )


def resolve_generative_base_url(env: dict[str, str]) -> str:
    return first_env_value(
        env,
        "LOCALIZATION_ENGINE_LLM_BASE_URL",
        *provider_env_keys(env, "OPENAI_API_BASE_URL", "OPENAI_BASE_URL", "API_BASE_URL", "BASE_URL"),
        "OPENAI_API_BASE_URL",
        "OPENAI_BASE_URL",
        "API_BASE_URL",
        "BASE_URL",
    )


def set_generative_env(env: dict[str, str], *, api_key: str, base_url: str, model: str) -> None:
    env["LOCALIZATION_ENGINE_LLM_API_KEY"] = api_key
    env["LOCALIZATION_ENGINE_LLM_BASE_URL"] = base_url
    env["LOCALIZATION_ENGINE_LLM_MODEL"] = model
    env["OPENAI_API_KEY"] = api_key
    env["OPENAI_API_BASE_URL"] = base_url
    env["OPENAI_BASE_URL"] = base_url
    env["MODEL"] = model
    for key in provider_env_keys(env, "OPENAI_API_KEY", "API_KEY"):
        env[key] = api_key
    for key in provider_env_keys(env, "OPENAI_API_BASE_URL", "OPENAI_BASE_URL", "API_BASE_URL", "BASE_URL"):
        env[key] = base_url
    for key in provider_env_keys(env, "MODEL", "OPENAI_MODEL", "MODEL_NAME"):
        env[key] = model


def run_logged(command: list[str], *, cwd: Path, log_path: Path, env: dict[str, str] | None = None) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", newline="\n") as log:
        log.write("[command] " + format_command(command) + "\n")
        log.flush()
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    return int(completed.returncode)


def build_localization_command(args: argparse.Namespace, *, run_id: str) -> list[str]:
    command = [
        args.localization_python_exe,
        str(ROOT / "localization" / "run_localization.py"),
        "--dataset",
        str(args.dataset.resolve()),
        "--rows",
        args.rows,
        "--repo-pool",
        str(args.localization_repo_pool.resolve()),
        "--run-id",
        run_id,
        "--top-k-files",
        str(args.top_k_files),
    ]
    if args.top_k_hits is not None:
        command.extend(["--top-k-hits", str(args.top_k_hits)])
    if args.raw_scores:
        command.append("--raw-scores")
    if args.no_llm_filter:
        command.append("--no-llm-filter")
    if args.no_dep_expansion:
        command.append("--no-dep-expansion")
    if args.force_index:
        command.append("--force-index")
    if args.no_write_scope:
        command.append("--no-write-scope")
    if args.keep_going:
        command.append("--keep-going")
    if args.reuse_embedding_candidates_root:
        command.extend(["--reuse-embedding-candidates-root", str(args.reuse_embedding_candidates_root.resolve())])
    for flag, value in (
        ("--chunk-workers", args.chunk_workers),
        ("--embedding-batch-size", args.embedding_batch_size),
        ("--embedding-parallel-requests", args.embedding_parallel_requests),
        ("--milvus-upsert-batch-size", args.milvus_upsert_batch_size),
        ("--milvus-upsert-workers", args.milvus_upsert_workers),
        ("--index-queue-size", args.index_queue_size),
        ("--progress-interval-seconds", args.progress_interval_seconds),
    ):
        if value and value > 0:
            command.extend([flag, str(value)])
    return command


def build_repair_command(args: argparse.Namespace, *, enriched_dataset: Path, timestamp: str) -> list[str]:
    command = [
        args.repair_python_exe,
        str(ROOT / "arkfix" / "run_repair.py"),
        "--dataset",
        str(enriched_dataset.resolve()),
        "--rows",
        args.rows,
        "--timestamp",
        timestamp,
        "--output-root",
        str(args.output_root.resolve()),
        "--scoped-dataset-dir",
        str(args.scoped_dataset_dir.resolve()),
        "--repo-pool",
        str(args.repo_pool.resolve()),
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
        "--model-name",
        args.model_name,
        "--temperature",
        str(args.temperature),
        "--top-p",
        str(args.top_p),
        "--quiet",
    ]
    if args.repair_engine_root:
        command.extend(["--repair-engine-root", str(args.repair_engine_root.resolve())])
    if args.config_file:
        command.extend(["--config-file", str(args.config_file.resolve())])
    if args.repo_pools.strip():
        command.extend(["--repo-pools", args.repo_pools.strip()])
    if args.serial_apply_check:
        command.append("--serial-apply-check")
    else:
        command.append("--no-serial-apply-check")
    if args.apply_check_repo_root:
        command.extend(["--apply-check-repo-root", str(args.apply_check_repo_root.resolve())])
    if args.deveco_path.strip():
        command.extend(["--deveco-path", args.deveco_path.strip()])
    if args.skip_preflight:
        command.append("--skip-preflight")
    return command


def summarize_repair_outputs(repair_manifest_path: Path) -> dict[str, Any]:
    if not repair_manifest_path.is_file():
        return {}
    repair_manifest = read_json(repair_manifest_path)
    model_output_root = Path(str(repair_manifest.get("model_output_root") or ""))
    timestamp = str(repair_manifest.get("timestamp") or "")
    model_output_dir = model_output_root / f"model_{timestamp}" if timestamp else model_output_root
    batch_manifest = model_output_dir / "manifest.jsonl"
    patches = []
    for row in read_jsonl(batch_manifest):
        patches.append(
            {
                "row": row.get("row"),
                "instance_id": row.get("instance_id", ""),
                "source_patch": row.get("source_patch", ""),
                "target_patch": row.get("target_patch", ""),
                "source_trajectory": "",
                "bytes": row.get("bytes"),
                "sha256": row.get("sha256", ""),
            }
        )
    repair_times = model_output_dir / "repair_times.json"
    if repair_times.is_file():
        by_row = {item.get("row"): item for item in read_json(repair_times).get("rows", [])}
        for patch in patches:
            timing_row = by_row.get(patch.get("row"))
            if isinstance(timing_row, dict):
                patch["source_trajectory"] = timing_row.get("source_trajectory", "")

    failure_report = model_output_dir / "batch_failure_report.json"
    worker_logs: list[str] = []
    if failure_report.is_file():
        try:
            failure_data = read_json(failure_report)
            workers = failure_data.get("workers", [])
            if isinstance(workers, list):
                for worker in workers:
                    if isinstance(worker, dict) and worker.get("log_path"):
                        worker_logs.append(str(worker["log_path"]))
        except (OSError, json.JSONDecodeError):
            worker_logs = []

    return {
        "repair_manifest": str(repair_manifest_path),
        "batch_manifest": str(batch_manifest) if batch_manifest.is_file() else "",
        "failure_report": str(failure_report),
        "worker_logs": worker_logs,
        "model_output_dir": str(model_output_dir),
        "patches": patches,
    }


def write_pipeline_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run ArkTS localization and repair as one pipeline.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--rows", required=True, help="Original dataset rows to run, e.g. 3 or 1,4,8-12.")
    parser.add_argument("--timestamp", default="", help="Stable run id shared by localization and repair.")
    parser.add_argument("--python-exe", default="", help="Optional compatibility override for both subprocesses.")
    parser.add_argument(
        "--localization-python-exe",
        default=str(DEFAULT_LOCALIZATION_PYTHON if DEFAULT_LOCALIZATION_PYTHON.is_file() else Path(sys.executable)),
    )
    parser.add_argument("--repair-python-exe", default="")

    parser.add_argument("--localization-repo-pool", type=Path, default=DEFAULT_LOCALIZATION_REPO_POOL)
    parser.add_argument("--top-k-files", type=int, default=10)
    parser.add_argument("--top-k-hits", type=int, default=None)
    parser.add_argument("--raw-scores", action="store_true")
    parser.add_argument("--no-llm-filter", action="store_true")
    parser.add_argument("--no-dep-expansion", action="store_true")
    parser.add_argument("--force-index", action="store_true")
    parser.add_argument("--no-write-scope", action="store_true")
    parser.add_argument("--keep-going", action="store_true")
    parser.add_argument(
        "--reuse-embedding-candidates-root",
        type=Path,
        default=None,
        help="Reuse rows/row_xxxxxx/embedding_candidates.jsonl from a previous localization output root.",
    )
    parser.add_argument(
        "--localization-env-file",
        type=Path,
        default=resolve_default_localization_env_file(),
        help="Optional .env file for localization tokens; values are not printed.",
    )
    parser.add_argument(
        "--embedding-backend",
        choices=("dgx", "local", "modelscope"),
        default="dgx",
        help="Deprecated; localization embedding is fixed to dgx.",
    )
    parser.add_argument("--embedding-base-url", default="", help="Embedding server base URL.")
    parser.add_argument("--embedding-base-urls", default="", help="Comma-separated embedding server base URLs.")
    parser.add_argument("--embedding-model", default="", help="Embedding model name/id.")
    parser.add_argument("--embedding-endpoint-path", default="", help="Embedding endpoint path, e.g. embed or embeddings.")
    parser.add_argument("--embedding-timeout-seconds", type=float, default=0.0)
    parser.add_argument("--embedding-max-retries", type=int, default=0)
    parser.add_argument("--embedding-max-length", type=int, default=0, help="DGX custom embedding max_length.")
    parser.add_argument(
        "--embedding-batch-size",
        type=int,
        default=32,
        help="Chunks per embedding request during localization indexing.",
    )
    parser.add_argument(
        "--embedding-parallel-requests",
        type=int,
        default=13,
        help="Concurrent embedding requests during localization indexing.",
    )
    parser.add_argument("--chunk-workers", type=int, default=16)
    parser.add_argument("--milvus-upsert-batch-size", type=int, default=512)
    parser.add_argument("--milvus-upsert-workers", type=int, default=2)
    parser.add_argument("--index-queue-size", type=int, default=2048)
    parser.add_argument("--progress-interval-seconds", type=float, default=2.0)

    parser.add_argument("--repair-engine-root", type=Path, default=None)
    parser.add_argument("--config-file", type=Path, default=None)
    parser.add_argument("--repo-pool", type=Path, default=DEFAULT_REPO_POOL)
    parser.add_argument("--repo-pools", default="")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--scoped-dataset-dir", type=Path, default=DEFAULT_SCOPED_DATASET_DIR)
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--worker-timeout-seconds", type=float, default=14400.0)
    parser.add_argument("--worker-start-interval-seconds", type=float, default=0.25)
    parser.add_argument("--worker-task-batch-size", type=int, default=3)
    parser.add_argument("--build-concurrency", type=int, default=8)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--max-steps-per-instance", type=int, default=50)
    parser.add_argument("--model-name", default="")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--top-p", type=float, default=1.0)
    parser.add_argument(
        "--serial-apply-check",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--apply-check-repo-root", type=Path, default=None)
    parser.add_argument("--deveco-path", default="")
    parser.add_argument("--skip-preflight", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.repair_python_exe:
        args.repair_python_exe = args.python_exe or default_repair_python()
    timestamp = safe_file_stem(args.timestamp or datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    localization_run_id = f"loc_{timestamp}"
    localization_env = os.environ.copy()
    if args.localization_env_file:
        localization_env.update(load_dotenv_values(args.localization_env_file.resolve()))
    args.model_name = resolve_generative_model(localization_env, args.model_name)
    generative_api_key = resolve_generative_api_key(localization_env)
    generative_base_url = resolve_generative_base_url(localization_env)
    missing_generative = []
    if not generative_api_key:
        missing_generative.append("OPENAI_API_KEY")
    if not generative_base_url:
        missing_generative.append("OPENAI_API_BASE_URL")
    if not args.model_name:
        missing_generative.append("MODEL")
    if missing_generative:
        print(
            f"[pipeline] missing generative model config: {', '.join(missing_generative)}",
            flush=True,
        )
        return 2
    set_generative_env(
        localization_env,
        api_key=generative_api_key,
        base_url=generative_base_url,
        model=args.model_name,
    )

    repair_run_dir = args.output_root.resolve() / f"repair_{timestamp}"
    logs_dir = repair_run_dir / "logs"
    localization_log = logs_dir / "localization.log"
    repair_log = logs_dir / "repair_batch.log"
    pipeline_manifest = repair_run_dir / "pipeline_manifest.json"

    repair_run_dir.mkdir(parents=True, exist_ok=True)

    print(f"[pipeline] start rows={args.rows} timestamp={timestamp}", flush=True)

    localization_command = build_localization_command(args, run_id=localization_run_id)
    args.embedding_backend = "dgx"
    localization_env["LOCALIZATION_ENGINE_EMBEDDING_BACKEND"] = "dgx"
    if args.embedding_base_urls.strip():
        localization_env["LOCALIZATION_ENGINE_DGX_EMBEDDING_BASE_URLS"] = args.embedding_base_urls.strip()
    if args.embedding_base_url.strip():
        localization_env["LOCALIZATION_ENGINE_DGX_EMBEDDING_BASE_URL"] = args.embedding_base_url.strip()
    if args.embedding_model.strip():
        localization_env["LOCALIZATION_ENGINE_DGX_EMBEDDING_MODEL"] = args.embedding_model.strip()
    if args.embedding_endpoint_path.strip():
        localization_env["LOCALIZATION_ENGINE_DGX_EMBEDDING_ENDPOINT_PATH"] = args.embedding_endpoint_path.strip()
    if args.embedding_timeout_seconds > 0:
        localization_env["LOCALIZATION_ENGINE_DGX_EMBEDDING_TIMEOUT_SECONDS"] = str(args.embedding_timeout_seconds)
    if args.embedding_max_retries > 0:
        localization_env["LOCALIZATION_ENGINE_DGX_EMBEDDING_MAX_RETRIES"] = str(args.embedding_max_retries)
    if args.embedding_max_length > 0:
        localization_env["LOCALIZATION_ENGINE_DGX_EMBEDDING_MAX_LENGTH"] = str(args.embedding_max_length)
    if args.embedding_batch_size > 0:
        localization_env["LOCALIZATION_ENGINE_EMBEDDING_BATCH_SIZE"] = str(args.embedding_batch_size)
    if args.embedding_parallel_requests > 0:
        localization_env["LOCALIZATION_ENGINE_EMBEDDING_PARALLEL_REQUESTS"] = str(args.embedding_parallel_requests)
    if args.chunk_workers > 0:
        localization_env["LOCALIZATION_ENGINE_CHUNK_WORKERS"] = str(args.chunk_workers)
    if args.milvus_upsert_batch_size > 0:
        localization_env["LOCALIZATION_ENGINE_MILVUS_UPSERT_BATCH_SIZE"] = str(args.milvus_upsert_batch_size)
    if args.milvus_upsert_workers > 0:
        localization_env["LOCALIZATION_ENGINE_MILVUS_UPSERT_WORKERS"] = str(args.milvus_upsert_workers)
    if args.index_queue_size > 0:
        localization_env["LOCALIZATION_ENGINE_INDEX_QUEUE_SIZE"] = str(args.index_queue_size)
    if args.progress_interval_seconds > 0:
        localization_env["LOCALIZATION_ENGINE_PROGRESS_INTERVAL_SECONDS"] = str(args.progress_interval_seconds)
    localization_code = run_logged(
        localization_command,
        cwd=ROOT,
        log_path=localization_log,
        env=localization_env,
    )
    localization_stage = localization_output_stage(args)
    localization_dir = ROOT / "localization" / "outputs" / localization_stage / localization_run_id
    enriched_dataset = localization_dir / "enriched_dataset.jsonl"
    manifest: dict[str, Any] = {
        "timestamp": timestamp,
        "dataset": str(args.dataset.resolve()),
        "rows": args.rows,
        "localization_output_stage": localization_stage,
        "logs": {
            "localization": str(localization_log),
            "repair_batch": str(repair_log),
        },
        "localization_indexing": {
            "backend": args.embedding_backend,
            "base_url": args.embedding_base_url.strip(),
            "base_urls": args.embedding_base_urls.strip(),
            "model": args.embedding_model.strip(),
            "endpoint_path": args.embedding_endpoint_path.strip(),
            "timeout_seconds": args.embedding_timeout_seconds,
            "max_retries": args.embedding_max_retries,
            "max_length": args.embedding_max_length if args.embedding_backend == "dgx" else 0,
            "embedding_batch_size": args.embedding_batch_size,
            "embedding_parallel_requests": args.embedding_parallel_requests,
            "chunk_workers": args.chunk_workers,
            "milvus_upsert_batch_size": args.milvus_upsert_batch_size,
            "milvus_upsert_workers": args.milvus_upsert_workers,
            "index_queue_size": args.index_queue_size,
            "progress_interval_seconds": args.progress_interval_seconds,
            "reuse_embedding_candidates_root": str(args.reuse_embedding_candidates_root.resolve()) if args.reuse_embedding_candidates_root else "",
        },
        "embedding": {
            "backend": args.embedding_backend,
            "base_url": args.embedding_base_url.strip(),
            "base_urls": args.embedding_base_urls.strip(),
            "model": args.embedding_model.strip(),
            "endpoint_path": args.embedding_endpoint_path.strip(),
            "timeout_seconds": args.embedding_timeout_seconds,
            "max_retries": args.embedding_max_retries,
            "max_length": args.embedding_max_length if args.embedding_backend == "dgx" else 0,
            "embedding_batch_size": args.embedding_batch_size,
            "embedding_parallel_requests": args.embedding_parallel_requests,
        },
        "generative_model": {
            "model": args.model_name,
            "localization_env_file": str(args.localization_env_file.resolve()) if args.localization_env_file else "",
        },
        "localization": {
            "command": localization_command,
            "returncode": localization_code,
            "output_dir": str(localization_dir),
            "enriched_dataset": str(enriched_dataset),
        },
    }
    if localization_code != 0 or not enriched_dataset.is_file():
        manifest["status"] = "failed"
        manifest["failed_stage"] = "localization"
        write_pipeline_manifest(pipeline_manifest, manifest)
        print(f"[pipeline] failed stage=localization log={localization_log}", flush=True)
        return localization_code or 1

    print(f"[pipeline] localization ok output={localization_dir}", flush=True)

    repair_command = build_repair_command(args, enriched_dataset=enriched_dataset, timestamp=timestamp)
    repair_code = run_logged(repair_command, cwd=ROOT, log_path=repair_log, env=localization_env)
    repair_manifest_path = repair_run_dir / "manifest.json"
    repair_manifest = read_json(repair_manifest_path) if repair_manifest_path.is_file() else {}
    scoped_dataset = str(repair_manifest.get("scoped_dataset") or "")
    row_mapping = str(repair_manifest.get("row_mapping") or "")
    repair_summary = summarize_repair_outputs(repair_manifest_path)

    manifest["repair"] = {
        "command": repair_command,
        "returncode": repair_code,
        "run_dir": str(repair_run_dir),
        "manifest": str(repair_manifest_path),
        "scoped_dataset": scoped_dataset,
        "row_mapping": row_mapping,
        **repair_summary,
    }
    print(f"[pipeline] repair_input scoped_dataset={scoped_dataset}", flush=True)

    patches = repair_summary.get("patches") if isinstance(repair_summary, dict) else []
    if repair_code != 0:
        manifest["status"] = "failed"
        manifest["failed_stage"] = "repair"
        write_pipeline_manifest(pipeline_manifest, manifest)
        failure_report = repair_summary.get("failure_report", "") if isinstance(repair_summary, dict) else ""
        suffix = f" failure_report={failure_report}" if failure_report else ""
        print(f"[pipeline] failed stage=repair log={repair_log}{suffix}", flush=True)
        return repair_code

    manifest["status"] = "ok"
    write_pipeline_manifest(pipeline_manifest, manifest)
    print(f"[pipeline] repair ok patches={len(patches)}", flush=True)
    for patch in patches:
        patch_path = patch.get("source_patch") or patch.get("target_patch")
        if patch_path:
            print(f"[pipeline] patch row={patch.get('row')} path={patch_path}", flush=True)
    print(f"[pipeline] done run_dir={repair_run_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
