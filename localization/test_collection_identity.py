from __future__ import annotations

import json
import subprocess
import unittest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from localization_engine.config import load_config
from localization_engine.indexer import (
    CollectionIntegrityError,
    _audit_collection,
    _chunk_manifest_meta,
    _delete_changed_paths,
    _manifest_file_stats,
    _manifest_reusable,
    _resolve_index_mode,
    _saved_merkle_root,
    _stream_chunk_manifest,
    collection_identity,
)
from localization_engine.locate_flow import LocalizationRetrievalError
from run_localization import (
    GIT_MUTATION_TIMEOUT_SECONDS,
    GIT_READ_TIMEOUT_SECONDS,
    ensure_indexed,
    is_milvus_unavailable,
    locate_files,
    acquire_repo_worker_locks,
    is_case_collision_false_dirty,
    reset_repo_to_base,
    resolve_repo_root,
    run_git,
    status_without_codephoenix,
)


class CollectionIdentityTest(unittest.TestCase):
    def test_identity_is_stable_for_same_host_and_repo(self) -> None:
        repo = Path("E:/WorkApp/arkeval/depend/repair_repo/run01/applications_app_samples")
        first = collection_identity("codephoenix", repo, hostname="pc-a")
        second = collection_identity("codephoenix", repo, hostname="pc-a")
        self.assertEqual(first, second)

    def test_slots_and_hosts_are_isolated(self) -> None:
        run01 = Path("E:/WorkApp/arkeval/depend/repair_repo/run01/applications_app_samples")
        run02 = Path("E:/WorkApp/arkeval/depend/repair_repo/run02/applications_app_samples")
        names = {
            collection_identity("codephoenix", run01, hostname="pc-a").collection_name,
            collection_identity("codephoenix", run02, hostname="pc-a").collection_name,
            collection_identity("codephoenix", run01, hostname="pc-b").collection_name,
        }
        self.assertEqual(len(names), 3)

    def test_collection_name_is_milvus_safe(self) -> None:
        identity = collection_identity("1 bad-prefix", "E:/repo/name-with-dashes", hostname="pc-a")
        self.assertRegex(identity.collection_name, r"^[A-Za-z_][0-9A-Za-z_]*$")
        self.assertLess(len(identity.collection_name), 255)

    def test_missing_collection_forces_full_even_with_old_merkle(self) -> None:
        self.assertEqual(
            _resolve_index_mode(
                dry_run=False,
                collection_exists=False,
                collection_count=None,
                force_full=False,
                old_merkle_exists=True,
                reusable_state=True,
            ),
            (True, "collection_missing_full_rebuild"),
        )

    def test_populated_collection_uses_incremental(self) -> None:
        self.assertEqual(
            _resolve_index_mode(
                dry_run=False,
                collection_exists=True,
                collection_count=100,
                force_full=False,
                old_merkle_exists=True,
                reusable_state=True,
            ),
            (False, "collection_and_state_reusable_incremental"),
        )

    def test_force_full_overrides_populated_collection(self) -> None:
        self.assertEqual(
            _resolve_index_mode(
                dry_run=False,
                collection_exists=True,
                collection_count=100,
                force_full=True,
                old_merkle_exists=True,
                reusable_state=True,
            ),
            (True, "force_index_requested"),
        )

    def test_incomplete_state_forces_full_rebuild(self) -> None:
        self.assertEqual(
            _resolve_index_mode(
                dry_run=False,
                collection_exists=True,
                collection_count=100,
                force_full=False,
                old_merkle_exists=True,
                reusable_state=False,
            ),
            (True, "index_state_not_reusable_full_rebuild"),
        )

    def test_repo_name_cannot_escape_assigned_pool(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            pool = Path(root) / "run01"
            pool.mkdir()
            with self.assertRaisesRegex(ValueError, "invalid dataset repo name"):
                resolve_repo_root({"repo": "../run03/repo"}, repo_root=None, repo_pool=pool)

    def test_same_repo_worker_conflict_fails_without_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            first = acquire_repo_worker_locks(repo)
            try:
                with self.assertRaisesRegex(RuntimeError, "assign this row to another repo pool"):
                    acquire_repo_worker_locks(repo)
            finally:
                for lock in reversed(first):
                    lock.release()

    def test_base_reset_allows_parallel_disk_contention(self) -> None:
        calls: list[tuple[list[str], int]] = []

        def fake_run_git(_repo: Path, args: list[str], *, timeout: int = 120) -> str:
            calls.append((args, timeout))
            if args == ["rev-parse", "--is-inside-work-tree"]:
                return "true"
            if args[:2] == ["rev-parse", "--verify"] or args == ["rev-parse", "HEAD"]:
                return "abc"
            return ""

        with tempfile.TemporaryDirectory() as root:
            with (
                patch("run_localization.run_git", side_effect=fake_run_git),
                patch("run_localization.status_without_codephoenix", return_value=""),
            ):
                self.assertEqual(reset_repo_to_base(Path(root), "abc"), "abc")
        reset_call = next(item for item in calls if item[0][:2] == ["reset", "--hard"])
        self.assertEqual(reset_call[1], GIT_MUTATION_TIMEOUT_SECONDS)

    def test_index_retries_milvus_outage_with_full_rebuild(self) -> None:
        outage = RuntimeError("proxy not healthy")
        with (
            patch("localization_engine.indexer.index_repo", side_effect=[outage, None]) as index,
            patch("run_localization.time.sleep") as sleep,
            patch("run_localization.record_milvus_retry") as record,
        ):
            ensure_indexed(Path("E:/repo"), force_index=False)
        self.assertEqual([call.kwargs["full"] for call in index.call_args_list], [False, True])
        sleep.assert_called_once()
        record.assert_called_once()

    def test_non_milvus_index_error_does_not_retry(self) -> None:
        with (
            patch("localization_engine.indexer.index_repo", side_effect=RuntimeError("invalid manifest")) as index,
            self.assertRaisesRegex(RuntimeError, "invalid manifest"),
        ):
            ensure_indexed(Path("E:/repo"), force_index=False)
        index.assert_called_once()

    def test_collection_integrity_error_retries_once_with_full_rebuild(self) -> None:
        mismatch = CollectionIntegrityError("Milvus collection does not match current manifest")
        with (
            patch("localization_engine.indexer.index_repo", side_effect=[mismatch, None]) as index,
            patch("run_localization.record_collection_integrity_rebuild") as record,
        ):
            ensure_indexed(Path("E:/repo"), force_index=False)
        self.assertEqual([call.kwargs["full"] for call in index.call_args_list], [False, True])
        record.assert_called_once_with(error=mismatch)

    def test_collection_integrity_error_during_full_rebuild_does_not_loop(self) -> None:
        mismatch = CollectionIntegrityError("Milvus collection does not match current manifest")
        with (
            patch("localization_engine.indexer.index_repo", side_effect=[mismatch, mismatch]) as index,
            patch("run_localization.record_collection_integrity_rebuild"),
            self.assertRaisesRegex(RuntimeError, "index full rebuild failed"),
        ):
            ensure_indexed(Path("E:/repo"), force_index=False)
        self.assertEqual([call.kwargs["full"] for call in index.call_args_list], [False, True])

    def test_milvus_unavailable_classifier_is_narrow(self) -> None:
        self.assertTrue(is_milvus_unavailable(RuntimeError("Fail connecting to server on 127.0.0.1:19530")))
        self.assertTrue(is_milvus_unavailable(RuntimeError("proxy not healthy")))
        self.assertTrue(
            is_milvus_unavailable(
                RuntimeError("find no available datacoord, check datacoord state")
            )
        )
        self.assertTrue(
            is_milvus_unavailable(
                RuntimeError("find no available indexcoord, check indexcoord state")
            )
        )
        self.assertTrue(
            is_milvus_unavailable(
                RuntimeError("role datacoord[nodeID: 101] is not serving, reason: sate code: Abnormal")
            )
        )
        self.assertTrue(
            is_milvus_unavailable(
                RuntimeError("failed to flush segment, etcdserver: request timed out")
            )
        )
        self.assertFalse(is_milvus_unavailable(RuntimeError("invalid datacoord state")))
        self.assertFalse(is_milvus_unavailable(RuntimeError("index metadata mismatch")))
        self.assertFalse(is_milvus_unavailable(RuntimeError("duplicate chunk")))

    def test_search_retries_milvus_outage(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            paths = [Path(root) / name for name in ("embedding.jsonl", "core.jsonl", "dep.jsonl")]
            for path in paths:
                path.write_text("partial", encoding="utf-8")
            with (
                patch(
                    "run_localization._locate_files_once",
                    side_effect=[LocalizationRetrievalError("proxy not healthy"), ["ok"]],
                ) as locate,
                patch("run_localization.time.sleep") as sleep,
                patch("run_localization.record_milvus_retry") as record,
            ):
                result = locate_files(
                    Path(root),
                    "query",
                    top_k_files=10,
                    top_k_hits=None,
                    no_llm_filter=True,
                    no_dep_expansion=True,
                    raw_scores=False,
                    embedding_candidates_path=paths[0],
                    llm_core_files_path=paths[1],
                    llm_dep_files_path=paths[2],
                    reuse_embedding_candidates_path=None,
                )
            self.assertEqual(result, ["ok"])
            self.assertEqual(locate.call_count, 2)
            self.assertFalse(paths[0].exists())
            self.assertEqual(paths[1].read_text(encoding="utf-8"), "partial")
            self.assertEqual(paths[2].read_text(encoding="utf-8"), "partial")
            sleep.assert_called_once()
            record.assert_called_once()

    def test_llm_connection_error_is_not_milvus_retry(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            paths = [Path(root) / name for name in ("embedding.jsonl", "core.jsonl", "dep.jsonl")]
            with (
                patch(
                    "run_localization._locate_files_once",
                    side_effect=RuntimeError("LLM API server unavailable"),
                ) as locate,
                patch("run_localization.time.sleep") as sleep,
                patch("run_localization.record_milvus_retry") as record,
            ):
                with self.assertRaisesRegex(RuntimeError, "LLM API"):
                    locate_files(
                        Path(root),
                        "query",
                        top_k_files=10,
                        top_k_hits=None,
                        no_llm_filter=False,
                        no_dep_expansion=False,
                        raw_scores=False,
                        embedding_candidates_path=paths[0],
                        llm_core_files_path=paths[1],
                        llm_dep_files_path=paths[2],
                        reuse_embedding_candidates_path=Path(root) / "reuse.jsonl",
                    )
            locate.assert_called_once()
            sleep.assert_not_called()
            record.assert_not_called()

    def test_status_excludes_codephoenix_and_retries_timeout(self) -> None:
        timeout = subprocess.TimeoutExpired(["git", "status"], GIT_READ_TIMEOUT_SECONDS)
        with (
            patch("run_localization.run_git", side_effect=[timeout, "?? ordinary.txt"]) as run,
            patch("run_localization.time.sleep") as sleep,
        ):
            self.assertEqual(status_without_codephoenix(Path("E:/repo")), "?? ordinary.txt")
        self.assertEqual(run.call_count, 2)
        args = run.call_args.args[1]
        self.assertIn(":(top,exclude).codephoenix", args)
        self.assertIn(":(top,exclude).codephoenix/**", args)
        self.assertEqual(run.call_args.kwargs["timeout"], GIT_READ_TIMEOUT_SECONDS)
        sleep.assert_called_once_with(2)

    def test_status_second_timeout_stays_fail_closed(self) -> None:
        timeout = subprocess.TimeoutExpired(["git", "status"], GIT_READ_TIMEOUT_SECONDS)
        with (
            patch("run_localization.run_git", side_effect=[timeout, timeout]),
            patch("run_localization.time.sleep"),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            status_without_codephoenix(Path("E:/repo"))

    def test_run_git_preserves_porcelain_leading_status_space(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["git", "status"],
            returncode=0,
            stdout=" M File.har\r\n",
            stderr="",
        )
        with patch("run_localization.subprocess.run", return_value=completed):
            self.assertEqual(run_git(Path("E:/repo"), ["status"]), " M File.har")

    def test_status_ignores_only_unstaged_false_dirty(self) -> None:
        with (
            patch("run_localization.run_git", return_value=" M File.har"),
            patch("run_localization.is_case_collision_false_dirty", return_value=True) as false_dirty,
        ):
            self.assertEqual(status_without_codephoenix(Path("E:/repo")), "")
        false_dirty.assert_called_once_with(Path("E:/repo"), "File.har")

        with (
            patch("run_localization.run_git", return_value="M  File.har"),
            patch("run_localization.is_case_collision_false_dirty", return_value=True) as false_dirty,
        ):
            self.assertEqual(status_without_codephoenix(Path("E:/repo")), "M  File.har")
        false_dirty.assert_not_called()

    def test_changed_paths_are_absent_before_reinsertion(self) -> None:
        store = MagicMock()
        progress = MagicMock()
        changed = Path("E:/repo/changed.ets")
        removed = "E:/repo/removed.ets"
        store.delete_chunk_ids.return_value = 2
        with (
            patch(
                "localization_engine.indexer._manifest_chunk_keys",
                side_effect=[
                    {(removed, 1, 1, "a"), (str(changed), 1, 1, "b")},
                    set(),
                ],
            ),
            patch(
                "localization_engine.indexer._audit_collection",
                side_effect=[
                    {
                        "visible_chunks": 2,
                        "validated_rows": [
                            {"id": 1, "file_path": removed, "line_start": 1, "line_end": 1, "chunk_hash": "a"},
                            {"id": 2, "file_path": str(changed), "line_start": 1, "line_end": 1, "chunk_hash": "b"},
                        ],
                    },
                    {"visible_chunks": 0},
                ],
            ) as audit,
        ):
            _delete_changed_paths(
                store,
                collection_name="test",
                meta_dir=Path("E:/repo/.codephoenix"),
                removed_paths={removed},
                changed_files=[changed],
                progress=progress,
                mode="incremental",
            )
        store.delete_chunk_ids.assert_called_once_with(
            collection_name="test",
            ids=[1, 2],
        )
        store.flush_collection.assert_called_once_with(collection_name="test")
        store.wait_for_file_paths_absent.assert_called_once_with(
            collection_name="test",
            file_paths=[removed, str(changed)],
        )
        self.assertEqual(audit.call_count, 2)

    def test_changed_path_delete_count_mismatch_fails_closed(self) -> None:
        store = MagicMock()
        path = "E:/repo/changed.ets"
        with (
            patch(
                "localization_engine.indexer._manifest_chunk_keys",
                side_effect=[{(path, 1, 1, "a")}, set()],
            ),
            patch(
                "localization_engine.indexer._audit_collection",
                return_value={"visible_chunks": 1, "validated_rows": []},
            ),
            self.assertRaisesRegex(RuntimeError, "expected=1 actual=0"),
        ):
            _delete_changed_paths(
                store,
                collection_name="test",
                meta_dir=Path("E:/repo/.codephoenix"),
                removed_paths={path},
                changed_files=[],
                progress=MagicMock(),
                mode="incremental",
            )
        store.flush_collection.assert_not_called()
        store.wait_for_file_paths_absent.assert_not_called()
        store.delete_chunk_ids.assert_not_called()

    def test_false_dirty_requires_raw_head_and_index_bytes_to_match(self) -> None:
        def fake_run_git(_repo: Path, args: list[str], *, timeout: int = 120) -> str:
            if args[:2] == ["hash-object", "--no-filters"]:
                return "raw"
            if args == ["ls-files"]:
                return "Dir/File.har"
            if args[0] == "rev-parse":
                return "raw"
            raise AssertionError(args)

        with patch("run_localization.run_git", side_effect=fake_run_git) as run:
            self.assertTrue(is_case_collision_false_dirty(Path("E:/repo"), "dir/file.har"))
        self.assertIn("--no-filters", run.call_args_list[0].args[1])

    def test_false_dirty_fails_closed_when_index_differs(self) -> None:
        def fake_run_git(_repo: Path, args: list[str], *, timeout: int = 120) -> str:
            if args[:2] == ["hash-object", "--no-filters"]:
                return "raw"
            if args == ["ls-files"]:
                return "File.har"
            if args[1].startswith("HEAD:"):
                return "raw"
            if args[1].startswith(":"):
                return "staged"
            raise AssertionError(args)

        with patch("run_localization.run_git", side_effect=fake_run_git):
            self.assertFalse(is_case_collision_false_dirty(Path("E:/repo"), "File.har"))

    def test_collection_audit_rejects_duplicate_visible_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            meta = Path(root)
            payload = {
                "file_path": str(meta / "Index.ets"),
                "line_start": 1,
                "line_end": 1,
                "chunk_hash": "abc",
                "text": "source",
            }
            (meta / "chunks_manifest.jsonl").write_text(
                json.dumps(payload) + "\n",
                encoding="utf-8",
            )
            row = {key: payload[key] for key in ("file_path", "line_start", "line_end", "chunk_hash")}
            store = MagicMock()
            store.get_visible_chunks.return_value = [row, row]
            with self.assertRaisesRegex(CollectionIntegrityError, "duplicates=1"):
                _audit_collection(store, collection_name="test", meta_dir=meta)

    def test_collection_audit_refreshes_and_retries_missing_only(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            meta = Path(root)
            payload = {
                "file_path": str(meta / "Index.ets"),
                "line_start": 1,
                "line_end": 1,
                "chunk_hash": "abc",
                "text": "source",
            }
            (meta / "chunks_manifest.jsonl").write_text(
                json.dumps(payload) + "\n",
                encoding="utf-8",
            )
            row = {key: payload[key] for key in ("file_path", "line_start", "line_end", "chunk_hash")}
            store = MagicMock()
            store.get_visible_chunks.side_effect = [[], [row]]
            with patch("localization_engine.indexer.time.sleep"):
                audit = _audit_collection(
                    store,
                    collection_name="test",
                    meta_dir=meta,
                    missing_retry_seconds=10.0,
                )
            self.assertTrue(audit["ok"])
            store.refresh_collection.assert_called_once_with(collection_name="test")

    def test_collection_audit_missing_timeout_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            meta = Path(root)
            payload = {
                "file_path": str(meta / "Index.ets"),
                "line_start": 1,
                "line_end": 1,
                "chunk_hash": "abc",
                "text": "source",
            }
            (meta / "chunks_manifest.jsonl").write_text(
                json.dumps(payload) + "\n",
                encoding="utf-8",
            )
            store = MagicMock()
            store.get_visible_chunks.return_value = []
            with self.assertRaisesRegex(CollectionIntegrityError, "missing=1"):
                _audit_collection(
                    store,
                    collection_name="test",
                    meta_dir=meta,
                    missing_retry_seconds=0.0,
                )
            store.refresh_collection.assert_not_called()

    def test_truncated_manifest_is_not_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            meta = repo / ".codephoenix"
            meta.mkdir()
            manifest = meta / "chunks_manifest.jsonl"
            manifest.write_text("{\"file_path\":\"one\"}\n{\"file_path\":\"two\"}\n", encoding="utf-8")
            lines, digest = _manifest_file_stats(manifest)
            payload = _chunk_manifest_meta(
                load_config(repo),
                root_hash="root",
                mode="full",
                total_files=2,
                total_chunks=lines,
                manifest_sha256=digest,
            )
            (meta / "chunks_manifest.meta.json").write_text(json.dumps(payload), encoding="utf-8")
            manifest.write_text("{\"file_path\":\"one\"}\n", encoding="utf-8")
            self.assertFalse(_manifest_reusable(meta, load_config(repo), root_hash="root", mode="full"))

    def test_full_manifest_supports_zero_source_files(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            meta = repo / ".codephoenix"
            meta.mkdir()
            chunks = list(
                _stream_chunk_manifest(
                    load_config(repo),
                    meta,
                    files=[],
                    mode="full",
                    root_hash="empty",
                    progress=lambda _payload: None,
                )
            )
            self.assertEqual(chunks, [])
            payload = json.loads((meta / "chunks_manifest.meta.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["total_chunks"], 0)

    def test_saved_merkle_root_does_not_resolve_old_paths_against_new_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            meta = Path(root) / ".codephoenix"
            meta.mkdir()
            (meta / "merkle.json").write_text(
                json.dumps(
                    {
                        "path": root,
                        "hash": "saved-root",
                        "children": [{"path": str(Path(root) / "Index.ets"), "hash": "leaf"}],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(_saved_merkle_root(meta), "saved-root")


if __name__ == "__main__":
    unittest.main()
