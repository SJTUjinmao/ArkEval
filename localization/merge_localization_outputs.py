#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from localization_engine.config import load_config
from localization_engine.indexer import collection_identity


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "dataset" / "arkeval_dataset.jsonl"
DEFAULT_OUTPUTS_ROOT = Path(__file__).resolve().parent / "outputs"
OUTPUT_STAGE = "01_embedding_localization"
DEFAULT_RUN_ID = "embedding_arkeval502_v1"


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8", errors="strict") as handle:
        for line_no, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON at {path}:{line_no}: {exc}") from exc
            if isinstance(record, dict):
                yield record


def has_nonempty_llm_files(row_dir: Path) -> bool:
    for name in ("llm_trace.jsonl", "llm_core_files.jsonl", "llm_dep_expansion_files.jsonl"):
        path = row_dir / name
        if path.is_file() and path.stat().st_size > 0:
            return True
    return False


def normalize_relative_path(candidate: dict[str, Any], *, repo_root: str) -> str:
    raw_relative = str(candidate.get("relative_path") or "").strip()
    relative = Path(raw_relative)
    root = Path(repo_root).resolve()
    raw_file = str(candidate.get("file_path") or "").strip()
    file_relative = ""
    if raw_file:
        file_path = Path(raw_file)
        if file_path.is_absolute():
            try:
                file_relative = file_path.resolve().relative_to(root).as_posix()
            except ValueError as exc:
                raise ValueError(f"candidate path is outside result repo_root: repo_root={repo_root} file_path={raw_file}") from exc
    if raw_relative and not relative.is_absolute() and ".." not in relative.parts:
        normalized = relative.as_posix()
        if file_relative and file_relative.casefold() != normalized.casefold():
            raise ValueError(
                f"candidate relative_path does not match file_path: relative_path={normalized} file_path={raw_file}"
            )
        return normalized

    raw_file = raw_file or raw_relative
    file_path = Path(raw_file)
    if repo_root:
        try:
            return file_path.resolve().relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError(f"candidate path is outside result repo_root: repo_root={repo_root} file_path={raw_file}") from exc
    raise ValueError(f"cannot normalize candidate path without repo_root: {raw_file}")


def normalized_candidates(result: dict[str, Any], row_dir: Path, *, source_run_id: str) -> list[dict[str, Any]]:
    path = row_dir / "embedding_candidates.jsonl"
    if not path.is_file():
        return []
    repo_root = str(result.get("repo_root") or "")
    if not repo_root:
        raise ValueError(f"row {result.get('row')} has empty repo_root")
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rank, candidate in enumerate(read_jsonl(path), 1):
        relative_path = normalize_relative_path(candidate, repo_root=repo_root)
        key = relative_path.casefold()
        if key in seen:
            raise ValueError(f"row {result.get('row')} has duplicate candidate path: {relative_path}")
        seen.add(key)
        score = float(candidate.get("score", 0.0))
        if not math.isfinite(score):
            raise ValueError(f"row {result.get('row')} candidate {rank} has non-finite score")
        candidates.append(
            {
                "rank": rank,
                "file_path": str((Path(repo_root) / Path(relative_path)).resolve()),
                "relative_path": relative_path,
                "source": str(candidate.get("source") or "embedding"),
                "score": score,
                "source_run_id": source_run_id,
                "source_repo_root": repo_root,
                "collection_name": str(result.get("collection_name") or ""),
                "collection_hostname": str(result.get("collection_hostname") or ""),
                "collection_namespace_hash": str(result.get("collection_namespace_hash") or ""),
            }
        )
    if len(candidates) != 10:
        raise ValueError(f"row {result.get('row')} must contain 10 unique embedding candidates, got {len(candidates)}")
    return candidates


