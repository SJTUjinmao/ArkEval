"""
必须在 ``command_line_tools_test/tools`` 下每个可执行脚本里 **最先** ``import`` 本模块，
以便在导入 ``common`` 或其它逻辑之前，将 ``command_line_tools_test/.env`` 注入 ``os.environ``。

契约（agent / CI）：

1. 每个 CLI 文件顶部保留 ``import _load_env``（模块导入时即执行一次加载）。
2. 每个 ``main()`` **第一行** 调用 `ensure_command_line_tools_env()`：以 **override=True** 再次加载
   ``command_line_tools_test/.env``，**强制覆盖** 进程内已有同名环境变量，保证 **必须以 .env 为准**。
3. 通过 ``subprocess`` 再次启动本目录下其它 ``tools/*.py`` 时，子进程会重复执行上述流程。

单独运行：在 ``command_line_tools_test`` 目录执行 ``python tools/xxx.py``，``sys.path`` 含 ``tools/``，
``import _load_env`` 可解析。
"""
from __future__ import annotations

import os
from pathlib import Path


def _strip_dotenv_value(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def load_command_line_tools_dotenv(*, override: bool = False) -> Path | None:
    """
    Load ``command_line_tools_test/.env`` into the current process environment.

    By default, existing ``os.environ`` keys are **not** overwritten (shell / CI wins).
    Set ``override=True`` to force values from the file.
    """
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.is_file():
        return None
    try:
        text = env_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = _strip_dotenv_value(value)
        if override:
            os.environ[key] = value
        else:
            os.environ.setdefault(key, value)
    _apply_portable_env_defaults()
    _prepend_tool_paths_from_loaded_env()
    return env_path


def _is_truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _apply_portable_env_defaults() -> None:
    """Resolve bundled DevEco/OpenHarmony paths from the unpacked arkeval root."""
    if not _is_truthy_env("PORTABLE_ENV"):
        return

    root = Path(__file__).resolve().parents[3]
    harmony_env = root / "depend" / "harmony_env"
    deveco = harmony_env / "deveco"
    sdk = harmony_env / "openharmony_sdk"

    os.environ["ARKEVAL_ROOT"] = str(root)
    os.environ["DEVECO_PATH"] = str(deveco)
    os.environ["DEVECO_SDK_HOME"] = str(sdk)
    os.environ["OHOS_BASE_SDK_HOME"] = str(sdk)
    os.environ["OHOS_SDK_HOME"] = str(sdk)
    os.environ["HOS_SDK_HOME"] = str(sdk)
    os.environ["JAVA_HOME"] = str(deveco / "jbr")
    os.environ["NODE_HOME"] = str(deveco / "tools" / "node")
    os.environ["HDC_IN_PATH"] = "false"
    os.environ["HDC_PATH"] = str(deveco / "sdk" / "default" / "openharmony" / "toolchains" / "hdc.exe")
    os.environ["EMULATOR_PATH"] = str(deveco / "tools" / "emulator" / "Emulator.exe")
    os.environ["EMULATOR_DEPLOYED_PATH"] = str(harmony_env / "emulator_deployed")
    os.environ.setdefault("EMULATOR_INSTANCE", "Huawei_Phone_4")
    os.environ["EMULATOR_INSTANCE_PATH"] = str(harmony_env / "emulator_deployed" / os.environ["EMULATOR_INSTANCE"])
    os.environ["EMULATOR_IMAGE_ROOT"] = str(harmony_env)
    os.environ["HVIGOR_USER_HOME"] = str(root / "evaluation" / "command_line_tools_test" / ".hvigor")


def _prepend_tool_paths_from_loaded_env() -> None:
    """Prepend hdc / java / node dirs from env so ``shutil.which`` and child processes resolve tools."""
    parts: list[str] = []
    hdc = os.environ.get("HDC_PATH", "").strip()
    if hdc:
        p = Path(hdc)
        if p.is_file():
            parts.append(str(p.parent.resolve()))
    jh = os.environ.get("JAVA_HOME", "").strip()
    if jh:
        jb = Path(jh).expanduser().resolve() / "bin"
        if jb.is_dir():
            parts.append(str(jb))
    nh = os.environ.get("NODE_HOME", "").strip()
    if nh:
        nd = Path(nh).expanduser().resolve()
        if nd.is_dir():
            parts.append(str(nd))
    if not parts:
        return
    path = os.environ.get("PATH", "")
    existing_lower = {e.lower() for e in path.split(os.pathsep) if e}
    merged: list[str] = []
    for part in parts:
        if part and part.lower() not in existing_lower:
            merged.append(part)
            existing_lower.add(part.lower())
    if merged:
        os.environ["PATH"] = os.pathsep.join([*merged, path]).strip(os.pathsep)


def ensure_command_line_tools_env() -> Path | None:
    """
    在 ``main()`` 入口第一行调用：以 **override=True** 加载 ``.env``，覆盖已有同名变量，并刷新 PATH 前置。
    """
    return load_command_line_tools_dotenv(override=True)


load_command_line_tools_dotenv(override=True)
