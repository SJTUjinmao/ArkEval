from __future__ import annotations

"""Embedding clients.

External interface (MVP):
- `EmbeddingClient.embed_texts(texts: list[str]) -> list[list[float]]`
- `EmbeddingClient.dim` (vector dimension)

Implementations:
- `ModelScopeEmbeddingClient`: ModelScope API integration.
- `LocalEmbeddingClient`: 本地 vLLM/OpenAI 兼容的 embedding 服务（无需 token）。

所有 embedding 请求均不走代理（proxies=NO_PROXY），避免环境变量 SOCKS/HTTP 代理导致连 127.0.0.1 或魔搭超时。
"""

from dataclasses import dataclass
from itertools import cycle
import math
import os
from threading import Lock
import time
from typing import TYPE_CHECKING, Protocol

import requests

# 所有 embedding 请求强制直连，不使用环境变量中的代理
NO_PROXY = {"http": None, "https": None}

if TYPE_CHECKING:
    from ..config import AppConfig


class EmbeddingClient(Protocol):
    dim: int

    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


@dataclass
class ModelScopeEmbeddingClient:
    """ModelScope embedding client (OpenAI-compatible embeddings endpoint)."""

    base_url: str
    access_token: str
    model_name: str
    endpoint_path: str = "embeddings"
    timeout_seconds: float = 60.0
    max_retries: int = 3
    dim: int = 0

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        base = self.base_url.rstrip("/")
        url = f"{base}/{self.endpoint_path.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model_name,
            "input": texts,
            "encoding_format": "float",
        }

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.post(
                    url, headers=headers, json=payload, timeout=self.timeout_seconds, proxies=NO_PROXY
                )
                if resp.status_code >= 500:
                    raise RuntimeError(f"Embedding API server error: {resp.status_code} {resp.text[:500]}")
                if resp.status_code >= 400:
                    raise RuntimeError(f"Embedding API client error: {resp.status_code} {resp.text[:500]}")

                data = resp.json()
                rows = data.get("data")
                if not isinstance(rows, list):
                    raise RuntimeError(f"Invalid embedding response format: {data}")

                vectors: list[list[float]] = []
                for row in rows:
                    emb = row.get("embedding") if isinstance(row, dict) else None
                    if not isinstance(emb, list):
                        raise RuntimeError(f"Invalid embedding row: {row}")
                    vectors.append([float(x) for x in emb])

                if not vectors:
                    raise RuntimeError("Embedding API returned empty vectors")

                if self.dim == 0:
                    self.dim = len(vectors[0])

                for v in vectors:
                    if len(v) != self.dim:
                        raise RuntimeError(f"Embedding dimension mismatch: expected {self.dim}, got {len(v)}")

                return vectors
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(min(2 ** (attempt - 1), 4))

        raise RuntimeError(f"Embedding API failed after {self.max_retries} retries: {last_error}")


@dataclass
class LocalEmbeddingClient:
    """本地部署的 embedding 服务（vLLM / OpenAI 兼容），无需鉴权。"""

    base_url: str
    model_name: str
    endpoint_path: str = "embeddings"
    timeout_seconds: float = 60.0
    max_retries: int = 3
    dim: int = 0

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        base = self.base_url.rstrip("/")
        url = f"{base}/{self.endpoint_path.lstrip('/')}"
        headers = {"Content-Type": "application/json"}
        payload = {
            "model": self.model_name,
            "input": texts,
            "encoding_format": "float",
        }

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.post(
                    url, headers=headers, json=payload, timeout=self.timeout_seconds, proxies=NO_PROXY
                )
                if resp.status_code >= 500:
                    raise RuntimeError(f"Local embedding server error: {resp.status_code} {resp.text[:500]}")
                if resp.status_code >= 400:
                    raise RuntimeError(f"Local embedding client error: {resp.status_code} {resp.text[:500]}")

                data = resp.json()
                rows = data.get("data")
                if not isinstance(rows, list):
                    raise RuntimeError(f"Invalid embedding response format: {data}")

                vectors: list[list[float]] = []
                for row in rows:
                    emb = row.get("embedding") if isinstance(row, dict) else None
                    if not isinstance(emb, list):
                        raise RuntimeError(f"Invalid embedding row: {row}")
                    vectors.append([float(x) for x in emb])

                if not vectors:
                    raise RuntimeError("Embedding API returned empty vectors")

                if self.dim == 0:
                    object.__setattr__(self, "dim", len(vectors[0]))

                dim = self.dim
                for v in vectors:
                    if len(v) != dim:
                        raise RuntimeError(f"Embedding dimension mismatch: expected {dim}, got {len(v)}")

                return vectors
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(min(2 ** (attempt - 1), 4))

        raise RuntimeError(f"Local embedding API failed after {self.max_retries} retries: {last_error}")


