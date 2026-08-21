# localization_engine/merkle.py
from __future__ import annotations

"""Merkle 树与索引同步（PLAN §5）。

- 叶子节点：单文件内容的 SHA-256 哈希，首版使用真实路径作为键。
- 非叶子节点：子节点哈希按路径排序后拼接再哈希。
- 持久化于 .codephoenix/merkle.json，用于增量比较。
"""

import json
from pathlib import Path

from .utils.hashing import sha256_bytes, sha256_text


def build_merkle_tree(repo_root: Path, files: list[Path]) -> dict:
    """根据当前跟踪文件列表构建 Merkle 树。

    叶子 = 文件（path 为绝对路径，hash 为文件内容哈希）；
    非叶子 = 目录（path 为相对路径或根路径，hash 为子节点哈希的组合）。
    子节点按 path 字符串排序后拼接哈希再哈希。
    """
    repo = repo_root.resolve()
    # 按相对路径分组：rel_parts -> 对应绝对 Path
    by_parts: dict[tuple[str, ...], Path] = {}
    for p in files:
        try:
            rel = p.resolve().relative_to(repo)
            parts = tuple(rel.parts)
            by_parts[parts] = p
        except ValueError:
            continue

    def node_hash(content: str) -> str:
        return sha256_text(content)

    def build_node(parts_prefix: tuple[str, ...]) -> dict:
        prefix_len = len(parts_prefix)
        children_nodes: list[dict] = []
        # 直接子：parts 长度为 prefix_len+1 且前缀匹配的，按最后一段分组
        next_segments: set[str] = set()
        for parts, abs_path in by_parts.items():
            if len(parts) <= prefix_len:
                continue
            if parts[:prefix_len] != parts_prefix:
                continue
            next_segments.add(parts[prefix_len])
        for seg in sorted(next_segments):
            key = parts_prefix + (seg,)
            if key in by_parts:
                # 叶子：文件
                abs_path = by_parts[key]
                try:
                    h = sha256_bytes(abs_path.read_bytes())
                except Exception:
                    h = ""
                children_nodes.append({"path": str(abs_path), "hash": h})
            else:
                # 子目录
                children_nodes.append(build_node(key))
        # 非叶子哈希：子节点按 path 排序后 hash 拼接再哈希
        children_nodes.sort(key=lambda n: n["path"])
        combined = "".join(n["hash"] for n in children_nodes)
        dir_hash = node_hash(combined) if combined else ""
        rel_path = "/".join(parts_prefix) if parts_prefix else str(repo)
        return {
            "path": rel_path,
            "hash": dir_hash,
            "children": children_nodes,
        }

    root = build_node(())
    root["path"] = str(repo)
    # 根哈希：所有子节点哈希拼接
    root["hash"] = node_hash("".join(n["hash"] for n in sorted(root["children"], key=lambda n: n["path"]))) if root["children"] else ""
    return root


def merkle_leaves(node: dict) -> dict[str, str]:
    """收集树中所有叶子节点的 path -> hash（用于与向量库一致使用真实路径）。"""
    out: dict[str, str] = {}
    if "children" not in node or not node["children"]:
        # 叶子
        out[node["path"]] = node["hash"]
        return out
    for ch in node["children"]:
        out.update(merkle_leaves(ch))
    return out


def merkle_save(meta_dir: Path, tree: dict) -> None:
    """将 Merkle 树写入 .codephoenix/merkle.json。"""
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "merkle.json").write_text(
        json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def merkle_load(meta_dir: Path) -> dict | None:
    """从 .codephoenix/merkle.json 读取；不存在或无效则返回 None。"""
    path = meta_dir / "merkle.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


def merkle_load_leaves(meta_dir: Path) -> dict[str, str] | None:
    """加载上一轮 Merkle 并返回叶子 path->hash。

    若文件为旧版扁平格式（path->hash 且无 "children"），直接返回该 dict；
    否则按树解析并返回 merkle_leaves(树)。
    """
    raw = merkle_load(meta_dir)
    if raw is None:
        return None
    # 兼容旧版：顶层为扁平 path->hash（无 "children"）
    if "children" not in raw and isinstance(raw.get("path"), str) is False:
        # 检查是否像扁平：所有 value 为字符串且像 hex
        try:
            if all(isinstance(k, str) and isinstance(v, str) and len(v) == 64 for k, v in raw.items()):
                return raw
        except Exception:
            pass
    if "children" in raw:
        return merkle_leaves(raw)
    # 旧版扁平：merkle.json 直接是 { "path1": "hash1", ... }
    if all(isinstance(v, str) for v in raw.values()):
        return raw
    return None
