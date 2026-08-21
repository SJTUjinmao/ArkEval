from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .chunking import RagChunk, iter_code_chunks, iter_doc_chunks
from .config import ARKEVAL_ROOT, RagConfig
from .deps import ensure_arkeval_on_path


@dataclass(frozen=True)
class RagIndexStats:
    index_name: str
    docs_chunks: int
    code_chunks: int
    embedding_dim: int
    sidecar_path: str
    docs_collection: str
    code_collection: str


def _localization_runtime():
    ensure_arkeval_on_path()
    from localization.localization_engine.config import load_config
    from localization.localization_engine.embedding.cache import EmbeddingCache
    from localization.localization_engine.embedding.clients import create_embedding_client, get_embedding_model_name
    from localization.localization_engine.milvus.client import MilvusStore

    loc_cfg = load_config(ARKEVAL_ROOT)
    return loc_cfg, EmbeddingCache, create_embedding_client, get_embedding_model_name, MilvusStore


def _batched(items: list[RagChunk], batch_size: int = 32) -> Iterable[list[RagChunk]]:
    for index in range(0, len(items), batch_size):
        yield items[index : index + batch_size]


def _embed_chunks(chunks: list[RagChunk], *, loc_cfg, EmbeddingCache, create_embedding_client, get_embedding_model_name) -> tuple[list[list[float]], int]:
    cache = EmbeddingCache(Path(loc_cfg.repo_root) / loc_cfg.indexing.cache_dir)
    emb = create_embedding_client(loc_cfg)
    model_name = get_embedding_model_name(loc_cfg)
    vectors: list[list[float]] = []
    dim = 0
    pending: list[RagChunk] = []
    pending_indexes: list[int] = []

    vectors = [[] for _ in chunks]
    for idx, chunk in enumerate(chunks):
        cached = cache.get(model_name=model_name, chunk_hash=chunk.chunk_hash)
        if cached is None:
            pending.append(chunk)
            pending_indexes.append(idx)
        else:
            vectors[idx] = cached
            dim = dim or len(cached)

    for batch_start in range(0, len(pending), 32):
        batch = pending[batch_start : batch_start + 32]
        indexes = pending_indexes[batch_start : batch_start + 32]
        embedded = emb.embed_texts([chunk.text for chunk in batch])
        for idx, chunk, vector in zip(indexes, batch, embedded, strict=True):
            cache.put(model_name=model_name, chunk_hash=chunk.chunk_hash, vector=vector, dim=len(vector))
            vectors[idx] = vector
            dim = dim or len(vector)

    if chunks and not dim:
        raise RuntimeError("RAG embedding produced no vectors")
    return vectors, dim


def _write_sidecar(path: Path, chunks: list[RagChunk]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for chunk in chunks:
            handle.write(json.dumps(chunk.to_json(), ensure_ascii=False) + "\n")


def _upsert_source_type(
    *,
    cfg: RagConfig,
    chunks: list[RagChunk],
    source_type: str,
    loc_cfg,
    MilvusStore,
    vectors: list[list[float]],
    dim: int,
    full: bool,
) -> None:
    collection_name = cfg.collection_name(source_type)
    store = MilvusStore(host=loc_cfg.milvus.host, port=loc_cfg.milvus.port, metric=loc_cfg.milvus.metric)
    store.connect()
    if full:
        store.drop_collection(collection_name=collection_name)
    store.ensure_collection(collection_name=collection_name, dim=dim)
    if not chunks:
        return
    tuples = [(chunk.source_path, chunk.line_start, chunk.line_end, chunk.chunk_hash) for chunk in chunks]
    for start in range(0, len(chunks), 256):
        store.upsert_chunks(
            collection_name=collection_name,
            chunks=tuples[start : start + 256],
            vectors=vectors[start : start + 256],
        )


def build_index(cfg: RagConfig, *, full: bool = True) -> RagIndexStats:
    loc_cfg, EmbeddingCache, create_embedding_client, get_embedding_model_name, MilvusStore = _localization_runtime()
    docs_chunks = iter_doc_chunks(cfg.docs_roots)
    code_chunks = iter_code_chunks(
        cfg.samples_roots,
        node_executable=getattr(loc_cfg, "node_executable", "node"),
        max_chunk_chars=getattr(loc_cfg.indexing, "max_chunk_chars", 2048),
    )
    all_chunks = docs_chunks + code_chunks
    if not all_chunks:
        _write_sidecar(cfg.sidecar_path, [])
        return RagIndexStats(
            index_name=cfg.safe_index_name,
            docs_chunks=0,
            code_chunks=0,
            embedding_dim=0,
            sidecar_path=str(cfg.sidecar_path),
            docs_collection=cfg.collection_name("docs"),
            code_collection=cfg.collection_name("code"),
        )

    vectors, dim = _embed_chunks(
        all_chunks,
        loc_cfg=loc_cfg,
        EmbeddingCache=EmbeddingCache,
        create_embedding_client=create_embedding_client,
        get_embedding_model_name=get_embedding_model_name,
    )
    docs_vectors = vectors[: len(docs_chunks)]
    code_vectors = vectors[len(docs_chunks) :]

    _upsert_source_type(
        cfg=cfg,
        chunks=docs_chunks,
        source_type="docs",
        loc_cfg=loc_cfg,
        MilvusStore=MilvusStore,
        vectors=docs_vectors,
        dim=dim,
        full=full,
    )
    _upsert_source_type(
        cfg=cfg,
        chunks=code_chunks,
        source_type="code",
        loc_cfg=loc_cfg,
        MilvusStore=MilvusStore,
        vectors=code_vectors,
        dim=dim,
        full=full,
    )
    _write_sidecar(cfg.sidecar_path, all_chunks)
    return RagIndexStats(
        index_name=cfg.safe_index_name,
        docs_chunks=len(docs_chunks),
        code_chunks=len(code_chunks),
        embedding_dim=dim,
        sidecar_path=str(cfg.sidecar_path),
        docs_collection=cfg.collection_name("docs"),
        code_collection=cfg.collection_name("code"),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the ArkFix RAG Milvus index.")
    parser.add_argument("--rag-mode", default="on")
    parser.add_argument("--rag-docs-roots", default="")
    parser.add_argument("--rag-samples-roots", default="")
    parser.add_argument("--rag-index-name", default="arkfix_default")
    parser.add_argument("--rag-storage-dir", default="")
    parser.add_argument("--no-full", dest="full", action="store_false", default=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = RagConfig.from_values(
        mode=args.rag_mode,
        docs_roots=args.rag_docs_roots,
        samples_roots=args.rag_samples_roots,
        index_name=args.rag_index_name,
        storage_dir=args.rag_storage_dir or None,
    )
    stats = build_index(cfg, full=args.full)
    print(json.dumps(asdict(stats), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
