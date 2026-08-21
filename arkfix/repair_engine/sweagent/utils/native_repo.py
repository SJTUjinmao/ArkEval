from __future__ import annotations

import shutil
import subprocess
import os
import stat
import logging
from pathlib import Path
from typing import Iterable


class NativeRepoError(RuntimeError):
    """Raised when a native benchmark repository cannot be restored safely."""


logger = logging.getLogger(__name__)
_LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1\n"
_HISTORICAL_LFS_FULL_BLOB_SUFFIXES = {".har"}


def _run_git(
    repo_dir: Path,
    args: Iterable[str],
    *,
    check: bool = True,
    timeout: float | None = 600,
) -> subprocess.CompletedProcess[str]:
    cmd = ["git", "-C", str(repo_dir), *args]
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_process_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        raise NativeRepoError(
            f"{' '.join(cmd)} timed out after {timeout} seconds\n"
            f"stdout:\n{(stdout or '')[-4000:]}\n"
            f"stderr:\n{(stderr or '')[-4000:]}"
        ) from exc
    result = subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
    if check and result.returncode != 0:
        cmd = "git -C " + str(repo_dir) + " " + " ".join(args)
        raise NativeRepoError(
            f"{cmd} failed with exit code {result.returncode}\n"
            f"stdout:\n{result.stdout[-4000:]}\n"
            f"stderr:\n{result.stderr[-4000:]}"
        )
    return result


def _run_git_bytes(
    repo_dir: Path,
    args: Iterable[str],
    *,
    check: bool = True,
    timeout: float | None = 600,
) -> subprocess.CompletedProcess[bytes]:
    cmd = ["git", "-C", str(repo_dir), *args]
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_process_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout, stderr = b"", b""
        raise NativeRepoError(
            f"{' '.join(cmd)} timed out after {timeout} seconds\n"
            f"stdout:\n{(stdout or b'')[-4000:].decode('utf-8', errors='replace')}\n"
            f"stderr:\n{(stderr or b'')[-4000:].decode('utf-8', errors='replace')}"
        ) from exc
    result = subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
    if check and result.returncode != 0:
        cmd = "git -C " + str(repo_dir) + " " + " ".join(args)
        raise NativeRepoError(
            f"{cmd} failed with exit code {result.returncode}\n"
            f"stdout:\n{result.stdout[-4000:].decode('utf-8', errors='replace')}\n"
            f"stderr:\n{result.stderr[-4000:].decode('utf-8', errors='replace')}"
        )
    return result


def _kill_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        process.kill()


def _decode_git_path(raw: bytes) -> str:
    return raw.decode("utf-8", errors="surrogateescape")


def _repo_uses_lfs(repo_dir: Path) -> bool:
    attrs = _run_git(repo_dir, ["show", "HEAD:.gitattributes"], check=False)
    if attrs.returncode == 0:
        return "filter=lfs" in attrs.stdout

    try:
        listed = _run_git(repo_dir, ["lfs", "ls-files", "-n"], check=False, timeout=60)
    except NativeRepoError as exc:
        logger.warning("git lfs ls-files timed out while detecting LFS usage; treating repo as non-LFS: %s", exc)
        return False
    return listed.returncode == 0 and bool(listed.stdout.strip())


def ensure_lfs_materialized(
    repo_dir: str | Path,
    *,
    fetch: bool = False,
    fetch_all: bool = False,
) -> bool:
    """Install repo-local Git LFS filters and materialize LFS files if needed."""

    repo = Path(repo_dir).resolve()
    if not _repo_uses_lfs(repo):
        return False

    version = _run_git(repo, ["lfs", "version"], check=False, timeout=30)
    if version.returncode != 0:
        raise NativeRepoError(
            f"{repo} uses Git LFS, but `git lfs version` failed.\n"
            f"stderr:\n{version.stderr[-2000:]}"
        )

    try:
        install = _run_git(repo, ["lfs", "install", "--local"], check=False, timeout=30)
        if install.returncode != 0:
            logger.warning(
                "git lfs install --local failed; continuing with explicit checkout. stderr: %s",
                install.stderr[-1000:],
            )
    except NativeRepoError as exc:
        logger.warning("git lfs install --local timed out; continuing with explicit checkout: %s", exc)
    if fetch:
        fetch_args = ["lfs", "fetch", "--all"] if fetch_all else ["lfs", "fetch"]
        _run_git(repo, fetch_args, timeout=300)
    _run_git(repo, ["lfs", "checkout"], timeout=120)
    return True


