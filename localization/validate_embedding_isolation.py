#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
from pathlib import Path
from typing import Any, Iterable

from pymilvus import Collection, connections, utility

from localization_engine.config import load_config
from localization_engine.indexer import collection_identity
from run_localization import parse_row_spec


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "dataset" / "arkeval_dataset.jsonl"
REPORT_FIELDS = [
    "row",
    "instance_id",
    "repo",
    "status",
    "candidate_count",
    "unique_count",
    "collection_name",
    "collection_hostname",
    "errors",
]


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8", errors="strict") as handle:
        for line_no, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON at {path}:{line_no}: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"expected JSON object at {path}:{line_no}")
            yield value


def safe_relative_path(candidate: dict[str, Any], repo_root: Path) -> str:
    raw_relative = str(candidate.get("relative_path") or "").strip().replace("\\", "/")
    if not raw_relative:
        raise ValueError("candidate relative_path is empty")
    relative = Path(raw_relative)
    raw_file = str(candidate.get("file_path") or "").strip()
    if not raw_file:
        raise ValueError("candidate file_path is empty")
    file_path = Path(raw_file)
    if not file_path.is_absolute():
        raise ValueError(f"candidate file_path is not absolute: {raw_file}")
    try:
        file_relative = file_path.resolve().relative_to(repo_root).as_posix()
    except ValueError as exc:
        raise ValueError(f"candidate outside repo_root: repo_root={repo_root} file_path={raw_file}") from exc
    if raw_relative and not relative.is_absolute() and ".." not in relative.parts:
        normalized = relative.as_posix()
        if file_relative.casefold() != normalized.casefold():
            raise ValueError(
                f"candidate relative_path does not match file_path: relative_path={normalized} file_path={raw_file}"
            )
        return normalized
    raise ValueError(f"candidate relative_path is invalid: {raw_relative}")


def load_preflight_bindings(path: Path) -> tuple[dict[str, Any], dict[int, dict[str, Any]]]:
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("ok") is not True:
        raise ValueError(f"preflight artifact is not successful: {path}")
    bindings: dict[int, dict[str, Any]] = {}
    for item in report.get("repo_checks", []):
        collection = item.get("collection") if isinstance(item.get("collection"), dict) else {}
        binding = {
            "worker": str(item.get("worker") or ""),
            "repo": str(item.get("repo") or ""),
            "repo_root": str(item.get("repo_root") or ""),
            "collection": collection,
        }
        for row in item.get("rows", []):
            row_number = int(row)
            if row_number in bindings:
                raise ValueError(f"row {row_number} appears more than once in preflight bindings")
            bindings[row_number] = binding
    return report, bindings


