from __future__ import annotations

import argparse
import logging
import hashlib

from sweagent import CONFIG_DIR, REPO_ROOT
from sweagent.utils.log import get_logger

try:
    import rich
except ModuleNotFoundError as e:
    msg = (
        "You probably either forgot to install the dependencies "
        "or forgot to activate your conda or virtual environment."
    )
    raise RuntimeError(msg) from e
import json
import os
import re
import shlex
import subprocess
import tempfile
import traceback
from typing import Any

import rich.console
import rich.markdown
import rich.panel

try:
    from rich_argparse import RichHelpFormatter
except ImportError:
    RichHelpFormatter = argparse.HelpFormatter
from dataclasses import dataclass
from datetime import datetime
from getpass import getuser
from pathlib import Path

import yaml
from rich.markdown import Markdown
from simple_parsing import parse
from simple_parsing.helpers.flatten import FlattenedAccess
from simple_parsing.helpers.serialization.serializable import FrozenSerializable
from sweagent.environment.java.constants import KEY_INSTANCE_ID, KEY_MODEL, KEY_PREDICTION
from multi_swe_bench.harness.build_dataset import CliArgs
from sweagent.utils.patch_utils import (
    defect_tree_sha256,
    expand_patch_hunk_eols_to_match_worktree,
    find_arkts_forbidden_added_syntax,
    filter_submission_remove_self_tests,
    infer_harmony_project_root_from_filesystem,
    infer_hvigor_module_root_from_defect_files,
    normalize_patch_newlines,
)
from sweagent.utils.native_repo import NativeRepoError, rebuild_repo_from_source, reset_repo_to_commit

from sweagent.agent.agents import Agent, AgentArguments
from sweagent.agent.models import ModelArguments
from sweagent.environment.swe_env import EnvironmentArguments, SWEEnv
from sweagent.environment.utils import (
    InvalidGithubURL,
    get_associated_commit_urls,
    get_data_path_name,
    get_gh_issue_data,
    native_build_permit,
    parse_gh_issue_url,
)
from sweagent.utils.config import keys_config

__doc__: str = """ Run inference. Usage examples:

```bash
# Run over a github issue:
python run.py --model_name "gpt4" --data_path "https://github.com/pvlib/pvlib-python/issues/1603" --config_file "config/default_from_url.yaml"
# Apply a patch in a local repository to an issue specified as Markdown file and run a custom installer script in the container
python run.py --model_name "gpt4" --data_path "/path/to/my_issue.md" --repo_path "/path/to/my/local/repo" --environment_setup "/path/to/setup.sh" --config_file "config/default_from_url.yaml" --apply_patch_locally
```

**Step limit**: pass `--max_steps_per_instance N` (0 = unlimited) to cap agent turns per instance; evaluation scripts often use 80.

**For more information**: https://princeton-nlp.github.io/SWE-agent/usage/cl_tutorial/
"""


logger = get_logger("swe-agent-run")
logging.getLogger("simple_parsing").setLevel(logging.WARNING)

UNICODE_REPLACEMENT_CHAR = "\ufffd"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _normalize_patch_text_for_utf8_storage(patch_text: str) -> str:
    """Normalize patch text before persisting it as a leaderboard artifact."""

    normalized = patch_text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if UNICODE_REPLACEMENT_CHAR in normalized:
        raise ValueError(
            "patch contains Unicode replacement characters; original source bytes were decoded incorrectly"
        )
    if "\x00" in normalized:
        raise ValueError("patch contains NUL bytes; refusing to save a non-text model patch")
    return normalized if normalized.endswith("\n") else normalized + "\n"


def _append_missing_defect_file_notes(issue: str | None, missing_files: list[str]) -> str | None:
    if not missing_files:
        return issue
    base = (issue or "").rstrip()
    rendered = "\n".join(f"- {path}" for path in missing_files)
    return (
        f"{base}\n\nKNOWN DEFECT FILES currently missing at base checkout:\n"
        f"{rendered}\n"
        "These paths are still authoritative repair targets. Inspect their parent directories and neighboring code, "
        "then create the missing file at the exact listed path if the issue requires it.\n"
    )


def _append_retry_build_feedback(
    issue: str | None,
    instance_id: str,
    raw_feedback: str | None = None,
) -> str | None:
    raw = (raw_feedback if raw_feedback is not None else os.environ.get("ARKFIX_RETRY_FEEDBACK_JSON", "")).strip()
    if not raw:
        return issue
    feedback_by_instance = json.loads(raw)
    if not isinstance(feedback_by_instance, dict):
        raise ValueError("ARKFIX_RETRY_FEEDBACK_JSON must be a JSON object")
    feedback = feedback_by_instance.get(instance_id)
    if not feedback:
        return issue
    return f"{(issue or '').rstrip()}\n\nPRIOR REAL BUILD FEEDBACK:\n{feedback}"


def _missing_defect_files_at_base(native_root: Path | None, raw_defect_files: Any) -> list[str]:
    if not isinstance(native_root, Path) or not isinstance(raw_defect_files, list):
        return []
    missing: list[str] = []
    for item in raw_defect_files:
        path = str(item or "").replace("\\", "/").strip()
        while path.startswith("./"):
            path = path[2:]
        if path and not (native_root / path).exists():
            missing.append(path)
    return missing


def _normalize_relative_defect_path(value: Any) -> str:
    path = str(value or "").replace("\\", "/").strip()
    while path.startswith("./"):
        path = path[2:]
    parts: list[str] = []
    for part in path.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def _read_rag_defect_file_context(
    *,
    native_root: Any,
    raw_defect_files: Any,
    max_file_chars: int = 2400,
    max_total_chars: int = 9000,
) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(native_root, Path) or not isinstance(raw_defect_files, list):
        return "", []

    root = native_root.resolve()
    parts: list[str] = []
    files_meta: list[dict[str, Any]] = []
    used = 0

    for raw_path in raw_defect_files:
        rel = _normalize_relative_defect_path(raw_path)
        if not rel:
            continue
        path = (root / rel).resolve()
        meta: dict[str, Any] = {"path": rel, "included": False}
        try:
            path.relative_to(root)
        except ValueError:
            meta["reason"] = "outside_native_workdir"
            files_meta.append(meta)
            continue
        if not path.is_file():
            meta["reason"] = "missing_at_base"
            files_meta.append(meta)
            continue

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            meta["reason"] = f"read_failed: {exc}"
            files_meta.append(meta)
            continue

        excerpt = text[:max_file_chars].rstrip()
        if len(text) > max_file_chars:
            excerpt += "\n...[truncated]"
        block = f"\n--- {rel} ---\n```arkts\n{excerpt}\n```\n"
        if used + len(block) > max_total_chars:
            meta["reason"] = "total_limit_reached"
            files_meta.append(meta)
            break
        parts.append(block)
        used += len(block)
        meta.update({"included": True, "chars": len(excerpt), "total_chars": len(text)})
        files_meta.append(meta)

    return "".join(parts).strip(), files_meta


def _run_git_capture(repo_dir: Path, args: list[str], *, timeout: float = 120.0) -> tuple[int, str, str]:
    result = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    return result.returncode, result.stdout, result.stderr


