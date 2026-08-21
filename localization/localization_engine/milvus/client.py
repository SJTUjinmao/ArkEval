from __future__ import annotations

"""Milvus client wrapper (pymilvus).

External interface (MVP):
- `MilvusStore.ensure_collection(collection_name, dim)`
- `MilvusStore.upsert_chunks(collection_name, chunks, vectors)`
- `MilvusStore.delete_by_file_path(collection_name, file_path)`
- `MilvusStore.search(collection_name, query_vector, top_k)`

Schema fields:
- file_path (VARCHAR)
- line_start (INT64)
- line_end (INT64)
- chunk_hash (VARCHAR)
- vector (FLOAT_VECTOR)
"""

from dataclasses import dataclass
import ntpath
import time
from typing import Iterable

from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    connections,
    utility,
)

from ..types import DistanceMetric, MilvusIndexType, SearchHit


@dataclass(frozen=True)
class MilvusStore:
    host: str
    port: int
    metric: DistanceMetric = "L2"
    index_type: MilvusIndexType = "FLAT"

    def connect(self) -> None:
        connections.connect("default", host=self.host, port=str(self.port))

    def _load_collection(self, col: Collection, *, refresh: bool = False) -> None:
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                col.load(_refresh=refresh)
                return
            except Exception as exc:
                last_error = exc
                try:
                    col.release()
                except Exception:
                    pass
                time.sleep(min(2**attempt, 8))
        assert last_error is not None
        raise last_error

    def ensure_collection(self, *, collection_name: str, dim: int) -> Collection:
        if self.index_type != "FLAT":
            raise ValueError(f"unsupported Milvus index_type: {self.index_type}")
        if utility.has_collection(collection_name):
            col = Collection(collection_name)
            vector_field = next((field for field in col.schema.fields if field.name == "vector"), None)
            if vector_field is None or int(vector_field.params.get("dim", 0)) != dim:
                raise RuntimeError(f"Milvus collection vector schema mismatch: {collection_name}")
            # 检查是否有索引，没有则创建（可能之前创建失败）
            if not col.has_index():
                index_params = {
                    "index_type": self.index_type,
                    "metric_type": self.metric,
                    "params": {},
                }
                col.create_index(field_name="vector", index_params=index_params)
            self._load_collection(col)
            return col

        fields = [
            FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
            FieldSchema(name="file_path", dtype=DataType.VARCHAR, max_length=4096),
            FieldSchema(name="line_start", dtype=DataType.INT64),
            FieldSchema(name="line_end", dtype=DataType.INT64),
            FieldSchema(name="chunk_hash", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=dim),
        ]
        schema = CollectionSchema(fields, description="Localization engine chunks")
        col = Collection(collection_name, schema)

        # Create index (FLAT is the most stable default for MVP)
        index_params = {
            "index_type": self.index_type,
            "metric_type": self.metric,
            "params": {},
        }
        col.create_index(field_name="vector", index_params=index_params)
        self._load_collection(col)
        return col

    def has_collection(self, *, collection_name: str) -> bool:
        """是否存在指定 collection。"""
        return bool(utility.has_collection(collection_name))

    def drop_collection(self, *, collection_name: str) -> None:
        """删除 collection（用于全量重建索引前清空）。"""
        if utility.has_collection(collection_name):
            utility.drop_collection(collection_name)

    @staticmethod
    def normalize_file_path(file_path: str) -> str:
        return ntpath.normcase(ntpath.normpath(str(file_path)))

    def _get_chunks_for_file_paths(
        self,
        *,
        collection_name: str,
        file_paths: Iterable[str],
    ) -> list[dict]:
        """Find exact Windows paths client-side; Milvus 2.2 cannot match backslashes."""
        target_keys = {self.normalize_file_path(path) for path in file_paths}
        if not target_keys:
            return []
        return [
            row
            for row in self.get_visible_chunks(collection_name=collection_name)
            if self.normalize_file_path(str(row.get("file_path") or "")) in target_keys
        ]

    @staticmethod
    def _delete_ids(col: Collection, ids: Iterable[int], *, batch_size: int = 500) -> int:
        unique_ids = sorted(set(int(value) for value in ids))
        for start in range(0, len(unique_ids), batch_size):
            id_list = ",".join(str(value) for value in unique_ids[start : start + batch_size])
            col.delete(expr=f"id in [{id_list}]")
        return len(unique_ids)

    def delete_chunk_ids(self, *, collection_name: str, ids: Iterable[int]) -> int:
        """Delete an already validated snapshot of primary keys."""
        col = Collection(collection_name)
        return self._delete_ids(col, ids)

    def delete_by_file_paths(self, *, collection_name: str, file_paths: Iterable[str]) -> int:
        """Delete all currently visible chunks for the exact file paths by primary key."""
        rows = self._get_chunks_for_file_paths(
            collection_name=collection_name,
            file_paths=file_paths,
        )
        ids = [int(row["id"]) for row in rows]
        if not ids:
            return 0
        col = Collection(collection_name)
        return self._delete_ids(col, ids)

    def delete_by_file_path(self, *, collection_name: str, file_path: str) -> None:
        """Delete all chunks for one file path using primary-key deletes."""
        self.delete_by_file_paths(collection_name=collection_name, file_paths=[file_path])

    def wait_for_file_paths_absent(
        self,
        *,
        collection_name: str,
        file_paths: Iterable[str],
        timeout_seconds: float = 600.0,
    ) -> None:
        remaining = {self.normalize_file_path(path) for path in file_paths}
        if not remaining:
            return
        deadline = time.monotonic() + timeout_seconds
        while remaining:
            rows = self._get_chunks_for_file_paths(
                collection_name=collection_name,
                file_paths=remaining,
            )
            still_present = remaining & {
                self.normalize_file_path(str(row["file_path"])) for row in rows
            }
            if not still_present:
                return
            if time.monotonic() >= deadline:
                remaining_ids = sorted({int(row["id"]) for row in rows})
                raise TimeoutError(
                    "Milvus deletion visibility timed out for "
                    f"{len(still_present)} file path(s), {len(remaining_ids)} chunk(s): "
                    f"paths={sorted(still_present)[:3]}, ids={remaining_ids[:10]}"
                )
            col = Collection(collection_name)
            self._delete_ids(col, (int(row["id"]) for row in rows))
            col.flush()
            remaining = still_present
            time.sleep(2.0)

    def upsert_chunks(
        self,
        *,
        collection_name: str,
        chunks: list[tuple[str, int, int, str]],
        vectors: list[list[float]],
        flush: bool = False,
    ) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("chunks/vectors length mismatch")
        col = Collection(collection_name)
        file_paths = [c[0] for c in chunks]
        line_starts = [c[1] for c in chunks]
        line_ends = [c[2] for c in chunks]
        chunk_hashes = [c[3] for c in chunks]
        entities = [file_paths, line_starts, line_ends, chunk_hashes, vectors]
        col.insert(entities)
        if flush:
            col.flush()

    def flush_collection(self, *, collection_name: str) -> None:
        col = Collection(collection_name)
        col.flush()

    def refresh_collection(self, *, collection_name: str) -> None:
        col = Collection(collection_name)
        self._load_collection(col, refresh=True)

    def get_chunk_count(self, *, collection_name: str) -> int | None:
        """返回 collection 中的 chunk 数量；若 collection 不存在则返回 None。"""
        if not utility.has_collection(collection_name):
            return None
        col = Collection(collection_name)
        self._load_collection(col)
        return col.num_entities

    def get_visible_chunks(self, *, collection_name: str, page_size: int = 16384) -> list[dict]:
        col = Collection(collection_name)
        self._load_collection(col)
        rows: list[dict] = []
        seen_ids: set[int] = set()
        ranges = [(0, (1 << 63) - 1)]
        while ranges:
            lower, upper = ranges.pop()
            batch = col.query(
                expr=f"id >= {lower} and id <= {upper}",
                output_fields=["id", "file_path", "line_start", "line_end", "chunk_hash"],
                limit=page_size,
                consistency_level="Strong",
            )
            if len(batch) >= page_size and lower < upper:
                middle = (lower + upper) // 2
                ranges.append((middle + 1, upper))
                ranges.append((lower, middle))
                continue
            batch_ids = [int(item["id"]) for item in batch]
            duplicate_ids = seen_ids.intersection(batch_ids)
            if duplicate_ids:
                raise RuntimeError(f"visible chunk pagination repeated id={min(duplicate_ids)}")
            rows.extend(batch)
            seen_ids.update(batch_ids)
        return sorted(rows, key=lambda item: int(item["id"]))

    def search(
        self,
        *,
        collection_name: str,
        query_vector: list[float],
        top_k: int = 10,
    ) -> list[SearchHit]:
        col = Collection(collection_name)
        self._load_collection(col)
        results = col.search(
            data=[query_vector],
            anns_field="vector",
            param={"metric_type": self.metric, "params": {}},
            limit=top_k,
            output_fields=["file_path", "line_start", "line_end", "chunk_hash"],
            consistency_level="Strong",
        )
        hits: list[SearchHit] = []
        for h in results[0]:
            # 兼容不同 pymilvus 版本：优先 .score，否则 .distances[0]
            raw = getattr(h, "score", None)
            source = "score"
            if raw is None and hasattr(h, "distances"):
                d = h.distances
                raw = d[0] if isinstance(d, (list, tuple)) else d
                source = "distances"
            if raw is None:
                raw = 0.0
                source = "missing"
            raw = float(raw)

            # 统一语义：score 越大越相似。
            # - L2: 返回距离（越小越相似），转换为 (0, 1] 区间分数；距离为 0 时分数为 1。
            # - COSINE/IP: 直接使用原始相似度分数。
            if self.metric == "L2":
                dist = raw if raw >= 0 else 0.0
                score = 1.0 / (1.0 + dist)
            else:
                score = raw

            hits.append(
                SearchHit(
                    file_path=h.entity.get("file_path"),
                    line_start=int(h.entity.get("line_start")),
                    line_end=int(h.entity.get("line_end")),
                    chunk_hash=h.entity.get("chunk_hash"),
                    score=score,
                    extra={
                        "metric": self.metric,
                        "raw_score": raw,
                        "distance_source": source,
                        "transformed_score": score,
                    },
                )
            )
        return hits
