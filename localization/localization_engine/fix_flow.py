# localization_engine/fix_flow.py
from __future__ import annotations

"""端到端 fix 流程：定位 → 上下文收集 → Mini SWE Agent 生成 edit 并应用（或直接改文件）。"""

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any


# LLM 输出的结构化 edit 指令：每条为以下之一
# - {"action": "edit", "file_path": str, "start_line": int, "end_line": int, "new_content": str}
# - {"action": "edit", "file_path": str, "old_string": str, "new_string": str}
# - {"action": "write", "file_path": str, "content": str}


def _get_llm_client(repo_root: str | Path):
    from .config import load_config
    from .llm.client import ModelScopeLLMClient

    cfg = load_config(repo_root)
    if not (cfg.llm.api_key and cfg.llm.base_url and cfg.llm.model_name):
        raise RuntimeError(
            "Missing localization LLM config: set LOCALIZATION_ENGINE_LLM_API_KEY, "
            "LOCALIZATION_ENGINE_LLM_BASE_URL, and LOCALIZATION_ENGINE_LLM_MODEL."
        )
    return ModelScopeLLMClient(
        base_url=cfg.llm.base_url,
        access_token=cfg.llm.api_key,
        model_name=cfg.llm.model_name,
        endpoint_path=cfg.llm.endpoint_path,
        timeout_seconds=cfg.llm.timeout_seconds,
        max_retries=cfg.llm.max_retries,
        max_tokens=8192,
    )


def _write_scope_files(repo: Path, file_list: list[str]) -> None:
    """将定位到的文件列表写入 repo/.codephoenix/fix_scope_files.txt（相对路径，每行一个），供评估文件定位准确率。"""
    scope_dir = repo / ".codephoenix"
    scope_dir.mkdir(parents=True, exist_ok=True)
    rel_paths: list[str] = []
    repo_resolved = repo.resolve()
    for p in file_list:
        try:
            rel = Path(p).resolve().relative_to(repo_resolved)
            rel_paths.append(str(rel))
        except (ValueError, TypeError):
            rel_paths.append(p.strip() if isinstance(p, str) else str(p))
    (scope_dir / "fix_scope_files.txt").write_text("\n".join(rel_paths) + ("\n" if rel_paths else ""), encoding="utf-8")


def _extract_edits_json(llm_content: str) -> list[dict]:
    """从 LLM 回复中解析出 edits 数组（支持 ```json ... ``` 包裹）。"""
    content = llm_content.strip()
    # 尝试整体解析
    for raw in (content,):
        raw = raw.strip()
        if raw.startswith("["):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                pass
    # 尝试从 code block 中取
    for block in re.split(r"```\w*\n?", content):
        block = block.strip()
        if block.startswith("["):
            try:
                return json.loads(block)
            except json.JSONDecodeError:
                pass
    return []


# ---------------------------------------------------------------------------
# 修复执行器抽象：由 Mini SWE Agent 实现，输入 query + context，输出 edits 或 results
# ---------------------------------------------------------------------------

def _get_mini_swe_agent_cmd() -> str | None:
    """从环境变量读取 Mini SWE Agent 命令。未设置时返回 None。"""
    return (
        os.environ.get("LOCALIZATION_ENGINE_MINI_SWE_AGENT_CMD", "").strip()
        or os.environ.get("CODEPHOENIX_MINI_SWE_AGENT_CMD", "").strip()
        or None
    )