def _clear_assume_unchanged_for_binary_lfs_candidates(repo: Path) -> None:
    """Undo flags from previous resets so each instance starts from real Git state."""

    raw = _run_git_bytes(repo, ["ls-files", "-z"], check=False).stdout
    candidates = [
        _decode_git_path(item)
        for item in raw.split(b"\0")
        if item and Path(_decode_git_path(item)).suffix.lower() in _HISTORICAL_LFS_FULL_BLOB_SUFFIXES
    ]
    for index in range(0, len(candidates), 100):
        chunk = candidates[index:index + 100]
        _run_git(repo, ["update-index", "--no-assume-unchanged", "--", *chunk], check=False)


def _windows_case_collision_paths(repo: Path) -> list[str]:
    if os.name != "nt":
        return []
    raw = _run_git_bytes(repo, ["ls-files", "-z"], check=False).stdout
    groups: dict[str, list[str]] = {}
    for item in raw.split(b"\0"):
        if item:
            path = _decode_git_path(item)
            groups.setdefault(path.casefold(), []).append(path)
    return sorted(
        path
        for paths in groups.values()
        if len(set(paths)) > 1
        for path in paths
    )


def _set_case_collision_assume_unchanged(repo: Path, *, enabled: bool) -> list[str]:
    paths = _windows_case_collision_paths(repo)
    flag = "--assume-unchanged" if enabled else "--no-assume-unchanged"
    for index in range(0, len(paths), 100):
        _run_git(repo, ["update-index", flag, "--", *paths[index:index + 100]], check=False)
    return paths


def mask_windows_case_collisions(repo_dir: str | Path) -> list[str]:
    """Hide index entries that a case-insensitive Windows worktree cannot represent."""

    repo = Path(repo_dir).resolve()
    return _set_case_collision_assume_unchanged(repo, enabled=True)


def remove_untracked_reparse_points(
    repo_dir: str | Path,
    *,
    preserve_paths: tuple[str, ...] = (),
) -> list[str]:
    """Remove untracked directory junctions before Git clean can recurse through them."""

    if os.name != "nt":
        return []
    repo = Path(repo_dir).resolve()
    tracked = {
        _decode_git_path(item)
        for item in _run_git_bytes(repo, ["ls-files", "-z"], check=False).stdout.split(b"\0")
        if item
    }
    preserved = {path.strip("/").casefold() for path in preserve_paths}
    removed: list[str] = []
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    reserved_names = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
    for root, dirnames, filenames in os.walk(repo, topdown=True, followlinks=False):
        root_path = Path(root)
        kept: list[str] = []
        for name in dirnames:
            path = root_path / name
            relative = path.relative_to(repo).as_posix()
            first = relative.split("/", 1)[0].casefold()
            if first == ".git" or first in preserved:
                continue
            try:
                is_reparse = bool(getattr(os.lstat(path), "st_file_attributes", 0) & reparse_flag)
            except FileNotFoundError:
                continue
            if not is_reparse:
                kept.append(name)
                continue
            if relative in tracked:
                continue
            try:
                os.rmdir(path)
            except OSError as exc:
                raise NativeRepoError(f"failed to remove untracked reparse point {path}: {exc}") from exc
            removed.append(relative)
        dirnames[:] = kept
        for name in filenames:
            if name.casefold().split(".", 1)[0] not in reserved_names:
                continue
            path = root_path / name
            relative = path.relative_to(repo).as_posix()
            if relative in tracked or relative.split("/", 1)[0].casefold() in preserved:
                continue
            try:
                os.unlink("\\\\?\\" + str(path))
            except OSError as exc:
                raise NativeRepoError(f"failed to remove untracked reserved path {path}: {exc}") from exc
            removed.append(relative)
    return removed


