<#
    FleetHub - Unified Installer
    https://github.com/aw08-2004/Temp_Monitor

    Interactive menu over the three install paths:
      1) Agent      - C#/.NET Windows Service (recommended for new machines)
      2) Companion  - legacy Python scheduled-task agent (UNSUPPORTED; dimmed in the
                      menu and gated behind a confirmation, since it migrates itself
                      to the agent on its first self-update anyway)
      3) Hub        - Flask/Socket.IO server (this machine becomes the fleet hub)

    Prompts for whatever each path needs (enrollment secret, hub URL, OAuth
    creds, etc.), defaulting to values already present in a local .env when run
    from a clone. Non-interactive use is still supported by passing -Component
    plus the relevant parameters up front.

    Usage (elevated PowerShell):
        powershell -ExecutionPolicy Bypass -File install.ps1
        powershell -ExecutionPolicy Bypass -File install.ps1 -Component Agent -AgentUrl <url> -EnrollmentSecret <secret>
        powershell -ExecutionPolicy Bypass -File install.ps1 -Component Companion
        powershell -ExecutionPolicy Bypass -File install.ps1 -Component Hub
        powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall                    # legacy companion (back-compat)
        powershell -ExecutionPolicy Bypass -File install.ps1 -Component Agent -Uninstall
        powershell -ExecutionPolicy Bypass -File install.ps1 -Component Hub -Uninstall

    From the web:
        irm https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/install.ps1 | iex
#>

param(
    [ValidateSet("Agent", "Companion", "Hub")]
    [string]$Component,
    [switch]$Uninstall,

    # --- Companion (legacy) ---
    [string]$InstallDir = "C:\Program Files\TempMonitor",
    [int]$Port = 8085,

    # --- Agent ---
    [string]$AgentUrl,
    [string]$AgentExe,
    [string]$EnrollmentSecret,
    [string]$HubUrl,

    # --- Hub ---
    [int]$HubPort = 3001,
    [string]$HubInstallDir
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
# Invoke-WebRequest renders a progress bar on PowerShell 5.1 that costs more time than the
# transfer itself on the bigger downloads here (the ~25 MB Python installer, the LHM zip).
# Silencing it keeps the run moving and the log readable.
$ProgressPreference = "SilentlyContinue"

$Repo           = "aw08-2004/Temp_Monitor"
$InstallerUrl   = "https://raw.githubusercontent.com/$Repo/main/install.ps1"
$CompanionUrl   = "https://raw.githubusercontent.com/$Repo/main/companion.py"
$AgentInstallUrl= "https://raw.githubusercontent.com/$Repo/main/agent/install/agent-install.ps1"
$LhmApi         = "https://api.github.com/repos/LibreHardwareMonitor/LibreHardwareMonitor/releases/latest"
$LhmFallback    = "https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/download/v0.9.6/LibreHardwareMonitor.zip"
$PythonFallback = "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe"
$PawnIoUrl      = "https://raw.githubusercontent.com/LibreHardwareMonitor/LibreHardwareMonitor/refs/heads/master/LibreHardwareMonitor.Windows.Forms/Resources/PawnIO_setup.exe"
$LhmDir         = Join-Path $InstallDir "LibreHardwareMonitor"
$TaskLhm        = "TempMonitor - LibreHardwareMonitor"
$TaskCompanion  = "TempMonitor - Companion"
$TaskHub        = "TempMonitor - Hub"          # legacy scheduled task (pre-service), cleaned up on install/uninstall

# --- Shared install root ---
# Hub and Agent live side by side under one root so an operator has a single place to
# look. Pre-rename installs used unrelated locations; those are detected and migrated
# rather than left running alongside a second copy.
$InstallRoot        = "C:\Program Files\FleetHub"
$HubInstallDefault  = Join-Path $InstallRoot "Hub"
$AgentInstallDir    = Join-Path $InstallRoot "Agent"
$LegacyHubDir       = "C:\Program Files\TempMonitor\Hub"

# --- Hub-as-Windows-Service ---
# WinSW wraps the Python/waitress process as a real Windows Service (Python can't be one on
# its own). Pinned to a stable v2 release; same "download a pinned asset" pattern as LHM/PawnIO.
$WinSwUrl            = "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe"
$HubServiceId        = "FleetHub"
$HubServiceName      = "FleetHub - Hub"
$LegacyHubServiceId  = "TempMonitorHub"

# --- Hub runtime file set ---
# The hub only needs these; the repo also carries the 85 MB agent/ tree, tests and docs
# that have no business on a server. Anything not listed here is not installed.
# Keep in sync with the same list in app.py's self-updater.
# EVERY module app.py imports must be listed. A missing one is an ImportError at startup,
# not a missing feature -- which is what happened to packages.py/packages_web.py in
# 1.27.x, where a sparse install could not boot at all.
$HubRuntimeFiles = @(
    "app.py", "wsgi.py", "fleet.py", "fleet_web.py",
    "settings.py", "settings_web.py", "permissions.py", "permissions_web.py",
    "users.py", "users_web.py",
    "packages.py", "packages_web.py", "backups.py", "backups_web.py",
    "backup_paths.py", "alerts.py", "restore_backup.py", "requirements.txt"
)
$HubRuntimeDirs  = @("templates", "static")
# Source archive for both first install and self-update. codeload serves a zip of a branch
# without needing git on the box -- Expand-Archive is native to PowerShell 5.1, so this
# adds no dependency (the old path required Git for Windows to be installed).
$RepoZipUrl      = "https://codeload.github.com/$Repo/zip/refs/heads/main"

function Say($msg)  { Write-Host "  $msg" }
function Ok($msg)   { Write-Host "  [ok] $msg"   -ForegroundColor Green }
function Warn($msg) { Write-Host "  [!!] $msg"   -ForegroundColor Yellow }
# Prints the failure, then throws instead of `exit`-ing: the top-level dispatch catches it and
# returns, so the (elevated, -NoExit) console stays open for the log. The marker prefix lets the
# handler tell "we already printed a friendly [xx]" apart from an unexpected error.
function Die($msg)  { Write-Host "  [xx] $msg"   -ForegroundColor Red; throw "TempMonitorInstaller: $msg" }
function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }

# ----------------------------------------------------------------------
# Small helpers shared by every install path
# ----------------------------------------------------------------------
function Mask([string]$v) {
    if (-not $v) { return "" }
    if ($v.Length -le 6) { return "******" }
    return $v.Substring(0, 3) + ("*" * 6) + $v.Substring($v.Length - 3)
}

