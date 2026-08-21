from __future__ import annotations

"""Human-in-the-loop prompt.

Tool interface:
- name: ask_user
- args:
  - question: str
  - options: list[str] | None
- returns: str (raw user input)
"""

import sys
from typing import Iterable


def _get_input_stream():
    """当 stdin 不是 TTY（如被管道/IDE 重定向）时，尝试从 /dev/tty 读，保证能在终端输入。"""
    if sys.stdin.isatty():
        return sys.stdin
    try:
        return open("/dev/tty", "r", encoding="utf-8", errors="replace")
    except Exception:
        return sys.stdin


def read_line_from_terminal(prompt: str = "") -> str:
    """带提示从终端读一行；若 stdin 非 TTY 则尝试从 /dev/tty 读。供 ask_user 与 locate_flow 等复用。"""
    if prompt:
        sys.stdout.write(prompt)
        sys.stdout.flush()
    stream = _get_input_stream()
    try:
        line = stream.readline()
    finally:
        if stream is not sys.stdin and getattr(stream, "close", None):
            stream.close()
    return (line or "").strip()


def ask_user(*, question: str, options: Iterable[str] | None = None) -> str:
    print("\n" + question.strip() + "\n", flush=True)
    if options:
        for opt in options:
            print(f"  {opt}", flush=True)
    return read_line_from_terminal("请输入选项后回车 (例如 A / B / C): ")
