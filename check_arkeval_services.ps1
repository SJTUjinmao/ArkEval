param(
  [switch]$Json
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = if ($env:PYTHON_EXE) { $env:PYTHON_EXE } else { "python" }

function Test-TcpPort {
  param([string]$HostName, [int]$Port, [int]$TimeoutMs = 2000)
  try {
    $client = [System.Net.Sockets.TcpClient]::new()
    $async = $client.BeginConnect($HostName, $Port, $null, $null)
    $ok = $async.AsyncWaitHandle.WaitOne($TimeoutMs, $false)
    if ($ok) {
      $client.EndConnect($async)
      $client.Close()
      return $true
    }
    $client.Close()
    return $false
  } catch {
    return $false
  }
}

function Test-CommandAvailable {
  param([string]$Name)
  return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Add-Check {
  param(
    [string]$Name,
    [bool]$Ok,
    [string]$Detail,
    [string]$Fix
  )
  [pscustomobject]@{
    name = $Name
    ok = $Ok
    detail = $Detail
    fix = $Fix
  }
}

function Get-EmbeddingUrls {
  $urls = @()
  if (-not [string]::IsNullOrWhiteSpace($env:LOCALIZATION_ENGINE_DGX_EMBEDDING_BASE_URLS)) {
    $urls += $env:LOCALIZATION_ENGINE_DGX_EMBEDDING_BASE_URLS.Split(",") |
      ForEach-Object { $_.Trim() } |
      Where-Object { $_ }
  }
  if ($urls.Count -eq 0 -and -not [string]::IsNullOrWhiteSpace($env:LOCALIZATION_ENGINE_DGX_EMBEDDING_BASE_URL)) {
    $urls += $env:LOCALIZATION_ENGINE_DGX_EMBEDDING_BASE_URL.Trim()
  }
  if ($urls.Count -eq 0) {
    $urls += "http://127.0.0.1:8008"
  }
  return $urls
}

function Test-EmbeddingUrl {
  param([string]$BaseUrl)
  try {
    $body = @{
      texts = @("arkeval service check")
      include_embeddings = $false
      max_length = 16
    } | ConvertTo-Json
    $response = Invoke-RestMethod `
      -Uri "$($BaseUrl.TrimEnd('/'))/embed" `
      -Method Post `
      -ContentType "application/json" `
      -Body $body `
      -TimeoutSec 20
    if ([int]$response.dim -gt 0) {
      return "ok dim=$($response.dim)"
    }
    return "invalid dim"
  } catch {
    return $_.Exception.Message
  }
}

$checks = @()

$dockerAvailable = Test-CommandAvailable "docker"
$dockerOk = $false
$dockerDetail = "docker command not found"
if ($dockerAvailable) {
  try {
    docker info *> $null
    $dockerOk = ($LASTEXITCODE -eq 0)
    $dockerDetail = if ($dockerOk) { "docker daemon reachable" } else { "docker command exists, daemon not reachable" }
  } catch {
    $dockerDetail = $_.Exception.Message
  }
}
$checks += Add-Check "docker" $dockerOk $dockerDetail "Start Docker Desktop or Docker Engine."

$milvusOk = Test-TcpPort "127.0.0.1" 19530
$milvusDetail = if ($milvusOk) { "127.0.0.1:19530 is reachable" } else { "127.0.0.1:19530 is not reachable" }
$checks += Add-Check "milvus" $milvusOk $milvusDetail "Run .\start_arkeval_services.ps1 -MilvusOnly"

$embeddingUrls = Get-EmbeddingUrls
$embeddingResults = @()
$embeddingOkCount = 0
foreach ($url in $embeddingUrls) {
  $detail = Test-EmbeddingUrl $url
  if ($detail -like "ok *") {
    $embeddingOkCount += 1
  }
  $embeddingResults += "$url $detail"
}
$embeddingOk = ($embeddingOkCount -gt 0)
$embeddingDetail = "$embeddingOkCount/$($embeddingUrls.Count) embedding endpoint(s) ready: $($embeddingResults -join '; ')"
$checks += Add-Check "dgx_embedding" $embeddingOk $embeddingDetail "Run .\start_embedding_tunnels.ps1, or .\start_arkeval_services.ps1 -EmbeddingOnly for default 8008."

$sshOk = Test-CommandAvailable "ssh"
$sshDetail = if ($sshOk) { "ssh command available" } else { "ssh command not found" }
$checks += Add-Check "ssh" $sshOk $sshDetail "Install or enable OpenSSH Client."

$pythonOk = Test-Path -LiteralPath $PythonExe
$checks += Add-Check "localization_python" $pythonOk $PythonExe "Install or repair the huawei conda env, or pass --localization-python-exe."

$modelRegistered = $false
$modelsFile = Join-Path $Root "arkfix\repair_engine\sweagent\agent\models.py"
if (Test-Path -LiteralPath $modelsFile) {
  $modelRegistered = Select-String -LiteralPath $modelsFile -Pattern '"gpt-5\.3-codex-spark"' -Quiet
}
$checks += Add-Check "repair_model_metadata" $modelRegistered "gpt-5.3-codex-spark registration in models.py" "Add model metadata before running repair with this model."

$allOk = -not ($checks | Where-Object { -not $_.ok })

if ($Json) {
  [pscustomobject]@{
    ok = $allOk
    checks = $checks
  } | ConvertTo-Json -Depth 5
} else {
  foreach ($check in $checks) {
    $status = if ($check.ok) { "OK" } else { "FAIL" }
    Write-Host ("[{0}] {1}: {2}" -f $status, $check.name, $check.detail)
    if (-not $check.ok) {
      Write-Host ("      fix: {0}" -f $check.fix)
    }
  }
  if ($allOk) {
    Write-Host "All ArkEval services are ready."
  } else {
    Write-Host "Some ArkEval services are not ready."
  }
}

if ($allOk) { exit 0 } else { exit 1 }