def _reset_submodules(repo: Path) -> None:
    raw = _run_git_bytes(repo, ["ls-files", "-s", "-z"], check=False).stdout
    if not any(item.startswith(b"160000 ") for item in raw.split(b"\0") if item):
        return
    _run_git(repo, ["submodule", "sync", "--recursive"], check=False, timeout=60)
    _run_git(repo, ["submodule", "update", "--recursive", "--force"], timeout=600)


def _status_args(*, nul: bool, preserve_paths: tuple[str, ...] = ()) -> list[str]:
    args = ["status", "--porcelain=v1"]
    if nul:
        args.append("-z")
    if preserve_paths:
        args.extend(["--", "."])
        args.extend(f":(exclude){path.rstrip('/')}/**" for path in preserve_paths)
    return args


def _porcelain_status_entries(
    repo: Path,
    *,
    preserve_paths: tuple[str, ...] = (),
) -> list[tuple[str, str]]:
    raw = _run_git_bytes(repo, _status_args(nul=True, preserve_paths=preserve_paths)).stdout
    entries: list[tuple[str, str]] = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        text = _decode_git_path(item)
        if len(text) < 3:
            entries.append((text, ""))
            continue
        entries.append((text[:2], text[3:]))
    return entries


def _path_uses_lfs_filter(repo: Path, path: str) -> bool:
    result = _run_git(repo, ["check-attr", "filter", "--", path], check=False)
    return result.returncode == 0 and result.stdout.rstrip().endswith("filter: lfs")


def _head_blob_oid(repo: Path, path: str) -> str | None:
    result = _run_git_bytes(repo, ["ls-tree", "-z", "HEAD", "--", path], check=False)
    if result.returncode != 0 or not result.stdout:
        return None
    header = result.stdout.split(b"\t", 1)[0].decode("ascii", errors="replace")
    parts = header.split()
    if len(parts) < 3 or parts[1] != "blob":
        return None
    return parts[2]


def _head_blob_is_lfs_pointer(repo: Path, oid: str) -> bool:
    result = _run_git_bytes(repo, ["cat-file", "blob", oid], check=False)
    return result.returncode == 0 and result.stdout.startswith(_LFS_POINTER_PREFIX)


