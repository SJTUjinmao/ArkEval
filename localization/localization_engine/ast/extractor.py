from __future__ import annotations

"""TypeScript AST extraction via Node.js (ts-morph).

External interface (MVP):
- `extract_function_ranges(ts_file: Path, node_executable: str) -> list[dict]`

This wrapper calls the Node script under `localization_engine/ast/node/`.
"""

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ..utils.hashing import sha256_bytes


_AST_BATCH_SIZE = 128
_AST_BATCH_WORKERS = 2
_AST_SINGLE_TIMEOUT_SECONDS = 120
_AST_BATCH_TIMEOUT_SECONDS = 600
_GIT_TIMEOUT_SECONDS = 120
_EXPORT_MAP_CACHE_VERSION = 2


def _validate_dep_payload(payload: object, file_path: Path) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError(f"AST dependency extractor returned a non-object for {file_path}")
    if payload.get("ok") is not True:
        raise RuntimeError(
            f"AST dependency extractor failed for {file_path}: {payload.get('error') or 'unknown error'}"
        )
    out: dict[str, Any] = {}
    for key in ("imports", "exports", "typeRefs"):
        value = payload.get(key)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise RuntimeError(f"AST dependency extractor returned invalid {key} for {file_path}")
        out[key] = value
    return out


def extract_function_ranges(*, ts_file: Path, node_executable: str = "node") -> list[dict[str, Any]]:
    script = Path(__file__).parent / "node" / "extract_functions.mjs"
    cmd = [node_executable, str(script), str(ts_file)]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_AST_SINGLE_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"AST extractor failed: {proc.stderr.strip()}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"AST extractor returned non-JSON output: {proc.stdout[:2000]}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("functions"), list):
        raise RuntimeError("AST extractor returned an invalid function payload")
    return payload["functions"]


def _resolve_import_to_paths(
    importing_file: Path,
    specifier: str,
    repo_root: Path,
    extensions: tuple[str, ...] = (".ts", ".d.ts", ".ets"),
) -> list[Path]:
    """将相对 import specifier 解析为仓库内存在的文件路径（尝试多种扩展名）。"""
    base = (importing_file.parent / specifier).resolve()
    repo_root = repo_root.resolve()
    out: list[Path] = []
    if base.is_file():
        if repo_root in base.parents:
            out.append(base)
        return out
    for ext in extensions:
        p = base.with_name(base.name + ext)
        if p.is_file() and (repo_root in p.parents):
            out.append(p)
            break
    if not out and base.is_dir():
        for ext in extensions:
            p = base / f"index{ext}"
            if p.is_file() and (repo_root in p.parents):
                out.append(p)
                break
    return out


def _run_extract_deps(file_path: Path, node_executable: str = "node") -> dict[str, Any]:
    """调用 extract_deps.mjs，返回 { imports, exports, typeRefs }。"""
    script = Path(__file__).parent / "node" / "extract_deps.mjs"
    cmd = [node_executable, str(script), str(file_path)]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_AST_SINGLE_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"AST dependency extractor failed for {file_path}: {proc.stderr.strip()}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"AST dependency extractor returned non-JSON output for {file_path}: {proc.stdout[:2000]}"
        ) from exc
    return _validate_dep_payload(payload, file_path)


