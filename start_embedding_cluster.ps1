param(
    [switch]$PersistEnv,
    [switch]$StartThreeWayLocalization,
    [switch]$StartLocalizationWorkers,
    [switch]$EmbeddingOnly,
    [switch]$SkipVerify,
    [string]$PythonExe = "",
    [string]$Dataset = "dataset\arkeval_dataset.jsonl",
    [string]$Rows1 = "1-167",
    [string]$Rows2 = "168-334",
    [string]$Rows3 = "335-502",
    [string[]]$LocalizationRepoPools = @(),
    [string[]]$LocalizationRowGroups = @(),
    [string]$RunPrefix = "loc_parallel",
    [string]$LocalizationRunStamp = "",
    [int]$EmbeddingBatchSize = 32,
    [int]$EmbeddingParallelRequests = 13,
    [int]$EmbeddingMaxLength = 1024,
    [int]$EmbeddingTimeoutSeconds = 30,
    [int]$EmbeddingMaxRetries = 1,
    [double]$EmbeddingPoolOutageGraceSeconds = 600,
    [double]$EmbeddingPoolRetryIntervalSeconds = 5,
    [switch]$TunnelSupervisor
)

$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

if (-not $PythonExe) {
    $candidate = Join-Path $env:USERPROFILE "miniconda3\envs\huawei\python.exe"
    if (Test-Path $candidate) {
        $PythonExe = $candidate
    } else {
        $PythonExe = "python"
    }
}

$tunnels = @(
    @{ Name = "v100-gpu0"; Host = "v100"; LocalPort = 8108; RemotePort = 8008 },
    @{ Name = "v100-gpu1"; Host = "v100"; LocalPort = 8109; RemotePort = 8009 },
    @{ Name = "v100-gpu2"; Host = "v100"; LocalPort = 8110; RemotePort = 8010 },
    @{ Name = "v100-gpu3"; Host = "v100"; LocalPort = 8111; RemotePort = 8011 },
    @{ Name = "v100-gpu4"; Host = "v100"; LocalPort = 8112; RemotePort = 8012 },
    @{ Name = "v100-gpu5"; Host = "v100"; LocalPort = 8113; RemotePort = 8013 },
    @{ Name = "v100-gpu6"; Host = "v100"; LocalPort = 8114; RemotePort = 8014 },
    @{ Name = "v100-gpu7"; Host = "v100"; LocalPort = 8115; RemotePort = 8015 },
    @{ Name = "v100-gpu8"; Host = "v100"; LocalPort = 8116; RemotePort = 8016 },
    @{ Name = "v100-gpu9"; Host = "v100"; LocalPort = 8117; RemotePort = 8017 },
    @{ Name = "dgx-1152"; Host = "dgx"; LocalPort = 8208; RemotePort = 8008 },
    @{ Name = "dgx-2271"; Host = "dgx-spark-2271"; LocalPort = 8209; RemotePort = 8008 },
    @{ Name = "dgx-0182"; Host = "dgx-spark-0182"; LocalPort = 8210; RemotePort = 8008 }
)

function Test-PortListening {
    param([int]$Port)
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    return $null -ne $conn
}

function Start-Tunnel {
    param($Tunnel)
    if (Test-PortListening -Port $Tunnel.LocalPort) {
        Write-Host "[tunnel] port $($Tunnel.LocalPort) already listening; skip $($Tunnel.Name)"
        return $null
    }
    $forward = "$($Tunnel.LocalPort):127.0.0.1:$($Tunnel.RemotePort)"
    Write-Host "[tunnel] start $($Tunnel.Name): 127.0.0.1:$($Tunnel.LocalPort) -> $($Tunnel.Host):$($Tunnel.RemotePort)"
    return Start-Process -WindowStyle Hidden -FilePath "ssh" -ArgumentList @(
        "-N",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ConnectionAttempts=1",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        "-L", $forward,
        $Tunnel.Host
    ) -PassThru
}

