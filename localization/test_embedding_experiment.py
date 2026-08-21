from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

from preflight_embedding_experiment import parse_assignments
from validate_embedding_isolation import calculate_metrics, path_is_within, safe_relative_path
from localization_engine.embedding.cache import EmbeddingCache
from localization_engine.embedding.clients import DgxEmbeddingClient, DistributedDgxEmbeddingClient
from run_localization import validate_reuse_candidate_source, validate_success_artifacts


class EmbeddingExperimentTest(unittest.TestCase):
    def test_assignments_require_exact_coverage_and_worker_order(self) -> None:
        values = [f"run{index:02d}={index}" for index in range(1, 11)]
        assignments = parse_assignments(values, max_row=10)
        self.assertEqual(list(assignments), [f"run{index:02d}" for index in range(1, 11)])
        with self.assertRaisesRegex(ValueError, "coverage mismatch"):
            parse_assignments(values[:-1] + ["run10=9"], max_row=10)

    def test_candidate_absolute_and_relative_paths_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root) / "repo"
            foreign = Path(root) / "foreign"
            repo.mkdir()
            foreign.mkdir()
            candidate = {"relative_path": "Index.ets", "file_path": str(foreign / "Index.ets")}
            with self.assertRaisesRegex(ValueError, "outside repo_root"):
                safe_relative_path(candidate, repo.resolve())

    def test_candidate_file_path_must_be_absolute_and_present(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root).resolve()
            with self.assertRaisesRegex(ValueError, "file_path is empty"):
                safe_relative_path({"relative_path": "Index.ets"}, repo)
            with self.assertRaisesRegex(ValueError, "not absolute"):
                safe_relative_path({"relative_path": "Index.ets", "file_path": "Index.ets"}, repo)

    def test_metrics_are_case_insensitive_and_count_all_defect_files(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            output = Path(root)
            rows_root = output / "rows"
            row_dir = rows_root / "row_000001"
            repo = output / "repo"
            row_dir.mkdir(parents=True)
            repo.mkdir()
            (row_dir / "result.json").write_text(json.dumps({"repo_root": str(repo)}), encoding="utf-8")
            candidates = [
                {"rank": 1, "relative_path": "SRC/One.ets", "file_path": str(repo / "SRC" / "One.ets")},
                {"rank": 2, "relative_path": "src/two.ets", "file_path": str(repo / "src" / "two.ets")},
            ]
            (row_dir / "embedding_candidates.jsonl").write_text(
                "".join(json.dumps(item) + "\n" for item in candidates),
                encoding="utf-8",
            )
            metrics = calculate_metrics(
                [{"defect_files": ["src/one.ets", "SRC/TWO.ETS"]}],
                [1],
                rows_root,
            )
            self.assertEqual(metrics["issue_any_hit"]["issues"], 1)
            self.assertEqual(metrics["issue_all_hit"]["issues"], 1)
            self.assertEqual(metrics["file_recall"]["hit_files"], 2)
            self.assertEqual(metrics["mrr"], 1.0)

    def test_path_is_within_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root) / "run01" / "repo"
            foreign = Path(root) / "run03" / "repo" / "Index.ets"
            self.assertTrue(path_is_within(str(repo / "Index.ets"), repo))
            self.assertFalse(path_is_within(str(foreign), repo))

    def test_corrupt_embedding_cache_is_treated_as_miss(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            cache = EmbeddingCache(Path(root))
            cache.put(model_name="model", chunk_hash="abc", vector=[1.0], dim=1, signature="sig")
            path = cache._path("model", "abc", "sig")
            path.write_text("{", encoding="utf-8")
            self.assertIsNone(cache.get(model_name="model", chunk_hash="abc", signature="sig"))
            self.assertFalse(path.exists())

    def test_embedding_response_max_length_must_match_request(self) -> None:
        client = DgxEmbeddingClient(
            base_url="http://127.0.0.1:8008",
            model_name="Qwen/Qwen3-Embedding-8B",
            max_length=1024,
        )
        payload = {"model": "Qwen/Qwen3-Embedding-8B", "dim": 4096, "count": 1, "max_length": 256}
        with self.assertRaisesRegex(RuntimeError, "max_length"):
            client._validate_response_metadata(payload, expected_count=1)

    def test_dgx_request_timeout_is_capped_by_pool_deadline(self) -> None:
        client = DgxEmbeddingClient(
            base_url="http://127.0.0.1:8008",
            model_name="Qwen/Qwen3-Embedding-8B",
            max_retries=1,
        )
        with patch(
            "localization_engine.embedding.clients.requests.post",
            side_effect=ConnectionError("offline"),
        ) as post:
            with self.assertRaisesRegex(RuntimeError, "offline"):
                client.embed_texts(["query"], request_timeout_seconds=3.0)
        self.assertEqual(post.call_args.kwargs["timeout"], 3.0)

    def test_distributed_embedding_waits_for_endpoint_recovery(self) -> None:
        endpoint = MagicMock()
        endpoint.name = "endpoint-1"
        endpoint.weight = 1
        endpoint.embed_texts.side_effect = [ConnectionError("offline"), [[1.0]]]
        client = DistributedDgxEmbeddingClient(
            [endpoint],
            outage_grace_seconds=10.0,
            retry_interval_seconds=0.0,
        )
        with patch("localization_engine.embedding.clients.time.sleep") as sleep:
            self.assertEqual(client.embed_texts(["query"]), [[1.0]])
        self.assertEqual(endpoint.embed_texts.call_count, 2)
        sleep.assert_called_once_with(0.0)

    def test_distributed_embedding_fails_at_pool_deadline(self) -> None:
        endpoint = MagicMock()
        endpoint.name = "endpoint-1"
        endpoint.weight = 1
        endpoint.embed_texts.side_effect = ConnectionError("offline")
        client = DistributedDgxEmbeddingClient(
            [endpoint],
            outage_grace_seconds=1.0,
            retry_interval_seconds=0.1,
        )
        clock = iter([0.0, 0.0, 0.0, 0.0, 1.0])
        with patch("localization_engine.embedding.clients.time.monotonic", side_effect=lambda: next(clock)):
            with self.assertRaisesRegex(RuntimeError, "1 round"):
                client.embed_texts(["query"])

    def test_reuse_source_must_match_dataset_identity(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            row_dir = Path(root) / "row_000001"
            row_dir.mkdir()
            candidates = row_dir / "embedding_candidates.jsonl"
            candidates.write_text("", encoding="utf-8")
            (row_dir / "result.json").write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "row": 1,
                        "instance_id": "wrong",
                        "base_sha": "abc",
                        "repo": "repo",
                    }
                ),
                encoding="utf-8",
            )
            record = {"instance_id": "expected", "repo": "repo", "base": {"sha": "abc"}}
            with self.assertRaisesRegex(RuntimeError, "instance_id mismatch"):
                validate_reuse_candidate_source(record, row=1, candidates_path=candidates)

    def test_llm_success_requires_complete_stage_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            repo = base / "repo"
            row_dir = base / "row_000001"
            repo.mkdir()
            row_dir.mkdir()
            candidate_one = repo / "Index.ets"
            candidate_two = repo / "Model.ets"
            dependency = repo / "Dependency.ets"
            for path in (candidate_one, candidate_two, dependency):
                path.write_text("export const value = 1", encoding="utf-8")

            def write_jsonl(name: str, records: list[dict]) -> Path:
                path = row_dir / name
                path.write_text(
                    "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
                    encoding="utf-8",
                )
                return path

            candidates = write_jsonl(
                "embedding_candidates.jsonl",
                [
                    {"rank": 1, "file_path": str(candidate_one), "relative_path": "Index.ets"},
                    {"rank": 2, "file_path": str(candidate_two), "relative_path": "Model.ets"},
                ],
            )
            core = write_jsonl(
                "llm_core_files.jsonl",
                [
                    {
                        "rank": 1,
                        "file_path": str(candidate_one),
                        "relative_path": "Index.ets",
                        "model": "kimi-k2.7-code",
                    }
                ],
            )
            deps = write_jsonl(
                "llm_dep_expansion_files.jsonl",
                [
                    {
                        "rank": 1,
                        "file_path": str(dependency),
                        "relative_path": "Dependency.ets",
                        "model": "kimi-k2.7-code",
                    }
                ],
            )
            row_trace = write_jsonl(
                "row_trace.jsonl",
                [
                    {"stage": "llm_filter_done"},
                    {"stage": "ast_dependency_analysis_done"},
                    {"stage": "llm_dep_expansion_done"},
                ],
            )
            llm_trace = write_jsonl(
                "llm_trace.jsonl",
                [{"stage": "llm_filter"}, {"stage": "llm_dep_expansion"}],
            )
            validate_success_artifacts(
                repo_root=repo,
                top_k_files=2,
                no_llm_filter=False,
                no_dep_expansion=False,
                llm_model="kimi-k2.7-code",
                embedding_candidates_path=candidates,
                llm_core_files_path=core,
                llm_dep_files_path=deps,
                row_trace_path=row_trace,
                llm_trace_path=llm_trace,
                absolute_paths=[str(candidate_one.resolve()), str(dependency.resolve())],
                relative_paths=["Index.ets", "Dependency.ets"],
            )

            row_trace.write_text(json.dumps({"stage": "llm_filter_done"}) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "AST dependency analysis"):
                validate_success_artifacts(
                    repo_root=repo,
                    top_k_files=2,
                    no_llm_filter=False,
                    no_dep_expansion=False,
                    llm_model="kimi-k2.7-code",
                    embedding_candidates_path=candidates,
                    llm_core_files_path=core,
                    llm_dep_files_path=deps,
                    row_trace_path=row_trace,
                    llm_trace_path=llm_trace,
                    absolute_paths=[str(candidate_one.resolve()), str(dependency.resolve())],
                    relative_paths=["Index.ets", "Dependency.ets"],
                )


if __name__ == "__main__":
    unittest.main()
