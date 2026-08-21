# localization_engine/tools/run.py
from __future__ import annotations

"""阶段五验证与执行：⑬ terminal。在子进程中执行命令，返回退出码与 stdout/stderr。"""

from pathlib import Path
from typing import Any


def terminal(
    *,
    command: str,
    cwd: str | Path | None = None,
    timeout_seconds: float | int | None = 60,
    allowed_roots: list[str] | Path | None = None,
) -> dict[str, Any]:
    """在子进程中执行 shell 命令，捕获 stdout/stderr。

    入参：command 必填；cwd 为工作目录（默认当前目录）；timeout_seconds 超时（默认 60）；
    allowed_roots 若提供则 cwd 必须位于其一之下（首版安全限制）。
    成功返回 {"ok": True, "exit_code": int, "stdout": str, "stderr": str}；
    超时或执行异常返回 {"ok": False, "error": str}。
    """
    import subprocess

    work_dir: Path | None = None
    if cwd is not None:
        work_dir = Path(cwd).resolve()
        if not work_dir.is_dir():
            return {"ok": False, "error": f"cwd is not a directory: {work_dir}"}
    if allowed_roots is not None:
        roots = [Path(r).resolve() for r in (allowed_roots if isinstance(allowed_roots, list) else [allowed_roots])]
        actual = work_dir or Path.cwd()
        if not any(actual == r or str(actual).startswith(str(r) + "/") for r in roots):
            return {"ok": False, "error": f"cwd {actual} not under allowed_roots"}

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(work_dir) if work_dir else None,
            capture_output=True,
            text=True,
            timeout=float(timeout_seconds) if timeout_seconds is not None else None,
        )
        return {
            "ok": True,
            "exit_code": result.returncode,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
        }
    except subprocess.TimeoutExpired as e:
        return {"ok": False, "error": f"Command timed out after {timeout_seconds}s"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
