param(
  [switch]$MilvusOnly,
  [switch]$EmbeddingOnly,
  [switch]$SkipEmbedding
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$CheckScript = Join-Path $Root "check_arkeval_services.ps1"
$MilvusScript = Join-Path $Root "depend\milvus\start_milvus.ps1"
$EmbeddingScript = Join-Path $Root "localization\localization_engine\embedding\start_dgx_embedding.ps1"
$EmbeddingTunnelScript = Join-Path $Root "start_embedding_tunnels.ps1"
$DockerDesktop = "C:\Program Files\Docker\Docker\Docker Desktop.exe"

function Test-DockerReady {
  try {
    docker info *> $null
    return ($LASTEXITCODE -eq 0)
  } catch {
    return $false
  }
}

function Ensure-DockerReady {
  if (Test-DockerReady) {
    Write-Host "Docker daemon already ready."
    return
  }
  if (-not (Test-Path -LiteralPath $DockerDesktop)) {
    throw "Docker daemon is not reachable and Docker Desktop was not found: $DockerDesktop"
  }
  Write-Host "Starting Docker Desktop..."
  Start-Process -WindowStyle Hidden -FilePath $DockerDesktop
  $deadline = (Get-Date).AddMinutes(4)
  while ((Get-Date) -lt $deadline) {
    if (Test-DockerReady) {
      Write-Host "Docker daemon is ready."
      return
    }
    Start-Sleep -Seconds 5
  }
  throw "Docker daemon did not become ready within 4 minutes."
}

function Read-Checks {
  $raw = powershell -NoProfile -ExecutionPolicy Bypass -File $CheckScript -Json
  return $raw | ConvertFrom-Json
}

function Get-Check {
  param($Report, [string]$Name)
  return $Report.checks | Where-Object { $_.name -eq $Name } | Select-Object -First 1
}

$report = Read-Checks

if (-not $EmbeddingOnly) {
  $milvus = Get-Check $report "milvus"
  if ($milvus.ok) {
    Write-Host "Milvus already ready: $($milvus.detail)"
  } else {
    Ensure-DockerReady
    if (-not (Test-Path -LiteralPath $MilvusScript)) {
      throw "Milvus start script not found: $MilvusScript"
    }
    Write-Host "Starting Milvus..."
    powershell -NoProfile -ExecutionPolicy Bypass -File $MilvusScript
  }
}

if (-not $MilvusOnly -and -not $SkipEmbedding) {
  $report = Read-Checks
  $embedding = Get-Check $report "dgx_embedding"
  if ($embedding.ok) {
    Write-Host "DGX embedding already ready: $($embedding.detail)"
  } else {
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALIZATION_ENGINE_DGX_EMBEDDING_BASE_URLS)) {
      if (-not (Test-Path -LiteralPath $EmbeddingTunnelScript)) {
        throw "Embedding tunnel script not found: $EmbeddingTunnelScript"
      }
      Write-Host "Starting configured embedding tunnels..."
      powershell -NoProfile -ExecutionPolicy Bypass -File $EmbeddingTunnelScript
    } else {
      if (-not (Test-Path -LiteralPath $EmbeddingScript)) {
        throw "DGX embedding start script not found: $EmbeddingScript"
      }
      Write-Host "Starting DGX embedding..."
      powershell -NoProfile -ExecutionPolicy Bypass -File $EmbeddingScript
    }
  }
}

Write-Host "Final service check:"
powershell -NoProfile -ExecutionPolicy Bypass -File $CheckScript
