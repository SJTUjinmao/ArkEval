from __future__ import annotations

"""Repository indexing for localization.

The indexer keeps the external API small (`index_repo`, `locate_hits`,
`get_chunk_count`) while running the expensive path as a streaming pipeline:
scan/hash -> chunk checkpoint -> embedding/cache -> Milvus micro-batch upsert.
"""

import hashlib
import json
import math
import os
import re
import socket
import sys
import tempfile
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable

from filelock import FileLock, Timeout as FileLockTimeout

from .ast.extractor import extract_function_ranges
from .config import AppConfig, load_config
from .embedding.cache import EmbeddingCache
from .embedding.clients import create_embedding_client, get_embedding_model_name
from .merkle import merkle_load, merkle_load_leaves, merkle_save
from .milvus.client import MilvusStore
from .types import ChunkRef, SearchHit
from .utils.hashing import sha256_bytes, sha256_text
from .utils.ignore import build_ignore_matcher


ProgressCallback = Callable[[dict], None]
CHUNK_MANIFEST = "chunks_manifest.jsonl"
CHUNK_MANIFEST_META = "chunks_manifest.meta.json"
INDEX_STATE = "index_state.json"
COLLECTION_IDENTITY_VERSION = 2
INDEX_STATE_VERSION = 2
MANIFEST_SCOPE = "full"
DEFAULT_FULL_REBUILD_CONCURRENCY = 2


class CollectionIntegrityError(RuntimeError):
    """The live Milvus collection no longer matches its persisted manifest."""


def _acquire_full_rebuild_slot(cfg: AppConfig, progress: ProgressCallback) -> tuple[FileLock, int]:
    concurrency = max(
        1,
        int(os.environ.get("LOCALIZATION_ENGINE_FULL_REBUILD_CONCURRENCY", DEFAULT_FULL_REBUILD_CONCURRENCY)),
    )
    lock_prefix = f"arkeval_milvus_{cfg.milvus.host}_{cfg.milvus.port}_full_rebuild"
    waiting_reported = False
    while True:
        for slot in range(concurrency):
            lock = FileLock(str(Path(tempfile.gettempdir()) / f"{lock_prefix}_{slot}.lock"))
            try:
                lock.acquire(timeout=0)
                progress(
                    {
                        "phase": "milvus_full_rebuild_slot_acquired",
                        "full_rebuild_slot": slot,
                        "full_rebuild_concurrency": concurrency,
                    }
                )
                return lock, slot
            except FileLockTimeout:
                continue
        if not waiting_reported:
            progress(
                {
                    "phase": "milvus_full_rebuild_wait",
                    "full_rebuild_concurrency": concurrency,
                }
            )
            waiting_reported = True
        time.sleep(1.0)


@dataclass(frozen=True)
class CollectionIdentity:
    collection_identity_version: int
    collection_name: str
    collection_hostname: str
    collection_repo_root: str
    collection_namespace_hash: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def collection_identity(
    prefix: str,
    repo_root: str | Path,
    *,
    hostname: str | None = None,
) -> CollectionIdentity:
    repo = Path(repo_root).resolve()
    canonical_root = repo.as_posix()
    if os.name == "nt":
        canonical_root = canonical_root.casefold()
    canonical_hostname = (hostname or socket.gethostname()).strip().casefold() or "localhost"
    namespace_hash = sha256_text(f"{canonical_hostname}|{canonical_root}")[:12]
    safe_prefix = re.sub(r"[^0-9A-Za-z_]", "_", prefix).strip("_") or "codephoenix"
    safe_repo = re.sub(r"[^0-9A-Za-z_]", "_", repo.name).strip("_") or "repo"
    if safe_prefix[0].isdigit():
        safe_prefix = f"c_{safe_prefix}"
    collection_name = f"{safe_prefix[:48]}_{safe_repo[:96]}_{namespace_hash}"
    return CollectionIdentity(
        collection_identity_version=COLLECTION_IDENTITY_VERSION,
        collection_name=collection_name,
        collection_hostname=canonical_hostname,
        collection_repo_root=str(repo),
        collection_namespace_hash=namespace_hash,
    )


def get_collection_identity(repo_root: str | Path) -> CollectionIdentity:
    cfg = load_config(repo_root)
    return collection_identity(cfg.milvus.collection_prefix, cfg.repo_root)


def _resolve_index_mode(
    *,
    dry_run: bool,
    collection_exists: bool,
    collection_count: int | None,
    force_full: bool,
    old_merkle_exists: bool,
    reusable_state: bool,
) -> tuple[bool, str]:
    has_vectors = bool(collection_exists and collection_count is not None and collection_count > 0)
    if force_full:
        return True, "force_index_requested"
    if not dry_run and not collection_exists:
        return True, "collection_missing_full_rebuild"
    if not dry_run and collection_count is None:
        return True, "collection_count_unavailable_full_rebuild"
    if not dry_run and collection_exists:
        if not has_vectors:
            return True, "collection_empty_full_rebuild"
        if not old_merkle_exists:
            return True, "old_merkle_missing_full_rebuild"
        if not reusable_state:
            return True, "index_state_not_reusable_full_rebuild"
        return False, "collection_and_state_reusable_incremental"
    if not old_merkle_exists:
        return True, "old_merkle_missing_without_reusable_vectors"
    return False, "old_merkle_available"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(5):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.1 * (attempt + 1))


def _write_progress(meta_dir: Path, payload: dict) -> None:
    payload = {"updated_at": _now(), **payload}
    try:
        _write_json_atomic(meta_dir / "index_progress.json", payload)
    except PermissionError as exc:
        print(f"[index] skip progress write: {exc}", file=sys.stderr, flush=True)
    print("[index] " + json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)


def _write_state(meta_dir: Path, payload: dict) -> None:
    _write_json_atomic(meta_dir / INDEX_STATE, {"updated_at": _now(), **payload})


def _read_state(meta_dir: Path) -> dict:
    path = meta_dir / INDEX_STATE
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _saved_merkle_root(meta_dir: Path) -> str:
    tree = merkle_load(meta_dir)
    return str(tree.get("hash", "")) if isinstance(tree, dict) else ""


def _embedding_signature(cfg: AppConfig, model_name: str, dim: int) -> str:
    payload = {
        "backend": cfg.embedding_backend,
        "model_name": model_name,
        "dim": dim,
        "max_length": cfg.dgx_embedding.max_length if cfg.embedding_backend == "dgx" else None,
        "revision": os.environ.get("LOCALIZATION_ENGINE_EMBEDDING_REVISION", "").strip(),
    }
    return sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))[:16]