def _git_apply_check_patch(repo_dir: Path, patch_text: str, *, label: str = "model_patch") -> tuple[bool, str]:
    patch_text = normalize_patch_newlines(patch_text)
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=f".{label}.patch",
        delete=False,
        encoding="utf-8",
        newline="\n",
    ) as fp:
        fp.write(patch_text)
        tmp_path = fp.name
    try:
        rc, stdout, stderr = _run_git_capture(
            repo_dir,
            ["apply", "--check", "--whitespace=nowarn", "--unsafe-paths", tmp_path],
        )
        if rc == 0:
            return True, "OK"
        first_error = stderr.strip() or stdout.strip()
        if "corrupt patch" in first_error.lower():
            rc, stdout, stderr = _run_git_capture(
                repo_dir,
                ["apply", "--check", "--recount", "--whitespace=nowarn", "--unsafe-paths", tmp_path],
            )
            if rc == 0:
                return True, "OK (git apply --check --recount)"
        return False, first_error
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _postprocess_model_patch_against_base(
    repo_dir: Path,
    base_sha: str,
    patch_text: str,
    benchmark_entry: dict[str, Any] | None = None,
    *,
    defer_canonical_preprocess: bool = False,
) -> tuple[str, str]:
    """Return UTF-8-safe patch text that applies in leaderboard order, or raise ValueError.

    Native ArkTS evaluation applies the model patch to the raw benchmark base,
    then runs environment preprocessing. Mirror that order here exactly.
    """

    normalized = _normalize_patch_text_for_utf8_storage(patch_text)
    base_sha = (base_sha or "").strip()
    if not base_sha:
        raise ValueError("cannot verify model patch applicability: benchmark base.sha is empty")
    if not repo_dir.is_dir():
        raise ValueError(f"cannot verify model patch applicability: repo does not exist: {repo_dir}")

    rc, stdout, stderr = _run_git_capture(repo_dir, ["reset", "--hard", base_sha], timeout=180.0)
    if rc != 0:
        raise ValueError("cannot reset repo slot to benchmark base:\n" + (stderr.strip() or stdout.strip()))
    rc, stdout, stderr = _run_git_capture(
        repo_dir,
        ["clean", "-ffdx", "-e", ".codephoenix/"],
        timeout=180.0,
    )
    if rc != 0:
        raise ValueError("cannot clean repo slot before model patch check:\n" + (stderr.strip() or stdout.strip()))

    if benchmark_entry is not None:
        try:
            from evaluation.run_llm_patch_eval import (
                _find_deveco_path,
                apply_patch,
                find_harmony_project_dir,
                run_environment_preprocess,
            )
        except Exception as exc:
            raise ValueError(f"cannot import leaderboard apply-check components: {exc}") from exc

        deveco_path = _find_deveco_path()
        if not deveco_path:
            raise ValueError("cannot verify model patch applicability: DEVECO_PATH is not configured")
        candidates: list[tuple[str, str]] = [("leaderboard_raw_base_utf8_lf", normalized)]

        failures: list[str] = []
        seen: set[str] = set()
        for candidate_index, (mode, candidate) in enumerate(candidates):
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate_index > 0:
                rc, stdout, stderr = _run_git_capture(repo_dir, ["reset", "--hard", base_sha], timeout=180.0)
                if rc != 0:
                    failures.append(f"{mode}: reset failed: {stderr.strip() or stdout.strip()}")
                    continue
                rc, stdout, stderr = _run_git_capture(
                    repo_dir,
                    ["clean", "-ffdx", "-e", ".codephoenix/"],
                    timeout=180.0,
                )
                if rc != 0:
                    failures.append(f"{mode}: clean failed: {stderr.strip() or stdout.strip()}")
                    continue
            ok, message = apply_patch(repo_dir, candidate, mode)
            if not ok:
                failures.append(f"{mode}: {message}")
                continue
            if defer_canonical_preprocess:
                return candidate, f"{mode}_deferred_preprocess"
            project_dir = find_harmony_project_dir(repo_dir, benchmark_entry, candidate, "")
            with native_build_permit():
                preprocess_code, preprocess_out = run_environment_preprocess(project_dir, deveco_path)
            if preprocess_code == 0:
                return candidate, mode
            tail = "\n".join(preprocess_out.splitlines()[-80:])
            failures.append(f"{mode}: preprocess failed after patch: {tail}")
        raise ValueError(
            "model patch does not pass canonical apply-then-preprocess validation; refusing to save it.\n"
            + "\n".join(failures)
        )

    candidates: list[tuple[str, str]] = [("strict_utf8_lf", normalized)]
    eol_expanded = expand_patch_hunk_eols_to_match_worktree(normalized, repo_dir)
    if eol_expanded != normalized:
        candidates.append(("strict_utf8_worktree_eol", eol_expanded))

    failures: list[str] = []
    seen: set[str] = set()
    for mode, candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        ok, message = _git_apply_check_patch(repo_dir, candidate, label=mode)
        if ok:
            return candidate, mode
        failures.append(f"{mode}: {message}")
    raise ValueError(
        "model patch does not apply cleanly to benchmark base; refusing to save it.\n"
        + "\n".join(failures)
    )


def _native_mode_for_dataset(record, env_args) -> bool:
    """Match SWEEnv.reset(): ArkTS native shell when language is arkts or pr_file hints arkts."""
    cli_pr_file = str(getattr(getattr(env_args, "cli_args", None), "pr_file", "") or "").lower()
    return (getattr(record, "language", None) == "arkts") or ("arkts" in cli_pr_file)


def _resolve_native_repo_dir(record, env_args) -> Path | None:
    """Same layout as SWEEnv._reset_native: <repo_dir or repos_base_dir>/<pr.repo>."""
    repo_folder = record.instance.pr.repo
    base_dir = _resolve_native_repos_base_dir(env_args)
    if base_dir:
        repo_dir = base_dir / repo_folder
    else:
        repo_dir = Path(REPO_ROOT).resolve().parent / repo_folder
    return repo_dir if repo_dir.is_dir() else None


def _resolve_native_repos_base_dir(env_args) -> Path | None:
    repos_base_dir = (getattr(env_args, "repos_base_dir", "") or "").strip()
    if not repos_base_dir:
        rd = getattr(getattr(env_args, "cli_args", None), "repo_dir", None)
        repos_base_dir = str(rd).strip() if rd is not None else ""
    if repos_base_dir:
        return Path(repos_base_dir).resolve()
    return None


