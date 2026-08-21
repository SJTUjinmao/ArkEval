# DGX Embedding Service

This folder keeps the Windows-side helper scripts for the DGX Qwen3 embedding
service used by the localization/RAG pipeline.

## Start

```powershell
cd C:\path\to\ArkEval\localization\localization_engine\embedding
powershell -ExecutionPolicy Bypass -File .\start_dgx_embedding.ps1
```

The script expects the SSH alias `dgx` to be configured on the Windows host. It
starts/checks the remote DGX service and opens the local tunnel:

```text
http://127.0.0.1:8008
```

## Test

```powershell
powershell -ExecutionPolicy Bypass -File .\test_dgx_embedding.ps1
```

Expected result:

```text
model: Qwen/Qwen3-Embedding-8B
dim: 4096
```

The endpoint is a custom API:

```text
POST http://127.0.0.1:8008/embed
```

It is not OpenAI-compatible `/v1/embeddings`.