def _index_fingerprint(cfg: AppConfig, embedding_signature: str) -> str:
    payload = {
        "state_version": INDEX_STATE_VERSION,
        "collection_identity_version": COLLECTION_IDENTITY_VERSION,
        "embedding_signature": embedding_signature,
        "metric": cfg.milvus.metric,
        "index_type": cfg.milvus.index_type,
        "max_chunk_chars": cfg.indexing.max_chunk_chars,
        "node_executable": cfg.node_executable,
        "use_gitignore": cfg.indexing.use_gitignore,
        "use_builtin_ignore": cfg.indexing.use_builtin_ignore,
        "builtin_ignore_file": cfg.indexing.builtin_ignore_file,
    }
    return sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _progress_due(last_at: float, interval: float) -> bool:
    return time.time() - last_at >= max(0.2, interval)


def _scan_source_files(cfg: AppConfig, ignore, progress: ProgressCallback) -> tuple[list[Path], dict[str, str], dict]:
    files: list[Path] = []
    leaves: dict[str, str] = {}
    scanned = 0
    matched = 0
    last_progress = 0.0
    interval = cfg.indexing.progress_interval_seconds

    progress({"phase": "scan", "scanned_files": 0, "matched_files": 0})
    for path in cfg.repo_root.rglob("*"):
        if not path.is_file():
            continue
        scanned += 1
        if path.suffix in (".ts", ".ets") and not ignore.is_ignored(path):
            files.append(path)
            try:
                leaves[str(path.resolve())] = sha256_bytes(path.read_bytes())
            except Exception:
                leaves[str(path.resolve())] = ""
            matched += 1
        if scanned == 1 or _progress_due(last_progress, interval):
            last_progress = time.time()
            progress({"phase": "scan", "scanned_files": scanned, "matched_files": matched})

    tree = _build_merkle_from_file_hashes(cfg.repo_root, leaves)
    progress(
        {
            "phase": "scan_done",
            "scanned_files": scanned,
            "total_files": len(files),
            "root_hash": tree.get("hash", ""),
        }
    )
    return files, leaves, tree


def _build_merkle_from_file_hashes(repo_root: Path, leaves: dict[str, str]) -> dict:
    repo = repo_root.resolve()
    by_parts: dict[tuple[str, ...], tuple[str, str]] = {}
    for raw_path, file_hash in leaves.items():
        try:
            rel = Path(raw_path).resolve().relative_to(repo)
        except ValueError:
            continue
        by_parts[tuple(rel.parts)] = (str(Path(raw_path).resolve()), file_hash)

    def build_node(prefix: tuple[str, ...]) -> dict:
        prefix_len = len(prefix)
        next_segments: set[str] = set()
        for parts in by_parts:
            if len(parts) > prefix_len and parts[:prefix_len] == prefix:
                next_segments.add(parts[prefix_len])

        children: list[dict] = []
        for segment in sorted(next_segments):
            key = prefix + (segment,)
            if key in by_parts:
                abs_path, file_hash = by_parts[key]
                children.append({"path": abs_path, "hash": file_hash})
            else:
                children.append(build_node(key))
        children.sort(key=lambda item: item["path"])
        rel_path = "/".join(prefix) if prefix else str(repo)
        return {
            "path": rel_path,
            "hash": sha256_text(
                "".join(f"{item['path']}\0{item['hash']}\n" for item in children)
            ) if children else "",
            "children": children,
        }

    root = build_node(())
    root["path"] = str(repo)
    return root


def _chunk_by_functions(cfg: AppConfig, file_path: Path) -> list[ChunkRef]:
    text = file_path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    refs: list[ChunkRef] = []

    functions = extract_function_ranges(ts_file=file_path, node_executable=cfg.node_executable)
    for fn in functions:
        start = int(fn["line_start"])
        end = int(fn["line_end"])
        chunk_lines = lines[start - 1 : end]
        chunk_text = "\n".join(chunk_lines)
        if len(chunk_text) > cfg.indexing.max_chunk_chars:
            window: list[str] = []
            win_start = start
            cur_line = start
            for line in chunk_lines:
                if sum(len(x) for x in window) + len(line) + 1 > cfg.indexing.max_chunk_chars and window:
                    win_text = "\n".join(window)
                    refs.append(
                        ChunkRef(
                            file_path=str(file_path),
                            line_start=win_start,
                            line_end=cur_line - 1,
                            chunk_hash=sha256_text(f"{file_path}:{win_start}:{cur_line - 1}:{win_text}"),
                            text=win_text,
                        )
                    )
                    window = []
                    win_start = cur_line
                window.append(line)
                cur_line += 1
            if window:
                win_text = "\n".join(window)
                refs.append(
                    ChunkRef(
                        file_path=str(file_path),
                        line_start=win_start,
                        line_end=end,
                        chunk_hash=sha256_text(f"{file_path}:{win_start}:{end}:{win_text}"),
                        text=win_text,
                    )
                )
        else:
            refs.append(
                ChunkRef(
                    file_path=str(file_path),
                    line_start=start,
                    line_end=end,
                    chunk_hash=sha256_text(f"{file_path}:{start}:{end}:{chunk_text}"),
                    text=chunk_text,
                )
            )
    return refs


def _chunk_fallback_text(cfg: AppConfig, file_path: Path) -> list[ChunkRef]:
    text = file_path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    refs: list[ChunkRef] = []
    start = 1
    buf: list[str] = []
    for idx, line in enumerate(lines, start=1):
        if sum(len(x) for x in buf) + len(line) + 1 > cfg.indexing.max_chunk_chars and buf:
            chunk_text = "\n".join(buf)
            refs.append(ChunkRef(str(file_path), start, idx - 1, sha256_text(f"{file_path}:{start}:{idx - 1}:{chunk_text}"), chunk_text))
            buf = []
            start = idx
        buf.append(line)
    if buf:
        chunk_text = "\n".join(buf)
        refs.append(ChunkRef(str(file_path), start, len(lines), sha256_text(f"{file_path}:{start}:{len(lines)}:{chunk_text}"), chunk_text))
    return refs


def _chunk_one_file(cfg: AppConfig, file_path: Path) -> list[ChunkRef]:
    try:
        chunks = _chunk_by_functions(cfg, file_path)
        return chunks or _chunk_fallback_text(cfg, file_path)
    except Exception:
        return _chunk_fallback_text(cfg, file_path)