function Prompt-Value([string]$Label, [string]$Default = "", [switch]$Secret,
                     [switch]$Required, [scriptblock]$Validate, [string]$ValidateHint) {
    # Reprompts instead of failing: an empty answer keeps the [default]; -Required rejects an
    # empty result; -Validate {param($v) ...} rejects a value that doesn't pass. Existing callers
    # that pass neither behave exactly as before.
    while ($true) {
        $shown = ""
        if ($Default) { if ($Secret) { $shown = " [" + (Mask $Default) + "]" } else { $shown = " [$Default]" } }
        $val = "$(Read-Host "$Label$shown")".Trim()
        if (-not $val) { $val = $Default }
        if ($Required -and -not $val) {
            Warn "A value is required -- please enter one."
            continue
        }
        if ($Validate -and $val) {
            $ok = $false
            try { $ok = [bool](& $Validate $val) } catch { $ok = $false }
            if (-not $ok) {
                if ($ValidateHint) { Warn $ValidateHint } else { Warn "That value isn't valid -- please try again." }
                continue
            }
        }
        return $val
    }
}

function Prompt-YesNo([string]$Question, [ValidateSet("Yes", "No")][string]$Default = "Yes") {
    # Reprompts on anything that isn't a yes/no. The ad-hoc `Read-Host "... (Y/n)"` calls this
    # replaces treated every typo as the default, so a fat-fingered answer silently installed
    # (or skipped) something the operator was asked about on purpose.
    $hint = if ($Default -eq "Yes") { "(Y/n)" } else { "(y/N)" }
    while ($true) {
        $ans = "$(Read-Host "  $Question $hint")".Trim()
        if (-not $ans) { return ($Default -eq "Yes") }
        if ($ans -match '^(y|yes)$') { return $true }
        if ($ans -match '^(n|no)$')  { return $false }
        Warn "Please answer y or n."
    }
}

function New-RandomSecret([int]$Bytes = 24) {
    # RandomNumberGenerator.Fill() is .NET Core / 5+ only -- Windows PowerShell 5.1 runs on
    # .NET Framework 4.x, so use the Create()/GetBytes() API that exists on both.
    $b = New-Object byte[] $Bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($b) } finally { $rng.Dispose() }
    -join ($b | ForEach-Object { $_.ToString("x2") })
}

function Read-DotEnv([string]$Path) {
    $result = @{}
    if (Test-Path $Path) {
        Get-Content $Path | ForEach-Object {
            if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
                $result[$matches[1]] = $matches[2]
            }
        }
    }
    return $result
}

function Update-ProcessPath {
    # A just-installed Python is only on PATH for processes started afterwards -- this console
    # inherited its copy at launch. Re-read both scopes from the registry so the interpreter
    # is usable in this same run instead of needing a reboot or a second pass.
    $parts = @([Environment]::GetEnvironmentVariable("Path", "Machine"),
               [Environment]::GetEnvironmentVariable("Path", "User")) | Where-Object { $_ }
    $env:Path = $parts -join ";"
}

function Resolve-Python {
    # PATH first: the py launcher knows about every installed version, so ask it before python.exe.
    foreach ($cmd in @("py -3", "python")) {
        $exe, $rest = $cmd -split " ", 2
        $found = Get-Command $exe -ErrorAction SilentlyContinue
        if (-not $found) { continue }
        try {
            # Requiring a "Python 3" banner also rejects the Microsoft Store app-execution alias,
            # which sits on PATH as python.exe but only opens the Store when run.
            $v = & $exe $rest --version 2>&1
            if ("$v" -match "Python 3") { return @{ Exe = $found.Source; Args = $rest; Version = "$v".Trim() } }
        } catch { }
    }
    # Nothing usable on PATH. Look where the installers actually put it: a machine-wide install
    # done seconds ago can still be missing from PATH if the registry broadcast hasn't landed,
    # and giving up there would send the operator away for a reboot they don't need.
    foreach ($glob in @("$env:ProgramFiles\Python3*\python.exe",
                        "${env:ProgramFiles(x86)}\Python3*\python.exe",
                        "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
                        "$env:SystemDrive\Python3*\python.exe")) {
        $cand = Get-ChildItem $glob -ErrorAction SilentlyContinue |
                    Sort-Object FullName -Descending | Select-Object -First 1
        if (-not $cand) { continue }
        try {
            $v = & $cand.FullName --version 2>&1
            if ("$v" -match "Python 3") { return @{ Exe = $cand.FullName; Args = $null; Version = "$v".Trim() } }
        } catch { }
    }
    return $null
}

function Install-Python {
    <#
      Install Python 3 unattended and return the resolved interpreter (or $null if every
      route failed). winget first so the machine stays on a serviceable package; python.org's
      own installer covers the boxes where winget is missing (Server SKUs, older Windows 10
      builds) or blocked by policy.
    #>
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Say "Installing Python 3.12 via winget -- this takes a minute, leave it running..."
        # Pin --source winget: without it, winget also probes the msstore source, and a
        # bad msstore cert/network on the machine aborts the whole install even though
        # the winget source works fine.
        winget install --id Python.Python.3.12 --source winget --scope machine `
            --silent --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) { Warn "winget install failed (exit $LASTEXITCODE). Trying python.org instead." }
        Update-ProcessPath
        $py = Resolve-Python
        if ($py) { return $py }
    } else {
        Warn "winget is unavailable on this machine -- using python.org instead."
    }

    Say "Downloading the Python installer from python.org..."
    $pyInstaller = Join-Path $env:TEMP "python-installer.exe"
    try {
        Invoke-WebRequest -Uri $PythonFallback -OutFile $pyInstaller -UseBasicParsing
        Say "Running it silently (no prompts, this takes a minute)..."
        $proc = Start-Process -FilePath $pyInstaller -Wait -PassThru -ArgumentList `
            "/quiet", "InstallAllUsers=1", "PrependPath=1", "Include_test=0"
        if ($proc.ExitCode -ne 0) { Warn "python.org installer exited with code $($proc.ExitCode)." }
    } catch {
        Warn "Direct download failed: $($_.Exception.Message)"
    } finally {
        Remove-Item $pyInstaller -Force -ErrorAction SilentlyContinue
    }

    Update-ProcessPath
    return Resolve-Python
}

