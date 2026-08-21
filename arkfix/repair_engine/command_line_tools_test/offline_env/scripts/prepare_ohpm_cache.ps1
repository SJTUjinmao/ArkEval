param(
  [Parameter(Mandatory = $true)]
  [string]$RepoPath,

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

$repo = (Resolve-Path $RepoPath).Path
$offline = (Resolve-Path $OfflineEnvDir).Path
$storeDir = (Join-Path $offline "ohpm_store")
$cacheDir = (Join-Path $offline "ohpm_cache")

New-Item -ItemType Directory -Force -Path $storeDir | Out-Null
New-Item -ItemType Directory -Force -Path $cacheDir | Out-Null

# Capture current ohpm cache (often already populated under user profile).
$oldCache = (& $OhpmExe config get cache) 2>$null
if ($oldCache) { $oldCache = $oldCache.Trim() }

# Reuse the offline config helper (best-effort) to point ohpm cache/store at offline_env.
& (Join-Path $PSScriptRoot "use_offline_ohpm_cache.ps1") -OhpmExe $OhpmExe -OfflineEnvDir $offline | Out-Null

Push-Location $repo
try {
  & $OhpmExe install
} finally {
  Pop-Location
}

# If the offline cache is still empty, copy from the previous cache directory.
# This happens when the repo already has oh_modules and ohpm does not need to fetch anything.
try {
  $offlineCount = (Get-ChildItem -Recurse -Force $cacheDir | Measure-Object).Count
} catch { $offlineCount = 0 }
if (($offlineCount -eq 0) -and $oldCache -and (Test-Path $oldCache)) {
  New-Item -ItemType Directory -Force -Path $cacheDir | Out-Null
  Copy-Item -Recurse -Force (Join-Path $oldCache "*") $cacheDir
}

Write-Output "PREPARE_STATUS=SUCCESS"
Write-Output "REPO_PATH=$repo"
Write-Output "OFFLINE_ENV_DIR=$offline"
Write-Output "OHPM_STORE_DIR=$storeDir"
Write-Output "OHPM_CACHE_DIR=$cacheDir"

