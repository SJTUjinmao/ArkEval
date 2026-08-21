# localization_engine/locate_flow.py
from __future__ import annotations

"""阶段 C 前半：定位 → LLM 筛选 → ask_user（多轮，文案由 LLM 生成）→ 输出待修改文件。"""

import json
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .utils.hashing import sha256_text


_LLM_PREVIEW_LINES = 35
_LLM_PREVIEW_MAX_CHARS = 12000
_MILVUS_MAX_TOP_K = 16384


@dataclass(frozen=True)
class FileCandidate:
    rank: int
    file_path: str
    relative_path: str
    score: float


@dataclass(frozen=True)
class LocateResult:
    files: list[str]
    embedding_candidates: list[FileCandidate]


class LocalizationRetrievalError(RuntimeError):
    pass


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _append_jsonl_from_env(env_name: str, payload: dict[str, Any]) -> None:
    raw_path = os.environ.get(env_name, "").strip()
    if not raw_path:
        return
    path = Path(raw_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"created_at": _now_iso(), **payload}, ensure_ascii=False) + "\n")


def _trace_row(payload: dict[str, Any]) -> None:
    _append_jsonl_from_env("LOCALIZATION_ENGINE_ROW_TRACE_PATH", payload)


def _trace_llm(payload: dict[str, Any]) -> None:
    _append_jsonl_from_env("LOCALIZATION_ENGINE_LLM_TRACE_PATH", payload)


def _write_file_list(
    path_env: str,
    repo: Path,
    files: list[str],
    *,
    source: str,
    scores: dict[str, float] | None = None,
    model_name: str = "",
) -> None:
    raw_path = os.environ.get(path_env, "").strip()
    if not raw_path:
        return
    path = Path(raw_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            for rank, fp in enumerate(files, start=1):
                _, rel, _ = _current_repo_path(repo, fp)
                payload = {
                    "rank": rank,
                    "file_path": fp,
                    "relative_path": rel,
                    "source": source,
                }
                if model_name:
                    payload["model"] = model_name
                if scores and fp in scores:
                    payload["score"] = float(scores[fp])
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _load_embedding_candidates(path: Path, *, repo_root: Path, top_k_files: int) -> list[tuple[str, float]]:
    if not path.is_file():
        raise FileNotFoundError(f"embedding candidates reuse file not found: {path}")
    candidates: dict[str, tuple[str, float]] = {}
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            item = json.loads(line)
            relative_path = str(item.get("relative_path") or "").strip().replace("\\", "/")
            relative = Path(relative_path)
            if relative_path and not relative.is_absolute() and ".." not in relative.parts:
                file_path = str((repo_root / relative).resolve())
            else:
                file_path = str(item.get("file_path") or "").strip()
            if not file_path:
                continue
            absolute, _, key = _current_repo_path(repo_root, file_path)
            score = float(item.get("score", 0.0))
            previous = candidates.get(key)
            if previous is None or score > previous[1]:
                candidates[key] = (absolute, score)
    ordered = sorted(candidates.values(), key=lambda item: -item[1])[:top_k_files]
    if len(ordered) != top_k_files:
        raise RuntimeError(
            f"embedding candidate count mismatch after deduplication: expected={top_k_files} actual={len(ordered)} path={path}"
        )
    return ordered


def _should_use_localization_llm(repo_root: str | Path) -> bool:
    from .config import load_config

    cfg = load_config(repo_root)
    return bool(cfg.llm.api_key and cfg.llm.base_url and cfg.llm.model_name)


def _missing_llm_config_message() -> str:
    return "skip localization LLM: set LOCALIZATION_ENGINE_LLM_API_KEY, LOCALIZATION_ENGINE_LLM_BASE_URL, and LOCALIZATION_ENGINE_LLM_MODEL"


def _to_finite_score(value: object) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(score):
        return None
    return score


@lru_cache(maxsize=1)
def _git_tracked_path_cases(repo_root: str) -> dict[str, str]:
    repo = Path(repo_root)
    if not (repo / ".git").exists():
        return {}
    proc = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"failed to enumerate Git-tracked paths for {repo}: {detail}")
    tracked: dict[str, str] = {}
    ambiguous: set[str] = set()
    for raw in proc.stdout.split(b"\0"):
        if not raw:
            continue
        relative = raw.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        key = relative.casefold()
        previous = tracked.get(key)
        if previous is not None and previous != relative:
            tracked.pop(key, None)
            ambiguous.add(key)
        elif key not in ambiguous:
            tracked[key] = relative
    return tracked


