[CmdletBinding()]
param(
    [string]$InstallerPath = "",
    [string]$ProjectRoot = "",
    [int]$OldPid = 0,
    [string]$FallbackExe = "",
    [switch]$DryRun,
    [switch]$Worker,
    [string]$RequestFile = ""
)

$ErrorActionPreference = "Stop"
$ConfigDir = Join-Path $env:LOCALAPPDATA "LocalDevMCPBridge"
$LogFile = Join-Path $ConfigDir "upgrade.log"

function Write-UpgradeLog {
    param([string]$Message)
    New-Item -ItemType Directory -Force $ConfigDir | Out-Null
    $line = "[{0}] {1}`r`n" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    [IO.File]::AppendAllText($LogFile, $line, [Text.Encoding]::UTF8)
}

function Write-JsonAtomic {
    param([string]$Path, [object]$Value)
    $tmp = "$Path.tmp"
    $json = $Value | ConvertTo-Json -Depth 8
    [IO.File]::WriteAllText($tmp, $json, (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $tmp -Destination $Path -Force
}

function Test-LoopbackPort {
    param([int]$Port)
    if ($Port -le 0) { return $false }
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(500)) { return $false }
        $client.EndConnect($async)
        return $true
    }
    catch { return $false }
    finally { $client.Dispose() }
}

