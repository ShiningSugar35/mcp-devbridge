[CmdletBinding()]
param(
    [string]$InstallerPath = "",
    [string]$ProjectRoot = "",
    [int]$OldPid = 0,
    [string]$FallbackExe = "",
    [switch]$DryRun,
    [switch]$Worker,
    [string]$RequestFile = "",
    [string]$ArtifactDirectory = "",
    [switch]$PreserveShortcuts
)

$ErrorActionPreference = "Stop"
$ConfigDir = Join-Path $env:LOCALAPPDATA "LocalDevMCPBridge"
function Resolve-UpgradeArtifactDirectory {
    param([string]$ProjectRoot, [string]$Directory)
    if (-not $ProjectRoot -or -not $Directory) { throw "Explicit project and artifact directory are required." }
    $root = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
    $target = [IO.Path]::GetFullPath($Directory).TrimEnd('\')
    if (-not $target.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Upgrade artifacts must be inside the selected project."
    }
    # Reject existing junction/symlink ancestors before creating anything.
    $cursor = $target
    while ($cursor -and $cursor.Length -ge $root.Length) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw "Upgrade artifact directory cannot traverse a file or reparse point."
            }
        }
        if ([string]::Equals($cursor, $root, [StringComparison]::OrdinalIgnoreCase)) { break }
        $cursor = Split-Path -Parent $cursor
    }
    return $target
}

function Get-UpgradeInstallerArguments {
    param([string]$InstallRoot, [bool]$PreserveShortcuts)
    $arguments = @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER")
    if ($PreserveShortcuts) { $arguments += @('/NOICONS', '/TASKS=""') }
    else { $arguments += '/TASKS=desktopicon' }
    if ($InstallRoot) { $arguments += ('/DIR="{0}"' -f $InstallRoot) }
    return $arguments
}

$RunDir = $ConfigDir
if ($ArtifactDirectory) {
    $RunDir = Resolve-UpgradeArtifactDirectory -ProjectRoot $ProjectRoot -Directory $ArtifactDirectory
}
$LogFile = Join-Path $RunDir "upgrade.log"

