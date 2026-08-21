from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from localization_engine.indexer import collection_identity
from merge_localization_outputs import normalize_relative_path, normalized_candidates, validate_source_result


class MergeIsolationTest(unittest.TestCase):
    def test_foreign_absolute_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            run01 = base / "run01" / "repo"
            run03 = base / "run03" / "repo"
            run01.mkdir(parents=True)
            run03.mkdir(parents=True)
            candidate = {"file_path": str(run03 / "Index.ets"), "relative_path": str(run03 / "Index.ets")}
            with self.assertRaisesRegex(ValueError, "outside result repo_root"):
                normalize_relative_path(candidate, repo_root=str(run01))

    def test_duplicate_relative_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            repo = base / "repo"
            row_dir = base / "row_000001"
            repo.mkdir()
            row_dir.mkdir()
            candidates = [
                {"rank": 1, "file_path": str(repo / "Index.ets"), "relative_path": "Index.ets", "score": 0.8},
                {"rank": 2, "file_path": str(repo / "index.ets"), "relative_path": "index.ets", "score": 0.7},
            ]
            (row_dir / "embedding_candidates.jsonl").write_text(
                "".join(json.dumps(item) + "\n" for item in candidates),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate candidate path"):
                normalized_candidates({"row": 1, "repo_root": str(repo)}, row_dir, source_run_id="run")

    def test_remote_worker_hostname_is_validated_from_result(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root) / "repo"
            repo.mkdir()
            identity = collection_identity("codephoenix", repo, hostname="remote-pc")
            dataset = {"instance_id": "org__repo+sha-1", "base": {"sha": "abc"}}
            result = {
                "row": 1,
                "instance_id": dataset["instance_id"],
                "base_sha": "abc",
                "repo_head_after_reset": "abc",
                "repo_root": str(repo),
                "collection_name": identity.collection_name,
                "collection_hostname": identity.collection_hostname,
                "collection_namespace_hash": identity.collection_namespace_hash,
                "collection_repo_root": identity.collection_repo_root,
            }
            validate_source_result(dataset, result)


if __name__ == "__main__":
    unittest.main()
