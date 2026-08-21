# ArkEval 定位阶段

这个目录包含迁移后的定位阶段。`localization` 在修复 bug 时负责定位可能需要修改的文件，输出结果供 `arkfix` 消费。

## 组件

- `localization/localization_engine`：从 CodePhoenix 迁移而来的定位引擎。它负责索引仓库、嵌入代码片段、查询 Milvus，并排序最可能需要修复的文件。
- `localization/arkts_chunker`：ArkTS/ETS AST 感知的切块工具，供定位引擎使用。
- `localization/run_localization.py`：ArkEval 入口脚本。它适配 ArkEval JSONL schema，并为修复阶段写出稳定产物。

## 输入

默认数据集：

```text
C:\path\to\ArkEval\dataset\arkeval_dataset.jsonl
```

正式实验输入不能依赖 `defect_files`。定位查询由 `title`、`body`、`resolved_issues` 和 `hints` 构造；仓库由 `repo` 解析；base 提交从 `base.sha` 读取。

默认仓库池：

```text
C:\path\to\ArkEval\depend\repair_repo\run01
```

## base commit 与索引重建

正式定位必须基于每条 row 的 `base.sha`。`localization/run_localization.py` 在每条 row 定位前会：

1. 将对应 repo slot reset/clean 到 row 的 `base.sha`。
2. 验证 `HEAD` 与 `base.sha` 一致且工作区干净。
3. 按当前 commit 同步定位索引。默认使用 `.codephoenix/merkle.json` 的 hash 增量路径；首次缺索引、Milvus collection 缺失或显式 `--force-index` 时才全量重建。
4. 再执行 issue query 的文件定位。

因此定位阶段会修改 `C:\path\to\ArkEval\depend\repair_repo\run01\<repo>` 的工作区状态。不要在该 slot 中保留手工修改；如果 reset 或索引同步失败，本 row 会记录为 localization error，而不是继续使用旧 commit 的索引。

Milvus collection 按 `hostname + repo_root` 隔离。同一仓库在 `run01`、`run02`、`run03` 中使用不同 collection，不同机器即使工作区路径相同也不会共享 collection。同一个真实子仓库若被两个进程同时占用，后启动的 row 会立即失败并要求调度到另一个 repo pool；不同 repo root 仍可并行运行。要增加 row 并发，应增加预置 repo pool/worktree，并通过 `start_embedding_cluster.ps1 -StartLocalizationWorkers -LocalizationRepoPools ... -LocalizationRowGroups ...` 启动任意数量的 worker。

当前三个预置 repo pool 的 embedding-only 全量启动方式：

```powershell
.\start_embedding_cluster.ps1 -StartLocalizationWorkers -EmbeddingOnly `
  -LocalizationRepoPools @("depend\repair_repo\run01", "depend\repair_repo\run02", "depend\repair_repo\run03") `
  -LocalizationRowGroups @("1-167", "168-334", "335-502") `
  -RunPrefix "embedding_arkeval502_v2_isolated"
```

## 流式索引与进度

定位索引现在按流式流水线执行：

```text
scan/hash -> chunk checkpoint -> embedding/cache -> Milvus micro-batch upsert -> flush -> merkle
```

关键产物都写在当前 repo slot 的 `.codephoenix/` 下：

```text
.codephoenix/index_progress.json        实时阶段进度
.codephoenix/index_state.json           上次索引成功或失败状态
.codephoenix/chunks_manifest.jsonl      chunk checkpoint
.codephoenix/chunks_manifest.meta.json  checkpoint 元信息
.codephoenix/merkle.json                完整索引成功后才更新
```

`chunks_manifest.jsonl` 会在 chunking 阶段边切边写。若后续 Milvus 写入失败，重跑同一 commit 且 chunk 配置未变时会复用该 manifest，不必重新 chunk 全仓。`merkle.json` 仍然只在 Milvus 全部写入并 flush 成功后更新，避免把不完整向量库误判为可用索引。

Milvus 写入使用微批次和自适应拆分。默认 `--milvus-upsert-batch-size 512`，遇到 `RESOURCE_EXHAUSTED` 或 `message larger than max` 会自动把当前 batch 拆半重试。DGX/local embedding 后端不依赖 ModelScope LLM；只有显式使用 modelscope backend 且 token 可用时才启用 ModelScope LLM filter。

可调参数：

```text
--chunk-workers
--embedding-batch-size
--embedding-parallel-requests
--milvus-upsert-batch-size
--milvus-upsert-workers
--index-queue-size
--progress-interval-seconds
```

## Milvus

离线 Milvus 资产放在：

```text
C:\path\to\ArkEval\depend\milvus
```

启动 Milvus：

```powershell
powershell -ExecutionPolicy Bypass -File C:\path\to\ArkEval\depend\milvus\start_milvus.ps1
```

脚本会使用相对 `depend/milvus` 的路径，并把运行时 volume 写到 `depend/milvus_runtime`。

## 输出约定

每次运行会写出：

```text
localization/outputs/<stage>/<run_id>/
  manifest.json
  localization_results.jsonl
  arkfix_input.jsonl
  enriched_dataset.jsonl
  rows/
    row_000003/
      localized_files_abs.txt
      localized_files_rel.txt
      query.txt
      result.json
      row_trace.jsonl
      llm_trace.jsonl
      index_snapshot.json
      embedding_candidates.jsonl
      llm_core_files.jsonl
      llm_dep_expansion_files.jsonl
