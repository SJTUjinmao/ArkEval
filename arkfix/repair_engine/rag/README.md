# ArkFix RAG

这个模块用于构建并查询基于 Milvus 的 ArkFix RAG 知识库。

预期知识来源：

- HarmonyOS / ArkUI / ArkTS 官方文档。
- 官方 ArkTS sample 应用。

实现上刻意使用 Milvus，而不是论文中提到的 ChromaDB 后端，因为当前仓库的定位阶段已经使用 Milvus。

建索引示例：

```powershell
python -m rag.index `
  --rag-docs-roots E:\knowledge\harmonyos_docs `
  --rag-samples-roots E:\knowledge\harmonyos_samples `
  --rag-index-name harmony_api9_api12
```

运行修复时启用 RAG：

```powershell
python C:\path\to\ArkEval\arkfix\run_repair.py `
  --rag-mode on `
  --rag-index-name harmony_api9_api12
```

如果需要在 worker 启动前重建索引，可以在修复命令中加入 `--rag-build-index`。

## 命中记录

每个启用 RAG 的修复实例都会在轨迹目录下写出审计文件：

```text
trajectories/<user>/<run_name>/rag_hits/<instance_id>.rag.json
```

文件包含：

- `docs_collection` / `code_collection`：实际搜索的 Milvus collection。
- `sidecar_path`：用于通过 `chunk_hash` 找回源文本的 `chunks.jsonl`。
- `hits`：每个召回 chunk，包括 `source_type`、`source_path`、`line_start`、`line_end`、`chunk_hash`、`score` 和 collection 名。
- `context`：注入初始实例提示词的完整 RAG 上下文。

如果补丁被保存，同一份 RAG 元数据也会复制到补丁的 `.meta.json` 中。

## Embedding 配置

RAG 复用 `localization.localization_engine` 的 embedding 配置。构建索引前需要准备下面任意一种后端。

### 方案 A：DGX Spark Qwen3 Embedding Service

当前仓库默认使用 DGX Spark 自定义服务，说明文档在：

```text
C:\path\to\ArkEval\docs\DGX_QWEN3_EMBEDDING_SERVICE.md
```

当前仓库配置：

```text
C:\path\to\ArkEval\.codephoenix\config.json
```

配置的 endpoint：

```text
POST http://127.0.0.1:8008/embed
```

如果 Windows tunnel 或 DGX service 没有运行，可以这样启动：

```powershell
cd C:\path\to\embedding-service
powershell -ExecutionPolicy Bypass -File .\start_dgx_embedding.ps1
```

然后检查：

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8008/health -Method Get
```

### 方案 B：ModelScope API

没有部署本地 embedding 模型时使用这个方案：

```powershell
$env:LOCALIZATION_ENGINE_EMBEDDING_BACKEND = "modelscope"
$env:MODEL_SCOPE_ACCESS_TOKEN = "<your-token>"
python -m rag.index `
  --rag-docs-roots E:\knowledge\harmonyos_docs `
  --rag-samples-roots E:\knowledge\harmonyos_samples `
  --rag-index-name harmony_api9_api12
```

默认 ModelScope embedding 模型：`Qwen/Qwen3-Embedding-8B`。

### 方案 C：本地 OpenAI-Compatible Embedding Server

本地 vLLM/OpenAI-compatible embedding endpoint 已运行时使用这个方案：

```powershell
$env:LOCALIZATION_ENGINE_EMBEDDING_BACKEND = "local"
$env:LOCALIZATION_ENGINE_LOCAL_EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-8B"
```

默认 endpoint：

```text
http://127.0.0.1:8000/v1/embeddings
```

如需修改本地 endpoint，创建 `C:\path\to\ArkEval\.codephoenix\config.json`：

```json
{
  "embedding_backend": "local",
  "local_embedding": {
    "base_url": "http://127.0.0.1:8000/v1",
    "model_name": "Qwen/Qwen3-Embedding-8B",
    "endpoint_path": "embeddings"
  }
}
```

Milvus 也必须处于运行状态；本仓库提供了 `C:\path\to\ArkEval\depend\milvus\start_milvus.ps1`。