function Set-EmbeddingEnvironment {
    $urls = ($tunnels | ForEach-Object { "http://127.0.0.1:$($_.LocalPort)" }) -join ","
    $env:LOCALIZATION_ENGINE_DGX_EMBEDDING_BASE_URLS = $urls
    $env:LOCALIZATION_ENGINE_DGX_EMBEDDING_MAX_LENGTH = [string]$EmbeddingMaxLength
    $env:LOCALIZATION_ENGINE_EMBEDDING_BATCH_SIZE = [string]$EmbeddingBatchSize
    $env:LOCALIZATION_ENGINE_EMBEDDING_PARALLEL_REQUESTS = [string]$EmbeddingParallelRequests
    $env:LOCALIZATION_ENGINE_EMBEDDING_BACKEND = "dgx"
    $env:LOCALIZATION_ENGINE_DGX_EMBEDDING_TIMEOUT_SECONDS = [string]$EmbeddingTimeoutSeconds
    $env:LOCALIZATION_ENGINE_DGX_EMBEDDING_MAX_RETRIES = [string]$EmbeddingMaxRetries
    $env:LOCALIZATION_ENGINE_DGX_POOL_OUTAGE_GRACE_SECONDS = [string]$EmbeddingPoolOutageGraceSeconds
    $env:LOCALIZATION_ENGINE_DGX_POOL_RETRY_INTERVAL_SECONDS = [string]$EmbeddingPoolRetryIntervalSeconds

    if ($PersistEnv) {
        [Environment]::SetEnvironmentVariable("LOCALIZATION_ENGINE_DGX_EMBEDDING_BASE_URLS", $urls, "User")
        [Environment]::SetEnvironmentVariable("LOCALIZATION_ENGINE_DGX_EMBEDDING_MAX_LENGTH", [string]$EmbeddingMaxLength, "User")
        [Environment]::SetEnvironmentVariable("LOCALIZATION_ENGINE_EMBEDDING_BATCH_SIZE", [string]$EmbeddingBatchSize, "User")
        [Environment]::SetEnvironmentVariable("LOCALIZATION_ENGINE_EMBEDDING_PARALLEL_REQUESTS", [string]$EmbeddingParallelRequests, "User")
        [Environment]::SetEnvironmentVariable("LOCALIZATION_ENGINE_EMBEDDING_BACKEND", "dgx", "User")
        [Environment]::SetEnvironmentVariable("LOCALIZATION_ENGINE_DGX_EMBEDDING_TIMEOUT_SECONDS", [string]$EmbeddingTimeoutSeconds, "User")
        [Environment]::SetEnvironmentVariable("LOCALIZATION_ENGINE_DGX_EMBEDDING_MAX_RETRIES", [string]$EmbeddingMaxRetries, "User")
        [Environment]::SetEnvironmentVariable("LOCALIZATION_ENGINE_DGX_POOL_OUTAGE_GRACE_SECONDS", [string]$EmbeddingPoolOutageGraceSeconds, "User")
        [Environment]::SetEnvironmentVariable("LOCALIZATION_ENGINE_DGX_POOL_RETRY_INTERVAL_SECONDS", [string]$EmbeddingPoolRetryIntervalSeconds, "User")
        Write-Host "[env] persisted user environment variables"
    }

    Write-Host "[env] LOCALIZATION_ENGINE_DGX_EMBEDDING_BASE_URLS=$urls"
    Write-Host "[env] max_length=$EmbeddingMaxLength batch=$EmbeddingBatchSize parallel=$EmbeddingParallelRequests timeout=$EmbeddingTimeoutSeconds retries=$EmbeddingMaxRetries outage_grace=$EmbeddingPoolOutageGraceSeconds retry_interval=$EmbeddingPoolRetryIntervalSeconds"
}

function Test-Endpoint {
    param($Tunnel)
    $base = "http://127.0.0.1:$($Tunnel.LocalPort)"
    $health = Invoke-RestMethod -Uri "$base/health" -TimeoutSec 30
    if (-not ($health.ok -or $health.status -eq "ok")) {
        throw "health status not ok: $($Tunnel.Name)"
    }
    if ("$($health.model)" -notmatch "Qwen3-Embedding-8B") {
        throw "unexpected model on $($Tunnel.Name): $($health.model)"
    }
    if ([int]$health.dim -ne 4096) {
        throw "unexpected dim on $($Tunnel.Name): $($health.dim)"
    }
    if ([int]$health.default_max_length -ne 256 -or [int]$health.max_allowed_length -ne 1024) {
        throw "unexpected max_length contract on $($Tunnel.Name)"
    }

    $body = @{
        texts = @("embedding endpoint startup check")
        include_embeddings = $false
        max_length = 1024
    } | ConvertTo-Json -Depth 4
    $embed = Invoke-RestMethod -Method Post -Uri "$base/embed" -Body $body -ContentType "application/json" -TimeoutSec 120
    if ([int]$embed.dim -ne 4096 -or [int]$embed.max_length -ne 1024 -or -not $embed.preview) {
        throw "embed contract failed on $($Tunnel.Name)"
    }
    Write-Host "[verify] ok $($Tunnel.Name) $base dtype=$($health.dtype) pid=$($health.pid)"
}

