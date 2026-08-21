param(
  [string]$RemoteAlias = "dgx",
  [string]$EndpointSpec = "",
  [int]$StartupWaitSeconds = 2
)

$ErrorActionPreference = "Stop"

function Get-EndpointUrls {
  $urls = @()
  $raw = $env:LOCALIZATION_ENGINE_DGX_EMBEDDING_BASE_URLS
  if (-not [string]::IsNullOrWhiteSpace($raw)) {
    $urls += $raw.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ }
  }
  if ($urls.Count -eq 0 -and -not [string]::IsNullOrWhiteSpace($env:LOCALIZATION_ENGINE_DGX_EMBEDDING_BASE_URL)) {
    $urls += $env:LOCALIZATION_ENGINE_DGX_EMBEDDING_BASE_URL.Trim()
  }
  if ($urls.Count -eq 0) {
    $urls += "http://127.0.0.1:8008"
  }
  return $urls
}

function Get-PortFromUrl {
  param([string]$Url)
  $uri = [Uri]$Url
  if ($uri.Port -gt 0) {
    return $uri.Port
  }
  if ($uri.Scheme -eq "https") {
    return 443
  }
  return 80
}

function Test-LocalPortListening {
  param([int]$Port)
  return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Test-EmbeddingEndpoint {
  param([int]$Port)
  try {
    $body = @{
      texts = @("arkeval tunnel check")
      include_embeddings = $false
      max_length = 16
    } | ConvertTo-Json
    $response = Invoke-RestMethod `
      -Uri "http://127.0.0.1:$Port/embed" `
      -Method Post `
      -ContentType "application/json" `
      -Body $body `
      -TimeoutSec 30
    return ([int]$response.dim -eq 4096)
  } catch {
    return $false
  }
}

function Parse-EndpointSpec {
  param([string]$Spec)
  $items = @()
  if ([string]::IsNullOrWhiteSpace($Spec)) {
    foreach ($url in Get-EndpointUrls) {
      $port = Get-PortFromUrl $url
      $items += [pscustomobject]@{
        LocalPort = $port
        RemoteAlias = $RemoteAlias
        RemotePort = $port
      }
    }
    return $items
  }

  foreach ($part in $Spec.Split(",")) {
    $item = $part.Trim()
    if (-not $item) {
      continue
    }
    if ($item -notmatch "^(\d+)=([^:]+):(\d+)$") {
      throw "Invalid endpoint spec item '$item'. Expected format: localPort=sshAlias:remotePort"
    }
    $items += [pscustomobject]@{
      LocalPort = [int]$Matches[1]
      RemoteAlias = $Matches[2]
      RemotePort = [int]$Matches[3]
    }
  }
  return $items
}

$items = Parse-EndpointSpec $EndpointSpec
if ($items.Count -eq 0) {
  throw "No embedding endpoints configured."
}

foreach ($item in $items) {
  if (Test-EmbeddingEndpoint $item.LocalPort) {
    Write-Host "[OK] embedding localhost:$($item.LocalPort) already ready"
    continue
  }

  if (Test-LocalPortListening $item.LocalPort) {
    Write-Host "[WARN] localhost:$($item.LocalPort) is listening but /embed check failed; not starting duplicate tunnel"
    continue
  }

  Write-Host "Starting tunnel: localhost:$($item.LocalPort) -> $($item.RemoteAlias):127.0.0.1:$($item.RemotePort)"
  Start-Process -WindowStyle Hidden -FilePath "ssh" -ArgumentList @(
    "-N",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-L", "$($item.LocalPort)`:127.0.0.1:$($item.RemotePort)",
    $item.RemoteAlias
  )
}

Start-Sleep -Seconds $StartupWaitSeconds

$failed = @()
foreach ($item in $items) {
  if (Test-EmbeddingEndpoint $item.LocalPort) {
    Write-Host "[OK] embedding localhost:$($item.LocalPort)"
  } else {
    Write-Host "[FAIL] embedding localhost:$($item.LocalPort)"
    $failed += $item
  }
}

if ($failed.Count -gt 0) {
  throw "Some embedding tunnels are not ready: $($failed.LocalPort -join ',')"
}

Write-Host "All configured embedding tunnels are ready."