def run_fix_executor_mini_swe_agent(
    repo_root: Path,
    query: str,
    file_list: list[str],
    context_md: str,
    *,
    dry_run: bool = False,
    chunks: list[dict] | None = None,
) -> tuple[list[dict], list[dict[str, Any]] | None]:
    """调用 Mini SWE Agent，返回 (edits, results)。

    约定：
    - 环境变量 LOCALIZATION_ENGINE_MINI_SWE_AGENT_CMD：可执行命令（如 python -m localization_engine.mini_swe_agent_bridge）。
    - 子进程：传入参数为 repo_root；stdin 为 JSON：query, file_list, context_md, dry_run, chunks?。
    - 子进程 stdout 两种格式：
      1) JSON 数组 → edit 列表，返回 (edits, None)，由 run_fix 内 _apply_one_edit 应用；
      2) JSON 对象 {"applied": true, "modified_files": ["path", ...]} → agent 已改文件，返回 ([], results)。
    - 若 agent 未配置，抛出 RuntimeError。
    """
    cmd_str = _get_mini_swe_agent_cmd()
    if not cmd_str:
        raise RuntimeError(
            "Mini SWE Agent 未配置：请设置环境变量 LOCALIZATION_ENGINE_MINI_SWE_AGENT_CMD，"
            "或在本模块中实现/替换 run_fix_executor_mini_swe_agent。"
        )
    try:
        parts = shlex.split(cmd_str)
    except Exception as e:
        raise RuntimeError(f"LOCALIZATION_ENGINE_MINI_SWE_AGENT_CMD 解析失败: {e}") from e
    if not parts:
        raise RuntimeError("LOCALIZATION_ENGINE_MINI_SWE_AGENT_CMD 为空。")
    argv = parts + [str(repo_root)]
    payload: dict[str, Any] = {
        "query": query,
        "file_list": file_list,
        "context_md": context_md,
        "dry_run": dry_run,
    }
    if chunks is not None:
        payload["chunks"] = chunks
    try:
        proc = subprocess.run(
            argv,
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(repo_root),
            env={
                **os.environ,
                "LOCALIZATION_ENGINE_REPO_ROOT": str(repo_root),
                "CODEPHOENIX_REPO_ROOT": str(repo_root),
            },
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Mini SWE Agent 执行超时: {e}") from e
    except FileNotFoundError as e:
        raise RuntimeError(f"Mini SWE Agent 命令未找到: {argv[0]}: {e}") from e
    except Exception as e:
        raise RuntimeError(f"Mini SWE Agent 执行失败: {e}") from e
    raw = (proc.stdout or "").strip()
    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or raw
        raise RuntimeError(f"Mini SWE Agent 退出码 {proc.returncode}: {err}")

    # 已应用：{"applied": true, "modified_files": [...]}
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict) and "applied" in obj and "modified_files" in obj:
                if obj.get("applied") is True:
                    modified = obj.get("modified_files") or []
                    results = []
                    repo_resolved = repo_root.resolve()
                    for p in modified:
                        p_str = str(p).strip()
                        if not p_str:
                            continue
                        try:
                            full = (repo_resolved / p_str).resolve()
                            full.relative_to(repo_resolved)
                            results.append({"ok": True, "file_path": str(full)})
                        except (ValueError, TypeError):
                            results.append({"ok": True, "file_path": p_str})
                    return ([], results)
                # applied: false → 无修改
                return ([], [])
        except json.JSONDecodeError:
            pass

    # edit 数组
    edits = _extract_edits_json(raw)
    if not edits:
        raise RuntimeError(
            "Mini SWE Agent 未返回有效的 edit 数组或 applied 对象。请确保 stdout 输出 JSON 数组或 {\"applied\": true, \"modified_files\": [...]}。"
        )
    return (edits, None)


def _apply_one_edit(
    repo_root: Path,
    item: dict,
    dry_run: bool,
) -> dict[str, Any]:
    """应用单条 edit 指令，返回 { "ok": bool, "file_path"?: str, "error"?: str }。"""
    from .tools.edit import edit_file, write_file

    action = (item.get("action") or "").strip().lower()
    file_path = item.get("file_path")
    if not file_path:
        return {"ok": False, "error": "Missing file_path"}
    # 相对路径转绝对
    if not Path(file_path).is_absolute():
        file_path = str(repo_root / file_path)

    if action == "write":
        content = item.get("content")
        if content is None:
            return {"ok": False, "error": "write action missing content"}
        if dry_run:
            return {"ok": True, "file_path": file_path, "dry_run": True}
        return write_file(file_path=file_path, content=content, repo_root=str(repo_root))

    if action == "edit":
        if dry_run:
            return {"ok": True, "file_path": file_path, "dry_run": True}
        start_line = item.get("start_line")
        end_line = item.get("end_line")
        new_content = item.get("new_content")
        old_string = item.get("old_string")
        new_string = item.get("new_string")
        if start_line is not None and end_line is not None and new_content is not None:
            return edit_file(
                file_path=file_path,
                start_line=int(start_line),
                end_line=int(end_line),
                new_content=new_content,
                repo_root=str(repo_root),
            )
        if old_string is not None and new_string is not None:
            return edit_file(
                file_path=file_path,
                old_string=old_string,
                new_string=new_string,
                repo_root=str(repo_root),
            )
        return {"ok": False, "error": "edit action needs (start_line, end_line, new_content) or (old_string, new_string)"}

    return {"ok": False, "error": f"Unknown action: {action}"}


