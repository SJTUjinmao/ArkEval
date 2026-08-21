# localization_engine/tools/edit.py
from __future__ import annotations

"""阶段四修改工具：edit_file（⑩）、write_file（⑪）、apply_diff（⑫）。可被 LLM/Agent 或 fix 流程调用。"""

import subprocess
import tempfile
from pathlib import Path
from typing import Any


def _replace_by_fuzzy_line_block(
    text: str,
    lines: list[str],
    old_norm: str,
    new_norm: str,
) -> str | None:
    """当精确/换行标准化匹配都失败时：按行 strip 后做块匹配，找到后整体替换该行块。返回新文本或 None。"""
    old_lines = old_norm.splitlines()
    if not old_lines:
        return None
    old_stripped = [ln.strip() for ln in old_lines]
    file_stripped = [ln.rstrip("\n\r").strip() for ln in lines]
    n = len(old_stripped)
    for i in range(len(file_stripped) - n + 1):
        if file_stripped[i : i + n] == old_stripped:
            # 替换 lines[i : i+n] 为 new_norm 对应的行（带换行）
            new_lines = new_norm.split("\n")
            if new_lines and new_lines[-1] == "":
                new_lines.pop()
            new_lines_with_nl = [ln + "\n" for ln in new_lines]
            result_lines = lines[:i] + new_lines_with_nl + lines[i + n :]
            out = "".join(result_lines).rstrip()
            if not out.endswith("\n"):
                out += "\n"
            return out
    return None


def edit_file(
    *,
    file_path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    new_content: str | None = None,
    old_string: str | None = None,
    new_string: str | None = None,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """修改现有文件。两种方式二选一：
    - 行范围替换：start_line, end_line, new_content（替换 [start_line, end_line] 为 new_content）
    - 字符串替换：old_string, new_string（首次匹配替换，不支持正则）

    成功返回 {"ok": True, "file_path": str}；失败返回 {"ok": False, "error": str}。
    """
    path = Path(file_path).resolve()
    if repo_root is not None:
        try:
            path.relative_to(Path(repo_root).resolve())
        except ValueError:
            return {"ok": False, "error": f"file_path not under repo_root: {file_path}"}
    if not path.is_file():
        return {"ok": False, "error": f"file not found: {path}"}

    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines(keepends=True)
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"
    except Exception as e:
        return {"ok": False, "error": str(e)}

    if start_line is not None and end_line is not None and new_content is not None:
        if start_line < 1 or end_line < start_line:
            return {"ok": False, "error": "Invalid line range"}
        start_idx = max(0, start_line - 1)
        end_idx = min(end_line, len(lines))
        new_lines = new_content.split("\n")
        if new_lines and not new_content.endswith("\n"):
            new_lines[-1] = new_lines[-1] + "\n"
        else:
            new_lines = [ln + "\n" for ln in new_lines]
        before = lines[:start_idx]
        after = lines[end_idx:]
        result_lines = before + new_lines + after
        new_text = "".join(result_lines).rstrip()
        if not new_text.endswith("\n"):
            new_text = new_text + "\n"
    elif old_string is not None and new_string is not None:
        if old_string in text:
            new_text = text.replace(old_string, new_string, 1)
        else:
            # 精确匹配失败时尝试统一换行符后再匹配
            text_norm = text.replace("\r\n", "\n").replace("\r", "\n")
            old_norm = old_string.replace("\r\n", "\n").replace("\r", "\n")
            new_norm = new_string.replace("\r\n", "\n").replace("\r", "\n")
            if old_norm in text_norm:
                new_text = text_norm.replace(old_norm, new_norm, 1)
            else:
                # 回退：按行 strip 后做块匹配，找到后整体替换该行块（容忍缩进/行尾差异）
                new_text = _replace_by_fuzzy_line_block(
                    text, lines, old_norm, new_norm
                )
                if new_text is None:
                    return {"ok": False, "error": "old_string not found in file"}
    else:
        return {"ok": False, "error": "Provide (start_line, end_line, new_content) or (old_string, new_string)"}

    try:
        path.write_text(new_text, encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "file_path": str(path)}


def write_file(
    *,
    file_path: str,
    content: str,
    repo_root: str | Path | None = None,
    overwrite: bool = True,
) -> dict[str, Any]:
    """创建或覆盖文件。若父目录不存在则创建。content 按原样写入。

    成功返回 {"ok": True, "file_path": str}；失败返回 {"ok": False, "error": str}。
    """
    path = Path(file_path).resolve()
    if repo_root is not None:
        try:
            path.relative_to(Path(repo_root).resolve())
        except ValueError:
            return {"ok": False, "error": f"file_path not under repo_root: {file_path}"}
    if path.exists() and not path.is_file():
        return {"ok": False, "error": f"path exists and is not a file: {path}"}
    if path.exists() and not overwrite:
        return {"ok": False, "error": f"file exists and overwrite=False: {path}"}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "file_path": str(path)}