function Write-UpgradeLog {
    param([string]$Message)
    New-Item -ItemType Directory -Force $RunDir | Out-Null
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

function Test-AdministratorToken {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
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
    $configPath = Join-Path $ConfigDir "config.json"
    if (Test-Path $configPath) {
        try {
            $global = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($global -and [int]$global.gateway_port -gt 0) { return [int]$global.gateway_port }
        }
        catch { }
    }
    return 8786
}
function Get-ElevatedBrokerState {
    $statePath = Join-Path $ConfigDir "elevated-broker.json"
    if (-not (Test-Path -LiteralPath $statePath)) { return $null }
    try {
        $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
        if (-not $state -or -not [bool]$state.elevated -or [int]$state.pid -le 0) {
            return $null
        }
        if (-not (Get-Process -Id ([int]$state.pid) -ErrorAction SilentlyContinue)) {
            return $null
        }
        return $state
    }
    catch { return $null }
}

function Get-UpgradeOwnedProcesses {
    param([string]$InstallRoot, [object[]]$Processes)
    if ([string]::IsNullOrWhiteSpace($InstallRoot)) {
        throw "Cannot establish the upgrade installation identity."
    }
    $expectedExe = [IO.Path]::GetFullPath((Join-Path $InstallRoot "MCPDevBridge.exe"))
    return @($Processes | Where-Object {
        $_.Name -ieq "MCPDevBridge.exe" -and $_.ExecutablePath -and
        [string]::Equals(
            [IO.Path]::GetFullPath([string]$_.ExecutablePath),
            $expectedExe, [StringComparison]::OrdinalIgnoreCase
        )
    })
}

function Test-ResumeProjectReady {
    param([object]$Project, [object]$BrokerState)
    $root = [string]$Project.root
    $port = [int]$Project.port
    $permissionMode = [string]$Project.permission_mode
    if (-not $root -or $port -le 0 -or -not (Test-LoopbackPort -Port $port)) {
        return $false
    }
    try { $root = [IO.Path]::GetFullPath($root) }
    catch { return $false }
    $rootPattern = '--root\s+(?:"' + [regex]::Escape($root) + '"|' + [regex]::Escape($root) + ')(?=\s+--port\s)'
    $portPattern = '--port\s+' + [regex]::Escape([string]$port) + '(?:\s|$)'
    $nodes = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -ieq "node.exe" -and
        [string]$_.CommandLine -match $rootPattern -and
        [string]$_.CommandLine -match $portPattern
    })
    foreach ($node in $nodes) {
        if ($permissionMode -eq "system") {
            if ($BrokerState -and [bool]$BrokerState.elevated -and
                [int]$node.ParentProcessId -eq [int]$BrokerState.pid) {
                return $true
            }
            continue
        }
        return $true
    }
    return $false
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
    New-Item -ItemType Directory -Force $RunDir | Out-Null
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
    if ($ProjectRoot) { $ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot) }
    $currentInstallDir = ""
    if ($OldPid -le 0) {
        $candidate = Get-CimInstance Win32_Process |
            Where-Object { $_.Name -ieq "MCPDevBridge.exe" } |
            Sort-Object ProcessId |
            Select-Object -First 1
        if ($candidate) {
            $OldPid = [int]$candidate.ProcessId
            if ($candidate.ExecutablePath) {
                $currentInstallDir = Split-Path -Parent ([IO.Path]::GetFullPath([string]$candidate.ExecutablePath))
            }
        }
    } else {
        $candidate = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $OldPid) -ErrorAction SilentlyContinue
        if ($candidate -and $candidate.ExecutablePath) {
            $currentInstallDir = Split-Path -Parent ([IO.Path]::GetFullPath([string]$candidate.ExecutablePath))
        }
    }
    if ($FallbackExe) { $FallbackExe = [IO.Path]::GetFullPath($FallbackExe) }

    # Preserve every currently-running project engine. No project is a Hub entry/owner.
    # Each additional project has its own CodexPro port and can be restored independently
    # after the desktop process tree is replaced by the installer.
    $resumeProjectRoots = @()
    $resumeProjects = @()
    $projectsPath = Join-Path $ConfigDir "projects.json"
    if (Test-Path -LiteralPath $projectsPath) {
        try {
            $projectPayload = Get-Content -LiteralPath $projectsPath -Raw -Encoding UTF8 | ConvertFrom-Json
            foreach ($candidateProject in @($projectPayload.projects)) {
                $candidateRoot = [string]$candidateProject.root_path
                $candidatePort = [int]$candidateProject.codexpro_port
                if ($candidateRoot -and $candidatePort -gt 0 -and (Test-LoopbackPort -Port $candidatePort)) {
                    $normalizedRoot = [IO.Path]::GetFullPath($candidateRoot)
                    if ($resumeProjectRoots -notcontains $normalizedRoot) {
                        $resumeProjectRoots += $normalizedRoot
                        $resumeProjects += [ordered]@{
                            root = $normalizedRoot
                            port = $candidatePort
                            permission_mode = [string]$candidateProject.permission_mode
                        }
                    }
                }
            }
        }
        catch {
            Write-UpgradeLog "Unable to snapshot additional running projects: $($_.Exception.Message)"
        }
    }

    $launcherElevated = Test-AdministratorToken
    $request = [ordered]@{
        installer_path = $InstallerPath
        project_root = $ProjectRoot
        artifact_directory = $ArtifactDirectory
        preserve_shortcuts = [bool]$PreserveShortcuts
        resume_project_roots = @($resumeProjectRoots)
        resume_projects = @($resumeProjects)
        old_pid = $OldPid
        fallback_exe = $FallbackExe
        install_dir = $currentInstallDir
        dry_run = [bool]$DryRun
        launcher_elevated = [bool]$launcherElevated
        task_name = "MCPDevBridge-LiveUpgrade-" + (Get-Date -Format "yyyyMMdd-HHmmss")
        requested_at = (Get-Date).ToString("o")
    }
    $RequestFile = Join-Path $RunDir "upgrade-worker-request.json"
    Write-JsonAtomic -Path $RequestFile -Value $request

    $workerCmd = Join-Path $RunDir "upgrade-worker.cmd"
    $scriptPath = [IO.Path]::GetFullPath($PSCommandPath)
    $workerOptions = ""
    if ($ArtifactDirectory) {
        $workerOptions = " -ProjectRoot `"$ProjectRoot`" -ArtifactDirectory `"$RunDir`""
    }
    @(
        "@echo off",
        "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`" -Worker -RequestFile `"$RequestFile`"$workerOptions"
    ) | Set-Content -LiteralPath $workerCmd -Encoding ASCII

    $when = (Get-Date).AddMinutes(1).ToString("HH:mm")
    $taskCommand = "cmd.exe /c `"$workerCmd`""
    $createArgs = @("/Create", "/TN", $request.task_name, "/TR", $taskCommand, "/SC", "ONCE", "/ST", $when, "/F")
    if ($launcherElevated) { $createArgs += @("/RL", "HIGHEST") }
    & schtasks.exe @createArgs | Out-Null
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
        $resultPath = Join-Path $RunDir "upgrade-dryrun-result.json"
        Write-JsonAtomic -Path $resultPath -Value ([ordered]@{
            ok = $true
            worker_pid = $PID
            project_root = [string]$request.project_root
            task_name = $taskName
            launcher_elevated = [bool]$request.launcher_elevated
            worker_elevated = [bool](Test-AdministratorToken)
            finished_at = (Get-Date).ToString("o")
        })
        Write-UpgradeLog "Detached upgrade dry-run completed."
        return
    }

    $installer = [IO.Path]::GetFullPath([string]$request.installer_path)
    $projectRoot = [string]$request.project_root
    if ($projectRoot) { $projectRoot = [IO.Path]::GetFullPath($projectRoot) }
    $oldPidValue = [int]$request.old_pid
    $fallback = [string]$request.fallback_exe
    $installDir = [string]$request.install_dir
    if ($installDir) { $installDir = [IO.Path]::GetFullPath($installDir) }
    $resumePath = Join-Path $ConfigDir "upgrade-resume.json"
    $resumeRoots = @($request.resume_project_roots)
    $resumeProjects = @($request.resume_projects)
    if ($resumeProjects.Count -eq 0 -and $resumeRoots.Count -gt 0) {
        $projectsPath = Join-Path $ConfigDir "projects.json"
        if (Test-Path -LiteralPath $projectsPath) {
            try {
                $projectPayload = Get-Content -LiteralPath $projectsPath -Raw -Encoding UTF8 | ConvertFrom-Json
                foreach ($candidateProject in @($projectPayload.projects)) {
                    $candidateRoot = [IO.Path]::GetFullPath([string]$candidateProject.root_path)
                    if ($resumeRoots -contains $candidateRoot) {
                        $resumeProjects += [ordered]@{
                            root = $candidateRoot
                            port = [int]$candidateProject.codexpro_port
                            permission_mode = [string]$candidateProject.permission_mode
                        }
                    }
                }
            }
            catch {
                Write-UpgradeLog "Unable to reconstruct resume project metadata: $($_.Exception.Message)"
            }
        }
    }
    if ($resumeRoots.Count -gt 0) {
        Write-JsonAtomic -Path $resumePath -Value ([ordered]@{
            project_roots = @($resumeRoots)
            requested_at = (Get-Date).ToString("o")
        })
        Write-UpgradeLog "Resume request written for $($resumeRoots.Count) equal running roots."
    } else {
        Remove-Item -LiteralPath $resumePath -Force -ErrorAction SilentlyContinue
        Write-UpgradeLog "No running project roots; update will restart only the desktop."
    }
    Start-Sleep -Seconds 2
    $snapshot = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $oldProcesses = @(Get-UpgradeOwnedProcesses -InstallRoot $installDir -Processes $snapshot)
    if ($oldPidValue -le 0 -or @($oldProcesses | Where-Object {
        [int]$_.ProcessId -eq $oldPidValue
    }).Count -ne 1) {
        throw "Upgrade parent is not an owned installation process."
    }
    # Only the selected desktop and this profile's recorded broker are owned.
    # Another user's process or installation must never be stopped by name.
    $ownedIds = @($oldPidValue)
    $ownedBroker = Get-ElevatedBrokerState
    if ($ownedBroker) { $ownedIds += [int]$ownedBroker.pid }
    $oldProcesses = @($oldProcesses | Where-Object { $ownedIds -contains [int]$_.ProcessId })
    foreach ($proc in @($oldProcesses)) {
        $pidValue = [int]$proc.ProcessId
        if ($pidValue -le 0) { continue }
        $current = @(Get-CimInstance Win32_Process -Filter ("ProcessId=" + $pidValue) -ErrorAction SilentlyContinue)
        if ($current.Count -eq 0) { continue }
        $confirmed = @(Get-UpgradeOwnedProcesses -InstallRoot $installDir -Processes $current)
        if ($confirmed.Count -ne 1 -or $confirmed[0].CreationDate -ne $proc.CreationDate) {
            throw "Upgrade process identity changed before stopping."
        }
        Write-UpgradeLog "Stopping owned MCP DevBridge process tree: PID=$pidValue"
        & taskkill.exe /PID $pidValue /T /F | Out-Null
    }
    if (@($oldProcesses).Count -gt 0) { Start-Sleep -Seconds 2 }

    $installArgs = @(Get-UpgradeInstallerArguments -InstallRoot $installDir -PreserveShortcuts ([bool]$request.preserve_shortcuts))
    Write-UpgradeLog "Installing: $installer; target=$installDir"
    $previousTemp = $env:TEMP
    $previousTmp = $env:TMP
    try {
        if ($ArtifactDirectory) {
            $installTemp = Join-Path $RunDir "installer-temp"
            New-Item -ItemType Directory -Force $installTemp | Out-Null
            $env:TEMP = $installTemp
            $env:TMP = $installTemp
        }
        $install = Start-Process -FilePath $installer -ArgumentList $installArgs -PassThru -Wait
    }
    finally {
        $env:TEMP = $previousTemp
        $env:TMP = $previousTmp
    }
    if ($install.ExitCode -ne 0) {
        Write-UpgradeLog "Installer failed with exit code $($install.ExitCode)."
        if ($fallback -and (Test-Path -LiteralPath $fallback)) {
            Write-UpgradeLog "Launching staging fallback: $fallback"
            Start-Process -FilePath $fallback | Out-Null
        }
        throw "Installer failed: exit=$($install.ExitCode)"
    }

    $installedExe = if ($installDir) {
        Join-Path $installDir "MCPDevBridge.exe"
    } else {
        Join-Path $env:LOCALAPPDATA "Programs\MCP DevBridge\MCPDevBridge.exe"
    }
    if (-not (Test-Path -LiteralPath $installedExe)) {
        if ($fallback -and (Test-Path -LiteralPath $fallback)) {
            Write-UpgradeLog "Installed executable missing; launching staging fallback."
            Start-Process -FilePath $fallback | Out-Null
        }
        throw "Installed executable not found: $installedExe"
    }
    $shortcut = ""
    if (-not [bool]$request.preserve_shortcuts) {
        $shortcut = New-DesktopShortcut -TargetExe $installedExe
        Write-UpgradeLog "Desktop shortcut replaced: $shortcut"
    } else {
        Write-UpgradeLog "Existing desktop and Start Menu shortcuts preserved."
    }

    $newProcess = Start-Process -FilePath $installedExe -PassThru
    Write-UpgradeLog "Started new MCP DevBridge candidate: PID=$($newProcess.Id)"

    $deadline = (Get-Date).AddSeconds(150)
    $ready = $false
    $expectedPort = 0
    $readyProjectCount = 0
    $resumeConsumed = $resumeRoots.Count -eq 0
    $brokerElevated = $false
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        if ($newProcess.HasExited) {
            Write-UpgradeLog "New MCP DevBridge exited early: $($newProcess.ExitCode)"
            break
        }
        if ($resumeRoots.Count -eq 0) {
            $ready = $true
            break
        }
        $expectedPort = Get-ExpectedPort
        $resumeConsumed = -not (Test-Path -LiteralPath $resumePath)
        $brokerState = Get-ElevatedBrokerState
        $brokerElevated = [bool]($brokerState -and [bool]$brokerState.elevated)
        $readyProjectCount = 0
        foreach ($resumeProject in $resumeProjects) {
            if (Test-ResumeProjectReady -Project $resumeProject -BrokerState $brokerState) {
                $readyProjectCount += 1
            }
        }
        if ((Test-LoopbackPort -Port $expectedPort) -and $resumeConsumed -and
            $readyProjectCount -eq $resumeProjects.Count -and
            $resumeProjects.Count -eq $resumeRoots.Count) {
            $ready = $true
            break
        }
    }

    $resultPath = Join-Path $RunDir "upgrade-result.json"
    Write-JsonAtomic -Path $resultPath -Value ([ordered]@{
        ok = $ready
        project_root = $projectRoot
        expected_port = $expectedPort
        expected_project_count = $resumeProjects.Count
        ready_project_count = $readyProjectCount
        resume_consumed = $resumeConsumed
        elevated_broker_ready = $brokerElevated
        process_id = $newProcess.Id
        installed_exe = $installedExe
        shortcut = $shortcut
        finished_at = (Get-Date).ToString("o")
    })
    if (-not $ready) {
        throw "New bridge did not restore every previously running project before timeout."
    }
    Write-UpgradeLog $(if ($expectedPort -gt 0) {
        "Upgrade completed; Gateway $expectedPort and $readyProjectCount/$($resumeProjects.Count) project roots are ready."
    } else {
        "Upgrade completed; desktop restarted successfully."
    })
}
catch {
    Write-UpgradeLog "Upgrade worker failed: $($_.Exception.Message)"
    $failurePath = Join-Path $RunDir "upgrade-result.json"
    Write-JsonAtomic -Path $failurePath -Value ([ordered]@{
        ok = $false
        error = $_.Exception.Message
        finished_at = (Get-Date).ToString("o")
    })
}
finally {
    if ($taskName) { & schtasks.exe /Delete /TN $taskName /F 2>$null | Out-Null }
    Remove-Item -LiteralPath $RequestFile -Force -ErrorAction SilentlyContinue
    $workerCmd = Join-Path $RunDir "upgrade-worker.cmd"
    Remove-Item -LiteralPath $workerCmd -Force -ErrorAction SilentlyContinue
}
