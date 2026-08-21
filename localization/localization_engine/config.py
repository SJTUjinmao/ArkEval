from __future__ import annotations

"""Configuration loading.

External interface:
- `load_config(repo_root: str | Path) -> AppConfig`

Config sources (in priority order):
1) explicit config file `.codephoenix/config.json` under repo root
2) environment variables
3) built-in defaults

Embedding 双后端（默认 local）：
- embedding_backend: "local" 使用本地 vLLM 服务（如 start_server.sh，无需 token）
- embedding_backend: "modelscope" 使用魔搭 API（需 MODEL_SCOPE_ACCESS_TOKEN）
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .types import DistanceMetric, MilvusIndexType


def _env_value(*keys: str) -> str:
    for key in keys:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""


def _provider_key_suffixes(*suffixes: str) -> list[str]:
    provider = _env_value("LLM_PROVIDER", "OPENAI_PROVIDER", "MODEL_PROVIDER")
    provider = "".join(char if char.isalnum() else "_" for char in provider).strip("_").upper()
    if not provider:
        return []
    return [f"{provider}_{suffix}" for suffix in suffixes]


@dataclass(frozen=True)
class MilvusConfig:
    host: str = "127.0.0.1"
    port: int = 19530
    database: str | None = None
    collection_prefix: str = "codephoenix"
    metric: DistanceMetric = "L2"
    index_type: MilvusIndexType = "FLAT"


@dataclass(frozen=True)
class ModelScopeConfig:
    base_url: str = "https://api-inference.modelscope.cn/v1/"
    access_token_env: str = "MODEL_SCOPE_ACCESS_TOKEN"
    embedding_model: str = "Qwen/Qwen3-Embedding-8B"
    coder_model: str = "Qwen/Qwen3-Coder-30B-A3B-Instruct"
    embedding_endpoint_path: str = "embeddings"
    embedding_timeout_seconds: float = 60.0
    embedding_max_retries: int = 3


@dataclass(frozen=True)
class LocalEmbeddingConfig:
    """本地 embedding 服务（如 vLLM start_server.sh 启动的 Qwen3-Embedding-8B）。"""

    base_url: str = "http://127.0.0.1:8000/v1"
    model_name: str = "Qwen/Qwen3-Embedding-8B"
    endpoint_path: str = "embeddings"
    timeout_seconds: float = 60.0
    max_retries: int = 3


@dataclass(frozen=True)
class EmbeddingEndpointConfig:
    name: str
    base_url: str
    endpoint_path: str = "embed"
    weight: int = 1


@dataclass(frozen=True)
class DgxEmbeddingConfig:
    """DGX Spark custom Qwen3 embedding service.

    The endpoint is not OpenAI-compatible. It accepts POST /embed with
    {"texts": [...], "include_embeddings": true, "max_length": N}.
    """

    base_url: str = "http://127.0.0.1:8008"
    model_name: str = "Qwen/Qwen3-Embedding-8B"
    endpoint_path: str = "embed"
    timeout_seconds: float = 120.0
    max_retries: int = 3
    max_length: int = 256
    endpoints: tuple[EmbeddingEndpointConfig, ...] = ()


@dataclass(frozen=True)
class LLMConfig:
    base_url: str = ""
    api_key: str = ""
    model_name: str = ""
    endpoint_path: str = "chat/completions"
    timeout_seconds: float = 120.0
    max_retries: int = 3
    max_tokens: int = 2048


@dataclass(frozen=True)
class IndexingConfig:
    max_chunk_chars: int = 2048
    use_gitignore: bool = True
    use_builtin_ignore: bool = True
    builtin_ignore_file: str = ".codephoenix/default_ignore.txt"
    cache_dir: str = ".codephoenix/cache"
    chunk_workers: int = 16
    embedding_batch_size: int = 32
    embedding_parallel_requests: int = 13
    milvus_upsert_batch_size: int = 512
    milvus_upsert_workers: int = 2
    index_queue_size: int = 2048
    progress_interval_seconds: float = 2.0


@dataclass(frozen=True)
class AppConfig:
    repo_root: Path
    milvus: MilvusConfig = MilvusConfig()
    modelscope: ModelScopeConfig = ModelScopeConfig()
    local_embedding: LocalEmbeddingConfig = LocalEmbeddingConfig()
    dgx_embedding: DgxEmbeddingConfig = DgxEmbeddingConfig()
    llm: LLMConfig = LLMConfig()
    embedding_backend: str = "dgx"  # "modelscope" | "local" | "dgx"
    indexing: IndexingConfig = IndexingConfig()
    embedding_dim: int | None = None
    node_executable: str = "node"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_embedding_endpoints(raw_endpoints: Any, *, default_endpoint_path: str) -> tuple[EmbeddingEndpointConfig, ...]:
    endpoints: list[EmbeddingEndpointConfig] = []
    if isinstance(raw_endpoints, list):
        for idx, item in enumerate(raw_endpoints, start=1):
            if not isinstance(item, dict):
                continue
            base_url = str(item.get("base_url", "")).strip()
            if not base_url:
                continue
            endpoints.append(
                EmbeddingEndpointConfig(
                    name=str(item.get("name") or f"endpoint-{idx}"),
                    base_url=base_url,
                    endpoint_path=str(item.get("endpoint_path") or default_endpoint_path),
                    weight=max(1, int(item.get("weight", 1))),
                )
            )
    return tuple(endpoints)


def _parse_endpoint_urls(value: str, *, endpoint_path: str) -> tuple[EmbeddingEndpointConfig, ...]:
    urls = [part.strip() for part in value.split(",") if part.strip()]
    return tuple(
        EmbeddingEndpointConfig(name=f"env-endpoint-{idx}", base_url=url, endpoint_path=endpoint_path)
        for idx, url in enumerate(urls, start=1)
    )


def load_config(repo_root: str | Path) -> AppConfig:
    repo_root_path = Path(repo_root).resolve()
    raw = _read_json(repo_root_path / ".codephoenix" / "config.json")

    milvus_raw = raw.get("milvus", {})
    modelscope_raw = raw.get("modelscope", {})
    local_embedding_raw = raw.get("local_embedding", {})
    dgx_embedding_raw = raw.get("dgx_embedding", {})
    indexing_raw = raw.get("indexing", {})

    milvus = MilvusConfig(
        host=str(milvus_raw.get("host", MilvusConfig.host)),
        port=int(milvus_raw.get("port", MilvusConfig.port)),
        database=milvus_raw.get("database", None),
        collection_prefix=str(milvus_raw.get("collection_prefix", MilvusConfig.collection_prefix)),
        metric=str(milvus_raw.get("metric", MilvusConfig.metric)).upper(),
        index_type=str(milvus_raw.get("index_type", MilvusConfig.index_type)).upper(),
    )

    modelscope = ModelScopeConfig(
        base_url=str(modelscope_raw.get("base_url", ModelScopeConfig.base_url)),
        access_token_env=str(modelscope_raw.get("access_token_env", ModelScopeConfig.access_token_env)),
        embedding_model=str(modelscope_raw.get("embedding_model", ModelScopeConfig.embedding_model)),
        coder_model=str(modelscope_raw.get("coder_model", ModelScopeConfig.coder_model)),
        embedding_endpoint_path=str(
            modelscope_raw.get("embedding_endpoint_path", ModelScopeConfig.embedding_endpoint_path)
        ),
        embedding_timeout_seconds=float(
            modelscope_raw.get("embedding_timeout_seconds", ModelScopeConfig.embedding_timeout_seconds)
        ),
        embedding_max_retries=int(
            modelscope_raw.get("embedding_max_retries", ModelScopeConfig.embedding_max_retries)
        ),
    )

    local_embedding = LocalEmbeddingConfig(
        base_url=str(local_embedding_raw.get("base_url", LocalEmbeddingConfig.base_url)),
        model_name=str(local_embedding_raw.get("model_name", LocalEmbeddingConfig.model_name)),
        endpoint_path=str(
            local_embedding_raw.get("endpoint_path", LocalEmbeddingConfig.endpoint_path)
        ),
        timeout_seconds=float(
            local_embedding_raw.get("timeout_seconds", LocalEmbeddingConfig.timeout_seconds)
        ),
        max_retries=int(
            local_embedding_raw.get("max_retries", LocalEmbeddingConfig.max_retries)
        ),
    )
    # 环境变量覆盖：本地 vLLM 暴露的 model id 可能与默认不一致，用此变量指定
    env_model = (
        os.environ.get("LOCALIZATION_ENGINE_LOCAL_EMBEDDING_MODEL", "").strip()
        or os.environ.get("CODEPHOENIX_LOCAL_EMBEDDING_MODEL", "").strip()
    )
    env_local_base_url = (
        os.environ.get("LOCALIZATION_ENGINE_LOCAL_EMBEDDING_BASE_URL", "").strip()
        or os.environ.get("CODEPHOENIX_LOCAL_EMBEDDING_BASE_URL", "").strip()
    )
    env_local_endpoint = (
        os.environ.get("LOCALIZATION_ENGINE_LOCAL_EMBEDDING_ENDPOINT_PATH", "").strip()
        or os.environ.get("CODEPHOENIX_LOCAL_EMBEDDING_ENDPOINT_PATH", "").strip()
    )
    env_local_timeout = (
        os.environ.get("LOCALIZATION_ENGINE_LOCAL_EMBEDDING_TIMEOUT_SECONDS", "").strip()
        or os.environ.get("CODEPHOENIX_LOCAL_EMBEDDING_TIMEOUT_SECONDS", "").strip()
    )
    env_local_retries = (
        os.environ.get("LOCALIZATION_ENGINE_LOCAL_EMBEDDING_MAX_RETRIES", "").strip()
        or os.environ.get("CODEPHOENIX_LOCAL_EMBEDDING_MAX_RETRIES", "").strip()
    )
    if env_model or env_local_base_url or env_local_endpoint or env_local_timeout or env_local_retries:
        local_embedding = LocalEmbeddingConfig(
            base_url=env_local_base_url or local_embedding.base_url,
            model_name=env_model or local_embedding.model_name,
            endpoint_path=env_local_endpoint or local_embedding.endpoint_path,
            timeout_seconds=float(env_local_timeout or local_embedding.timeout_seconds),
            max_retries=int(env_local_retries or local_embedding.max_retries),
        )

    dgx_endpoint_path = str(dgx_embedding_raw.get("endpoint_path", DgxEmbeddingConfig.endpoint_path))
    dgx_endpoints = _parse_embedding_endpoints(
        dgx_embedding_raw.get("endpoints", ()),
        default_endpoint_path=dgx_endpoint_path,
    )
    dgx_embedding = DgxEmbeddingConfig(
        base_url=str(dgx_embedding_raw.get("base_url", DgxEmbeddingConfig.base_url)),
        model_name=str(dgx_embedding_raw.get("model_name", DgxEmbeddingConfig.model_name)),
        endpoint_path=dgx_endpoint_path,
        timeout_seconds=float(dgx_embedding_raw.get("timeout_seconds", DgxEmbeddingConfig.timeout_seconds)),
        max_retries=int(dgx_embedding_raw.get("max_retries", DgxEmbeddingConfig.max_retries)),
        max_length=int(dgx_embedding_raw.get("max_length", DgxEmbeddingConfig.max_length)),
        endpoints=dgx_endpoints,
    )
    env_dgx_base_url = (
        os.environ.get("LOCALIZATION_ENGINE_DGX_EMBEDDING_BASE_URL", "").strip()
        or os.environ.get("CODEPHOENIX_DGX_EMBEDDING_BASE_URL", "").strip()
    )
    env_dgx_base_urls = (
        os.environ.get("LOCALIZATION_ENGINE_DGX_EMBEDDING_BASE_URLS", "").strip()
        or os.environ.get("CODEPHOENIX_DGX_EMBEDDING_BASE_URLS", "").strip()
    )
    env_dgx_model = (
        os.environ.get("LOCALIZATION_ENGINE_DGX_EMBEDDING_MODEL", "").strip()
        or os.environ.get("CODEPHOENIX_DGX_EMBEDDING_MODEL", "").strip()
    )
    env_dgx_endpoint = (
        os.environ.get("LOCALIZATION_ENGINE_DGX_EMBEDDING_ENDPOINT_PATH", "").strip()
        or os.environ.get("CODEPHOENIX_DGX_EMBEDDING_ENDPOINT_PATH", "").strip()
    )
    env_dgx_timeout = (
        os.environ.get("LOCALIZATION_ENGINE_DGX_EMBEDDING_TIMEOUT_SECONDS", "").strip()
        or os.environ.get("CODEPHOENIX_DGX_EMBEDDING_TIMEOUT_SECONDS", "").strip()
    )
    env_dgx_retries = (
        os.environ.get("LOCALIZATION_ENGINE_DGX_EMBEDDING_MAX_RETRIES", "").strip()
        or os.environ.get("CODEPHOENIX_DGX_EMBEDDING_MAX_RETRIES", "").strip()
    )
    env_dgx_max_length = (
        os.environ.get("LOCALIZATION_ENGINE_DGX_EMBEDDING_MAX_LENGTH", "").strip()
        or os.environ.get("CODEPHOENIX_DGX_EMBEDDING_MAX_LENGTH", "").strip()
    )
    if env_dgx_base_urls:
        dgx_endpoints = _parse_endpoint_urls(env_dgx_base_urls, endpoint_path=env_dgx_endpoint or dgx_embedding.endpoint_path)
    elif env_dgx_base_url:
        dgx_endpoints = ()
    if env_dgx_base_url or env_dgx_base_urls or env_dgx_model or env_dgx_endpoint or env_dgx_timeout or env_dgx_retries or env_dgx_max_length:
        dgx_embedding = DgxEmbeddingConfig(
            base_url=env_dgx_base_url or dgx_embedding.base_url,
            model_name=env_dgx_model or dgx_embedding.model_name,
            endpoint_path=env_dgx_endpoint or dgx_embedding.endpoint_path,
            timeout_seconds=float(env_dgx_timeout or dgx_embedding.timeout_seconds),
            max_retries=int(env_dgx_retries or dgx_embedding.max_retries),
            max_length=int(env_dgx_max_length or dgx_embedding.max_length),
            endpoints=dgx_endpoints,
        )

    llm_raw = raw.get("llm", {})
    llm_api_key = _env_value(
        "LOCALIZATION_ENGINE_LLM_API_KEY",
        *_provider_key_suffixes("OPENAI_API_KEY", "API_KEY"),
        "OPENAI_API_KEY",
        "API_KEY",
    )
    llm_base_url = _env_value(
        "LOCALIZATION_ENGINE_LLM_BASE_URL",
        *_provider_key_suffixes("OPENAI_API_BASE_URL", "OPENAI_BASE_URL", "API_BASE_URL", "BASE_URL"),
        "OPENAI_API_BASE_URL",
        "OPENAI_BASE_URL",
        "API_BASE_URL",
        "BASE_URL",
    )
    llm_model = _env_value(
        "LOCALIZATION_ENGINE_LLM_MODEL",
        "LLM_MODEL",
        "GENERATIVE_MODEL",
        *_provider_key_suffixes("MODEL", "OPENAI_MODEL", "MODEL_NAME"),
        "MODEL",
        "OPENAI_MODEL",
        "MODEL_NAME",
    )
    llm = LLMConfig(
        base_url=llm_base_url or str(llm_raw.get("base_url", LLMConfig.base_url)),
        api_key=llm_api_key or str(llm_raw.get("api_key", LLMConfig.api_key)),
        model_name=llm_model or str(llm_raw.get("model_name", LLMConfig.model_name)),
        endpoint_path=str(llm_raw.get("endpoint_path", _env_value("LOCALIZATION_ENGINE_LLM_ENDPOINT_PATH") or LLMConfig.endpoint_path)),
        timeout_seconds=float(
            _env_value("LOCALIZATION_ENGINE_LLM_TIMEOUT_SECONDS", "OPENAI_HTTP_TIMEOUT")
            or llm_raw.get("timeout_seconds", LLMConfig.timeout_seconds)
        ),
        max_retries=int(
            _env_value("LOCALIZATION_ENGINE_LLM_MAX_RETRIES")
            or llm_raw.get("max_retries", LLMConfig.max_retries)
        ),
        max_tokens=int(
            _env_value("LOCALIZATION_ENGINE_LLM_MAX_TOKENS")
            or llm_raw.get("max_tokens", LLMConfig.max_tokens)
        ),
    )

    embedding_backend = str(
        os.environ.get("LOCALIZATION_ENGINE_EMBEDDING_BACKEND")
        or os.environ.get("CODEPHOENIX_EMBEDDING_BACKEND")
        or raw.get("embedding_backend", "dgx")
    ).strip().lower()
    if embedding_backend not in ("modelscope", "local", "dgx"):
        embedding_backend = "dgx"

    indexing = IndexingConfig(
        max_chunk_chars=int(indexing_raw.get("max_chunk_chars", IndexingConfig.max_chunk_chars)),
        use_gitignore=bool(indexing_raw.get("use_gitignore", IndexingConfig.use_gitignore)),
        use_builtin_ignore=bool(indexing_raw.get("use_builtin_ignore", IndexingConfig.use_builtin_ignore)),
        builtin_ignore_file=str(indexing_raw.get("builtin_ignore_file", IndexingConfig.builtin_ignore_file)),
        cache_dir=str(indexing_raw.get("cache_dir", IndexingConfig.cache_dir)),
        chunk_workers=int(
            os.environ.get("LOCALIZATION_ENGINE_CHUNK_WORKERS", "").strip()
            or os.environ.get("CODEPHOENIX_CHUNK_WORKERS", "").strip()
            or indexing_raw.get("chunk_workers", IndexingConfig.chunk_workers)
        ),
        embedding_batch_size=int(
            os.environ.get("LOCALIZATION_ENGINE_EMBEDDING_BATCH_SIZE", "").strip()
            or os.environ.get("CODEPHOENIX_EMBEDDING_BATCH_SIZE", "").strip()
            or indexing_raw.get("embedding_batch_size", IndexingConfig.embedding_batch_size)
        ),
        embedding_parallel_requests=int(
            os.environ.get("LOCALIZATION_ENGINE_EMBEDDING_PARALLEL_REQUESTS", "").strip()
            or os.environ.get("CODEPHOENIX_EMBEDDING_PARALLEL_REQUESTS", "").strip()
            or indexing_raw.get("embedding_parallel_requests", IndexingConfig.embedding_parallel_requests)
        ),
        milvus_upsert_batch_size=int(
            os.environ.get("LOCALIZATION_ENGINE_MILVUS_UPSERT_BATCH_SIZE", "").strip()
            or os.environ.get("CODEPHOENIX_MILVUS_UPSERT_BATCH_SIZE", "").strip()
            or indexing_raw.get("milvus_upsert_batch_size", IndexingConfig.milvus_upsert_batch_size)
        ),
        milvus_upsert_workers=int(
            os.environ.get("LOCALIZATION_ENGINE_MILVUS_UPSERT_WORKERS", "").strip()
            or os.environ.get("CODEPHOENIX_MILVUS_UPSERT_WORKERS", "").strip()
            or indexing_raw.get("milvus_upsert_workers", IndexingConfig.milvus_upsert_workers)
        ),
        index_queue_size=int(
            os.environ.get("LOCALIZATION_ENGINE_INDEX_QUEUE_SIZE", "").strip()
            or os.environ.get("CODEPHOENIX_INDEX_QUEUE_SIZE", "").strip()
            or indexing_raw.get("index_queue_size", IndexingConfig.index_queue_size)
        ),
        progress_interval_seconds=float(
            os.environ.get("LOCALIZATION_ENGINE_PROGRESS_INTERVAL_SECONDS", "").strip()
            or os.environ.get("CODEPHOENIX_PROGRESS_INTERVAL_SECONDS", "").strip()
            or indexing_raw.get("progress_interval_seconds", IndexingConfig.progress_interval_seconds)
        ),
    )

    embedding_dim = raw.get("embedding_dim")
    if embedding_dim is not None:
        embedding_dim = int(embedding_dim)

    node_executable = str(raw.get("node_executable", "node"))
    # Environment overrides (minimal MVP)
    if os.getenv("MILVUS_HOST"):
        milvus = MilvusConfig(
            host=os.environ["MILVUS_HOST"],
            port=milvus.port,
            database=milvus.database,
            collection_prefix=milvus.collection_prefix,
            metric=milvus.metric,
            index_type=milvus.index_type,
        )
    if os.getenv("MILVUS_PORT"):
        milvus = MilvusConfig(
            host=milvus.host,
            port=int(os.environ["MILVUS_PORT"]),
            database=milvus.database,
            collection_prefix=milvus.collection_prefix,
            metric=milvus.metric,
            index_type=milvus.index_type,
        )

    return AppConfig(
        repo_root=repo_root_path,
        milvus=milvus,
        modelscope=modelscope,
        local_embedding=local_embedding,
        dgx_embedding=dgx_embedding,
        llm=llm,
        embedding_backend=embedding_backend,
        indexing=indexing,
        embedding_dim=embedding_dim,
        node_executable=node_executable,
    )


def get_modelscope_token(cfg: AppConfig) -> str | None:
    return os.getenv(cfg.modelscope.access_token_env)