def collect_best(
    outputs_root: Path,
    rows: set[int],
    dataset: list[dict[str, Any]],
    source_run_ids: list[str] | None = None,
) -> dict[int, tuple[tuple[int, int, float], Path, dict[str, Any], list[dict[str, Any]]]]:
    best: dict[int, tuple[tuple[int, int, float], Path, dict[str, Any], list[dict[str, Any]]]] = {}
    if source_run_ids:
        run_dirs = [outputs_root / OUTPUT_STAGE / run_id for run_id in source_run_ids]
        missing_runs = [str(path) for path in run_dirs if not (path / "localization_results.jsonl").is_file()]
        if missing_runs:
            raise FileNotFoundError(f"source localization runs not found: {missing_runs}")
    else:
        run_dirs = sorted(
            {path.parent for path in outputs_root.rglob("localization_results.jsonl")},
            key=lambda path: path.stat().st_mtime,
        )
    for run_dir in run_dirs:
        results_path = run_dir / "localization_results.jsonl"
        if not results_path.is_file():
            continue
        for result in read_jsonl(results_path):
            row = result.get("row")
            if row not in rows or result.get("status") != "ok":
                continue
            row_dir = run_dir / "rows" / f"row_{row:06d}"
            try:
                validate_source_result(dataset[int(row) - 1], result)
                candidates = normalized_candidates(result, row_dir, source_run_id=run_dir.name)
            except ValueError as exc:
                print(f"[merge] reject source row={row} run={run_dir.name}: {exc}", file=sys.stderr)
                continue
            if not candidates:
                continue
            score = (0 if has_nonempty_llm_files(row_dir) else 1, len(candidates), run_dir.stat().st_mtime)
            if source_run_ids and row in best:
                previous_run = best[row][1].parents[1].name
                raise RuntimeError(f"row {row} appears in multiple explicit source runs: {previous_run}, {run_dir.name}")
            if row not in best or score > best[row][0]:
                best[row] = (score, row_dir, result, candidates)
    return best


def result_with_merged_paths(result: dict[str, Any], candidates: list[dict[str, Any]], row_dir: Path) -> dict[str, Any]:
    merged = dict(result)
    merged["absolute_paths"] = [candidate["file_path"] for candidate in candidates]
    merged["relative_paths"] = [candidate["relative_path"] for candidate in candidates]
    merged["embedding_candidates_path"] = str(row_dir / "embedding_candidates.jsonl")
    merged["llm_core_files_path"] = str(row_dir / "llm_core_files.jsonl")
    merged["llm_dep_files_path"] = str(row_dir / "llm_dep_expansion_files.jsonl")
    return merged


def validate_source_result(dataset_record: dict[str, Any], result: dict[str, Any]) -> None:
    row = result.get("row")
    expected_instance = str(dataset_record.get("instance_id") or "")
    if str(result.get("instance_id") or "") != expected_instance:
        raise ValueError(f"row {row} instance_id mismatch")
    base = dataset_record.get("base") if isinstance(dataset_record.get("base"), dict) else {}
    expected_base = str(base.get("sha") or dataset_record.get("base_sha") or "").strip()
    result_base = str(result.get("base_sha") or "").strip()
    reset_head = str(result.get("repo_head_after_reset") or "").strip()
    if result_base.casefold() != expected_base.casefold():
        raise ValueError(f"row {row} base_sha mismatch: expected={expected_base} actual={result_base}")
    if reset_head.casefold() != expected_base.casefold():
        raise ValueError(f"row {row} reset HEAD mismatch: expected={expected_base} actual={reset_head}")
    repo_root = str(result.get("repo_root") or "").strip()
    hostname = str(result.get("collection_hostname") or "").strip()
    if not hostname:
        raise ValueError(f"row {row} collection_hostname is empty")
    cfg = load_config(repo_root)
    identity = collection_identity(cfg.milvus.collection_prefix, cfg.repo_root, hostname=hostname)
    if str(result.get("collection_name") or "") != identity.collection_name:
        raise ValueError(f"row {row} collection_name mismatch")
    if str(result.get("collection_namespace_hash") or "") != identity.collection_namespace_hash:
        raise ValueError(f"row {row} collection namespace mismatch")
    if Path(str(result.get("collection_repo_root") or "")).resolve() != Path(repo_root).resolve():
        raise ValueError(f"row {row} collection_repo_root mismatch")


