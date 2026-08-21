from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shlex
import subprocess
import tarfile
import tempfile
import time
import traceback
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from subprocess import PIPE, STDOUT
from typing import Any, Callable

from datasets import load_dataset, load_from_disk
from ghapi.all import GhApi
from git import InvalidGitRepositoryError, Repo
from filelock import FileLock, Timeout as FileLockTimeout

import docker
from docker.models.containers import Container
from sweagent.utils.config import keys_config
from sweagent.utils.log import get_logger
from multi_swe_bench.harness.build_dataset import prepare_datas, data_registry
from multi_swe_bench.harness.instance import Record

class NoOutputTimeoutError(TimeoutError): ...

DOCKER_START_UP_DELAY = float(keys_config.get("SWE_AGENT_DOCKER_START_UP_DELAY", 1))
GITHUB_ISSUE_URL_PATTERN = re.compile(r"github\.com\/(.*?)\/(.*?)\/issues\/(\d+)")
GITHUB_REPO_URL_PATTERN = re.compile(r".*[/@]?github\.com\/([^/]+)\/([^/]+)")

LANGUAGE_MAP = {
    "java": ["java"],
    "javascript": ["javascript", "js", "nodejs"],
    "cpp": ["cpp", "c++"],
    "c": ["c"],
    "typescript": ["typescript", "ts"],
    # Aliases: path may contain ``arktsfix_`` / ``arkts_pr_`` / ``arkts_gitee_`` (not ``arkts_``).
    "arkts": ["arkts", "arktsfix", "arkts_pr", "arkts_gitee", "arktsadd"],
    "go": ["go"],
    "rust": ["rust"],
}

logger = get_logger("env_utils")

# Huawei Command Line Tools: bind-mount host root into the agent container so YAML commands like
# `node /opt/command-line-tools/hvigor/bin/hvigorw.js ...` work (same layout as evaluation/run_arkts_hvigor_native.py).
# Configure via keys.cfg or environment:
# - COMMAND_LINE_TOOLS_ROOT_PATH, MSWE_COMMAND_LINE_TOOLS (full path to CLI root, highest priority)
# - COMMAND_LINE_TOOLS_ROOT (alias)
# - MSWE_COMMAND_LINE_TOOLS_ALL + version:
#     * preferred: {all}/command-line-tools-{version}/command-line-tools
#     * legacy:    {all}/{version}/command-line-tools
#   Version comes from: keys.cfg / env JSON ``MSWE_COMMAND_LINE_TOOLS_REPO_MAP`` keyed by ``org/repo``,
#   else global ``MSWE_COMMAND_LINE_TOOLS_VERSION``. (Does not read benchmark jsonl — no schema change.)
CONTAINER_COMMAND_LINE_TOOLS_PATH = "/opt/command-line-tools"


def iter_command_line_tools_roots_from_all_and_version(tools_all: Path | str, version: str) -> list[Path]:
    """Host paths to try for ``MSWE_COMMAND_LINE_TOOLS_ALL`` + CLI folder name/version.

    Supports both:
    - bare semver: ``5.0.5``
    - folder style: ``command-line-tools-5.0.5``

    Typical on-disk layout:
    ``.../command-line-tools-all/command-line-tools-5.0.5/command-line-tools``.
    """
    v_raw = str(version).strip()
    if not v_raw:
        return []
    v = v_raw.removeprefix("command-line-tools-").strip()
    if not v:
        return []
    base = Path(str(tools_all).strip()).expanduser()
    out: list[Path] = []
    seen: set[str] = set()

    def push(p: Path) -> None:
        k = str(p)
        if k in seen:
            return
        seen.add(k)
        out.append(p)

    # Preferred explicit folder-name form.
    push(base / f"command-line-tools-{v}" / "command-line-tools")
    # Legacy variant where caller passes bare semver as directory.
    push(base / v / "command-line-tools")
    # Caller may already pass folder-style token in config/env.
    push(base / v_raw / "command-line-tools")
    return out


def _cli_version_from_repo_map(repo_key: str | None) -> str | None:
    if not repo_key or not str(repo_key).strip():
        return None
    raw = keys_config.get("MSWE_COMMAND_LINE_TOOLS_REPO_MAP", None)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        m = json.loads(str(raw).strip())
    except json.JSONDecodeError:
        logger.warning("MSWE_COMMAND_LINE_TOOLS_REPO_MAP is not valid JSON; ignoring.")
        return None
    if not isinstance(m, dict):
        return None
    key = str(repo_key).strip()
    v = m.get(key)
    if v is None:
        v = m.get(key.replace("/", "__"))
    if v is not None and str(v).strip():
        return str(v).strip()
    return None


def _pick_cli_root_from_candidates(candidates: list[Path]) -> Path | None:
    """Prefer a directory that contains ``hvigor/bin/hvigorw.js``; else first existing dir."""
    resolved: list[Path] = []
    for p in candidates:
        try:
            r = p.expanduser().resolve()
        except OSError:
            continue
        resolved.append(r)
        if r.is_dir() and (r / "hvigor" / "bin" / "hvigorw.js").is_file():
            return r
    for r in resolved:
        if r.is_dir():
            return r
    return None