function Get-ExpectedPort {
    param([string]$Root)
    $projectsPath = Join-Path $ConfigDir "projects.json"
    $configPath = Join-Path $ConfigDir "config.json"
    $project = $null
    if (Test-Path $projectsPath) {
        try {
            $payload = Get-Content -LiteralPath $projectsPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $project = @($payload.projects) | Where-Object { $_.root_path -and ([IO.Path]::GetFullPath($_.root_path).TrimEnd('\') -ieq [IO.Path]::GetFullPath($Root).TrimEnd('\')) } | Select-Object -First 1
        }
        catch { }
    }
    $global = $null
    if (Test-Path $configPath) {
        try { $global = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { }
    }
    if ($project -and $project.connection -eq "local") {
        if ([int]$project.codexpro_port -gt 0) { return [int]$project.codexpro_port }
        if ($global -and [int]$global.codexpro_port -gt 0) { return [int]$global.codexpro_port }
        return 8787
    }
    if ($project -and [int]$project.gateway_port -gt 0) { return [int]$project.gateway_port }
    if ($global -and [int]$global.gateway_port -gt 0) { return [int]$global.gateway_port }
    return 8786
}

function New-DesktopShortcut {
    param([string]$TargetExe)
    $desktop = [Environment]::GetFolderPath("Desktop")
    Get-ChildItem -LiteralPath $desktop -Filter "*.lnk" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "MCPDevBridge*" -or $_.Name -like "MCP DevBridge*" } |
        Remove-Item -Force -ErrorAction SilentlyContinue
    $shortcutPath = Join-Path $desktop "MCP DevBridge.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $TargetExe
    $shortcut.WorkingDirectory = Split-Path -Parent $TargetExe
    $shortcut.IconLocation = "$TargetExe,0"
    $shortcut.Description = "MCP DevBridge"
    $shortcut.Save()
    return $shortcutPath
}

if (-not $Worker) {
    New-Item -ItemType Directory -Force $ConfigDir | Out-Null
    if (-not $InstallerPath) { throw "InstallerPath is required." }
    $InstallerPath = [IO.Path]::GetFullPath($InstallerPath)
    if (-not (Test-Path -LiteralPath $InstallerPath)) { throw "Installer not found: $InstallerPath" }
    if (-not $ProjectRoot) {
        $configPath = Join-Path $ConfigDir "config.json"
        if (Test-Path $configPath) {
            $cfg = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $ProjectRoot = [string]$cfg.active_workspace
        }
    }
    if (-not $ProjectRoot) { throw "ProjectRoot is required and active_workspace is empty." }
    $ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
    if ($OldPid -le 0) {
        $candidate = Get-CimInstance Win32_Process |
            Where-Object { $_.Name -ieq "MCPDevBridge.exe" } |
            Sort-Object ProcessId |
            Select-Object -First 1
        if ($candidate) { $OldPid = [int]$candidate.ProcessId }
    }
    if ($FallbackExe) { $FallbackExe = [IO.Path]::GetFullPath($FallbackExe) }

    $request = [ordered]@{
        installer_path = $InstallerPath
        project_root = $ProjectRoot
        old_pid = $OldPid
        fallback_exe = $FallbackExe
        dry_run = [bool]$DryRun
        task_name = "MCPDevBridge-LiveUpgrade-" + (Get-Date -Format "yyyyMMdd-HHmmss")
        requested_at = (Get-Date).ToString("o")
    }
    $RequestFile = Join-Path $ConfigDir "upgrade-worker-request.json"
    Write-JsonAtomic -Path $RequestFile -Value $request

    $workerCmd = Join-Path $ConfigDir "upgrade-worker.cmd"
    $scriptPath = [IO.Path]::GetFullPath($PSCommandPath)
    @(
        "@echo off",
        "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`" -Worker -RequestFile `"$RequestFile`""
    ) | Set-Content -LiteralPath $workerCmd -Encoding ASCII

    $when = (Get-Date).AddMinutes(1).ToString("HH:mm")
    $taskCommand = "cmd.exe /c `"$workerCmd`""
    & schtasks.exe /Create /TN $request.task_name /TR $taskCommand /SC ONCE /ST $when /F | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Unable to create detached upgrade task." }
    & schtasks.exe /Run /TN $request.task_name | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Unable to start detached upgrade task." }
    Write-UpgradeLog "Detached upgrade task started: $($request.task_name); dry_run=$([bool]$DryRun)"
    Write-Output "UPGRADE_TASK_STARTED $($request.task_name)"
    return
}

if (-not $RequestFile -or -not (Test-Path -LiteralPath $RequestFile)) {
    throw "Worker request file is missing."
}
$request = Get-Content -LiteralPath $RequestFile -Raw -Encoding UTF8 | ConvertFrom-Json
$taskName = [string]$request.task_name
try {
    if ([bool]$request.dry_run) {
        $resultPath = Join-Path $ConfigDir "upgrade-dryrun-result.json"
        Write-JsonAtomic -Path $resultPath -Value ([ordered]@{
            ok = $true
            worker_pid = $PID
            project_root = [string]$request.project_root
            task_name = $taskName
            finished_at = (Get-Date).ToString("o")
        })
        Write-UpgradeLog "Detached upgrade dry-run completed."
        return
    }

    $installer = [IO.Path]::GetFullPath([string]$request.installer_path)
    $projectRoot = [IO.Path]::GetFullPath([string]$request.project_root)
    $oldPidValue = [int]$request.old_pid
    $fallback = [string]$request.fallback_exe
    $resumePath = Join-Path $ConfigDir "upgrade-resume.json"
    Write-JsonAtomic -Path $resumePath -Value ([ordered]@{
        project_root = $projectRoot
        requested_at = (Get-Date).ToString("o")
    })
    Write-UpgradeLog "Resume request written for project: $projectRoot"

    Start-Sleep -Seconds 2
    if ($oldPidValue -gt 0 -and (Get-Process -Id $oldPidValue -ErrorAction SilentlyContinue)) {
        Write-UpgradeLog "Stopping old MCP DevBridge process tree: PID=$oldPidValue"
        & taskkill.exe /PID $oldPidValue /T /F | Out-Null
        Start-Sleep -Seconds 2
    }

    $installArgs = @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER", "/TASKS=desktopicon")
    Write-UpgradeLog "Installing: $installer"
    $install = Start-Process -FilePath $installer -ArgumentList $installArgs -PassThru -Wait
    if ($install.ExitCode -ne 0) {
        Write-UpgradeLog "Installer failed with exit code $($install.ExitCode)."
        if ($fallback -and (Test-Path -LiteralPath $fallback)) {
            Write-UpgradeLog "Launching staging fallback: $fallback"
            Start-Process -FilePath $fallback | Out-Null
        }
        throw "Installer failed: exit=$($install.ExitCode)"
    }

    $installedExe = Join-Path $env:LOCALAPPDATA "Programs\MCP DevBridge\MCPDevBridge.exe"
    if (-not (Test-Path -LiteralPath $installedExe)) {
        if ($fallback -and (Test-Path -LiteralPath $fallback)) {
            Write-UpgradeLog "Installed executable missing; launching staging fallback."
            Start-Process -FilePath $fallback | Out-Null
        }
        throw "Installed executable not found: $installedExe"
    }
    $shortcut = New-DesktopShortcut -TargetExe $installedExe
    Write-UpgradeLog "Desktop shortcut replaced: $shortcut"

    $newProcess = Start-Process -FilePath $installedExe -PassThru
    Write-UpgradeLog "Started new MCP DevBridge candidate: PID=$($newProcess.Id)"

    $deadline = (Get-Date).AddSeconds(150)
    $ready = $false
    $expectedPort = 0
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        if ($newProcess.HasExited) {
            Write-UpgradeLog "New MCP DevBridge exited early: $($newProcess.ExitCode)"
            break
        }
        $expectedPort = Get-ExpectedPort -Root $projectRoot
        if (Test-LoopbackPort -Port $expectedPort) {
            $ready = $true
            break
        }
    }

    $resultPath = Join-Path $ConfigDir "upgrade-result.json"
    Write-JsonAtomic -Path $resultPath -Value ([ordered]@{
        ok = $ready
        project_root = $projectRoot
        expected_port = $expectedPort
        process_id = $newProcess.Id
        installed_exe = $installedExe
        shortcut = $shortcut
        finished_at = (Get-Date).ToString("o")
    })
    if (-not $ready) { throw "New bridge did not become ready before timeout." }
    Write-UpgradeLog "Upgrade completed; loopback port $expectedPort is ready."
}
catch {
    Write-UpgradeLog "Upgrade worker failed: $($_.Exception.Message)"
    $failurePath = Join-Path $ConfigDir "upgrade-result.json"
    Write-JsonAtomic -Path $failurePath -Value ([ordered]@{
        ok = $false
        error = $_.Exception.Message
        finished_at = (Get-Date).ToString("o")
    })
}
finally {
    if ($taskName) { & schtasks.exe /Delete /TN $taskName /F 2>$null | Out-Null }
    Remove-Item -LiteralPath $RequestFile -Force -ErrorAction SilentlyContinue
    $workerCmd = Join-Path $ConfigDir "upgrade-worker.cmd"
    Remove-Item -LiteralPath $workerCmd -Force -ErrorAction SilentlyContinue
}
