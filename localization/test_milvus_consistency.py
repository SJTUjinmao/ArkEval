from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from localization_engine.milvus.client import MilvusStore


class MilvusConsistencyTest(unittest.TestCase):
    def test_search_uses_strong_consistency(self) -> None:
        collection = MagicMock()
        collection.search.return_value = [[]]
        with patch("localization_engine.milvus.client.Collection", return_value=collection):
            store = MilvusStore(host="127.0.0.1", port=19530)
            self.assertEqual(store.search(collection_name="test", query_vector=[0.0], top_k=10), [])
        self.assertEqual(collection.search.call_args.kwargs["consistency_level"], "Strong")

    def test_delete_lookup_uses_strong_consistency(self) -> None:
        collection = MagicMock()
        collection.query.return_value = []
        with patch("localization_engine.milvus.client.Collection", return_value=collection):
            store = MilvusStore(host="127.0.0.1", port=19530)
            store.delete_by_file_path(collection_name="test", file_path="E:/repo/Index.ets")
        self.assertEqual(collection.query.call_args.kwargs["consistency_level"], "Strong")

    def test_bulk_delete_reads_once_and_deletes_primary_keys_in_batches(self) -> None:
        collection = MagicMock()
        rows = [
            {"id": index, "file_path": "E:/repo/changed.ets"}
            for index in range(1, 1002)
        ]
        collection.query.return_value = rows
        with patch("localization_engine.milvus.client.Collection", return_value=collection):
            store = MilvusStore(host="127.0.0.1", port=19530)
            deleted = store.delete_by_file_paths(
                collection_name="test",
                file_paths=["E:/repo/changed.ets", "E:/repo/missing.ets"],
            )
        self.assertEqual(deleted, 1001)
        self.assertEqual(collection.query.call_count, 1)
        self.assertEqual(collection.delete.call_count, 3)
        for call in collection.delete.call_args_list:
            ids = call.kwargs["expr"].removeprefix("id in [").removesuffix("]").split(",")
            self.assertLessEqual(len(ids), 500)

    def test_bulk_delete_uses_all_exactly_queried_visible_rows(self) -> None:
        store = MilvusStore(host="127.0.0.1", port=19530)
        visible = [
            {"id": index, "file_path": "E:/repo/changed.ets"}
            for index in range(1, 17002)
        ]
        collection = MagicMock()
        with (
            patch.object(MilvusStore, "_get_chunks_for_file_paths", return_value=visible) as get_visible,
            patch("localization_engine.milvus.client.Collection", return_value=collection),
        ):
            deleted = store.delete_by_file_paths(
                collection_name="test",
                file_paths=["E:/repo/changed.ets"],
            )
        self.assertEqual(deleted, 17001)
        get_visible.assert_called_once_with(
            collection_name="test",
            file_paths=["E:/repo/changed.ets"],
        )
        self.assertEqual(collection.delete.call_count, 35)

    def test_bulk_delete_empty_paths_does_not_touch_milvus(self) -> None:
        with patch("localization_engine.milvus.client.Collection") as collection:
            store = MilvusStore(host="127.0.0.1", port=19530)
            self.assertEqual(store.delete_by_file_paths(collection_name="test", file_paths=[]), 0)
        collection.assert_not_called()

    def test_bulk_delete_matches_special_paths_exactly(self) -> None:
        special = 'E:\\repo\\中"文.ets'
        visible = [
            {"id": 1, "file_path": special},
            {"id": 2, "file_path": "E:/repo/index.ets"},
            {"id": 3, "file_path": "E:/repo/INDEX.ets"},
        ]
        store = MilvusStore(host="127.0.0.1", port=19530)
        collection = MagicMock()
        collection.query.return_value = visible[:2]
        with patch("localization_engine.milvus.client.Collection", return_value=collection):
            deleted = store.delete_by_file_paths(
                collection_name="test",
                file_paths=[special, special, "E:/repo/index.ets"],
            )
        self.assertEqual(deleted, 2)
        collection.delete.assert_called_once_with(expr="id in [1,2]")

    def test_wait_for_deleted_paths_rechecks_until_absent(self) -> None:
        collection = MagicMock()
        collection.query.side_effect = [
            [{"id": 1, "file_path": "E:/repo/old.ets"}],
            [],
            [],
        ]
        with (
            patch("localization_engine.milvus.client.Collection", return_value=collection),
            patch("localization_engine.milvus.client.time.sleep"),
        ):
            store = MilvusStore(host="127.0.0.1", port=19530)
            store.wait_for_file_paths_absent(collection_name="test", file_paths=["E:/repo/old.ets"])
        self.assertEqual(collection.query.call_count, 2)
        self.assertEqual(collection.query.call_args.kwargs["consistency_level"], "Strong")

    def test_wait_for_deleted_paths_only_rechecks_remaining_paths(self) -> None:
        store = MilvusStore(host="127.0.0.1", port=19530)
        with (
            patch.object(
                MilvusStore,
                "_get_chunks_for_file_paths",
                side_effect=[
                    [{"id": 1, "file_path": "a"}, {"id": 2, "file_path": "b"}],
                    [{"id": 2, "file_path": "b"}],
                    [],
                ],
            ) as visible,
            patch("localization_engine.milvus.client.Collection", return_value=MagicMock()),
            patch("localization_engine.milvus.client.time.sleep"),
        ):
            store.wait_for_file_paths_absent(collection_name="test", file_paths=["a", "b", "b"])
        self.assertEqual(visible.call_count, 3)

    def test_wait_for_deleted_paths_times_out_with_actual_paths(self) -> None:
        store = MilvusStore(host="127.0.0.1", port=19530)
        with (
            patch.object(
                MilvusStore,
                "_get_chunks_for_file_paths",
                return_value=[{"id": 1, "file_path": "still-there"}],
            ),
            patch("localization_engine.milvus.client.time.monotonic", return_value=0.0),
        ):
            with self.assertRaisesRegex(TimeoutError, "still-there"):
                store.wait_for_file_paths_absent(
                    collection_name="test",
                    file_paths=["still-there", "already-gone"],
                    timeout_seconds=0.0,
                )

    def test_wait_for_deleted_paths_redeletes_late_visible_ids(self) -> None:
        store = MilvusStore(host="127.0.0.1", port=19530)
        collection = MagicMock()
        with (
            patch.object(
                MilvusStore,
                "_get_chunks_for_file_paths",
                side_effect=[
                    [{"id": 11, "file_path": "old"}],
                    [{"id": 12, "file_path": "old"}],
                    [],
                ],
            ),
            patch("localization_engine.milvus.client.Collection", return_value=collection),
            patch("localization_engine.milvus.client.time.sleep"),
        ):
            store.wait_for_file_paths_absent(collection_name="test", file_paths=["old"])
        self.assertEqual(collection.delete.call_count, 2)
        self.assertEqual(collection.delete.call_args_list[0].kwargs["expr"], "id in [11]")
        self.assertEqual(collection.delete.call_args_list[1].kwargs["expr"], "id in [12]")
        self.assertEqual(collection.flush.call_count, 2)

    def test_exact_path_lookup_is_case_and_separator_insensitive(self) -> None:
        store = MilvusStore(host="127.0.0.1", port=19530)
        visible = [
            {"id": 1, "file_path": "E:\\Repo\\Dir\\Index.ets"},
            {"id": 2, "file_path": "E:\\Repo\\Other.ets"},
        ]
        with patch.object(MilvusStore, "get_visible_chunks", return_value=visible):
            rows = store._get_chunks_for_file_paths(
                collection_name="test",
                file_paths=["e:/repo/dir/./index.ets"],
            )
        self.assertEqual([row["id"] for row in rows], [1])

    def test_visible_chunk_audit_uses_strong_consistency(self) -> None:
        collection = MagicMock()
        collection.query.return_value = []
        with patch("localization_engine.milvus.client.Collection", return_value=collection):
            store = MilvusStore(host="127.0.0.1", port=19530)
            self.assertEqual(store.get_visible_chunks(collection_name="test"), [])
        self.assertEqual(collection.query.call_args.kwargs["consistency_level"], "Strong")
        self.assertIn("chunk_hash", collection.query.call_args.kwargs["output_fields"])

    def test_visible_chunk_audit_paginates_by_primary_key(self) -> None:
        collection = MagicMock()
        all_rows = [{"id": 3}, {"id": 2}, {"id": 1}]

        def query(*, expr: str, limit: int, **_kwargs):
            lower, upper = [int(value) for value in expr.replace("id >= ", "").replace("id <= ", "").split(" and ")]
            return [row for row in all_rows if lower <= row["id"] <= upper][:limit]

        collection.query.side_effect = query
        with patch("localization_engine.milvus.client.Collection", return_value=collection):
            store = MilvusStore(host="127.0.0.1", port=19530)
            rows = store.get_visible_chunks(collection_name="test", page_size=2)
        self.assertEqual([row["id"] for row in rows], [1, 2, 3])
        self.assertGreater(collection.query.call_count, 1)

    def test_visible_chunk_audit_paginates_beyond_16384_rows(self) -> None:
        collection = MagicMock()
        all_rows = [{"id": index} for index in range(1, 17002)]

        def query(*, expr: str, limit: int, **_kwargs):
            lower, upper = [int(value) for value in expr.replace("id >= ", "").replace("id <= ", "").split(" and ")]
            return [row for row in all_rows if lower <= row["id"] <= upper][:limit]

        collection.query.side_effect = query
        with patch("localization_engine.milvus.client.Collection", return_value=collection):
            store = MilvusStore(host="127.0.0.1", port=19530)
            rows = store.get_visible_chunks(collection_name="test", page_size=10000)
        self.assertEqual(len(rows), 17001)
        self.assertEqual(rows[0]["id"], 1)
        self.assertEqual(rows[-1]["id"], 17001)
        self.assertGreater(collection.query.call_count, 1)

    def test_visible_chunk_audit_accepts_complete_short_range(self) -> None:
        collection = MagicMock()
        collection.query.return_value = [{"id": 1}, {"id": 2}]
        with patch("localization_engine.milvus.client.Collection", return_value=collection):
            store = MilvusStore(host="127.0.0.1", port=19530)
            rows = store.get_visible_chunks(collection_name="test", page_size=3)
        self.assertEqual([row["id"] for row in rows], [1, 2])
        self.assertEqual(collection.query.call_count, 1)


if __name__ == "__main__":
    unittest.main()