def _run_lint_fix_round(
    repo: Path,
    modified_paths: list[str],
    lint_errors: str,
    client: Any,
) -> tuple[list[dict], bool]:
    """根据 linter 报错让 LLM 生成修复 edit，应用并返回本轮 results。返回 (results, any_applied)。"""
    from .tools.edit import edit_file, write_file

    system = (
        "You are a code fix assistant. The linter/compiler reported errors in the modified files. "
        "Output ONLY a JSON array of edit instructions to fix these errors. "
        "Each instruction: {\"action\": \"edit\", \"file_path\": \"<path>\", \"start_line\": n, \"end_line\": n, \"new_content\": \"<text>\"} "
        "or {\"action\": \"edit\", \"file_path\": \"<path>\", \"old_string\": \"<exact>\", \"new_string\": \"<new>\"} "
        "or {\"action\": \"write\", \"file_path\": \"<path>\", \"content\": \"<full content>\"}. "
        "Use paths relative to repo root. Do not output any explanation, only the JSON array."
    )
    user = (
        "Modified files (paths):\n"
        + "\n".join(f"  - {p}" for p in modified_paths[:20])
        + "\n\nLinter/compiler output:\n```\n"
        + (lint_errors or "(no output)")[:8000]
        + "\n```\n\nOutput a JSON array of edit instructions to fix the compilation/lint errors."
    )
    response = client.chat([
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ])
    edits = _extract_edits_json(response)
    results: list[dict[str, Any]] = []
    for item in edits:
        if not isinstance(item, dict):
            results.append({"ok": False, "error": "Invalid edit item"})
            continue
        results.append(_apply_one_edit(repo, item, dry_run=False))
    return results, len(edits) > 0


