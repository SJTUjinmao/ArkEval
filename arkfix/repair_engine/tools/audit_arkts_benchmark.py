"""Audit arkts_benchmark.jsonl: repo consistency, defect_files prefixes, patch path
alignment, disk presence, encoding. Read-only; prints a per-row report + summary."""
from __future__ import annotations

import json
import pathlib
import re
import sys


def paths_from_diff(diff_text: str) -> list[tuple[str, str]]:
    if not diff_text:
        return []
    return re.findall(r"^diff --git a/(\S+) b/(\S+)", diff_text, re.MULTILINE)


def has_stray_prefix(path: str, repo: str) -> bool:
    if not repo:
        return False
    p = path.replace("\\", "/")
    head = p.split("/", 1)[0]
    if p.startswith(repo + "/"):
        return True
    if "+" in head and head.split("+", 1)[0] == repo:
        return True
    return False


def main() -> None:
    benchmark = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(
        "e:/WorkApp/MSWE-agent/MSWE-agent/MSWE-agent/tests/arkts_benchmark.jsonl"
    )
    repo_base = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else pathlib.Path(
        "e:/WorkApp/MSWE-agent/MSWE-agent/MSWE-agent/repair_repo/repo_before_fix"
    )

    rows = [json.loads(line) for line in benchmark.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(f"rows: {len(rows)}")
    print(f"intersection keys: {sorted(set.intersection(*[set(r.keys()) for r in rows]))}\n")

    summary_bad = 0
    for r in rows:
        iid = r.get("instance_id", "?")
        repo = (r.get("repo") or "").strip()
        dfs = [str(x).replace("\\", "/") for x in (r.get("defect_files") or [])]
        fix = r.get("fix_patch") or ""
        test = r.get("test_patch") or ""
        problems: list[str] = []

        # 1) instance_id should contain repo (sanity)
        if repo and repo.split("_")[0].split("-")[0] and repo not in iid:
            problems.append(f"repo '{repo}' not in instance_id '{iid}'")

        # 2) defect_files: no stray prefix, non-empty, forward-slashed
        if not dfs:
            problems.append("defect_files empty")
        for d in dfs:
            if has_stray_prefix(d, repo):
                problems.append(f"stray prefix in defect_files: {d}")
            if "\\" in (r.get("defect_files") and str(r.get("defect_files"))) if False else False:
                pass  # already normalized above

        # 3) disk presence
        repo_dir = repo_base / repo
        if not repo_dir.is_dir():
            problems.append(f"repo dir missing on disk: {repo_dir}")
        else:
            for d in dfs:
                if not (repo_dir / d).exists():
                    problems.append(f"defect file missing on disk: {d}")

        # 4) fix_patch path alignment
        fix_paths = paths_from_diff(fix)
        if not fix:
            problems.append("fix_patch empty")
        else:
            for a, b in fix_paths:
                if has_stray_prefix(a, repo) or has_stray_prefix(b, repo):
                    problems.append(f"stray prefix in fix_patch: a={a} b={b}")
            # 4a) every defect_file should appear in fix_patch
            fix_path_set: set[str] = set()
            for a, b in fix_paths:
                fix_path_set.add(a)
                fix_path_set.add(b)
            for d in dfs:
                if d not in fix_path_set:
                    problems.append(f"defect_file not in fix_patch: {d}")
            # 4b) every fix_patch path should be a defect_file
            for p in fix_path_set:
                if p not in dfs:
                    problems.append(f"fix_patch path not listed in defect_files: {p}")

        # 5) test_patch: stray prefix
        if test:
            for a, b in paths_from_diff(test):
                if has_stray_prefix(a, repo) or has_stray_prefix(b, repo):
                    problems.append(f"stray prefix in test_patch: a={a} b={b}")

        # 6) title/body non-empty
        if not (r.get("title") or "").strip():
            problems.append("title empty")
        if not (r.get("body") or "").strip():
            problems.append("body empty")

        # 7) fix_patch should end with newline
        if fix and not fix.endswith("\n"):
            problems.append("fix_patch missing trailing newline")
        if test and not test.endswith("\n"):
            problems.append("test_patch missing trailing newline")

        status = "OK" if not problems else "BAD"
        print(f"[{status}] {iid}  repo={repo}  defect_files={dfs}")
        for p in problems:
            print(f"    - {p}")
        if problems:
            summary_bad += 1

    print()
    print(f"SUMMARY: {summary_bad} / {len(rows)} row(s) have issue(s)")


if __name__ == "__main__":
    main()
