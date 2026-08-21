from __future__ import annotations

from dataclasses import dataclass

from sweagent.utils.patch_utils import (
    is_agent_self_test_patch_path,
    normalize_repo_relative_path,
    paths_from_diff_git_first_line,
)


CODE_SUFFIXES = (".ets", ".ts")


def _is_code_path(path: str) -> bool:
    return normalize_repo_relative_path(path).lower().endswith(CODE_SUFFIXES)


def _is_generated_build_artifact_path(path: str) -> bool:
    normalized = normalize_repo_relative_path(path)
    parts = normalized.split("/")
    generated_dirs = {
        "oh_modules",
        "node_modules",
        "build",
        ".hvigor",
        ".cxx",
        ".preview",
        "coverage",
        "dist",
        "out",
    }
    return (
        normalized == "BuildProfile.ets"
        or normalized.endswith("/BuildProfile.ets")
        or any(part in generated_dirs or part.startswith("arkagent_legacy_sdk_api") for part in parts)
    )


def _path_matches(left: str, right: str) -> bool:
    left_norm = normalize_repo_relative_path(left)
    right_norm = normalize_repo_relative_path(right)
    if not left_norm or not right_norm:
        return False
    return (
        left_norm == right_norm
        or left_norm.endswith("/" + right_norm)
        or right_norm.endswith("/" + left_norm)
    )


def _matches_any(path: str, candidates: list[str]) -> bool:
    return any(_path_matches(path, candidate) for candidate in candidates)


def _unique_normalized(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for path in paths:
        normalized = normalize_repo_relative_path(path)
        if not normalized or normalized in seen:
            continue
        result.append(normalized)
        seen.add(normalized)
    return result


@dataclass(frozen=True)
class RepairStatus:
    defect_code_files: list[str]
    changed_code_files: list[str]
    self_test_files: list[str]
    modified_defect_code_files: list[str]
    unmodified_defect_code_files: list[str]
    outside_defect_code_files: list[str]

    @property
    def has_outside_defect_code(self) -> bool:
        return bool(self.outside_defect_code_files)

    @property
    def has_unmodified_defect_code(self) -> bool:
        return bool(self.unmodified_defect_code_files)

    @property
    def has_defect_code_files(self) -> bool:
        return bool(self.defect_code_files)


def compute_repair_status(
    defect_files: list[str],
    changed_files: list[str],
    *,
    allow_test_patch: bool = False,
) -> RepairStatus:
    def is_allowed_test_defect(path: str) -> bool:
        return allow_test_patch and _matches_any(path, defect_files)

    defect_code_files = _unique_normalized(
        [
            path
            for path in defect_files
            if _is_code_path(path)
            and (not is_agent_self_test_patch_path(path) or allow_test_patch)
        ]
    )
    changed_code_files = _unique_normalized(
        [
            path
            for path in changed_files
            if _is_code_path(path)
            and (
                not is_agent_self_test_patch_path(path)
                or is_allowed_test_defect(path)
            )
            and not _is_generated_build_artifact_path(path)
        ]
    )
    self_test_files = _unique_normalized(
        [
            path
            for path in changed_files
            if is_agent_self_test_patch_path(path) and not is_allowed_test_defect(path)
        ]
    )

    modified_defect_code_files = [
        defect for defect in defect_code_files if _matches_any(defect, changed_code_files)
    ]
    unmodified_defect_code_files = [
        defect for defect in defect_code_files if not _matches_any(defect, changed_code_files)
    ]
    outside_defect_code_files = [
        changed for changed in changed_code_files if not _matches_any(changed, defect_code_files)
    ]
    return RepairStatus(
        defect_code_files=defect_code_files,
        changed_code_files=changed_code_files,
        self_test_files=self_test_files,
        modified_defect_code_files=modified_defect_code_files,
        unmodified_defect_code_files=unmodified_defect_code_files,
        outside_defect_code_files=outside_defect_code_files,
    )


def changed_files_from_patch(patch_text: str) -> list[str]:
    changed: list[str] = []
    for line in patch_text.splitlines():
        if not line.startswith("diff --git "):
            continue
        parsed = paths_from_diff_git_first_line(line)
        if not parsed:
            continue
        left, right = parsed
        path = right if right != "/dev/null" else left
        changed.append(path)
    return _unique_normalized(changed)


def _format_list(title: str, values: list[str], *, limit: int) -> list[str]:
    if not values:
        return [f"{title}: None"]
    lines = [f"{title}:"]
    for value in values[:limit]:
        lines.append(f"- {value}")
    remaining = len(values) - limit
    if remaining > 0:
        lines.append(f"- ... {remaining} more")
    return lines


def format_repair_status(
    status: RepairStatus,
    *,
    max_files: int = 20,
    build_success_note: bool = False,
) -> str:
    total = len(status.defect_code_files)
    modified = len(status.modified_defect_code_files)
    lines = [
        "REPAIR_STATUS",
        f"modified_defect_code_files: {modified}/{total}",
        f"changed_code_files: {len(status.changed_code_files)}",
    ]
    lines.extend(_format_list("unmodified_defect_code_files", status.unmodified_defect_code_files, limit=max_files))
    lines.extend(_format_list("outside_defect_code_files", status.outside_defect_code_files, limit=max_files))
    if status.self_test_files:
        lines.extend(_format_list("self_test_files_excluded_from_repair_patch", status.self_test_files, limit=max_files))
    if status.unmodified_defect_code_files:
        lines.append("submit_readiness: SCOPE_OK_PARTIAL_DEFECT_COVERAGE")
        lines.append(
            "next_step: inspect or edit unmodified KNOWN DEFECT FILES only when they are issue-related; "
            "do not make coverage-only edits. You may submit after build/test gates pass."
        )
    elif status.outside_defect_code_files:
        lines.append("submit_readiness: SCOPE_OK_OUTSIDE_DEFECT_WILL_BE_DROPPED")
        lines.append(
            "next_step: you may submit after build/test gates pass; outside-defect code diffs "
            "will be dropped from the final model_patch."
        )
    else:
        lines.append("submit_readiness: SCOPE_OK")
    if build_success_note:
        lines.append("build_success_note: build passed, but build success alone is not enough to submit; check scope and run focused self-validation.")
    return "\n".join(lines)
