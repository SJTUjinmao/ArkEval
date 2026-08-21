from __future__ import annotations

import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

_SET_UP_LOGGERS = set()
_STDIO_CONFIGURED = False


def _configure_stdio_utf8() -> None:
    global _STDIO_CONFIGURED
    if _STDIO_CONFIGURED:
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    _STDIO_CONFIGURED = True


def get_logger(name: str, log_dir: Path = None) -> logging.Logger:
    _configure_stdio_utf8()
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        name = f"{log_dir.name}_{name}"
    logger = logging.getLogger(name)
    if name in _SET_UP_LOGGERS:
        # Already set up
        return logger
    console = Console(stderr=True, force_terminal=False, legacy_windows=False)
    handler = RichHandler(show_time=False, show_path=False, console=console)
    handler.setLevel(logging.DEBUG)
    if log_dir is not None:
        file_handler = logging.FileHandler(log_dir / "log", encoding="utf-8", errors="replace")
        file_handler.setLevel(logging.DEBUG)
        logger.addHandler(file_handler)
        handler.setLevel(logging.ERROR)
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logger.propagate = False
    _SET_UP_LOGGERS.add(name)
    return logger


default_logger = get_logger("swe-agent")
