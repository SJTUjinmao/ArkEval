# arkeval 502 条补丁评测

该目录提供 `dataset\arkeval_dataset.jsonl` 中 502 条 OpenHarmony/ArkTS 数据的补丁上传、完整真实评测和结果汇总，不依赖 `arkagent` 工作区。

## 启动

双击根目录的 `start-leaderboards.cmd`，浏览器会打开 `http://127.0.0.1:8765/`。

默认复用当前根目录中的：

- 仓库池：`depend\repair_repo\run01..runNN`；
- DevEco、SDK 和模拟器：`depend\harmony_env`；
- 评测器：`evaluation\run_llm_patch_eval.py`；
- 构建和测试工具：`evaluation\command_line_tools_test\tools`。

网页端固定使用上述本地仓库池和 DevEco，不接受外部路径；评测器也会拒绝 `arkeval` 根目录外的数据、补丁、输出、仓库和 DevEco 路径。缺少 base commit 或 npm、pnpm、OHPM 缓存时直接报错，不会切换在线下载。OHPM、HVIGOR 缓存及前端资源均位于 `arkeval` 内。

主机只需能从 `PATH` 调用 Python 和 Git；不会读取 `arkagent` 中的项目文件。

## 上传测试

可上传 1 至 502 个模型补丁，只评测本次上传对应的行，不要求每次凑齐全部 502 条。文件名末尾必须包含原始数据集行号，例如：

```text
model_patch_1.patch
model_patch_27.patch
model_patch_502.patch
```

选择补丁后点击 `Submit Selected Patches`。保持 `Full regression` 勾选时，会对每个已选 patch 执行真实的补丁应用、构建和完整回归测试。

运行结果写入 `Leaderboards\results\web_runs\<run_id>`，上传补丁归档到 `Leaderboards\model_patch\model_patch_<时间>`。

## 资产校验

```powershell
conda run -n huawei python .\Leaderboards\leaderboards.py verify-lock
```

受保护测试补丁、benchmark 和元数据均应包含 502 条。