def resolve_command_line_tools_host_path(
    *,
    repo_key: str | None = None,
) -> Path | None:
    """Resolve host path to Huawei CLI root (directory containing ``hvigor/``).

    Used for Docker bind-mount to :data:`CONTAINER_COMMAND_LINE_TOOLS_PATH`. Per-repo version uses
    ``MSWE_COMMAND_LINE_TOOLS_REPO_MAP`` (``org/repo`` -> semver); no benchmark jsonl fields required.

    Returns ``None`` if unset; logs a warning if set but invalid.
    """
    raw = keys_config.get("COMMAND_LINE_TOOLS_ROOT_PATH", None)
    if raw is None or str(raw).strip() == "":
        raw = keys_config.get("MSWE_COMMAND_LINE_TOOLS", None)
    if raw is None or str(raw).strip() == "":
        raw = keys_config.get("COMMAND_LINE_TOOLS_ROOT", None)
    if raw is not None and str(raw).strip() != "":
        p = Path(str(raw).strip()).expanduser().resolve()
        if not p.is_dir():
            logger.warning(
                "Huawei CLI path (MSWE_COMMAND_LINE_TOOLS / COMMAND_LINE_TOOLS_ROOT) "
                "points to %s which is not a directory; "
                "skipping bind mount to %s (ArkTS hvigor will not work in the agent container).",
                p,
                CONTAINER_COMMAND_LINE_TOOLS_PATH,
            )
            return None
        hvigorw = p / "hvigor" / "bin" / "hvigorw.js"
        if not hvigorw.is_file():
            logger.warning(
                "MSWE_COMMAND_LINE_TOOLS: %s does not contain hvigor/bin/hvigorw.js; "
                "hvigor may fail inside the agent container.",
                p,
            )
        return p

    tools_all = keys_config.get("MSWE_COMMAND_LINE_TOOLS_ALL", None)
    if tools_all is None or str(tools_all).strip() == "":
        return None

    version: str | None = _cli_version_from_repo_map(repo_key)
    if not version:
        tv = keys_config.get("MSWE_COMMAND_LINE_TOOLS_VERSION", None)
        if tv and str(tv).strip():
            version = str(tv).strip()

    if not version:
        return None

    candidates = iter_command_line_tools_roots_from_all_and_version(str(tools_all).strip(), version)
    p = _pick_cli_root_from_candidates(candidates)
    if p is None:
        logger.warning(
            "Huawei CLI: MSWE_COMMAND_LINE_TOOLS_ALL=%s version=%s — no existing directory among %s; "
            "skipping bind mount to %s.",
            tools_all,
            version,
            candidates,
            CONTAINER_COMMAND_LINE_TOOLS_PATH,
        )
        return None
    if not (p / "hvigor" / "bin" / "hvigorw.js").is_file():
        logger.warning(
            "MSWE_COMMAND_LINE_TOOLS_ALL: %s does not contain hvigor/bin/hvigorw.js; "
            "hvigor may fail inside the agent container.",
            p,
        )
    logger.info(
        "Resolved Huawei CLI for mount: %s (version=%s from repo map / global)",
        p,
        version,
    )
    return p


def _resolve_command_line_tools_host_path() -> Path | None:
    """Backward-compatible: global keys / env only (no repo_key)."""
    return resolve_command_line_tools_host_path(repo_key=None)


def _command_line_tools_docker_run_argv(host: Path | None = None) -> list[str]:
    """Extra ``docker run`` argv fragments: ``-v host:/opt/command-line-tools:rw``."""
    resolved = host if host is not None else resolve_command_line_tools_host_path()
    if resolved is None:
        return []
    logger.info(
        "Bind-mounting Huawei command-line-tools: %s -> %s",
        resolved,
        CONTAINER_COMMAND_LINE_TOOLS_PATH,
    )
    return ["-v", f"{resolved}:{CONTAINER_COMMAND_LINE_TOOLS_PATH}:rw"]


def _command_line_tools_volumes_for_docker_py(host: Path | None = None) -> dict[str, dict[str, str]]:
    """``volumes`` argument for ``docker.client.containers.run``."""
    resolved = host if host is not None else resolve_command_line_tools_host_path()
    if resolved is None:
        return {}
    logger.info(
        "Bind-mounting Huawei command-line-tools: %s -> %s",
        resolved,
        CONTAINER_COMMAND_LINE_TOOLS_PATH,
    )
    return {str(resolved): {"bind": CONTAINER_COMMAND_LINE_TOOLS_PATH, "mode": "rw"}}


def arkts_container_huawei_cli_env_bash() -> str:
    """One bash line: export OH/SDK/node PATH + optional default ohpm registry.

    Host layout: ``<command-line-tools>/ohpm`` (e.g. ``.../command-line-tools-5.0.1/command-line-tools/ohpm``)
    maps to ``/opt/command-line-tools/ohpm`` in the agent container.

    Optional ``keys.cfg`` / env:
    - ``MSWE_OHPM_HOME`` — container path to ohpm root (default ``/opt/command-line-tools/ohpm``)
    - ``MSWE_DEVECO_NODE_HOME`` — default ``/opt/command-line-tools/tool/node``
    - ``MSWE_DEVECO_SDK_HOME`` — default ``/opt/command-line-tools/sdk``
    - ``MSWE_OHPM_REGISTRY`` — default ``https://ohpm.openharmony.cn/ohpm/``
    - ``MSWE_OHPM_CONFIG_REGISTRY`` — ``true``/``false``, run ``ohpm config set registry`` when true
    """
    cli = CONTAINER_COMMAND_LINE_TOOLS_PATH
    ohpm_home = str(keys_config.get("MSWE_OHPM_HOME", f"{cli}/ohpm")).strip()
    node_home = str(keys_config.get("MSWE_DEVECO_NODE_HOME", f"{cli}/tool/node")).strip()
    sdk_home = str(keys_config.get("MSWE_DEVECO_SDK_HOME", f"{cli}/sdk")).strip()
    # Layout (Huawei Command Line Tools 5.x Linux): see docs/HUAWEI_COMMAND_LINE_TOOLS_LAYOUT.md
    # - <cli>/bin: official hvigorw / ohpm bash wrappers (set DEVECO_* if unset, then delegate)
    # - <cli>/ohpm/bin: real ohpm CLI
    # - <cli>/tool/ohpm/bin: optional on some installs (often absent on Linux)
    # - <cli>/tool/node/bin: bundled Node for hvigor / ohpm
    path_extra = ":".join(
        [
            f"{cli}/bin",
            f"{ohpm_home}/bin",
            f"{cli}/tool/ohpm/bin",
            f"{node_home}/bin",
        ]
    )
    exports = (
        f"export HOME=/root && "
        f"export OHPM_HOME={shlex.quote(ohpm_home)} && "
        f"export DEVECO_SDK_HOME={shlex.quote(sdk_home)} && "
        f"export HOS_SDK_HOME={shlex.quote(sdk_home)} && "
        f"export OHOS_SDK_HOME={shlex.quote(sdk_home)} && "
        f"export DEVECO_NODE_HOME={shlex.quote(node_home)} && "
        f"export NODE_HOME=$DEVECO_NODE_HOME && "
        f"export PATH={path_extra}:$PATH"
    )
    reg = str(keys_config.get("MSWE_OHPM_REGISTRY", "https://ohpm.openharmony.cn/ohpm/")).strip()
    # Default npm mirror for non-@ohos packages (China). Docker global_env often sets npm_config_registry=npmmirror;
    # that env can make pnpm resolve @ohos/* against the mirror → 404. We write both lines to ~/.npmrc and unset the env.
    npm_default = str(
        keys_config.get("MSWE_NPM_DEFAULT_REGISTRY", "https://registry.npmmirror.com/")
    ).strip()
    # hvigor/pnpm look for ~/.npmrc; without @ohos:registry they hit npm mirror → 404 on @ohos/* packages.
    npmrc_lines = f"registry={npm_default}\n@ohos:registry={reg}"
    write_npmrc = (
        f"printf '%s\\n' {shlex.quote(npmrc_lines)} > /root/.npmrc && "
        "unset npm_config_registry NPM_CONFIG_REGISTRY 2>/dev/null || true"
    )
    do_reg = str(keys_config.get("MSWE_OHPM_CONFIG_REGISTRY", "true")).lower() in (
        "1",
        "true",
        "yes",
    )
    parts = [exports, write_npmrc]
    if do_reg:
        parts.append(
            f"command -v ohpm >/dev/null && ohpm config set registry {shlex.quote(reg)} 2>/dev/null || true"
        )
    return " && ".join(parts)