function Ensure-Python {
    <#
      Resolve a Python 3 interpreter, offering to install one when the machine hasn't got it.
      A missing prerequisite is a question, not a dead end: both Python paths (hub and
      companion) go through here so neither aborts the run on a bare machine.
    #>
    Step "Checking Python"
    $py = Resolve-Python
    if ($py) { Ok "Found $($py.Version)"; return $py }

    Warn "Python not detected."
    if (-not (Prompt-YesNo "Do you want to install it now?" -Default Yes)) {
        Die "Python 3 is required. Install it from python.org (tick 'Add python.exe to PATH'), then re-run this installer."
    }

    $py = Install-Python
    if (-not $py) {
        Die ("Python still isn't available after the install attempt. Install Python 3 manually from " +
             "python.org (tick 'Add python.exe to PATH'), reboot if the installer asked for one, then re-run this installer.")
    }
    Ok "Installed $($py.Version)"
    return $py
}

function Get-LatestAgentAssetUrl {
    try {
        $rels = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases" -Headers @{ "User-Agent" = "FleetHub-Installer" } -TimeoutSec 15
        $rel = $rels | Where-Object { $_.tag_name -like "agent-v*" } | Select-Object -First 1
        if ($rel) {
            $asset = $rel.assets | Where-Object { $_.name -eq "TempMonitorAgent.exe" } | Select-Object -First 1
            if ($asset) { return $asset.browser_download_url }
        }
    } catch { }
    return $null
}

function Confirm-CompanionChoice {
    <#
      The companion is the pre-agent Python scheduled task: it only runs inside a logged-on
      user's session, and from companion 2.10.0 a machine that installs it self-updates
      straight onto the C# agent anyway. Installing it today is almost always a misclick by
      someone reaching for "the Python one" out of habit, so it costs a deliberate yes.
      Returns $true if they mean it, $false to go back to the menu.
    #>
    Write-Host ""
    Warn "The Companion is no longer supported."
    Say "It runs only while a user is logged on, and from version 2.10.0 it migrates"
    Say "itself to the C# agent on its first self-update -- so this mostly installs a"
    Say "detour to option 1. Pick it only for a machine that genuinely can't run the agent."
    Write-Host ""
    if (Prompt-YesNo "Do you really want to install the unsupported Companion?" -Default No) { return $true }
    Say "Cancelled -- back to the menu."
    return $false
}

function Show-Menu {
    # Loops rather than recursing on a rejected answer: every "ask again" used to be another
    # `return Show-Menu` stack frame, so enough invalid input (or a redirected stdin handing
    # back an endless stream of empty answers) walked the menu into PowerShell's call-depth
    # limit instead of just asking again.
    while ($true) {
        # Written line by line rather than as one here-string so the deprecated entry can be
        # dimmed on its own.
        Write-Host ""
        Write-Host "  FleetHub - Unified Installer"                                                  -ForegroundColor Cyan
        Write-Host "  ================================="                                             -ForegroundColor Cyan
        Write-Host "   1) Install Agent      (C#/.NET Windows Service - recommended)"                 -ForegroundColor Cyan
        Write-Host "   2) Install Companion  (legacy Python scheduled-task agent - UNSUPPORTED)"      -ForegroundColor DarkGray
        Write-Host "   3) Install Hub        (Flask/Socket.IO server - this machine becomes the hub)" -ForegroundColor Cyan
        Write-Host "   4) Uninstall..."                                                               -ForegroundColor Cyan
        Write-Host "   0) Exit"                                                                       -ForegroundColor Cyan
        Write-Host ""
        $choice = Read-Host "Choose an option"
        switch ($choice) {
            "1" { return "Agent" }
            "2" { if (Confirm-CompanionChoice) { return "Companion" } }
            "3" { return "Hub" }
            "4" { return "UninstallMenu" }
            "0" { return "Exit" }
            default { Warn "Invalid choice." }
        }
    }
}

function Show-UninstallMenu {
    # Companion is deliberately NOT dimmed here: it's deprecated to install, but removing one
    # is exactly what an operator should be doing, so that path stays friction-free.
    while ($true) {
        Write-Host "`n  Which component do you want to uninstall?" -ForegroundColor Cyan
        Write-Host "   1) Agent"
        Write-Host "   2) Companion"
        Write-Host "   3) Hub"
        Write-Host "   0) Cancel"
        $choice = Read-Host "Choose an option"
        switch ($choice) {
            "1" { return "Agent" }
            "2" { return "Companion" }
            "3" { return "Hub" }
            "0" { return "Exit" }
            default { Warn "Invalid choice." }
        }
    }
}