function Start-LocalizationWindow {
    param(
        [string]$Rows,
        [string]$RepoPool,
        [string]$RunId,
        [string]$EmbeddingUrls,
        [int]$WorkerParallelRequests
    )
    if (-not (Test-Path $RepoPool)) {
        throw "repo pool not found: $RepoPool"
    }
    if (-not (Test-Path $Dataset)) {
        throw "dataset not found: $Dataset"
    }
    $workerArgs = @(
        "localization\run_localization.py",
        "--dataset", $Dataset,
        "--rows", $Rows,
        "--repo-pool", $RepoPool,
        "--run-id", $RunId,
        "--embedding-batch-size", "$EmbeddingBatchSize",
        "--embedding-parallel-requests", "$WorkerParallelRequests",
        "--no-write-scope"
    )
    if ($EmbeddingOnly) {
        $workerArgs += @("--no-llm-filter", "--no-dep-expansion")
    }
    $logRoot = Join-Path $PSScriptRoot "localization\outputs\99_experiment_artifacts"
    if (-not (Test-Path -LiteralPath $logRoot)) {
        throw "worker log directory not found: $logRoot"
    }
    $stdoutLog = Join-Path $logRoot "$RunId.stdout.log"
    $stderrLog = Join-Path $logRoot "$RunId.stderr.log"
    $previousUrls = $env:LOCALIZATION_ENGINE_DGX_EMBEDDING_BASE_URLS
    $previousParallel = $env:LOCALIZATION_ENGINE_EMBEDDING_PARALLEL_REQUESTS
    try {
        $env:LOCALIZATION_ENGINE_DGX_EMBEDDING_BASE_URLS = $EmbeddingUrls
        $env:LOCALIZATION_ENGINE_EMBEDDING_PARALLEL_REQUESTS = "$WorkerParallelRequests"
        $process = Start-Process `
            -FilePath $PythonExe `
            -ArgumentList $workerArgs `
            -WorkingDirectory $PSScriptRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutLog `
            -RedirectStandardError $stderrLog `
            -PassThru
    } finally {
        $env:LOCALIZATION_ENGINE_DGX_EMBEDDING_BASE_URLS = $previousUrls
        $env:LOCALIZATION_ENGINE_EMBEDDING_PARALLEL_REQUESTS = $previousParallel
    }
    Write-Host "[localization] started pid=$($process.Id) rows=$Rows repo_pool=$RepoPool run_id=$RunId parallel=$WorkerParallelRequests"
    Write-Host "[localization] logs stdout=$stdoutLog stderr=$stderrLog"
    return $process
}

function Start-TunnelSupervisorProcess {
    return Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $PSCommandPath, "-TunnelSupervisor") `
        -WindowStyle Hidden `
        -PassThru
}

if ($TunnelSupervisor) {
    $mutex = [System.Threading.Mutex]::new($false, "Local\ArkEvalEmbeddingTunnelSupervisor")
    try {
        try {
            $ownsMutex = $mutex.WaitOne(0)
        } catch [System.Threading.AbandonedMutexException] {
            $ownsMutex = $true
        }
        if (-not $ownsMutex) {
            exit 0
        }
        $managed = @{}
        $supervisorLog = Join-Path $PSScriptRoot "localization\outputs\99_experiment_artifacts\embedding_tunnel_supervisor.log"
        while ($true) {
            foreach ($tunnel in $tunnels) {
                try {
                    $port = [int]$tunnel.LocalPort
                    $process = $managed[$port]
                    if ($null -ne $process -and -not $process.HasExited) {
                        continue
                    }
                    [void]$managed.Remove($port)
                    if (Test-PortListening -Port $port) {
                        continue
                    }
                    $process = Start-Tunnel -Tunnel $tunnel
                    if ($null -ne $process) {
                        $managed[$port] = $process
                    }
                } catch {
                    Add-Content -LiteralPath $supervisorLog -Value "$(Get-Date -Format o) port=$($tunnel.LocalPort) error=$($_.Exception.Message)" -ErrorAction SilentlyContinue
                }
            }
            Start-Sleep -Seconds 5
        }
    } finally {
        if ($ownsMutex) {
            $mutex.ReleaseMutex()
        }
        $mutex.Dispose()
    }
}

Write-Host "[start] arkeval embedding cluster bootstrap"
Set-EmbeddingEnvironment

$null = Start-TunnelSupervisorProcess

$startupDeadline = (Get-Date).AddSeconds(60)
do {
    Start-Sleep -Seconds 1
    $localPorts = $tunnels | ForEach-Object { [int]$_.LocalPort }
    $listening = Get-NetTCPConnection -LocalPort $localPorts -State Listen -ErrorAction SilentlyContinue |
        Sort-Object LocalPort |
        Select-Object -Unique LocalPort, OwningProcess, @{Name = "ProcessName"; Expression = { (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName } }
} while (@($listening).Count -lt $tunnels.Count -and (Get-Date) -lt $startupDeadline)

$localPorts = $tunnels | ForEach-Object { [int]$_.LocalPort }
$listening = Get-NetTCPConnection -LocalPort $localPorts -State Listen -ErrorAction SilentlyContinue |
    Sort-Object LocalPort |
    Select-Object -Unique LocalPort, OwningProcess, @{Name = "ProcessName"; Expression = { (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName } }
$listening | Format-Table -AutoSize
if (@($listening).Count -ne $tunnels.Count) {
    throw "embedding tunnels incomplete: expected=$($tunnels.Count) listening=$(@($listening).Count)"
}
$foreignListeners = @($listening | Where-Object { $_.ProcessName -ne "ssh" })
if ($foreignListeners.Count -gt 0) {
    throw "embedding tunnel port is owned by a non-ssh process: $($foreignListeners.LocalPort -join ',')"
}
$supervisors = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq "powershell.exe" -and $_.CommandLine -match "(?:^|\s)-TunnelSupervisor(?:\s|$)"
})
if ($supervisors.Count -ne 1) {
    throw "embedding tunnel supervisor count mismatch: $($supervisors.Count)"
}
foreach ($tunnel in $tunnels) {
    $listener = $listening | Where-Object { $_.LocalPort -eq [int]$tunnel.LocalPort } | Select-Object -First 1
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    $forward = "$($tunnel.LocalPort):127.0.0.1:$($tunnel.RemotePort)"
    if ($null -eq $owner -or $owner.CommandLine -notlike "*$forward*" -or $owner.CommandLine -notlike "*$($tunnel.Host)*") {
        throw "embedding tunnel ownership mismatch: port=$($tunnel.LocalPort) pid=$($listener.OwningProcess)"
    }
}

if (-not $SkipVerify) {
    foreach ($tunnel in $tunnels) {
        Test-Endpoint -Tunnel $tunnel
    }
}

if ($StartThreeWayLocalization -or $StartLocalizationWorkers) {
    $stamp = if ($LocalizationRunStamp) { $LocalizationRunStamp } else { Get-Date -Format "yyyyMMdd_HHmmss" }
    if ($stamp -notmatch "^[0-9A-Za-z_-]+$") {
        throw "invalid localization run stamp: $stamp"
    }
    if ($LocalizationRepoPools.Count -eq 0) {
        $LocalizationRepoPools = @(
            "depend\repair_repo\run01",
            "depend\repair_repo\run02",
            "depend\repair_repo\run03"
        )
    }
    if ($LocalizationRowGroups.Count -eq 0) {
        $LocalizationRowGroups = @($Rows1, $Rows2, $Rows3)
    }
    if ($LocalizationRepoPools.Count -ne $LocalizationRowGroups.Count) {
        throw "worker configuration mismatch: repo_pools=$($LocalizationRepoPools.Count) row_groups=$($LocalizationRowGroups.Count)"
    }
    $resolvedPools = @($LocalizationRepoPools | ForEach-Object {
        if (-not (Test-Path $_)) {
            throw "repo pool not found: $_"
        }
        (Resolve-Path $_).Path
    })
    $uniquePools = @($resolvedPools | Sort-Object -Unique)
    if ($uniquePools.Count -ne $resolvedPools.Count) {
        throw "localization workers must use distinct repo pools"
    }
    if ($EmbeddingParallelRequests -lt $resolvedPools.Count) {
        throw "embedding parallel request budget must be at least the worker count"
    }
    $allEmbeddingUrls = @($tunnels | ForEach-Object { "http://127.0.0.1:$($_.LocalPort)" })
    $baseParallel = [math]::Floor($EmbeddingParallelRequests / $resolvedPools.Count)
    $extraParallel = $EmbeddingParallelRequests % $resolvedPools.Count
    Write-Host "[localization] host=$env:COMPUTERNAME workers=$($resolvedPools.Count)"
    for ($index = 0; $index -lt $resolvedPools.Count; $index++) {
        $worker = $index + 1
        $rows = $LocalizationRowGroups[$index]
        $pool = $resolvedPools[$index]
        $workerParallel = $baseParallel + $(if ($index -lt $extraParallel) { 1 } else { 0 })
        $rotatedUrls = @()
        for ($offset = 0; $offset -lt $allEmbeddingUrls.Count; $offset++) {
            $rotatedUrls += $allEmbeddingUrls[($index + $offset) % $allEmbeddingUrls.Count]
        }
        $process = Start-LocalizationWindow `
            -Rows $rows `
            -RepoPool $pool `
            -RunId "$RunPrefix`_p$worker`_$stamp" `
            -EmbeddingUrls ($rotatedUrls -join ",") `
            -WorkerParallelRequests $workerParallel
    }
}

Write-Host "[done] embedding tunnels are ready"