def get_data_path_name(data_path: str) -> str:
    """if data_path is a file, return the file stem
    elif it's a github url, return the owner__repo_name
    """
    if data_path.startswith("text://"):
        return hashlib.sha256(data_path.removeprefix("text://").encode()).hexdigest()[:6]
    match = GITHUB_ISSUE_URL_PATTERN.search(data_path)
    if match:
        owner, repo, _ = match.groups()
        return f"{owner}__{repo}"
    return Path(data_path).stem


def is_github_issue_url(data_path: str) -> bool:
    """Check if data_path is an URL pointing to a github issue"""
    return GITHUB_ISSUE_URL_PATTERN.search(data_path) is not None


def is_github_repo_url(data_path: str) -> bool:
    """Check if data_path is an URL pointing to a github repository.
    Paths to issues or PRs will also match this pattern.
    """
    return GITHUB_REPO_URL_PATTERN.search(data_path) is not None


# TODO: Why not just use copy_anything_to_container?
def copy_file_to_container(container: Container, contents: str, container_path: str) -> None:
    """
    Copies a given string into a Docker container at a specified path.

    Args:
        container: Docker SDK container object.
        contents: The string to copy into the container.
        container_path: The path inside the container where the string should be copied to.

    Returns:
        None
    """
    temp_file_name = None

    try:
        # Create a temporary file
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_file_name = temp_file.name
            # Write the string to the temporary file and ensure it's written to disk
            temp_file.write(contents.encode("utf-8"))
            temp_file.flush()
            os.fsync(temp_file.fileno())

        # Create a TAR archive in memory containing the temporary file
        with tempfile.NamedTemporaryFile():
            with open(temp_file_name, "rb") as temp_file:
                # Prepare the TAR archive
                with BytesIO() as tar_stream:
                    with tarfile.open(fileobj=tar_stream, mode="w") as tar:
                        tar_info = tarfile.TarInfo(name=os.path.basename(container_path))
                        tar_info.size = os.path.getsize(temp_file_name)
                        tar.addfile(tarinfo=tar_info, fileobj=temp_file)
                    tar_stream.seek(0)
                    # Copy the TAR stream to the container
                    container.put_archive(path=os.path.dirname(container_path), data=tar_stream.read())

    except Exception as e:
        logger.error(f"An error occurred: {e}")
        logger.error(traceback.format_exc())
    finally:
        # Cleanup: Remove the temporary file if it was created
        if temp_file_name and os.path.exists(temp_file_name):
            os.remove(temp_file_name)


def copy_anything_to_container(container: Container, host_path: str, container_path: str) -> None:
    """Copy files or directories from host to container

    Note: Will need to set ownership on the copied files in the container.
    """
    if not Path(host_path).exists():
        msg = f"Path {host_path} does not exist, cannot copy it to container."
        raise FileNotFoundError(msg)
    cmd = ["docker", "cp", host_path, f"{container.id}:{container_path}"]
    logger.debug(f"Copying {host_path} to container at {container_path} with command: {shlex.join(cmd)}")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        msg = f"Error copying {host_path} to container at {container_path}: {e}"
        raise RuntimeError(msg) from e


def read_with_timeout(container: subprocess.Popen, pid_func: Callable, timeout_duration: int) -> str:
    """
    Read data from a subprocess with a timeout.
    This function uses a file descriptor to read data from the subprocess in a non-blocking way.

    Args:
        container: The subprocess container.
        pid_func: A function that returns a list of process IDs (except the PID of the main process).
        timeout_duration: The timeout duration in seconds.

    Returns:
        output: The data read from the subprocess, stripped of trailing newline characters.

    Raises:
        TimeoutError: If the timeout duration is reached while reading from the subprocess.
    """
    buffer = b""
    end_time = time.time() + timeout_duration

    is_windows = platform.system() == "Windows"

    if is_windows:
        import threading
        import queue as _queue

        line_queue: _queue.Queue = _queue.Queue()

        def _reader():
            try:
                fd = container.stdout.fileno() if hasattr(container.stdout, 'fileno') else container.stdout.buffer.fileno()
                while True:
                    chunk = os.read(fd, 4096)
                    if not chunk:
                        break
                    line_queue.put(chunk)
            except Exception:
                pass

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        while time.time() < end_time:
            pids = pid_func()
            if len(pids) > 0:
                time.sleep(0.05)
                continue
            try:
                chunk = line_queue.get(timeout=0.05)
                buffer += chunk
            except _queue.Empty:
                break
            time.sleep(0.05)

    else:
        import select
        fd = container.stdout.fileno()

        def ready_to_read(fd) -> bool:
            return bool(select.select([fd], [], [], 0.01)[0])

        while time.time() < end_time:
            pids = pid_func()
            if len(pids) > 0:
                time.sleep(0.05)
                continue
            if ready_to_read(fd):
                data = os.read(fd, 8192)
                if data:
                    buffer += data
            else:
                break
            time.sleep(0.05)

    if container.poll() is not None:
        msg = f"Subprocess exited unexpectedly.\nCurrent buffer: {buffer.decode()}"
        raise RuntimeError(msg)
    if time.time() >= end_time:
        msg = f"Timeout reached while reading from subprocess.\nCurrent buffer: {buffer.decode()}\nRunning PIDs: {pids}"
        raise TimeoutError(msg)
    return buffer.decode()


