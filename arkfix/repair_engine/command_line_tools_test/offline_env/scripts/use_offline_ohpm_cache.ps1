param(
  [Parameter(Mandatory = $true)]
  [string]$OhpmExe,

  [Parameter(Mandatory = $false)]
  [string]$OfflineEnvDir = (Resolve-Path (Join-Path (Get-Location) "offline_env")).Path
)

$ErrorActionPreference = "Stop"

function Set-OhpmConfig {
  param(
    [string]$Ohpm,
    [string]$Key,
    [string]$Value
  )
  & $Ohpm config set $Key $Value | Out-Null
}

$offline = (Resolve-Path $OfflineEnvDir).Path
$storeDir = (Join-Path $offline "ohpm_store")
$cacheDir = (Join-Path $offline "ohpm_cache")

New-Item -ItemType Directory -Force -Path $storeDir | Out-Null
New-Item -ItemType Directory -Force -Path $cacheDir | Out-Null

# Best-effort compatibility across ohpm versions:
# - ohpm 6.x uses `cache`
# - some versions may accept cacheDir/storeDir variants
$storeKeys = @("storeDir", "store-dir", "globalStoreDir", "global-store-dir", "store")
$cacheKeys = @("cache", "cacheDir", "cache-dir", "globalCacheDir", "global-cache-dir")

foreach ($k in $storeKeys) {
  try { Set-OhpmConfig -Ohpm $OhpmExe -Key $k -Value $storeDir } catch {}
}
foreach ($k in $cacheKeys) {
  try { Set-OhpmConfig -Ohpm $OhpmExe -Key $k -Value $cacheDir } catch {}
}

Write-Output "OFFLINE_ENV_DIR=$offline"
Write-Output "OHPM_EXE=$OhpmExe"
Write-Output "OHPM_STORE_DIR=$storeDir"
Write-Output "OHPM_CACHE_DIR=$cacheDir"
Write-Output "NOTE=If ohpm config keys differ on your version, run: `"$OhpmExe config list`" and adjust scripts."