```

`arkfix_input.jsonl` 是修复阶段稳定的机器可读接口。修复代码应读取这个文件，而不是逐行文本文件。

Row 级轨迹固定放在 `rows/row_xxxxxx/` 下：

- `row_trace.jsonl`：记录 base reset、index sync、locate、LLM 阶段开始/结束、耗时和数量。
- `llm_trace.jsonl`：记录每次生成式模型调用的 stage、model、prompt 字符数、候选数量、耗时、response 和解析数量。
- `index_snapshot.json`：记录本 row 索引完成后的 `.codephoenix/index_progress.json` 与 `index_state.json` 摘要。
- `embedding_candidates.jsonl`：embedding/Milvus Top-K 文件候选，每行包含 `rank`、`file_path`、`relative_path`、`score`、`source`。
- `llm_core_files.jsonl`：第一轮生成式模型从 embedding 候选中筛选出的核心修复文件；后续定位结果不再保留未选中的候选。
- `llm_dep_expansion_files.jsonl`：第二轮生成式模型只分析 LLM1 核心文件的依赖，并记录建议新增的修复文件；最终结果为 LLM1 核心文件加 LLM2 新增依赖文件。
- `manifest.json`、`result.json`、`localization_results.jsonl` 和 `arkfix_input.jsonl` 会记录实际生成式模型名；两个 LLM 文件列表的每条记录也包含 `model` 字段，避免不同模型实验混淆。

`.codephoenix/` 只放索引运行态和 checkpoint，不放 row 实验轨迹。

如果要固定 embedding Top-K，复用同一批候选比较多个 LLM/修复模型，可以把后续运行指向第一次定位输出根目录：

```powershell
python .\localization\run_localization.py `
  --dataset .\dataset\arkeval_dataset.jsonl `
  --rows 1 `
  --run-id loc_row1_model_b `
  --reuse-embedding-candidates-root .\localization\outputs\01_embedding_localization\embedding_row1_v1
```

复用模式会 reset 到当前 row 的 `base.sha`，但跳过索引同步和 Milvus 检索，直接读取旧 run 的 `rows/row_xxxxxx/embedding_candidates.jsonl`，再重新执行第一轮 LLM 文件筛选和第二轮依赖扩展。指定的候选文件不存在时会直接失败，不会静默重新 embedding。

`outputs` 按定位阶段固定分为：

```text
01_embedding_localization/
02_llm1_filter/
03_llm2_dependency_expansion/
99_experiment_artifacts/
```

前三个目录只放后续流程会直接消费的正式结果。`99_experiment_artifacts/<stage>/` 保存分批、resume、失败和历史尝试。run 名统一采用 `<stage>_<dataset-scope>_<model-or-version>`。正式 502 条 embedding 输入命名为 `embedding_arkeval502_v1`；归档目录保留原 run-id 以便追溯。

分批运行的结果可以汇总成一个标准 localization run 目录 `localization/outputs/01_embedding_localization/embedding_arkeval502_v1/`：

```powershell
python .\localization\merge_localization_outputs.py `
  --dataset .\dataset\arkeval_dataset.jsonl `
  --run-id embedding_arkeval502_v1
```

正式合并前必须先执行隔离审计：

```powershell
python .\localization\validate_embedding_isolation.py `
  --dataset .\dataset\arkeval_dataset.jsonl `
  --output-root .\localization\outputs\01_embedding_localization\<run-id> `
  --csv-report .\docs\embedding_isolation_validation.csv `
  --md-report .\docs\embedding_isolation_validation.md
```

审计要求所有候选属于当前 `repo_root`，每条恰好包含10个大小写不敏感意义下的不同相对路径，并且 collection identity、`base_sha` 和 reset HEAD 全部一致。审计失败时不能继续 merge 或运行 LLM1/LLM2。

该目录仍包含 `manifest.json`、`localization_results.jsonl`、`arkfix_input.jsonl`、`enriched_dataset.jsonl` 和 `rows/row_xxxxxx/`，与直接运行 rows 1-502 的输出格式相同。后续 LLM1 文件筛选和 LLM2 依赖扩展继续使用现有 `--reuse-embedding-candidates-root` 接口：

```powershell
python .\localization\run_localization.py `
  --dataset .\dataset\arkeval_dataset.jsonl `
  --rows 1-502 `
  --run-id loc_llm_from_embedding_502 `
  --reuse-embedding-candidates-root .\localization\outputs\01_embedding_localization\embedding_arkeval502_v1
```

每条 `arkfix_input.jsonl` 记录形态如下：

```json
{
  "row": 3,
  "instance_id": "org__repo+sha-3",
  "repo": "repo",
  "repo_root": "E:\\WorkApp\\arkeval\\depend\\repair_repo\\run01\\repo",
  "base_sha": "full_base_sha",
  "problem": "localized problem text",
  "localized_file_abs_paths": ["..."],
  "localized_file_rel_paths": ["..."],
  "localization_status": "ok",
  "localization_error": ""
}
```

## 冒烟运行

使用包含 `pymilvus` 的 Python 环境：

```powershell
cd C:\path\to\ArkEval
$env:LOCALIZATION_ENGINE_EMBEDDING_BACKEND = "modelscope"
python .\localization\run_localization.py `
  --rows 3 `
  --raw-scores `
  --no-write-scope `
  --keep-going `
  --run-id loc_example_row3
```

关键产物会写到：

```text
C:\path\to\ArkEval\localization\outputs\03_llm2_dependency_expansion\loc_example_row3\arkfix_input.jsonl
```
