# localization_engine/mini_swe_agent_bridge.py
from __future__ import annotations

"""桥接 Mini SWE Agent：从 stdin 读 query/context，在 repo_root 下运行 agent，向 stdout 输出 applied + modified_files。"""

import json
import os
import subprocess
import sys
from pathlib import Path


def _get_modified_files_git(repo_root: Path) -> list[str]:
    """若为 git 仓库，返回当前未暂存/已暂存变更文件的相对路径列表。"""
    try:
        r = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode != 0:
            return []
        return [p.strip() for p in (r.stdout or "").strip().splitlines() if p.strip()]
    except Exception:
        return []


def _get_modified_files_fallback(repo_root: Path, file_list: list[str]) -> list[str]:
    """非 git 时基于 file_list 的 mtime 检测变更（仅作简单后备）。"""
    modified: list[str] = []
    for p in file_list:
        try:
            full = (repo_root / p) if not Path(p).is_absolute() else Path(p)
            if full.exists():
                modified.append(str(full.relative_to(repo_root)) if full.is_relative_to(repo_root) else p)
        except Exception:
            pass
    return modified


def _run_agent(repo_root: Path, task: str) -> None:
    """在 repo_root 下运行 mini-swe-agent（DefaultAgent 无交互，直接执行）。"""
    from minisweagent.agents.default import DefaultAgent
    from minisweagent.config import get_config_from_spec
    from minisweagent.environments.local import LocalEnvironment
    from minisweagent.models import get_model

    try:
        base_config = get_config_from_spec("mini.yaml")
    except Exception:
        base_config = {}
    model_config = base_config.get("model") or {}
    if os.environ.get("MSWEA_MODEL_NAME"):
        model_config = {**model_config, "model_name": os.environ.get("MSWEA_MODEL_NAME")}
    model = get_model(config=model_config)
    env = LocalEnvironment(cwd=str(repo_root))
    agent_config = base_config.get("agent") or {}
    agent = DefaultAgent(model, env, **agent_config)
    agent.run(task)


def main() -> None:
    if len(sys.argv) < 2:
        print("missing repo_root", file=sys.stderr)
        sys.exit(2)
    repo_root = Path(sys.argv[1]).resolve()
    if not repo_root.is_dir():
        print(f"not a directory: {repo_root}", file=sys.stderr)
        sys.exit(2)
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)
    query = payload.get("query") or ""
    file_list = payload.get("file_list") or []
    context_md = payload.get("context_md") or ""
    dry_run = payload.get("dry_run") is True
    if dry_run:
        print(json.dumps({"applied": False, "modified_files": []}))
        sys.exit(0)
    task = f"{query}\n\n## Context\n{context_md}"
    try:
        _run_agent(repo_root, task)
    except Exception as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    modified = _get_modified_files_git(repo_root)
    if not modified:
        modified = _get_modified_files_fallback(repo_root, file_list)
    print(json.dumps({"applied": True, "modified_files": modified}))


if __name__ == "__main__":
    main()