PROCESS_DONE_MARKER_START = "///PROCESS-DONE:"
PROCESS_DONE_MARKER_END = ":PROCESS-DONE///"
PROCESS_DONE_REGEX = re.compile(rf"{PROCESS_DONE_MARKER_START}(.+?){PROCESS_DONE_MARKER_END}")
DECODED_BUFFER_FAILURE_THRESHOLD = 0.1

def _check_for_too_many_non_unicode_bytes(buffer: bytes):
    number_of_failures = int(DECODED_BUFFER_FAILURE_THRESHOLD * len(buffer))
    start_byte = 0
    for _ in range(number_of_failures):
        try:
            buffer[start_byte:].decode()
            return
        except UnicodeDecodeError as e:
            start_byte = e.start + 1
    msg = "Too many non-unicode characters in output of command."
    raise UnicodeError(msg)


def read_with_timeout_experimental(
    container: subprocess.Popen, timeout_duration: int | float, no_output_timeout_duration: int | float
) -> tuple[str, str]:
    """
    Read data from a subprocess with a timeout.
    This function uses a file descriptor to read data from the subprocess in a non-blocking way.

    NOTE: This is an experimental implementation that is faster than `read_with_timeout`, but
    has not been thoroughly tested.

    Args:
        container: The subprocess container.
        timeout_duration: The timeout duration in seconds.
        no_output_timeout_duration: The timeout duration to wait if no output is produced, in seconds.

    Returns:
        Output and exit code, both as strings (!)

    Raises:
        TimeoutError: If the timeout duration is reached while reading from the subprocess.
    """
    buffer = b""
    start_time = time.time()
    end_time = start_time + timeout_duration
    end_time_no_output = start_time + no_output_timeout_duration

    is_windows = platform.system() == "Windows"

    if is_windows:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        fd = container.stdout.fileno() if hasattr(container.stdout, "fileno") else container.stdout.buffer.fileno()
        pipe_handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
        peek_named_pipe = ctypes.windll.kernel32.PeekNamedPipe
        peek_named_pipe.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        peek_named_pipe.restype = wintypes.BOOL
        process_done = False
        while time.time() < min(end_time, end_time_no_output):
            available = wintypes.DWORD()
            if not peek_named_pipe(pipe_handle, None, 0, None, ctypes.byref(available), None):
                raise OSError(ctypes.get_last_error(), "PeekNamedPipe failed")
            if available.value:
                chunk = os.read(fd, min(available.value, 65536))
                if not chunk:
                    break
                end_time_no_output = time.time() + no_output_timeout_duration
                buffer += chunk
                if PROCESS_DONE_MARKER_START.encode() in buffer:
                    process_done = True
                    break
            else:
                time.sleep(0.01)

    else:
        import select

        fd = container.stdout.fileno()

        def ready_to_read(fd) -> bool:
            return bool(select.select([fd], [], [], 0.01)[0])

        process_done = False
        while time.time() < min(end_time, end_time_no_output):
            if ready_to_read(fd):
                try:
                    data = os.read(fd, 65536)
                except BlockingIOError:
                    time.sleep(0.05)
                    continue
                if data:
                    end_time_no_output = time.time() + no_output_timeout_duration
                    buffer += data
                    if PROCESS_DONE_MARKER_START in buffer.decode("utf-8", errors="backslashreplace").replace("\r\n", "\n"):
                        process_done = True
                        break
            time.sleep(0.01)

    decoded = buffer.decode("utf-8", errors="backslashreplace").replace("\r\n", "\n")
    body = "\n".join(line for line in decoded.splitlines() if not line.startswith(PROCESS_DONE_MARKER_START))

    if container.poll() is not None:
        exit_c = container.poll()
        msg = f"Subprocess exited unexpectedly with code {exit_c}.\nCurrent buffer: {decoded}"
        raise RuntimeError(msg, body)

    current_time = time.time()
    if not process_done and current_time >= min(end_time, end_time_no_output):
        if current_time >= end_time:
            msg = f"Timeout reached while reading from subprocess.\nCurrent buffer: {decoded}"
            raise TimeoutError(msg, body)
        else:
            msg = f"No output timeout reached while reading from subprocess.\nCurrent buffer: {decoded}"
            raise NoOutputTimeoutError(msg, body)

    if platform.system() != "Windows":
        _check_for_too_many_non_unicode_bytes(buffer=buffer)
    _results = None
    for line in reversed(decoded.splitlines()):
        _results = PROCESS_DONE_REGEX.search(line)
        if _results is not None:
            break
    if _results is None:
        msg = f"Could not find process done marker in last line: {decoded=}, {body=}"
        raise ValueError(msg)
    exit_code = _results.group(1)
    return body.replace(f"{PROCESS_DONE_MARKER_START}{exit_code}{PROCESS_DONE_MARKER_END}", ""), exit_code


