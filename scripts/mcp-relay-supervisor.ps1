$ErrorActionPreference = 'Stop'
$stateFile = Join-Path $PSScriptRoot 'state.json'
$sshExe = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
$installedExe = 'D:\mcp-devbridge\MCP DevBridge-0.8.9.7\MCPDevBridge.exe'
$mutex = New-Object System.Threading.Mutex($false, 'Local\MCPDevBridgeSshRelay20261013')
$ownsMutex = $false
$relayChild = $null
$lastState = ''
$failures = 0
$stopRequest = Join-Path $PSScriptRoot 'stop.request'

function Write-RelayState($state, $detail) {
    if ($script:lastState -eq ($state + ':' + $detail)) { return }
    $script:lastState = $state + ':' + $detail
    @{ at = (Get-Date).ToString('o'); state = $state; detail = $detail;
       ssh_pid = $(if ($script:relayChild) { $script:relayChild.Id } else { $null });
       ssh_started_at = $(if ($script:relayChild) { $script:relayChild.StartTime.ToString('o') } else { $null }) } |
        ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding UTF8
}

function Stop-OwnedForward {
    if ($script:relayChild) {
        $script:relayChild.Refresh()
        if (-not $script:relayChild.HasExited) {
            # Process object retains the original process handle; no name-wide termination.
            $script:relayChild.Kill()
            $script:relayChild.WaitForExit(8000) | Out-Null
        }
        $script:relayChild.Dispose()
        $script:relayChild = $null
    }
}

try {
    $ownsMutex = $mutex.WaitOne(0)
    if (-not $ownsMutex) { exit 0 }
    if (-not (Test-Path -LiteralPath $sshExe)) { throw 'Windows OpenSSH unavailable' }
    while ($true) {
        if (Test-Path -LiteralPath $stopRequest) { break }
        $listeners = @(Get-NetTCPConnection -State Listen -LocalPort 8786 -ErrorAction SilentlyContinue)
        $hubReady = $false
        foreach ($listener in $listeners) {
            $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
            if ($owner -and $owner.ExecutablePath -eq $installedExe) { $hubReady = $true }
        }
        if (-not $hubReady) {
            Stop-OwnedForward
            $failures = 0
            Write-RelayState 'idle' 'Waiting for the verified MCP Hub'
            Start-Sleep -Seconds 5
            continue
        }
        if ($relayChild) {
            $relayChild.Refresh()
            if (-not $relayChild.HasExited) {
                # This is SSH process liveness only; public MCP acceptance remains a separate check.
                Write-RelayState 'forward_running' 'SSH alive; public MCP must be verified separately'
                if (((Get-Date) - $relayChild.StartTime).TotalSeconds -ge 120) { $failures = 0 }
                Start-Sleep -Seconds 5
                continue
            }
            Write-RelayState 'forward_exited' ([string]$relayChild.ExitCode)
            Stop-OwnedForward
            $failures++
            if ($failures -ge 3) {
                Write-RelayState 'cooldown' 'Three failed forwards; cooling down for 60 seconds'
                Start-Sleep -Seconds 60
                $failures = 0
            } else { Start-Sleep -Seconds 10 }
        }
        $sshArgs = @('-N', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
            '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=15',
            '-o', 'ServerAliveCountMax=2', '-R', '127.0.0.1:8786:127.0.0.1:8786', 'opencode-server')
        $relayChild = Start-Process -FilePath $sshExe -ArgumentList $sshArgs -WindowStyle Hidden -PassThru
        Write-RelayState 'forward_starting' 'Dedicated MCP reverse forward only'
        Start-Sleep -Seconds 2
    }
} finally {
    Stop-OwnedForward
    if ($ownsMutex) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
