from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from sweagent.agent.models import ModelArguments, get_model


@dataclass
class BlindCriticResult:
    decision: str
    severity: str
    summary: str
    blocking_findings: list[str]
    repair_guidance: str
    raw_response: str = ""

    @property
    def accepted(self) -> bool:
        return self.decision == "accept"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_blind_critic_config(path: str | Path) -> dict[str, str]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    for key in ("system_template", "user_template"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"Blind critic config missing non-empty {key}: {path}")
    return {"system_template": data["system_template"], "user_template": data["user_template"]}


def _record_base_sha(record: Any) -> str:
    try:
        sha = getattr(record.instance.pr.base, "sha", "")
        if sha:
            return str(sha)
    except Exception:
        pass
    data = getattr(record, "data", {}) or {}
    base = data.get("base", {}) if isinstance(data, dict) else {}
    if isinstance(base, dict):
        return str(base.get("sha", "") or "")
    return ""


def _record_defect_files(record: Any) -> list[str]:
    data = getattr(record, "data", {}) or {}
    raw = data.get("defect_files", []) if isinstance(data, dict) else []
    if not isinstance(raw, list):
        return []
    return [str(path) for path in raw if str(path).strip()]


def _run_git_show(repo_dir: Path, ref_path: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "show", ref_path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(stderr or f"git show failed for {ref_path}")
    return result.stdout


def collect_base_context(
    *,
    record: Any,
    repo_dir: Path | None,
    max_chars_per_file: int = 8000,
    max_total_chars: int = 40000,
) -> str:
    defect_files = _record_defect_files(record)
    if not defect_files:
        return "(no defect_files in record)"
    if repo_dir is None or not repo_dir.is_dir():
        return "(base context unavailable: native repository directory is unavailable)"

    base_sha = _record_base_sha(record)
    if not base_sha:
        return "(base context unavailable: base.sha is unavailable)"

    parts: list[str] = []
    total = 0
    for path in defect_files:
        if total >= max_total_chars:
            parts.append("\n[truncated: total base context limit reached]")
            break
        normalized = path.replace("\\", "/")
        header = f"\n--- {normalized} @ {base_sha[:12]} ---\n"
        try:
            content = _run_git_show(repo_dir, f"{base_sha}:{normalized}")
        except Exception as exc:
            content = f"[file unavailable at base: {exc}]"
        if len(content) > max_chars_per_file:
            content = content[:max_chars_per_file] + "\n[truncated: file context limit reached]\n"
        chunk = header + content
        remaining = max_total_chars - total
        if len(chunk) > remaining:
            chunk = chunk[:remaining] + "\n[truncated: total base context limit reached]\n"
        parts.append(chunk)
        total += len(chunk)
    return "".join(parts).strip() or "(empty base context)"


_TRAJECTORY_SIGNAL_RE = re.compile(
    r"BUILD_STATUS|LOCAL_TEST_STATUS|TEST_RUN_STATUS|EXIT_CODE|repair_status|"
    r"modified_defect_code_files|unmodified_defect_code_files|SUBMIT_REJECTED|"
    r"BUILD FAILED|BUILD SUCCESSFUL|hvigor ERROR|ERROR|Exception",
    re.IGNORECASE,
)


def summarize_trajectory(trajectory: list[dict[str, Any]] | None, *, max_chars: int = 12000) -> str:
    if not trajectory:
        return "(no trajectory feedback)"
    snippets: list[str] = []
    for step in trajectory:
        if not isinstance(step, dict):
            continue
        action = str(step.get("action") or "").strip()
        observation = str(step.get("observation") or "").strip()
        text = "\n".join(part for part in (action, observation) if part)
        if not text:
            continue
        if _TRAJECTORY_SIGNAL_RE.search(text):
            snippets.append(text[-3000:])
    if not snippets:
        for step in trajectory[-3:]:
            if isinstance(step, dict):
                observation = str(step.get("observation") or "").strip()
                if observation:
                    snippets.append(observation[-2000:])
    summary = "\n\n---\n\n".join(snippets)
    if len(summary) > max_chars:
        summary = summary[-max_chars:]
    return summary or "(no relevant trajectory feedback)"


def truncate_middle(text: str, *, max_chars: int, label: str) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    marker = f"\n[truncated {label}: kept head and tail around middle omission]\n"
    keep = max(0, max_chars - len(marker))
    head_len = keep // 2
    tail_len = keep - head_len
    return text[:head_len] + marker + text[-tail_len:]


def _strip_outer_json_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
    return re.sub(r"\s*```$", "", stripped)


def _try_load_json_object(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def _find_json_object_end(text: str, start: int) -> int | None:
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def _iter_json_object_candidates(text: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for match in re.finditer(r"\{", text):
        end = _find_json_object_end(text, match.start())
        if end is None:
            continue
        parsed = _try_load_json_object(text[match.start() : end + 1])
        if parsed is not None:
            candidates.append(parsed)
    return candidates


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = _strip_outer_json_fence(text)
    direct = _try_load_json_object(stripped)
    if direct is not None:
        return direct

    candidates = _iter_json_object_candidates(stripped)
    if not candidates:
        raise ValueError("critic response does not contain a parseable JSON object")
    for candidate in candidates:
        if "decision" in candidate:
            return candidate
    return candidates[0]


def parse_critic_json(raw_response: str) -> BlindCriticResult:
    try:
        data = _extract_json_object(raw_response)
    except Exception as exc:
        return BlindCriticResult(
            decision="revise",
            severity="high",
            summary="Critic response could not be parsed as JSON.",
            blocking_findings=[str(exc)],
            repair_guidance="Rerun repair with extra caution: the critic failed to produce structured feedback.",
            raw_response=raw_response,
        )

    decision = str(data.get("decision", "revise")).strip().lower()
    if decision not in {"accept", "revise"}:
        decision = "revise"
    severity = str(data.get("severity", "medium")).strip().lower()
    if severity not in {"low", "medium", "high"}:
        severity = "medium"
    findings = data.get("blocking_findings", [])
    if isinstance(findings, str):
        findings = [findings]
    if not isinstance(findings, list):
        findings = []

    return BlindCriticResult(
        decision=decision,
        severity=severity,
        summary=str(data.get("summary", "")).strip(),
        blocking_findings=[str(item).strip() for item in findings if str(item).strip()],
        repair_guidance=str(data.get("repair_guidance", "")).strip(),
        raw_response=raw_response,
    )


def build_critic_messages(
    *,
    config_file: str | Path,
    issue: str,
    defect_files: str,
    base_context: str,
    candidate_patch: str,
    trajectory_summary: str,
    max_candidate_patch_chars: int = 60000,
) -> list[dict[str, str]]:
    cfg = load_blind_critic_config(config_file)
    candidate_patch = truncate_middle(
        candidate_patch,
        max_chars=max_candidate_patch_chars,
        label="candidate patch",
    )
    user = cfg["user_template"].format(
        issue=issue,
        defect_files=defect_files,
        base_context=base_context,
        candidate_patch=candidate_patch,
        trajectory_summary=trajectory_summary,
    )
    return [
        {"role": "system", "content": cfg["system_template"]},
        {"role": "user", "content": user},
    ]


def run_blind_critic(
    *,
    config_file: str | Path,
    issue: str,
    defect_files: str,
    candidate_patch: str,
    trajectory: list[dict[str, Any]] | None,
    record: Any,
    repo_dir: Path | None,
    model_args: ModelArguments,
    max_candidate_patch_chars: int = 60000,
) -> BlindCriticResult:
    base_context = collect_base_context(record=record, repo_dir=repo_dir)
    trajectory_summary = summarize_trajectory(trajectory)
    messages = build_critic_messages(
        config_file=config_file,
        issue=issue,
        defect_files=defect_files,
        base_context=base_context,
        candidate_patch=candidate_patch,
        trajectory_summary=trajectory_summary,
        max_candidate_patch_chars=max_candidate_patch_chars,
    )
    model = get_model(model_args, commands=[])
    raw = model.query(messages)
    return parse_critic_json(raw)


def format_feedback_for_rerun(result: BlindCriticResult) -> str:
    findings = "\n".join(f"- {item}" for item in result.blocking_findings) or "- No specific finding provided."
    guidance = result.repair_guidance or "Re-read the issue and defect files, then repair the root behavior."
    return (
        "BLIND CRITIC FEEDBACK FROM PRIOR MODEL PATCH REVIEW\n"
        "This feedback was produced without reading official fix_patch or gold test_patch.\n"
        f"Decision: {result.decision}\n"
        f"Severity: {result.severity}\n"
        f"Summary: {result.summary}\n"
        "Blocking findings:\n"
        f"{findings}\n"
        "Repair guidance:\n"
        f"{guidance}\n\n"
        "For this rerun, address the findings using only ISSUE, base code, KNOWN DEFECT FILES, and build/test feedback. "
        "Do not inspect official fix_patch, gold test_patch, or answer files."
    )
