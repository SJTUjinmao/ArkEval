# ArkEval 修复阶段

这个目录负责修复阶段。它消费 `localization` 的输出，准备修复范围受限的数据集，并调用迁移到 `arkfix/repair_engine` 下的修复引擎。

## 输入约定

默认输入是最新的：

```text
C:\path\to\ArkEval\localization\outputs\<run_id>\arkfix_input.jsonl
```

`arkfix_input.jsonl` 由 `localization/run_localization.py` 生成，包含问题文本、`base_sha`、仓库身份、仓库根目录以及定位出的待修复文件范围。

`arkfix/run_repair.py` 会把该文件转换成 `scoped_dataset.jsonl`，其中：

- `defect_files` 由 `localized_file_rel_paths` 填充。
- `fix_patch` 和 `test_patch` 会刻意保持为空。
- `base.sha`、标题/正文、解析后的 issue 文本和 `instance_id` 会保留或重建。

## 输出

每次运行会写出：

```text
arkfix/outputs/repair_<timestamp>/
  row_mapping.json
  manifest.json
  command.txt
  model_patch/

dataset/
  arkts_repair_scoped_<timestamp>.jsonl
```

`row_mapping.json` 用于把临时 scoped 数据集的行号映射回原始 ArkEval 行号。

生成的修复 benchmark 会刻意写入 `dataset/`，并让文件名以 `arkts` 开头，这样迁移后的修复引擎会选择 ArkTS 原生模式。

## 试运行

只生成修复范围受限的数据集和命令，不真正调用修复引擎：

```powershell
cd C:\path\to\ArkEval
python .\arkfix\run_repair.py `
  --dataset .\localization\outputs\loc_example_row3_rdbplus_schema_v1\arkfix_input.jsonl `
  --rows 3 `
  --dry-run `
  --skip-preflight
```

## 完整运行

```powershell
python .\arkfix\run_repair.py `
  --dataset .\localization\outputs\<run_id>\arkfix_input.jsonl `
  --rows 1-5 `
  --workers 5
```

默认参数：

- `--repair-engine-root`：`C:\path\to\ArkEval\arkfix\repair_engine`
- `--repo-pool`：`C:\path\to\ArkEval\depend\repair_repo`
- `--apply-check-repo-root`：`<repo-pool>\run01`

`--arkagent-root` 仍作为 `--repair-engine-root` 的兼容别名保留。