def _chunk_to_json(chunk: ChunkRef) -> dict:
    return {
        "file_path": chunk.file_path,
        "line_start": chunk.line_start,
        "line_end": chunk.line_end,
        "chunk_hash": chunk.chunk_hash,
        "text": chunk.text,
    }


def _chunk_from_json(payload: dict) -> ChunkRef:
    return ChunkRef(
        file_path=str(payload["file_path"]),
        line_start=int(payload["line_start"]),
        line_end=int(payload["line_end"]),
        chunk_hash=str(payload["chunk_hash"]),
        text=str(payload["text"]),
    )


def _manifest_file_stats(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    lines = 0
    with path.open("rb") as handle:
        for raw in handle:
            digest.update(raw)
            if raw.strip():
                lines += 1
    return lines, digest.hexdigest()


def _chunk_manifest_meta(
    cfg: AppConfig,
    *,
    root_hash: str,
    mode: str,
    total_files: int,
    total_chunks: int,
    manifest_sha256: str,
) -> dict:
    return {
        "repo_root": str(cfg.repo_root),
        "root_hash": root_hash,
        "mode": mode,
        "manifest_scope": MANIFEST_SCOPE,
        "max_chunk_chars": cfg.indexing.max_chunk_chars,
        "node_executable": cfg.node_executable,
        "total_files": total_files,
        "total_chunks": total_chunks,
        "manifest_sha256": manifest_sha256,
        "created_at": _now(),
    }


def _manifest_reusable(meta_dir: Path, cfg: AppConfig, *, root_hash: str, mode: str) -> bool:
    manifest = meta_dir / CHUNK_MANIFEST
    meta_path = meta_dir / CHUNK_MANIFEST_META
    if not manifest.is_file() or not meta_path.is_file():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    try:
        actual_chunks, actual_sha256 = _manifest_file_stats(manifest)
    except OSError:
        return False
    return (
        meta.get("repo_root") == str(cfg.repo_root)
        and meta.get("root_hash") == root_hash
        and meta.get("mode") == mode
        and meta.get("manifest_scope") == MANIFEST_SCOPE
        and int(meta.get("max_chunk_chars", -1)) == cfg.indexing.max_chunk_chars
        and meta.get("node_executable") == cfg.node_executable
        and int(meta.get("total_chunks", -1)) == actual_chunks
        and meta.get("manifest_sha256") == actual_sha256
    )


def _iter_manifest_chunks(meta_dir: Path) -> Iterable[ChunkRef]:
    with (meta_dir / CHUNK_MANIFEST).open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if line:
                yield _chunk_from_json(json.loads(line))


def _stream_chunk_manifest(
    cfg: AppConfig,
    meta_dir: Path,
    *,
    files: list[Path],
    mode: str,
    root_hash: str,
    progress: ProgressCallback,
) -> Iterable[ChunkRef]:
    manifest_path = meta_dir / CHUNK_MANIFEST
    meta_path = meta_dir / CHUNK_MANIFEST_META
    if meta_path.exists():
        meta_path.unlink()
    if manifest_path.exists():
        manifest_path.unlink()
    completed_files = 0
    total_chunks = 0
    last_progress = 0.0
    workers = max(1, min(int(cfg.indexing.chunk_workers), len(files) or 1, os.cpu_count() or 1))

    progress(
        {
            "phase": "chunking",
            "mode": mode,
            "completed_files": 0,
            "total_files": len(files),
            "total_chunks_so_far": 0,
            "parallel_workers": workers,
            "manifest_path": str(manifest_path),
        }
    )
    with manifest_path.open("w", encoding="utf-8", newline="\n") as handle:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending_paths = iter(files)
            futures = {}
            max_pending = max(workers, min(len(files), workers * 2))
            for _ in range(max_pending):
                try:
                    path = next(pending_paths)
                except StopIteration:
                    break
                futures[pool.submit(_chunk_one_file, cfg, path)] = path

            while futures:
                done, _pending = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    file_path = futures.pop(future)
                    try:
                        path = next(pending_paths)
                    except StopIteration:
                        pass
                    else:
                        futures[pool.submit(_chunk_one_file, cfg, path)] = path

                    chunks = future.result()
                    completed_files += 1
                    total_chunks += len(chunks)
                    for chunk in chunks:
                        handle.write(json.dumps(_chunk_to_json(chunk), ensure_ascii=False) + "\n")
                        yield chunk
                    if completed_files == 1 or completed_files == len(files) or _progress_due(last_progress, cfg.indexing.progress_interval_seconds):
                        handle.flush()
                        last_progress = time.time()
                        progress(
                            {
                                "phase": "chunking",
                                "mode": mode,
                                "completed_files": completed_files,
                                "total_files": len(files),
                                "total_chunks_so_far": total_chunks,
                                "current_file": str(file_path),
                                "parallel_workers": workers,
                                "pending_chunk_tasks": len(futures),
                                "manifest_path": str(manifest_path),
                            }
                        )
        handle.flush()
    actual_chunks, manifest_sha256 = _manifest_file_stats(manifest_path)
    if actual_chunks != total_chunks:
        raise RuntimeError(f"chunk manifest line count mismatch: expected={total_chunks} actual={actual_chunks}")
    _write_json_atomic(
        meta_path,
        _chunk_manifest_meta(
            cfg,
            root_hash=root_hash,
            mode=mode,
            total_files=len(files),
            total_chunks=total_chunks,
            manifest_sha256=manifest_sha256,
        ),
    )
    progress(
        {
            "phase": "chunking_done",
            "mode": mode,
            "completed_files": len(files),
            "total_files": len(files),
            "total_chunks": total_chunks,
            "manifest_path": str(manifest_path),
        }
    )


def _normalized_path_key(value: str | Path) -> str:
    text = Path(value).resolve().as_posix()
    return text.casefold() if os.name == "nt" else text


def _write_incremental_manifest(
    cfg: AppConfig,
    meta_dir: Path,
    *,
    files: list[Path],
    removed_paths: set[str],
    root_hash: str,
    total_files: int,
    progress: ProgressCallback,
) -> tuple[list[ChunkRef], int]:
    manifest_path = meta_dir / CHUNK_MANIFEST
    meta_path = meta_dir / CHUNK_MANIFEST_META
    if not manifest_path.is_file() or not meta_path.is_file():
        raise RuntimeError("incremental index requires a complete full manifest")
    try:
        previous_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("incremental index manifest metadata is invalid") from exc
    if previous_meta.get("manifest_scope") != MANIFEST_SCOPE:
        raise RuntimeError("incremental index requires a full-scope manifest")

    excluded = {_normalized_path_key(path) for path in removed_paths}
    excluded.update(_normalized_path_key(path) for path in files)
    workers = max(1, min(int(cfg.indexing.chunk_workers), len(files) or 1, os.cpu_count() or 1))
    changed_chunks: list[ChunkRef] = []
    if files:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_chunk_one_file, cfg, path): path for path in files}
            for completed, future in enumerate(as_completed(futures), start=1):
                changed_chunks.extend(future.result())
                progress(
                    {
                        "phase": "chunking",
                        "mode": "incremental",
                        "completed_files": completed,
                        "total_files": len(files),
                        "current_file": str(futures[future]),
                        "total_chunks_so_far": len(changed_chunks),
                        "parallel_workers": workers,
                    }
                )

    tmp = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    total_chunks = 0
    with tmp.open("w", encoding="utf-8", newline="\n") as target:
        with manifest_path.open("r", encoding="utf-8", errors="strict") as source:
            for raw in source:
                line = raw.strip()
                if not line:
                    continue
                payload = json.loads(line)
                if _normalized_path_key(str(payload.get("file_path") or "")) in excluded:
                    continue
                target.write(json.dumps(payload, ensure_ascii=False) + "\n")
                total_chunks += 1
        for chunk in changed_chunks:
            target.write(json.dumps(_chunk_to_json(chunk), ensure_ascii=False) + "\n")
            total_chunks += 1
    tmp.replace(manifest_path)
    actual_chunks, manifest_sha256 = _manifest_file_stats(manifest_path)
    if actual_chunks != total_chunks:
        raise RuntimeError(f"incremental manifest line count mismatch: expected={total_chunks} actual={actual_chunks}")
    _write_json_atomic(
        meta_path,
        _chunk_manifest_meta(
            cfg,
            root_hash=root_hash,
            mode="incremental",
            total_files=total_files,
            total_chunks=total_chunks,
            manifest_sha256=manifest_sha256,
        ),
    )
    progress(
        {
            "phase": "chunking_done",
            "mode": "incremental",
            "completed_files": len(files),
            "total_files": len(files),
            "total_chunks": total_chunks,
            "manifest_path": str(manifest_path),
        }
    )
    return changed_chunks, total_chunks