def get_background_pids(container_obj: Container):
    pids = container_obj.exec_run("ps -eo pid,comm --no-headers").output.decode().split("\n")
    pids = [x.split() for x in pids if x]
    pids = [x for x in pids if x[1] not in {"ps"} and x[0] != "1"]
    bash_pids = [x for x in pids if x[1] == "bash"]
    other_pids = [x for x in pids if x[1] not in {"bash"}]
    return bash_pids, other_pids


def _get_non_persistent_container(
    ctr_name: str,
    image_name: str,
    *,
    command_line_tools_host_path: Path | None = None,
) -> tuple[subprocess.Popen, set[str]]:
    startup_cmd = [
        "docker",
        "run",
        "-i",
        "--rm",
        *_command_line_tools_docker_run_argv(command_line_tools_host_path),
        "--name",
        ctr_name,
        image_name,
        "/bin/bash",
    ]
    logger.debug("Starting container with command: %s", shlex.join(startup_cmd))
    container = subprocess.Popen(
        startup_cmd,
        stdin=PIPE,
        stdout=PIPE,
        stderr=STDOUT,
        text=True,
        bufsize=1,  # line buffered
    )
    time.sleep(DOCKER_START_UP_DELAY)
    # try to read output from container setup (usually an error), timeout if no output
    output = read_with_timeout(container, lambda: list(), timeout_duration=2)
    if output:
        logger.error(f"Unexpected container setup output: {output}")
    # bash PID is always 1 for non-persistent containers
    return container, {
        "1",
    }


def _get_persistent_container(
    ctr_name: str,
    image_name: str,
    persistent: bool = False,
    *,
    command_line_tools_host_path: Path | None = None,
) -> tuple[subprocess.Popen, set[str]]:
    client = docker.from_env()
    containers = client.containers.list(all=True, filters={"name": ctr_name})
    if ctr_name in [c.name for c in containers]:
        container_obj = client.containers.get(ctr_name)
        if container_obj.status in {"created"}:
            container_obj.start()
        elif container_obj.status in {"running"}:
            pass
        elif container_obj.status in {"exited"}:
            container_obj.restart()
        elif container_obj.status in {"paused"}:
            container_obj.unpause()
        else:
            msg = f"Unexpected container status: {container_obj.status}"
            raise RuntimeError(msg)
    else:
        run_kwargs: dict[str, Any] = {
            "command": "/bin/bash -l -m",
            "name": ctr_name,
            "stdin_open": True,
            "tty": True,
            "detach": True,
            "auto_remove": not persistent,
        }
        vols = _command_line_tools_volumes_for_docker_py(command_line_tools_host_path)
        if vols:
            run_kwargs["volumes"] = vols
        container_obj = client.containers.run(image_name, **run_kwargs)
        container_obj.start()
    startup_cmd = [
        "docker",
        "exec",
        "-i",
        ctr_name,
        "/bin/bash",
        "-l",
    ]
    logger.debug("Starting container with command: %s", shlex.join(startup_cmd))
    container = subprocess.Popen(
        startup_cmd,
        stdin=PIPE,
        stdout=PIPE,
        stderr=STDOUT,
        text=True,
        bufsize=1,  # line buffered
    )
    time.sleep(DOCKER_START_UP_DELAY)
    # try to read output from container setup (usually an error), timeout if no output
    output = read_with_timeout(container, lambda: list(), timeout_duration=2)
    if output:
        logger.error(f"Unexpected container setup output: {output}")
    # Get the process IDs of the container
    # There should be at least a head process and possibly one child bash process
    bash_pids, other_pids = get_background_pids(container_obj)
    total_time_slept = DOCKER_START_UP_DELAY
    # Let's wait for a maximum of 5 x DOCKER_START_UP_DELAY seconds
    # and then check again.
    while len(bash_pids) > 1 or len(other_pids) > 0:
        time.sleep(1)
        total_time_slept += 1
        bash_pids, other_pids = get_background_pids(container_obj)
        if total_time_slept > 5 * DOCKER_START_UP_DELAY:
            break
    bash_pid = 1
    if len(bash_pids) == 1:
        bash_pid = bash_pids[0][0]
    elif len(bash_pids) > 1 or len(other_pids) > 0:
        msg = (
            "Detected alien processes attached or running. Please ensure that no other agents "
            f"are running on this container. PIDs: {bash_pids}, {other_pids}"
        )
        raise RuntimeError(msg)
    return container, {str(bash_pid), "1"}


def get_git_bash_path() -> Path:
    candidates = [
        "C:/Program Files/Git/bin/bash.exe",
        "C:/Program Files (x86)/Git/bin/bash.exe",
        "E:/Program Files/Git/bin/bash.exe",
        "E:/Program Files (x86)/Git/bin/bash.exe",
        "D:/Program Files/Git/bin/bash.exe",
        "D:/Program Files (x86)/Git/bin/bash.exe",
    ]
    for c in candidates:
        if Path(c).is_file():
            return Path(c)
    return Path("bash")


def terminate_process_tree(process: subprocess.Popen, *, timeout: float = 10.0) -> None:
    job = getattr(process, "arkfix_job", None)
    if job is not None:
        job.close()
        process.arkfix_job = None
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
        process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


@contextmanager
def native_build_permit():
    """Share one host-wide build limit across repair, preprocessing, and checks."""

    limit = max(1, int(os.environ.get("ARKFIX_BUILD_CONCURRENCY", "8")))
    timeout = max(1.0, float(os.environ.get("ARKFIX_BUILD_SLOT_TIMEOUT_SECONDS", "14400")))
    deadline = time.monotonic() + timeout
    acquired: FileLock | None = None
    while acquired is None:
        for index in range(limit):
            lock = FileLock(str(Path(tempfile.gettempdir()) / f"arkeval-arkfix-build-{index}.lock"))
            try:
                lock.acquire(timeout=0)
            except FileLockTimeout:
                continue
            acquired = lock
            break
        if acquired is None:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out after {timeout:g}s waiting for an ArkFix build slot")
            time.sleep(0.2)
    try:
        yield
    finally:
        acquired.release()


