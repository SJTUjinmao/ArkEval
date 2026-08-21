$ErrorActionPreference = "Continue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$StartScript = Join-Path $ScriptDir "start_dgx_embedding.ps1"
$LocalPort = 8008
$IntervalSeconds = 60

function Test-EmbeddingReady {
  try {
    $body = @{
      texts = @("watchdog ping")
      include_embeddings = $false
      max_length = 16
    } | ConvertTo-Json
    $response = Invoke-RestMethod `
      -Uri "http://127.0.0.1:$LocalPort/embed" `
      -Method Post `
      -ContentType "application/json" `
      -Body $body `
      -TimeoutSec 20
    return ([int]$response.dim -eq 4096)
  } catch {
    return $false
  }
}

while ($true) {
  if (-not (Test-EmbeddingReady)) {
    powershell -NoProfile -ExecutionPolicy Bypass -File $StartScript
  }
  Start-Sleep -Seconds $IntervalSeconds
}