# ----------------------------------------------------------------------
# Elevate: every path below needs admin (LHM/service/task registration)
# ----------------------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host "Elevating..." -ForegroundColor Yellow

    $remoteArgList = foreach ($key in $PSBoundParameters.Keys) {
        $val = $PSBoundParameters[$key]
        if ($val -is [switch]) { if ($val.IsPresent) { "-$key" } }
        else { "-$key"; "`"$val`"" }
    }

    # -NoExit keeps the elevated console open after the script finishes (or after you pick
    # Exit) so you can scroll back through the install log instead of the window vanishing.
    if ($PSCommandPath) {
        # Running from a local file -- relaunch that same file, forwarding every
        # bound parameter (not just -Uninstall) so -Component/-AgentUrl/etc. survive.
        $argList = @("-NoExit", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"") + $remoteArgList
        Start-Process powershell -Verb RunAs -ArgumentList $argList
    } else {
        # Running via `irm | iex` -- no script file to relaunch, so re-fetch
        # and re-invoke as a scriptblock (preserves param binding) in the
        # elevated process.
        $cmd = "& ([scriptblock]::Create((irm '$InstallerUrl'))) $($remoteArgList -join ' ')"
        Start-Process powershell -Verb RunAs -ArgumentList @("-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $cmd)
    }
    exit
}

# ========================================================================
# Companion (legacy Python scheduled-task agent)
# ========================================================================
function Uninstall-Companion {
    Step "Uninstalling FleetHub Companion"

    foreach ($t in @($TaskCompanion, $TaskLhm)) {
        if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $t -Confirm:$false
            Ok "Removed task: $t"
        }
    }

    Get-Process -Name "LibreHardwareMonitor" -ErrorAction SilentlyContinue | Stop-Process -Force
    Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
        Where-Object { $_.CommandLine -like "*companion.py*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Ok "Stopped running processes"

    if (Test-Path $InstallDir) {
        Remove-Item $InstallDir -Recurse -Force
        Ok "Deleted $InstallDir"
    }

    Write-Host "`nDone. Python itself was left alone.`n" -ForegroundColor Green
}

function Install-Companion {
    Write-Host @"

  FleetHub - Companion Agent Installer (UNSUPPORTED)
  Machine: $env:COMPUTERNAME
  Target : $InstallDir

"@ -ForegroundColor Cyan

    # The menu already made an interactive operator confirm this. Repeat it as a plain
    # warning -- not a prompt -- so `-Component Companion` still runs unattended from a
    # script while the log says plainly what got installed.
    Warn "The Companion is legacy and no longer supported; the Agent replaces it."

    # ------------------------------------------------------------------
    # 1. Python
    # ------------------------------------------------------------------
    $py = Ensure-Python

    # Resolve the real interpreter path (so scheduled tasks don't depend on PATH)
    $pythonExe = & $py.Exe $py.Args -c "import sys; print(sys.executable)"
    $pythonwExe = Join-Path (Split-Path $pythonExe) "pythonw.exe"   # windowless, no console popup
    if (-not (Test-Path $pythonwExe)) { $pythonwExe = $pythonExe }
    Ok "Interpreter: $pythonExe"

    Step "Installing Python packages"
    & $pythonExe -m pip install --upgrade pip --quiet
    # cryptography is needed so the companion can verify signed self-updates (Ed25519).
    & $pythonExe -m pip install requests cryptography --quiet
    if ($LASTEXITCODE -ne 0) { Die "pip install failed." }
    Ok "requests + cryptography installed"

    # ------------------------------------------------------------------
    # 2. Files
    # ------------------------------------------------------------------
    Step "Setting up $InstallDir"
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    New-Item -ItemType Directory -Force -Path $LhmDir     | Out-Null
    Ok "Directories ready"

    # ------------------------------------------------------------------
    # 3. LibreHardwareMonitor
    # ------------------------------------------------------------------
    Step "Installing LibreHardwareMonitor"

    $lhmExe = Join-Path $LhmDir "LibreHardwareMonitor.exe"

    if (Test-Path $lhmExe) {
        Ok "Already present, skipping download"
    } else {
        $zipUrl = $LhmFallback
        try {
            $rel = Invoke-RestMethod -Uri $LhmApi -Headers @{ "User-Agent" = "FleetHub-Installer" } -TimeoutSec 15
            $asset = $rel.assets | Where-Object { $_.name -like "*net472*.zip" } | Select-Object -First 1
            if ($asset) {
                $zipUrl = $asset.browser_download_url
                Say "Latest release: $($rel.tag_name)"
            }
        } catch {
            Warn "GitHub API unreachable (rate limit?). Using pinned v0.9.6."
        }

        $zipPath = Join-Path $env:TEMP "LibreHardwareMonitor.zip"
        Say "Downloading $zipUrl"
        Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing
        Expand-Archive -Path $zipPath -DestinationPath $LhmDir -Force
        Remove-Item $zipPath -Force

        # Some releases nest everything one folder deep
        if (-not (Test-Path $lhmExe)) {
            $found = Get-ChildItem $LhmDir -Recurse -Filter "LibreHardwareMonitor.exe" | Select-Object -First 1
            if ($found) {
                Get-ChildItem $found.DirectoryName | Move-Item -Destination $LhmDir -Force
            }
        }
        if (-not (Test-Path $lhmExe)) { Die "LibreHardwareMonitor.exe not found after extraction." }

        Unblock-File -Path (Join-Path $LhmDir "*") -ErrorAction SilentlyContinue
        Ok "Extracted to $LhmDir"
    }

    # ------------------------------------------------------------------
    # 4. PawnIO -- kernel driver LHM needs for sensor access (replaces WinRing0)
    # ------------------------------------------------------------------
    Step "Installing PawnIO driver"

    if (Get-Service -Name "PawnIO" -ErrorAction SilentlyContinue) {
        Ok "Already installed, skipping"
    } else {
        $pawnioPath = Join-Path $env:TEMP "PawnIO_setup.exe"
        Say "Downloading $PawnIoUrl"
        Invoke-WebRequest -Uri $PawnIoUrl -OutFile $pawnioPath -UseBasicParsing
        Unblock-File -Path $pawnioPath -ErrorAction SilentlyContinue

        $proc = Start-Process -FilePath $pawnioPath -ArgumentList "-install", "-silent" -Wait -PassThru -NoNewWindow
        Remove-Item $pawnioPath -Force -ErrorAction SilentlyContinue

        if ($proc.ExitCode -ne 0) {
            Warn "PawnIO installer exited with code $($proc.ExitCode). Sensors may not be readable."
        } else {
            Ok "PawnIO installed"
        }
    }

    # ------------------------------------------------------------------
    # 5. LHM config -- web server ON, start minimized, live in the tray
    #    LHM reads <exe name>.config from its own folder (PersistentSettings)
    # ------------------------------------------------------------------
    Step "Configuring LibreHardwareMonitor web server (port $Port)"

    $lhmConfig = Join-Path $LhmDir "LibreHardwareMonitor.config"
    @"
<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <appSettings>
    <add key="runWebServerMenuItem" value="true" />
    <add key="listenerPort" value="$Port" />
    <add key="authenticationEnabled" value="false" />
    <add key="startMinMenuItem" value="true" />
    <add key="minTrayMenuItem" value="true" />
    <add key="minCloseMenuItem" value="true" />
    <add key="cpuMenuItem" value="true" />
    <add key="mainForm.Location.X" value="100" />
    <add key="mainForm.Location.Y" value="100" />
  </appSettings>
</configuration>
"@ | Set-Content -Path $lhmConfig -Encoding UTF8

    Ok "Wrote $lhmConfig"

    # ------------------------------------------------------------------
    # 6. companion.py
    # ------------------------------------------------------------------
    Step "Downloading companion.py"
    $companionPath = Join-Path $InstallDir "companion.py"
    Invoke-WebRequest -Uri $CompanionUrl -OutFile $companionPath -UseBasicParsing
    $ver = (Select-String -Path $companionPath -Pattern '^VERSION\s*=\s*"([\d.]+)"').Matches.Groups[1].Value
    Ok "companion.py v$ver -> $companionPath"

    # ------------------------------------------------------------------
    # 7. Scheduled tasks (RunLevel Highest = admin without a UAC prompt every logon)
    # ------------------------------------------------------------------
    Step "Registering scheduled tasks"

    # Pass the SID directly rather than a "DOMAIN\User" string -- some machines
    # (seen on ones with a leftover/corrupted HomeGroup profile) fail the internal
    # name-to-SID lookup Register-ScheduledTask does for a name string, with
    # "No mapping between account names and security IDs was done" (0x80070534).
    # The SID is already resolved, so it skips that lookup entirely.
    $currentUserSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $principal = New-ScheduledTaskPrincipal -UserId $currentUserSid `
                                            -LogonType Interactive -RunLevel Highest
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                                              -DontStopIfGoingOnBatteries `
                                              -StartWhenAvailable `
                                              -ExecutionTimeLimit ([TimeSpan]::Zero) `
                                              -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

    # LHM first
    Register-ScheduledTask -TaskName $TaskLhm -Force `
        -Action    (New-ScheduledTaskAction -Execute $lhmExe -WorkingDirectory $LhmDir) `
        -Trigger   (New-ScheduledTaskTrigger -AtLogOn) `
        -Principal $principal -Settings $settings `
        -Description "Hardware sensor daemon for FleetHub. Serves JSON on localhost:$Port." | Out-Null
    Ok "Task: $TaskLhm"

    # Companion 30s later, so LHM's web server is up. Also repeats every 2 minutes
    # (indefinitely) as a self-heal mechanism: Task Scheduler puts the task in a job
    # object that kills any child we spawn when we exit, so neither a "detached"
    # relaunch helper nor the -RestartCount/-RestartInterval settings above reliably
    # bring the task back after companion.py swaps itself during a self-update (verified
    # empirically -- RestartCount/RestartInterval do not fire on a plain nonzero exit,
    # they're for a narrower "task failed to launch" class). The repetition trigger is
    # the one relaunch path that's actually reliable, since it's driven by the Task
    # Scheduler service itself, not a descendant of our job. -MultipleInstances
    # IgnoreNew (the default) means a tick while we're already running is a no-op; it
    # only actually starts a new instance once we've exited.
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $trigger.Delay = "PT30S"
    $trigger.Repetition.Interval = "PT2M"
    # Duration deliberately left empty: Task Scheduler rejects year/month designators
    # there (e.g. "P10Y" errors as "incorrectly formatted or out of range"), and an
    # empty Duration already means "repeat every Interval indefinitely".
    Register-ScheduledTask -TaskName $TaskCompanion -Force `
        -Action    (New-ScheduledTaskAction -Execute $pythonwExe -Argument "`"$companionPath`"" -WorkingDirectory $InstallDir) `
        -Trigger   $trigger `
        -Principal $principal -Settings $settings `
        -Description "Reports CPU temperature to the FleetHub hub." | Out-Null
    Ok "Task: $TaskCompanion (30s delay, repeats every 2min as a self-heal restart path)"

    # ------------------------------------------------------------------
    # 8. Start and verify
    # ------------------------------------------------------------------
    Step "Starting services"

    if (-not (Get-Process -Name "LibreHardwareMonitor" -ErrorAction SilentlyContinue)) {
        Start-ScheduledTask -TaskName $TaskLhm
    }

    Say "Waiting for the sensor web server..."
    $live = $false
    foreach ($i in 1..20) {
        Start-Sleep -Seconds 1
        try {
            $r = Invoke-RestMethod -Uri "http://localhost:$Port/data.json" -TimeoutSec 2
            $live = $true
            break
        } catch { }
    }

    if (-not $live) {
        Warn "No response on port $Port after 20s."
        Warn "Open $lhmExe manually and check Options > Run web server."
    } else {
        Ok "Web server responding on http://localhost:$Port/data.json"

        # Show what the companion will actually pick up
        function Find-Temps($node, $inCpu) {
            if ("$($node.HardwareId)" -like "*cpu*") { $inCpu = $true }
            if ($inCpu -and $node.Type -eq "Temperature") {
                $script:temps += [pscustomobject]@{ Sensor = $node.Text; Value = $node.Value }
            }
            foreach ($c in $node.Children) { Find-Temps $c $inCpu }
        }
        $script:temps = @()
        Find-Temps $r $false
        if ($script:temps.Count -gt 0) {
            Say "CPU sensors detected:"
            $script:temps | ForEach-Object { Say "   $($_.Sensor): $($_.Value)" }
        } else {
            Warn "No CPU temperature sensors visible. LHM may need a reboot to load its kernel driver."
        }

        Start-ScheduledTask -TaskName $TaskCompanion
        Ok "Companion started"
    }

    Write-Host @"

  Done.

  Machine name reported to the hub: $env:COMPUTERNAME
  Sensors : http://localhost:$Port/data.json
  Files   : $InstallDir

  companion.py updates itself from GitHub on every start, and weekly if left running.
  Uninstall: powershell -ExecutionPolicy Bypass -File install.ps1 -Component Companion -Uninstall

"@ -ForegroundColor Green
}

# ========================================================================
# Agent (C#/.NET Windows Service)
# ========================================================================
function Uninstall-Agent {
    Step "Uninstalling Agent"
    $localInstaller = $null
    if ($PSScriptRoot) {
        $p = Join-Path $PSScriptRoot "agent\install\agent-install.ps1"
        if (Test-Path $p) { $localInstaller = $p }
    }

    if ($localInstaller) {
        & $localInstaller -Uninstall
    } else {
        $tmp = Join-Path $env:TEMP "temp-monitor-agent-install.ps1"
        Invoke-WebRequest -Uri $AgentInstallUrl -OutFile $tmp -UseBasicParsing
        & $tmp -Uninstall
    }
}

function Install-Agent {
    Write-Host @"

  FleetHub - Agent Installer (C#/.NET Windows Service)

"@ -ForegroundColor Cyan

    $envDefaults = @{}
    if ($PSScriptRoot) { $envDefaults = Read-DotEnv (Join-Path $PSScriptRoot ".env") }

    $resolvedExe = $AgentExe
    $resolvedUrl = $AgentUrl

    if (-not $resolvedExe -and -not $resolvedUrl) {
        $localExeDefault = $null
        if ($PSScriptRoot) {
            $p = Join-Path $PSScriptRoot "agent\dist\TempMonitorAgent.exe"
            if (Test-Path $p) { $localExeDefault = $p }
        }

        if ($localExeDefault) {
            if (Prompt-YesNo "Found a built exe at $localExeDefault. Use it?" -Default Yes) {
                $resolvedExe = $localExeDefault
            }
        }

        if (-not $resolvedExe) {
            Say "Looking up the latest agent release on GitHub..."
            $latest = Get-LatestAgentAssetUrl
            $resolvedUrl = Prompt-Value "Agent download URL" $latest
            if (-not $resolvedUrl) { Die "No agent URL available. Re-run with -AgentUrl <url> or -AgentExe <path>." }
        }
    }

    $hubUrlDefault = $envDefaults["HUB_URL"]
    if (-not $hubUrlDefault) { $hubUrlDefault = "https://temp.arkeanos.net" }
    if ($HubUrl) { $hubUrlDefault = $HubUrl }
    $resolvedHubUrl = Prompt-Value "Hub URL" $hubUrlDefault

    $secretDefault = $envDefaults["AGENT_ENROLLMENT_SECRET"]
    if ($EnrollmentSecret) { $secretDefault = $EnrollmentSecret }
    $resolvedSecret = Prompt-Value "Agent enrollment secret (blank = telemetry-only until enrolled later)" $secretDefault -Secret

    $agentArgs = @{}
    if ($resolvedExe) { $agentArgs.AgentExe = $resolvedExe } else { $agentArgs.AgentUrl = $resolvedUrl }
    if ($resolvedSecret) { $agentArgs.EnrollmentSecret = $resolvedSecret }
    if ($resolvedHubUrl) { $agentArgs.HubUrl = $resolvedHubUrl }
    # Keep hub and agent under one root. Passed explicitly so a downloaded agent-install.ps1
    # (whose own default could be an older release's) still lands where this installer says.
    $agentArgs.InstallDir = $AgentInstallDir

    $localInstaller = $null
    if ($PSScriptRoot) {
        $p = Join-Path $PSScriptRoot "agent\install\agent-install.ps1"
        if (Test-Path $p) { $localInstaller = $p }
    }

    if ($localInstaller) {
        & $localInstaller @agentArgs
    } else {
        $tmp = Join-Path $env:TEMP "temp-monitor-agent-install.ps1"
        Invoke-WebRequest -Uri $AgentInstallUrl -OutFile $tmp -UseBasicParsing
        & $tmp @agentArgs
    }
}

# ========================================================================
# Hub (Flask + Socket.IO)
# ========================================================================
# Where the hub lives: -HubInstallDir if given, else the default under Program Files, else --
# for uninstall -- an existing clone next to install.ps1. Kept in one place so install and
# uninstall agree on the location.
function Resolve-HubDir {
    if ($HubInstallDir) { return $HubInstallDir.TrimEnd('\') }
    if (Test-Path (Join-Path $HubInstallDefault "app.py")) { return $HubInstallDefault }
    # Pre-rename layout, so `-Uninstall` still finds a hub installed before the rename.
    if (Test-Path (Join-Path $LegacyHubDir "app.py")) { return $LegacyHubDir }
    if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "app.py"))) { return $PSScriptRoot }
    return $HubInstallDefault
}

function Remove-HubService([string]$hubDir) {
    # Both ids, because a box installed before the rename registered TempMonitorHub and
    # would otherwise be left running alongside the new service, both fighting for the port.
    $removed = $false
    foreach ($id in @($HubServiceId, $LegacyHubServiceId)) {
        if (-not (Get-Service -Name $id -ErrorAction SilentlyContinue)) { continue }
        $wrapperExe = Join-Path $hubDir "$id.exe"
        if (Test-Path $wrapperExe) {
            & $wrapperExe stop      2>&1 | Out-Null
            & $wrapperExe uninstall 2>&1 | Out-Null
        } else {
            # Wrapper exe is gone but the service registration lingers -- tear it down directly.
            & sc.exe stop   $id | Out-Null
            & sc.exe delete $id | Out-Null
        }
        $removed = $true
    }
    if ($removed) { Start-Sleep -Seconds 2 }
    return $removed
}

function Uninstall-Hub {
    Step "Uninstalling Hub"
    $hubDir = Resolve-HubDir
    if (Remove-HubService $hubDir) {
        Ok "Removed service: $HubServiceName"
    } else {
        Say "Service not present."
    }
    # Also clear the legacy scheduled task, in case this box predates the service.
    if (Get-ScheduledTask -TaskName $TaskHub -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskHub -Confirm:$false
        Ok "Removed legacy scheduled task: $TaskHub"
    }
    Warn "Left the hub files at $hubDir (including .env and logs) in place -- they may still hold data you want."
    Write-Host "`nDone.`n" -ForegroundColor Green
}

function Copy-HubRuntimeFiles {
    <#
      Copy just the hub's runtime file set from an extracted repo tree into $Dest.
      Everything outside $HubRuntimeFiles/$HubRuntimeDirs -- the agent tree, tests, docs
      -- is deliberately left behind.

      Directories are mirrored rather than merged: a template or static asset deleted
      upstream must disappear here too, otherwise a stale .html lingers forever. The
      operator's own files (.env, logs/, the WinSW wrapper) live outside these dirs and
      are never touched.
    #>
    param([string]$Source, [string]$Dest)

    foreach ($f in $HubRuntimeFiles) {
        $src = Join-Path $Source $f
        if (-not (Test-Path $src)) { Die "Source archive is missing $f -- refusing to install a partial hub." }
        Copy-Item $src (Join-Path $Dest $f) -Force
    }
    foreach ($d in $HubRuntimeDirs) {
        $src = Join-Path $Source $d
        if (-not (Test-Path $src)) { Die "Source archive is missing $d\ -- refusing to install a partial hub." }
        $target = Join-Path $Dest $d
        if (Test-Path $target) { Remove-Item $target -Recurse -Force }
        Copy-Item $src $target -Recurse -Force
    }
}

function Get-HubFiles {
    <#
      Download main as a zip and lay down only the hub runtime files at $Dest.
      Replaces the previous `git clone` of the whole repo: no Git dependency, and
      ~2 MB on disk instead of ~85 MB.
    #>
    param([string]$Dest)

    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("fleethub-" + [guid]::NewGuid().ToString("n"))
    $zip = "$tmp.zip"
    try {
        Say "Downloading hub files from $RepoZipUrl ..."
        try { Invoke-WebRequest -Uri $RepoZipUrl -OutFile $zip -UseBasicParsing }
        catch { Die "Could not download the hub source archive -- $($_.Exception.Message)" }

        New-Item -ItemType Directory -Force -Path $tmp | Out-Null
        try { Expand-Archive -Path $zip -DestinationPath $tmp -Force }
        catch { Die "Could not expand the hub source archive -- $($_.Exception.Message)" }

        # codeload wraps everything in a single <repo>-<branch>/ folder.
        $root = Get-ChildItem -Directory $tmp | Select-Object -First 1
        if (-not $root) { Die "Source archive looked empty." }

        if (-not (Test-Path $Dest)) { New-Item -ItemType Directory -Force -Path $Dest | Out-Null }
        Copy-HubRuntimeFiles -Source $root.FullName -Dest $Dest
        Ok "Installed hub runtime files to $Dest"
    }
    finally {
        Remove-Item $zip -Force -ErrorAction SilentlyContinue
        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Move-LegacyHubInstall {
    <#
      Pre-rename hubs live at C:\Program Files\TempMonitor\Hub under the TempMonitorHub
      service. Carry the operator's data (.env and logs/, including the telemetry DB)
      over to the new root rather than silently standing up an empty second install.
    #>
    param([string]$NewDir)

    if (-not (Test-Path (Join-Path $LegacyHubDir "app.py"))) { return }
    if ($NewDir -eq $LegacyHubDir) { return }

    Warn "Found an existing hub at $LegacyHubDir (pre-FleetHub layout)."
    if (-not (Prompt-YesNo "Move its config and data to $NewDir ?" -Default Yes)) {
        Say "Leaving it alone. Note both hubs would bind port $HubPort -- only one can run."
        return
    }

    if (Get-Service -Name $LegacyHubServiceId -ErrorAction SilentlyContinue) {
        Say "Stopping the old $LegacyHubServiceId service..."
        Stop-Service -Name $LegacyHubServiceId -Force -ErrorAction SilentlyContinue
        $oldWrapper = Join-Path $LegacyHubDir "$LegacyHubServiceId.exe"
        if (Test-Path $oldWrapper) { & $oldWrapper uninstall | Out-Null }
        Ok "Removed the old service"
    }

    if (-not (Test-Path $NewDir)) { New-Item -ItemType Directory -Force -Path $NewDir | Out-Null }
    foreach ($item in @(".env", "logs")) {
        $src = Join-Path $LegacyHubDir $item
        if (Test-Path $src) {
            Move-Item $src (Join-Path $NewDir $item) -Force
            Ok "Moved $item"
        }
    }
    Warn "Left the old tree at $LegacyHubDir -- delete it once you've confirmed the new hub is healthy."
}

function Install-Hub {
    # Resolve where the hub will live, then lay down just the runtime files. No git
    # clone and no Git dependency: self-update now pulls the same zip (see app.py).
    $default = if ($HubInstallDir) { $HubInstallDir } else { $HubInstallDefault }
    $hubDir  = (Prompt-Value "Hub install location" $default).TrimEnd('\')

    Write-Host @"

  FleetHub - Hub Installer
  Installs app.py (Flask + Socket.IO) as the '$HubServiceName' Windows Service.
  Location: $hubDir

"@ -ForegroundColor Cyan

    # Before anything touches disk: an operator who declines the Python install shouldn't be
    # left with a half-laid-down hub directory to clean up.
    $py = Ensure-Python
    $pythonExe = & $py.Exe $py.Args -c "import sys; print(sys.executable)"
    Ok "Interpreter: $pythonExe"

    Step "Preparing hub files at $hubDir"
    Move-LegacyHubInstall -NewDir $hubDir

    if (Test-Path (Join-Path $hubDir ".git")) {
        # A developer clone (this is also the folder the installer is often launched from).
        # Overwriting tracked files here would stomp uncommitted work, and the hub's
        # self-updater keeps using git when it sees .git, so leave the tree as-is.
        Ok "Using the existing git clone at $hubDir (files left untouched)"
    } else {
        Get-HubFiles -Dest $hubDir
    }

    $envPath  = Join-Path $hubDir ".env"
    $existing = Read-DotEnv $envPath

    Step "Installing Python packages"
    & $pythonExe -m pip install --upgrade pip --quiet
    & $pythonExe -m pip install -r (Join-Path $hubDir "requirements.txt") --quiet
    if ($LASTEXITCODE -ne 0) { Die "pip install failed." }
    Ok "Dependencies installed"

    Step "Configuring .env"
    Say "Press Enter to keep the value shown in [brackets]."
    $googleId      = Prompt-Value "Google OAuth client ID" $existing["GOOGLE_CLIENT_ID"] -Required
    $googleSecret  = Prompt-Value "Google OAuth client secret" $existing["GOOGLE_CLIENT_SECRET"] -Secret -Required

    $flaskSecretDefault = $existing["FLASK_SECRET_KEY"]
    if (-not $flaskSecretDefault) { $flaskSecretDefault = New-RandomSecret }
    $flaskSecret   = Prompt-Value "Flask session secret key" $flaskSecretDefault -Secret -Required

    $allowedEmails = Prompt-Value "Allowed Google emails (comma-separated)" $existing["ALLOWED_EMAILS"] `
                        -Required -Validate { param($v) $v -match '@' } `
                        -ValidateHint "Enter at least one email address (must contain '@')."

    $hubUrlDefault = $existing["HUB_URL"]
    if (-not $hubUrlDefault) { $hubUrlDefault = "https://temp.arkeanos.net" }
    $hubUrlValue   = Prompt-Value "Public hub URL" $hubUrlDefault `
                        -Required -Validate { param($v) $v -match '^https?://' } `
                        -ValidateHint "Enter a full URL starting with http:// or https://."

    Say ""
    Say "Fleet command channel (optional -- leave blank to keep telemetry-only):"
    $enrollSecretDefault = $existing["AGENT_ENROLLMENT_SECRET"]
    if (-not $enrollSecretDefault) {
        if (Prompt-YesNo "No AGENT_ENROLLMENT_SECRET set. Auto-generate one?" -Default Yes) {
            $enrollSecretDefault = New-RandomSecret
        }
    }
    $enrollSecret  = Prompt-Value "  Agent enrollment secret" $enrollSecretDefault -Secret

    Say ""
    # Self-update works in both layouts: a files-only install pulls the branch archive
    # and replaces the runtime file set; a clone still uses git. See perform_hub_update().
    $autoUpdateDefault = $existing["HUB_AUTO_UPDATE"]
    if (-not $autoUpdateDefault) {
        if (Prompt-YesNo "Enable hub self-update from main (downloads and replaces hub files)?" -Default No) {
            $autoUpdateDefault = "1"
        }
    }

    $lines = @(
        "GOOGLE_CLIENT_ID=$googleId"
        "GOOGLE_CLIENT_SECRET=$googleSecret"
        "FLASK_SECRET_KEY=$flaskSecret"
        "ALLOWED_EMAILS=$allowedEmails"
        "HUB_URL=$hubUrlValue"
    )
    if ($enrollSecret)      { $lines += "AGENT_ENROLLMENT_SECRET=$enrollSecret" }
    if ($autoUpdateDefault) { $lines += "HUB_AUTO_UPDATE=$autoUpdateDefault" }
    # Write WITHOUT a BOM: PowerShell 5.1's `Set-Content -Encoding UTF8` prepends a UTF-8 BOM,
    # which python-dotenv folds into the first key (﻿GOOGLE_CLIENT_ID) so the hub reads its
    # config as unset and crash-loops. UTF8Encoding($false) = no BOM.
    [System.IO.File]::WriteAllLines($envPath, [string[]]$lines, (New-Object System.Text.UTF8Encoding($false)))
    Ok "Wrote $envPath"

    Step "Installing the $HubServiceName service"
    # Serve via waitress; prefer its console script, fall back to `python -m waitress`.
    $scriptsDir  = Join-Path (Split-Path $pythonExe -Parent) "Scripts"
    $waitressExe = Join-Path $scriptsDir "waitress-serve.exe"
    if (Test-Path $waitressExe) {
        $hubExec = $waitressExe
        $hubArgs = "--host=0.0.0.0 --port=$HubPort wsgi:application"
    } else {
        $hubExec = $pythonExe
        $hubArgs = "-m waitress --host=0.0.0.0 --port=$HubPort wsgi:application"
    }

    # Retire the legacy scheduled task if this box predates the service, so the two don't
    # both bind the port.
    if (Get-ScheduledTask -TaskName $TaskHub -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskHub -Confirm:$false
        Ok "Removed legacy scheduled task: $TaskHub"
    }
    # Reinstall cleanly if a prior service is already registered.
    if (Remove-HubService $hubDir) { Say "Reconfiguring existing service." }

    $wrapperExe = Join-Path $hubDir "$HubServiceId.exe"
    $wrapperXml = Join-Path $hubDir "$HubServiceId.xml"
    if (-not (Test-Path $wrapperExe)) {
        Say "Downloading WinSW service wrapper..."
        try { Invoke-WebRequest -Uri $WinSwUrl -OutFile $wrapperExe -UseBasicParsing }
        catch { Die "Could not download the WinSW service wrapper from $WinSwUrl -- $($_.Exception.Message)" }
        Ok "Wrapper: $wrapperExe"
    }

    # WinSW runs as LocalSystem by default (matches the old SYSTEM task). The service inherits
    # config from .env because waitress runs with <workingdirectory>=$hubDir and app.py calls
    # load_dotenv() from cwd. onfailure=restart is also what the hub self-update relies on:
    # restart_hub() exits non-zero, WinSW relaunches within ~5s.
    $xml = @"
<service>
  <id>$HubServiceId</id>
  <name>$HubServiceName</name>
  <description>FleetHub hub (Flask/Socket.IO via waitress).</description>
  <executable>$([System.Security.SecurityElement]::Escape($hubExec))</executable>
  <arguments>$([System.Security.SecurityElement]::Escape($hubArgs))</arguments>
  <workingdirectory>$([System.Security.SecurityElement]::Escape($hubDir))</workingdirectory>
  <startmode>Automatic</startmode>
  <onfailure action="restart" delay="5 sec"/>
  <resetfailure>1 hour</resetfailure>
  <log mode="roll-by-size">
    <sizeThreshold>10240</sizeThreshold>
    <keepFiles>5</keepFiles>
  </log>
</service>
"@
    [System.IO.File]::WriteAllText($wrapperXml, $xml, (New-Object System.Text.UTF8Encoding($false)))
    Ok "Wrote $wrapperXml"

    & $wrapperExe install
    if ($LASTEXITCODE -ne 0) { Die "Service install failed (WinSW exit $LASTEXITCODE)." }
    Start-Service -Name $HubServiceId
    Ok "Service '$HubServiceName' installed and started (LocalSystem, Automatic)"

    Step "Starting hub"
    Say "Waiting for the hub to respond..."
    $live = $false
    foreach ($i in 1..20) {
        Start-Sleep -Seconds 1
        try {
            Invoke-WebRequest -Uri "http://localhost:$HubPort/" -UseBasicParsing -TimeoutSec 2 | Out-Null
            $live = $true
            break
        } catch {
            if ($_.Exception.Response) { $live = $true; break }
        }
    }
    if ($live) { Ok "Hub responding on http://localhost:$HubPort/" }
    else { Warn "No response yet on port $HubPort -- check 'Get-Service $HubServiceId' and $hubDir\$HubServiceId.wrapper.log." }

    Write-Host @"

  Done.

  Hub URL (local) : http://localhost:$HubPort/
  Hub URL (public): $hubUrlValue
  Location        : $hubDir
  Config          : $envPath
  Service         : $HubServiceName  (Get-Service $HubServiceId)

  Uninstall: powershell -ExecutionPolicy Bypass -File install.ps1 -Component Hub -Uninstall

"@ -ForegroundColor Green
}

# ----------------------------------------------------------------------
# Resolve which component to act on, then dispatch
# ----------------------------------------------------------------------
if (-not $Component) {
    if ($Uninstall) {
        # Back-compat: bare -Uninstall (no -Component) matches the documented
        # legacy behavior of uninstalling the companion.
        $Component = "Companion"
    } else {
        $Component = Show-Menu
        if ($Component -eq "UninstallMenu") {
            $Component = Show-UninstallMenu
            $Uninstall = $true
        }
    }
}

# Exit chosen from a menu: end the script gracefully but DON'T `exit` -- that would kill the
# (elevated) console the user wants to keep open to read the log. `return` at script scope
# stops here and, with the relaunch's -NoExit, leaves them at a live prompt.
if ($Component -eq "Exit" -or -not $Component) {
    Write-Host "`n  Exiting installer. This window stays open so you can review the log above." -ForegroundColor Cyan
    return
}

try {
    switch ($Component) {
        "Agent"     { if ($Uninstall) { Uninstall-Agent }     else { Install-Agent } }
        "Companion" { if ($Uninstall) { Uninstall-Companion } else { Install-Companion } }
        "Hub"       { if ($Uninstall) { Uninstall-Hub }       else { Install-Hub } }
    }
} catch {
    # A Die (or any terminating error, since $ErrorActionPreference = 'Stop') lands here.
    # Die already printed a friendly [xx] line; only surface the raw message for other errors.
    # Don't `exit` -- keep the console open so the log above stays readable.
    if ("$($_.Exception.Message)" -notlike "TempMonitorInstaller:*") {
        Write-Host "  [xx] $($_.Exception.Message)" -ForegroundColor Red
    }
    Write-Host "`n  Install did not complete -- review the log above. This window stays open." -ForegroundColor Yellow
    return
}
