# localization_engine/swe_bench_resolve.py
from __future__ import annotations

"""SWE-bench 实例解析与仓库准备：从 JSON 读实例，按需 clone + checkout base_commit。

网络策略：仅拉取 GitHub 仓库（git clone）时使用环境变量中的代理；其余请求（embedding、LLM 等）均不走代理。
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote


REQUIRED_KEYS = ("repo", "base_commit", "problem_statement", "defect_file_abs_paths")


def _log_repo_event(event: str, **kwargs: Any) -> None:
    details = " ".join(f"{k}={kwargs[k]!r}" for k in sorted(kwargs.keys()))
    print(f"[repo] {event} {details}".strip(), file=sys.stderr)


def load_instances(
    path: str | Path,
    index: int | None = None,
    instance_id: str | None = None,
) -> list[dict[str, Any]]:
    """从 JSON 文件加载实例列表；可选按 index 或 instance_id 过滤为单条。

    要求每条实例包含 repo, base_commit, problem_statement, defect_file_abs_paths。
    """
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Instance file not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raw = [raw]
    instances = []
    for i, obj in enumerate(raw):
        if not isinstance(obj, dict):
            continue
        missing = [k for k in REQUIRED_KEYS if not obj.get(k)]
        if missing:
            raise ValueError(f"Instance at index {i} missing required keys: {missing}")
        if index is not None and i != index:
            continue
        if instance_id is not None and obj.get("instance_id") != instance_id:
            continue
        instances.append(obj)
    if index is not None or instance_id is not None:
        if not instances:
            raise ValueError(
                f"No instance found for index={index!r} instance_id={instance_id!r}"
            )
    return instances


def resolve_repo_path(instance: dict[str, Any], repos_root: str | Path) -> Path:
    """根据 instance['repo'] 计算本地仓库路径：repos_root / repo（repo 中 '/' 转为 os.sep）。"""
    repo = instance.get("repo")
    if not repo or "/" not in repo:
        raise ValueError("instance must have 'repo' in form owner/name")
    root = Path(repos_root).resolve()
    path = root / repo.replace("/", os.sep)
    return path


def _is_valid_git_repo(repo_path: Path) -> bool:
    """目录存在且为至少有一个 commit 的 git 仓库则返回 True。"""
    if not repo_path.is_dir():
        return False
    git_ref = repo_path / ".git"
    # 支持普通仓库（.git 目录）和 worktree（.git 文件）
    if not git_ref.exists():
        return False
    r = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=str(repo_path),
        capture_output=True,
        timeout=5,
        text=True,
    )
    if r.returncode != 0 or (r.stdout or "").strip().lower() != "true":
        return False
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_path),
        capture_output=True,
        timeout=5,
        text=True,
    )
    return r.returncode == 0 and bool(r.stdout and r.stdout.strip())


def _assert_repo_integrity(repo_path: Path, *, context: str) -> None:
    if not repo_path.is_dir():
        raise RuntimeError(f"repo_path is not a directory: {repo_path}")
    if not _is_valid_git_repo(repo_path):
        raise RuntimeError(f"invalid git repo ({context}): {repo_path}")

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_path),
        capture_output=True,
        timeout=10,
        text=True,
    )
    if head.returncode != 0 or not (head.stdout or "").strip():
        err = (head.stderr or "").strip()
        raise RuntimeError(f"cannot resolve HEAD ({context}): {repo_path} {err}")

    # 至少应有一个可见文件/目录（排除 .git），避免空目录或异常状态混入。
    visible_children = [p for p in repo_path.iterdir() if p.name != ".git"]
    if not visible_children:
        raise RuntimeError(f"repo has no visible files ({context}): {repo_path}")


def _get_github_token(repos_root: str | Path | None) -> str | None:
    """默认从 data/.github_token 读取；其次从环境变量 LOCALIZATION_ENGINE_GITHUB_TOKEN / GITHUB_TOKEN 读取。"""
    # 1) 优先使用 token 文件（默认）：data/.github_token 或 repos 同级 .github_token
    candidates: list[Path] = []
    if repos_root is not None:
        candidates.append(Path(repos_root).resolve().parent / ".github_token")
    candidates.append(Path.cwd() / "data" / ".github_token")
    for token_file in candidates:
        if token_file.is_file():
            try:
                t = token_file.read_text(encoding="utf-8").strip()
                if t:
                    return t
            except Exception:
                pass
    # 2) 环境变量
    token = (
        os.environ.get("LOCALIZATION_ENGINE_GITHUB_TOKEN")
        or os.environ.get("CODEPHOENIX_GITHUB_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
    )
    if token and token.strip():
        return token.strip()
    return None


def clone_if_needed(repo: str, repo_path: Path, repos_root: str | Path | None = None) -> None:
    """若 repo_path 不存在或为无效/空仓库则执行 git clone（无效时先删除再 clone）。可选 token 加速。"""
    if repo_path.exists() and _is_valid_git_repo(repo_path):
        _assert_repo_integrity(repo_path, context="reuse-existing")
        return
    if repo_path.exists():
        _log_repo_event("remove-invalid-path", repo=repo, path=str(repo_path))
        shutil.rmtree(repo_path, ignore_errors=True)
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    token = _get_github_token(repos_root)
    if token:
        # 个人 PAT 用 https://<token>@github.com/...；token 中若有 +/ 等需 URL 编码
        url = "https://{}@github.com/{}.git".format(quote(token, safe=""), repo)
        print("[clone] Cloning {} (using token)...".format(repo), file=sys.stderr)
        print("[clone] URL (token 已脱敏): https://***@github.com/{}.git".format(repo), file=sys.stderr)
    else:
        url = f"https://github.com/{repo}.git"
        print("[clone] Cloning {}...".format(repo), file=sys.stderr)
        print("[clone] URL: https://github.com/{}.git".format(repo), file=sys.stderr)

    clone_attempts: list[tuple[str, list[str], dict[str, str]]] = []
    base_env = os.environ.copy()
    clone_timeout = int(
        os.environ.get("LOCALIZATION_ENGINE_GIT_CLONE_TIMEOUT")
        or os.environ.get("CODEPHOENIX_GIT_CLONE_TIMEOUT", "240")
    )

    # 尝试 1：显式直连（禁用代理），用于处理错误代理导致的超时
    direct_env = base_env.copy()
    for k in [
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
    ]:
        direct_env.pop(k, None)
    clone_attempts.append(
        (
            "direct-no-proxy",
            ["git", "-c", "http.proxy=", "-c", "https.proxy=", "clone", url, str(repo_path)],
            direct_env,
        )
    )

    # 尝试 2：按当前环境执行（兼容用户自定义代理）
    clone_attempts.append(("default-env", ["git", "clone", url, str(repo_path)], base_env))

    # 尝试 3：若 token URL 仍失败，回退匿名 URL（公开仓库场景）
    if token:
        plain_url = f"https://github.com/{repo}.git"
        clone_attempts.append(
            (
                "plain-url-no-proxy",
                ["git", "-c", "http.proxy=", "-c", "https.proxy=", "clone", plain_url, str(repo_path)],
                direct_env,
            )
        )

    last_error: RuntimeError | None = None
    for idx, (label, cmd, env_map) in enumerate(clone_attempts, start=1):
        if repo_path.exists():
            shutil.rmtree(repo_path, ignore_errors=True)
        _log_repo_event("clone-attempt", repo=repo, path=str(repo_path), attempt=idx, mode=label)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=clone_timeout,
                env=env_map,
                text=True,
            )
            if result.returncode != 0:
                _log_repo_event(
                    "clone-failed",
                    repo=repo,
                    path=str(repo_path),
                    attempt=idx,
                    mode=label,
                    returncode=result.returncode,
                    stderr=(result.stderr or "")[:500],
                )
                raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
            _assert_repo_integrity(repo_path, context=f"post-clone:{label}")
            _log_repo_event("clone-succeeded", repo=repo, path=str(repo_path), attempt=idx, mode=label)
            return
        except subprocess.TimeoutExpired as e:
            _log_repo_event(
                "clone-timeout",
                repo=repo,
                path=str(repo_path),
                attempt=idx,
                mode=label,
                timeout=e.timeout,
            )
            last_error = RuntimeError(f"git clone timeout after {e.timeout}s (mode={label})")
        except subprocess.CalledProcessError as e:
            err_msg = (e.stderr or "").strip() if e.stderr else ""
            hint = " git: {}".format(err_msg[:500]) if err_msg else ""
            last_error = RuntimeError(
                "git clone failed (exit {}, mode={}). Check token scope and repo access.{}".format(
                    e.returncode, label, hint
                )
            )

        # 轻微退避，避免瞬时网络抖动
        time.sleep(1.0)

    if last_error is not None:
        raise last_error
    raise RuntimeError("git clone failed for unknown reason")


def checkout_commit(repo_path: Path, base_commit: str) -> None:
    """在 repo_path 下执行 git checkout base_commit。"""
    try:
        subprocess.run(
            ["git", "checkout", base_commit],
            cwd=str(repo_path),
            check=True,
            timeout=60,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        _log_repo_event(
            "checkout-failed",
            path=str(repo_path),
            base_commit=base_commit,
            returncode=e.returncode,
            stderr=(e.stderr or "")[:500],
        )
        raise

    resolved = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_path),
        capture_output=True,
        timeout=10,
        text=True,
        check=True,
    )
    current = (resolved.stdout or "").strip()
    if current != base_commit:
        raise RuntimeError(
            "checkout mismatch: expected {} got {} at {}".format(base_commit, current, repo_path)
        )


def ensure_repo_for_instance(
    instance: dict[str, Any],
    repos_root: str | Path,
) -> Path:
    """根据实例解析仓库路径；若存在 resolved_repo_path 则直接使用，否则 clone + checkout。

    返回可用的 repo_root（Path）。
    """
    resolved = instance.get("resolved_repo_path")
    if resolved:
        p = Path(resolved).resolve()
        if _is_valid_git_repo(p):
            _assert_repo_integrity(p, context="resolved_repo_path")
            return p
        raise RuntimeError(f"resolved_repo_path is not a valid git repository: {p}")
    repo_path = resolve_repo_path(instance, repos_root)
    clone_if_needed(instance["repo"], repo_path, repos_root=repos_root)
    checkout_commit(repo_path, instance["base_commit"])
    _assert_repo_integrity(repo_path, context="post-checkout")
    return repo_path


# ---------------------------------------------------------------------------
# 定位准确率：仅 .ts/.ets 参与，与 defect_file_abs_paths 交集算 Recall/Precision
# ---------------------------------------------------------------------------

LOCATE_EXTENSIONS = (".ts", ".ets")
DEFECT_PATH_PREFIX = "/workspace/repos/"


def _normalize_rel(p: str) -> str:
    """统一为 POSIX 风格相对路径便于比较。"""
    return str(Path(p).as_posix())


def _filter_ts_ets(paths: list[str]) -> list[str]:
    """只保留 .ts 和 .ets 后缀的路径。"""
    return [p for p in paths if p.endswith(".ts") or p.endswith(".ets")]


def defect_abs_to_rel(defect_abs_paths: list[str], repo: str) -> set[str]:
    """将 defect_file_abs_paths（绝对路径，前缀 /workspace/repos/{repo}/）转为相对路径 set；仅保留 .ts/.ets。"""
    prefix = f"{DEFECT_PATH_PREFIX}{repo}/"
    out = set()
    for p in _filter_ts_ets(defect_abs_paths):
        if p.startswith(prefix):
            rel = p[len(prefix) :]
        else:
            rel = str(Path(p).name)
        out.add(_normalize_rel(rel))
    return out


def pred_files_to_rel(pred_files: list[str], repo_root: Path) -> set[str]:
    """将预测文件列表（相对 repo_root 或绝对）归一化为相对路径 set；仅保留 .ts/.ets。"""
    root = repo_root.resolve()
    out = set()
    for p in _filter_ts_ets(pred_files):
        path = Path(p)
        if not path.is_absolute():
            path = root / p
        try:
            rel = path.resolve().relative_to(root)
            out.add(_normalize_rel(str(rel)))
        except ValueError:
            out.add(_normalize_rel(p))
    return out


def compute_locate_metrics(
    pred_files: list[str],
    defect_file_abs_paths: list[str],
    repo: str,
    repo_root: Path,
) -> dict[str, Any]:
    """计算定位准确率：仅 .ts/.ets，交集后 Recall、Precision。"""
    pred_set = pred_files_to_rel(pred_files, repo_root)
    gt_set = defect_abs_to_rel(defect_file_abs_paths or [], repo)
    inter = pred_set & gt_set
    recall = len(inter) / len(gt_set) if gt_set else 0.0
    precision = len(inter) / len(pred_set) if pred_set else 0.0
    return {
        "intersection_size": len(inter),
        "pred_count": len(pred_set),
        "gt_count": len(gt_set),
        "recall": recall,
        "precision": precision,
        "intersection": sorted(inter),
    }