def _run_git_text(repo_dir: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise NativeRepoError(
            f"git -C {repo_dir} {' '.join(args)} failed with exit code {result.returncode}\n"
            f"stdout:\n{result.stdout[-4000:]}\n"
            f"stderr:\n{result.stderr[-4000:]}"
        )
    return result.stdout.strip()


def restore_host_repo_pool_to_baseline(env_args, record=None, *, strict: bool = True) -> None:
    """After each native instance, restore its exact run-local repository."""
    cli_pr_file = str(getattr(getattr(env_args, "cli_args", None), "pr_file", "") or "").lower()
    if "arkts" not in cli_pr_file:
        return

    repos_base_dir = _resolve_native_repos_base_dir(env_args)
    if repos_base_dir is None:
        return

    failures: list[str] = []
    restored: list[str] = []
    if record is not None:
        repo_targets = [
            (
                record.instance.pr.repo,
                (record.instance.pr.base.sha or "").strip(),
            )
        ]
    else:
        repo_targets = [(name, "") for name in ("ImageKnife", "applications_app_samples")]

    for repo_name, base_sha in repo_targets:
        target_repo = repos_base_dir / repo_name
        if not target_repo.exists():
            logger.debug("[修复后] 跳过仓库池恢复：目标仓库不存在 path=%s", target_repo)
            continue
        try:
            target_head = base_sha or _run_git_text(target_repo, ["rev-parse", "HEAD"])
            reset_repo_to_commit(
                target_repo,
                target_head,
                clean_args=["clean", "-ffdxq", "-e", ".codephoenix/"],
                verify_clean=True,
                fetch_lfs_on_dirty=False,
                preserve_paths=(".codephoenix",),
            )
            actual_top = _run_git_text(target_repo, ["rev-parse", "--show-toplevel"]).replace("\\", "/").rstrip("/")
            expected_top = str(target_repo.resolve()).replace("\\", "/").rstrip("/")
            if actual_top != expected_top:
                raise NativeRepoError(
                    f"pool restore verification failed: expected git top {expected_top}, got {actual_top}"
                )
            if (target_repo / "data_files").exists():
                raise NativeRepoError(f"pool restore verification failed: data_files remains in {target_repo}")
            restored.append(f"{repo_name}@{target_head[:9]}")
        except Exception as exc:
            failures.append(f"{target_repo}: {exc}")

    if failures:
        message = "[修复后] 仓库池恢复失败，停止当前 worker，避免污染下一条实例。\n" + "\n".join(failures)
        logger.error(message)
        if strict:
            raise NativeRepoError(message)
        return
    if restored:
        logger.info("[修复后] 仓库池已恢复到基准 HEAD: %s", ", ".join(restored))


def reset_host_workdir_to_dataset_base(env_args, record) -> None:
    """Before repair: reset local benchmark checkout to ``base.sha`` and drop stray untracked files."""
    iid = record.data.get("instance_id", "?")
    if not _native_mode_for_dataset(record, env_args):
        logger.info(
            "[修复前] 跳过本机仓库重置（非 ArkTS 本机模式） instance_id=%s",
            iid,
        )
        return
    repo_dir = _resolve_native_repo_dir(record, env_args)
    if repo_dir is None:
        logger.warning(
            "[修复前] 跳过本机仓库重置：目录不存在（本机模式需 <repo_dir>/%s） instance_id=%s",
            record.instance.pr.repo,
            iid,
        )
        return
    base_sha = (record.instance.pr.base.sha or "").strip()
    if not base_sha:
        logger.warning(
            "[修复前] 跳过本机仓库重置：本实例 base.sha 为空 instance_id=%s",
            iid,
        )
        return
    logger.info(
        "[修复前] 开始将本地仓库恢复到 benchmark base 提交 base.sha=%s path=%s instance_id=%s",
        base_sha,
        repo_dir,
        iid,
    )
    clean_args = ["clean", "-ffdxq", "-e", ".codephoenix/"]
    try:
        head = reset_repo_to_commit(
            repo_dir,
            base_sha,
            clean_args=clean_args,
            verify_clean=True,
            preserve_paths=(".codephoenix",),
        )
    except NativeRepoError as first_error:
        logger.error(
            "[repair-pre] failed to reset repo slot to benchmark base; fix this slot/object cache before rerunning. "
            "repo_dir=%s base.sha=%s instance_id=%s error=%s",
            repo_dir,
            base_sha,
            iid,
            first_error,
        )
        raise
        if "repair_repo_parallel" in repo_dir.parts:
            logger.error(
                "[修复前] 并行仓库池 reset 失败，禁止 run.py 在运行中重建大仓库。请先修复 repo_dir=%s error=%s",
                repo_dir,
                first_error,
            )
            raise
        source_repo_dir = None
        if source_repo_dir.resolve() == repo_dir.resolve():
            logger.error("[修复前] 本机仓库重置失败，且当前目录就是源仓库，不能自动重建：%s", first_error)
            raise
        logger.warning(
            "[修复前] 本机仓库重置失败，准备从干净源仓库重建 run 副本。target=%s source=%s error=%s",
            repo_dir,
            source_repo_dir,
            first_error,
        )
        rebuild_repo_from_source(repo_dir, source_repo_dir, fetch_lfs=True)
        head = reset_repo_to_commit(repo_dir, base_sha, clean_args=clean_args, verify_clean=True)
    logger.info(
        "[修复前] 已完成：本地仓库已处于 base 提交 HEAD=%s（与 base.sha 一致）path=%s instance_id=%s",
        head,
        repo_dir,
        iid,
    )


@dataclass(frozen=True)
class ActionsArguments(FlattenedAccess, FrozenSerializable):
    """Run real-life actions (opening PRs, etc.) if we can solve the issue."""

    # Open a PR with the patch if we can solve the issue
    open_pr: bool = False
    # When working with local repository: Apply patch
    apply_patch_locally: bool = False
    # Option to be used with open_pr: Skip action if there are already commits claiming
    # to fix the issue. Please only set this to False if you are sure the commits are
    # not fixes or if this is your own repository!
    skip_if_commits_reference_issue: bool = True
    # OBSOLETE. Do not use, will raise error. Please specify --repo_path instead.
    push_gh_repo_url: str = ""

    def __post_init__(self):
        if self.push_gh_repo_url:
            msg = "push_gh_repo_url is obsolete. Use repo_path instead"
            raise ValueError(msg)


@dataclass(frozen=True)
class ScriptArguments(FlattenedAccess, FrozenSerializable):
    """Configure the control flow of the run.py script"""

    environment: EnvironmentArguments
    agent: AgentArguments
    actions: ActionsArguments
    # Only run instances that completely match this regex
    instance_filter: str = ".*"
    # Skip instances with existing trajectories
    skip_existing: bool = True
    # Suffix for the run name (used for example in trajectory directory naming)
    suffix: str = ""
    # Raise unhandled exceptions during the run (useful for debugging)
    raise_exceptions: bool = False
    # Dump the entire config to the log
    print_config: bool = True
    # Before each instance: git reset local native workdir to dataset base.sha (set True to skip for debugging)
    skip_workdir_reset: bool = False
    # Retrieval-augmented ArkTS/HarmonyOS context. Default off preserves baseline behavior.
    rag_mode: str = "off"
    rag_docs_roots: str = ""
    rag_samples_roots: str = ""
    rag_index_name: str = "arkfix_default"
    rag_top_k_docs: int = 4
    rag_top_k_code: int = 4
    rag_max_context_chars: int = 12000
    rag_storage_dir: str = ""
    rag_fail_open: bool = True

    @property
    def run_name(self):
        """Generate a unique name for this run: model__dataset__timestamp."""
        model_name = self.agent.model.model_name.replace(":", "-")
        data_stem = get_data_path_name(str(self.environment.cli_args.pr_file))
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

        return (
            f"{model_name}__{data_stem}__{timestamp}"
            + (f"__{self.suffix}" if self.suffix else "")
        )


def _retrieve_rag_for_instance(
    args: ScriptArguments,
    *,
    issue: str | None,
    defect_files: str,
    project_path: str,
    observation: str | None = None,
    defect_file_context: str | None = None,
    defect_file_context_files: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any]]:
    from rag.config import RagConfig
    from rag.retrieve import retrieve_rag_context

    cfg = RagConfig.from_values(
        mode=args.rag_mode,
        docs_roots=args.rag_docs_roots,
        samples_roots=args.rag_samples_roots,
        index_name=args.rag_index_name,
        top_k_docs=args.rag_top_k_docs,
        top_k_code=args.rag_top_k_code,
        max_context_chars=args.rag_max_context_chars,
        storage_dir=args.rag_storage_dir or None,
        fail_open=args.rag_fail_open,
    )
    result = retrieve_rag_context(
        cfg,
        issue=issue,
        defect_files=defect_files,
        project_path=project_path,
        observation=observation,
        defect_file_context=defect_file_context,
    )
    if result.metadata.get("enabled") or result.metadata.get("error"):
        result.metadata["defect_file_context_files"] = defect_file_context_files or []
    return result.context, result.metadata


def _safe_artifact_stem(value: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    stem = stem.strip("._-")
    return stem or "instance"


def _write_rag_audit(
    *,
    traj_dir: Path,
    instance_id: str,
    metadata: dict[str, Any],
    context: str,
) -> None:
    if not metadata.get("enabled") and not metadata.get("error"):
        return
    try:
        out_dir = traj_dir / "rag_hits"
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            **metadata,
            "instance_id": instance_id,
            "context": context,
        }
        out_path = out_dir / f"{_safe_artifact_stem(instance_id)}.rag.json"
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    except Exception as exc:
        logger.warning("Failed to write RAG audit metadata for %s: %s", instance_id, exc)


class _ContinueLoop(Exception):
    """Used for internal control flow."""

    def __init__(self, *, cleanup: bool = True):
        super().__init__()
        self.cleanup = cleanup


