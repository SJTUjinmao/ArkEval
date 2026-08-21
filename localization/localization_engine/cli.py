from __future__ import annotations

import json
import os

"""CLI entry (Typer).

Commands:
- index, sync, locate, locate-files, locate-chunks, context, fix
"""

from pathlib import Path

import typer

from .context import build_fix_context, context_to_markdown
from .fix_flow import _write_scope_files, run_fix
from .indexer import get_chunk_count, index_repo, locate as locate_chunks
from .locate_flow import get_file_ranking_by_score, get_files_to_modify, get_focus_chunks
from .swe_bench_resolve import (
    compute_locate_metrics,
    ensure_repo_for_instance,
    load_instances,
)


app = typer.Typer(add_completion=False)


def _apply_index_options(
    *,
    chunk_workers: int = 0,
    embedding_batch_size: int = 0,
    embedding_parallel_requests: int = 0,
    milvus_upsert_batch_size: int = 0,
    milvus_upsert_workers: int = 0,
    index_queue_size: int = 0,
    progress_interval_seconds: float = 0.0,
) -> None:
    for env_name, value in (
        ("LOCALIZATION_ENGINE_CHUNK_WORKERS", chunk_workers),
        ("LOCALIZATION_ENGINE_EMBEDDING_BATCH_SIZE", embedding_batch_size),
        ("LOCALIZATION_ENGINE_EMBEDDING_PARALLEL_REQUESTS", embedding_parallel_requests),
        ("LOCALIZATION_ENGINE_MILVUS_UPSERT_BATCH_SIZE", milvus_upsert_batch_size),
        ("LOCALIZATION_ENGINE_MILVUS_UPSERT_WORKERS", milvus_upsert_workers),
        ("LOCALIZATION_ENGINE_INDEX_QUEUE_SIZE", index_queue_size),
        ("LOCALIZATION_ENGINE_PROGRESS_INTERVAL_SECONDS", progress_interval_seconds),
    ):
        if value and value > 0:
            os.environ[env_name] = str(value)


