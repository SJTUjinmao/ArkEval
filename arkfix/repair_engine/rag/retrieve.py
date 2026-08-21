from __future__ import annotations

import json
import re
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ARKEVAL_ROOT, RagConfig
from .deps import ensure_arkeval_on_path


@dataclass(frozen=True)
class RagResult:
    context: str
    metadata: dict[str, Any]


def _localization_runtime():
    ensure_arkeval_on_path()
    from localization.localization_engine.config import load_config
    from localization.localization_engine.embedding.clients import create_embedding_client
    from localization.localization_engine.milvus.client import MilvusStore

    loc_cfg = load_config(ARKEVAL_ROOT)
    return loc_cfg, create_embedding_client, MilvusStore


def _load_sidecar(path: Path) -> dict[str, dict[str, Any]]:
    chunks: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return chunks
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            data = json.loads(line)
            chunk_hash = str(data.get("chunk_hash") or "")
            if chunk_hash:
                chunks[chunk_hash] = data
    return chunks


def _truncate(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 80)].rstrip() + "\n...[truncated]"


def _extract_query_signals(text: str, *, limit: int = 80) -> str:
    tokens = re.findall(
        r"@[A-Za-z_][A-Za-z0-9_]*|"
        r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+|"
        r"[A-Z][A-Za-z0-9_]*(?:Error|Exception)|"
        r"\b(?:ArkTS|ArkUI|hvigor|ets|AppStorage|LocalStorage|ForEach|"
        r"aboutToAppear|build|State|Prop|Link|ObjectLink|Observed|Builder|"
        r"BuilderParam|StorageLink|StorageProp|BusinessError|Want)\b",
        text,
    )
    seen: set[str] = set()
    out: list[str] = []
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
        if len(out) >= limit:
            break
    return ", ".join(out)


def _query_text(
    *,
    issue: str | None,
    defect_files: str,
    project_path: str,
    observation: str | None = None,
    defect_file_context: str | None = None,
) -> str:
    observation_text = _truncate(observation or "", 4000)
    file_context_text = _truncate(defect_file_context or "", 8000)
    signals = _extract_query_signals(
        "\n".join([issue or "", defect_files, observation_text, file_context_text])
    )
    return "\n".join(
        part
        for part in [
            "HarmonyOS ArkTS repair query",
            f"Project root: {project_path}",
            "Relevant error/API signals:",
            signals,
            "Issue:",
            issue or "",
            "Known defect files:",
            defect_files,
            "Initial environment/build observation:",
            observation_text,
            "Defect file code excerpts from the base checkout:",
            file_context_text,
        ]
        if str(part).strip()
    )


def _search_source_type(
    *,
    cfg: RagConfig,
    source_type: str,
    top_k: int,
    query_vector: list[float],
    chunks_by_hash: dict[str, dict[str, Any]],
    store,
) -> list[dict[str, Any]]:
    if top_k <= 0:
        return []
    collection_name = cfg.collection_name(source_type)
    if not store.has_collection(collection_name=collection_name):
        return []
    hits = store.search(collection_name=collection_name, query_vector=query_vector, top_k=top_k)
    out: list[dict[str, Any]] = []
    for hit in hits:
        chunk = dict(chunks_by_hash.get(hit.chunk_hash, {}))
        if not chunk:
            chunk = {
                "source_type": source_type,
                "source_path": hit.file_path,
                "title": Path(hit.file_path).name,
                "line_start": hit.line_start,
                "line_end": hit.line_end,
                "chunk_hash": hit.chunk_hash,
                "text": "",
            }
        chunk["score"] = hit.score
        chunk["collection"] = collection_name
        chunk["extra"] = hit.extra
        out.append(chunk)
    return out