class WindowsKillOnCloseJob:
    """Own a Windows process tree and terminate it when the job handle closes."""

    def __init__(self) -> None:
        self._handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise OSError(error, "SetInformationJobObject failed")
        self._handle = handle
        self._kernel32 = kernel32

    def assign(self, process: subprocess.Popen) -> None:
        if self._handle is None:
            return
        import ctypes
        from ctypes import wintypes

        process_handle = wintypes.HANDLE(int(process._handle))
        if not self._kernel32.AssignProcessToJobObject(self._handle, process_handle):
            raise OSError(ctypes.get_last_error(), f"AssignProcessToJobObject failed for pid {process.pid}")

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def get_native_shell(work_dir: Path | str) -> tuple[subprocess.Popen, set[str]]:
    bash_path = get_git_bash_path()
    logger.info(f"Starting native shell: {bash_path} at {work_dir}")
    env = os.environ.copy()
    env["LANG"] = "C.UTF-8"
    env["LC_ALL"] = "C.UTF-8"
    env["BASH_SILENCE_DEPRECATION_WARNING"] = "1"
    commands_dir = Path(tempfile.mkdtemp(prefix="swe-agent-commands."))
    env["SWE_AGENT_COMMANDS_DIR"] = commands_dir.as_posix()

    cflags = 0

    # Use unbuffered binary mode to prevent Python's BufferedReader from
    # pre-fetching 8KB and losing data across sequential _communicate calls.
    job = WindowsKillOnCloseJob()
    process: subprocess.Popen | None = None
    try:
        process = subprocess.Popen(
            [str(bash_path), "--login"],
            stdin=PIPE,
            stdout=PIPE,
            stderr=STDOUT,
            text=False,
            bufsize=0,
            cwd=str(work_dir),
            env=env,
            creationflags=cflags,
        )
        process.arkfix_job = job
        job.assign(process)
    except Exception:
        import shutil

        if process is not None and process.poll() is None:
            terminate_process_tree(process)
        job.close()
        shutil.rmtree(commands_dir, ignore_errors=True)
        raise
    process.arkfix_commands_dir = commands_dir
    time.sleep(1)
    # Send a sentinel command and drain everything until we see its output.
    # This reliably discards all login shell startup noise.
    sentinel = b"___NATIVE_SHELL_READY___"
    os.write(process.stdin.fileno(), b"echo ___NATIVE_SHELL_READY___\n")
    process.stdin.flush()
    fd = process.stdout.fileno()
    drain_buf = b""
    import threading

    def drain_stream():
        nonlocal drain_buf
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            drain_buf += chunk
            if sentinel in drain_buf:
                break

    t = threading.Thread(target=drain_stream, daemon=True)
    t.start()
    t.join(timeout=10)
    if t.is_alive() or sentinel not in drain_buf:
        terminate_process_tree(process)
        t.join(timeout=2)
        import shutil

        shutil.rmtree(commands_dir, ignore_errors=True)
        raise RuntimeError("native shell startup timed out before the readiness sentinel")
    logger.debug(f"Native shell startup drained {len(drain_buf)} bytes")
    return process, {"1"}


def get_container(
    ctr_name: str,
    image_name: str,
    persistent: bool = False,
    *,
    command_line_tools_host_path: Path | None = None,
) -> tuple[subprocess.Popen, set]:
    """
    Get a container object for a given container name and image name

    Arguments:
        ctr_name (str): Name of container
        image_name (str): Name of image
        persistent (bool): Whether to use a persistent container or not
        command_line_tools_host_path: Optional resolved host Huawei CLI root for bind-mount
            (see :func:`resolve_command_line_tools_host_path`). When ``None``, uses global
            keys/env only.
    Returns:
        Container object
    """
    if not image_exists(image_name):
        msg = (
            f"Image {image_name} not found. Please ensure it is built and available. "
            "Please double-check that you followed all installation/setup instructions from the "
            "readme."
        )
        raise RuntimeError(msg)

    if persistent:
        return _get_persistent_container(
            ctr_name, image_name, persistent, command_line_tools_host_path=command_line_tools_host_path
        )
    else:
        return _get_non_persistent_container(ctr_name, image_name, command_line_tools_host_path=command_line_tools_host_path)


def image_exists(image_name: str) -> bool:
    """
    Check that the image exists and give some better error messages.

    Arguments:
        image_name: Name of image
    Returns:
        bool: True if image exists
    """
    try:
        client = docker.from_env()
    except docker.errors.DockerException as e:
        docker_not_running = any(
            (
                "connection aborted" in str(e).lower(),
                "connection refused" in str(e).lower(),
                "error while fetching server api version" in str(e).lower(),
            ),
        )
        if docker_not_running:
            msg = (
                "Probably the Docker daemon is not running. Please start the Docker daemon and try again. "
                "You might need to allow the use of the docker socket "
                "(https://github.com/princeton-nlp/SWE-agent/issues/159) or symlink the socket "
                "if it's at a non-standard location "
                "(https://github.com/princeton-nlp/SWE-agent/issues/20#issuecomment-2047506005)."
            )
            raise RuntimeError(msg) from e
        raise
    filterred_images = client.images.list(filters={"reference": image_name})
    if len(filterred_images) == 0:
        return False
    elif len(filterred_images) > 1:
        RuntimeError(f"Multiple images found for {image_name}, that's weird.")
    attrs = filterred_images[0].attrs
    if attrs is not None:
        logger.info(
            f"Found image {image_name} with tags: {attrs['RepoTags']}, created: {attrs['Created']} "
            f"for {attrs['Os']} {attrs['Architecture']}.",
        )
    return True

def remove_image(image_name: str) -> None:
    """Remove an image from the local docker registry"""
    client = docker.from_env()
    filterred_images = client.images.list(filters={"reference": image_name})
    if len(filterred_images) == 0:
        logger.warning(f"Image {image_name} not found, skipping removal.")
        return
    elif len(filterred_images) > 1:
        RuntimeError(f"Multiple images found for {image_name}, that's weird.")
    image = filterred_images[0]
    image.remove()
    logger.info(f"Removed image {image_name}.")
    