@dataclass
class DgxEmbeddingClient:
    """DGX Spark custom Qwen3 embedding service."""

    base_url: str
    model_name: str
    endpoint_path: str = "embed"
    timeout_seconds: float = 120.0
    max_retries: int = 3
    max_length: int = 256
    dim: int = 0
    name: str = "dgx"

    def embed_texts(
        self,
        texts: list[str],
        *,
        request_timeout_seconds: float | None = None,
    ) -> list[list[float]]:
        if not texts:
            return []

        base = self.base_url.rstrip("/")
        url = f"{base}/{self.endpoint_path.lstrip('/')}"
        headers = {"Content-Type": "application/json"}
        payload = {
            "texts": texts,
            "include_embeddings": True,
            "max_length": self.max_length,
        }

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                timeout_seconds = self.timeout_seconds
                if request_timeout_seconds is not None:
                    timeout_seconds = min(timeout_seconds, max(0.1, request_timeout_seconds))
                resp = requests.post(url, headers=headers, json=payload, timeout=timeout_seconds, proxies=NO_PROXY)
                if resp.status_code >= 500:
                    raise RuntimeError(f"DGX embedding server error: {resp.status_code} {resp.text[:500]}")
                if resp.status_code >= 400:
                    raise RuntimeError(f"DGX embedding client error: {resp.status_code} {resp.text[:500]}")

                data = resp.json()
                self._validate_response_metadata(data, expected_count=len(texts))
                vectors = self._extract_vectors(data)
                if len(vectors) != len(texts):
                    raise RuntimeError(
                        f"DGX embedding count mismatch: expected {len(texts)}, got {len(vectors)}"
                    )
                if not vectors:
                    raise RuntimeError("DGX embedding API returned empty vectors")

                if self.dim == 0:
                    object.__setattr__(self, "dim", len(vectors[0]))

                dim = self.dim
                for v in vectors:
                    if len(v) != dim:
                        raise RuntimeError(f"Embedding dimension mismatch: expected {dim}, got {len(v)}")

                return vectors
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(min(2 ** (attempt - 1), 4))

        raise RuntimeError(f"DGX embedding API failed after {self.max_retries} retries: {last_error}")

    def _validate_response_metadata(self, data, *, expected_count: int) -> None:
        if not isinstance(data, dict):
            raise RuntimeError(f"Invalid DGX embedding response format: {data}")
        model = str(data.get("model", ""))
        if model != self.model_name:
            raise RuntimeError(f"Unexpected embedding model: expected {self.model_name}, got {model}")
        dim = data.get("dim")
        if dim is None or int(dim) != 4096:
            raise RuntimeError(f"Unexpected embedding dim: {dim}")
        count = data.get("count")
        if count is None or int(count) != expected_count:
            raise RuntimeError(f"Unexpected embedding count: expected {expected_count}, got {count}")
        max_length = data.get("max_length")
        if max_length is None or int(max_length) != self.max_length:
            raise RuntimeError(f"Unexpected embedding max_length: expected {self.max_length}, got {max_length}")

    @staticmethod
    def _extract_vectors(data) -> list[list[float]]:
        raw_vectors = None
        if isinstance(data, dict):
            raw_vectors = data.get("embeddings") or data.get("vectors")
            if raw_vectors is None and isinstance(data.get("data"), list):
                raw_vectors = [
                    row.get("embedding")
                    for row in data["data"]
                    if isinstance(row, dict) and isinstance(row.get("embedding"), list)
                ]
        if not isinstance(raw_vectors, list):
            raise RuntimeError(f"Invalid DGX embedding response format: {data}")
        vectors: list[list[float]] = []
        for row in raw_vectors:
            if not isinstance(row, list):
                raise RuntimeError(f"Invalid DGX embedding row: {row}")
            vector = [float(x) for x in row]
            if len(vector) != 4096:
                raise RuntimeError(f"Unexpected embedding vector dim: {len(vector)}")
            if not all(math.isfinite(x) for x in vector):
                raise RuntimeError("DGX embedding vector contains NaN or infinity")
            vectors.append(vector)
        return vectors