def _worktree_raw_blob_oid(repo: Path, path: str) -> str | None:
    result = _run_git(repo, ["hash-object", "--no-filters", "--", path], check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _is_historical_lfs_full_blob_dirty(repo: Path, xy: str, path: str) -> bool:
    if xy != " M":
        return False
    if Path(path).suffix.lower() not in _HISTORICAL_LFS_FULL_BLOB_SUFFIXES:
        return False
    if not _path_uses_lfs_filter(repo, path):
        return False
    head_oid = _head_blob_oid(repo, path)
    if not head_oid or _head_blob_is_lfs_pointer(repo, head_oid):
        return False
    return _worktree_raw_blob_oid(repo, path) == head_oid


def _mask_only_historical_lfs_full_blob_dirty(
    repo: Path,
    *,
    preserve_paths: tuple[str, ...] = (),
) -> list[str]:
    """Hide legacy full-blob LFS binaries that exactly match the base commit.

    Some OpenHarmony history contains committed `.har` binaries even though
    `.gitattributes` marks `*.har` as LFS. Git LFS then reports the file as
    modified after a correct reset because it expects an LFS pointer. We only
    mask the path when the raw worktree bytes equal the commit blob exactly.
    """

    entries = _porcelain_status_entries(repo, preserve_paths=preserve_paths)
    if not entries:
        return []
    allowed: list[str] = []
    for xy, path in entries:
        if not _is_historical_lfs_full_blob_dirty(repo, xy, path):
            return []
        allowed.append(path)
    for index in range(0, len(allowed), 100):
        chunk = allowed[index:index + 100]
        _run_git(repo, ["update-index", "--assume-unchanged", "--", *chunk])
    return allowed


def reset_repo_to_commit(
    repo_dir: str | Path,
    base_sha: str,
    *,
    clean_args: list[str] | None = None,
    verify_clean: bool = True,
    fetch_lfs_on_dirty: bool = True,
    preserve_paths: tuple[str, ...] = (),
) -> str:
    """Reset a native repo to a benchmark base commit and keep LFS files clean."""

    repo = Path(repo_dir).resolve()
    base = (base_sha or "").strip()
    if not base:
        raise NativeRepoError("base.sha is empty")
    if not repo.is_dir():
        raise NativeRepoError(f"repository directory does not exist: {repo}")

    base = _run_git(repo, ["rev-parse", "--verify", f"{base}^{{commit}}"]).stdout.strip()
    _clear_assume_unchanged_for_binary_lfs_candidates(repo)
    _set_case_collision_assume_unchanged(repo, enabled=False)
    uses_lfs = ensure_lfs_materialized(repo, fetch=False)
    _run_git(repo, ["reset", "--hard", base])
    remove_untracked_reparse_points(repo, preserve_paths=preserve_paths)
    _run_git(repo, clean_args or ["clean", "-ffdxq"])
    _reset_submodules(repo)
    if uses_lfs:
        _run_git(repo, ["lfs", "checkout"])
    collision_paths = mask_windows_case_collisions(repo)
    if collision_paths:
        logger.info("masked Windows case-colliding index paths: %s", ", ".join(collision_paths))

    head = _run_git(repo, ["rev-parse", "HEAD"]).stdout.strip()
    if head.lower() != base.lower():
        raise NativeRepoError(f"reset verification failed: expected {base}, got {head}")

    if verify_clean:
        status_args = _status_args(nul=False, preserve_paths=preserve_paths)
        dirty = _run_git(repo, status_args).stdout.strip()
        if dirty and uses_lfs and fetch_lfs_on_dirty:
            ensure_lfs_materialized(repo, fetch=True)
            _set_case_collision_assume_unchanged(repo, enabled=False)
            _run_git(repo, ["reset", "--hard", base])
            remove_untracked_reparse_points(repo, preserve_paths=preserve_paths)
            _run_git(repo, clean_args or ["clean", "-ffdxq"])
            _reset_submodules(repo)
            _run_git(repo, ["lfs", "checkout"])
            mask_windows_case_collisions(repo)
            dirty = _run_git(repo, status_args).stdout.strip()
        if dirty:
            masked_paths = _mask_only_historical_lfs_full_blob_dirty(
                repo,
                preserve_paths=preserve_paths,
            )
            if masked_paths:
                dirty = _run_git(repo, status_args).stdout.strip()
                if not dirty:
                    logger.info(
                        "reset verification masked historical LFS full-blob file(s): %s",
                        ", ".join(masked_paths),
                    )
                    return head
            raise NativeRepoError(
                "reset verification failed: working tree is not clean after reset.\n"
                f"git status --porcelain output:\n{dirty}"
            )
    return head


def rebuild_repo_from_source(
    target_repo_dir: str | Path,
    source_repo_dir: str | Path,
    *,
    fetch_lfs: bool = True,
) -> None:
    """Replace a polluted run-local repo copy with a clean source repo copy."""

    target = Path(target_repo_dir).resolve()
    source = Path(source_repo_dir).resolve()
    if not source.is_dir():
        raise NativeRepoError(f"clean source repository does not exist: {source}")
    if target == source:
        raise NativeRepoError(f"refusing to rebuild source repository in place: {target}")
    if source in target.parents:
        raise NativeRepoError(f"refusing to rebuild a path inside the source repository: {target}")
    if not target.name or target.name in {".", ".."}:
        raise NativeRepoError(f"unsafe target repository path: {target}")

    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        def _remove_readonly(func, path, exc_info):
            try:
                os.chmod(path, stat.S_IWRITE)
                func(path)
            except Exception:
                raise exc_info[1]

        shutil.rmtree(target, onexc=_remove_readonly)
    shutil.copytree(source, target, symlinks=True)
    ensure_lfs_materialized(target, fetch=fetch_lfs, fetch_all=False)