def get_commit(api: GhApi, owner: str, repo: str, ref: str | None = None):
    """Get commit object from github api

    Args:
        api (GhApi):
        owner (str): Repo owner, e.g., "princeton-nlp"
        repo (str): Repo, e.g., "SWE-agent"
        ref (str, optional): Branch, tag or commit hash

    Returns:
        _type_: _description_
    """
    if ref:
        return api.repos.get_commit(owner, repo, ref)
    return api.repos.list_commits(owner, repo)[0]


class InvalidGithubURL(ValueError): ...


def parse_gh_issue_url(issue_url: str) -> tuple[str, str, str]:
    """
    Returns:
        owner: Repo owner
        repo: Repo name
        issue number: Issue number as str

    Raises:
        InvalidGithubURL: If the URL is not a valid github issue URL
    """
    match = GITHUB_ISSUE_URL_PATTERN.search(issue_url)
    if not match:
        msg = f"Invalid GitHub issue URL: {issue_url}"
        raise InvalidGithubURL(msg)
    res = match.groups()
    assert len(res) == 3
    return tuple(res)  # type: ignore


def parse_gh_repo_url(repo_url: str) -> tuple[str, str]:
    """
    Returns:
        owner: Repo owner/org
        repo: Repo name

    Raises:
        InvalidGithubURL: If the URL is not a valid github repo URL
    """
    match = GITHUB_REPO_URL_PATTERN.search(repo_url)
    if not match:
        msg = f"Invalid GitHub issue URL: {repo_url}"
        raise InvalidGithubURL(msg)
    res = match.groups()
    assert len(res) == 2
    return tuple(res)  # type: ignore


def get_gh_issue_data(issue_url: str, *, token: str = ""):
    """Returns github issue data in the form of a dictionary.
    See https://docs.github.com/en/rest/issues/issues?apiVersion=2022-11-28#get-an-issue
    for return format
    """
    owner, repo, issue_number = parse_gh_issue_url(issue_url)
    api = GhApi(token=token)
    return api.issues.get(owner, repo, issue_number)


def get_problem_statement_from_github_issue(owner: str, repo: str, issue_number: str, *, token: str | None = "") -> str:
    """Return problem statement from github issue"""
    api = GhApi(token=token)
    issue = api.issues.get(owner, repo, issue_number)
    title = issue.title if issue.title else ""
    body = issue.body if issue.body else ""
    return f"{title}\n{body}\n"


class InstanceBuilder:
    def __init__(self, token: str | None = None, language: str | None = None):
        """This helper class is used to build the data for an instance object,
        retrieving problem statements from github issues or local files and setting
        repo paths from github urls or local paths.
        """
        # Args that will be passed to the Instance constructor
        self.args = {
            "language": language
        }
        self.token = token
        self._instance_id_problem_suffix = ""

    def set_problem_statement_from_gh_issue(self, issue_url: str):
        owner, repo, issue_number = parse_gh_issue_url(issue_url)
        self.args["problem_statement"] = get_problem_statement_from_github_issue(
            owner,
            repo,
            issue_number,
            token=self.token,
        )
        self.args["instance_id"] = f"{owner}__{repo}-i{issue_number}"
        self.args["problem_statement_source"] = "online"

    def set_problem_statement_from_file(self, file_path: str):
        self.set_problem_statement_from_text(Path(file_path).read_text())

    def set_problem_statement_from_text(self, text: str):
        self.args["problem_statement"] = text
        self.args["instance_id"] = hashlib.sha256(self.args["problem_statement"].encode()).hexdigest()[:6]
        self.args["problem_statement_source"] = "local"

    def set_problem_statement(self, data_path: str):
        """Get problem statement for a single instance from a github issue url or a
        path to a markdown or text file.
        """
        if data_path.startswith("text://"):
            return self.set_problem_statement_from_text(data_path.removeprefix("text://"))
        if is_github_issue_url(data_path):
            return self.set_problem_statement_from_gh_issue(data_path)
        if Path(data_path).is_file():
            return self.set_problem_statement_from_file(data_path)
        msg = f"Not sure how to get problem statement from {data_path=}."
        raise ValueError(msg)

    def set_repo_info_from_gh_url(self, url: str, base_commit: str | None = None):
        owner, repo = parse_gh_repo_url(url)
        self.args["repo"] = f"{owner}/{repo}"
        self.args["repo_type"] = "github"
        # Always get commit hash, because base_commit can also be branch or tag
        api = GhApi(token=self.token)
        self.args["base_commit"] = get_commit(api, owner, repo, ref=base_commit).sha
        if base_commit != self.args["base_commit"]:
            logger.info(f"Base commit reference {base_commit} resolved to commit hash {self.args['base_commit']}")
        self.args["version"] = self.args["base_commit"][:7]

    def set_repo_info_from_local_path(self, path: str, base_commit: str | None = None):
        self.args["repo"] = str(Path(path).resolve())
        self.args["repo_type"] = "local"
        if base_commit:
            self.args["base_commit"] = base_commit
        else:
            try:
                repo = Repo(path, search_parent_directories=True)
            except InvalidGitRepositoryError as e:
                msg = f"Could not find git repository at {path=}."
                raise ValueError(msg) from e
            if repo.is_dirty():
                msg = f"Local git repository {path} is dirty. Please commit or stash changes."
                raise ValueError(msg)
            self.args["base_commit"] = repo.head.object.hexsha
        self.args["version"] = self.args["base_commit"][:7]

    def set_repo_info(self, repo: str, base_commit: str | None = None):
        if is_github_repo_url(repo):
            self.set_repo_info_from_gh_url(repo, base_commit=base_commit)
        elif Path(repo).is_dir():
            self.set_repo_info_from_local_path(repo, base_commit=base_commit)
        else:
            msg = f"Could not determine repo path from {repo=}."
            raise ValueError(msg)

    def set_from_dict(self, instance_dict: dict[str, Any]):
        self.args |= instance_dict

    def set_missing_fields(self):
        # TODO: This field is only needed while swe_env is using some questionable logic
        # to determine whether to clone from a mirror or not. This should be removed in the future.
        # Values: 'swe-bench' (loaded from json/jsonl for swe-bench style inference),
        # 'online' (loaded from github issue or similar) or 'local' (loaded from local file)
        if "problem_statement_source" not in self.args:
            self.args["problem_statement_source"] = "swe-bench"
        if "repo_type" not in self.args:
            self.args["repo_type"] = "github"

    def validate(self):
        required_fields = [
            "language",
            "problem_statement",
            "instance_id",
            "repo",
            "repo_type",
            "base_commit",
            "version",
            "problem_statement_source",
        ]
        if not all(x in self.args for x in required_fields):
            missing = set(required_fields) - set(self.args.keys())
            msg = f"Missing required fields: {missing=}"
            raise ValueError(msg)
        if self.args["repo_type"] not in {"github", "local"}:
            msg = f"Invalid repo type: {self.args['repo_type']=}"
            raise ValueError(msg)
        if self.args["repo_type"] == "github" and self.args["repo"].count("/") != 1:
            msg = f"Invalid repo format for {self.args['repo_type']=}: {self.args['repo']=}"
            raise ValueError(msg)

    def build(self) -> dict[str, Any]:
        self.set_missing_fields()
        self.validate()
        return self.args