def _ensure_indexed(repo_root: str) -> None:
    """若仓库未建立索引（chunk 数为 0 或 None），则先执行 index_repo。"""
    try:
        total = get_chunk_count(repo_root)
        if total is None or total == 0:
            typer.echo("仓库尚未索引，正在自动建立索引...", err=True)
            index_repo(repo_root, dry_run=False, full=False)
            typer.echo("索引完成，继续执行。", err=True)
    except Exception as e:
        typer.echo(f"索引检查/建立失败: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def index(
    repo_root: str = typer.Argument(..., help="Path to the target git repo"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Skip Milvus: only chunk + embed, write merkle"),
    full: bool = typer.Option(False, "--full", help="Force full re-index (drop collection); default is incremental when merkle exists"),
    chunk_workers: int = typer.Option(0, "--chunk-workers"),
    embedding_batch_size: int = typer.Option(0, "--embedding-batch-size"),
    embedding_parallel_requests: int = typer.Option(0, "--embedding-parallel-requests"),
    milvus_upsert_batch_size: int = typer.Option(0, "--milvus-upsert-batch-size"),
    milvus_upsert_workers: int = typer.Option(0, "--milvus-upsert-workers"),
    index_queue_size: int = typer.Option(0, "--index-queue-size"),
    progress_interval_seconds: float = typer.Option(0.0, "--progress-interval-seconds"),
) -> None:
    """Index or sync repository: incremental by Merkle when possible, or full with --full."""

    _apply_index_options(
        chunk_workers=chunk_workers,
        embedding_batch_size=embedding_batch_size,
        embedding_parallel_requests=embedding_parallel_requests,
        milvus_upsert_batch_size=milvus_upsert_batch_size,
        milvus_upsert_workers=milvus_upsert_workers,
        index_queue_size=index_queue_size,
        progress_interval_seconds=progress_interval_seconds,
    )
    result = index_repo(repo_root, dry_run=dry_run, full=full)
    if result is not None:
        files_count, chunks_count, dim = result
        typer.echo(f"Dry run completed: {files_count} files, {chunks_count} chunks, dim={dim} (Milvus skipped).")
    else:
        typer.echo("Index completed")


@app.command()
def sync(
    repo_root: str = typer.Argument(..., help="Path to the target git repo"),
    chunk_workers: int = typer.Option(0, "--chunk-workers"),
    embedding_batch_size: int = typer.Option(0, "--embedding-batch-size"),
    embedding_parallel_requests: int = typer.Option(0, "--embedding-parallel-requests"),
    milvus_upsert_batch_size: int = typer.Option(0, "--milvus-upsert-batch-size"),
    milvus_upsert_workers: int = typer.Option(0, "--milvus-upsert-workers"),
    index_queue_size: int = typer.Option(0, "--index-queue-size"),
    progress_interval_seconds: float = typer.Option(0.0, "--progress-interval-seconds"),
) -> None:
    """增量同步：根据 Merkle 只更新变更/新增文件。可配合 cron 定时执行（如 */3 * * * * 每 3 分钟）。"""

    _apply_index_options(
        chunk_workers=chunk_workers,
        embedding_batch_size=embedding_batch_size,
        embedding_parallel_requests=embedding_parallel_requests,
        milvus_upsert_batch_size=milvus_upsert_batch_size,
        milvus_upsert_workers=milvus_upsert_workers,
        index_queue_size=index_queue_size,
        progress_interval_seconds=progress_interval_seconds,
    )
    index_repo(repo_root, dry_run=False, full=False)
    typer.echo("Sync completed")


@app.command(name="locate")
def locate(
    repo_root: str = typer.Argument(..., help="Path to the target git repo"),
    query: str = typer.Argument(..., help="Natural language query"),
    top_k: int = typer.Option(5, help="Number of hits"),
) -> None:
    """Locate relevant chunks from Milvus (chunk-level)."""

    _ensure_indexed(repo_root)
    for line in locate_chunks(repo_root, query, top_k=top_k):
        typer.echo(line)


@app.command(name="locate-files-raw")
def locate_files_raw(
    repo_root: str = typer.Argument(..., help="Path to the target git repo"),
    query: str = typer.Argument(..., help="Natural language query"),
    top_k: int = typer.Option(10, "--top-k", help="Max number of files to return"),
    top_k_hits: int | None = typer.Option(None, "--top-k-hits", help="Chunk hits to fetch (default: auto by repo size ratio)"),
) -> None:
    """仅跑定位：语义检索后按文件聚合得分，输出匹配度最高的文件及分数（不跑 LLM 筛选、不修复）。"""

    _ensure_indexed(repo_root)
    ranking = get_file_ranking_by_score(
        repo_root,
        query,
        top_k_files=top_k,
        top_k_hits=top_k_hits,
    )
    typer.echo("匹配度最高的文件 (按 score 降序):")
    for i, (path, score) in enumerate(ranking, 1):
        typer.echo(f"  [{i}] {score:.4f}\t{path}")


@app.command(name="locate-files")
def locate_files(
    repo_root: str = typer.Argument(..., help="Path to the target git repo"),
    query: str = typer.Argument(..., help="Natural language query"),
    top_k: int = typer.Option(10, "--top-k", help="Max number of files to return"),
    top_k_hits: int | None = typer.Option(None, "--top-k-hits", help="Chunk hits to fetch (default: auto by repo size ratio)"),
    no_ask: bool = typer.Option(False, "--no-ask", help="Skip ask_user confirmation"),
    no_llm_filter: bool = typer.Option(False, "--no-llm-filter", help="Skip LLM filter, use full top-k as result"),
    write_scope: bool = typer.Option(False, "--write-scope", "-w", help="Write result to repo/.codephoenix/fix_scope_files.txt for locate-only eval"),
) -> None:
    """定位到待修改的 k 份文件：codebase_search → LLM 筛选需修改文件 → 可选 ask_user → 输出列表。"""

    _ensure_indexed(repo_root)
    files = get_files_to_modify(
        repo_root,
        query,
        top_k_files=top_k,
        top_k_hits=top_k_hits,
        ask=not no_ask,
        use_llm_filter=not no_llm_filter,
    )
    if write_scope and files:
        from pathlib import Path
        from .fix_flow import _write_scope_files
        _write_scope_files(Path(repo_root).resolve(), files)
        typer.echo(f"已写入 {len(files)} 个路径到 .codephoenix/fix_scope_files.txt", err=True)
    for path in files:
        typer.echo(path)


@app.command(name="locate-chunks")
def locate_chunks_cmd(
    repo_root: str = typer.Argument(..., help="Path to the target git repo"),
    query: str = typer.Argument(..., help="Natural language query"),
    files: str | None = typer.Option(None, "--files", help="Comma-separated file paths (if omitted, run locate-files with --no-ask first)"),
    no_ask: bool = typer.Option(False, "--no-ask", help="When --files not set: skip ask when computing file list"),
    top_k_chunks: int = typer.Option(30, "--top-k-chunks", help="Max focus chunks to return"),
    max_per_file: int = typer.Option(5, "--max-per-file", help="Max chunks per file"),
) -> None:
    """阶段三细粒度定位：在问题文件内做语义检索，输出需重点关注的 chunk（file:line_start-line_end score）。"""

    _ensure_indexed(repo_root)
    if files is not None and files.strip():
        file_list = [p.strip() for p in files.split(",") if p.strip()]
    else:
        file_list = get_files_to_modify(
            repo_root,
            query,
            ask=not no_ask,
            use_llm_filter=True,
            use_llm_dep_expansion=True,
        )
    if not file_list:
        typer.echo("No files to focus.", err=True)
        raise typer.Exit(1)
    chunks = get_focus_chunks(
        repo_root,
        query,
        file_list,
        top_k_chunks=top_k_chunks,
        max_chunks_per_file=max_per_file,
    )
    for c in chunks:
        typer.echo(f"{c['file_path']}:{c['line_start']}-{c['line_end']} score={c['score']:.4f}")


@app.command(name="context")
def context_cmd(
    repo_root: str = typer.Argument(..., help="Path to the target git repo"),
    query: str = typer.Argument(..., help="Natural language query"),
    files: str | None = typer.Option(None, "--files", help="Comma-separated file paths (if omitted, run locate-files with --no-ask first)"),
    no_ask: bool = typer.Option(False, "--no-ask", help="When --files not set: skip ask when computing file list"),
    output: str | None = typer.Option(None, "--output", "-o", help="Write context to file (default: stdout)"),
    top_k_chunks: int = typer.Option(30, "--top-k-chunks", help="Max focus chunks for context"),
    padding: int = typer.Option(2, "--padding", help="Line padding around each snippet"),
) -> None:
    """阶段三上下文收集：locate-files → locate-chunks → read_file/glob/grep，输出供 LLM 使用的 Markdown 上下文。"""

    _ensure_indexed(repo_root)
    if files is not None and files.strip():
        file_list = [p.strip() for p in files.split(",") if p.strip()]
    else:
        file_list = get_files_to_modify(
            repo_root,
            query,
            ask=not no_ask,
            use_llm_filter=True,
            use_llm_dep_expansion=True,
        )
    if not file_list:
        typer.echo("No files to focus.", err=True)
        raise typer.Exit(1)
    chunks = get_focus_chunks(
        repo_root,
        query,
        file_list,
        top_k_chunks=top_k_chunks,
        max_chunks_per_file=5,
    )
    ctx = build_fix_context(
        repo_root,
        query,
        file_list,
        chunks,
        read_file_padding=padding,
        include_glob_patterns=["*Test*", "*.spec.*", "*.test.*"] if files else None,
        grep_symbols_from_chunks=True,
    )
    md = context_to_markdown(ctx)
    if output:
        Path(output).write_text(md, encoding="utf-8")
        typer.echo(f"Context written to {output}", err=True)
    else:
        typer.echo(md)


@app.command(name="fix")
def fix_cmd(
    repo_root: str = typer.Argument(..., help="Path to the target git repo"),
    query: str = typer.Argument(..., help="Natural language description of the fix"),
    no_ask: bool = typer.Option(False, "--no-ask", help="Skip ask_user in locate-files"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Only print planned edits, do not write files"),
    confirm: bool = typer.Option(False, "--confirm", help="Before applying edits, ask user to confirm (A=Yes B=No)"),
    run_test: str | None = typer.Option(None, "--run-test", help="After applying, run this command (e.g. npm test) and show result"),
    lint_cmd: str | None = typer.Option(None, "--lint-cmd", help="After applying, run this to check compile/lint (e.g. npx tsc --noEmit); on failure LLM will fix errors up to --lint-fix-rounds"),
    lint_fix_rounds: int = typer.Option(2, "--lint-fix-rounds", help="Max rounds of LLM fix after lint failure (default 2)"),
    top_k_files: int = typer.Option(10, "--top-k-files", help="Max files to consider"),
    top_k_chunks: int = typer.Option(30, "--top-k-chunks", help="Max focus chunks for context"),
) -> None:
    """端到端修复：locate-files → locate-chunks → context → LLM 生成结构化 edit → edit_file/write_file 应用；可选 --lint-cmd 做修改后编译/静态检查并由 LLM 修错。"""

    _ensure_indexed(repo_root)
    try:
        total_chunks = get_chunk_count(repo_root)
        if total_chunks is not None:
            typer.echo(f"仓库索引 chunk 总数: {total_chunks}", err=True)
    except Exception:
        pass
    result = run_fix(
        repo_root,
        query,
        no_ask=no_ask,
        dry_run=dry_run,
        confirm_before_apply=confirm,
        run_test_after=run_test,
        lint_command=lint_cmd,
        lint_fix_rounds=lint_fix_rounds,
        top_k_chunks=top_k_chunks,
        top_k_files=top_k_files,
    )
    if not result.get("ok") and result.get("error"):
        typer.echo(result["error"], err=True)
        raise typer.Exit(1)
    if result.get("skipped_apply"):
        typer.echo("Edits not applied (user chose B).")
        return
    file_list = result.get("file_list", [])
    typer.echo("--- 定位到的文件 ---")
    typer.echo(f"  共 {len(file_list)} 个，焦点 chunk 数: {result.get('chunks_count', 0)}")
    for i, fp in enumerate(file_list, 1):
        typer.echo(f"  [{i}] {fp}")
    modified = [r.get("file_path") for r in result.get("results", []) if r.get("ok") and r.get("file_path")]
    modified = list(dict.fromkeys(modified))
    typer.echo("--- 最后修改的文件 ---")
    typer.echo(f"  共 {len(modified)} 个，Edits: {result.get('edits_count', 0)}")
    for i, fp in enumerate(modified, 1):
        typer.echo(f"  [{i}] {fp}")
    for i, r in enumerate(result.get("results", [])):
        if r.get("ok"):
            typer.echo(f"  [{i+1}] OK {r.get('file_path', '')}")
        else:
            typer.echo(f"  [{i+1}] FAIL {r.get('error', '')}", err=True)
    if dry_run:
        typer.echo("(dry-run: no files written)")
    # Linter 验证结果
    lint_result = result.get("lint_result")
    if lint_result is not None:
        typer.echo("--- Lint / compile check ---")
        if result.get("lint_passed"):
            typer.echo("Lint passed.")
        else:
            typer.echo("Lint failed (see output below).", err=True)
            if lint_result.get("stdout"):
                typer.echo(lint_result["stdout"])
            if lint_result.get("stderr"):
                typer.echo(lint_result["stderr"], err=True)
        for fix_round in result.get("lint_fix_results") or []:
            typer.echo(f"  Fix round {fix_round.get('round', '?')}: {len(fix_round.get('results', []))} edit(s) applied.")
    tr = result.get("terminal_result")
    if tr:
        typer.echo("--- Run test result ---")
        if tr.get("ok"):
            typer.echo(f"Exit code: {tr.get('exit_code', '')}")
            if tr.get("stdout"):
                typer.echo(tr["stdout"])
            if tr.get("stderr"):
                typer.echo(tr["stderr"], err=True)
        else:
            typer.echo(tr.get("error", "Unknown error"), err=True)


def _default_repos_root() -> str:
    """默认仓库根：环境变量或 当前目录/data/repos。"""
    import os
    env = (
        os.environ.get("LOCALIZATION_ENGINE_REPOS_ROOT", "").strip()
        or os.environ.get("CODEPHOENIX_REPOS_ROOT", "").strip()
    )
    if env:
        return env
    return str(Path.cwd() / "data" / "repos")


@app.command(name="swebench-locate")
def swebench_locate_cmd(
    instance_file: str = typer.Option(..., "--instance-file", "-f", help="Path to SWE-bench JSON (e.g. data/swe_bench_three_repos.json)"),
    index: int | None = typer.Option(None, "--index", help="Run only instance at this index (0-based)"),
    instance_id: str | None = typer.Option(None, "--instance-id", help="Run only instance with this instance_id"),
    repos_root: str | None = typer.Option(None, "--repos-root", help="Root for cloned repos (default: LOCALIZATION_ENGINE_REPOS_ROOT or ./data/repos)"),
    top_k_files: int = typer.Option(10, "--top-k-files", help="Max files for get_files_to_modify"),
    out_dir: str | None = typer.Option(None, "--out-dir", help="Write located files per instance to out_dir/instance_id.txt; metrics to out_dir/locate_metrics.json"),
) -> None:
    """SWE-bench 自动化：从 JSON 读实例 → 准备仓库 → 定位文件 → 产出定位列表 + 与 defect_file_abs_paths 算定位准确率（仅 .ts/.ets）。不执行修复。"""

    repos_root = repos_root or _default_repos_root()
    try:
        instances = load_instances(instance_file, index=index, instance_id=instance_id)
    except (FileNotFoundError, ValueError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2)
    out_path = Path(out_dir) if out_dir else None
    if out_path:
        out_path.mkdir(parents=True, exist_ok=True)
    all_metrics = []
    for inst in instances:
        iid = inst.get("instance_id") or "unknown"
        typer.echo(f"Instance: {iid}", err=True)
        try:
            repo_root = ensure_repo_for_instance(inst, repos_root)
        except Exception as e:
            typer.echo(f"  ensure_repo failed: {e}", err=True)
            continue
        # checkout 后工作区已变，必须按当前 commit 重建索引，否则会用到上一实例的旧索引
        typer.echo("  正在按当前 commit 重建索引...", err=True)
        index_repo(str(repo_root), dry_run=False, full=True)
        query = inst.get("problem_statement") or ""
        files = get_files_to_modify(
            repo_root,
            query,
            top_k_files=top_k_files,
            ask=False,
            use_llm_filter=True,
            use_llm_dep_expansion=True,
        )
        _write_scope_files(Path(repo_root), files)
        if out_path:
            (out_path / f"{iid}.txt").write_text("\n".join(files) + ("\n" if files else ""), encoding="utf-8")
        metrics = compute_locate_metrics(
            files,
            inst.get("defect_file_abs_paths") or [],
            inst["repo"],
            Path(repo_root),
        )
        metrics["instance_id"] = iid
        all_metrics.append(metrics)
        typer.echo(f"  pred={metrics['pred_count']} gt={metrics['gt_count']} intersection={metrics['intersection_size']} recall={metrics['recall']:.4f} precision={metrics['precision']:.4f}", err=True)
    if out_path and all_metrics:
        (out_path / "locate_metrics.json").write_text(
            json.dumps(all_metrics, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        typer.echo(f"Wrote {out_path / 'locate_metrics.json'}", err=True)
    for m in all_metrics:
        typer.echo(f"{m['instance_id']}\trecall={m['recall']:.4f}\tprecision={m['precision']:.4f}")


if __name__ == "__main__":
    app()