def clear_git_tracked_path_cache() -> None:
    _git_tracked_path_cases.cache_clear()


def _current_repo_path(repo: Path, file_path: str | Path) -> tuple[str, str, str]:
    raw = Path(file_path)
    absolute = (repo / raw).resolve() if not raw.is_absolute() else raw.resolve()
    try:
        relative = absolute.relative_to(repo).as_posix()
    except ValueError as exc:
        raise RuntimeError(f"foreign localization hit: repo_root={repo} file_path={absolute}") from exc
    tracked_relative = _git_tracked_path_cases(str(repo)).get(relative.casefold())
    if tracked_relative is not None:
        relative = tracked_relative
        absolute = repo / Path(relative)
    if not absolute.is_file():
        raise RuntimeError(f"stale localization hit does not exist in current checkout: {absolute}")
    return str(absolute), relative, relative.casefold()


def _hit_matches_current_checkout(absolute: str, hit, line_cache: dict[str, list[str]]) -> bool:
    lines = line_cache.get(absolute)
    if lines is None:
        lines = Path(absolute).read_text(encoding="utf-8", errors="ignore").splitlines()
        line_cache[absolute] = lines
    start = int(getattr(hit, "line_start", 0))
    end = int(getattr(hit, "line_end", 0))
    if start < 1 or end < start or end > len(lines):
        return False
    chunk_text = "\n".join(lines[start - 1 : end])
    expected = sha256_text(f"{absolute}:{start}:{end}:{chunk_text}")
    return expected == str(getattr(hit, "chunk_hash", ""))


def _aggregate_file_scores(repo: Path, hits: list) -> dict[str, tuple[str, float]]:
    candidates: dict[str, tuple[str, float]] = {}
    stale_hits = 0
    line_cache: dict[str, list[str]] = {}
    for hit in hits:
        score = _to_finite_score(getattr(hit, "score", None))
        if score is None:
            continue
        try:
            absolute, _, key = _current_repo_path(repo, str(hit.file_path))
        except RuntimeError as exc:
            if str(exc).startswith("stale localization hit"):
                stale_hits += 1
                continue
            raise
        if not _hit_matches_current_checkout(absolute, hit, line_cache):
            stale_hits += 1
            continue
        previous = candidates.get(key)
        if previous is None or score > previous[1]:
            candidates[key] = (absolute, score)
    if stale_hits:
        _trace_row({"stage": "stale_hits_filtered", "stale_hit_count": stale_hits})
    return candidates


def _print_score_diagnostics(repo: Path, hits: list, file_scores: dict[str, float]) -> None:
    try:
        chunk_scores = [_to_finite_score(getattr(h, "score", None)) for h in hits]
        chunk_scores = [s for s in chunk_scores if s is not None]
        if chunk_scores:
            avg = sum(chunk_scores) / len(chunk_scores)
            print(
                "[定位] chunk 分数分布: count={} min={:.6f} max={:.6f} avg={:.6f}".format(
                    len(chunk_scores), min(chunk_scores), max(chunk_scores), avg
                ),
                file=sys.stderr,
            )

        ordered = sorted(file_scores.items(), key=lambda x: -x[1])
        if ordered:
            print("[定位] file 聚合分数 Top10:", file=sys.stderr)
            for i, (fp, sc) in enumerate(ordered[:10], 1):
                try:
                    rel = str(Path(fp).resolve().relative_to(repo))
                except ValueError:
                    rel = fp
                print("  [{}] {}  score={:.6f}".format(i, rel, sc), file=sys.stderr)
    except Exception:
        pass


def _read_preview(file_path: str, max_lines: int = _LLM_PREVIEW_LINES) -> str:
    try:
        head: list[str] = []
        truncated = False
        with Path(file_path).open("r", encoding="utf-8", errors="ignore") as handle:
            for index, line in enumerate(handle):
                if index >= max_lines:
                    truncated = True
                    break
                head.append(line.rstrip("\r\n"))
        if truncated:
            head.append("... (后续内容已省略)")
        return "\n".join(head)
    except Exception:
        return "(无法读取)"


def _parse_llm_json_array(llm_content: str) -> tuple[bool, list[object]]:
    content = llm_content.strip()
    parts = [content]
    if "```" in content:
        parts.extend(re.split(r"```\w*\n?", content))
    for part in parts:
        part = part.strip()
        if not part.startswith("["):
            continue
        try:
            value = json.loads(part)
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            return True, value
    return False, []