def get_instances(
    file_path: str,
    cli_args,
    *,
    prebuild: bool = False
) -> dict[str, Record]:
    """
    Getter function for handling json, jsonl files

    Args:
        file_path (str): Path to file

    Returns:
        List of Instances
    """
    fallback_lang = specify_languages(file_path)
    instances = prepare_datas(file_path, cli_args, prebuild)
    

    return {
        k: Record(
            instances[k],
            str(data_registry[k].get("language") or fallback_lang or "") or None,
            data_registry[k],
        )
        for k in instances.keys()
    }
    

def specify_languages(file_path: str | Path):
    """Infer dataset language from jsonl path for gitignore / agent tooling.

    Matches ``{alias}_`` or ``{alias}-`` in the path (e.g. ``typescript_eval``).
    ArkTS benchmarks often use ``arktsfix_`` / ``arkts_pr_``; also accept any
    filename starting with ``arkts`` (e.g. ``arkts_uncategorized.jsonl``).
    """
    p = str(file_path)
    for lang in LANGUAGE_MAP:
        for x in LANGUAGE_MAP[lang]:
            if f"{x}_" in p or f"{x}-" in p:
                return lang
    base = Path(p).name
    if base.startswith("arkts"):
        return "arkts"
    return None

def get_associated_commit_urls(org: str, repo: str, issue_number: str, *, token: str = "") -> list[str]:
    """Return the URLs of commits that would close an issue."""
    api = GhApi(token=token)
    # Strangely the "pull_request" field of api.issues.get is often not set
    # so we have to go through the events to check if there's a commit
    events = api.issues.list_events(org, repo, issue_number)
    commit_urls = []
    for event in events:
        if event.event != "referenced":
            continue
        if not event.commit_id:
            continue
        commit = api.repos.get_commit(org, repo, event.commit_id)
        message = commit.commit.message
        if f"fixes #{issue_number}" in message.lower() or f"closes #{issue_number}" in message.lower():
            commit_urls.append(commit.html_url)
    return commit_urls


def remove_triple_backticks(text: str) -> str:
    return "\n".join(line.removeprefix("```") for line in text.splitlines())


_MARKDOWN_TRAJECTORY_EMOJI_MAPPING = {
    "observation": "",
    "response": "‍",
    "state": "",
    "thought": "",
}


def format_trajectory_markdown(trajectory: list[dict[str, str]]):
    """Format a trajectory as a markdown string for use in gh PR description."""
    prefix = [
        "<details>",
        "<summary>Thought process ('trajectory') of SWE-agent (click to expand)</summary>",
        "",
        "",
    ]
    steps = []
    for i, step in enumerate(trajectory):
        step_strs = []
        for key, value in step.items():
            emoji = _MARKDOWN_TRAJECTORY_EMOJI_MAPPING.get(key, "")
            if emoji:
                emoji += " "
            step_strs.append(f"**{emoji}{key.capitalize()} ({i})**:")
            if key in ["observation", "state", "action"]:
                step_strs.append("```")
                step_strs.append(remove_triple_backticks(value).strip())
                step_strs.append("```")
            else:
                step_strs.append(value.strip())
        steps.append("\n".join(step_strs))
    suffix = [
        "",
        "</details>",
    ]
    return "\n".join(prefix) + "\n\n---\n\n".join(steps) + "\n".join(suffix)


def action_hacking(action: str) -> str:
    '''
    TODO: this is a hack, need some way to fix this in the long term.
    due to some shell command may cause some problems.
    '''
    hacking_endstokens_commands = [
        './gradlew'
    ]
    for cmd in hacking_endstokens_commands:
        if cmd in action:
            action = action.rstrip() + f'; echo {PROCESS_DONE_MARKER_START}$?{PROCESS_DONE_MARKER_END}\n'
            break

    # `pnpm test <file/args>` maps to `vitest <file>` (from package.json "test": "vitest"),
    # which enters watch mode and never exits unless CI=1 is set.
    # With CI=1, vitest switches to run mode: executes once, outputs results, then exits.
    # Note: `pnpm test-*` (hyphenated forms like test-unit, test-dts) are excluded.
    import re as _re
    if _re.search(r'\bpnpm test(?:\s|$)', action) and "pnpm test-" not in action:
        if "CI=1" not in action:
            action = _re.sub(r'\bpnpm test\b', "CI=1 pnpm test", action, count=1)

    hacking_npms_commands = [
        'npm run',
        'yarn run'
    ]
    for cmd in hacking_npms_commands:
        # npm / yarn running is attached to the main process and get hanging, 
        # with which running raw commands will get timeout or container killed.
        if cmd in action:
            action = f"(nohup  {action} & > /dev/null) && sleep 30 && cat /dev/null \n"
            break
    return action