class MainHook:
    """Hook structure for the web server or other addons to interface with"""

    @staticmethod
    def _is_promising_patch(info: dict[str, Any]) -> bool:
        """Do we actually believe that the patch will solve the issue?
        Or are we just submitting the last patch we generated before hitting an error?
        """
        # The exit status can also be `submitted (exit_cost)` etc.
        return info["exit_status"] == "submitted" and info.get("submission") is not None

    def on_init(self, *, args: ScriptArguments, agent: Agent, env: SWEEnv, traj_dir: Path):
        """Called when hook is initialized"""

    def on_start(self):
        """Called at the beginning of `Main.main`"""

    def on_end(self):
        """Called at the end of `Main.main`"""

    def on_instance_start(self, *, index: int, instance: dict[str, Any]):
        """Called at the beginning of each instance loop in `Main.run`"""

    def on_instance_skipped(
        self,
    ):
        """Called when an instance is skipped in `Main.run`"""

    def on_instance_completed(self, *, info, trajectory):
        """Called when an instance is completed in `Main.run`"""


class SaveApplyPatchHook(MainHook):
    """This hook saves patches to a separate directory and optionally applies them to a local repository."""

    # Patterns forbidden in ALL patches regardless of language
    _FORBIDDEN_PATCH_PATTERNS: list[re.Pattern[str]] = []

    _ENV_OR_GATE_FAILURE_PATTERNS: list[re.Pattern[str]] = [
        re.compile(r"BUILD FAILED", re.IGNORECASE),
        re.compile(r"hvigor ERROR", re.IGNORECASE),
        re.compile(r"ERR_PNPM_FETCH", re.IGNORECASE),
        re.compile(r"Unable to find 'sdk\.dir'", re.IGNORECASE),
        re.compile(r"Unable to find the following components", re.IGNORECASE),
    ]

    _GATE_ATTEMPT_PATTERNS: list[re.Pattern[str]] = [
        re.compile(r"hvigor", re.IGNORECASE),   # 修复：因为Windows真实回显只有 'hvigor' 没有 'w'，这里必须放宽匹配以识别尝试
        re.compile(r"\bcodelinter\b", re.IGNORECASE),
        re.compile(r"BUILD SUCCESSFUL", re.IGNORECASE),
        re.compile(r"BUILD FAILED", re.IGNORECASE),  # 如果出现构建失败字样，也说明它尝试过执行门禁，不应因为没找到命令词被误杀
    ]

    def on_init(self, *, args: ScriptArguments, agent: Agent, env: SWEEnv, traj_dir: Path):
        self._traj_dir = traj_dir
        self._apply_patch_locally = args.actions.apply_patch_locally
        self._args = args
        self._instance = None

    @staticmethod
    def _write_patch_metadata(meta_file: Path, metadata: dict[str, Any]) -> None:
        _atomic_write_text(meta_file, json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")

    def on_instance_start(self, *, index: int, instance):
        self._instance = instance

    def on_instance_completed(self, *, info, trajectory):
        assert self._instance is not None  # mypy
        instance_id = self._instance.data["instance_id"]
        patch_path = self._save_patch(instance_id, info, trajectory)
        if patch_path:
            if not self._apply_patch_locally:
                return
            if not self._is_promising_patch(info):
                return
            assert self._instance  # mypy
            if self._instance["repo_type"] != "local":
                return
            local_dir = Path(self._instance.instance.pr.repo)
            self._apply_patch(patch_path, local_dir)

    @staticmethod
    def _print_patch_message(patch_output_file: Path):
        console = rich.console.Console()
        msg = [
            "SWE-agent has produced a patch that it believes will solve the issue you submitted!",
            "Use the code snippet below to inspect or apply it!",
        ]
        panel = rich.panel.Panel.fit(
            "\n".join(msg),
            title="Submission successful",
        )
        console.print(panel)
        content = [
            "```bash",
            "# The patch has been saved to your local filesystem at:",
            f"PATCH_FILE_PATH='{patch_output_file.resolve()}'",
            "# Inspect it:",
            'cat "${PATCH_FILE_PATH}"',
            "# Apply it to a local repository:",
            "cd <your local repo root>",
            'git apply "${PATCH_FILE_PATH}"',
            "```",
        ]
        console.print(rich.markdown.Markdown("\n".join(content)))

    @classmethod
    def _trajectory_text(cls, trajectory) -> str:
        if not trajectory:
            return ""
        parts: list[str] = []
        # Stored `.traj` format: usually list[dict], where each step has `observation`.
        for step in trajectory:
            if isinstance(step, dict):
                obs = step.get("observation") or ""
                if obs:
                    parts.append(str(obs))
        return "\n".join(parts)

    @classmethod
    def _trajectory_has_any_gate_attempt(cls, trajectory) -> bool:
        txt = cls._trajectory_text(trajectory)
        return any(rx.search(txt) for rx in cls._GATE_ATTEMPT_PATTERNS)

    @classmethod
    def _trajectory_has_env_or_gate_failure(cls, trajectory) -> bool:
        txt = cls._trajectory_text(trajectory)
        return any(rx.search(txt) for rx in cls._ENV_OR_GATE_FAILURE_PATTERNS)

    @staticmethod
    def _trajectory_action(step: Any) -> str:
        if not isinstance(step, dict):
            return ""
        action = step.get("action") or ""
        if isinstance(action, dict):
            action = action.get("command") or action.get("name") or ""
        return str(action).strip()

    @staticmethod
    def _trajectory_exit_code(step: Any) -> int | None:
        if not isinstance(step, dict):
            return None
        results = step.get("command_results")
        if not isinstance(results, list):
            return None
        for result in reversed(results):
            if not isinstance(result, dict):
                continue
            value = result.get("exit_code")
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        return None

    @classmethod
    def _trajectory_build_exit_code(cls, step: Any) -> int | None:
        if isinstance(step, dict):
            matches = re.findall(
                r"^BUILD_ACTION_EXIT_CODE=(-?\d+)$",
                str(step.get("observation") or ""),
                re.MULTILINE,
            )
            if matches:
                return int(matches[-1])
        return cls._trajectory_exit_code(step)

    @staticmethod
    def _is_real_build_action(action: str) -> bool:
        # Keep the real build as the final command. Otherwise a later echo or
        # edit can hide the build exit status while preserving a success word.
        if re.search(r"\|\||(?<!\|)\|(?!\|)|;|(?<![&<>])&(?![&>])", action):
            return False
        last_command: tuple[str, list[str]] | None = None
        for segment in re.split(r"(?:&&|\n)", action):
            try:
                parts = shlex.split(segment.strip().replace("\\", "/"), posix=True)
            except ValueError:
                return False
            while parts and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", parts[0]):
                parts.pop(0)
            if not parts or parts[0] == "cd":
                continue
            command = parts[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
            args = [part.replace("\\", "/").lower() for part in parts[1:]]
            last_command = (command, args)
        if last_command is None:
            return False
        command, args = last_command
        if command in {"python", "python3", "python.exe", "py", "py.exe"}:
            return any(arg.rsplit("/", 1)[-1] == "build_app.py" for arg in args)
        if command == "build_app.py":
            return True
        if command in {"hvigor", "hvigorw", "hvigor.bat", "hvigorw.bat", "hvigor.cmd", "hvigorw.cmd"}:
            return any(re.fullmatch(r"assemble(?:hap|har|hsp)", arg, re.IGNORECASE) for arg in args)
        return False

    @staticmethod
    def _build_success_marker(observation: str) -> str:
        for marker in ("BUILD_STATUS=SUCCESS", "COMPILE RESULT:SUCCESS", "BUILD SUCCESSFUL"):
            if marker in observation:
                return marker
        return ""

    @staticmethod
    def _edit_was_applied(step: Any) -> bool:
        if not isinstance(step, dict):
            return False
        statuses = re.findall(r"^EDIT_STATUS=(APPLIED|REJECTED)$", str(step.get("observation") or ""), re.MULTILINE)
        return bool(statuses) and statuses[-1] == "APPLIED"

    @classmethod
    def _final_validation(
        cls,
        info: dict[str, Any],
        trajectory,
        *,
        require_build: bool = True,
    ) -> tuple[bool, str, dict[str, Any]]:
        if info.get("exit_status") != "submitted":
            return False, "exit_status_not_submitted", {}
        if not isinstance(trajectory, list):
            return False, "missing_trajectory", {}

        edit_pattern = re.compile(r"^(?:edit|edit_file|str_replace|create)\b")
        last_edit = max(
            (
                index
                for index, step in enumerate(trajectory)
                if edit_pattern.match(cls._trajectory_action(step))
                and cls._edit_was_applied(step)
            ),
            default=-1,
        )
        if last_edit < 0:
            return False, "no_successful_edit_action", {}

        if not require_build:
            repair_status_index = -1
            repair_status_exit_code: int | None = None
            for index, step in enumerate(trajectory[last_edit:], last_edit):
                action_name = cls._trajectory_action(step).split(maxsplit=1)[0:1]
                if index != last_edit and action_name != ["repair_status"]:
                    continue
                observation = str(step.get("observation") or "") if isinstance(step, dict) else ""
                exit_code = cls._trajectory_exit_code(step)
                if (
                    exit_code in (None, 0)
                    and "REPAIR_STATUS" in observation
                    and re.search(r"^submit_readiness:\s*SCOPE_OK", observation, re.MULTILINE)
                    and not re.search(
                        r"EXECUTION TIMED OUT|Native shell was restarted|COMMAND FAILED TO EXECUTE|BROKEN PIPE ERROR",
                        observation,
                        re.IGNORECASE,
                    )
                ):
                    repair_status_index = index
                    repair_status_exit_code = exit_code
                elif action_name == ["repair_status"]:
                    repair_status_index = -1
                    repair_status_exit_code = exit_code
            if repair_status_index < 0:
                return False, "missing_valid_repair_status_after_edit", {
                    "last_edit": last_edit,
                    "repair_status": repair_status_index,
                    "repair_status_exit_code": repair_status_exit_code,
                }

            submit_index = -1
            submit_exit_code: int | None = None
            for index, step in enumerate(trajectory[repair_status_index + 1 :], repair_status_index + 1):
                if cls._trajectory_action(step).split(maxsplit=1)[0:1] != ["submit"]:
                    continue
                submit_index = index
                submit_exit_code = cls._trajectory_exit_code(step)
                break
            if submit_index < 0:
                return False, "missing_submit_after_repair_status", {
                    "last_edit": last_edit,
                    "repair_status": repair_status_index,
                }
            if submit_exit_code not in (None, 0):
                return False, "submit_exit_nonzero", {
                    "last_edit": last_edit,
                    "repair_status": repair_status_index,
                    "submit": submit_index,
                    "submit_exit_code": submit_exit_code,
                }
            return True, "passed_patch_only", {
                "last_edit": last_edit,
                "last_edit_action": cls._trajectory_action(trajectory[last_edit]),
                "repair_status": repair_status_index,
                "repair_status_exit_code": repair_status_exit_code,
                "repair_status_source": (
                    "edit_observation" if repair_status_index == last_edit else "repair_status_action"
                ),
                "submit": submit_index,
                "submit_exit_code": submit_exit_code,
                "serial_build_required": True,
            }

        build_indexes = [
            index
            for index, step in enumerate(trajectory[last_edit + 1 :], last_edit + 1)
            if cls._is_real_build_action(cls._trajectory_action(step))
        ]
        build_index = build_indexes[-1] if build_indexes else -1
        if build_index < 0:
            return False, "no_build_after_last_edit", {"last_edit": last_edit}

        build_step = trajectory[build_index]
        build_exit_code = cls._trajectory_build_exit_code(build_step)
        build_observation = str(build_step.get("observation") or "")
        build_marker = cls._build_success_marker(build_observation)
        build_tree_matches = re.findall(
            r"^BUILD_TREE_SHA256=([0-9a-f]{64})$",
            build_observation,
            re.MULTILINE | re.IGNORECASE,
        )
        if build_exit_code != 0:
            return False, "last_build_exit_nonzero", {
                "last_edit": last_edit,
                "build": build_index,
                "build_exit_code": build_exit_code,
            }
        if not build_marker:
            return False, "last_build_missing_success_marker", {
                "last_edit": last_edit,
                "build": build_index,
                "build_exit_code": build_exit_code,
            }
        if re.search(
            r"(?:^|\n)(?:BUILD_STATUS=FAIL(?:ED|URE)?|COMPILE RESULT:FAIL(?:ED|URE)?|BUILD FAILED)\b",
            build_observation,
            re.IGNORECASE,
        ):
            return False, "last_build_contains_failure_marker", {
                "last_edit": last_edit,
                "build": build_index,
                "build_exit_code": build_exit_code,
            }
        if not build_tree_matches:
            return False, "last_build_missing_tree_sha256", {
                "last_edit": last_edit,
                "build": build_index,
                "build_exit_code": build_exit_code,
            }
        build_tree_sha256 = build_tree_matches[-1].lower()

        repair_status_index = -1
        last_repair_status_index = -1
        last_repair_status_exit_code: int | None = None
        for index, step in enumerate(trajectory[last_edit + 1 :], last_edit + 1):
            if cls._trajectory_action(step).split(maxsplit=1)[0:1] != ["repair_status"]:
                continue
            last_repair_status_index = index
            last_repair_status_exit_code = cls._trajectory_exit_code(step)
            observation = str(step.get("observation") or "") if isinstance(step, dict) else ""
            if (
                last_repair_status_exit_code in (None, 0)
                and "REPAIR_STATUS" in observation
                and re.search(r"^submit_readiness:\s*SCOPE_OK", observation, re.MULTILINE)
                and not re.search(
                    r"EXECUTION TIMED OUT|Native shell was restarted|COMMAND FAILED TO EXECUTE|BROKEN PIPE ERROR",
                    observation,
                    re.IGNORECASE,
                )
            ):
                repair_status_index = index
            else:
                repair_status_index = -1
        if repair_status_index < 0:
            return False, "missing_valid_repair_status_after_edit", {
                "last_edit": last_edit,
                "build": build_index,
                "repair_status": last_repair_status_index,
                "repair_status_exit_code": last_repair_status_exit_code,
            }

        submit_index = -1
        submit_exit_code: int | None = None
        validation_index = max(build_index, repair_status_index)
        for index, step in enumerate(trajectory[validation_index + 1 :], validation_index + 1):
            if cls._trajectory_action(step).split(maxsplit=1)[0:1] != ["submit"]:
                continue
            submit_index = index
            submit_exit_code = cls._trajectory_exit_code(step)
            break
        if submit_index < 0:
            return False, "missing_submit_after_repair_status", {
                "last_edit": last_edit,
                "build": build_index,
                "repair_status": repair_status_index,
            }
        if submit_exit_code not in (None, 0):
            return False, "submit_exit_nonzero", {
                "last_edit": last_edit,
                "build": build_index,
                "repair_status": repair_status_index,
                "submit": submit_index,
                "submit_exit_code": submit_exit_code,
            }
        return True, "passed", {
            "last_edit": last_edit,
            "last_edit_action": cls._trajectory_action(trajectory[last_edit]),
            "build": build_index,
            "build_action": cls._trajectory_action(build_step),
            "build_exit_code": build_exit_code,
            "build_success_marker": build_marker,
            "build_tree_sha256": build_tree_sha256,
            "repair_status": repair_status_index,
            "repair_status_exit_code": cls._trajectory_exit_code(trajectory[repair_status_index]),
            "submit": submit_index,
            "submit_exit_code": submit_exit_code,
        }

    @staticmethod
    def _trajectory_prompt_hashes(trajectory) -> list[str]:
        hashes: list[str] = []
        seen: set[str] = set()
        if not trajectory:
            return hashes
        for step in trajectory:
            if not isinstance(step, dict):
                continue
            value = step.get("prompt_hash")
            if not value:
                timing = step.get("timing")
                if isinstance(timing, dict):
                    value = timing.get("prompt_hash")
            value = str(value or "").strip()
            if value and value not in seen:
                seen.add(value)
                hashes.append(value)
        return hashes

    def _save_patch(self, instance_id: str, info, trajectory) -> Path | None:
        """Create patch files that can be applied with `git am`.

        Returns:
            The path to the patch file, if it was saved. Otherwise, returns None.
        """
        patch_output_dir = self._traj_dir / "patches"
        patch_output_dir.mkdir(exist_ok=True, parents=True)
        patch_output_file = patch_output_dir / f"{instance_id}.patch"
        if not info.get("submission"):
            logger.info("No patch to save.")
            return None
        known_defect_files = (
            self._instance.data.get("defect_files", [])
            if self._instance is not None
            else []
        )
        allow_test_patch = bool(
            self._instance is not None
            and self._instance.data.get("allow_test_patch") is True
        )
        model_patch = filter_submission_remove_self_tests(
            info["submission"],
            known_defect_files if isinstance(known_defect_files, list) else [],
            allow_test_patch=allow_test_patch,
        )
        if model_patch != info["submission"]:
            logger.info(
                "Removed agent self-test diff blocks from saved model patch (%d -> %d chars).",
                len(info["submission"]),
                len(model_patch),
            )
            info["submission"] = model_patch
        if not model_patch.strip():
            logger.info("No repair patch left to save after removing agent self-test files.")
            return None
        try:
            model_patch = _normalize_patch_text_for_utf8_storage(model_patch)
        except ValueError as exc:
            logger.info("Reject patch: %s (no patch saved).", exc)
            info["submission"] = None
            return None
        source_patch_sha256 = hashlib.sha256(model_patch.encode("utf-8")).hexdigest()
        native_instance = bool(
            self._instance is not None
            and _native_mode_for_dataset(self._instance, self._args.environment)
        )
        repo_dir: Path | None = None
        base_sha = ""
        has_gate_attempt = self._trajectory_has_any_gate_attempt(trajectory)
        has_gate_or_env_failure = self._trajectory_has_env_or_gate_failure(trajectory)
        forced_submit_due_to_step_limit = bool(info.get("max_steps_forced_submit", False))
        agent_config = getattr(getattr(self._args, "agent", None), "config", None)
        patch_only_generation = getattr(agent_config, "patch_only_generation", False) is True
        final_validation_ok, final_validation_reason, final_validation_steps = self._final_validation(
            info,
            trajectory,
            require_build=not patch_only_generation,
        )
        forced_unvalidated = False
        if not final_validation_ok:
            logger.info("Reject patch: final validation failed: %s", final_validation_reason)
            info["submission"] = None
            return None

        if native_instance:
            repo_dir = _resolve_native_repo_dir(self._instance, self._args.environment)
            base_sha = (self._instance.instance.pr.base.sha or "").strip()
            if repo_dir is None:
                logger.info("Reject patch: cannot locate native repo for tree hash (no patch saved).")
                info["submission"] = None
                return None
            raw_defect_files = self._instance.data.get("defect_files", [])
            if not isinstance(raw_defect_files, list):
                logger.info("Reject patch: runtime defect_files is not a list (no patch saved).")
                info["submission"] = None
                return None
            try:
                submit_tree_sha256 = defect_tree_sha256(repo_dir, [str(path) for path in raw_defect_files])
            except (OSError, ValueError) as exc:
                logger.info("Reject patch: cannot hash submit-time defect tree: %s", exc)
                info["submission"] = None
                return None
            if (
                not forced_unvalidated
                and not patch_only_generation
                and submit_tree_sha256 != final_validation_steps.get("build_tree_sha256")
            ):
                logger.info("Reject patch: defect files changed after the validated build (no patch saved).")
                info["submission"] = None
                return None
            final_validation_steps["submit_tree_sha256"] = submit_tree_sha256

        # Final validation above already requires a post-edit build; keep this syntax rail independent.
        if find_arkts_forbidden_added_syntax(model_patch):
            logger.info("Reject patch: contains ArkTS-forbidden syntax (no patch saved).")
            info["submission"] = None
            return None

        base_apply_check = "not_checked"
        if native_instance:
            if repo_dir is None:
                logger.info("Reject patch: cannot locate native repo for apply check (no patch saved).")
                info["submission"] = None
                return None
            try:
                model_patch, base_apply_check = _postprocess_model_patch_against_base(
                    repo_dir,
                    base_sha,
                    model_patch,
                    self._instance.data if hasattr(self._instance, "data") else None,
                    defer_canonical_preprocess=(
                        os.environ.get("ARKFIX_DEFER_CANONICAL_PREPROCESS", "").strip() == "1"
                        and bool(os.environ.get("ARKFIX_BATCH_RUN_ID", "").strip())
                    ),
                )
            except ValueError as exc:
                logger.info("Reject patch: %s", exc)
                info["submission"] = None
                return None

        _atomic_write_text(patch_output_file, model_patch)
        info["submission"] = model_patch
        runtime_project_path = str(self._instance.data.get("project_path") or "").strip()
        runtime_defect_files_raw = self._instance.data.get("defect_files", [])
        runtime_defect_files = (
            [str(item) for item in runtime_defect_files_raw if str(item).strip()]
            if isinstance(runtime_defect_files_raw, list)
            else []
        )
        metadata = {
            "instance_id": instance_id,
            "batch_run_id": (
                os.environ.get("ARKFIX_BATCH_RUN_ID", "").strip()
                or str(getattr(self._args, "suffix", "") or "")
            ),
            "worker_slot": os.environ.get("ARKFIX_WORKER_SLOT", "").strip(),
            "worker_suffix": os.environ.get("ARKFIX_WORKER_SUFFIX", "").strip(),
            "base_sha": base_sha,
            "project_path": runtime_project_path,
            "defect_files": runtime_defect_files,
            "allow_test_patch": allow_test_patch,
            "allow_test_patch_reason": str(
                self._instance.data.get("allow_test_patch_reason") or "none"
            ),
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "gate_attempt_detected": has_gate_attempt,
            "gate_or_env_failure_detected": has_gate_or_env_failure,
            "max_steps_forced_submit": forced_submit_due_to_step_limit,
            "patch_only_generation": patch_only_generation,
            "base_apply_check": base_apply_check,
            "patch_bytes": len(model_patch.encode("utf-8")),
            "patch_sha256": hashlib.sha256(model_patch.encode("utf-8")).hexdigest(),
            "source_patch_sha256": source_patch_sha256,
            "final_validation": (
                "forced_unvalidated"
                if forced_unvalidated
                else "patch_only_pending_serial_build"
                if patch_only_generation
                else "passed"
            ),
            "final_validation_reason": final_validation_reason,
            "final_validation_steps": final_validation_steps,
            "prompt_hashes": self._trajectory_prompt_hashes(trajectory),
            "validation_status": (
                "forced_submit_scope_apply_only"
                if forced_unvalidated
                else "patch_only_scope_apply_pending_serial_build"
                if patch_only_generation
                else "validated_build_repair_status_submit"
            ),
        }
        if info.get("rag"):
            metadata["rag"] = info["rag"]
        meta_file = patch_output_file.with_suffix(".meta.json")
        self._write_patch_metadata(meta_file, metadata)
        if self._is_promising_patch(info):
            # Only print big congratulations if we actually believe
            # the patch will solve the issue
            self._print_patch_message(patch_output_file)
        return patch_output_file

    def _apply_patch(self, patch_file: Path, local_dir: Path) -> None:
        """Apply a patch to a local directory."""

        assert local_dir.is_dir()
        assert patch_file.exists()
        # The resolve() is important, because we're gonna run the cmd
        # somewhere else
        cmd = ["git", "apply", str(patch_file.resolve())]
        try:
            subprocess.run(cmd, cwd=local_dir, check=True)
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to apply patch {patch_file} to {local_dir}: {e}")
            return
        logger.info(f"Applied patch {patch_file} to {local_dir}")


class OpenPRHook(MainHook):
    """This hook opens a PR if the issue is solved and the user has enabled the option."""

    def on_init(self, *, args: ScriptArguments, agent: Agent, env: SWEEnv, traj_dir: Path):
        self._env = env
        self._token: str = env._github_token
        self._data_path = args.environment.data_path
        self._open_pr = args.actions.open_pr
        self._skip_if_commits_reference_issue = args.actions.skip_if_commits_reference_issue

    def on_instance_completed(self, *, info, trajectory):
        if self._open_pr and self.should_open_pr(info):
            self._env.open_pr(trajectory=trajectory)

    def should_open_pr(self, info: dict[str, Any]) -> bool:
        """Does opening a PR make sense?"""
        if not info.get("submission"):
            logger.info("Not opening PR because no submission was made.")
            return False
        if info["exit_status"] != "submitted":
            logger.info("Not opening PR because exit status was %s and not submitted.", info["exit_status"])
            return False
        try:
            issue = get_gh_issue_data(self._data_path, token=self._token)
        except InvalidGithubURL:
            logger.info("Currently only GitHub is supported to open PRs to. Skipping PR creation.")
            return False
        if issue.state != "open":
            logger.info(f"Issue is not open (state={issue.state}. Skipping PR creation.")
            return False
        if issue.assignee:
            logger.info("Issue is already assigned. Skipping PR creation. Be nice :)")
            return False
        if issue.locked:
            logger.info("Issue is locked. Skipping PR creation.")
            return False
        org, repo, issue_number = parse_gh_issue_url(self._data_path)
        associated_commits = get_associated_commit_urls(org, repo, issue_number, token=self._token)
        if associated_commits:
            commit_url_strs = ", ".join(associated_commits)
            if self._skip_if_commits_reference_issue:
                logger.info(f"Issue already has associated commits (see {commit_url_strs}). Skipping PR creation.")
                return False
            else:
                logger.warning(
                    "Proceeding with PR creation even though there are already commits "
                    f"({commit_url_strs}) associated with the issue. Please only do this for your own repositories "
                    "or after verifying that the existing commits do not fix the issue.",
                )
        return True


class Main:
    def __init__(self, args: ScriptArguments):
        if args.print_config:
            logger.info(f"Arguments: {args.dumps_yaml()}")
        self.args = args
        if getattr(args.agent, "patch_only_generation", False) is True:
            os.environ["ARKFIX_PATCH_ONLY_GENERATION"] = "1"
        else:
            os.environ.pop("ARKFIX_PATCH_ONLY_GENERATION", None)
        self.env = SWEEnv(args.environment)
        self.agent = Agent("primary", args.agent)
        self.traj_dir = Path("trajectories") / Path(getuser()) / args.run_name
        self.traj_dir.mkdir(parents=True, exist_ok=True)
        self._save_arguments()
        default_hooks = [
            SaveApplyPatchHook()
        ]
        self.hooks: list[MainHook] = []
        for hook in default_hooks:
            self.add_hook(hook)

    def add_hook(self, hook: MainHook):
        hook.on_init(args=self.args, agent=self.agent, env=self.env, traj_dir=self.traj_dir)
        self.hooks.append(hook)

    def run(self, instance_id):
        # Reset environment
        for hook in self.hooks:
            hook.on_instance_start(index=0, instance=self.env.data[instance_id])
        assert isinstance(instance_id, str)  # mypy
        if self.should_skip(instance_id):
            for hook in self.hooks:
                hook.on_instance_skipped()
            raise _ContinueLoop(cleanup=False)
        logger.info("Beginning task " + instance_id)

        if self.args.skip_workdir_reset:
            logger.info(
                "[修复前] 已跳过本机仓库重置（--skip_workdir_reset）instance_id=%s",
                instance_id,
            )
        else:
            reset_host_workdir_to_dataset_base(self.args.environment, self.env.data[instance_id])

        observation, info = self.env.reset(instance_id)
        if info is None:
            raise _ContinueLoop(cleanup=True)

        # Get info and defect-file information.
        issue = getattr(self.env, "query", None)
        assert self.env.record is not None  # mypy
        raw_defect_files = self.env.record.data.get("defect_files", [])
        missing_defect_files = _missing_defect_files_at_base(
            getattr(self.env, "native_workdir", None),
            raw_defect_files,
        )
        if missing_defect_files:
            issue = _append_missing_defect_file_notes(issue, missing_defect_files)
            logger.info(
                "Known defect files missing at base checkout and treated as creation targets: %s",
                ", ".join(missing_defect_files),
            )
        issue_with_feedback = _append_retry_build_feedback(issue, instance_id)
        if issue_with_feedback != issue:
            issue = issue_with_feedback
            logger.info("Appended prior real-build feedback for %s", instance_id)
        if isinstance(raw_defect_files, list):
            defect_files = "\n".join(f"- {f}" for f in raw_defect_files) if raw_defect_files else "(not specified)"
        else:
            defect_files = str(raw_defect_files)
        # Blind model-patch generation must not inspect gold patch fields. Older configs
        # still contain {files}/{test_files} placeholders, so leave them empty and use only
        # defect_files as the authoritative repair scope.
        files = ""
        test_files = ""
        tests = ""
        # if "FAIL_endTO_PASS" in self.env.record:
        #     tests = "\n".join([f"- {x}" for x in self.env.record["FAIL_TO_PASS"]])

        # HarmonyOS monorepo: hvigor must run from module root (hvigor/hvigor-config.json5), not entry/
        project_path_raw = self.env.record.data.get("project_path", "") or ""
        if isinstance(project_path_raw, str):
            project_path = project_path_raw.strip()
        else:
            project_path = str(project_path_raw).strip()
        if not project_path and isinstance(raw_defect_files, list) and raw_defect_files:
            defect_path_list = [str(x) for x in raw_defect_files]
            native_workdir = getattr(self.env, "native_workdir", None)
            if native_workdir:
                project_path = infer_harmony_project_root_from_filesystem(
                    Path(native_workdir),
                    defect_path_list,
                )
            if not project_path:
                project_path = infer_hvigor_module_root_from_defect_files(defect_path_list)
        if not project_path:
            project_path = "."
        project_path_for_prompt = "." if getattr(self.env, "native_mode", False) else project_path
        defect_file_context, defect_file_context_files = _read_rag_defect_file_context(
            native_root=getattr(self.env, "native_workdir", None),
            raw_defect_files=raw_defect_files,
        )
        rag_context, rag_metadata = _retrieve_rag_for_instance(
            self.args,
            issue=issue,
            defect_files=defect_files,
            project_path=project_path_for_prompt,
            observation=observation,
            defect_file_context=defect_file_context,
            defect_file_context_files=defect_file_context_files,
        )
        if rag_metadata.get("error"):
            logger.warning("RAG retrieval failed for %s: %s", instance_id, rag_metadata.get("error"))
        elif rag_metadata.get("enabled"):
            logger.info(
                "RAG retrieval for %s returned %s hits",
                instance_id,
                rag_metadata.get("hit_count", 0),
            )
        _write_rag_audit(
            traj_dir=self.traj_dir,
            instance_id=instance_id,
            metadata=rag_metadata,
            context=rag_context,
        )

        setup_args = {
            "issue": issue,
            "files": files,
            "test_files": test_files,
            "tests": tests,
            "defect_files": defect_files,
            "project_path": project_path_for_prompt,
            "rag_context": rag_context,
        }
        info, trajectory = self.agent.run(
            setup_args=setup_args,
            env=self.env,
            observation=observation,
            traj_dir=self.traj_dir,
            return_type="info_trajectory",
        )
        if rag_metadata:
            info["rag"] = rag_metadata
        for hook in self.hooks:
            hook.on_instance_completed(info=info, trajectory=trajectory)
        self._save_predictions(instance_id, info)
        if not self.args.skip_workdir_reset:
            restore_host_repo_pool_to_baseline(
                self.args.environment,
                self.env.data[instance_id],
            )

    def main(self):
        for hook in self.hooks:
            hook.on_start()
        try:
            self._run_instances()
        finally:
            if not self.args.skip_workdir_reset and self.env.record is not None:
                restore_host_repo_pool_to_baseline(
                    self.args.environment,
                    self.env.record,
                    strict=False,
                )
            self.env.close()
            for hook in self.hooks:
                hook.on_end()

    def _run_instances(self):
        for instance_id in self.env.data.keys():
            try:
                self.run(instance_id)
            except _ContinueLoop as loop:
                if loop.cleanup and not self.args.skip_workdir_reset:
                    try:
                        restore_host_repo_pool_to_baseline(
                            self.args.environment,
                            self.env.record,
                        )
                    except Exception as cleanup_error:
                        logger.critical("Stopping run because post-instance pool cleanup failed: %s", cleanup_error)
                        self.env.close()
                        break
                continue
            except KeyboardInterrupt:
                logger.info("Exiting InterCode environment...")
                self.env.close()
                break
            except SystemExit:
                logger.critical("Exiting because SystemExit was called")
                self.env.close()
                logger.info("Container closed")
                raise
            except Exception as e:
                traceback.print_exc()
                if self.args.raise_exceptions:
                    self.env.close()
                    raise e
                if self.env.record:
                    logger.warning(f"Failed on {self.env.record.data['instance_id']}: {e}")
                else:
                    logger.warning("Failed on unknown instance")
                if not self.args.skip_workdir_reset:
                    try:
                        restore_host_repo_pool_to_baseline(
                            self.args.environment,
                            self.env.record,
                        )
                    except Exception as cleanup_error:
                        logger.critical("Stopping run because post-instance pool cleanup failed: %s", cleanup_error)
                        self.env.close()
                        break
                self.env.close()
                continue

    def _save_arguments(self) -> None:
        """Save the arguments to a yaml file to the run's trajectory directory."""
        log_path = self.traj_dir / "args.yaml"

        if log_path.exists():
            try:
                other_args = self.args.load_yaml(log_path)
                if self.args.dumps_yaml() != other_args.dumps_yaml():  # check yaml equality instead of object equality
                    logger.warning("**************************************************")
                    logger.warning("Found existing args.yaml with different arguments!")
                    logger.warning("**************************************************")
            except Exception as e:
                logger.warning(f"Failed to load existing args.yaml: {e}")

        with log_path.open("w") as f:
            self.args.dump_yaml(f)

    def should_skip(self, instance_id: str) -> bool:
        """Check if we should skip this instance based on the instance filter and skip_existing flag."""
        # Skip instances that don't match the instance filter
        if re.match(self.args.instance_filter, instance_id) is None:
            logger.info(f"Instance filter not matched. Skipping instance {instance_id}")
            return True

        # If flag is set to False, don't skip
        if not self.args.skip_existing:
            return False

        # Check if there's an existing trajectory for this instance
        log_path = self.traj_dir / (instance_id + ".traj")
        if log_path.exists():
            with log_path.open("r") as f:
                data = json.load(f)
            # If the trajectory has no exit status, it's incomplete and we will redo it
            exit_status = data["info"].get("exit_status", None)
            if exit_status == "early_exit" or exit_status is None:
                logger.info(f"Found existing trajectory with no exit status: {log_path}")
                logger.info("Removing incomplete trajectory...")
                os.remove(log_path)
            else:
                logger.info(f" Skipping existing trajectory: {log_path}")
                return True
        return False

    def _save_predictions(self, instance_id: str, info):
        output_file = self.traj_dir / "all_preds.jsonl"
        known_defect_files = self.env.data[instance_id].data.get("defect_files", [])
        allow_test_patch = self.env.data[instance_id].data.get("allow_test_patch") is True
        model_patch = (
            filter_submission_remove_self_tests(
                info["submission"],
                known_defect_files if isinstance(known_defect_files, list) else [],
                allow_test_patch=allow_test_patch,
            )
            if "submission" in info
            else None
        )
        if "submission" in info:
            if model_patch is not None:
                try:
                    model_patch = _normalize_patch_text_for_utf8_storage(model_patch)
                except ValueError as exc:
                    logger.warning("Dropping prediction for %s: %s", instance_id, exc)
                    model_patch = None
            info["submission"] = model_patch
        datum = {
            KEY_MODEL: Path(self.traj_dir).name,
            KEY_INSTANCE_ID: instance_id,
            KEY_PREDICTION: model_patch,
        }
        with open(output_file, "a+", encoding="utf-8", newline="\n") as fp:
            print(json.dumps(datum, ensure_ascii=False), file=fp, flush=True)
        logger.info(f"Saved predictions to {output_file}")


def get_args(args=None) -> ScriptArguments:
    """Parse command line arguments and return a ScriptArguments object.

    Args:
        args: Optional list of arguments to parse. If not provided, uses sys.argv.
    """
    defaults = ScriptArguments(
        suffix="",
        environment=EnvironmentArguments(
            cli_args= CliArgs(
                workdir=Path("data_files"),
                repo_dir=None,
                pr_file='data/',
                need_clone=True,
                max_workers_build_image=64,
                max_workers_run_instance=64,
                clear_env=False,
                # Force common package managers to use China mirrors during
                # Docker image build / dependency installation.
                # This is injected into generated Dockerfiles as `ENV ...`
                # via multi_swe_bench/harness.
                global_env=[
                    # npm / pnpm both respect npm_config_registry.
                    "npm_config_registry=https://registry.npmmirror.com",
                    "NPM_CONFIG_REGISTRY=https://registry.npmmirror.com",
                    "npm_config_fetch_retries=5",
                    "npm_config_fetch_retry_maxtimeout=120000",
                    # pip mirror (harmless for non-python tasks).
                    "PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple",
                    "PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn",
                ],
            ),
            verbose=True,
            install_environment=True,
            cache_task_images=False,
        ),
        skip_existing=True,
        agent=AgentArguments(
            model=ModelArguments(
                model_name=keys_config.get("MODEL", "gpt4"),
                total_cost_limit=0.0,
                per_instance_cost_limit=3.0,
                temperature=0.0,
                top_p=0.95,
            ),
            config_file=CONFIG_DIR / "arkts_system_prompt.yaml",
        ),
        actions=ActionsArguments(open_pr=False, skip_if_commits_reference_issue=True),
    )

    # Nicer yaml dumping of multiline strings
    def multiline_representer(dumper, data):
        """configures yaml for dumping multiline strings
        Ref: https://stackoverflow.com/questions/8640959/how-can-i-control-what-scalar-form-pyyaml-uses-for-my-data
        """
        if data.count("\n") > 0:  # check for multiline string
            return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
        return dumper.represent_scalar("tag:yaml.org,2002:str", data)

    yaml.add_representer(str, multiline_representer)

    parsed_args = parse(
        ScriptArguments,
        default=defaults,
        add_config_path_arg=False,
        args=args,
        formatter_class=RichHelpFormatter,
        description=Markdown(__doc__),
    )

    return parsed_args


def main(args: ScriptArguments):
    Main(args).main()


if __name__ == "__main__":
    main(get_args())
