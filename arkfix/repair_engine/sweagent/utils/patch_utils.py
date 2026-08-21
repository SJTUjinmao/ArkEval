"""Utilities for cleaning git unified-diff submissions (e.g. defect benchmark hygiene)."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from sweagent.utils.log import get_logger

logger = get_logger(__name__)

_ARKTS_FORBIDDEN_ADDED_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("any type", re.compile(r"(?::\s*any\b|\bas\s+any\b|<\s*any\s*>|\bany\s*\[\])")),
    ("require(...) itself", re.compile(r"\brequire\s*\(", re.IGNORECASE)),
    ("arguments object", re.compile(r"\barguments\b")),
    ("var declaration", re.compile(r"\bvar\b")),
    ("delete operator", re.compile(r"(?<!\.)\bdelete\s+[a-zA-Z_$\[]")),
    ("for...in loop", re.compile(r"\bfor\s*\([^\)]*\bin\b[^\)]*\)")),
    ("call/apply/bind", re.compile(r"\.(call|apply|bind)\s*\(", re.IGNORECASE)),
    ("TypeScript suppression", re.compile(r"@ts-(ignore|expect-error|nocheck)\b", re.IGNORECASE)),
    ("generator/yield", re.compile(r"(function\s*\*|\byield\b)")),
    ("ECMAScript private field", re.compile(r"#[A-Za-z_]\w*")),
]


def _strip_arkts_comments_and_strings(text: str) -> str:
    result: list[str] = []
    in_block_comment = False
    quote: str | None = None
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            if ch == "\n":
                result.append("\n")
            i += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
                result.append(ch)
            elif ch == "\n":
                result.append("\n")
                if quote != "`":
                    quote = None
            i += 1
            continue
        if ch == "/" and nxt == "/":
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
            result.append(ch)
            i += 1
            continue
        result.append(ch)
        i += 1
    return "".join(result)


def find_arkts_forbidden_added_syntax(patch_text: str) -> str | None:
    """Return the first forbidden construct introduced in an ``.ets`` diff."""
    current_file = ""
    added_by_file: dict[str, list[str]] = {}
    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            current_file = ""
        elif line.startswith("+++ "):
            current_file = line[4:].strip()
            if current_file.startswith("b/"):
                current_file = current_file[2:]
            if current_file == "/dev/null":
                current_file = ""
            elif current_file.lower().endswith(".ets"):
                added_by_file.setdefault(current_file, [])
            else:
                current_file = ""
        elif current_file and line.startswith("+") and not line.startswith("+++"):
            added_by_file[current_file].append(line[1:])
    for path, lines in added_by_file.items():
        code_only = _strip_arkts_comments_and_strings("\n".join(lines))
        for label, pattern in _ARKTS_FORBIDDEN_ADDED_PATTERNS:
            if pattern.search(code_only):
                return f"{path}: {label}"
    return None


def normalize_repo_relative_path(p: str) -> str:
    return str(p).replace("\\", "/").strip().lstrip("./")


def defect_tree_sha256(repo_root: Path, defect_paths: list[str]) -> str:
    """Hash the exact scoped defect-file contents at one validation point."""

    root = repo_root.resolve()
    normalized_paths: set[str] = set()
    for raw_path in defect_paths:
        normalized = str(raw_path or "").replace("\\", "/").strip()
        while normalized.startswith("./"):
            normalized = normalized[2:]
        parts = [part for part in normalized.split("/") if part and part != "."]
        if (
            not parts
            or normalized.startswith("/")
            or re.match(r"^[A-Za-z]:", normalized)
            or any(part == ".." for part in parts)
        ):
            raise ValueError(f"unsafe defect path for tree hash: {raw_path!r}")
        normalized_paths.add("/".join(parts))
    if not normalized_paths:
        raise ValueError("cannot hash an empty defect-file scope")

    digest = hashlib.sha256(b"ARKFIX_DEFECT_TREE_V1\0")
    for relative in sorted(normalized_paths):
        path = (root / Path(*relative.split("/"))).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"defect path escapes repository for tree hash: {relative}") from exc
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        if not path.exists():
            digest.update(b"M")
            continue
        if not path.is_file():
            raise ValueError(f"defect path is not a regular file: {relative}")
        digest.update(b"F")
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def normalize_patch_newlines(patch_text: str) -> str:
    """Normalize patch text to LF between lines and ensure a trailing newline.

    Note: Some upstream files (e.g. OpenHarmony ``.ets`` samples) use **CRLF** on disk.
    :func:`expand_patch_hunk_eols_to_match_worktree` runs *after* this step so ``git apply``
    can match those files without hand-editing every patch.
    """
    t = patch_text.replace("\r\n", "\n").replace("\r", "\n")
    if t and not t.endswith("\n"):
        t += "\n"
    return t


def _file_uses_crlf(path: Path) -> bool:
    """Heuristic: treat file as CRLF if CRLF pairs are a majority of line endings."""
    try:
        data = path.read_bytes()[:65536]
    except OSError:
        return False
    crlf = data.count(b"\r\n")
    if crlf == 0:
        return False
    total_nl = data.count(b"\n")
    lone_lf = total_nl - crlf  # \n not part of \r\n
    return crlf >= lone_lf


def expand_patch_hunk_eols_to_match_worktree(patch_text: str, cwd: Path) -> str:
    """Rewrite unified-diff hunk lines so line endings match on-disk files.

    ``git apply`` compares patch lines to file lines byte-for-byte. If the patch was
    produced with LF-only lines but the repo file uses CRLF (common for Windows-style
    OpenHarmony samples), apply fails with "patch does not apply" even when the logical
    diff is correct. For each ``diff --git`` block, if the target path exists under
    *cwd* and :func:`_file_uses_crlf` is true, hunk body lines (`` `` / ``+`` / ``-`` /
    ``\\``) are emitted with ``\\r\\n`` line terminators.

    **Exception:** a hunk line **immediately followed** by ``\\ No newline at end of file``
    must **not** get a ``\\r`` before the patch line terminator: the corresponding source
    line has no end-of-line bytes (no CR/LF), so ``-foo\\r\\n`` would not match.

    Headers (``diff --git``, ``index``, ``---``/``+++``, ``@@``) stay LF-terminated.
    """
    if not patch_text.strip():
        return patch_text
    cwd = cwd.resolve()
    lines = patch_text.splitlines(keepends=False)
    n = len(lines)
    out: list[str] = []
    current_path: str | None = None
    in_hunk = False
    for i, line_no_nl in enumerate(lines):
        if line_no_nl.startswith("diff --git "):
            parsed = paths_from_diff_git_first_line(line_no_nl)
            current_path = normalize_repo_relative_path(parsed[1]) if parsed else None
            in_hunk = False
            out.append(line_no_nl + "\n")
            continue
        if line_no_nl.startswith("@@"):
            in_hunk = True
            out.append(line_no_nl + "\n")
            continue
        if in_hunk:
            if line_no_nl.startswith("\\"):
                out.append(line_no_nl + "\n")
                continue
            if line_no_nl.startswith((" ", "+", "-")) and not line_no_nl.startswith(
                ("---", "+++")
            ):
                p = cwd / current_path if current_path else None
                next_line = lines[i + 1] if i + 1 < n else ""
                next_is_no_eof_newline = next_line.startswith("\\") and "No newline at end of file" in next_line
                if (
                    p
                    and p.is_file()
                    and _file_uses_crlf(p)
                    and not next_is_no_eof_newline
                ):
                    out.append(line_no_nl + "\r\n")
                else:
                    out.append(line_no_nl + "\n")
                continue
            in_hunk = False
        out.append(line_no_nl + "\n")
    return "".join(out)


def paths_from_diff_git_first_line(line: str) -> tuple[str, str] | None:
    """Parse ``a/`` and ``b/`` paths from the first line of a ``diff --git`` block.

    Handles unquoted paths and quoted paths (spaces). Returns paths **without** ``a/`` / ``b/`` prefixes.
    """
    line = line.strip()
    if not line.startswith("diff --git "):
        return None
    rest = line[len("diff --git ") :].strip()
    if rest.startswith('"'):
        m = re.match(r'^"a/([^"]+)"\s+"b/([^"]+)"', rest)
        if m:
            return m.group(1), m.group(2)
        return None
    if not rest.startswith("a/"):
        return None
    sep = rest.find(" b/")
    if sep == -1:
        return None
    path_a = rest[2:sep]
    path_b = rest[sep + 3 :]
    return path_a, path_b


def filter_submission_to_defect_files(submission: str, defect_paths: list[str]) -> str:
    """Keep only unified-diff hunks whose file path is listed in *defect_paths*.

    Compares normalized ``a/`` and ``b/`` paths from each ``diff --git`` header; keeps the
    block if **either** side matches an allowed path (covers renames). Drops noise such as
    ``.gitignore``, ``.hvigor/``, build logs, etc. when the dataset lists explicit
    ``defect_files``.
    """
    if not submission or not defect_paths:
        return submission
    allowed = {normalize_repo_relative_path(x) for x in defect_paths if str(x).strip()}
    if not allowed:
        return submission

    parts = re.split(r"(?=^diff --git )", submission, flags=re.MULTILINE)
    kept: list[str] = []
    for part in parts:
        if not part.strip():
            continue
        if not part.lstrip().startswith("diff --git "):
            continue
        first_line = part.splitlines()[0] if part.splitlines() else ""
        parsed = paths_from_diff_git_first_line(first_line)
        if not parsed:
            # Fallback: legacy regex (paths without unusual spaces)
            m = re.match(r"diff --git a/(.*?) b/(.*?)\r?\n", part)
            if not m:
                continue
            path_a = normalize_repo_relative_path(m.group(1))
            path_b = normalize_repo_relative_path(m.group(2))
        else:
            path_a = normalize_repo_relative_path(parsed[0])
            path_b = normalize_repo_relative_path(parsed[1])
        
        # Suffix matching to handle split repositories vs monorepo JSONL paths
        def matches_allowed(p: str, allowed_set: set[str]) -> bool:
            if p in allowed_set:
                return True
            for a in allowed_set:
                if p.endswith(a) or a.endswith(p):
                    return True
            return False

        if matches_allowed(path_a, allowed) or matches_allowed(path_b, allowed):
            kept.append(part)
    return "".join(kept)


def is_agent_self_test_patch_path(path: str) -> bool:
    """Return True for agent-written self-validation test files.

    Repair agents may create local Hypium tests to validate their fix before
    submit. Those files are useful during the run, but benchmark model patches
    should contain only the repair code. Gold-standard test patches are handled
    separately by the evaluation pipeline.
    """

    normalized = normalize_repo_relative_path(path).lower()
    scoped = f"/{normalized}"
    return (
        "/src/test/" in scoped
        or "/src/ohostest/" in scoped
        or normalized.endswith(".test.ets")
    )


def filter_submission_remove_self_tests(
    submission: str,
    defect_paths: list[str] | None = None,
    *,
    allow_test_patch: bool = False,
) -> str:
    """Drop test diffs unless policy allows the exact current defect path."""

    if not submission:
        return submission

    allowed = {
        normalize_repo_relative_path(str(path)).lower()
        for path in (defect_paths or [])
        if str(path).strip()
    }

    def matches_allowed(path: str) -> bool:
        normalized = normalize_repo_relative_path(path).lower()
        return any(
            normalized == candidate
            or normalized.endswith(candidate)
            or candidate.endswith(normalized)
            for candidate in allowed
        )

    parts = re.split(r"(?=^diff --git )", submission, flags=re.MULTILINE)
    kept: list[str] = []
    for part in parts:
        if not part.strip():
            continue
        if not part.lstrip().startswith("diff --git "):
            kept.append(part)
            continue
        first_line = part.splitlines()[0] if part.splitlines() else ""
        parsed = paths_from_diff_git_first_line(first_line)
        if not parsed:
            m = re.match(r"diff --git a/(.*?) b/(.*?)\r?\n", part)
            if not m:
                kept.append(part)
                continue
            path_a = m.group(1)
            path_b = m.group(2)
        else:
            path_a, path_b = parsed
        is_self_test = is_agent_self_test_patch_path(path_a) or is_agent_self_test_patch_path(path_b)
        is_allowed_test_defect = allow_test_patch and (
            matches_allowed(path_a) or matches_allowed(path_b)
        )
        if is_self_test and not is_allowed_test_defect:
            continue
        kept.append(part)
    return "".join(kept)


def apply_defect_files_filter_to_info(info: dict[str, Any], raw_defect_files: Any) -> None:
    """When dataset provides ``defect_files``, drop unrelated paths from ``info['submission']``."""
    if not isinstance(raw_defect_files, list) or not raw_defect_files:
        return
    sub = info.get("submission")
    if not sub or not isinstance(sub, str):
        return
    paths = [str(x) for x in raw_defect_files if str(x).strip()]
    filtered = filter_submission_to_defect_files(sub, paths)
    if filtered != sub:
        logger.info(
            "Filtered submission to defect_files only (%d -> %d chars, %d allowed path(s))",
            len(sub),
            len(filtered),
            len(paths),
        )
    if not filtered.strip() and sub.strip():
        logger.warning(
            "After defect_files filter, submission is empty (model changed no allowed files or "
            "paths did not match diff headers).",
        )
    info["submission"] = filtered


def filter_patch_text_to_defect_files(patch_text: str, defect_paths: list[str]) -> str:
    """Same as :func:`filter_submission_to_defect_files` but named for eval pipelines."""
    return filter_submission_to_defect_files(patch_text, defect_paths)


def infer_hvigor_module_root_from_defect_files(defect_paths: list[str]) -> str:
    """OpenHarmony monorepo: hvigor-config is under ``<module>/hvigor/``, not ``<module>/entry/``.

    For single-module repos where the agent's cwd is already the module root (so defect
    paths start with ``entry/...``), return ``.`` instead of an empty string so the
    prompt can show a concrete HVIGOR MODULE ROOT.
    """
    if not defect_paths:
        return ""
    first = defect_paths[0].replace("\\", "/")
    parts = Path(first).parts
    if "entry" in parts:
        idx = parts.index("entry")
        if idx > 0:
            return Path(*parts[:idx]).as_posix()
        # defect path is ``entry/...`` — cwd already sits on the module root.
        return "."
    return ""


def _path_depth(path: Path) -> int:
    return len(path.parts)


def _is_harmony_project_root(path: Path) -> bool:
    """Return True for the app-level Harmony project root, not a nested HAR/HSP module."""

    build_profile = path / "build-profile.json5"
    if not build_profile.is_file():
        return False
    try:
        text = build_profile.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        text = ""
    if re.search(r'["\']modules["\']\s*:', text):
        return True
    return (
        (path / "AppScope").is_dir()
        or (path / "entry").is_dir()
    )


def _build_profile_ancestors(repo_root: Path, defect_path: str) -> list[Path]:
    normalized = normalize_repo_relative_path(defect_path)
    if not normalized:
        return []
    current = (repo_root / normalized).parent.resolve()
    repo_root = repo_root.resolve()
    candidates: list[Path] = []
    while True:
        try:
            current.relative_to(repo_root)
        except ValueError:
            break
        if (current / "build-profile.json5").is_file():
            candidates.append(current)
        if current == repo_root:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    return candidates


def scope_ranked_defect_files_to_harmony_project(
    repo_root: str | Path,
    defect_paths: list[str],
) -> tuple[str, list[str]]:
    """Scope ranked defect files to the unique Harmony project containing rank 1."""

    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise ValueError(f"base worktree does not exist: {root}")

    paths: list[str] = []
    for raw_path in defect_paths:
        text = str(raw_path).replace("\\", "/").strip()
        if not text or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
            continue
        parts = [part for part in text.split("/") if part and part != "."]
        if not parts or ".." in parts:
            continue
        normalized = "/".join(parts)
        if normalized not in paths:
            paths.append(normalized)
    if not paths:
        raise ValueError("no safe ranked defect paths")

    project_roots = [
        path
        for path in _build_profile_ancestors(root, paths[0])
        if _is_harmony_project_root(path)
    ]
    project_roots = list(dict.fromkeys(path.resolve() for path in project_roots))
    if not project_roots:
        legacy_project = infer_hvigor_module_root_from_defect_files(paths)
        legacy_root = (root / legacy_project).resolve() if legacy_project else None
        if legacy_root is not None and legacy_root.is_dir():
            try:
                legacy_root.relative_to(root)
            except ValueError:
                pass
            else:
                project_roots = [legacy_root]
    if len(project_roots) != 1:
        candidates = [path.as_posix() for path in project_roots]
        raise ValueError(
            f"rank-1 defect path must resolve to exactly one Harmony project; "
            f"found {len(project_roots)}: {candidates}"
        )

    project_root = project_roots[0]
    project_path = _repo_relative_or_dot(root, project_root)
    scoped_paths: list[str] = []
    for path in paths:
        try:
            (root / path).resolve().relative_to(project_root)
        except ValueError:
            continue
        scoped_paths.append(path)
    if not scoped_paths or scoped_paths[0] != paths[0]:
        raise ValueError(f"rank-1 defect path is outside Harmony project {project_path}")
    return project_path, scoped_paths


def _repo_relative_or_dot(repo_root: Path, path: Path) -> str:
    rel = path.resolve().relative_to(repo_root.resolve())
    rel_text = rel.as_posix()
    return "." if rel_text == "." else rel_text


def _infer_app_samples_code_root(defect_paths: list[str]) -> str:
    roots: set[str] = set()
    for defect_path in defect_paths:
        parts = normalize_repo_relative_path(defect_path).split("/")
        try:
            code_idx = parts.index("code")
        except ValueError:
            continue
        if len(parts) >= code_idx + 4:
            roots.add("/".join(parts[code_idx : code_idx + 4]))
    if len(roots) == 1:
        return next(iter(roots))
    return ""


def _looks_like_app_samples_repo(repo_root: Path, defect_paths: list[str]) -> bool:
    if "applications_app_samples" in repo_root.parts:
        return True
    return any("applications_app_samples" in normalize_repo_relative_path(path).split("/") for path in defect_paths)


def infer_harmony_project_root_from_filesystem(
    repo_root: str | Path,
    defect_paths: list[str],
) -> str:
    """Infer the app-level Harmony project root from checked-out repo files.

    ``applications_app_samples`` is a monorepo of many Harmony apps. Defect files
    can live below nested HAR/HSP modules such as ``feature/detailPageHsp``; running
    ``ohpm install`` or hvigor from the repository root is wrong and slow/failing.
    Prefer the common app-level ``build-profile.json5`` ancestor over nested module
    roots, falling back to the older string heuristic when filesystem markers are
    unavailable.
    """

    paths = [str(x) for x in defect_paths if str(x).strip()]
    if not paths:
        return ""

    root = Path(repo_root).resolve()
    is_app_samples = _looks_like_app_samples_repo(root, paths)
    ancestor_lists = [_build_profile_ancestors(root, p) for p in paths]
    ancestor_lists = [items for items in ancestor_lists if items]
    if ancestor_lists:
        common = set(ancestor_lists[0])
        for items in ancestor_lists[1:]:
            common &= set(items)
        if common:
            project_roots = [p for p in common if _is_harmony_project_root(p)]
            if project_roots:
                return _repo_relative_or_dot(root, max(project_roots, key=_path_depth))
            return _repo_relative_or_dot(root, min(common, key=_path_depth))

        all_project_roots = [
            p
            for items in ancestor_lists
            for p in items
            if _is_harmony_project_root(p)
        ]
        if all_project_roots:
            first_ranked_roots = [
                path for path in ancestor_lists[0] if _is_harmony_project_root(path)
            ]
            selected = min(
                first_ranked_roots or all_project_roots,
                key=lambda path: (_path_depth(path), path.as_posix()),
            )
            return _repo_relative_or_dot(root, selected)

    if is_app_samples:
        app_samples_root = _infer_app_samples_code_root(paths)
        if app_samples_root:
            return app_samples_root

    return infer_hvigor_module_root_from_defect_files(paths)