def apply_diff(
    *,
    repo_root: str | Path,
    diff_text: str | None = None,
    diff_path: str | Path | None = None,
    strip: int = 1,
) -> dict[str, Any]:
    """应用 unified diff。优先调用系统 patch；失败则回退到纯 Python 解析并应用。

    入参：repo_root 必填；diff_text 或 diff_path 二选一。strip 为 patch -p 参数（默认 1）。
    成功返回 {"ok": True, "patched_files": list[str]}；失败返回 {"ok": False, "error": str}。
    """
    root = Path(repo_root).resolve()
    if not root.is_dir():
        return {"ok": False, "error": f"repo_root is not a directory: {root}"}
    if diff_path is not None:
        p = Path(diff_path)
        if not p.is_file():
            return {"ok": False, "error": f"diff_path not found: {p}"}
        diff_content = p.read_text(encoding="utf-8", errors="replace")
    elif diff_text:
        diff_content = diff_text
    else:
        return {"ok": False, "error": "Provide diff_text or diff_path"}

    # 1) 优先系统 patch
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".diff", delete=False, encoding="utf-8"
        ) as f:
            f.write(diff_content)
            tmp = f.name
        try:
            result = subprocess.run(
                ["patch", f"-p{strip}", "--forward", "-i", tmp, "-d", str(root)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                patched = _paths_from_unified_diff(diff_content, strip)
                return {"ok": True, "patched_files": patched}
        finally:
            Path(tmp).unlink(missing_ok=True)
    except FileNotFoundError:
        pass
    except Exception:
        pass

    return _apply_diff_python(diff_content, root, strip)


def _paths_from_unified_diff(diff_content: str, strip: int) -> list[str]:
    out: list[str] = []
    for line in diff_content.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            if path and path not in out:
                out.append(path)
    return out


def _apply_diff_python(diff_content: str, repo_root: Path, strip: int) -> dict[str, Any]:
    import re
    hunks: list[tuple[str, list[tuple[int, int, int, list[str]]]]] = []
    current_file: str | None = None
    current_hunk: list[tuple[int, int, int, list[str]]] | None = None
    for line in diff_content.splitlines(keepends=True):
        if not line.endswith("\n"):
            line = line + "\n"
        if line.startswith("--- "):
            path_a = line[4:].strip().lstrip("a/")
            for _ in range(strip):
                if "/" in path_a:
                    path_a = path_a.split("/", 1)[1]
            current_file = path_a
            current_hunk = None
        elif line.startswith("+++ "):
            path_b = line[4:].strip().lstrip("b/")
            for _ in range(strip):
                if "/" in path_b:
                    path_b = path_b.split("/", 1)[1]
            current_file = path_b
            current_hunk = None
        elif line.startswith("@@ "):
            if current_file and current_hunk is not None:
                hunks.append((current_file, current_hunk))
            m = re.match(r"@@ \-(\d+),?(\d*) \+(\d+),?(\d*) @@", line)
            if m:
                old_start = int(m.group(1))
                old_count = int(m.group(2) or 1)
                new_count_lines = int(m.group(4) or 1)
                current_hunk = [(old_start, old_count, new_count_lines, [])]
            else:
                current_hunk = None
        elif current_hunk is not None and current_file:
            if line.startswith("+"):
                current_hunk[-1][3].append(line[1:])
            elif line.startswith("-"):
                pass
            else:
                if line.startswith(" "):
                    current_hunk[-1][3].append(line[1:])
    if current_file and current_hunk:
        hunks.append((current_file, current_hunk))

    by_file: dict[str, list[tuple[int, int, int, list[str]]]] = {}
    for fp, hunk_list in hunks:
        if fp not in by_file:
            by_file[fp] = []
        by_file[fp].extend(hunk_list)

    patched: list[str] = []
    for rel_path, hunk_list in by_file.items():
        full = repo_root / rel_path
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines(keepends=True)
            if lines and not lines[-1].endswith("\n"):
                lines[-1] = lines[-1] + "\n"
        except Exception as e:
            return {"ok": False, "error": f"Read {rel_path}: {e}"}
        for old_start, old_count, _nc, new_lines in sorted(hunk_list, key=lambda x: -x[0]):
            start_idx = max(0, old_start - 1)
            end_idx = min(start_idx + old_count, len(lines))
            before = lines[:start_idx]
            after = lines[end_idx:]
            replacement = [ln if ln.endswith("\n") else ln + "\n" for ln in new_lines]
            lines = before + replacement + after
        try:
            out_text = "".join(lines).rstrip()
            if not out_text.endswith("\n"):
                out_text += "\n"
            full.write_text(out_text, encoding="utf-8")
            patched.append(rel_path)
        except Exception as e:
            return {"ok": False, "error": f"Write {rel_path}: {e}"}
    return {"ok": True, "patched_files": patched}
