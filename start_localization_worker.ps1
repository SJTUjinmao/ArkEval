param(
  [Parameter(Mandatory = $true)][string]$Rows,
  [Parameter(Mandatory = $true)][string]$RepoPool,
  [Parameter(Mandatory = $true)][string]$RunIdPrefix,
  [Parameter(Mandatory = $true)][string]$EmbeddingUrls,
  [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$env:LOCALIZATION_ENGINE_DGX_EMBEDDING_BASE_URLS = $EmbeddingUrls
$env:LOCALIZATION_ENGINE_EMBEDDING_BACKEND = "dgx"
$env:LOCALIZATION_ENGINE_DGX_EMBEDDING_TIMEOUT_SECONDS = "300"
$env:LOCALIZATION_ENGINE_DGX_EMBEDDING_MAX_RETRIES = "5"

$ts = $RunIdPrefix + "_" + (Get-Date -Format "yyyyMMdd_HHmmss")

powershell -NoProfile -ExecutionPolicy Bypass -File .\start_arkeval_services.ps1 -SkipEmbedding

& $PythonExe .\localization\run_localization.py `
  --dataset .\dataset\arkeval_dataset.jsonl `
  --rows $Rows `
  --repo-pool $RepoPool `
  --run-id $ts `
  --top-k-files 10 `
  --embedding-batch-size 32 `
  --embedding-parallel-requests 1 `
  --chunk-workers 16 `
  --milvus-upsert-batch-size 512 `
  --milvus-upsert-workers 2 `
  --no-llm-filter `
  --no-dep-expansion `
  --keep-going

Write-Host "OUTPUT=.\localization\outputs\$ts"