def _manifest_chunk_keys(
    meta_dir: Path,
    *,
    excluded_file_paths: Iterable[str] = (),
) -> set[tuple[str, int, int, str]]:
    excluded_keys = {
        MilvusStore.normalize_file_path(path)
        for path in excluded_file_paths
    }
    keys: set[tuple[str, int, int, str]] = set()
    for chunk in _iter_manifest_chunks(meta_dir):
        if MilvusStore.normalize_file_path(chunk.file_path) in excluded_keys:
            continue
        key = (chunk.file_path, chunk.line_start, chunk.line_end, chunk.chunk_hash)
        if key in keys:
            raise RuntimeError(f"duplicate chunk in manifest: {key}")
        keys.add(key)
    return keys


def _audit_collection(
    store: MilvusStore,
    *,
    collection_name: str,
    meta_dir: Path,
    excluded_file_paths: Iterable[str] = (),
    missing_retry_seconds: float = 600.0,
    retry_interval_seconds: float = 2.0,
    include_rows: bool = False,
) -> dict[str, object]:
    expected = _manifest_chunk_keys(
        meta_dir,
        excluded_file_paths=excluded_file_paths,
    )
    deadline = time.monotonic() + missing_retry_seconds
    while True:
        rows = store.get_visible_chunks(collection_name=collection_name)
        actual_list = [
            (
                str(row.get("file_path") or ""),
                int(row.get("line_start") or 0),
                int(row.get("line_end") or 0),
                str(row.get("chunk_hash") or ""),
            )
            for row in rows
        ]
        actual = set(actual_list)
        duplicate_count = len(actual_list) - len(actual)
        missing = expected - actual
        extra = actual - expected
        if not duplicate_count and not missing and not extra:
            break
        message = (
            "Milvus collection does not match current manifest: "
            f"expected={len(expected)} actual={len(actual_list)} duplicates={duplicate_count} "
            f"missing={len(missing)} extra={len(extra)}"
        )
        if duplicate_count or extra or time.monotonic() >= deadline:
            raise CollectionIntegrityError(message)
        store.refresh_collection(collection_name=collection_name)
        time.sleep(retry_interval_seconds)
    digest = sha256_text("\n".join("\t".join(map(str, key)) for key in sorted(expected)))
    result: dict[str, object] = {
        "ok": True,
        "expected_chunks": len(expected),
        "visible_chunks": len(actual_list),
        "duplicate_chunks": 0,
        "chunk_set_sha256": digest,
    }
    if include_rows:
        result["validated_rows"] = rows
    return result


def _is_payload_too_large(exc: Exception) -> bool:
    text = str(exc).lower()
    return "resource_exhausted" in text or "message larger than max" in text or "received message larger" in text


def _upsert_adaptive(
    store: MilvusStore,
    *,
    collection_name: str,
    items: list[tuple[tuple[str, int, int, str], list[float]]],
) -> None:
    if not items:
        return
    for chunk, vector in items:
        if not all(math.isfinite(float(x)) for x in vector):
            raise RuntimeError(f"Invalid embedding vector for {chunk[0]}:{chunk[1]}-{chunk[2]}")
    chunks = [item[0] for item in items]
    vectors = [item[1] for item in items]
    try:
        store.upsert_chunks(collection_name=collection_name, chunks=chunks, vectors=vectors, flush=False)
    except Exception as exc:
        if len(items) == 1 or not _is_payload_too_large(exc):
            raise
        mid = len(items) // 2
        _upsert_adaptive(store, collection_name=collection_name, items=items[:mid])
        _upsert_adaptive(store, collection_name=collection_name, items=items[mid:])


def _embed_batch(
    batch: list[ChunkRef],
    *,
    cache: EmbeddingCache,
    emb,
    model_name: str,
    embedding_signature: str,
) -> list[tuple[ChunkRef, list[float]]]:
    vectors = emb.embed_texts([chunk.text for chunk in batch])
    if len(vectors) != len(batch):
        raise RuntimeError(f"Embedding count mismatch: expected {len(batch)}, got {len(vectors)}")
    out: list[tuple[ChunkRef, list[float]]] = []
    for chunk, vector in zip(batch, vectors, strict=True):
        cache.put(
            model_name=model_name,
            chunk_hash=chunk.chunk_hash,
            vector=vector,
            dim=len(vector),
            signature=embedding_signature,
        )
        out.append((chunk, vector))
    return out