def run_fix(
    repo_root: str | Path,
    query: str,
    *,
    no_ask: bool = False,
    dry_run: bool = False,
    confirm_before_apply: bool = False,
    run_test_after: str | None = None,
    lint_command: str | None = None,
    lint_fix_rounds: int = 2,
    top_k_chunks: int = 30,
    top_k_files: int = 10,
) -> dict[str, Any]:
    """执行 fix 最小闭环：locate-files → locate-chunks → context → Mini SWE Agent 生成 edits → 应用。

    可选：confirm_before_apply 应用前确认；run_test_after 应用后执行命令；
    lint_command 应用后运行以检查编译/静态检查（如 npx tsc --noEmit），若失败则由 LLM 根据报错生成修复 edit，最多 lint_fix_rounds 轮。
    返回 {"ok": bool, "file_list": list, "edits_count": int, "results": list, "error": str?, "lint_passed": bool?, "lint_result": dict?, "lint_fix_results": list?}。
    """
    from .context import build_fix_context, context_to_markdown
    from .locate_flow import get_files_to_modify, get_focus_chunks

    repo = Path(repo_root).resolve()
    if not repo.is_dir():
        return {"ok": False, "error": f"repo_root is not a directory: {repo}"}

    # 1) 文件列表
    file_list = get_files_to_modify(
        repo,
        query,
        ask=not no_ask,
        use_llm_filter=True,
        use_llm_dep_expansion=True,
        top_k_files=top_k_files,
    )
    if not file_list:
        return {"ok": False, "error": "No files to modify (locate-files returned empty)"}

    # 写出定位到的文件列表，供评估“文件定位准确率”使用（即使后续 edit 失败也保留）
    _write_scope_files(repo, file_list)
    print("[LocalizationEngine] 定位到的文件:", file=sys.stderr)
    for i, fp in enumerate(file_list, 1):
        print(f"  [{i}] {fp}", file=sys.stderr)

    # 2) 焦点 chunk + 上下文
    chunks = get_focus_chunks(repo, query, file_list, top_k_chunks=top_k_chunks, max_chunks_per_file=5)
    ctx = build_fix_context(
        repo,
        query,
        file_list,
        chunks,
        read_file_padding=2,
        include_glob_patterns=["*Test*", "*.spec.*", "*.test.*"],
        grep_symbols_from_chunks=True,
    )
    md = context_to_markdown(ctx)

    # 3) Mini SWE Agent 生成 edit 或直接改文件
    try:
        edits, results_pre = run_fix_executor_mini_swe_agent(
            repo,
            query,
            file_list,
            md,
            dry_run=dry_run,
            chunks=chunks,
        )
    except RuntimeError as e:
        return {
            "ok": False,
            "error": str(e),
            "file_list": file_list,
            "chunks_count": len(chunks),
        }

    # 可选：应用前 ask_user 确认（仅当有 edits 待应用时）
    if results_pre is None and edits and confirm_before_apply and not dry_run:
        from .tools.ask_user import ask_user
        answer = ask_user(
            question=f"Apply {len(edits)} edit(s)? A) Yes  B) No",
            options=["A", "B"],
        )
        if (answer or "").strip().upper() != "A":
            return {
                "ok": True,
                "file_list": file_list,
                "chunks_count": len(chunks),
                "edits_count": len(edits),
                "results": [],
                "error": None,
                "skipped_apply": True,
            }

    # 4) 应用：若 agent 已改文件（results_pre 非 None）则直接用；否则对 edits 执行 _apply_one_edit
    if results_pre is not None:
        results = results_pre
        edits_count = len(edits) if edits else len(results)
    else:
        results = []
        for item in edits:
            if not isinstance(item, dict):
                results.append({"ok": False, "error": "Invalid edit item (not dict)"})
                continue
            results.append(_apply_one_edit(repo, item, dry_run=dry_run))
        edits_count = len(edits)

    failed = [r for r in results if not r.get("ok")]
    modified_files = list(dict.fromkeys([r["file_path"] for r in results if r.get("ok") and r.get("file_path")]))
    print("[LocalizationEngine] 最后修改的文件:", file=sys.stderr)
    for i, fp in enumerate(modified_files, 1):
        print(f"  [{i}] {fp}", file=sys.stderr)
    out: dict[str, Any] = {
        "ok": len(failed) == 0,
        "file_list": file_list,
        "chunks_count": len(chunks),
        "edits_count": edits_count,
        "results": results,
        "error": failed[0].get("error") if failed else None,
    }

    # 修改后 LLM linter 验证：运行 lint_command，若报错则交给 LLM 生成修复 edit，重复直至通过或达上限
    modified_paths = []
    for r in results:
        if r.get("ok") and r.get("file_path"):
            p = r["file_path"]
            try:
                rel = Path(p).resolve().relative_to(repo)
                modified_paths.append(str(rel))
            except ValueError:
                modified_paths.append(p)
    modified_paths = list(dict.fromkeys(modified_paths))
    if lint_command and not dry_run and modified_paths:
        from .tools.run import terminal

        client = _get_llm_client(repo)
        lint_fix_results_all: list[dict[str, Any]] = []
        for lint_round in range(lint_fix_rounds + 1):
            tr = terminal(
                command=lint_command,
                cwd=str(repo),
                timeout_seconds=120,
                allowed_roots=[str(repo)],
            )
            out["lint_result"] = tr
            if tr.get("ok") and tr.get("exit_code") == 0:
                out["lint_passed"] = True
                out["lint_fix_rounds_used"] = lint_round
                if lint_fix_results_all:
                    out["lint_fix_results"] = lint_fix_results_all
                break
            out["lint_passed"] = False
            lint_errors = (tr.get("stderr") or "") + "\n" + (tr.get("stdout") or "")
            if lint_round < lint_fix_rounds:
                fix_results, applied = _run_lint_fix_round(
                    repo, modified_paths, lint_errors, client
                )
                lint_fix_results_all.append({"round": lint_round + 1, "results": fix_results})
                for r in fix_results:
                    if r.get("ok") and r.get("file_path"):
                        try:
                            rel = Path(r["file_path"]).resolve().relative_to(repo)
                            rel_str = str(rel)
                            if rel_str not in modified_paths:
                                modified_paths.append(rel_str)
                        except ValueError:
                            if r["file_path"] not in modified_paths:
                                modified_paths.append(r["file_path"])
                if not applied:
                    break
            else:
                out["lint_fix_rounds_used"] = lint_fix_rounds
                if lint_fix_results_all:
                    out["lint_fix_results"] = lint_fix_results_all
                # 要求通过 linter 但未通过，视为失败
                out["ok"] = False
                out["error"] = out.get("error") or "Lint/compile check failed after fix rounds."
                break

    # 可选：应用后执行 terminal 复查（如运行测试）
    if run_test_after and not dry_run and results:
        from .tools.run import terminal
        tr = terminal(
            command=run_test_after,
            cwd=str(repo),
            timeout_seconds=120,
            allowed_roots=[str(repo)],
        )
        out["terminal_result"] = tr

    return out