def validate_row(
    row: int,
    dataset_record: dict[str, Any],
    row_dir: Path,
    expected_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    result_path = row_dir / "result.json"
    candidates_path = row_dir / "embedding_candidates.jsonl"
    if not result_path.is_file():
        return {
            "row": row, "instance_id": "", "repo": "", "status": "error", "candidate_count": 0,
            "unique_count": 0, "collection_name": "", "collection_hostname": "", "errors": "missing result.json",
        }
    if not candidates_path.is_file():
        return {
            "row": row, "instance_id": "", "repo": "", "status": "error", "candidate_count": 0,
            "unique_count": 0, "collection_name": "", "collection_hostname": "", "errors": "missing embedding_candidates.jsonl",
        }

    result = json.loads(result_path.read_text(encoding="utf-8"))
    repo_root = Path(str(result.get("repo_root") or "")).resolve()
    expected_instance = str(dataset_record.get("instance_id") or "")
    base = dataset_record.get("base") if isinstance(dataset_record.get("base"), dict) else {}
    expected_base = str(base.get("sha") or dataset_record.get("base_sha") or "").strip()
    if result.get("status") != "ok":
        errors.append(f"result status={result.get('status')}")
    if str(result.get("instance_id") or "") != expected_instance:
        errors.append("instance_id mismatch")
    if str(result.get("base_sha") or "").casefold() != expected_base.casefold():
        errors.append("base_sha mismatch")
    if str(result.get("repo_head_after_reset") or "").casefold() != expected_base.casefold():
        errors.append("repo_head_after_reset mismatch")

    if expected_binding is not None:
        expected_root = Path(str(expected_binding.get("repo_root") or "")).resolve()
        if repo_root != expected_root:
            errors.append(f"repo_root does not match preflight worker={expected_binding.get('worker')}")
        if str(result.get("repo") or "") != str(expected_binding.get("repo") or ""):
            errors.append("repo does not match preflight")
        expected_collection = expected_binding.get("collection")
        if isinstance(expected_collection, dict):
            for field in (
                "collection_name",
                "collection_hostname",
                "collection_namespace_hash",
                "collection_repo_root",
            ):
                actual = str(result.get(field) or "")
                expected = str(expected_collection.get(field) or "")
                if field.endswith("repo_root"):
                    if Path(actual).resolve() != Path(expected).resolve():
                        errors.append(f"{field} does not match preflight")
                elif actual != expected:
                    errors.append(f"{field} does not match preflight")

    try:
        hostname = str(result.get("collection_hostname") or "").strip()
        if not hostname:
            raise ValueError("collection_hostname is empty")
        cfg = load_config(repo_root)
        identity = collection_identity(cfg.milvus.collection_prefix, cfg.repo_root, hostname=hostname)
        if str(result.get("collection_name") or "") != identity.collection_name:
            errors.append("collection_name mismatch")
        if str(result.get("collection_namespace_hash") or "") != identity.collection_namespace_hash:
            errors.append("collection_namespace_hash mismatch")
        if Path(str(result.get("collection_repo_root") or "")).resolve() != repo_root:
            errors.append("collection_repo_root mismatch")
    except Exception as exc:
        errors.append(f"collection identity error: {exc}")

    snapshot_path = row_dir / "index_snapshot.json"
    if not snapshot_path.is_file():
        errors.append("missing index_snapshot.json")
    else:
        try:
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            state = snapshot.get("index_state")
            audit = state.get("collection_audit") if isinstance(state, dict) else None
            if not isinstance(state, dict) or state.get("status") != "done":
                errors.append("index snapshot status is not done")
            if not isinstance(audit, dict) or audit.get("ok") is not True:
                errors.append("index snapshot collection audit is not successful")
            elif (
                int(audit.get("duplicate_chunks", -1)) != 0
                or int(audit.get("expected_chunks", -1)) != int(audit.get("visible_chunks", -2))
            ):
                errors.append("index snapshot collection audit counts mismatch")
            if isinstance(state, dict) and str(state.get("collection") or "") != str(result.get("collection_name") or ""):
                errors.append("index snapshot collection mismatch")
            if not str(snapshot.get("chunks_manifest_sha256") or ""):
                errors.append("index snapshot manifest hash is missing")
        except Exception as exc:
            errors.append(f"invalid index snapshot: {exc}")

    candidates = list(read_jsonl(candidates_path))
    seen: set[str] = set()
    ranks: list[int] = []
    for candidate in candidates:
        try:
            relative = safe_relative_path(candidate, repo_root)
            key = relative.casefold()
            if key in seen:
                errors.append(f"duplicate relative path: {relative}")
            seen.add(key)
        except Exception as exc:
            errors.append(str(exc))
        try:
            rank = int(candidate.get("rank"))
            ranks.append(rank)
        except (TypeError, ValueError):
            errors.append("invalid rank")
        try:
            score = float(candidate.get("score"))
            if not math.isfinite(score):
                errors.append("non-finite score")
        except (TypeError, ValueError):
            errors.append("invalid score")
    if len(candidates) != 10:
        errors.append(f"candidate_count={len(candidates)}")
    if len(seen) != 10:
        errors.append(f"unique_count={len(seen)}")
    if ranks != list(range(1, len(candidates) + 1)):
        errors.append("ranks are not continuous")

    return {
        "row": row,
        "instance_id": expected_instance,
        "repo": dataset_record.get("repo", ""),
        "status": "ok" if not errors else "error",
        "candidate_count": len(candidates),
        "unique_count": len(seen),
        "collection_name": result.get("collection_name", ""),
        "collection_hostname": result.get("collection_hostname", ""),
        "errors": " | ".join(dict.fromkeys(errors)),
    }


def calculate_metrics(
    dataset: list[dict[str, Any]],
    selected_rows: list[int],
    rows_root: Path,
) -> dict[str, Any]:
    top_k_hits = {1: 0, 3: 0, 5: 0, 10: 0}
    issue_all_hit = 0
    reciprocal_rank_sum = 0.0
    defect_file_total = 0
    defect_file_hit_total = 0
    candidate_positions = 0
    row_metrics: list[dict[str, Any]] = []

    for row in selected_rows:
        record = dataset[row - 1]
        result = json.loads((rows_root / f"row_{row:06d}" / "result.json").read_text(encoding="utf-8"))
        repo_root = Path(str(result.get("repo_root") or "")).resolve()
        candidates = list(read_jsonl(rows_root / f"row_{row:06d}" / "embedding_candidates.jsonl"))
        candidate_paths = [safe_relative_path(candidate, repo_root).casefold() for candidate in candidates]
        defect_files = [
            Path(str(path).strip().replace("\\", "/")).as_posix().casefold()
            for path in record.get("defect_files", [])
            if str(path).strip()
        ]
        defect_set = set(defect_files)
        ranks = [index for index, path in enumerate(candidate_paths, 1) if path in defect_set]
        first_rank = min(ranks) if ranks else None
        hit_files = defect_set.intersection(candidate_paths)
        for k in top_k_hits:
            if first_rank is not None and first_rank <= k:
                top_k_hits[k] += 1
        if defect_set and hit_files == defect_set:
            issue_all_hit += 1
        if first_rank is not None:
            reciprocal_rank_sum += 1.0 / first_rank
        defect_file_total += len(defect_set)
        defect_file_hit_total += len(hit_files)
        candidate_positions += len(candidate_paths)
        row_metrics.append(
            {
                "row": row,
                "defect_file_count": len(defect_set),
                "hit_file_count": len(hit_files),
                "first_hit_rank": first_rank,
                "any_hit": first_rank is not None,
                "all_hit": bool(defect_set and hit_files == defect_set),
            }
        )

    row_count = len(selected_rows)
    return {
        "rows": row_count,
        "candidate_positions": candidate_positions,
        "top_k": {
            str(k): {"issues": count, "rate": count / row_count if row_count else 0.0}
            for k, count in top_k_hits.items()
        },
        "issue_any_hit": {
            "issues": top_k_hits[10],
            "rate": top_k_hits[10] / row_count if row_count else 0.0,
        },
        "issue_all_hit": {
            "issues": issue_all_hit,
            "rate": issue_all_hit / row_count if row_count else 0.0,
        },
        "file_recall": {
            "hit_files": defect_file_hit_total,
            "defect_files": defect_file_total,
            "rate": defect_file_hit_total / defect_file_total if defect_file_total else 0.0,
        },
        "mrr": reciprocal_rank_sum / row_count if row_count else 0.0,
        "row_metrics": row_metrics,
    }


def write_metrics(metrics: dict[str, Any], json_path: Path | None, md_path: Path | None) -> None:
    if json_path is not None:
        if not json_path.parent.is_dir():
            raise FileNotFoundError(f"metrics directory does not exist: {json_path.parent}")
        json_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if md_path is not None:
        if not md_path.parent.is_dir():
            raise FileNotFoundError(f"metrics directory does not exist: {md_path.parent}")
        lines = [
            "# Embedding Localization Accuracy",
            "",
            f"- Rows: {metrics['rows']}",
            f"- Candidate positions: {metrics['candidate_positions']}",
        ]
        for k in (1, 3, 5, 10):
            item = metrics["top_k"][str(k)]
            lines.append(f"- Top-{k}: {item['issues']}/{metrics['rows']} ({item['rate']:.2%})")
        any_hit = metrics["issue_any_hit"]
        all_hit = metrics["issue_all_hit"]
        recall = metrics["file_recall"]
        lines.extend(
            [
                f"- Issue Any Hit: {any_hit['issues']}/{metrics['rows']} ({any_hit['rate']:.2%})",
                f"- Issue All Hit: {all_hit['issues']}/{metrics['rows']} ({all_hit['rate']:.2%})",
                f"- File Recall: {recall['hit_files']}/{recall['defect_files']} ({recall['rate']:.2%})",
                f"- MRR: {metrics['mrr']:.6f}",
                "",
            ]
        )
        md_path.write_text("\n".join(lines), encoding="utf-8")


def path_is_within(raw_path: str, repo_root: Path) -> bool:
    if not raw_path or not Path(raw_path).is_absolute():
        return False
    path_text = os.path.normcase(str(Path(raw_path).resolve())).rstrip("\\/")
    root_text = os.path.normcase(str(repo_root.resolve())).rstrip("\\/")
    return path_text == root_text or path_text.startswith(root_text + os.sep)


def audit_milvus_collections(
    selected_rows: list[int],
    rows_root: Path,
    *,
    host: str,
    port: int,
    expected_collections: dict[str, Path] | None = None,
) -> dict[str, Any]:
    result_collections: dict[str, Path] = {}
    mapping_errors: list[str] = []
    for row in selected_rows:
        result = json.loads((rows_root / f"row_{row:06d}" / "result.json").read_text(encoding="utf-8"))
        name = str(result.get("collection_name") or "")
        repo_root = Path(str(result.get("repo_root") or "")).resolve()
        if not name:
            mapping_errors.append(f"row {row} has empty collection_name")
            continue
        previous = result_collections.get(name)
        if previous is not None and os.path.normcase(str(previous)) != os.path.normcase(str(repo_root)):
            mapping_errors.append(f"collection {name} maps to multiple repo roots: {previous}, {repo_root}")
        result_collections[name] = repo_root
    expected = expected_collections or result_collections
    if expected_collections is not None:
        if set(result_collections) != set(expected_collections):
            mapping_errors.append(
                "collection set does not match preflight: "
                f"actual={sorted(result_collections)} expected={sorted(expected_collections)}"
            )
        for name in set(result_collections).intersection(expected_collections):
            if os.path.normcase(str(result_collections[name])) != os.path.normcase(str(expected_collections[name])):
                mapping_errors.append(f"collection {name} repo root does not match preflight")

    alias = "embedding_isolation_audit"
    connections.connect(alias, host=host, port=str(port))
    existing = set(utility.list_collections(using=alias))
    collections: list[dict[str, Any]] = []
    total_entities = 0
    total_audited = 0
    total_foreign = 0
    total_duplicates = 0
    for name, repo_root in sorted(expected.items()):
        errors: list[str] = []
        foreign_examples: list[str] = []
        collection_foreign = 0
        duplicate_chunks = 0
        audited = 0
        entity_count = 0
        raw_num_entities = 0
        if name not in existing:
            errors.append("collection missing")
        else:
            collection = Collection(name, using=alias)
            collection.load()
            raw_num_entities = int(collection.num_entities)
            batch: list[dict[str, Any]] = []
            last_id = -1
            while True:
                page = collection.query(
                    expr=f"id > {last_id}",
                    output_fields=["id", "file_path", "line_start", "line_end", "chunk_hash"],
                    limit=10000,
                    consistency_level="Strong",
                )
                if not page:
                    break
                page.sort(key=lambda item: int(item["id"]))
                next_id = int(page[-1]["id"])
                if next_id <= last_id:
                    errors.append(f"entity audit pagination did not advance after id={last_id}")
                    break
                batch.extend(page)
                last_id = next_id
                if len(page) < 10000:
                    break
            seen_chunks: set[tuple[str, int, int, str]] = set()
            for item in batch:
                raw_path = str(item.get("file_path") or "")
                if not path_is_within(raw_path, repo_root):
                    collection_foreign += 1
                    total_foreign += 1
                    if len(foreign_examples) < 20:
                        foreign_examples.append(raw_path)
                key = (
                    raw_path,
                    int(item.get("line_start") or 0),
                    int(item.get("line_end") or 0),
                    str(item.get("chunk_hash") or ""),
                )
                if key in seen_chunks:
                    duplicate_chunks += 1
                seen_chunks.add(key)
            audited = len(batch)
            entity_count = audited
        total_duplicates += duplicate_chunks
        total_entities += entity_count
        total_audited += audited
        collections.append(
            {
                "collection_name": name,
                "repo_root": str(repo_root),
                "num_entities": entity_count,
                "raw_num_entities": raw_num_entities,
                "audited_entities": audited,
                "foreign_path_count": collection_foreign,
                "duplicate_chunk_count": duplicate_chunks,
                "foreign_path_examples": foreign_examples,
                "ok": not errors and collection_foreign == 0 and duplicate_chunks == 0,
                "errors": errors,
            }
        )
    failed = [item for item in collections if not item["ok"]]
    return {
        "ok": not mapping_errors and not failed and total_foreign == 0 and total_duplicates == 0,
        "host": host,
        "port": port,
        "expected_collections": len(expected),
        "num_entities": total_entities,
        "audited_entities": total_audited,
        "foreign_path_count": total_foreign,
        "duplicate_chunk_count": total_duplicates,
        "mapping_errors": mapping_errors,
        "collections": collections,
    }


def write_reports(records: list[dict[str, Any]], csv_path: Path | None, md_path: Path | None) -> None:
    if csv_path is not None:
        if not csv_path.parent.is_dir():
            raise FileNotFoundError(f"report directory does not exist: {csv_path.parent}")
        with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS)
            writer.writeheader()
            writer.writerows(records)
    if md_path is not None:
        if not md_path.parent.is_dir():
            raise FileNotFoundError(f"report directory does not exist: {md_path.parent}")
        failed = [record for record in records if record["status"] != "ok"]
        lines = [
            "# Embedding Isolation Validation",
            "",
            f"- Rows: {len(records)}",
            f"- Passed: {len(records) - len(failed)}",
            f"- Failed: {len(failed)}",
            "",
        ]
        if failed:
            lines.extend(["## Failures", ""])
            lines.extend(f"- row {record['row']}: {record['errors']}" for record in failed)
            lines.append("")
        md_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate embedding candidates are isolated to each row's repo worker.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--csv-report", type=Path)
    parser.add_argument("--md-report", type=Path)
    parser.add_argument("--expected-rows", default="")
    parser.add_argument("--require-all-rows", action="store_true")
    parser.add_argument("--audit-milvus", action="store_true")
    parser.add_argument("--milvus-host", default="127.0.0.1")
    parser.add_argument("--milvus-port", type=int, default=19530)
    parser.add_argument("--milvus-report", type=Path)
    parser.add_argument("--metrics-json", type=Path)
    parser.add_argument("--metrics-md", type=Path)
    parser.add_argument("--preflight", type=Path)
    parser.add_argument("--worker", default="")
    args = parser.parse_args()

    dataset = list(read_jsonl(args.dataset.resolve()))
    output_root = args.output_root.resolve()
    rows_root = output_root / "rows"
    manifest_path = output_root / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        selected_rows = [int(row) for row in manifest.get("rows", [])]
    else:
        selected_rows = list(range(1, len(dataset) + 1))
    if not selected_rows:
        raise ValueError(f"no rows declared by output manifest: {manifest_path}")
    if args.expected_rows:
        expected_rows = parse_row_spec(args.expected_rows, max_row=len(dataset))
        if selected_rows != expected_rows:
            raise ValueError(f"output row declaration mismatch: declared={selected_rows} expected={expected_rows}")
    if args.require_all_rows and selected_rows != list(range(1, len(dataset) + 1)):
        raise ValueError(f"output does not declare all dataset rows: declared={len(selected_rows)} expected={len(dataset)}")
    preflight = None
    bindings: dict[int, dict[str, Any]] = {}
    if args.preflight:
        preflight, bindings = load_preflight_bindings(args.preflight.resolve())
        missing_bindings = [row for row in selected_rows if row not in bindings]
        if missing_bindings:
            raise ValueError(f"rows missing from preflight bindings: {missing_bindings}")
        if args.worker:
            wrong_worker = [row for row in selected_rows if bindings[row].get("worker") != args.worker]
            if wrong_worker:
                raise ValueError(f"rows do not belong to preflight worker {args.worker}: {wrong_worker}")
    records = [
        validate_row(row, record, rows_root / f"row_{row:06d}", bindings.get(row))
        for row in selected_rows
        for record in [dataset[row - 1]]
    ]
    write_reports(
        records,
        args.csv_report.resolve() if args.csv_report else None,
        args.md_report.resolve() if args.md_report else None,
    )
    failed = [record for record in records if record["status"] != "ok"]
    metrics = None
    if args.metrics_json or args.metrics_md:
        metrics = calculate_metrics(dataset, selected_rows, rows_root)

    milvus_audit = None
    if args.audit_milvus:
        preflight_collections: dict[str, Path] | None = None
        if bindings:
            preflight_collections = {}
            for row in selected_rows:
                collection = bindings[row].get("collection")
                if isinstance(collection, dict):
                    name = str(collection.get("collection_name") or "")
                    root = Path(str(collection.get("collection_repo_root") or "")).resolve()
                    if name:
                        preflight_collections[name] = root
        milvus_audit = audit_milvus_collections(
            selected_rows,
            rows_root,
            host=args.milvus_host,
            port=args.milvus_port,
            expected_collections=preflight_collections,
        )
        if args.milvus_report:
            report_path = args.milvus_report.resolve()
            if not report_path.parent.is_dir():
                raise FileNotFoundError(f"Milvus report directory does not exist: {report_path.parent}")
            report_path.write_text(json.dumps(milvus_audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    summary = {"rows": len(records), "passed": len(records) - len(failed), "failed": len(failed)}
    if metrics is not None:
        summary["candidate_positions"] = metrics["candidate_positions"]
        summary["issue_any_hit"] = metrics["issue_any_hit"]
        summary["file_recall"] = metrics["file_recall"]
        summary["mrr"] = metrics["mrr"]
    if milvus_audit is not None:
        summary["milvus_audit_ok"] = milvus_audit["ok"]
        summary["milvus_foreign_path_count"] = milvus_audit["foreign_path_count"]
        summary["milvus_duplicate_chunk_count"] = milvus_audit["duplicate_chunk_count"]
    print(json.dumps(summary, ensure_ascii=False))
    if failed:
        for record in failed[:20]:
            print(f"row {record['row']}: {record['errors']}")
        return 1
    if metrics is not None and metrics["candidate_positions"] != len(selected_rows) * 10:
        print(
            f"candidate position mismatch: actual={metrics['candidate_positions']} expected={len(selected_rows) * 10}",
        )
        return 1
    if milvus_audit is not None and not milvus_audit["ok"]:
        for error in milvus_audit["mapping_errors"]:
            print(f"Milvus mapping error: {error}")
        for item in milvus_audit["collections"]:
            if not item["ok"]:
                print(
                    f"Milvus collection failed: {item['collection_name']} "
                    f"foreign={item['foreign_path_count']} duplicates={item['duplicate_chunk_count']} errors={item['errors']}"
                )
        return 1
    if metrics is not None:
        write_metrics(
            metrics,
            args.metrics_json.resolve() if args.metrics_json else None,
            args.metrics_md.resolve() if args.metrics_md else None,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
