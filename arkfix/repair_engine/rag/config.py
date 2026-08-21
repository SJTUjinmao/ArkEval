from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


REPAIR_ENGINE_ROOT = Path(__file__).resolve().parents[1]
ARKFIX_ROOT = REPAIR_ENGINE_ROOT.parent
ARKEVAL_ROOT = ARKFIX_ROOT.parent
DEFAULT_STORAGE_DIR = REPAIR_ENGINE_ROOT / "rag_store"


def split_roots(value: str | None) -> tuple[Path, ...]:
    """Parse a semicolon/comma separated path list.

    Semicolon is preferred on Windows because drive letters contain colons.
    """

    if not value:
        return ()
    parts = [part.strip().strip('"') for part in re.split(r"[;,]", value) if part.strip()]
    return tuple(Path(part).expanduser().resolve() for part in parts)


@dataclass(frozen=True)
class RagConfig:
    mode: str = "off"
    docs_roots: tuple[Path, ...] = ()
    samples_roots: tuple[Path, ...] = ()
    index_name: str = "arkfix_default"
    top_k_docs: int = 4
    top_k_code: int = 4
    max_context_chars: int = 12000
    storage_dir: Path = DEFAULT_STORAGE_DIR
    fail_open: bool = True

    @property
    def enabled(self) -> bool:
        return self.mode.strip().lower() not in {"", "off", "false", "0", "none"}

    @property
    def index_dir(self) -> Path:
        return self.storage_dir / self.safe_index_name

    @property
    def sidecar_path(self) -> Path:
        return self.index_dir / "chunks.jsonl"

    @property
    def safe_index_name(self) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.index_name.strip())
        safe = safe.strip("._-")
        return safe or "arkfix_default"

    def collection_name(self, source_type: str) -> str:
        if source_type not in {"docs", "code"}:
            raise ValueError(f"unknown RAG source type: {source_type}")
        return f"arkfix_rag_{source_type}_{self.safe_index_name}"

    @classmethod
    def from_values(
        cls,
        *,
        mode: str = "off",
        docs_roots: str | None = "",
        samples_roots: str | None = "",
        index_name: str = "arkfix_default",
        top_k_docs: int = 4,
        top_k_code: int = 4,
        max_context_chars: int = 12000,
        storage_dir: str | Path | None = None,
        fail_open: bool = True,
    ) -> "RagConfig":
        return cls(
            mode=mode,
            docs_roots=split_roots(docs_roots),
            samples_roots=split_roots(samples_roots),
            index_name=index_name,
            top_k_docs=max(0, int(top_k_docs)),
            top_k_code=max(0, int(top_k_code)),
            max_context_chars=max(0, int(max_context_chars)),
            storage_dir=Path(storage_dir).expanduser().resolve() if storage_dir else DEFAULT_STORAGE_DIR,
            fail_open=bool(fail_open),
        )
