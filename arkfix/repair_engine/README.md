# ArkEval 修复引擎

这个目录是从 `E:\WorkApp\arkagent` 迁移过来的修复核心。

包含的运行时组件：

- `run.py`：单实例 SWE-agent 修复运行器。
- `scripts/run_arkts_model_patch_batch.py`：批量运行脚本，支持重试和补丁收集。
- `sweagent`：agent、模型、环境和补丁过滤代码。
- `multi_swe_bench`：benchmark 数据集和运行时模型。
- `config/arkts_system_prompt.yaml`：ArkTS 修复提示词，工具路径已改写到当前迁移后的引擎。
- `command_line_tools_test`：本地 DevEco/OpenHarmony 工具链的构建/测试封装和 `.env`。
- `evaluation/run_llm_patch_eval.py`：保存补丁时使用的 apply/preprocess 检查 helper。
- `keys.cfg`：从旧修复环境复制过来的本地模型凭证。

迁移后的批量脚本默认仓库池：

```text
C:\path\to\ArkEval\depend\repair_repo
```

外层入口：

```powershell
python C:\path\to\ArkEval\arkfix\run_repair.py --rows 3
```
