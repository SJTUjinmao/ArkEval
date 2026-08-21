from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


DistanceMetric = Literal["COSINE", "IP", "L2"]
MilvusIndexType = Literal["FLAT", "HNSW", "IVF_FLAT"]


@dataclass(frozen=True)
class ChunkRef:
    """A reference to a code chunk.

    Notes:
    - `text` is used in-memory for embedding; it should not be persisted to Milvus.
    - `chunk_hash` is the stable identifier used for cache + Milvus upsert/delete.
    """

    file_path: str
    line_start: int
    line_end: int
    chunk_hash: str
    text: str


@dataclass(frozen=True)
class SearchHit:
    file_path: str
    line_start: int
    line_end: int
    chunk_hash: str
    score: float
    extra: dict[str, Any]