def _parse_llm_selected_paths(llm_content: str, candidate_paths: list[str]) -> tuple[bool, list[str]]:
    parsed, values = _parse_llm_json_array(llm_content)
    if not parsed:
        return False, []
    candidate_set = set(candidate_paths)
    if any(not isinstance(value, str) or value not in candidate_set for value in values):
        return False, []
    out: list[str] = []
    for value in values:
        if value not in out:
            out.append(value)
    return True, out


def _parse_llm_path_list(llm_content: str, candidate_paths: list[str]) -> list[str]:
    return _parse_llm_selected_paths(llm_content, candidate_paths)[1]


def filter_files_by_llm(
    repo_root: str | Path,
    query: str,
    file_list: list[str],
    *,
    use_preview: bool = True,
) -> list[str]:
    from .config import load_config
    from .llm.client import ModelScopeLLMClient

    cfg = load_config(repo_root)
    if not _should_use_localization_llm(repo_root):
        raise RuntimeError(_missing_llm_config_message())
    client = ModelScopeLLMClient(
        base_url=cfg.llm.base_url,
        access_token=cfg.llm.api_key,
        model_name=cfg.llm.model_name,
        endpoint_path=cfg.llm.endpoint_path,
        timeout_seconds=cfg.llm.timeout_seconds,
        max_retries=cfg.llm.max_retries,
        max_tokens=cfg.llm.max_tokens,
    )
    path_to_preview: dict[str, str] = {}
    if use_preview:
        total_chars = 0
        for fp in file_list:
            if total_chars >= _LLM_PREVIEW_MAX_CHARS:
                path_to_preview[fp] = "(内容过长已省略)"
                continue
            prev = _read_preview(fp)
            path_to_preview[fp] = prev
            total_chars += len(prev)
    per_file_cap = max(800, _LLM_PREVIEW_MAX_CHARS // max(1, len(file_list)))
    blocks = []
    for i, fp in enumerate(file_list, 1):
        block = f"文件 {i}: {fp}"
        if fp in path_to_preview:
            prev = path_to_preview[fp]
            if len(prev) > per_file_cap:
                prev = prev[:per_file_cap] + "\n...(已截断)"
            block += "\n```\n" + prev + "\n```"
        blocks.append(block)
    files_section = "\n\n".join(blocks)
    user_content = (
        "用户需求：\n"
        f"{query}\n\n"
        "以下是语义检索得到的候选文件（路径 + 内容预览）。请判断其中哪些文件**需要被修改**才能满足上述需求。\n"
        "只输出一个 JSON 数组，元素为需要修改的文件的**完整路径**，与上面列出的完全一致。必须至少选择一个最可能需要修改的文件，禁止输出空数组。不要输出其他解释。\n\n"
        f"{files_section}"
    )
    messages = [{"role": "user", "content": user_content}]
    started_at = _now_iso()
    t0 = time.time()
    _trace_row(
        {
            "stage": "llm_filter_start",
            "candidate_files_count": len(file_list),
            "prompt_chars": len(user_content),
            "model": cfg.llm.model_name,
        }
    )
    response = ""
    filtered: list[str] = []
    for selection_attempt in range(1, 4):
        response = client.chat(messages)
        parsed, filtered = _parse_llm_selected_paths(response, file_list)
        if parsed and filtered:
            break
        _trace_llm(
            {
                "stage": "llm_filter_empty",
                "started_at": started_at,
                "model": cfg.llm.model_name,
                "selection_attempt": selection_attempt,
                "candidate_files_count": len(file_list),
                "prompt_chars": len(user_content),
                "response_chars": len(response),
                "response": response,
                "parse_valid": parsed,
            }
        )
        _trace_row(
            {
                "stage": "llm_filter_empty_retry",
                "selection_attempt": selection_attempt,
                "candidate_files_count": len(file_list),
            }
        )
    elapsed = round(time.time() - t0, 3)
    if not filtered:
        raise RuntimeError("LLM filter returned no valid file after 3 attempts")
    _trace_llm(
        {
            "stage": "llm_filter",
            "started_at": started_at,
            "elapsed_seconds": elapsed,
            "model": cfg.llm.model_name,
            "candidate_files_count": len(file_list),
            "prompt_chars": len(user_content),
            "response_chars": len(response),
            "parsed_files_count": len(filtered),
            "response": response,
        }
    )
    _trace_row(
        {
            "stage": "llm_filter_done",
            "elapsed_seconds": elapsed,
            "candidate_files_count": len(file_list),
            "parsed_files_count": len(filtered),
        }
    )
    return filtered


def _parse_llm_add_deps(llm_content: str, candidate_dep_paths: list[str]) -> list[str]:
    return _parse_llm_selected_paths(llm_content, candidate_dep_paths)[1]


def ask_llm_to_add_deps(
    repo_root: str | Path,
    query: str,
    core_files: list[str],
    dep_pairs: list[tuple[str, str]],
) -> list[str]:
    core_set = set(core_files)
    dep_to_importers: dict[str, list[str]] = {}
    for dep_path, imported_by in dep_pairs:
        if dep_path in core_set:
            continue
        if dep_path not in dep_to_importers:
            dep_to_importers[dep_path] = []
        if imported_by not in dep_to_importers[dep_path]:
            dep_to_importers[dep_path].append(imported_by)
    candidate_deps = list(dep_to_importers.keys())
    if not candidate_deps:
        _trace_row(
            {
                "stage": "llm_dep_expansion_skipped",
                "reason": "no_candidate_deps",
                "dep_pairs_count": len(dep_pairs),
                "core_files_count": len(core_files),
            }
        )
        return []

    from .config import load_config
    from .llm.client import ModelScopeLLMClient

    cfg = load_config(repo_root)
    if not _should_use_localization_llm(repo_root):
        raise RuntimeError(_missing_llm_config_message())
    client = ModelScopeLLMClient(
        base_url=cfg.llm.base_url,
        access_token=cfg.llm.api_key,
        model_name=cfg.llm.model_name,
        endpoint_path=cfg.llm.endpoint_path,
        timeout_seconds=cfg.llm.timeout_seconds,
        max_retries=cfg.llm.max_retries,
        max_tokens=cfg.llm.max_tokens,
    )
    lines = [
        f"- {dep}（被以下核心文件引用：{', '.join(dep_to_importers[dep])}）"
        for dep in candidate_deps
    ]
    deps_section = "\n".join(lines)
    user_content = (
        "用户需求：\n"
        f"{query}\n\n"
        "当前已确定需要修改的核心文件：\n"
        + "\n".join(f"- {f}" for f in core_files)
        + "\n\n"
        "以下是上述核心文件的**依赖文件**（由 import 等得到），它们可能未被语义检索命中，但随核心文件修改可能也需要修改（如类型定义、接口等）。\n"
        "请判断其中哪些依赖文件也应加入待修改列表。只输出一个 JSON 数组，元素为要**新增**的文件的完整路径，与下面列表完全一致。若都不需要则输出 []。不要输出其他解释。\n\n"
        f"依赖文件列表：\n{deps_section}"
    )
    messages = [{"role": "user", "content": user_content}]
    started_at = _now_iso()
    t0 = time.time()
    _trace_row(
        {
            "stage": "llm_dep_expansion_start",
            "core_files_count": len(core_files),
            "dep_pairs_count": len(dep_pairs),
            "candidate_deps_count": len(candidate_deps),
            "prompt_chars": len(user_content),
            "model": cfg.llm.model_name,
        }
    )
    response = ""
    added: list[str] = []
    parsed = False
    for selection_attempt in range(1, 4):
        response = client.chat(messages)
        parsed, added = _parse_llm_selected_paths(response, candidate_deps)
        if parsed:
            break
        _trace_llm(
            {
                "stage": "llm_dep_expansion_invalid",
                "started_at": started_at,
                "model": cfg.llm.model_name,
                "selection_attempt": selection_attempt,
                "candidate_deps_count": len(candidate_deps),
                "prompt_chars": len(user_content),
                "response_chars": len(response),
                "response": response,
            }
        )
        _trace_row(
            {
                "stage": "llm_dep_expansion_invalid_retry",
                "selection_attempt": selection_attempt,
                "candidate_deps_count": len(candidate_deps),
            }
        )
    if not parsed:
        raise RuntimeError("LLM dependency expansion returned invalid JSON after 3 attempts")
    elapsed = round(time.time() - t0, 3)
    _trace_llm(
        {
            "stage": "llm_dep_expansion",
            "started_at": started_at,
            "elapsed_seconds": elapsed,
            "model": cfg.llm.model_name,
            "core_files_count": len(core_files),
            "dep_pairs_count": len(dep_pairs),
            "candidate_deps_count": len(candidate_deps),
            "prompt_chars": len(user_content),
            "response_chars": len(response),
            "parsed_added_files_count": len(added),
            "response": response,
        }
    )
    _trace_row(
        {
            "stage": "llm_dep_expansion_done",
            "elapsed_seconds": elapsed,
            "core_files_count": len(core_files),
            "dep_pairs_count": len(dep_pairs),
            "candidate_deps_count": len(candidate_deps),
            "parsed_added_files_count": len(added),
        }
    )
    return added


def _get_llm_client(repo_root: str | Path):
    from .config import load_config
    from .llm.client import ModelScopeLLMClient

    cfg = load_config(repo_root)
    if not _should_use_localization_llm(repo_root):
        raise RuntimeError(_missing_llm_config_message())
    return ModelScopeLLMClient(
        base_url=cfg.llm.base_url,
        access_token=cfg.llm.api_key,
        model_name=cfg.llm.model_name,
        endpoint_path=cfg.llm.endpoint_path,
        timeout_seconds=cfg.llm.timeout_seconds,
        max_retries=cfg.llm.max_retries,
        max_tokens=cfg.llm.max_tokens,
    )


def ask_llm_for_ask_prompt(
    repo_root: str | Path,
    query: str,
    file_list: list[str],
    round_no: int,
    *,
    initial_selected_files: list[str] | None = None,
    llm_added_dep_files: list[str] | None = None,
    last_round_answer: str | None = None,
    last_round_extra: str | None = None,
) -> dict:
    """由配置的 LLM（如 ModelScope Qwen3 Coder）生成 ask 阶段全部文案，无硬编码。
    返回 {"display_text": str, "prompt_other": str}。display_text 需包含：轮次说明、需求、文件列表、
    1～3 个问题或任务、以及以 A)、B)、C) 开头的三个选项；prompt_other 为用户选 C 时的追问。
    initial_selected_files：检索/筛选阶段选出的文件；llm_added_dep_files：LLM 依赖分析后建议新增的文件（首轮展示用）。
    last_round_answer / last_round_extra：上一轮用户选择与补充，供 LLM 生成下一轮文案。
    """
    client = _get_llm_client(repo_root)
    file_list_text = "\n".join(f"  - {f}" for f in file_list[:30])
    user_content = (
        "你正在协助工程师确认「待修改文件列表」。\n"
        f"当前是第 {round_no} 轮确认。\n"
        f"用户需求：{query}\n\n"
    )
    if initial_selected_files is not None and llm_added_dep_files is not None:
        user_content += (
            "【检索/筛选阶段选出的文件】\n"
            + "\n".join(f"  - {f}" for f in initial_selected_files[:30])
            + "\n\n"
        )
        if llm_added_dep_files:
            user_content += (
                "【你（LLM）根据依赖分析建议新增的修改文件】\n"
                + "\n".join(f"  - {f}" for f in llm_added_dep_files[:30])
                + "\n\n"
            )
    user_content += (
        f"【当前待修改文件列表（共 {len(file_list)} 个，供工程师确认）】\n{file_list_text}\n\n"
    )
    if last_round_answer:
        user_content += (
            f"【上一轮工程师的选择】{last_round_answer}。\n"
        )
        if last_round_extra:
            user_content += f"【上一轮选 C 时的补充内容】「{last_round_extra}」。当前文件列表已根据该补充更新。\n"
        user_content += "请根据上述反馈生成本轮的确认文案。\n\n"
    user_content += (
        "请生成两段内容，用严格 JSON 输出，不要其他解释：\n"
        "1) display_text：一段**完整**的给工程师看的提示文案。"
        "若有「检索选出的文件」与「你建议新增的依赖文件」两段，请先分别说明再给出当前完整列表；"
        "然后写出你想问工程师的问题或需要其确认的信息，以及三个选项，分别以 A)、B)、C) 开头——"
        "A 表示确认当前列表并继续下一步修复，B 表示取消不修改，C 表示其他（补充说明或补充文件路径）。"
        "最后加一句提示工程师输入 A、B 或 C。\n"
        "2) prompt_other：当工程师选择 C 时，你要追问他的那一句话（例如请其输入补充文件路径或说明）。\n"
        '输出格式：{"display_text": "...", "prompt_other": "..."}'
    )
    messages = [{"role": "user", "content": user_content}]
    response = client.chat(messages)
    content = response.strip()
    for raw in (content, *re.split(r"```\w*\n?", content)):
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                display = (obj.get("display_text") or "").strip()
                prompt_other = (obj.get("prompt_other") or "").strip()
                if display:
                    return {"display_text": display, "prompt_other": prompt_other or "Optional input: "}
        except json.JSONDecodeError:
            continue
    return {
        "display_text": f"Round {round_no}. Files:\n{file_list_text}\n\nA) Confirm  B) Cancel  C) Other. Reply A, B or C.",
        "prompt_other": "Optional input: ",
    }


def _tiered_top_k_hits(n: int) -> int:
    if n <= 100:
        return n
    if n <= 500:
        return int(100 + (n - 100) * 0.2)
    if n <= 2000:
        return int(180 + (n - 500) * 0.2)
    return int(480 + (n - 2000) * 0.2)


def _clamp_top_k_hits(value: int) -> int:
    return max(1, min(_MILVUS_MAX_TOP_K, int(value)))


def _resolve_top_k_hits(repo_root: Path, explicit_top_k_hits: int | None) -> int:
    if explicit_top_k_hits is not None and explicit_top_k_hits > 0:
        return _clamp_top_k_hits(explicit_top_k_hits)
    from .indexer import get_chunk_count

    total = get_chunk_count(repo_root)
    if total is None or total <= 0:
        return 50
    return _clamp_top_k_hits(_tiered_top_k_hits(total))


def get_focus_chunks(
    repo_root: str | Path,
    query: str,
    file_list: list[str],
    *,
    top_k_chunks: int = 30,
    max_chunks_per_file: int = 5,
    top_k_hits_retrieve: int | None = None,
) -> list[dict]:
    """阶段三细粒度定位：在已定位的问题文件内做语义检索，返回需重点关注的 chunk 列表。

    供后续 read_file(offset/limit) 精读、grep 查调用链、glob 发现相关文件等补充上下文使用。
    返回 list[dict]，每项含 file_path, line_start, line_end, score。
    """
    from .indexer import locate_hits

    repo = Path(repo_root).resolve()
    file_set = {Path(f).resolve() for f in file_list}
    file_set_str = {str(p) for p in file_set}
    if not file_set_str:
        return []

    k_retrieve = top_k_hits_retrieve or min(200, max(50, len(file_set) * 20))
    hits = locate_hits(repo, query, top_k=k_retrieve)
    normalized_hits = []
    line_cache: dict[str, list[str]] = {}
    for hit in hits:
        absolute, _, _ = _current_repo_path(repo, str(hit.file_path))
        if not _hit_matches_current_checkout(absolute, hit, line_cache):
            continue
        normalized_hits.append((hit, absolute))
    in_scope = [(hit, absolute) for hit, absolute in normalized_hits if absolute in file_set_str]
    per_file: dict[str, list] = {}
    for hit, absolute in in_scope:
        per_file.setdefault(absolute, []).append(hit)
    out: list[dict] = []
    for fp in sorted(per_file.keys(), key=lambda x: -max(h.score for h in per_file[x])):
        for h in sorted(per_file[fp], key=lambda x: -x.score)[:max_chunks_per_file]:
            out.append({
                "file_path": str(h.file_path),
                "line_start": int(h.line_start),
                "line_end": int(h.line_end),
                "score": float(h.score),
            })
            if len(out) >= top_k_chunks:
                break
        if len(out) >= top_k_chunks:
            break
    out = out[:top_k_chunks]
    return out


def get_file_ranking_by_score(
    repo_root: str | Path,
    query: str,
    *,
    top_k_files: int = 10,
    top_k_hits: int | None = None,
) -> list[tuple[str, float]]:
    """仅做语义检索 + 按文件聚合得分，返回 [(file_path, score), ...] 按 score 降序。不跑 LLM 筛选与 ask。"""
    from .indexer import locate_hits

    repo = Path(repo_root).resolve()
    resolved_hits = _resolve_top_k_hits(repo, top_k_hits)
    while True:
        hits = locate_hits(repo, query, top_k=resolved_hits)
        candidates = _aggregate_file_scores(repo, hits)
        if len(candidates) >= top_k_files or resolved_hits >= _MILVUS_MAX_TOP_K or len(hits) < resolved_hits:
            break
        resolved_hits = min(_MILVUS_MAX_TOP_K, resolved_hits * 2)
    file_scores = {absolute: score for absolute, score in candidates.values()}

    _print_score_diagnostics(repo, hits, file_scores)

    ordered = sorted(file_scores.items(), key=lambda x: -x[1])[:top_k_files]
    if len(ordered) != top_k_files:
        raise RuntimeError(
            f"embedding candidate count mismatch after deduplication: expected={top_k_files} actual={len(ordered)} repo_root={repo}"
        )
    return [(fp, sc) for fp, sc in ordered]


def get_files_to_modify(
    repo_root: str | Path,
    query: str,
    *,
    top_k_files: int = 10,
    top_k_hits: int | None = None,
    ask: bool = True,
    use_llm_filter: bool = True,
    use_llm_dep_expansion: bool = True,
) -> list[str]:
    """理想完整工作流：① 定位引擎检索选出文件 → ② 选出文件与其依赖交给 LLM 判断是否增加依赖修改
    → ③ LLM 输出「建议新增的依赖文件 + 之前选中的文件」→ ④ LLM 生成想问用户的问题与需确认信息（三个选项）
    → ⑤ 与用户多轮交互直到用户确认待修改文件无误 → 返回 file_list，供下一步修复使用。
    """
    from .ast.extractor import get_dependencies_for_files
    from .indexer import locate_hits
    from .tools.ask_user import ask_user

    repo = Path(repo_root).resolve()
    reuse_candidates_path = os.environ.get("LOCALIZATION_ENGINE_REUSE_EMBEDDING_CANDIDATES", "").strip()
    if reuse_candidates_path:
        ordered = _load_embedding_candidates(Path(reuse_candidates_path), repo_root=repo, top_k_files=top_k_files)
        _write_file_list(
            "LOCALIZATION_ENGINE_EMBEDDING_CANDIDATES_PATH",
            repo,
            [fp for fp, _ in ordered],
            source="embedding_reused",
            scores={fp: score for fp, score in ordered},
        )
        _trace_row(
            {
                "stage": "embedding_candidates_reused",
                "path": reuse_candidates_path,
                "candidate_files_count": len(ordered),
            }
        )
    else:
        try:
            resolved_hits = _resolve_top_k_hits(repo, top_k_hits)
            hits = locate_hits(repo, query, top_k=resolved_hits)
        except Exception as exc:
            raise LocalizationRetrievalError(f"localization retrieval failed: {exc}") from exc
        candidates = _aggregate_file_scores(repo, hits)
        file_scores = {absolute: score for absolute, score in candidates.values()}

        _print_score_diagnostics(repo, hits, file_scores)

        ordered = sorted(file_scores.items(), key=lambda x: -x[1])[:top_k_files]
        if len(ordered) != top_k_files:
            raise RuntimeError(
                f"embedding candidate count mismatch after deduplication: expected={top_k_files} actual={len(ordered)} repo_root={repo}"
            )
        _write_file_list(
            "LOCALIZATION_ENGINE_EMBEDDING_CANDIDATES_PATH",
            repo,
            [fp for fp, _ in ordered],
            source="embedding",
            scores={fp: score for fp, score in ordered},
        )
        candidates_path = os.environ.get("LOCALIZATION_ENGINE_EMBEDDING_CANDIDATES_PATH", "").strip()
        if candidates_path:
            _trace_row(
                {
                    "stage": "embedding_candidates_written",
                    "path": candidates_path,
                    "candidate_files_count": len(ordered),
                }
            )
    file_list = [fp for fp, _ in ordered]
    initial_selected_files: list[str] = list(file_list)

    # 日志：根据 embedding chunk 选择出的结果
    try:
        rel_ordered = []
        for fp, sc in ordered:
            try:
                rel = Path(fp).resolve().relative_to(repo)
            except ValueError:
                rel = fp
            rel_ordered.append((str(rel), sc))
        print("[定位] 根据 embedding chunk 选择出的结果 (共 {} 个):".format(len(rel_ordered)), file=sys.stderr)
        for i, (rel, sc) in enumerate(rel_ordered, 1):
            print("  [{}] {}  score={:.4f}".format(i, rel, sc), file=sys.stderr)
        if rel_ordered and all(abs(sc) < 1e-12 for _, sc in rel_ordered):
            print(
                "[定位] 提示: 所有 file score 接近 0，可能是 embedding 异常、Milvus 返回分数字段缺失，或向量构建退化。请结合 chunk/file 分数分布日志排查。",
                file=sys.stderr,
            )
    except Exception:
        pass

    if not file_list:
        return []

    llm_core_files: list[str] = []
    llm_model_name = ""
    if use_llm_filter:
        if not _should_use_localization_llm(repo):
            raise RuntimeError(_missing_llm_config_message())
        if _should_use_localization_llm(repo):
            from .config import load_config

            llm_model_name = load_config(repo).llm.model_name
            llm_core_files = filter_files_by_llm(repo, query, file_list, use_preview=True)
            file_list = list(llm_core_files)
        else:
            use_llm_filter = False
            print(f"[定位] {_missing_llm_config_message()}", file=sys.stderr)
        _write_file_list(
            "LOCALIZATION_ENGINE_LLM_CORE_FILES_PATH",
            repo,
            llm_core_files,
            source="llm_filter",
            model_name=llm_model_name,
        )
        try:
            rel_list = []
            for fp in llm_core_files:
                try:
                    rel_list.append(str(Path(fp).resolve().relative_to(repo)))
                except ValueError:
                    rel_list.append(fp)
            print("[定位] 大模型筛选后的核心文件 (共 {} 个):".format(len(rel_list)), file=sys.stderr)
            for i, rel in enumerate(rel_list, 1):
                print("  [{}] {}".format(i, rel), file=sys.stderr)
        except Exception:
            pass

    # 理想工作流：检索选出的文件 + 其依赖交给 LLM，LLM 判断是否增加依赖文件，输出「新增的修改文件 + 之前选中的文件」给用户确认
    llm_added_dep_files: list[str] = []
    if use_llm_dep_expansion:
        if not _should_use_localization_llm(repo):
            raise RuntimeError(_missing_llm_config_message())
        if _should_use_localization_llm(repo):
            if not llm_model_name:
                from .config import load_config

                llm_model_name = load_config(repo).llm.model_name
            ast_started = time.time()
            _trace_row(
                {
                    "stage": "ast_dependency_analysis_start",
                    "core_files_count": len(file_list),
                }
            )
            dep_pairs = get_dependencies_for_files(repo, file_list)
            _trace_row(
                {
                    "stage": "ast_dependency_analysis_done",
                    "elapsed_seconds": round(time.time() - ast_started, 3),
                    "core_files_count": len(file_list),
                    "dep_pairs_count": len(dep_pairs),
                }
            )
            added = ask_llm_to_add_deps(repo, query, file_list, dep_pairs)
            llm_added_dep_files = list(added)
            for p in added:
                if p not in file_list:
                    file_list.append(p)
        else:
            use_llm_dep_expansion = False
            print(f"[定位] {_missing_llm_config_message()}", file=sys.stderr)
        _write_file_list(
            "LOCALIZATION_ENGINE_LLM_DEP_FILES_PATH",
            repo,
            llm_added_dep_files,
            source="llm_dep_expansion",
            model_name=llm_model_name,
        )
        # 日志：大模型补充依赖后的文件
        try:
            rel_list = []
            for fp in file_list:
                try:
                    rel_list.append(str(Path(fp).resolve().relative_to(repo)))
                except ValueError:
                    rel_list.append(fp)
            print("[定位] 大模型补充依赖后的文件 (共 {} 个):".format(len(rel_list)), file=sys.stderr)
            for i, rel in enumerate(rel_list, 1):
                print("  [{}] {}".format(i, rel), file=sys.stderr)
        except Exception:
            pass

    if ask:
        from .tools.ask_user import read_line_from_terminal

        max_ask_rounds = 5
        prompt_other = ""
        last_answer: str | None = None
        last_extra: str | None = None
        for round_no in range(max_ask_rounds):
            qot = ask_llm_for_ask_prompt(
                repo,
                query,
                file_list,
                round_no + 1,
                initial_selected_files=initial_selected_files,
                llm_added_dep_files=llm_added_dep_files if round_no == 0 else None,
                last_round_answer=last_answer,
                last_round_extra=last_extra,
            )
            display_text = qot.get("display_text", "")
            prompt_other = qot.get("prompt_other", "")

            print("\n" + display_text.strip() + "\n", flush=True)
            answer = read_line_from_terminal("请选择 (A/B/C): ").strip().upper()[:1]
            last_answer = answer or None
            last_extra = None

            if answer == "B":
                return []
            if answer == "A":
                return file_list
            if answer == "C":
                extra = read_line_from_terminal((prompt_other or "Optional input: ").strip() + " ").strip()
                last_extra = extra if extra else None
                if extra:
                    for raw in re.split(r"[,，\s]+", extra):
                        raw = raw.strip().strip('"')
                        if not raw:
                            continue
                        p = Path(raw)
                        if not p.is_absolute():
                            p = (repo / raw).resolve()
                        else:
                            p = p.resolve()
                        if p.is_file() and (repo in p.parents or p == repo) and str(p) not in file_list:
                            file_list.append(str(p))
                    if use_llm_dep_expansion and file_list and _should_use_localization_llm(repo):
                        dep_pairs = get_dependencies_for_files(repo, file_list)
                        added = ask_llm_to_add_deps(repo, query, file_list, dep_pairs)
                        for p in added:
                            if p not in file_list:
                                file_list.append(p)
                continue
        return file_list
    return file_list