def arkfix_record(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "row": result["row"],
        "instance_id": result["instance_id"],
        "repo": result["repo"],
        "repo_root": result["repo_root"],
        "base_sha": result["base_sha"],
        "problem": result["query"],
        "localized_file_abs_paths": result["absolute_paths"],
        "localized_file_rel_paths": result["relative_paths"],
        "localization_status": result["status"],
        "localization_error": result.get("error", ""),
        "repo_head_after_reset": result.get("repo_head_after_reset", ""),
        "embedding_candidates_path": result["embedding_candidates_path"],
        "llm_core_files_path": result["llm_core_files_path"],
        "llm_dep_files_path": result["llm_dep_files_path"],
    }


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge split localization runs into one standard localization output.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--outputs-root", type=Path, default=DEFAULT_OUTPUTS_ROOT)
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID)
    parser.add_argument("--source-run-id", action="append", default=[])
    args = parser.parse_args()

    dataset_path = args.dataset.resolve()
    outputs_root = args.outputs_root.resolve()
    out_dir = outputs_root / OUTPUT_STAGE / args.run_id
    if out_dir.exists():
        raise FileExistsError(f"output already exists: {out_dir}")

    dataset = list(read_jsonl(dataset_path))
    rows = set(range(1, len(dataset) + 1))
    source_manifests: list[dict[str, Any]] = []
    for source_run_id in args.source_run_id:
        manifest_path = outputs_root / OUTPUT_STAGE / source_run_id / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"source manifest not found: {manifest_path}")
        source_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if Path(str(source_manifest.get("dataset") or "")).resolve() != dataset_path:
            raise ValueError(f"source manifest dataset mismatch: {source_run_id}")
        source_manifests.append(source_manifest)
    best = collect_best(outputs_root, rows, dataset, args.source_run_id or None)
    missing = sorted(rows - set(best))
    if missing:
        raise RuntimeError(f"missing successful embedding candidates for rows: {missing}")

    rows_dir = out_dir / "rows"
    rows_dir.mkdir(parents=True)
    results: list[dict[str, Any]] = []
    arkfix_records: list[dict[str, Any]] = []
    enriched_records: list[dict[str, Any]] = []

    for row in sorted(rows):
        _, source_row_dir, source_result, candidates = best[row]
        validate_source_result(dataset[row - 1], source_result)
        target_row_dir = rows_dir / f"row_{row:06d}"
        shutil.copytree(source_row_dir, target_row_dir)
        for name in ("llm_trace.jsonl", "llm_core_files.jsonl", "llm_dep_expansion_files.jsonl"):
            (target_row_dir / name).write_text("", encoding="utf-8")
        write_jsonl(target_row_dir / "embedding_candidates.jsonl", candidates)

        result = result_with_merged_paths(source_result, candidates, target_row_dir)
        (target_row_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (target_row_dir / "localized_files_abs.txt").write_text(
            "\n".join(result["absolute_paths"]) + "\n",
            encoding="utf-8",
        )
        (target_row_dir / "localized_files_rel.txt").write_text(
            "\n".join(result["relative_paths"]) + "\n",
            encoding="utf-8",
        )
        results.append(result)
        arkfix_records.append(arkfix_record(result))
        enriched = dict(dataset[row - 1])
        enriched["localized_file_abs_paths"] = result["absolute_paths"]
        enriched["localized_file_rel_paths"] = result["relative_paths"]
        enriched["_localization"] = result
        enriched_records.append(enriched)

    manifest = {
        "dataset": str(dataset_path),
        "repo_root": "",
        "repo_pool": "",
        "repo_pools": sorted({str(item.get("repo_pool") or "") for item in source_manifests}),
        "localization_engine_root": str((ROOT / "localization").resolve()),
        "rows": sorted(rows),
        "top_k_files": 10,
        "top_k_hits": None,
        "no_llm_filter": True,
        "no_dep_expansion": True,
        "raw_scores": False,
        "reuse_embedding_candidates_root": "",
        "force_index_requested": False,
        "force_index_effective": False,
        "index_sync_effective": True,
        "index_mode": "merged_sources",
        "indexing": {
            "worker_count": len(source_manifests),
            "embedding_parallel_requests_total": sum(
                int((item.get("indexing") or {}).get("embedding_parallel_requests") or 0)
                for item in source_manifests
            ),
            "workers": [item.get("indexing") or {} for item in source_manifests],
        },
        "run_id": args.run_id,
        "source_run_ids": args.source_run_id,
        "source_manifests": [
            {
                "run_id": item.get("run_id"),
                "repo_pool": item.get("repo_pool"),
                "rows": item.get("rows"),
                "indexing": item.get("indexing"),
            }
            for item in source_manifests
        ],
        "output_stage": OUTPUT_STAGE,
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
    write_jsonl(out_dir / "localization_results.jsonl", results)
    write_jsonl(out_dir / "arkfix_input.jsonl", arkfix_records)
    write_jsonl(out_dir / "enriched_dataset.jsonl", enriched_records)
    print(f"[done] merged {len(results)} rows; output={out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