class DistributedDgxEmbeddingClient:
    """Round-robin pool of equivalent custom Qwen3 embedding endpoints."""

    def __init__(
        self,
        clients: list[DgxEmbeddingClient],
        *,
        outage_grace_seconds: float = 600.0,
        retry_interval_seconds: float = 5.0,
    ):
        if not clients:
            raise ValueError("DistributedDgxEmbeddingClient requires at least one endpoint")
        weighted: list[DgxEmbeddingClient] = []
        for client in clients:
            weighted.extend([client] * max(1, int(getattr(client, "weight", 1))))
        self._clients = clients
        self._cycle = cycle(weighted)
        self._lock = Lock()
        self.dim = 0
        self.completed_batches: dict[str, int] = {client.name: 0 for client in clients}
        self.failed_batches: dict[str, int] = {client.name: 0 for client in clients}
        self._cooldown_until: dict[str, float] = {client.name: 0.0 for client in clients}
        self._stats_lock = Lock()
        self.outage_grace_seconds = max(0.0, float(outage_grace_seconds))
        self.retry_interval_seconds = max(0.0, float(retry_interval_seconds))

    @property
    def endpoint_names(self) -> list[str]:
        return [client.name for client in self._clients]

    def stats(self) -> dict:
        with self._stats_lock:
            return {
                "endpoint_count": len(self._clients),
                "endpoint_names": self.endpoint_names,
                "completed_batches": dict(self.completed_batches),
                "failed_batches": dict(self.failed_batches),
            }

    def _ordered_clients(self) -> list[DgxEmbeddingClient]:
        with self._lock:
            first = next(self._cycle)
        ordered = [first]
        ordered.extend(client for client in self._clients if client is not first)
        now = time.monotonic()
        available = [client for client in ordered if self._cooldown_until.get(client.name, 0.0) <= now]
        return available or ordered

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        deadline = time.monotonic() + self.outage_grace_seconds
        rounds = 0
        last_errors: list[str] = []
        while True:
            rounds += 1
            errors: list[str] = []
            for client in self._ordered_clients():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    vectors = client.embed_texts(texts, request_timeout_seconds=remaining)
                    if self.dim == 0:
                        self.dim = len(vectors[0])
                    with self._stats_lock:
                        self.completed_batches[client.name] += 1
                        self._cooldown_until[client.name] = 0.0
                    return vectors
                except Exception as exc:
                    with self._stats_lock:
                        self.failed_batches[client.name] += 1
                        self._cooldown_until[client.name] = time.monotonic() + 30.0
                    errors.append(f"{client.name}: {exc}")
            if errors:
                last_errors = errors
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"All DGX embedding endpoints failed for {rounds} round(s): " + " | ".join(last_errors)
                )
            time.sleep(min(self.retry_interval_seconds, remaining))


def create_embedding_client(cfg: "AppConfig"):
    """根据配置返回对应的 embedding 客户端，实现 modelscope / local 无缝切换。"""
    from ..config import get_modelscope_token

    backend = getattr(cfg, "embedding_backend", "local")
    if backend == "local":
        le = cfg.local_embedding
        return LocalEmbeddingClient(
            base_url=le.base_url,
            model_name=le.model_name,
            endpoint_path=le.endpoint_path,
            timeout_seconds=le.timeout_seconds,
            max_retries=le.max_retries,
        )
    if backend == "dgx":
        de = cfg.dgx_embedding
        endpoints = list(de.endpoints)
        if not endpoints:
            from ..config import EmbeddingEndpointConfig

            endpoints = [EmbeddingEndpointConfig(name="dgx", base_url=de.base_url, endpoint_path=de.endpoint_path)]
        clients: list[DgxEmbeddingClient] = []
        for endpoint in endpoints:
            for replica_idx in range(max(1, int(endpoint.weight))):
                name = endpoint.name if endpoint.weight <= 1 else f"{endpoint.name}#{replica_idx + 1}"
                clients.append(
                    DgxEmbeddingClient(
                        base_url=endpoint.base_url,
                        model_name=de.model_name,
                        endpoint_path=endpoint.endpoint_path or de.endpoint_path,
                        timeout_seconds=de.timeout_seconds,
                        max_retries=de.max_retries,
                        max_length=de.max_length,
                        name=name,
                    )
                )
        if len(clients) == 1:
            return clients[0]
        return DistributedDgxEmbeddingClient(
            clients,
            outage_grace_seconds=float(
                os.getenv("LOCALIZATION_ENGINE_DGX_POOL_OUTAGE_GRACE_SECONDS", "600")
            ),
            retry_interval_seconds=float(
                os.getenv("LOCALIZATION_ENGINE_DGX_POOL_RETRY_INTERVAL_SECONDS", "5")
            ),
        )
    # modelscope
    token = get_modelscope_token(cfg)
    if not token:
        raise RuntimeError(
            f"Missing token env: {cfg.modelscope.access_token_env}. "
            "Embedding is API-only when using modelscope backend."
        )
    return ModelScopeEmbeddingClient(
        base_url=cfg.modelscope.base_url,
        access_token=token,
        model_name=cfg.modelscope.embedding_model,
        endpoint_path=cfg.modelscope.embedding_endpoint_path,
        timeout_seconds=cfg.modelscope.embedding_timeout_seconds,
        max_retries=cfg.modelscope.embedding_max_retries,
    )


def get_embedding_model_name(cfg: "AppConfig") -> str:
    """用于缓存 key 的模型名：按当前 embedding_backend 返回对应 model_name。"""
    backend = getattr(cfg, "embedding_backend", "local")
    if backend == "local":
        return cfg.local_embedding.model_name
    if backend == "dgx":
        return cfg.dgx_embedding.model_name
    return cfg.modelscope.embedding_model
