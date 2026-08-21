$ErrorActionPreference = "Stop"

$LocalPort = 8008
$RemoteAlias = "dgx"
$RemoteHealth = "http://127.0.0.1:8008/health"
$RemoteStart = "/home/student233/start_dgx_embedding_service.sh"

function Test-LocalEmbedding {
  try {
    $body = @{
      texts = @("startup ping")
      include_embeddings = $false
      max_length = 16
    } | ConvertTo-Json
    $result = Invoke-RestMethod `
      -Uri "http://127.0.0.1:$LocalPort/embed" `
      -Method Post `
      -ContentType "application/json" `
      -Body $body `
      -TimeoutSec 60
    if ([int]$result.dim -ne 4096) {
      return $false
    }
    return $true
  } catch {
    return $false
  }
}

Write-Host "Starting/checking DGX embedding service on $RemoteAlias ..."
ssh $RemoteAlias "bash $RemoteStart"

if (-not (Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue)) {
  Write-Host "Starting SSH tunnel: localhost:$LocalPort -> $RemoteAlias:127.0.0.1:$LocalPort"
  Start-Process -WindowStyle Hidden -FilePath "ssh" -ArgumentList @(
    "-N",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-L", "$LocalPort`:127.0.0.1:$LocalPort",
    $RemoteAlias
  )
  Start-Sleep -Seconds 2
} else {
  Write-Host "Local port $LocalPort is already listening; not starting a duplicate tunnel."
}

Write-Host "Waiting for local embedding forward ..."
for ($i = 1; $i -le 60; $i++) {
  if (Test-LocalEmbedding) {
    Write-Host "DGX embedding API is ready."
    Invoke-RestMethod -Uri "http://127.0.0.1:$LocalPort/health" -Method Get | ConvertTo-Json -Depth 5
    Write-Host ""
    Write-Host "BASE URL: http://127.0.0.1:$LocalPort"
    Write-Host "API KEY: leave empty"
    $watchdogScript = Join-Path $PSScriptRoot "start_dgx_embedding_watchdog.ps1"
    $watchdogRunning = Get-CimInstance Win32_Process |
      Where-Object { $_.CommandLine -like "*start_dgx_embedding_watchdog.ps1*" }
    if (-not $watchdogRunning) {
      Write-Host "Starting background watchdog for tunnel/service keepalive."
      Start-Process -WindowStyle Hidden -FilePath "powershell" -ArgumentList @(
        "-ExecutionPolicy", "Bypass",
        "-File", $watchdogScript
      )
    } else {
      Write-Host "Watchdog is already running."
    }
    exit 0
  }
  Start-Sleep -Seconds 5
}

Write-Error "Timed out waiting for http://127.0.0.1:$LocalPort/health. Check DGX log: ssh dgx 'tail -f /home/student233/embedding_server.log'"