def _run_extract_deps_batch(
    file_paths: list[Path],
    node_executable: str = "node",
) -> dict[str, dict[str, Any]]:
    if not file_paths:
        return {}
    script = Path(__file__).parent / "node" / "extract_deps.mjs"
    cmd = [node_executable, str(script), "--stdin"]
    proc = subprocess.run(
        cmd,
        input=json.dumps([str(path) for path in file_paths], ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_AST_BATCH_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"AST dependency batch extractor failed: {proc.stderr.strip()}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"AST dependency batch extractor returned non-JSON output: {proc.stdout[:2000]}") from exc
    if len(file_paths) == 1:
        return {str(file_paths[0]): _validate_dep_payload(payload, file_paths[0])}
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise RuntimeError("AST dependency batch extractor returned an invalid result envelope")
    expected = {str(path): path for path in file_paths}
    out: dict[str, dict[str, Any]] = {}
    for item in payload["results"]:
        if not isinstance(item, dict):
            raise RuntimeError("AST dependency batch extractor returned a non-object item")
        path_text = str(item.get("path") or "")
        if path_text not in expected or path_text in out:
            raise RuntimeError(f"AST dependency batch extractor returned an unexpected path: {path_text}")
        out[path_text] = _validate_dep_payload(item, expected[path_text])
    missing = sorted(set(expected) - set(out))
    if missing:
        raise RuntimeError(f"AST dependency batch extractor omitted {len(missing)} input files")
    return out


def _extract_deps_many(
    file_paths: list[Path],
    node_executable: str = "node",
) -> dict[str, dict[str, Any]]:
    batches = [
        file_paths[start : start + _AST_BATCH_SIZE]
        for start in range(0, len(file_paths), _AST_BATCH_SIZE)
    ]
    if not batches:
        return {}
    out: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(_AST_BATCH_WORKERS, len(batches))) as pool:
        futures = {
            pool.submit(_run_extract_deps_batch, batch, node_executable): batch
            for batch in batches
        }
        for future in as_completed(futures):
            batch = futures[future]
            result = future.result()
            for path in batch:
                out[str(path)] = result[str(path)]
    return out


def _iter_ts_files_for_export_map(repo_root: Path) -> list[Path]:
    """枚举仓库内参与 export map 的 .ts / .ets / .d.ts 文件（与 get_repo_export_map 一致）。"""
    repo_root = repo_root.resolve()
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    if proc.returncode == 0:
        out = []
        seen: set[Path] = set()
        for raw in proc.stdout.split("\0"):
            if not raw:
                continue
            path = repo_root / raw
            if path.suffix in (".ts", ".ets") or path.name.endswith(".d.ts"):
                if path.is_file():
                    resolved = path.resolve()
                    if resolved not in seen:
                        seen.add(resolved)
                        out.append(resolved)
        return out
    raise RuntimeError(f"git ls-files failed for AST export map: {proc.stderr.strip()}")


def _file_content_hash(path: Path) -> str:
    """单文件内容 SHA256，用于增量判断是否变更。"""
    return sha256_bytes(path.read_bytes())


def _current_file_hashes(repo_root: Path, ts_files: list[Path]) -> dict[str, str]:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-s", "-z"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    tracked: dict[str, str] = {}
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-files -s failed for AST export map: {proc.stderr.strip()}")
    for record in proc.stdout.split("\0"):
        if not record or "\t" not in record:
            continue
        meta, raw_path = record.split("\t", 1)
        fields = meta.split()
        if len(fields) < 2:
            continue
        path = (repo_root / raw_path).resolve()
        tracked[str(path)] = f"git:{fields[1]}"

    out: dict[str, str] = {}
    untracked: list[Path] = []
    for path in ts_files:
        key = str(path)
        if key in tracked:
            out[key] = tracked[key]
        else:
            untracked.append(path)
    if untracked:
        with ThreadPoolExecutor(max_workers=min(4, len(untracked))) as pool:
            futures = {pool.submit(_file_content_hash, path): path for path in untracked}
            for future in as_completed(futures):
                path = futures[future]
                out[str(path)] = f"sha256:{future.result()}"
    return out


def get_repo_export_map(
    repo_root: Path,
    node_executable: str = "node",
    *,
    use_cache: bool = True,
    force_full: bool = False,
) -> dict[str, list[Path]]:
    """构建仓库内「符号 -> 导出该符号的文件路径列表」的映射，用于类型引用追踪。

    缓存与增量（类似 Merkle 思路）：
    - 缓存：repo_root/.codephoenix/cache/export_map.json（符号 -> 路径列表）
    - 元数据：repo_root/.codephoenix/cache/export_map_meta.json（file_hashes + file_exports）
    - 首次或 force_full 时全量构建；否则用 file_hashes 对比当前文件内容哈希，只对「变更/新增」文件
      重新跑 extract_deps，对「已移除」文件从 export_map 中剔除其贡献，再合并结果。
    - 删除上述两个缓存文件可强制下次全量重建。
    """
    repo_root = repo_root.resolve()
    cache_dir = repo_root / ".codephoenix" / "cache"
    cache_file = cache_dir / "export_map.json"
    meta_file = cache_dir / "export_map_meta.json"

    ts_files = _iter_ts_files_for_export_map(repo_root)
    current_paths = {str(p) for p in ts_files}

    def _current_hashes() -> dict[str, str]:
        return _current_file_hashes(repo_root, ts_files)

    # 尝试增量：有缓存且未强制全量时，加载元数据并做 diff
    if use_cache and not force_full and cache_file.is_file() and meta_file.is_file():
        try:
            export_map_raw = json.loads(cache_file.read_text(encoding="utf-8"))
            export_map = {k: [Path(p) for p in v] for k, v in export_map_raw.items()}
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if meta.get("schema_version") != _EXPORT_MAP_CACHE_VERSION:
                raise ValueError("stale AST export map cache schema")
            file_hashes: dict[str, str] = meta.get("file_hashes", {})
            file_exports: dict[str, list[str]] = meta.get("file_exports", {})
        except (json.JSONDecodeError, OSError, ValueError):
            export_map = {}
            file_hashes = {}
            file_exports = {}
        else:
            current_hashes = _current_hashes()
            removed = set(file_hashes) - current_paths
            changed_or_new: list[Path] = [
                p for p in ts_files
                if str(p) not in file_hashes or file_hashes[str(p)] != current_hashes.get(str(p), "")
            ]

            # 从 export_map 中移除「已删除或已变更」文件的贡献
            for path_str in removed | {str(p) for p in changed_or_new}:
                for sym in file_exports.get(path_str, []):
                    export_map[sym] = [q for q in export_map.get(sym, []) if str(q) != path_str]
                    if not export_map[sym]:
                        del export_map[sym]
                file_exports.pop(path_str, None)
                file_hashes.pop(path_str, None)

            # 仅对变更/新增文件跑 extract_deps 并合并
            if changed_or_new:
                extracted = _extract_deps_many(changed_or_new, node_executable)
                for fp in changed_or_new:
                    data = extracted.get(str(fp), {})
                    path_str = str(fp)
                    file_hashes[path_str] = current_hashes.get(path_str, "")
                    file_exports[path_str] = list(data.get("exports", []))
                    for name in data.get("exports", []):
                        export_map.setdefault(name, []).append(fp)

            if use_cache and (removed or changed_or_new):
                try:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    cache_file.write_text(
                        json.dumps({k: [str(p) for p in v] for k, v in export_map.items()}, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    meta_file.write_text(
                        json.dumps(
                            {
                                "schema_version": _EXPORT_MAP_CACHE_VERSION,
                                "hash_scheme": "git_blob_v1",
                                "file_hashes": file_hashes,
                                "file_exports": file_exports,
                            },
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                except OSError:
                    pass
            return export_map

    # 全量构建
    export_map: dict[str, list[Path]] = {}
    file_hashes: dict[str, str] = {}
    file_exports: dict[str, list[str]] = {}
    current_hashes = _current_hashes()
    extracted = _extract_deps_many(ts_files, node_executable)
    for fp in ts_files:
        data = extracted.get(str(fp), {})
        path_str = str(fp)
        file_hashes[path_str] = current_hashes.get(path_str, "")
        file_exports[path_str] = list(data.get("exports", []))
        for name in data.get("exports", []):
            export_map.setdefault(name, []).append(fp)

    if use_cache:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps({k: [str(p) for p in v] for k, v in export_map.items()}, ensure_ascii=False),
                encoding="utf-8",
            )
            meta_file.write_text(
                json.dumps(
                    {
                        "schema_version": _EXPORT_MAP_CACHE_VERSION,
                        "hash_scheme": "git_blob_v1",
                        "file_hashes": file_hashes,
                        "file_exports": file_exports,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass
    return export_map


def get_file_dependencies(
    repo_root: Path,
    file_path: Path,
    node_executable: str = "node",
    *,
    use_type_refs: bool = True,
) -> list[Path]:
    """获取单个文件依赖的、位于 repo_root 下的文件路径列表。
    包括：(1) 相对 import 解析；(2) 类型引用追踪（使用的类型符号在其定义文件中的位置）。
    """
    payload = _run_extract_deps(file_path, node_executable)
    specifiers = payload.get("imports", [])
    type_refs = payload.get("typeRefs", []) if use_type_refs else []
    repo_root = repo_root.resolve()
    file_path = file_path.resolve()
    seen: set[Path] = set()
    result: list[Path] = []

    for spec in specifiers:
        for p in _resolve_import_to_paths(file_path, spec, repo_root):
            p = p.resolve()
            if p not in seen and (repo_root in p.parents or p == repo_root):
                seen.add(p)
                result.append(p)

    if type_refs:
        export_map = get_repo_export_map(repo_root, node_executable)
        for name in type_refs:
            for def_path in export_map.get(name, []):
                def_path = def_path.resolve()
                if def_path == file_path or def_path in seen:
                    continue
                if repo_root in def_path.parents and def_path.is_file():
                    seen.add(def_path)
                    result.append(def_path)
    return result


def _dependencies_from_payload(
    repo_root: Path,
    file_path: Path,
    payload: dict[str, Any],
    export_map: dict[str, list[Path]],
) -> list[Path]:
    repo_root = repo_root.resolve()
    file_path = file_path.resolve()
    seen: set[Path] = set()
    result: list[Path] = []
    for spec in payload.get("imports", []):
        for path in _resolve_import_to_paths(file_path, spec, repo_root):
            path = path.resolve()
            if path not in seen and (repo_root in path.parents or path == repo_root):
                seen.add(path)
                result.append(path)
    for name in payload.get("typeRefs", []):
        for def_path in export_map.get(name, []):
            def_path = def_path.resolve()
            if def_path == file_path or def_path in seen:
                continue
            if repo_root in def_path.parents and def_path.is_file():
                seen.add(def_path)
                result.append(def_path)
    return result


def get_dependencies_for_files(
    repo_root: Path,
    file_paths: list[str] | list[Path],
    node_executable: str = "node",
) -> list[tuple[str, str]]:
    """返回 (依赖文件路径, 引用它的源文件路径) 列表；仅处理 .ts/.ets/.d.ts。"""
    repo_root = Path(repo_root).resolve()
    ext_ok = (".ts", ".ets")
    valid_paths: list[Path] = []
    for fp in file_paths:
        raw = Path(fp)
        p = (repo_root / raw) if not raw.is_absolute() else raw
        p = p.resolve()
        if p.suffix not in ext_ok and not p.name.endswith(".d.ts"):
            continue
        if not p.is_file() or (repo_root not in p.parents and p != repo_root):
            continue
        valid_paths.append(p)

    payloads = _extract_deps_many(valid_paths, node_executable)
    needs_export_map = any(payloads.get(str(path), {}).get("typeRefs") for path in valid_paths)
    export_map = get_repo_export_map(repo_root, node_executable) if needs_export_map else {}
    pairs: list[tuple[str, str]] = []
    for path in valid_paths:
        payload = payloads.get(str(path), {})
        for dep in _dependencies_from_payload(repo_root, path, payload, export_map):
            pairs.append((str(dep.resolve()), str(path)))
    return pairs
