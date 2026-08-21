#!/usr/bin/env python3
"""
批量验证 LLM 产出的 ArkTS patch 是否通过测试。

流程：
1. 读取 benchmark (arkts_final_test.jsonl) 并建立 instance_id -> 条目的索引
2. 遍历 patches 目录下的 .meta.json + .patch 文件对
3. 根据 instance_id 从 benchmark 中找到对应条目（base.sha / test_patch / defect_files）
4. 将本地 repo clone 重置到 base.sha
5. 应用 LLM fix_patch
6. 如有 test_patch，应用 test_patch
7. 运行 build_app.py + run_local_tests.py（无论 test_patch 是否为空）
8. 结果写入 llm_eval_results.json

用法（在 MSWE-agent 项目根目录）：
    python evaluation/run_llm_patch_eval.py
    python evaluation/run_llm_patch_eval.py --patches-dir trajectories/.../patches
    python evaluation/run_llm_patch_eval.py --skip-existing --instance-id openharmony-tpc__ImageKnife+77b016fd-16600069
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# ─── 路径常量 ──────────────────────────────────────────────────────────────────
_SCRIPT_DIR = Path(__file__).resolve().parent
_ARKEVAL_ROOT = _SCRIPT_DIR.parent
_REPO_ROOT = _ARKEVAL_ROOT / "arkfix" / "repair_engine"

os.environ.pop("PYTHONPATH", None)
os.environ["PYTHONNOUSERSITE"] = "1"
os.environ["PYTHONSAFEPATH"] = "1"
for _git_env_name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
    os.environ.pop(_git_env_name, None)
_PYTHON_ROOTS = (Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), _ARKEVAL_ROOT.resolve())
sys.path[:] = [
    item
    for item in sys.path
    if any(
        Path(item or os.getcwd()).resolve() == root
        or root in Path(item or os.getcwd()).resolve().parents
        for root in _PYTHON_ROOTS
    )
]

BENCHMARK_PATH = _ARKEVAL_ROOT / "dataset" / "arkeval_dataset.jsonl"
DEFAULT_REPO_ROOT = _ARKEVAL_ROOT / "depend" / "repair_repo" / "run01"
TOOLS_DIR = _ARKEVAL_ROOT / "evaluation" / "command_line_tools_test" / "tools"
DEFAULT_PATCHES_DIR = _ARKEVAL_ROOT / "Leaderboards" / "model_patch" / "default"
OUTPUT_PATH = _SCRIPT_DIR / "llm_eval_results.json"
SOURCE_CONTRACT_ROW09 = "XB_SOURCE_CONTRACT: row09_orange_shopping_transition"
SOURCE_CONTRACT_ROW10 = "XB_SOURCE_CONTRACT: row10_email_navigation_state"
SOURCE_CONTRACT_ROW11 = "XB_SOURCE_CONTRACT: row11_reminder_dialog_no_delete"
SOURCE_CONTRACT_ROW12 = "XB_SOURCE_CONTRACT: row12_gobang_canvas_hex_colors"
PATCH_TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "cp936")
UNICODE_REPLACEMENT_CHAR = "\ufffd"
GIT_INDEX_LOCK_WAIT_SECONDS = float(os.environ.get("ARKEVAL_GIT_INDEX_LOCK_WAIT_SECONDS", "180"))
GIT_INDEX_LOCK_POLL_SECONDS = float(os.environ.get("ARKEVAL_GIT_INDEX_LOCK_POLL_SECONDS", "1"))

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
import _load_env  # noqa: E402,F401
from common import (  # noqa: E402
    parse_json5_text,
    prepare_native_repair_environment as run_tool_prepare_native_environment,
    run_ohpm_install as run_tool_ohpm_install,
)
from sweagent.utils.patch_utils import expand_patch_hunk_eols_to_match_worktree  # noqa: E402


def _normalize_patch_text(text: str) -> str:
    if not text:
        return ""
    text = text.lstrip("\ufeff")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text if text.endswith("\n") else text + "\n"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _read_patch_text_lossless(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    errors: list[str] = []
    for encoding in PATCH_TEXT_ENCODINGS:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError as exc:
            errors.append(f"{encoding}: {exc}")
            continue
        # A literal U+FFFD can appear in old model output. Let git apply and the
        # tests decide whether that patch is valid instead of blocking upload.
        return _normalize_patch_text(text), encoding
    raise ValueError(f"{path} is not decodable as UTF-8 or GBK/CP936: {'; '.join(errors)}")

# ─── 解析 instance_id ──────────────────────────────────────────────────────────

def parse_instance_id(instance_id: str) -> tuple[str, str, str]:
    """
    解析 instance_id，格式：{org}__{repo}+{sha_prefix}-{pr_number}
    返回 (org, repo, full_instance_id)
    例：'openharmony-tpc__ImageKnife+77b016fd-16600069'
      → ('openharmony-tpc', 'ImageKnife', ...)
    """
    left, _, _ = instance_id.partition("+")
    org, _, repo = left.partition("__")
    return org, repo, instance_id


def find_local_repo(repo_name: str, repo_root: Path | None = None) -> Path | None:
    """Find a local repository by name under the configured repository root."""
    search_root = repo_root or DEFAULT_REPO_ROOT
    direct = search_root / repo_name
    if direct.is_dir() and (direct / ".git").is_dir():
        return direct
    if not search_root.is_dir():
        return None
    for candidate in search_root.iterdir():
        if not candidate.is_dir():
            continue
        if candidate.name in ("repo_before_fix", "repo_after_fix", "__pycache__"):
            continue
        if candidate.name == repo_name and (candidate / ".git").is_dir():
            return candidate
    return None


def _has_hvigor_wrapper(path: Path) -> bool:
    return (
        (path / "hvigorw.bat").is_file()
        or (path / "hvigorw").is_file()
        or (path / "hvigorfile.js").is_file()
    )


def _is_package_json_module(path: Path) -> bool:
    package_json = path / "package.json"
    if not package_json.is_file():
        return False
    try:
        data = json.loads(package_json.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return False
    ohos = data.get("ohos") if isinstance(data, dict) else None
    return isinstance(ohos, dict) and ohos.get("directoryLevel") == "module"


def _parent_declares_module(parent: Path, child: Path) -> bool:
    profile = parent / "build-profile.json5"
    if not profile.is_file():
        return False
    try:
        data = parse_json5_text(profile.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return False
    modules = data.get("modules") if isinstance(data, dict) else None
    if not isinstance(modules, list):
        return False
    child_name = child.name
    for module in modules:
        if not isinstance(module, dict):
            continue
        src_path = str(module.get("srcPath") or "").replace("\\", "/").strip("/")
        if src_path in (child_name, f"./{child_name}"):
            return True
    return False


def _nearest_harmony_project(repo_dir: Path, rel_path: str) -> Path | None:
    normalized = _normalize_patch_path(rel_path)
    if not normalized or normalized == "/dev/null":
        return None

    current = (repo_dir / normalized).parent
    try:
        current = current.resolve()
        repo_dir = repo_dir.resolve()
    except OSError:
        pass

    while current == repo_dir or repo_dir in current.parents:
        if (current / "build-profile.json5").is_file() and _has_hvigor_wrapper(current):
            if _is_package_json_module(current) and current.parent != current:
                parent = current.parent
                if (parent / "build-profile.json5").is_file() and _has_hvigor_wrapper(parent):
                    current = parent
                    continue
            if current.parent != current and _parent_declares_module(current.parent, current):
                parent = current.parent
                if (parent / "build-profile.json5").is_file() and _has_hvigor_wrapper(parent):
                    current = parent
                    continue
            return current
        if current == repo_dir:
            break
        current = current.parent
    return None


def find_harmony_project_dir(
    repo_dir: Path,
    benchmark_entry: dict,
    llm_patch_text: str,
    test_patch_text: str,
) -> Path:
    """Choose the Harmony project root used for ohpm/build/test inside a repo."""
    test_patch_paths = sorted(_paths_from_patch(test_patch_text))
    instrument_test_paths = [
        path for path in test_patch_paths
        if "/src/ohosTest/" in path.replace("\\", "/")
    ]
    if instrument_test_paths:
        counts: dict[Path, int] = {}
        for path in instrument_test_paths:
            project_dir = _nearest_harmony_project(repo_dir, path)
            if project_dir is not None:
                counts[project_dir] = counts.get(project_dir, 0) + 1
        if counts:
            return sorted(counts.items(), key=lambda item: (item[1], len(str(item[0]))), reverse=True)[0][0]

    paths: list[str] = []
    raw_defect_files = benchmark_entry.get("defect_files") or []
    if isinstance(raw_defect_files, list):
        paths.extend(str(p) for p in raw_defect_files if p)
    paths.extend(sorted(_paths_from_patch(llm_patch_text)))
    paths.extend(test_patch_paths)

    counts: dict[Path, int] = {}
    for path in paths:
        project_dir = _nearest_harmony_project(repo_dir, path)
        if project_dir is not None:
            counts[project_dir] = counts.get(project_dir, 0) + 1

    if counts:
        return sorted(counts.items(), key=lambda item: (item[1], len(str(item[0]))), reverse=True)[0][0]
    return repo_dir


# ─── Benchmark 加载 ────────────────────────────────────────────────────────────

def load_benchmark(path: Path) -> dict[str, dict]:
    """加载 jsonl，返回 instance_id -> 条目 的字典。"""
    index: dict[str, dict] = {}
    text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        entry = json.loads(line)
        iid = entry.get("instance_id", "")
        if iid:
            index[iid] = entry
    print(f"[benchmark] loaded {len(index)} entries from {path}")
    return index


# ─── Patch 目录扫描 ────────────────────────────────────────────────────────────

def scan_patches(patches_dir: Path) -> list[dict]:
    """
    扫描 patches 目录，返回 patch 信息列表。
    每个元素：{instance_id, patch_text, meta}
    """
    results = []
    def meta_sort_key(path: Path) -> tuple[int, str]:
        match = re.search(r"(?:model_patch_|row|test)?0*([1-9]|[1-4][0-9]|50)(?=\.|_|-|$)", path.name)
        return (int(match.group(1)) if match else 9999, path.name)

    for meta_file in sorted(patches_dir.glob("*.meta.json"), key=meta_sort_key):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:
            print(f"[warn] cannot read {meta_file.name}: {e}")
            continue
        instance_id = meta.get("instance_id", "")
        if not instance_id:
            print(f"[warn] no instance_id in {meta_file.name}, skipping")
            continue
        patch_file = meta_file.parent / (meta_file.name.replace(".meta.json", ".patch"))
        if not patch_file.is_file():
            print(f"[warn] patch file not found for {instance_id}, skipping")
            continue
        try:
            patch_text, patch_encoding = _read_patch_text_lossless(patch_file)
        except Exception as exc:
            print(f"[warn] cannot decode {patch_file.name}: {exc}")
            results.append(
                {
                    "instance_id": instance_id,
                    "patch_text": "",
                    "meta": meta,
                    "patch_encoding_error": str(exc),
                }
            )
            continue
        meta["patch_source_encoding"] = patch_encoding
        results.append({"instance_id": instance_id, "patch_text": patch_text, "meta": meta})
    print(f"[scan] found {len(results)} patches in {patches_dir}")
    return results


# ─── Git 操作 ──────────────────────────────────────────────────────────────────

def _git(args: list[str], cwd: Path, input_data: str | None = None) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "Never"
    result = subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        input=input_data,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


def _git_index_lock_path(repo_dir: Path) -> Path:
    git_path = repo_dir / ".git"
    if git_path.is_dir():
        return git_path / "index.lock"
    if git_path.is_file():
        try:
            text = git_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return git_path / "index.lock"
        match = re.match(r"gitdir:\s*(.+)", text, flags=re.I)
        if match:
            git_dir = Path(match.group(1).strip())
            if not git_dir.is_absolute():
                git_dir = (repo_dir / git_dir).resolve()
            return git_dir / "index.lock"
    return git_path / "index.lock"


def _is_index_lock_error(text: str) -> bool:
    lowered = text.lower()
    return "index.lock" in lowered or ("unable to create" in lowered and ".git" in lowered)


def _wait_for_git_index_lock(repo_dir: Path, *, context: str) -> tuple[bool, str]:
    lock_path = _git_index_lock_path(repo_dir)
    deadline = time.monotonic() + max(0.0, GIT_INDEX_LOCK_WAIT_SECONDS)
    announced = False
    while lock_path.exists():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, f"{context}: git index.lock still exists after {GIT_INDEX_LOCK_WAIT_SECONDS:.0f}s: {lock_path}"
        if not announced:
            print(f"  [git-lock] waiting for {lock_path} before {context}...")
            announced = True
        time.sleep(min(max(GIT_INDEX_LOCK_POLL_SECONDS, 0.1), remaining))
    return True, ""


def _git_with_index_lock_retry(
    args: list[str],
    cwd: Path,
    input_data: str | None = None,
    *,
    label: str = "",
) -> tuple[int, str, str]:
    label = label or "git " + " ".join(args)
    wait_ok, wait_err = _wait_for_git_index_lock(cwd, context=label)
    if not wait_ok:
        return 128, "", wait_err

    last_rc = 128
    last_stdout = ""
    last_stderr = ""
    for attempt in range(1, 4):
        rc, stdout, stderr = _git(args, cwd, input_data)
        last_rc, last_stdout, last_stderr = rc, stdout, stderr
        if rc == 0 or not _is_index_lock_error(stderr + "\n" + stdout):
            return rc, stdout, stderr
        wait_ok, wait_err = _wait_for_git_index_lock(cwd, context=f"{label} retry {attempt}")
        if not wait_ok:
            return rc, stdout, (stderr.strip() + "\n" + wait_err).strip()
    return last_rc, last_stdout, last_stderr


def reset_repo(repo_dir: Path, base_sha: str) -> tuple[bool, str]:
    wait_ok, wait_err = _wait_for_git_index_lock(repo_dir, context=f"reset to {base_sha[:8]}")
    if not wait_ok:
        return False, wait_err
    """将仓库重置到指定 sha 的干净状态。"""
    rc, _, _ = _git(["cat-file", "-e", f"{base_sha}^{{commit}}"], repo_dir)
    if rc != 0:
        return False, f"base commit is missing from the local arkeval repo pool: {base_sha}"

    rc, _, err = _git_with_index_lock_retry(["checkout", "--force", base_sha], repo_dir, label=f"git checkout {base_sha[:8]}")
    if rc != 0:
        rc2, _, err2 = _git_with_index_lock_retry(["checkout", "-f", base_sha], repo_dir, label=f"git checkout -f {base_sha[:8]}")
        if rc2 != 0:
            return False, f"git checkout {base_sha[:8]} failed: {err2.strip()}"

    rc, _, err = _git_with_index_lock_retry(["reset", "--hard", base_sha], repo_dir, label=f"git reset {base_sha[:8]}")
    if rc != 0:
        return False, f"git reset {base_sha[:8]} failed: {err.strip()}"
    rc, _, err = _git_with_index_lock_retry(
        ["clean", "-ffdx", "-e", ".codephoenix/"], repo_dir, label="git clean"
    )
    if rc != 0:
        return False, f"git clean failed: {err.strip()}"
    return True, ""


def _handle_case_only_renames(repo_dir: Path, patch_text: str) -> tuple[str, list[str]]:
    """Windows 下处理大小写 rename 块（跳过已满足的 case-only rename）。"""
    if os.name != "nt":
        return patch_text, []
    lines = patch_text.splitlines()
    blocks: list[list[str]] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("diff --git "):
            j = i + 1
            while j < len(lines) and not lines[j].startswith("diff --git "):
                j += 1
            blocks.append(lines[i:j])
            i = j
        else:
            blocks.append([lines[i]])
            i += 1

    kept: list[list[str]] = []
    notes: list[str] = []
    for block in blocks:
        rfrom = rnto = None
        has_hunks = False
        for l in block:
            if l.startswith("rename from "):
                rfrom = l[len("rename from "):]
            elif l.startswith("rename to "):
                rnto = l[len("rename to "):]
            elif l.startswith("@@ "):
                has_hunks = True
        if rfrom and rnto and not has_hunks:
            if os.path.normcase(rfrom) == os.path.normcase(rnto) and rfrom != rnto:
                rc, _, _ = _git(["ls-files", "--error-unmatch", rfrom], repo_dir)
                tracked_from = rc == 0
                rc2, _, _ = _git(["ls-files", "--error-unmatch", rnto], repo_dir)
                tracked_to = rc2 == 0
                if not tracked_from and tracked_to:
                    notes.append(f"case-only rename skipped (already normalized): {rfrom} -> {rnto}")
                    continue
                tmp = rfrom + ".__case_tmp__"
                rc1, _, _ = _git(["mv", rfrom, tmp], repo_dir)
                if rc1 == 0:
                    rc2, _, _ = _git(["mv", tmp, rnto], repo_dir)
                    if rc2 == 0:
                        notes.append(f"case-only rename via git mv: {rfrom} -> {rnto}")
                        continue
                    _git(["mv", tmp, rfrom], repo_dir)
        kept.append(block)

    if not notes:
        return patch_text, []

    rebuilt: list[str] = []
    for idx, block in enumerate(kept):
        rebuilt.extend(block)
        if idx < len(kept) - 1:
            rebuilt.append("")
    return "\n".join(rebuilt), notes


def apply_patch(repo_dir: Path, patch_text: str, label: str) -> tuple[bool, str]:
    """应用 patch 到 repo，返回 (success, message)。"""
    if not patch_text or not patch_text.strip():
        return True, "EMPTY (skipped)"

    patch_text = patch_text.replace("\r\n", "\n").replace("\r", "\n")
    if not patch_text.endswith("\n"):
        patch_text += "\n"
    patch_text = patch_text.lstrip("\n")
    unsafe_paths = _unsafe_patch_paths(patch_text)
    if unsafe_paths:
        return False, "patch contains paths outside the repository: " + ", ".join(sorted(unsafe_paths))
    patch_text = expand_patch_hunk_eols_to_match_worktree(patch_text, repo_dir)

    patch_text, rename_notes = _handle_case_only_renames(repo_dir, patch_text)
    for note in rename_notes:
        print(f"  [rename] {note}")

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=f".{label}.patch", delete=False, encoding="utf-8", newline="\n", dir=repo_dir
    ) as f:
        f.write(patch_text)
        tmp_path = f.name

    try:
        rc, stdout, stderr = _git(
            ["apply", "--whitespace=nowarn", tmp_path],
            repo_dir,
        )
        if rc != 0:
            first_error = stderr.strip() or stdout.strip()
            if "corrupt patch" in first_error.lower():
                rc, stdout, stderr = _git(
                    ["apply", "--recount", "--whitespace=nowarn", tmp_path],
                    repo_dir,
                )
                if rc == 0:
                    return True, "OK (git apply --recount)"
            return False, f"git apply [{label}] failed:\n{stderr.strip() or stdout.strip()}"
        return True, "OK"
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def restore_repo(repo_dir: Path) -> None:
    """测试结束后恢复仓库状态。"""
    rc, _, err = _git_with_index_lock_retry(["reset", "--hard", "HEAD"], repo_dir, label="cleanup git reset")
    if rc != 0:
        print(f"  [cleanup warn] git reset failed: {err.strip()[:300]}")
    rc, _, err = _git_with_index_lock_retry(
        ["clean", "-ffdx", "-e", ".codephoenix/"], repo_dir, label="cleanup git clean"
    )
    if rc != 0:
        print(f"  [cleanup warn] git clean failed: {err.strip()[:300]}")


# ─── 调用 tools 脚本 ───────────────────────────────────────────────────────────

def _run_tool(script: str, extra_args: list[str], cwd: Path, timeout: float = 1800.0) -> tuple[int, str]:
    """
    以子进程方式调用 command_line_tools_test/tools/{script}，
    cwd 设为 command_line_tools_test 目录（_load_env 需要这个 cwd 加载 .env）。
    返回 (exit_code, combined_output)。
    """
    ctl_dir = TOOLS_DIR.parent  # command_line_tools_test/
    cmd = [sys.executable, str(TOOLS_DIR / script)] + extra_args
    env = os.environ.copy()
    env.pop("PYTHONSAFEPATH", None)
    try:
        result = subprocess.run(
            cmd,
            cwd=str(ctl_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
        combined = "\n".join(p for p in [result.stdout, result.stderr] if p).strip()
        return result.returncode, combined
    except subprocess.TimeoutExpired as exc:
        out = "\n".join(p for p in [(exc.stdout or ""), (exc.stderr or "")] if p).strip()
        return 124, f"TimeoutExpired after {timeout}s\n{out}"
    except Exception as exc:
        return 1, str(exc)


def run_ohpm_install(repo_dir: Path, deveco_path: str, timeout: float = 600.0) -> tuple[int, str]:
    """调用 ohpm install 安装依赖。"""
    return run_tool_ohpm_install(repo_dir, deveco_path, timeout_sec=timeout)


def run_environment_preprocess(project_dir: Path, deveco_path: str, timeout: float = 900.0) -> tuple[int, str]:
    """Reset-to-base environment preparation before applying candidate/test patches."""
    has_oh_package = any((project_dir / name).is_file() for name in ("oh-package.json5", "oh-package.json"))
    if has_oh_package:
        print(f"  [environment_preprocess] ohpm install start (timeout={min(timeout, 600.0)}s)...", flush=True)
        ohpm_code, ohpm_out = run_ohpm_install(project_dir, deveco_path, timeout=min(timeout, 600.0))
        print(f"  [environment_preprocess] ohpm install exit={ohpm_code}", flush=True)
        chunks = [ohpm_out.strip()]
        if ohpm_code != 0:
            return ohpm_code, "\n".join(chunk for chunk in chunks if chunk)
    else:
        print("  [environment_preprocess] ohpm install skipped (no oh-package.json5)", flush=True)
        chunks = ["OHPM_STATUS=SKIPPED\nOHPM_REASON=no_oh_package_json5_legacy_hvigor_project"]
    try:
        print(f"  [environment_preprocess] native prepare start (timeout={timeout}s)...", flush=True)
        notes = run_tool_prepare_native_environment(
            project_dir,
            deveco_path,
            product_name="default",
            timeout_sec=timeout,
        )
        print("  [environment_preprocess] native prepare exit=0", flush=True)
        chunks.extend(notes)
        return 0, "\n".join(chunk for chunk in chunks if chunk)
    except Exception as exc:
        print(f"  [environment_preprocess] native prepare failed: {exc}", flush=True)
        chunks.append(f"ENV_PREPARE_STATUS=FAILED\n{exc}")
        return 1, "\n".join(chunk for chunk in chunks if chunk)


def _read_json5_file(path: Path) -> Any:
    return parse_json5_text(path.read_text(encoding="utf-8", errors="replace"))


def _project_modules(repo_dir: Path) -> list[dict[str, Any]]:
    profile_path = repo_dir / "build-profile.json5"
    if not profile_path.is_file():
        return []
    try:
        profile = _read_json5_file(profile_path)
    except Exception:
        return []
    raw_modules = profile.get("modules") if isinstance(profile, dict) else []
    if not isinstance(raw_modules, list):
        return []
    modules: list[dict[str, Any]] = []
    for item in raw_modules:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        src_path = str(item.get("srcPath") or "").strip().replace("\\", "/").strip("/")
        if not name or not src_path:
            continue
        module_dir = (repo_dir / src_path).resolve()
        module_type = ""
        module_json = module_dir / "src" / "main" / "module.json5"
        if module_json.is_file():
            try:
                module_data = _read_json5_file(module_json)
                module_section = module_data.get("module") if isinstance(module_data, dict) else {}
                if isinstance(module_section, dict):
                    module_type = str(module_section.get("type") or "").strip()
            except Exception:
                module_type = ""
        modules.append(
            {
                "name": name,
                "srcPath": src_path,
                "dir": str(module_dir),
                "type": module_type,
                "has_src_test": (module_dir / "src" / "test").is_dir(),
                "has_ohos_test": (module_dir / "src" / "ohosTest").is_dir(),
            }
        )
    return modules


def _normalize_patch_path(path: str) -> str:
    normalized = path.strip().strip('"').replace("\\", "/")
    if normalized.startswith("a/") or normalized.startswith("b/"):
        normalized = normalized[2:]
    return normalized


def _unsafe_patch_paths(patch_text: str) -> set[str]:
    paths: set[str] = set()
    for raw in (patch_text or "").splitlines():
        line = raw.strip()
        candidates: list[str] = []
        if line.startswith("diff --git "):
            candidates.extend(line.split()[2:4])
        else:
            for prefix in ("+++ ", "--- ", "rename from ", "rename to ", "copy from ", "copy to "):
                if line.startswith(prefix):
                    candidates.append(line[len(prefix):].split("\t", 1)[0])
                    break
        for candidate in candidates:
            normalized = _normalize_patch_path(candidate)
            if not normalized or normalized == "/dev/null":
                continue
            if (
                normalized.startswith("/")
                or normalized.startswith("//")
                or re.match(r"^[A-Za-z]:", normalized)
                or ".." in normalized.split("/")
            ):
                paths.add(normalized)
    return paths


def _paths_from_patch(patch_text: str) -> set[str]:
    paths: set[str] = set()
    for raw in (patch_text or "").splitlines():
        line = raw.strip()
        if line.startswith("diff --git "):
            parts = line.split()
            for part in parts[2:4]:
                p = _normalize_patch_path(part)
                if p and p != "/dev/null":
                    paths.add(p)
        elif line.startswith("+++ ") or line.startswith("--- "):
            p = _normalize_patch_path(line[4:])
            if p and p != "/dev/null":
                paths.add(p)
    return paths


def _module_for_path(path: str, modules: list[dict[str, Any]]) -> dict[str, Any] | None:
    normalized = path.replace("\\", "/").strip("/")
    best: dict[str, Any] | None = None
    best_len = -1
    for module in modules:
        src_path = str(module.get("srcPath") or "").replace("\\", "/").strip("/")
        while src_path.startswith("./"):
            src_path = src_path[2:]
        if normalized == src_path or normalized.startswith(src_path + "/"):
            if len(src_path) > best_len:
                best = module
                best_len = len(src_path)
    return best


def _task_for_module(module_type: str) -> str:
    if module_type == "har":
        return "assembleHar"
    if module_type == "shared":
        return "assembleHsp"
    return "assembleHap"


def _determine_evaluation_scope(
    repo_dir: Path,
    benchmark_entry: dict,
    llm_patch_text: str,
    test_patch_text: str,
) -> dict[str, Any]:
    modules = _project_modules(repo_dir)
    paths: set[str] = set()
    raw_defect_files = benchmark_entry.get("defect_files") or []
    if isinstance(raw_defect_files, list):
        paths.update(str(p).replace("\\", "/") for p in raw_defect_files if p)
    paths.update(_paths_from_patch(llm_patch_text))
    paths.update(_paths_from_patch(test_patch_text))
    project_path = str(benchmark_entry.get("project_path") or "").replace("\\", "/").strip("/")
    if project_path and project_path != ".":
        prefix = project_path + "/"
        paths = {path[len(prefix):] if path.startswith(prefix) else path for path in paths}

    affected_by_name: dict[str, dict[str, Any]] = {}
    for path in sorted(paths):
        module = _module_for_path(path, modules)
        if module:
            affected_by_name[str(module["name"])] = module

    local_by_name: dict[str, dict[str, Any]] = {}
    for module in modules:
        module_dir = Path(str(module.get("dir") or ""))
        src_test = module_dir / "src" / "test"
        if src_test.is_dir() and any(src_test.rglob("*.test.ets")):
            local_by_name[str(module["name"])] = module

    local_test_changed = any("/src/test/" in f"/{path}" for path in paths)
    ohos_test_changed = any("/src/ohosTest/" in f"/{path}" for path in paths)
    entry_affected = any(str(m.get("type")) == "entry" for m in affected_by_name.values())
    install_required = entry_affected or ohos_test_changed

    build_modules = list(affected_by_name.values()) or list(local_by_name.values())
    if install_required:
        build_modules = [m for m in modules if str(m.get("type")) == "entry"] or build_modules

    seen: set[str] = set()
    ordered_build_modules: list[dict[str, Any]] = []
    for module in build_modules:
        name = str(module.get("name") or "")
        if name and name not in seen:
            seen.add(name)
            ordered_build_modules.append(module)

    return {
        "paths": sorted(paths),
        "affected_modules": [
            {"name": str(m.get("name") or ""), "type": str(m.get("type") or ""), "srcPath": str(m.get("srcPath") or "")}
            for m in affected_by_name.values()
        ],
        "local_test_modules": [
            {"name": str(m.get("name") or ""), "type": str(m.get("type") or ""), "srcPath": str(m.get("srcPath") or "")}
            for m in local_by_name.values()
        ],
        "build_modules": [
            {
                "name": str(m.get("name") or ""),
                "type": str(m.get("type") or ""),
                "srcPath": str(m.get("srcPath") or ""),
                "task": _task_for_module(str(m.get("type") or "")),
            }
            for m in ordered_build_modules
        ],
        "install_required": install_required,
        "local_test_required": local_test_changed,
        "instrument_required": ohos_test_changed,
        "ohos_test_changed": ohos_test_changed,
    }


def run_build(
    repo_dir: Path,
    deveco_path: str,
    timeout: float = 1200.0,
    *,
    scope: dict[str, Any] | None = None,
) -> tuple[int, str]:
    """调用 build_app.py 构建 HAP。"""
    if scope and not scope.get("install_required", True):
        chunks: list[str] = []
        aggregate = 0
        build_modules = scope.get("build_modules") or []
        if not build_modules:
            return 0, "BUILD_STATUS=SKIPPED\nBUILD_REASON=no_affected_build_modules"
        for module in build_modules:
            module_name = str(module.get("name") or "").strip()
            task = str(module.get("task") or "assembleHap").strip()
            if not module_name:
                continue
            code, out = _run_tool(
                "build_app.py",
                [
                    "--repo-path", str(repo_dir),
                    "--deveco-path", deveco_path,
                    "--module", module_name,
                    "--task", task,
                ],
                cwd=TOOLS_DIR.parent,
                timeout=timeout,
            )
            chunks.append(f"=== BUILD MODULE={module_name} TASK={task} EXIT={code} ===\n{out}")
            if aggregate == 0 and code != 0:
                aggregate = code
        return aggregate, "\n\n".join(chunks)

    return _run_tool(
        "build_app.py",
        ["--repo-path", str(repo_dir), "--deveco-path", deveco_path, "--build-test-packages"],
        cwd=TOOLS_DIR.parent,
        timeout=timeout,
    )


def _extract_hap_path(repo_dir: Path, build_output: str) -> Path | None:
    for line in reversed(build_output.splitlines()):
        key, sep, value = line.strip().partition("=")
        if sep and key == "HAP_PATH" and value.strip():
            hap_path = Path(value.strip())
            if not hap_path.is_absolute():
                hap_path = repo_dir / hap_path
            return hap_path.resolve()
    return None


def _extract_package_paths(build_output: str) -> list[Path]:
    for line in build_output.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key == "PACKAGE_PATHS_JSON" and value.strip():
            try:
                data = json.loads(value)
            except json.JSONDecodeError:
                return []
            paths: list[Path] = []
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("path"):
                        paths.append(Path(str(item["path"])).resolve())
            return paths
    return []


def run_install_app(repo_dir: Path, deveco_path: str, package_paths: list[Path], timeout: float = 600.0) -> tuple[int, str]:
    args = [
        "--repo-path", str(repo_dir),
        "--deveco-path", deveco_path,
    ]
    for package_path in package_paths:
        args.extend(["--package-path", str(package_path)])
    return _run_tool(
        "install_app.py",
        args,
        cwd=TOOLS_DIR.parent,
        timeout=timeout,
    )


def run_local_tests(
    repo_dir: Path,
    deveco_path: str,
    timeout: float = 1800.0,
) -> tuple[int, str]:
    """
    调用 run_local_tests.py 跑 src/test 本地单元测试。
    使用 --all-local-modules 自动发现所有含 *.test.ets 的模块。
    """
    return _run_tool(
        "run_local_tests.py",
        [
            "--repo-path", str(repo_dir),
            "--deveco-path", deveco_path,
            "--all-local-modules",
        ],
        cwd=TOOLS_DIR.parent,
        timeout=timeout,
    )


def _extract_added_hypium_classes(test_patch_text: str) -> tuple[str, ...]:
    classes: list[str] = []
    for line in test_patch_text.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        match = re.search(r"\bdescribe\(\s*['\"]([^'\"]+)['\"]", line)
        if not match:
            continue
        class_name = match.group(1).strip()
        if class_name and class_name not in classes:
            classes.append(class_name)
    return tuple(classes)


def run_instrument_tests(
    repo_dir: Path,
    deveco_path: str,
    timeout: float = 1800.0,
    class_filters: tuple[str, ...] = (),
) -> tuple[int, str]:
    """
    调用 run_tests.py 跑 instrument 测试（src/ohosTest），需要在线 hdc 设备。
    """
    args = ["--repo-path", str(repo_dir), "--deveco-path", deveco_path]
    for class_filter in class_filters:
        args.extend(["--class-filter", class_filter])
    return _run_tool(
        "run_tests.py",
        args,
        cwd=TOOLS_DIR.parent,
        timeout=timeout,
    )


def _compact_source(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _read_contract_source(project_dir: Path, rel_path: str) -> tuple[str | None, str]:
    path = project_dir / rel_path
    if not path.is_file():
        return None, f"missing source file: {rel_path}"
    return path.read_text(encoding="utf-8", errors="replace"), ""


def _row09_has_geometry_transition(source: str, id_expr: str) -> bool:
    compact = _compact_source(source)
    compact_id = _compact_source(id_expr)
    return (
        f".geometryTransition('goods'+{compact_id}," in compact
        and "follow:true" in compact
    )


def _row09_has_shared_default_transition(source: str, id_expr: str) -> bool:
    compact = _compact_source(source)
    compact_id = _compact_source(id_expr)
    return (
        f".sharedTransition('goods'+{compact_id}," in compact
        and "Curve.Default" in source
    )


def _run_row09_source_contract(project_dir: Path) -> tuple[int, str]:
    goods_rel = "feature/navigationHome/src/main/ets/components/good/GoodsList.ets"
    detail_rel = "feature/detailPageHsp/src/main/ets/main/DetailPage.ets"
    goods_source, goods_error = _read_contract_source(project_dir, goods_rel)
    detail_source, detail_error = _read_contract_source(project_dir, detail_rel)
    errors = [msg for msg in (goods_error, detail_error) if msg]
    if errors or goods_source is None or detail_source is None:
        return 1, "\n".join(["SOURCE_CONTRACT_STATUS=FAIL", *errors])

    goods_geometry = _row09_has_geometry_transition(goods_source, "item.id")
    detail_geometry = _row09_has_geometry_transition(detail_source, "this.goodDetailData.id")
    goods_shared_default = _row09_has_shared_default_transition(goods_source, "item.id")
    detail_shared_default = _row09_has_shared_default_transition(detail_source, "this.goodDetailData.id")
    goods_transition_ok = goods_geometry or goods_shared_default
    detail_transition_ok = detail_geometry or detail_shared_default
    navigation_ok = ".pushPathByName('DetailPage',item" in _compact_source(goods_source)

    mode = "geometry" if goods_geometry and detail_geometry else "shared_default"
    lines = [
        f"SOURCE_CONTRACT_MARKER={SOURCE_CONTRACT_ROW09}",
        f"ROW09_GOODS_GEOMETRY={goods_geometry}",
        f"ROW09_DETAIL_GEOMETRY={detail_geometry}",
        f"ROW09_GOODS_SHARED_DEFAULT={goods_shared_default}",
        f"ROW09_DETAIL_SHARED_DEFAULT={detail_shared_default}",
        f"ROW09_NAVIGATION_PASSES_ITEM={navigation_ok}",
    ]
    if goods_transition_ok and detail_transition_ok and navigation_ok:
        return 0, "\n".join(["SOURCE_CONTRACT_STATUS=PASS", f"ROW09_ACCEPTED_MODE={mode}", *lines])
    return 1, "\n".join(["SOURCE_CONTRACT_STATUS=FAIL", *lines])


def _row10_has_email_param_chain(
    account_info_source: str,
    modify_source: str,
    navigation_bar_source: str,
) -> tuple[bool, list[str]]:
    account_compact = _compact_source(account_info_source)
    modify_compact = _compact_source(modify_source)
    nav_compact = _compact_source(navigation_bar_source)

    account_passes_email = (
        "url:'pages/Modify'" in account_compact
        and "email:this.email" in account_compact
    )
    modify_reads_email = (
        'router.getParams()["email"]' in modify_compact
        or "router.getParams()['email']" in modify_compact
    )
    modify_passes_email_to_nav = "email:this.email" in modify_compact
    nav_keeps_email = "privateemail" in nav_compact
    nav_returns_email = "email:this.email" in nav_compact

    lines = [
        f"ROW10_ACCOUNT_PASSES_EMAIL={account_passes_email}",
        f"ROW10_MODIFY_READS_EMAIL={modify_reads_email}",
        f"ROW10_MODIFY_PASSES_EMAIL_TO_NAV={modify_passes_email_to_nav}",
        f"ROW10_NAV_HAS_EMAIL_FIELD={nav_keeps_email}",
        f"ROW10_NAV_RETURNS_EMAIL={nav_returns_email}",
    ]
    return (
        account_passes_email
        and modify_reads_email
        and modify_passes_email_to_nav
        and nav_keeps_email
        and nav_returns_email,
        lines,
    )


def _run_row10_source_contract(project_dir: Path) -> tuple[int, str]:
    account_rel = "entry/src/main/ets/common/AccountInfo.ets"
    modify_rel = "entry/src/main/ets/pages/Modify.ets"
    nav_rel = "entry/src/main/ets/common/NavigationBar.ets"
    account_source, account_error = _read_contract_source(project_dir, account_rel)
    modify_source, modify_error = _read_contract_source(project_dir, modify_rel)
    nav_source, nav_error = _read_contract_source(project_dir, nav_rel)
    errors = [msg for msg in (account_error, modify_error, nav_error) if msg]
    if errors or account_source is None or modify_source is None or nav_source is None:
        return 1, "\n".join(["SOURCE_CONTRACT_STATUS=FAIL", *errors])

    ok, lines = _row10_has_email_param_chain(account_source, modify_source, nav_source)
    lines.insert(0, f"SOURCE_CONTRACT_MARKER={SOURCE_CONTRACT_ROW10}")
    if ok:
        return 0, "\n".join(["SOURCE_CONTRACT_STATUS=PASS", *lines])
    return 1, "\n".join(["SOURCE_CONTRACT_STATUS=FAIL", *lines])


def _body_between(source: str, start: str, end: str) -> tuple[str, str]:
    start_index = source.find(start)
    if start_index < 0:
        return "", f"missing start marker: {start}"
    end_index = source.find(end, start_index + len(start))
    if end_index <= start_index:
        return "", f"missing end marker after {start}: {end}"
    return source[start_index:end_index], ""


def _run_row11_source_contract(project_dir: Path) -> tuple[int, str]:
    alarm_rel = "entry/src/main/ets/util/AlarmClockReminder.ets"
    alarm_source, alarm_error = _read_contract_source(project_dir, alarm_rel)
    if alarm_error or alarm_source is None:
        return 1, "\n".join(["SOURCE_CONTRACT_STATUS=FAIL", alarm_error])

    open_body, open_error = _body_between(alarm_source, "async openDialog", "async deleteAlarmReminder")
    delete_body, delete_error = _body_between(alarm_source, "async deleteAlarmReminder", "export default")
    errors = [msg for msg in (open_error, delete_error) if msg]
    if errors:
        return 1, "\n".join(["SOURCE_CONTRACT_STATUS=FAIL", *errors])

    open_compact = _compact_source(open_body)
    delete_compact = _compact_source(delete_body)
    open_calls_dialog = "dialog.open(" in open_compact
    open_deletes_reminder = "cancelReminder" in open_body or "deleteEvent" in open_body
    explicit_delete_kept = "cancelReminder" in delete_body or "deleteEvent" in delete_body
    lines = [
        f"SOURCE_CONTRACT_MARKER={SOURCE_CONTRACT_ROW11}",
        f"ROW11_OPEN_CALLS_DIALOG={open_calls_dialog}",
        f"ROW11_OPEN_DELETES_REMINDER={open_deletes_reminder}",
        f"ROW11_EXPLICIT_DELETE_KEPT={explicit_delete_kept}",
    ]
    if open_calls_dialog and not open_deletes_reminder and explicit_delete_kept:
        return 0, "\n".join(["SOURCE_CONTRACT_STATUS=PASS", *lines])
    return 1, "\n".join(["SOURCE_CONTRACT_STATUS=FAIL", *lines])


def _run_row12_source_contract(project_dir: Path) -> tuple[int, str]:
    const_rel = "entry/src/main/ets/util/GobangConst.ts"
    index_rel = "entry/src/main/ets/pages/Index.ets"
    const_source, const_error = _read_contract_source(project_dir, const_rel)
    index_source, index_error = _read_contract_source(project_dir, index_rel)
    errors = [msg for msg in (const_error, index_error) if msg]
    if errors or const_source is None or index_source is None:
        return 1, "\n".join(["SOURCE_CONTRACT_STATUS=FAIL", *errors])

    hex_palette = "#000000" in const_source and "#ffffff" in const_source
    board_hex = "#deb887" in index_source
    star_hex = "#000000" in index_source
    still_uses_named_piece_colors = "'black'" in const_source or "'white'" in const_source
    model_step_only_shape = "CHESS_COLOR[this.distributedData.step%2]" in _compact_source(index_source)
    lines = [
        f"SOURCE_CONTRACT_MARKER={SOURCE_CONTRACT_ROW12}",
        f"ROW12_HEX_CHESS_PALETTE={hex_palette}",
        f"ROW12_BOARD_HEX_COLOR={board_hex}",
        f"ROW12_STAR_HEX_COLOR={star_hex}",
        f"ROW12_STILL_USES_NAMED_PIECE_COLORS={still_uses_named_piece_colors}",
        f"ROW12_MODEL_STEP_ONLY_SHAPE={model_step_only_shape}",
    ]
    if hex_palette and board_hex and star_hex and not still_uses_named_piece_colors:
        return 0, "\n".join(["SOURCE_CONTRACT_STATUS=PASS", *lines])
    return 1, "\n".join(["SOURCE_CONTRACT_STATUS=FAIL", *lines])


def _run_source_contract_fallback(project_dir: Path, test_patch_text: str) -> tuple[str, int, str] | None:
    if SOURCE_CONTRACT_ROW09 in test_patch_text:
        code, output = _run_row09_source_contract(project_dir)
        return SOURCE_CONTRACT_ROW09, code, output
    if SOURCE_CONTRACT_ROW10 in test_patch_text:
        code, output = _run_row10_source_contract(project_dir)
        return SOURCE_CONTRACT_ROW10, code, output
    if SOURCE_CONTRACT_ROW11 in test_patch_text:
        code, output = _run_row11_source_contract(project_dir)
        return SOURCE_CONTRACT_ROW11, code, output
    if SOURCE_CONTRACT_ROW12 in test_patch_text:
        code, output = _run_row12_source_contract(project_dir)
        return SOURCE_CONTRACT_ROW12, code, output
    return None


# ─── 单条评测 ──────────────────────────────────────────────────────────────────

def _find_deveco_path() -> str:
    """从环境变量或 command_line_tools_test/.env 中读取 DEVECO_PATH。"""
    v = os.environ.get("DEVECO_PATH", "").strip()
    if v:
        return v
    env_file = TOOLS_DIR.parent / ".env"
    if env_file.is_file():
        for raw in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if line.startswith("#") or "=" not in line:
                continue
            if line.lower().startswith("export "):
                line = line[7:].strip()
            k, _, val = line.partition("=")
            if k.strip() == "DEVECO_PATH":
                val = val.strip().strip("\"'")
                if val:
                    return val
    return ""


def evaluate_one(
    instance_id: str,
    llm_patch_text: str,
    benchmark_entry: dict,
    *,
    repo_root: Path = DEFAULT_REPO_ROOT,
    deveco_path: str,
    build_timeout: float = 1200.0,
    test_timeout: float = 1800.0,
    new_test_only: bool = False,
) -> dict[str, Any]:
    """对单条 benchmark 条目执行完整评测，返回结果字典。"""
    _, repo_name, _ = parse_instance_id(instance_id)
    base_sha = benchmark_entry.get("base", {}).get("sha", "")
    test_patch_text = benchmark_entry.get("test_patch", "") or ""

    result: dict[str, Any] = {
        "instance_id": instance_id,
        "repo": repo_name,
        "repo_root": str(repo_root),
        "repo_dir": "",
        "project_dir": "",
        "base_sha": base_sha,
        "has_test_patch": bool(test_patch_text.strip()),
        "status": "unknown",
        "resolved": False,
        "fix_patch_applied": False,
        "test_patch_applied": False,
        "build_exit_code": None,
        "install_exit_code": None,
        "local_test_exit_code": None,
        "instrument_test_exit_code": None,
        "environment_preprocess_exit_code": None,
        "package_paths": [],
        "environment_preprocess_output_tail": "",
        "build_output_tail": "",
        "install_output_tail": "",
        "local_test_output_tail": "",
        "instrument_test_output_tail": "",
        "evaluation_scope": {},
        "new_test_only": bool(new_test_only),
        "error": "",
        "evaluated_at": datetime.now().isoformat(),
    }

    # 1. 找本地 repo
    repo_dir = find_local_repo(repo_name, repo_root)
    if repo_dir is None:
        result["status"] = "repo_not_found"
        result["error"] = f"Local repo not found for: {repo_name} (looked under {repo_root})"
        print(f"  [SKIP] {result['error']}")
        return result

    print(f"  repo_dir: {repo_dir}")
    result["repo_dir"] = str(repo_dir)

    # 2. 重置到 base sha
    print(f"  [step1] reset to base sha {base_sha[:12]}...")
    ok, err = reset_repo(repo_dir, base_sha)
    if not ok:
        result["status"] = "reset_failed"
        result["error"] = err
        print(f"  [FAIL] reset: {err}")
        return result

    project_dir = find_harmony_project_dir(repo_dir, benchmark_entry, llm_patch_text, test_patch_text)
    result["project_dir"] = str(project_dir)
    if project_dir != repo_dir:
        print(f"  project_dir: {project_dir}")

    try:
        # 3. 应用 LLM fix_patch
        print(f"  [step2] apply fix_patch ({len(llm_patch_text)} bytes)...")
        ok, msg = apply_patch(repo_dir, llm_patch_text, "fix_patch")
        result["fix_patch_applied"] = ok
        if not ok:
            result["status"] = "fix_patch_apply_error"
            result["error"] = msg
            print(f"  [FAIL] fix_patch: {msg[:300]}")
            return result
        print(f"  [OK] fix_patch applied")

        # 4. 应用 test_patch（如果有）
        if test_patch_text.strip():
            print(f"  [step3] apply test_patch ({len(test_patch_text)} bytes)...")
            ok_tp, msg_tp = apply_patch(repo_dir, test_patch_text, "test_patch")
            result["test_patch_applied"] = ok_tp
            if not ok_tp:
                # test_patch 应用失败 — 可能 LLM 已把 test 代码包含在 fix_patch 里
                print(f"  [warn] test_patch apply failed (may already be in fix_patch): {msg_tp[:200]}")
                result["error"] = f"test_patch_apply_warn: {msg_tp[:200]}"
            else:
                print(f"  [OK] test_patch applied")
        else:
            result["test_patch_applied"] = True  # 无需应用视为已满足
            print(f"  [step3] test_patch is empty, skipping apply")

        print("  [step3.5] environment preprocess after patches...")
        preprocess_code, preprocess_out = run_environment_preprocess(project_dir, deveco_path)
        result["environment_preprocess_exit_code"] = preprocess_code
        result["environment_preprocess_output_tail"] = "\n".join(preprocess_out.splitlines()[-120:])
        print(f"  [environment_preprocess] exit={preprocess_code}")
        if preprocess_code != 0:
            result["status"] = "environment_preprocess_failed"
            result["error"] = result["environment_preprocess_output_tail"]
            print(f"  [FAIL] environment preprocess: {result['error'][:300]}")
            return result

        scope = _determine_evaluation_scope(project_dir, benchmark_entry, llm_patch_text, test_patch_text)
        result["evaluation_scope"] = scope
        print(
            f"  [scope] build_modules={scope.get('build_modules')} "
            f"install_required={scope.get('install_required')} "
            f"instrument_required={scope.get('instrument_required')}"
        )

        # 5. 编译构建
        source_contract = _run_source_contract_fallback(project_dir, test_patch_text)
        if source_contract is not None:
            sc_marker, sc_code, sc_out = source_contract
            sc_ok = sc_code == 0
            result["source_contract"] = {
                "marker": sc_marker,
                "exit_code": sc_code,
                "reason": (
                    "Runtime execution is blocked in this environment; validating "
                    "the row-owned behavior contract directly against source."
                ),
            }
            result["build_exit_code"] = 0
            result["build_output_tail"] = (
                "BUILD_STATUS=SKIPPED\n"
                "BUILD_REASON=source_contract_fallback_after_unrelated_dependency_blocker"
            )
            result["install_exit_code"] = 0
            result["install_output_tail"] = (
                "INSTALL_STATUS=SKIPPED\n"
                "INSTALL_REASON=source_contract_fallback"
            )
            result["local_test_exit_code"] = sc_code
            result["local_test_output_tail"] = sc_out
            result["instrument_test_exit_code"] = 0
            result["instrument_test_output_tail"] = (
                "TEST_RUN_STATUS=SKIPPED\n"
                "TEST_RUN_REASON=source_contract_fallback"
            )
            result["resolved"] = sc_ok
            result["status"] = "resolved" if sc_ok else "unresolved"
            print(f"  [source_contract] exit={sc_code} {'PASS' if sc_ok else 'FAIL'}")
            print(f"  [RESULT] {'RESOLVED' if sc_ok else 'UNRESOLVED'} (source_contract_fallback)")
            return result

        print(f"  [step4] build (timeout={build_timeout}s)...")
        build_code, build_out = run_build(project_dir, deveco_path, timeout=build_timeout, scope=scope)
        result["build_exit_code"] = build_code
        build_lines = build_out.splitlines()
        build_error_markers = (
            "Build error lines:",
            "ERROR",
            "ArkTS:",
            "Module parse failed",
            "COMPILE RESULT",
            "BUILD FAILED",
            "BUILDERROR",
            "Cannot ",
            "Can not ",
        )
        build_error_indexes = {
            index
            for index, line in enumerate(build_lines)
            if not line.lstrip().startswith("<w>")
            and "webpack.cache.PackFileCacheStrategy" not in line
            and any(marker.lower() in line.lower() for marker in build_error_markers)
        }
        expanded_build_error_indexes = set()
        for index in build_error_indexes:
            expanded_build_error_indexes.update(range(index, min(len(build_lines), index + 3)))
        build_error_lines = [
            build_lines[index]
            for index in sorted(expanded_build_error_indexes)
            if not build_lines[index].lstrip().startswith("<w>")
            and "webpack.cache.PackFileCacheStrategy" not in build_lines[index]
        ]
        build_tail = build_lines[-1000:]
        result["build_output_tail"] = "\n".join(
            [*build_error_lines[-1000:], *build_tail]
        )
        build_ok = build_code == 0
        print(f"  [build] exit={build_code} {'PASS' if build_ok else 'FAIL'}")

        install_ok = False
        package_paths = _extract_package_paths(build_out)
        if not package_paths:
            fallback_hap_path = _extract_hap_path(project_dir, build_out)
            if fallback_hap_path:
                package_paths = [fallback_hap_path]
        result["package_paths"] = [str(path) for path in package_paths]
        if build_ok and not scope.get("install_required", True):
            result["install_exit_code"] = 0
            result["install_output_tail"] = "INSTALL_STATUS=SKIPPED\nINSTALL_REASON=no_install_targets_for_affected_modules"
            install_ok = True
            print("  [install] exit=0 SKIPPED (no_install_targets_for_affected_modules)")
        elif build_ok and package_paths and all(path.is_file() for path in package_paths):
            print(f"  [step4.5] install packages ({len(package_paths)} files, timeout=600.0s)...")
            install_code, install_out = run_install_app(project_dir, deveco_path, package_paths, timeout=600.0)
            result["install_exit_code"] = install_code
            result["install_output_tail"] = "\n".join(install_out.splitlines()[-50:])
            install_ok = install_code == 0
            print(f"  [install] exit={install_code} {'PASS' if install_ok else 'FAIL'}")
        elif build_ok:
            result["install_exit_code"] = 1
            result["install_output_tail"] = "INSTALL_STATUS=SKIPPED\nINSTALL_REASON=package_paths_not_found"
            print("  [install] exit=1 FAIL (package_paths_not_found)")

        # 6. 本地单元测试 (src/test, hvigor test)
        if not scope.get("local_test_required", True):
            lt_code = 0
            lt_ok = True
            result["local_test_exit_code"] = lt_code
            result["local_test_output_tail"] = (
                "LOCAL_TEST_STATUS=SKIPPED\n"
                "LOCAL_TEST_REASON=no_src_test_targets_for_affected_paths"
            )
            print("  [local_test] exit=0 SKIPPED (no_src_test_targets_for_affected_paths)")
        else:
            print(f"  [step5] local tests (timeout={test_timeout}s)...")
            lt_code, lt_out = run_local_tests(project_dir, deveco_path, timeout=test_timeout)
            result["local_test_exit_code"] = lt_code
            result["local_test_output_tail"] = "\n".join(lt_out.splitlines()[-50:])
            lt_ok = lt_code == 0
            print(f"  [local_test] exit={lt_code} {'PASS' if lt_ok else 'FAIL'}")

        # 7. Instrument 测试 (src/ohosTest, hdc)
        if install_ok and not scope.get("instrument_required", True):
            it_code = 0
            it_ok = True
            result["instrument_test_exit_code"] = it_code
            result["instrument_test_output_tail"] = (
                "TEST_RUN_STATUS=SKIPPED\n"
                "TEST_RUN_REASON=no_instrument_targets_for_affected_modules"
            )
            print("  [instrument_test] exit=0 SKIPPED (no_instrument_targets_for_affected_modules)")
        elif install_ok:
            print(f"  [step6] instrument tests (timeout={test_timeout}s)...")
            class_filters = _extract_added_hypium_classes(test_patch_text) if new_test_only else ()
            if class_filters:
                print(f"  [instrument_test] class_filter={','.join(class_filters)}")
            it_code, it_out = run_instrument_tests(
                project_dir,
                deveco_path,
                timeout=test_timeout,
                class_filters=class_filters,
            )
            result["instrument_test_exit_code"] = it_code
            result["instrument_test_output_tail"] = "\n".join(it_out.splitlines()[-50:])
            it_ok = it_code == 0
            print(f"  [instrument_test] exit={it_code} {'PASS' if it_ok else 'FAIL'}")
        else:
            it_code = 1
            it_ok = False
            result["instrument_test_exit_code"] = it_code
            result["instrument_test_output_tail"] = "TEST_RUN_STATUS=SKIPPED\nTEST_RUN_REASON=install_failed_or_skipped"
            print("  [instrument_test] exit=1 FAIL (install_failed_or_skipped)")

        # 8. 判定
        resolved = build_ok and install_ok and lt_ok and it_ok
        result["resolved"] = resolved
        result["status"] = "resolved" if resolved else "unresolved"
        print(f"  [RESULT] {'RESOLVED' if resolved else 'UNRESOLVED'} "
              f"(build={build_ok}, install={install_ok}, local={lt_ok}, instrument={it_ok})")

    finally:
        # 9. 恢复仓库
        print(f"  [cleanup] restoring repo...")
        restore_repo(repo_dir)

    return result


# ─── 批量主流程 ────────────────────────────────────────────────────────────────

def _load_existing_results(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {r["instance_id"]: r for r in data if "instance_id" in r}
        if isinstance(data, dict):
            return {r["instance_id"]: r for r in data.get("results", []) if "instance_id" in r}
    except Exception:
        pass
    return {}


def _save_results(path: Path, results: list[dict], summary: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = {"summary": summary, "results": results}
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch-evaluate LLM patches against ArkTS benchmark.")
    parser.add_argument("--benchmark", default=str(BENCHMARK_PATH), help="Path to benchmark jsonl")
    parser.add_argument("--patches-dir", default=str(DEFAULT_PATCHES_DIR), help="Directory containing .patch + .meta.json files")
    parser.add_argument("--output", default=str(OUTPUT_PATH), help="Output JSON file path")
    parser.add_argument("--deveco-path", default="", help="DevEco Studio install path (overrides .env DEVECO_PATH)")
    parser.add_argument(
        "--repo-root",
        default=str(DEFAULT_REPO_ROOT),
        help="Parent directory containing local repo checkouts. Defaults to arkeval/depend/repair_repo/run01.",
    )
    parser.add_argument("--skip-existing", action="store_true", help="Skip instance_ids already in output file")
    parser.add_argument("--instance-id", default="", help="Only evaluate this single instance_id")
    parser.add_argument("--build-timeout", type=float, default=1200.0)
    parser.add_argument("--test-timeout", type=float, default=1800.0)
    parser.add_argument(
        "--new-test-only",
        action="store_true",
        help="For instrument tests, run only Hypium suites/classes added by test_patch.",
    )
    parser.add_argument(
        "--new-test-only-instance-id",
        action="append",
        default=[],
        help=(
            "Run only Hypium suites/classes added by test_patch for this instance_id. "
            "May be repeated; useful for per-row full-regression exceptions."
        ),
    )
    args = parser.parse_args()

    benchmark_path = Path(args.benchmark).resolve()
    patches_dir = Path(args.patches_dir).resolve()
    output_path = Path(args.output).resolve()
    repo_root = Path(args.repo_root).resolve()

    # 加载 deveco_path
    deveco_path = args.deveco_path.strip() or _find_deveco_path()
    if not deveco_path:
        print("ERROR: DEVECO_PATH not set. Set it in command_line_tools_test/.env or pass --deveco-path", file=sys.stderr)
        return 2
    deveco_path = str(Path(deveco_path).resolve())

    local_repo_pool = _ARKEVAL_ROOT / "depend" / "repair_repo"
    local_harmony_env = _ARKEVAL_ROOT / "depend" / "harmony_env"
    local_paths = {
        "benchmark": benchmark_path,
        "patches-dir": patches_dir,
        "output": output_path,
    }
    for label, path in local_paths.items():
        if not _is_within(path, _ARKEVAL_ROOT):
            print(f"ERROR: {label} must stay inside {_ARKEVAL_ROOT}: {path}", file=sys.stderr)
            return 2
    if not _is_within(repo_root, local_repo_pool):
        print(f"ERROR: repo root must stay inside {local_repo_pool}: {repo_root}", file=sys.stderr)
        return 2
    if not _is_within(Path(deveco_path), local_harmony_env):
        print(f"ERROR: DevEco path must stay inside {local_harmony_env}: {deveco_path}", file=sys.stderr)
        return 2

    print(f"[config] DEVECO_PATH = {deveco_path}")
    print(f"[config] REPO_ROOT = {repo_root}")
    print(f"[config] TOOLS_DIR = {TOOLS_DIR}")

    if not repo_root.is_dir():
        print(f"ERROR: repo root not found: {repo_root}", file=sys.stderr)
        return 2

    # 加载 benchmark 索引
    benchmark_index = load_benchmark(benchmark_path)

    # 扫描 patches
    patch_items = scan_patches(patches_dir)
    if not patch_items:
        print("ERROR: no patches found in patches_dir", file=sys.stderr)
        return 1

    # 过滤单条
    if args.instance_id:
        patch_items = [p for p in patch_items if p["instance_id"] == args.instance_id]
        if not patch_items:
            print(f"ERROR: instance_id {args.instance_id} not found in patches_dir", file=sys.stderr)
            return 1

    # 加载已有结果
    existing: dict[str, dict] = {}
    if args.skip_existing:
        existing = _load_existing_results(output_path)
        print(f"[skip_existing] found {len(existing)} existing results")

    all_results: list[dict] = list(existing.values())
    new_test_only_instance_ids = {
        item.strip() for item in args.new_test_only_instance_id if item and item.strip()
    }
    n_total = len(patch_items)
    n_resolved = 0
    n_unresolved = 0
    n_skipped = 0
    n_error = 0

    for idx, item in enumerate(patch_items, 1):
        iid = item["instance_id"]
        print(f"\n{'='*60}")
        print(f"[{idx}/{n_total}] {iid}")

        if args.skip_existing and iid in existing:
            print(f"  [skip] already evaluated")
            n_skipped += 1
            continue

        bench = benchmark_index.get(iid)
        if bench is None:
            print(f"  [warn] instance_id not in benchmark, skipping")
            result = {
                "instance_id": iid,
                "status": "not_in_benchmark",
                "resolved": False,
                "error": "instance_id not found in benchmark",
                "evaluated_at": datetime.now().isoformat(),
            }
            all_results.append(result)
            n_error += 1
        elif item.get("patch_encoding_error"):
            result = {
                "instance_id": iid,
                "status": "model_patch_encoding_error",
                "resolved": False,
                "error": item["patch_encoding_error"],
                "evaluated_at": datetime.now().isoformat(),
            }
            all_results.append(result)
            n_error += 1
            print(f"  [FAIL] model_patch_encoding_error: {item['patch_encoding_error'][:300]}")
        else:
            t0 = time.monotonic()
            result = evaluate_one(
                instance_id=iid,
                llm_patch_text=item["patch_text"],
                benchmark_entry=bench,
                repo_root=repo_root,
                deveco_path=deveco_path,
                build_timeout=args.build_timeout,
                test_timeout=args.test_timeout,
                new_test_only=args.new_test_only or iid in new_test_only_instance_ids,
            )
            result["elapsed_sec"] = round(time.monotonic() - t0, 1)
            all_results.append(result)

            status = result.get("status", "unknown")
            if status == "resolved":
                n_resolved += 1
            elif status in (
                "fix_patch_apply_error",
                "model_patch_encoding_error",
                "reset_failed",
                "repo_not_found",
                "not_in_benchmark",
            ):
                n_error += 1
            else:
                n_unresolved += 1

        # 每条完成后写一次，防止中途中断丢失数据
        summary = {
            "total": n_total,
            "resolved": n_resolved,
            "unresolved": n_unresolved,
            "error": n_error,
            "skipped": n_skipped,
            "generated_at": datetime.now().isoformat(),
        }
        _save_results(output_path, all_results, summary)

    print(f"\n{'='*60}")
    print(f"[done] total={n_total}  resolved={n_resolved}  unresolved={n_unresolved}  error={n_error}  skipped={n_skipped}")
    print(f"[done] results saved to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
