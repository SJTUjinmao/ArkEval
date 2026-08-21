from __future__ import annotations

"""Embedding cache.

External interface (MVP):
- `EmbeddingCache.get(chunk_hash) -> list[float] | None`
- `EmbeddingCache.put(chunk_hash, vector, *, model_name, dim)`

Cache format: JSON files (one per chunk_hash).
Note: cache stores vectors + minimal metadata; it does NOT store full source code.
"""

import json
import math
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EmbeddingCache:
    root_dir: Path

    def _path(self, model_name: str, chunk_hash: str, signature: str = "") -> Path:
        safe_model = model_name.replace("/", "__")
        if signature:
            safe_model = f"{safe_model}__{signature}"
        return self.root_dir / "embeddings" / safe_model / f"{chunk_hash}.json"

    def get(self, *, model_name: str, chunk_hash: str, signature: str = "") -> list[float] | None:
        path = self._path(model_name, chunk_hash, signature)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
            return None
        if signature and payload.get("signature") != signature:
            return None
        vector = payload.get("vector")
        if not isinstance(vector, list):
            return None
        try:
            parsed = [float(x) for x in vector]
        except (TypeError, ValueError):
            path.unlink(missing_ok=True)
            return None
        if not all(math.isfinite(x) for x in parsed):
            path.unlink(missing_ok=True)
            return None
        return parsed

    def put(
        self,
        *,
        model_name: str,
        chunk_hash: str,
        vector: list[float],
        dim: int,
        signature: str = "",
    ) -> None:
        if len(vector) != dim or not all(math.isfinite(float(x)) for x in vector):
            raise RuntimeError("Refusing to cache invalid embedding vector")
        path = self._path(model_name, chunk_hash, signature)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "model_name": model_name,
            "signature": signature,
            "dim": dim,
            "vector": vector,
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