def _format_context(hits: list[dict[str, Any]], *, max_chars: int) -> str:
    if not hits or max_chars <= 0:
        return ""
    header = (
        "ARKTS RAG CONTEXT:\n"
        "Use this retrieved context only for ArkTS/ArkUI API, syntax, type-system, and idiom guidance. "
        "It does not expand KNOWN DEFECT FILES or override the repair scope.\n"
        "Repair guidance: keep fixes typed and local; avoid introducing `any`, `as any`, dynamic object "
        "properties, `require`, `var`, `delete`, or JS-only patterns unless the existing project already "
        "requires them; prefer ArkUI state/decorator patterns only when the retrieved evidence matches "
        "the failing code.\n"
        "Retrieved snippets:\n"
    )
    parts = [header]
    used = len(header)
    for index, hit in enumerate(hits, 1):
        source_type = hit.get("source_type", "")
        source_path = hit.get("source_path", "")
        title = hit.get("title", "") or Path(str(source_path)).name
        line_start = hit.get("line_start", "")
        line_end = hit.get("line_end", "")
        score = float(hit.get("score", 0.0) or 0.0)
        text = _truncate(str(hit.get("text") or ""), 2200)
        block = (
            f"\n[{index}] type={source_type} score={score:.4f} title={title}\n"
            f"source={source_path}:{line_start}-{line_end}\n"
            "```text\n"
            f"{text}\n"
            "```\n"
        )
        if used + len(block) > max_chars:
            remaining = max_chars - used
            if remaining > 200:
                parts.append(block[:remaining].rstrip() + "\n...[RAG context truncated]\n")
            break
        parts.append(block)
        used += len(block)
    return "".join(parts).strip()


def retrieve_rag_context(
    cfg: RagConfig,
    *,
    issue: str | None,
    defect_files: str,
    project_path: str,
    observation: str | None = None,
    defect_file_context: str | None = None,
) -> RagResult:
    metadata: dict[str, Any] = {
        "enabled": cfg.enabled,
        "mode": cfg.mode,
        "index_name": cfg.safe_index_name,
        "sidecar_path": str(cfg.sidecar_path),
        "docs_collection": cfg.collection_name("docs"),
        "code_collection": cfg.collection_name("code"),
        "hits": [],
    }
    if not cfg.enabled:
        return RagResult(context="", metadata=metadata)

    try:
        chunks_by_hash = _load_sidecar(cfg.sidecar_path)
        if not chunks_by_hash:
            raise FileNotFoundError(f"RAG sidecar missing or empty: {cfg.sidecar_path}")
        loc_cfg, create_embedding_client, MilvusStore = _localization_runtime()
        emb = create_embedding_client(loc_cfg)
        query = _query_text(
            issue=issue,
            defect_files=defect_files,
            project_path=project_path,
            observation=observation,
            defect_file_context=defect_file_context,
        )
        metadata["query"] = query
        metadata["query_chars"] = len(query)
        metadata["query_observation_chars"] = len(observation or "")
        metadata["query_defect_file_context_chars"] = len(defect_file_context or "")
        query_vector = emb.embed_texts([query])[0]
        store = MilvusStore(host=loc_cfg.milvus.host, port=loc_cfg.milvus.port, metric=loc_cfg.milvus.metric)
        store.connect()
        docs_hits = _search_source_type(
            cfg=cfg,
            source_type="docs",
            top_k=cfg.top_k_docs,
            query_vector=query_vector,
            chunks_by_hash=chunks_by_hash,
            store=store,
        )
        code_hits = _search_source_type(
            cfg=cfg,
            source_type="code",
            top_k=cfg.top_k_code,
            query_vector=query_vector,
            chunks_by_hash=chunks_by_hash,
            store=store,
        )
        hits = docs_hits + code_hits
        hits.sort(key=lambda item: float(item.get("score", 0.0) or 0.0), reverse=True)
        metadata["hits"] = [
            {
                "source_type": hit.get("source_type"),
                "source_path": hit.get("source_path"),
                "line_start": hit.get("line_start"),
                "line_end": hit.get("line_end"),
                "chunk_hash": hit.get("chunk_hash"),
                "score": hit.get("score"),
                "collection": hit.get("collection"),
            }
            for hit in hits
        ]
        metadata["hit_count"] = len(hits)
        return RagResult(context=_format_context(hits, max_chars=cfg.max_context_chars), metadata=metadata)
    except Exception as exc:
        metadata["error"] = str(exc)
        metadata["traceback"] = traceback.format_exc(limit=5)
        if cfg.fail_open:
            return RagResult(context="", metadata=metadata)
        raise