def _embedding_endpoint_stats(emb) -> dict:
    stats_fn = getattr(emb, "stats", None)
    if callable(stats_fn):
        return stats_fn()
    name = getattr(emb, "name", "default")
    return {
        "endpoint_count": 1,
        "endpoint_names": [name],
        "completed_batches": {},
        "failed_batches": {},
    }


def _stream_embed_and_upsert(
    cfg: AppConfig,
    meta_dir: Path,
    *,
    chunks: Iterable[ChunkRef],
    total_chunks: int | None,
    mode: str,
    dry_run: bool,
    cache: EmbeddingCache,
    emb,
    model_name: str,
    embedding_signature: str,
    store: MilvusStore,
    collection_name: str,
    progress: ProgressCallback,
) -> None:
    batch_size = max(1, int(cfg.indexing.embedding_batch_size))
    parallel_requests = max(1, int(cfg.indexing.embedding_parallel_requests))
    upsert_batch_size = max(1, int(cfg.indexing.milvus_upsert_batch_size))
    upsert_workers = max(1, int(cfg.indexing.milvus_upsert_workers))
    max_pending_upsert_batches = max(1, int(cfg.indexing.index_queue_size) // upsert_batch_size)
    total_embedding_batches = None if total_chunks is None else max(0, math.ceil(total_chunks / batch_size))

    cached_chunks = 0
    embedded_chunks = 0
    completed_embedding_batches = 0
    consumed_chunks = 0
    upserted_chunks = 0
    submitted_upsert_batches = 0
    completed_upsert_batches = 0
    pending_embed: list[ChunkRef] = []
    pending_upsert: list[tuple[tuple[str, int, int, str], list[float]]] = []
    embed_futures = {}
    upsert_futures = {}
    last_progress = 0.0

    def emit(phase: str = "embedding") -> None:
        progress(
            {
                "phase": phase,
                "mode": mode,
                "total_chunks": total_chunks,
                "consumed_chunks": consumed_chunks,
                "cached_chunks": cached_chunks,
                "pending_chunks": None if total_chunks is None else max(0, total_chunks - consumed_chunks),
                "embedded_chunks": embedded_chunks,
                "completed_batches": completed_embedding_batches,
                "total_batches": total_embedding_batches,
                "upserted_chunks": upserted_chunks,
                "submitted_upsert_batches": submitted_upsert_batches,
                "completed_upsert_batches": completed_upsert_batches,
                "upsert_batch_size": upsert_batch_size,
                "embedding_endpoints": _embedding_endpoint_stats(emb),
            }
        )

    def add_upsert_item(chunk: ChunkRef, vector: list[float], upsert_pool: ThreadPoolExecutor) -> None:
        nonlocal completed_upsert_batches, submitted_upsert_batches, upserted_chunks
        pending_upsert.append(((chunk.file_path, chunk.line_start, chunk.line_end, chunk.chunk_hash), vector))
        if len(pending_upsert) >= upsert_batch_size and not dry_run:
            while len(upsert_futures) >= max_pending_upsert_batches:
                for done_future in as_completed(list(upsert_futures), timeout=None):
                    batch_len = upsert_futures.pop(done_future)
                    done_future.result()
                    upserted_chunks += batch_len
                    completed_upsert_batches += 1
                    break
            batch = list(pending_upsert)
            pending_upsert.clear()
            submitted_upsert_batches += 1
            upsert_futures[
                upsert_pool.submit(_upsert_adaptive, store, collection_name=collection_name, items=batch)
            ] = len(batch)

    progress(
        {
            "phase": "embedding",
            "mode": mode,
            "total_chunks": total_chunks,
            "cached_chunks": 0,
            "pending_chunks": total_chunks,
            "embedded_chunks": 0,
            "embedding_parallel_requests": parallel_requests,
            "embedding_batch_size": batch_size,
            "embedding_endpoints": _embedding_endpoint_stats(emb),
        }
    )
    progress(
        {
            "phase": "milvus_upsert",
            "mode": mode,
            "total_chunks": total_chunks,
            "upserted_chunks": 0,
            "submitted_batches": 0,
            "completed_batches": 0,
            "batch_size": upsert_batch_size,
            "parallel_workers": upsert_workers,
        }
    )

    with ThreadPoolExecutor(max_workers=parallel_requests) as embed_pool:
        with ThreadPoolExecutor(max_workers=upsert_workers) as upsert_pool:
            for chunk in chunks:
                consumed_chunks += 1
                cached = cache.get(
                    model_name=model_name,
                    chunk_hash=chunk.chunk_hash,
                    signature=embedding_signature,
                )
                if cached is not None:
                    cached_chunks += 1
                    add_upsert_item(chunk, cached, upsert_pool)
                else:
                    pending_embed.append(chunk)
                    if len(pending_embed) >= batch_size:
                        batch = pending_embed
                        pending_embed = []
                        embed_futures[
                            embed_pool.submit(
                                _embed_batch,
                                batch,
                                cache=cache,
                                emb=emb,
                                model_name=model_name,
                                embedding_signature=embedding_signature,
                            )
                        ] = len(batch)

                while len(embed_futures) >= parallel_requests:
                    for future in as_completed(list(embed_futures), timeout=None):
                        batch_len = embed_futures.pop(future)
                        completed_embedding_batches += 1
                        embedded_chunks += batch_len
                        for embedded_chunk, vector in future.result():
                            add_upsert_item(embedded_chunk, vector, upsert_pool)
                        break

                finished_upserts = [future for future in upsert_futures if future.done()]
                for future in finished_upserts:
                    batch_len = upsert_futures.pop(future)
                    future.result()
                    upserted_chunks += batch_len
                    completed_upsert_batches += 1

                if consumed_chunks == 1 or consumed_chunks == total_chunks or _progress_due(last_progress, cfg.indexing.progress_interval_seconds):
                    last_progress = time.time()
                    emit("embedding")
                    progress(
                        {
                            "phase": "milvus_upsert",
                            "mode": mode,
                            "total_chunks": total_chunks,
                            "upserted_chunks": upserted_chunks,
                            "submitted_batches": submitted_upsert_batches,
                            "completed_batches": completed_upsert_batches,
                            "batch_size": upsert_batch_size,
                            "parallel_workers": upsert_workers,
                        }
                    )

            if pending_embed:
                embed_futures[
                    embed_pool.submit(
                        _embed_batch,
                        pending_embed,
                        cache=cache,
                        emb=emb,
                        model_name=model_name,
                        embedding_signature=embedding_signature,
                    )
                ] = len(pending_embed)
                pending_embed = []

            for future in as_completed(list(embed_futures)):
                batch_len = embed_futures[future]
                completed_embedding_batches += 1
                embedded_chunks += batch_len
                for embedded_chunk, vector in future.result():
                    add_upsert_item(embedded_chunk, vector, upsert_pool)
                emit("embedding")

            if pending_upsert and not dry_run:
                batch = list(pending_upsert)
                pending_upsert.clear()
                submitted_upsert_batches += 1
                upsert_futures[
                    upsert_pool.submit(_upsert_adaptive, store, collection_name=collection_name, items=batch)
                ] = len(batch)

            for future in as_completed(list(upsert_futures)):
                batch_len = upsert_futures[future]
                future.result()
                upserted_chunks += batch_len
                completed_upsert_batches += 1
                progress(
                    {
                        "phase": "milvus_upsert",
                        "mode": mode,
                        "total_chunks": total_chunks,
                        "upserted_chunks": upserted_chunks,
                        "submitted_batches": submitted_upsert_batches,
                        "completed_batches": completed_upsert_batches,
                        "batch_size": upsert_batch_size,
                        "parallel_workers": upsert_workers,
                    }
                )

    if not dry_run:
        store.flush_collection(collection_name=collection_name)
    progress(
        {
            "phase": "milvus_upsert_done",
            "mode": mode,
            "total_chunks": total_chunks if total_chunks is not None else consumed_chunks,
            "upserted_chunks": upserted_chunks if not dry_run else 0,
            "submitted_batches": submitted_upsert_batches,
            "completed_batches": completed_upsert_batches,
        }
    )


def _delete_changed_paths(
    store: MilvusStore,
    *,
    collection_name: str,
    meta_dir: Path,
    removed_paths: set[str],
    changed_files: list[Path],
    progress: ProgressCallback,
    mode: str,
) -> None:
    paths = list(dict.fromkeys(sorted(removed_paths) + [str(path) for path in changed_files]))
    total = len(paths)
    if paths:
        before_keys = _manifest_chunk_keys(meta_dir)
        retained_keys = _manifest_chunk_keys(meta_dir, excluded_file_paths=paths)
        expected_deleted_chunks = len(before_keys) - len(retained_keys)
        pre_delete_audit = _audit_collection(
            store,
            collection_name=collection_name,
            meta_dir=meta_dir,
            include_rows=True,
        )
        progress(
            {
                "phase": "milvus_pre_delete_audit",
                "mode": mode,
                "visible_chunks": pre_delete_audit["visible_chunks"],
                "expected_deleted_chunks": expected_deleted_chunks,
            }
        )
        expected_deleted_keys = before_keys - retained_keys
        target_paths = {MilvusStore.normalize_file_path(path) for path in paths}
        validated_rows = list(pre_delete_audit["validated_rows"])
        rows_to_delete = [
            row
            for row in validated_rows
            if MilvusStore.normalize_file_path(str(row.get("file_path") or "")) in target_paths
        ]
        actual_deleted_keys = {
            (
                str(row.get("file_path") or ""),
                int(row.get("line_start") or 0),
                int(row.get("line_end") or 0),
                str(row.get("chunk_hash") or ""),
            )
            for row in rows_to_delete
        }
        if actual_deleted_keys != expected_deleted_keys or len(rows_to_delete) != len(actual_deleted_keys):
            raise CollectionIntegrityError(
                "Milvus deletion snapshot does not match old manifest: "
                f"expected={expected_deleted_chunks} actual={len(rows_to_delete)}"
            )
        progress({"phase": "milvus_delete_lookup", "mode": mode, "total_files": total})
        deleted_chunks = store.delete_chunk_ids(
            collection_name=collection_name,
            ids=[int(row["id"]) for row in rows_to_delete],
        )
        progress(
            {
                "phase": "milvus_delete",
                "mode": mode,
                "completed_files": total,
                "total_files": total,
                "deleted_chunks": deleted_chunks,
            }
        )
        store.flush_collection(collection_name=collection_name)
        progress({"phase": "milvus_delete_visibility", "mode": mode, "remaining_files": total})
        store.wait_for_file_paths_absent(collection_name=collection_name, file_paths=paths)
        post_delete_audit = _audit_collection(
            store,
            collection_name=collection_name,
            meta_dir=meta_dir,
            excluded_file_paths=paths,
        )
        progress(
            {
                "phase": "milvus_post_delete_audit",
                "mode": mode,
                "visible_chunks": post_delete_audit["visible_chunks"],
                "deleted_chunks": deleted_chunks,
            }
        )


def index_repo(
    repo_root: str | Path,
    *,
    dry_run: bool = False,
    full: bool = False,
) -> None | tuple[int, int, int]:
    cfg = load_config(repo_root)
    if dry_run:
        ignore = build_ignore_matcher(
            cfg.repo_root,
            use_gitignore=cfg.indexing.use_gitignore,
            use_builtin_ignore=cfg.indexing.use_builtin_ignore,
            builtin_ignore_file=cfg.indexing.builtin_ignore_file,
        )
        files, _leaves, _tree = _scan_source_files(cfg, ignore, lambda _payload: None)
        workers = max(1, min(int(cfg.indexing.chunk_workers), len(files) or 1, os.cpu_count() or 1))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            total_chunks = sum(len(chunks) for chunks in pool.map(lambda path: _chunk_one_file(cfg, path), files))
        emb = create_embedding_client(cfg)
        probe_vec = emb.embed_texts(["localization_engine_dim_probe"])[0]
        resolved_dim = cfg.embedding_dim or len(probe_vec)
        return (len(files), total_chunks, resolved_dim)
    meta_dir = cfg.repo_root / ".codephoenix"
    meta_dir.mkdir(parents=True, exist_ok=True)
    started_at = time.time()
    error_context: dict[str, object] = {"repo_root": str(cfg.repo_root)}
    full_rebuild_lock: FileLock | None = None

    def progress(payload: dict) -> None:
        _write_progress(
            meta_dir,
            {
                "repo_root": str(cfg.repo_root),
                "elapsed_seconds": round(time.time() - started_at, 1),
                **payload,
            },
        )

    try:
        ignore = build_ignore_matcher(
            cfg.repo_root,
            use_gitignore=cfg.indexing.use_gitignore,
            use_builtin_ignore=cfg.indexing.use_builtin_ignore,
            builtin_ignore_file=cfg.indexing.builtin_ignore_file,
        )
        ts_files, current_leaves, current_tree = _scan_source_files(cfg, ignore, progress)
        old_leaves = merkle_load_leaves(meta_dir)
        previous_state = _read_state(meta_dir)

        cache = EmbeddingCache(cfg.repo_root / cfg.indexing.cache_dir)
        emb = create_embedding_client(cfg)
        model_name = get_embedding_model_name(cfg)

        progress({"phase": "embedding_probe", "embedding_model": model_name})
        probe_vec = emb.embed_texts(["localization_engine_dim_probe"])[0]
        if cfg.embedding_dim is not None and len(probe_vec) != cfg.embedding_dim:
            raise RuntimeError(f"Configured embedding_dim={cfg.embedding_dim} but model returned dim={len(probe_vec)}")
        resolved_dim = cfg.embedding_dim or len(probe_vec)
        embedding_signature = _embedding_signature(cfg, model_name, resolved_dim)
        index_fingerprint = _index_fingerprint(cfg, embedding_signature)

        identity = collection_identity(cfg.milvus.collection_prefix, cfg.repo_root)
        collection_name = identity.collection_name
        error_context.update(
            {
                "collection": collection_name,
                **identity.to_dict(),
                "embedding_model": model_name,
                "embedding_signature": embedding_signature,
                "index_fingerprint": index_fingerprint,
            }
        )
        store = MilvusStore(
            host=cfg.milvus.host,
            port=cfg.milvus.port,
            metric=cfg.milvus.metric,
            index_type=cfg.milvus.index_type,
        )
        if not dry_run:
            store.connect()

        collection_exists = False if dry_run else store.has_collection(collection_name=collection_name)
        collection_count = None
        if collection_exists:
            try:
                collection_count = store.get_chunk_count(collection_name=collection_name)
            except Exception:
                collection_count = None

        previous_manifest_meta: dict = {}
        previous_manifest_valid = False
        manifest_meta_path = meta_dir / CHUNK_MANIFEST_META
        manifest_path = meta_dir / CHUNK_MANIFEST
        if manifest_meta_path.is_file():
            try:
                value = json.loads(manifest_meta_path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    previous_manifest_meta = value
                    actual_chunks, actual_sha256 = _manifest_file_stats(manifest_path)
                    previous_manifest_valid = (
                        int(value.get("total_chunks", -1)) == actual_chunks
                        and value.get("manifest_sha256") == actual_sha256
                    )
            except (OSError, json.JSONDecodeError):
                previous_manifest_meta = {}
        old_root_hash = _saved_merkle_root(meta_dir)
        previous_audit = previous_state.get("collection_audit")
        reusable_state = bool(
            previous_state.get("status") == "done"
            and int(previous_state.get("index_state_version", 0)) == INDEX_STATE_VERSION
            and previous_state.get("index_fingerprint") == index_fingerprint
            and previous_state.get("embedding_signature") == embedding_signature
            and previous_state.get("collection") == collection_name
            and previous_state.get("root_hash") == old_root_hash
            and previous_state.get("manifest_scope") == MANIFEST_SCOPE
            and previous_manifest_meta.get("manifest_scope") == MANIFEST_SCOPE
            and previous_manifest_meta.get("root_hash") == old_root_hash
            and previous_manifest_valid
            and isinstance(previous_audit, dict)
            and previous_audit.get("ok") is True
            and int(previous_audit.get("expected_chunks", -1)) == int(previous_manifest_meta.get("total_chunks", -2))
            and collection_count == int(previous_manifest_meta.get("total_chunks", -2))
        )
        do_full, mode_reason = _resolve_index_mode(
            dry_run=dry_run,
            collection_exists=collection_exists,
            collection_count=collection_count,
            force_full=full,
            old_merkle_exists=old_leaves is not None,
            reusable_state=reusable_state,
        )
        mode = "full" if do_full else "incremental"
        removed_paths: set[str] = set()
        if not dry_run:
            _write_state(
                meta_dir,
                {
                    "status": "building",
                    "phase": "prepare",
                    "index_state_version": INDEX_STATE_VERSION,
                    "mode": mode,
                    "mode_reason": mode_reason,
                    "target_root_hash": current_tree.get("hash", ""),
                    "manifest_scope": MANIFEST_SCOPE,
                    **error_context,
                },
            )
        progress(
            {
                "phase": "prepare",
                "mode": mode,
                "mode_reason": mode_reason,
                "collection": collection_name,
                **identity.to_dict(),
                "collection_exists": collection_exists,
                "collection_count": collection_count,
                "embedding_dim": resolved_dim,
                "embedding_signature": embedding_signature,
                "index_fingerprint": index_fingerprint,
                "previous_state_status": previous_state.get("status", ""),
                "previous_state_reusable": reusable_state,
                "chunk_workers": cfg.indexing.chunk_workers,
                "embedding_batch_size": cfg.indexing.embedding_batch_size,
                "embedding_parallel_requests": cfg.indexing.embedding_parallel_requests,
                "milvus_upsert_batch_size": cfg.indexing.milvus_upsert_batch_size,
                "milvus_upsert_workers": cfg.indexing.milvus_upsert_workers,
            }
        )

        if do_full:
            full_rebuild_lock, _full_rebuild_slot = _acquire_full_rebuild_slot(cfg, progress)
            files_to_chunk = ts_files
            root_hash = str(current_tree.get("hash", ""))
            if not dry_run:
                progress(
                    {
                        "phase": "milvus_reset",
                        "mode": mode,
                        "mode_reason": mode_reason,
                        "collection": collection_name,
                        **identity.to_dict(),
                        "collection_exists": collection_exists,
                        "collection_count": collection_count,
                    }
                )
                if collection_exists:
                    store.drop_collection(collection_name=collection_name)
                store.ensure_collection(collection_name=collection_name, dim=resolved_dim)
        else:
            if old_leaves is None:
                raise RuntimeError("incremental index selected without a previous Merkle tree")
            removed_paths = set(old_leaves) - set(current_leaves)
            files_to_chunk = [
                path
                for path in ts_files
                if str(path.resolve()) not in old_leaves
                or old_leaves.get(str(path.resolve())) != current_leaves.get(str(path.resolve()))
            ]
            root_hash = str(current_tree.get("hash", ""))
            progress(
                {
                    "phase": "incremental_plan",
                    "mode": mode,
                    "total_files": len(ts_files),
                    "removed_files": len(removed_paths),
                    "changed_or_new_files": len(files_to_chunk),
                }
            )
            if not dry_run:
                store.ensure_collection(collection_name=collection_name, dim=resolved_dim)
                _delete_changed_paths(
                    store,
                    collection_name=collection_name,
                    meta_dir=meta_dir,
                    removed_paths=removed_paths,
                    changed_files=files_to_chunk,
                    progress=progress,
                    mode=mode,
                )

        manifest_total_chunks: int | None = None
        if mode == "full":
            if _manifest_reusable(meta_dir, cfg, root_hash=root_hash, mode="full"):
                manifest_meta = json.loads((meta_dir / CHUNK_MANIFEST_META).read_text(encoding="utf-8"))
                manifest_total_chunks = int(manifest_meta["total_chunks"])
                progress(
                    {
                        "phase": "chunking_reused",
                        "mode": mode,
                        "total_files": len(files_to_chunk),
                        "total_chunks": manifest_total_chunks,
                        "manifest_path": str(meta_dir / CHUNK_MANIFEST),
                    }
                )
                chunks = _iter_manifest_chunks(meta_dir)
            else:
                chunks = _stream_chunk_manifest(
                    cfg,
                    meta_dir,
                    files=files_to_chunk,
                    mode=mode,
                    root_hash=root_hash,
                    progress=progress,
                )
            _stream_embed_and_upsert(
                cfg,
                meta_dir,
                chunks=chunks,
                total_chunks=manifest_total_chunks,
                mode=mode,
                dry_run=dry_run,
                cache=cache,
                emb=emb,
                model_name=model_name,
                embedding_signature=embedding_signature,
                store=store,
                collection_name=collection_name,
                progress=progress,
            )
        elif mode == "incremental" and (files_to_chunk or removed_paths):
            changed_chunks, manifest_total_chunks = _write_incremental_manifest(
                cfg,
                meta_dir,
                files=files_to_chunk,
                removed_paths=removed_paths,
                root_hash=root_hash,
                total_files=len(ts_files),
                progress=progress,
            )
            if changed_chunks:
                _stream_embed_and_upsert(
                    cfg,
                    meta_dir,
                    chunks=changed_chunks,
                    total_chunks=len(changed_chunks),
                    mode=mode,
                    dry_run=dry_run,
                    cache=cache,
                    emb=emb,
                    model_name=model_name,
                    embedding_signature=embedding_signature,
                    store=store,
                    collection_name=collection_name,
                    progress=progress,
                )
        else:
            manifest_meta = json.loads((meta_dir / CHUNK_MANIFEST_META).read_text(encoding="utf-8"))
            manifest_total_chunks = int(manifest_meta["total_chunks"])
            progress(
                {
                    "phase": "no_index_changes",
                    "mode": mode,
                    "total_files": len(ts_files),
                    "total_chunks": manifest_total_chunks,
                }
            )

        if manifest_total_chunks is None:
            manifest_meta = json.loads((meta_dir / CHUNK_MANIFEST_META).read_text(encoding="utf-8"))
            manifest_total_chunks = int(manifest_meta["total_chunks"])
        collection_audit: dict[str, object] = {"ok": True, "skipped": "dry_run"}
        if not dry_run:
            progress({"phase": "milvus_audit", "mode": mode, "collection": collection_name})
            collection_audit = _audit_collection(
                store,
                collection_name=collection_name,
                meta_dir=meta_dir,
            )
            if int(collection_audit.get("expected_chunks", -1)) != manifest_total_chunks:
                raise CollectionIntegrityError(
                    "collection audit does not match manifest metadata: "
                    f"audit={collection_audit.get('expected_chunks')} meta={manifest_total_chunks}"
                )
        merkle_save(meta_dir, current_tree)
        state = {
            "status": "done",
            "index_state_version": INDEX_STATE_VERSION,
            "root_hash": current_tree.get("hash", ""),
            "mode": mode,
            "total_files": len(ts_files),
            "total_chunks": manifest_total_chunks,
            "manifest_scope": MANIFEST_SCOPE,
            "embedding_model": model_name,
            "embedding_signature": embedding_signature,
            "index_fingerprint": index_fingerprint,
            "embedding_endpoints": _embedding_endpoint_stats(emb),
            "collection": collection_name,
            "collection_audit": collection_audit,
            **identity.to_dict(),
            "index_progress_path": str(meta_dir / "index_progress.json"),
            "chunks_manifest_path": str(meta_dir / CHUNK_MANIFEST),
        }
        _write_state(meta_dir, state)
        progress(
            {
                "phase": "done",
                "mode": mode,
                "total_files": len(ts_files),
                "total_chunks": manifest_total_chunks,
                "collection_audit": collection_audit,
                **identity.to_dict(),
                "embedding_endpoints": _embedding_endpoint_stats(emb),
            }
        )
        if dry_run:
            return (len(ts_files), manifest_total_chunks, resolved_dim)
        if full_rebuild_lock is not None:
            full_rebuild_lock.release()
        return None
    except Exception as exc:
        if full_rebuild_lock is not None:
            full_rebuild_lock.release()
        _write_state(
            meta_dir,
            {
                "status": "error",
                "phase": "index",
                "error_type": type(exc).__name__,
                "error": str(exc),
                **error_context,
            },
        )
        raise


def get_chunk_count(repo_root: str | Path) -> int | None:
    cfg = load_config(repo_root)
    collection_name = collection_identity(cfg.milvus.collection_prefix, cfg.repo_root).collection_name
    store = MilvusStore(
        host=cfg.milvus.host,
        port=cfg.milvus.port,
        metric=cfg.milvus.metric,
        index_type=cfg.milvus.index_type,
    )
    store.connect()
    return store.get_chunk_count(collection_name=collection_name)


def locate_hits(repo_root: str | Path, query: str, top_k: int = 10) -> list[SearchHit]:
    cfg = load_config(repo_root)
    emb = create_embedding_client(cfg)

    collection_name = collection_identity(cfg.milvus.collection_prefix, cfg.repo_root).collection_name
    store = MilvusStore(
        host=cfg.milvus.host,
        port=cfg.milvus.port,
        metric=cfg.milvus.metric,
        index_type=cfg.milvus.index_type,
    )
    store.connect()

    qv = emb.embed_texts([query])[0]
    return store.search(collection_name=collection_name, query_vector=qv, top_k=top_k)


def locate(repo_root: str | Path, query: str, top_k: int = 5) -> list[str]:
    hits = locate_hits(repo_root, query, top_k=top_k)
    return [f"{h.file_path}:{h.line_start}-{h.line_end} score={h.score}" for h in hits]
