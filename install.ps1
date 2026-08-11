<#
    FleetHub - Unified Installer
    https://github.com/aw08-2004/Temp_Monitor

    Interactive menu over the three install paths:
      1) Agent      - C#/.NET Windows Service (the only telemetry client)
      2) Hub        - Flask/Socket.IO server (this machine becomes the fleet hub)
      3) Turn       - coturn WebRTC relay only, in a dedicated Ubuntu WSL2 distro.
                      Installing the hub can set this up too; this option exists for
                      when the relay belongs on a different machine from the hub
                      (typically one with a better public address).

    The legacy Python companion (companion.py) has been removed from the repo and can
    no longer be installed. Uninstalling one is still supported, for machines that were
    never migrated -- see the Uninstall menu, or -Component Companion -Uninstall.

    Prompts for whatever each path needs (enrollment secret, hub URL, OAuth
    creds, etc.), defaulting to values already present in a local .env when run
    from a clone. Non-interactive use is still supported by passing -Component
    plus the relevant parameters up front.

    Usage (elevated PowerShell):
        powershell -ExecutionPolicy Bypass -File install.ps1
        powershell -ExecutionPolicy Bypass -File install.ps1 -Component Agent -HubUrl <hub-url> -EnrollmentSecret <secret>
        powershell -ExecutionPolicy Bypass -File install.ps1 -Component Agent -HubUrl <hub-url> -EnrollmentSecret <secret> `
            -InstallDir "D:\Apps\FleetHub\Agent" -AddDefenderExclusion     # fully unattended
        powershell -ExecutionPolicy Bypass -File install.ps1 -Component Hub
        powershell -ExecutionPolicy Bypass -File install.ps1 -Component Turn -TurnHost <public-ip> -TurnSecret <secret>
        powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall                    # legacy companion (back-compat)
        powershell -ExecutionPolicy Bypass -File install.ps1 -Component Companion -Uninstall
        powershell -ExecutionPolicy Bypass -File install.ps1 -Component Agent -Uninstall
        powershell -ExecutionPolicy Bypass -File install.ps1 -Component Hub -Uninstall
        powershell -ExecutionPolicy Bypass -File install.ps1 -Component Turn -Uninstall

    From the web:
        irm https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/install.ps1 | iex
#>

param(
    # "Companion" is accepted for -Uninstall only: the legacy Python agent can no longer be
    # installed, but machines that still run one need a way to be cleaned up.
    [ValidateSet("Agent", "Companion", "Hub", "Turn")]
    [string]$Component,
    [switch]$Uninstall,

    # Where the selected component is installed. Left unbound, each component uses its own
    # default (Agent -> C:\Program Files\FleetHub\Agent, Hub -> ...\FleetHub\Hub, legacy
    # Companion -> C:\Program Files\TempMonitor). Passing it explicitly overrides that default
    # for whichever component is being acted on -- including the path offered for the Defender
    # exclusion, and the directory a Companion uninstall deletes.
    [string]$InstallDir = "C:\Program Files\TempMonitor",

    # --- Agent ---
    [string]$AgentUrl,                  # download URL for the agent exe (a release asset)
    [string]$AgentExe,                  # OR a local path to an already-built exe
    [string]$EnrollmentSecret,
    [string]$HubUrl,                    # the hub the agent reports to, e.g. https://hub.example.com
    # Pre-answer the Defender exclusion question so an unattended run never blocks on it.
    [switch]$AddDefenderExclusion,
    [switch]$SkipDefenderExclusion,

    # --- Hub ---
    [int]$HubPort = 3001,
    [string]$HubInstallDir,

    # --- TURN relay (coturn in a dedicated WSL2 distro) ---
    # Used by -Component Turn, and by -Component Hub when the hub also hosts the relay.
    # Supplying -TurnHost implies "yes, configure TURN" and skips that prompt, the same way
    # -EnrollmentSecret pre-answers the enrollment question.
    [switch]$SkipTurn,
    [string]$TurnHost,
    [string]$TurnSecret,
    [string]$TurnDistro = "FleetHubTurn",
    [string]$TurnWslLocation,
    [int]$TurnPort = 3478,
    [int]$TurnMinPort = 49160,
    [int]$TurnMaxPort = 49200,
    # The LAN-only second listener: same secret and realm, bound to the LAN address, and
    # advertising relay candidates WITHOUT the public-IP rewrite so two peers inside the relay's
    # own network can reach each other. Its relay range must not overlap the public one --
    # see New-TurnServerConfLines. 0 disables it.
    [int]$TurnLanPort = 3479,
    [int]$TurnLanMinPort = 49210,
    [int]$TurnLanMaxPort = 49250,
    [string]$TurnRealm = "fleethub",
    [int]$TurnMinFreeGB = 10,
    # Pre-answers the "this restarts every WSL distro and Docker container" confirmation.
    [switch]$AcceptWslNetworkChange,
    [switch]$SkipTurnFirewall
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
# Invoke-WebRequest renders a progress bar on PowerShell 5.1 that costs more time than the
# transfer itself on the bigger downloads here (the ~25 MB Python installer, the agent exe).
# Silencing it keeps the run moving and the log readable.
$ProgressPreference = "SilentlyContinue"

$Repo           = "aw08-2004/Temp_Monitor"
$InstallerUrl   = "https://raw.githubusercontent.com/$Repo/main/install.ps1"
$AgentInstallUrl= "https://raw.githubusercontent.com/$Repo/main/agent/install/agent-install.ps1"
$PythonFallback = "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe"
# Legacy Companion leftovers, referenced only by Uninstall-Companion.
$TaskLhm        = "TempMonitor - LibreHardwareMonitor"
$TaskCompanion  = "TempMonitor - Companion"
$TaskHub        = "TempMonitor - Hub"          # legacy scheduled task (pre-service), cleaned up on install/uninstall
$TaskTurn       = "FleetHub - TURN (WSL)"      # boots the coturn WSL distro; WSL distros do NOT auto-start

# The WSL virtual machine's creator id, used to target Hyper-V firewall rules. This GUID is a
# fixed WSL constant (documented by Microsoft), not something generated per machine.
$WslVmCreatorId = '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}'
# Pinned LTS rather than the rolling "Ubuntu" alias, so a re-run a year from now provisions the
# same thing it did today.
$TurnDistroImage = "Ubuntu-24.04"

# --- Shared install root ---
# Hub and Agent live side by side under one root so an operator has a single place to
# look. Pre-rename installs used unrelated locations; those are detected and migrated
# rather than left running alongside a second copy.
$InstallRoot        = "C:\Program Files\FleetHub"
$HubInstallDefault  = Join-Path $InstallRoot "Hub"
$AgentInstallDir    = Join-Path $InstallRoot "Agent"
$LegacyHubDir       = "C:\Program Files\TempMonitor\Hub"

# -InstallDir carries a default (the legacy companion location), so "was it supplied?" can only
# be answered from PSBoundParameters -- an explicit value redirects whichever component is being
# installed. The elevation relaunch below forwards every bound parameter, so this stays true in
# the elevated process too.
$InstallDirGiven = $PSBoundParameters.ContainsKey('InstallDir')
if ($InstallDirGiven) {
    $InstallDir      = $InstallDir.TrimEnd('\')
    $AgentInstallDir = $InstallDir
    if (-not $HubInstallDir) { $HubInstallDir = $InstallDir }
}

# --- Hub-as-Windows-Service ---
# WinSW wraps the Python/waitress process as a real Windows Service (Python can't be one on
# its own). Pinned to a stable v2 release; same "download a pinned asset" pattern as the agent exe.
$WinSwUrl            = "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe"
$HubServiceId        = "FleetHub"
$HubServiceName      = "FleetHub - Hub"
$LegacyHubServiceId  = "TempMonitorHub"

# --- Hub code layout ---
# All of the hub's code + assets live under this one subdirectory of the repo. The installer
# copies that whole subtree wholesale into <install root>\hub, and the hub's self-updater
# mirrors the same subtree -- so there is no hand-kept file list to drift out of sync. That
# drift is exactly what broke 1.35.0: a module added upstream (users.py) wasn't in the
# running version's list, so the update shipped app.py without it and the hub crash-looped.
# The install root itself holds only operator state (.env, logs\, the WinSW wrapper), which a
# code refresh never touches. The agent/ tree, tests and docs stay in the repo, never shipped.
$HubCodeSubdir = "hub"
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

function Invoke-Wsl {
    <#
      The single door to wsl.exe. Two things make a naive `& wsl ...` unreliable here:

        * wsl.exe writes UTF-16LE by default, so a captured "FleetHubTurn" arrives as
          "F`0l`0e`0e`0t..." and EVERY -match against it silently fails. That turns distro
          detection into "found nothing", which is the worst possible wrong answer -- it means
          the loud warning about restarting Docker never fires. WSL_UTF8=1 fixes it at source;
          the -replace is belt-and-braces for older builds that ignore the variable.
        * Native stderr redirection under $ErrorActionPreference='Stop' raises
          NativeCommandError even on a clean exit, so the preference is relaxed for the call.

      -Stream skips capture entirely for the multi-minute `wsl --install`, so its progress
      renders live; a silent twelve-minute pause reads as a hang and gets Ctrl-C'd, which is
      precisely how you end up with a half-registered distro.
    #>
    param([string[]]$Arguments, [switch]$Stream)

    $prevUtf8 = $env:WSL_UTF8
    $prevEap  = $ErrorActionPreference
    $env:WSL_UTF8 = "1"
    $ErrorActionPreference = "Continue"
    try {
        if ($Stream) {
            & wsl.exe @Arguments
            return @{ ExitCode = $LASTEXITCODE; Output = "" }
        }
        $out = & wsl.exe @Arguments 2>&1 | Out-String
        return @{ ExitCode = $LASTEXITCODE; Output = ($out -replace "`0", "") }
    } catch {
        return @{ ExitCode = -1; Output = $_.Exception.Message }
    } finally {
        $ErrorActionPreference = $prevEap
        if ($null -eq $prevUtf8) { Remove-Item Env:\WSL_UTF8 -ErrorAction SilentlyContinue }
        else { $env:WSL_UTF8 = $prevUtf8 }
    }
}

function Get-FreeSpaceGB([string]$Path) {
    # Walk up to the deepest existing ancestor: the target directory usually doesn't exist yet.
    # Returns $null for anything that isn't a local drive (UNC, mapped oddities) so the caller
    # can warn and carry on rather than guess.
    try {
        $probe = $Path
        while ($probe -and -not (Test-Path $probe)) { $probe = Split-Path $probe -Parent }
        if (-not $probe) { return $null }
        $root = [System.IO.Path]::GetPathRoot((Resolve-Path $probe).Path)
        if ($root -notmatch '^[A-Za-z]:\\$') { return $null }
        $drive = $root.Substring(0, 2)
        $disk  = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$drive'" -ErrorAction Stop
        if (-not $disk) { return $null }
        return @{
            Drive   = $drive
            FreeGB  = [math]::Round($disk.FreeSpace / 1GB, 1)
            TotalGB = [math]::Round($disk.Size / 1GB, 1)
        }
    } catch { return $null }
}

function Ensure-FreeSpace([string]$Path, [double]$RequiredGB, [double]$HardFloorGB = 4) {
    <#
      Gate the WSL distro creation on free space. Sizing: the Ubuntu rootfs download is ~0.7 GB,
      the registered ext4.vhdx settles around 1.5-2.5 GB, and apt metadata adds a few hundred MB
      -- but the VHDX only ever ratchets UP as logs and apt churn accumulate, it never shrinks
      on its own. Hence a soft gate well above the immediate need.

      Below the hard floor we refuse outright: `wsl --install` that runs out of room mid-extract
      leaves a half-registered distro, which is a worse place to be than not having started.
      Returns $true to proceed. Never Die -- TURN is optional, the hub install is not.
    #>
    $space = Get-FreeSpaceGB $Path
    if (-not $space) {
        Warn "Could not determine free disk space for '$Path' -- continuing without the check."
        return $true
    }
    if ($space.FreeGB -ge $RequiredGB) {
        Ok "Disk space: $($space.FreeGB) GB free on $($space.Drive)"
        return $true
    }
    if ($space.FreeGB -lt $HardFloorGB) {
        Warn "Only $($space.FreeGB) GB free on $($space.Drive) -- a WSL distro needs ~3 GB to install and grows from there."
        Say  "Refusing to start: running out of space mid-install leaves a half-registered distro."
        Say  "Free up space, or re-run with  -TurnWslLocation D:\FleetHubTurn  to use another drive."
        return $false
    }
    Warn "Only $($space.FreeGB) GB free on $($space.Drive) (recommended: $RequiredGB GB)."
    Say  "A WSL distro needs ~3 GB now, and its virtual disk grows over time without shrinking back."
    return (Prompt-YesNo "Continue anyway?" -Default No)
}

function ConvertTo-WslPath([string]$WindowsPath) {
    # C:\foo\bar -> /mnt/c/foo/bar . UNC has no /mnt equivalent, so refuse rather than emit
    # something that silently resolves to the wrong place inside the distro.
    if ($WindowsPath -notmatch '^([A-Za-z]):\\(.*)$') { return $null }
    $drive = $matches[1].ToLower()
    $rest  = $matches[2] -replace '\\', '/'
    return "/mnt/$drive/$rest"
}

function Write-LinuxFile([string]$Path, [string[]]$Lines) {
    <#
      Write a file that Linux will parse: LF endings, UTF-8, no BOM.

      Deliberately NOT WriteAllLines, which the .env writes use: on .NET Framework that emits
      Environment.NewLine (CRLF). python-dotenv tolerates a stray CR; bash, /etc/wsl.conf and
      coturn's config parser do not. A CRLF here makes `static-auth-secret=abc` into the secret
      "abc`r", and every allocation then fails with 401 -- indistinguishable from the hub/coturn
      secret desync already documented in turn\README.md. One newline convention, chosen here.
    #>
    # Normalise after joining, not before: callers pass both line arrays and here-strings (which
    # already carry CRLF internally on Windows), and only one of those is fixed by the join.
    $text = (($Lines -join "`n") -replace "`r`n", "`n") -replace "`r", "`n"
    if (-not $text.EndsWith("`n")) { $text += "`n" }
    [System.IO.File]::WriteAllText($Path, $text, (New-Object System.Text.UTF8Encoding($false)))
}

function New-TurnRestCredential([string]$Secret, [string]$SessionId, [int]$TtlSeconds = 600) {
    # The TURN REST scheme (draft-uberti-behave-turn-rest), mirroring hub/remote.py
    # mint_turn_credentials EXACTLY: username = "<expiry-unix>:<session-id>",
    # password = base64(HMAC-SHA1(secret, username)). Kept in lockstep with that function --
    # if one changes, change both, or verification will pass while real sessions fail.
    # Unix epoch the long way round, deliberately. `Get-Date -UFormat %s` on Windows PowerShell
    # 5.1 returns LOCAL time, not UTC -- on a UTC-3 box that yields a timestamp three hours in
    # the past, coturn rejects the credential as already expired, and verification reports a
    # broken relay that is in fact fine. DateTimeOffset.ToUnixTimeSeconds() would also work but
    # needs .NET 4.6+; this arithmetic works everywhere.
    $utcNow   = [DateTime]::UtcNow
    $unixBase = New-Object DateTime(1970, 1, 1, 0, 0, 0, [DateTimeKind]::Utc)
    $expiry   = [int](($utcNow - $unixBase).TotalSeconds) + $TtlSeconds
    $username = "$($expiry):$SessionId"
    $hmac     = New-Object System.Security.Cryptography.HMACSHA1
    try {
        $hmac.Key = [System.Text.Encoding]::UTF8.GetBytes($Secret)
        $digest   = $hmac.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($username))
    } finally { $hmac.Dispose() }
    return @{ Username = $username; Password = [Convert]::ToBase64String($digest); Expiry = $expiry }
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
      A missing prerequisite is a question, not a dead end: the hub path goes through
      here rather than aborting the run on a bare machine.
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

function Show-Menu {
    # Loops rather than recursing on a rejected answer: every "ask again" used to be another
    # `return Show-Menu` stack frame, so enough invalid input (or a redirected stdin handing
    # back an endless stream of empty answers) walked the menu into PowerShell's call-depth
    # limit instead of just asking again.
    while ($true) {
        Write-Host ""
        Write-Host "  FleetHub - Unified Installer"                                                  -ForegroundColor Cyan
        Write-Host "  ================================="                                             -ForegroundColor Cyan
        Write-Host "   1) Install Agent      (C#/.NET Windows Service - recommended)"                 -ForegroundColor Cyan
        Write-Host "   2) Install Hub        (Flask/Socket.IO server - this machine becomes the hub)" -ForegroundColor Cyan
        Write-Host "   3) Install TURN       (coturn relay only - for remote control, no hub)"        -ForegroundColor Cyan
        Write-Host "   4) Uninstall..."                                                               -ForegroundColor Cyan
        Write-Host "   0) Exit"                                                                       -ForegroundColor Cyan
        Write-Host ""
        $choice = Read-Host "Choose an option"
        switch ($choice) {
            "1" { return "Agent" }
            "2" { return "Hub" }
            "3" { return "Turn" }
            "4" { return "UninstallMenu" }
            "0" { return "Exit" }
            default { Warn "Invalid choice." }
        }
    }
}

function Show-UninstallMenu {
    # Companion is listed here even though it can no longer be installed: the legacy Python
    # scheduled task is still out there on unmigrated machines, and cleaning one up is exactly
    # what an operator should be doing, so that path stays friction-free.
    while ($true) {
        Write-Host "`n  Which component do you want to uninstall?" -ForegroundColor Cyan
        Write-Host "   1) Agent"
        Write-Host "   2) Companion     (legacy Python scheduled-task agent)"
        Write-Host "   3) Hub            (also removes the TURN relay, if this box has one)"
        Write-Host "   4) TURN relay     (leaves the hub alone)"
        Write-Host "   0) Cancel"
        $choice = Read-Host "Choose an option"
        switch ($choice) {
            "1" { return "Agent" }
            "2" { return "Companion" }
            "3" { return "Hub" }
            "4" { return "Turn" }
            "0" { return "Exit" }
            default { Warn "Invalid choice." }
        }
    }
}

# ----------------------------------------------------------------------
# Elevate: every path below needs admin (service/task registration)
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
# Companion (legacy Python scheduled-task agent) -- UNINSTALL ONLY
# companion.py no longer exists in the repo; this only cleans up machines that
# still carry an install from before it was removed.
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

    # Forwarded so an agent installed with -InstallDir can be removed with the same flag; the
    # sub-installer would otherwise only clean its own default location.
    if ($localInstaller) {
        & $localInstaller -Uninstall -InstallDir $AgentInstallDir
    } else {
        $tmp = Join-Path $env:TEMP "temp-monitor-agent-install.ps1"
        Invoke-WebRequest -Uri $AgentInstallUrl -OutFile $tmp -UseBasicParsing
        & $tmp -Uninstall -InstallDir $AgentInstallDir
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
    $resolvedHubUrl = $HubUrl

    # -AgentUrl is the download URL of a release asset, but "the URL" an operator has in hand is
    # almost always the hub's. A bare origin (no path, or no file at the end) can't be an asset,
    # so take it as the hub URL rather than installing an agent that downloads an HTML page.
    if ($resolvedUrl -and -not ($resolvedUrl -match '/[^/]+\.(exe|zip)$')) {
        if (-not $resolvedHubUrl) {
            Warn "-AgentUrl '$resolvedUrl' is not a downloadable agent asset (.exe/.zip)."
            Say  "Treating it as the hub URL. Pass -HubUrl next time; -AgentUrl is only for a specific release asset."
            $resolvedHubUrl = $resolvedUrl
        } else {
            Warn "-AgentUrl '$resolvedUrl' is not a downloadable agent asset (.exe/.zip) -- ignoring it."
        }
        $resolvedUrl = $null
    }

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
            # No prompt: the latest release is what an operator wants in every case except an
            # explicit pin, and -AgentUrl / -AgentExe already cover the pin.
            Say "Looking up the latest agent release on GitHub..."
            $resolvedUrl = Get-LatestAgentAssetUrl
            if (-not $resolvedUrl) { Die "No agent URL available. Re-run with -AgentUrl <url> or -AgentExe <path>." }
            Ok "Agent download: $resolvedUrl"
        }
    }

    # Anything supplied on the command line is used as given -- an answered question is not
    # asked again, so a fully-specified invocation runs start to finish without input.
    if ($resolvedHubUrl) {
        Ok "Hub URL: $resolvedHubUrl"
    } else {
        $hubUrlDefault = $envDefaults["HUB_URL"]
        if (-not $hubUrlDefault) { $hubUrlDefault = "https://your.domain.com" }
        $resolvedHubUrl = Prompt-Value "Hub URL" $hubUrlDefault `
                            -Required -Validate { param($v) $v -match '^https?://' } `
                            -ValidateHint "Enter a full URL starting with http:// or https://."
    }

    if ($EnrollmentSecret) {
        $resolvedSecret = $EnrollmentSecret
        Ok "Enrollment secret: $(Mask $resolvedSecret)"
    } else {
        $secretDefault  = $envDefaults["AGENT_ENROLLMENT_SECRET"]
        $resolvedSecret = Prompt-Value "Agent enrollment secret (blank = telemetry-only until enrolled later)" $secretDefault -Secret
    }

    $agentArgs = @{}
    if ($resolvedExe) { $agentArgs.AgentExe = $resolvedExe } else { $agentArgs.AgentUrl = $resolvedUrl }
    if ($resolvedSecret) { $agentArgs.EnrollmentSecret = $resolvedSecret }
    if ($resolvedHubUrl) { $agentArgs.HubUrl = $resolvedHubUrl }
    # Keep hub and agent under one root. Passed explicitly so a downloaded agent-install.ps1
    # (whose own default could be an older release's) still lands where this installer says.
    $agentArgs.InstallDir = $AgentInstallDir

    # The exclusion always follows the directory the agent is actually installed to, so
    # -InstallDir redirects it too. -AddDefenderExclusion / -SkipDefenderExclusion pre-answer
    # the question for unattended runs; without either, ask.
    $wantExclusion = $false
    if ($AddDefenderExclusion -and $SkipDefenderExclusion) {
        Die "Pass either -AddDefenderExclusion or -SkipDefenderExclusion, not both."
    } elseif ($AddDefenderExclusion) {
        $wantExclusion = $true
    } elseif ($SkipDefenderExclusion) {
        Say "Skipping the Windows Defender exclusion (-SkipDefenderExclusion)."
    } else {
        $wantExclusion = Prompt-YesNo "Add agent install directory '$AgentInstallDir' to Windows Defender exclusion?" -Default No
    }

    if ($wantExclusion) {
        # Ensure the path exists so the exclusion is meaningful
        if (-not (Test-Path $AgentInstallDir)) {
            try {
                New-Item -ItemType Directory -Force -Path $AgentInstallDir | Out-Null
                Ok "Created $AgentInstallDir (so it can be excluded)"
            } catch {
                Warn "Could not create ${AgentInstallDir}: $($_.Exception.Message)"
            }
        }
    
        # Add the exclusion if the cmdlet is available
        if (Get-Command Add-MpPreference -ErrorAction SilentlyContinue) {
            try {
                Write-Host "  Adding Windows Defender exclusion for $AgentInstallDir ..."
                Add-MpPreference -ExclusionPath $AgentInstallDir
                Ok "Added Windows Defender exclusion: $AgentInstallDir"
            } catch {
                Warn "Failed to add Defender exclusion: $($_.Exception.Message)"
                Warn "You may need to add an exclusion manually or via your AV vendor's management tools."
            }
        } else {
            Warn "Add-MpPreference not available on this system (Windows Defender cmdlets missing)."
            Warn "Add the exclusion manually or use your AV management tooling."
        }
    }

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
    # The install root holds state (.env, logs\, the wrapper); the code lives under hub\.
    # An install root is recognised by either the current hub\app.py or the pre-subfolder
    # flat app.py, so this keeps finding hubs deployed before the move (e.g. for -Uninstall).
    function Test-HubRoot([string]$dir) {
        return (Test-Path (Join-Path $dir "$HubCodeSubdir\app.py")) -or (Test-Path (Join-Path $dir "app.py"))
    }
    if ($HubInstallDir) { return $HubInstallDir.TrimEnd('\') }
    if (Test-HubRoot $HubInstallDefault) { return $HubInstallDefault }
    # Pre-rename layout, so `-Uninstall` still finds a hub installed before the rename.
    if (Test-HubRoot $LegacyHubDir) { return $LegacyHubDir }
    if ($PSScriptRoot -and (Test-HubRoot $PSScriptRoot)) { return $PSScriptRoot }
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
    # Tear down the TURN relay too -- a no-op, and silent, when it was never installed.
    Uninstall-TurnWsl
    Warn "Left the hub files at $hubDir (including .env and logs) in place -- they may still hold data you want."
    Write-Host "`nDone.`n" -ForegroundColor Green
}

function Copy-HubTree {
    <#
      Mirror the hub's whole code subtree ($Source\hub) into $Dest\hub. The subtree is
      authoritative about its own file set -- there is no allowlist to keep in sync -- so a
      module or asset added or removed upstream is picked up automatically. The code dir is
      mirrored (removed first) so an upstream deletion propagates; the operator's state
      (.env, logs\, the WinSW wrapper) lives in $Dest itself, outside the code dir, untouched.
    #>
    param([string]$Source, [string]$Dest)

    $srcCode = Join-Path $Source $HubCodeSubdir
    foreach ($essential in @("app.py", "wsgi.py", "requirements.txt")) {
        if (-not (Test-Path (Join-Path $srcCode $essential))) {
            Die "Source archive $HubCodeSubdir\ is missing $essential -- refusing to install a partial hub."
        }
    }
    $destCode = Join-Path $Dest $HubCodeSubdir
    if (Test-Path $destCode) { Remove-Item $destCode -Recurse -Force }
    Copy-Item $srcCode $destCode -Recurse -Force
}

function Get-HubFiles {
    <#
      Download main as a zip and lay down the hub's code subtree at $Dest\hub.
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
        Copy-HubTree -Source $root.FullName -Dest $Dest
        Ok "Installed hub code to $(Join-Path $Dest $HubCodeSubdir)"
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

# ============================================================================================
# TURN relay: coturn in a dedicated Ubuntu WSL2 distro
# ============================================================================================
# Why WSL and not a container: on a Windows host a coturn CONTAINER relays from the Docker
# bridge. --external-ip makes it advertise the public address and inbound DNAT works, so
# allocations succeed for both peers and everything looks healthy -- but relay->peer egress is
# SNAT'd to an arbitrary source port, and ICE requires the peer to receive from EXACTLY the
# advertised candidate. Every check then fails and the agent logs "peer connection state:
# failed". A distro in mirrored networking mode shares the host's network namespace, so the
# relay ports are symmetric and the problem disappears. See turn\README.md 'Host-OS notes'.
#
# House rule for everything below: NOTHING here calls Die. This runs after the hub service is
# already up and before the final banner that prints the once-only enrollment secret -- a throw
# would destroy output the operator cannot get back. Failures Warn, explain the fix, and let the
# hub install finish. The remediation for every partial state is the same single sentence:
# re-run  install.ps1 -Component Hub.

function Get-WslEnvironment {
    <#
      One probe of the machine's WSL situation, so the caller isn't shelling out repeatedly.
      Every string here comes through Invoke-Wsl, which is what makes the -match calls work at
      all (see the UTF-16 note there).
    #>
    param([string]$TargetDistro)

    # Named $info, not $env: a local named $env reads as a shadow of the environment drive and
    # is a trap for the next person editing this, even though PowerShell parses $env:X separately.
    $info = @{
        ExePresent = $false; Version = $null; SupportsNameFlag = $false
        Distros = @(); OtherDistros = @(); HasDockerDesktop = $false; TargetExists = $false
    }
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) { return $info }
    $info.ExePresent = $true

    $ver = Invoke-Wsl @('--version')
    if ($ver.ExitCode -eq 0 -and $ver.Output -match 'WSL version:\s*([0-9]+)\.([0-9]+)') {
        $info.Version = "$($matches[1]).$($matches[2])"
    }

    # Detect --name from the help text rather than comparing versions: the release that
    # introduced it isn't something to guess at, and a wrong constant would disqualify machines
    # that work perfectly well.
    #
    # Note `wsl --install --help` is NOT valid ("Invalid command line argument: --help") -- the
    # flags live under the top-level `wsl --help`. And a bare search for "--name" there would
    # false-positive on `--mount`'s own --name, so isolate the --install block first: top-level
    # arguments are indented four spaces, their options deeper.
    $help = Invoke-Wsl @('--help')
    if ($help.Output -match '(?s)\n\s{4}--install\b.*?(?=\n\s{4}--\w)') {
        if ($matches[0] -match '--name') { $info.SupportsNameFlag = $true }
    } elseif ($help.Output -match '--name') {
        # Unrecognised help layout: assume support rather than disqualify a capable machine.
        # If it turns out to be wrong, `wsl --install` says so plainly and that path Warns.
        $info.SupportsNameFlag = $true
    }

    $list = Invoke-Wsl @('--list', '--quiet')
    if ($list.ExitCode -eq 0) {
        $info.Distros = @($list.Output -split "`r?`n" | ForEach-Object { $_.Trim() } |
                          Where-Object { $_ })
    }
    $info.TargetExists = @($info.Distros | Where-Object { $_ -eq $TargetDistro }).Count -gt 0
    $info.OtherDistros = @($info.Distros | Where-Object { $_ -ne $TargetDistro })
    if (Get-Service com.docker.service -ErrorAction SilentlyContinue) { $info.HasDockerDesktop = $true }
    if (Get-Process 'Docker Desktop' -ErrorAction SilentlyContinue)   { $info.HasDockerDesktop = $true }
    return $info
}

function Get-HostLanIPv4 {
    # The address the distro will share in mirrored mode, and what verification points at.
    try {
        $cfg = Get-NetIPConfiguration -ErrorAction Stop |
               Where-Object { $_.IPv4DefaultGateway -and $_.IPv4Address } |
               Select-Object -First 1
        if ($cfg) { return $cfg.IPv4Address.IPAddress }
    } catch { }
    return $null
}

function Get-PublicIPv4 {
    <#
      Best-effort public address, used ONLY to pre-fill the prompt -- the operator still confirms
      it, and a wrong guess costs nothing. Kept short-timeout and fully swallowed: an installer
      must not hang or fail because an outside lookup service is down or egress is blocked.
    #>
    $prev = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'
    try {
        $r = Invoke-RestMethod -Uri 'https://api.ipify.org?format=json' -TimeoutSec 5 -ErrorAction Stop
        if ($r.ip -match '^\d{1,3}(\.\d{1,3}){3}$') { return $r.ip }
    } catch {
    } finally {
        $ProgressPreference = $prev
    }
    return ""
}

function New-TurnServerConfLines {
    <#
      Emit one coturn config. Two listeners are generated from this, and the difference between
      them is the whole reason a machine on the relay's own LAN can be remote-controlled:

        * PUBLIC listener  -- binds 0.0.0.0, sets external-ip, relay range A.
        * LAN listener     -- binds the LAN address, NO external-ip, relay range B.

      external-ip makes coturn advertise the public address in EVERY allocate response, including
      to clients on its own LAN. That is correct for a peer out on the internet and useless for
      two peers on the LAN: they are handed a relay candidate pointing at the router's WAN
      address and have to hairpin back in, which most routers either refuse or do while rewriting
      the source port -- and ICE requires the peer to receive from exactly the advertised
      address:port. A second listener that does not rewrite anything hands LAN peers a real LAN
      relay candidate and the media never leaves the switch.

      The two relay ranges MUST NOT overlap. Under WSL mirrored networking both instances share
      the host's network namespace, so an overlapping range means the second instance to claim a
      port wins and the other's allocation silently goes nowhere.
    #>
    param([string]$Secret, [string]$ExternalIp, [string]$LocalIp,
          [string]$Realm, [int]$Port, [int]$MinPort, [int]$MaxPort,
          [string]$ListeningIp = "0.0.0.0", [switch]$NoExternalIp,
          [string]$LogFile = "/var/log/coturn/turn.log")

    $lines = @(
        "# Generated by FleetHub install.ps1 -- edit and restart the unit to apply.",
        "listening-port=$Port",
        "listening-ip=$ListeningIp",
        "min-port=$MinPort",
        "max-port=$MaxPort",
        "realm=$Realm",
        "use-auth-secret",
        "static-auth-secret=$Secret"
    )

    if (-not $NoExternalIp) {
        # external-ip takes a PUBLIC/PRIVATE pair when the advertised address differs from the
        # bound one; that is exactly our case behind a home/office NAT. Fall back to the bare
        # public form if the LAN address couldn't be detected.
        if ($LocalIp) { $ext = "$ExternalIp/$LocalIp" } else { $ext = $ExternalIp }
        $lines += "external-ip=$ext"
    } else {
        $lines += "# No external-ip on purpose: this listener serves peers on the LAN, which need"
        $lines += "# a relay candidate they can actually route to. See New-TurnServerConfLines."
    }

    $lines += @(
        "fingerprint",
        "no-cli",
        "no-tls",
        "no-dtls",
        "no-multicast-peers",
        "# Deny relaying to loopback and link-local. RFC1918 is deliberately NOT denied: LAN",
        "# relay is a legitimate case here. On an internet-exposed hub you may want to add",
        "#   denied-peer-ip=10.0.0.0-10.255.255.255",
        "#   denied-peer-ip=172.16.0.0-172.31.255.255",
        "#   denied-peer-ip=192.168.0.0-192.168.255.255",
        "denied-peer-ip=127.0.0.0-127.255.255.255",
        "denied-peer-ip=169.254.0.0-169.254.255.255",
        "log-file=$LogFile",
        "simple-log"
    )
    return $lines
}

function Set-WslConfigMirrored([string]$Path) {
    <#
      Merge networkingMode=mirrored into %USERPROFILE%\.wslconfig, preserving every other line,
      comment and section byte-for-byte. PowerShell 5.1 has no INI parser and .wslconfig may
      hold memory/processor/swap settings the operator cares about, so this is deliberately a
      minimal surgical edit rather than a rewrite.

      Returns @{ Changed; Previous; BackupPath }. Changed=$false means no `wsl --shutdown` is
      needed, which also means the caller can skip the whole "this restarts your containers"
      confirmation.
    #>
    $result = @{ Changed = $false; Previous = $null; BackupPath = $null }

    if (-not (Test-Path $Path)) {
        Write-LinuxFile $Path @("[wsl2]", "networkingMode=mirrored")
        $result.Changed = $true
        return $result
    }

    $lines = @(Get-Content $Path -Encoding UTF8)
    $secStart = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match '^\s*\[wsl2\]\s*$') { $secStart = $i; break }
    }

    if ($secStart -lt 0) {
        $backup = "$Path.fleethub-" + (Get-Date -Format "yyyyMMdd-HHmmss")
        Copy-Item $Path $backup -Force
        $result.BackupPath = $backup
        Write-LinuxFile $Path (@($lines) + @("", "[wsl2]", "networkingMode=mirrored"))
        $result.Changed = $true
        return $result
    }

    # Extent of the [wsl2] section: up to the next section header or EOF.
    $secEnd = $lines.Count
    for ($i = $secStart + 1; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match '^\s*\[') { $secEnd = $i; break }
    }

    $modeIdx = -1
    for ($i = $secStart + 1; $i -lt $secEnd; $i++) {
        if ($lines[$i] -match '^\s*networkingMode\s*=\s*(.*?)\s*$') {
            $modeIdx = $i
            $result.Previous = $matches[1]
            break
        }
    }

    if ($modeIdx -ge 0 -and $result.Previous -eq 'mirrored') { return $result }   # already there

    $backup = "$Path.fleethub-" + (Get-Date -Format "yyyyMMdd-HHmmss")
    Copy-Item $Path $backup -Force
    $result.BackupPath = $backup

    $new = New-Object System.Collections.Generic.List[string]
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($i -eq $modeIdx) { $new.Add("networkingMode=mirrored"); continue }
        $new.Add($lines[$i])
        if ($modeIdx -lt 0 -and $i -eq $secStart) { $new.Add("networkingMode=mirrored") }
    }
    Write-LinuxFile $Path $new.ToArray()
    $result.Changed = $true
    return $result
}

function New-TurnFirewallRules {
    <#
      Two firewall layers must both allow this, and the second one is the one that catches
      people out: with WSL 2.0.9+ the Hyper-V firewall is on by default and blocks inbound to
      WSL even in mirrored mode. A coturn that works perfectly on the box and refuses every
      remote allocation is almost always this.

      Per-port Hyper-V rules rather than -DefaultInboundAction Allow, which would open every
      port of every WSL distro on the machine, docker-desktop included.
    #>
    param([int]$Port, [int]$MinPort, [int]$MaxPort,
          [int]$LanPort = 0, [int]$LanMinPort = 0, [int]$LanMaxPort = 0)

    $rules = @(
        @{ Name = 'FleetHub-TURN-Control-UDP'; Display = "FleetHub TURN control (UDP $Port)";            Proto = 'UDP'; Ports = "$Port" },
        @{ Name = 'FleetHub-TURN-Control-TCP'; Display = "FleetHub TURN control (TCP $Port)";            Proto = 'TCP'; Ports = "$Port" },
        @{ Name = 'FleetHub-TURN-Relay-UDP';   Display = "FleetHub TURN relay (UDP $MinPort-$MaxPort)";  Proto = 'UDP'; Ports = "$MinPort-$MaxPort" }
    )
    # The LAN listener is never port-forwarded, so these are scoped to LocalSubnet: the ports are
    # open to the machines that can use them and to nobody else.
    if ($LanPort -gt 0) {
        $rules += @(
            @{ Name = 'FleetHub-TURN-Lan-Control-UDP'; Display = "FleetHub TURN LAN control (UDP $LanPort)";              Proto = 'UDP'; Ports = "$LanPort";                Scope = 'LocalSubnet' },
            @{ Name = 'FleetHub-TURN-Lan-Control-TCP'; Display = "FleetHub TURN LAN control (TCP $LanPort)";              Proto = 'TCP'; Ports = "$LanPort";                Scope = 'LocalSubnet' },
            @{ Name = 'FleetHub-TURN-Lan-Relay-UDP';   Display = "FleetHub TURN LAN relay (UDP $LanMinPort-$LanMaxPort)"; Proto = 'UDP'; Ports = "$LanMinPort-$LanMaxPort"; Scope = 'LocalSubnet' }
        )
    }

    try {
        foreach ($r in $rules) {
            Remove-NetFirewallRule -Name $r.Name -ErrorAction SilentlyContinue
            if ($r.Scope) {
                New-NetFirewallRule -Name $r.Name -DisplayName $r.Display -Group 'FleetHub' `
                    -Direction Inbound -Action Allow -Protocol $r.Proto -LocalPort $r.Ports `
                    -RemoteAddress $r.Scope -Profile Any -ErrorAction Stop | Out-Null
            } else {
                New-NetFirewallRule -Name $r.Name -DisplayName $r.Display -Group 'FleetHub' `
                    -Direction Inbound -Action Allow -Protocol $r.Proto -LocalPort $r.Ports `
                    -Profile Any -ErrorAction Stop | Out-Null
            }
        }
        Ok "Windows Firewall: opened $Port/udp, $Port/tcp and $MinPort-$MaxPort/udp"
        if ($LanPort -gt 0) {
            Ok "Windows Firewall: opened $LanPort/udp, $LanPort/tcp and $LanMinPort-$LanMaxPort/udp (LocalSubnet only)"
        }
    } catch {
        Warn "Could not add Windows Firewall rules -- $($_.Exception.Message)"
        Say  "Open $Port/udp, $Port/tcp and $MinPort-$MaxPort/udp inbound by hand."
        if ($LanPort -gt 0) { Say "  ...and $LanPort/udp, $LanPort/tcp, $LanMinPort-$LanMaxPort/udp for the LAN listener." }
    }

    if (-not (Get-Command New-NetFirewallHyperVRule -ErrorAction SilentlyContinue)) {
        Warn "New-NetFirewallHyperVRule isn't available on this build."
        Say  "The Hyper-V firewall may block inbound traffic to WSL even with the rules above."
        if (Prompt-YesNo "Allow all inbound to WSL instead (opens every port of every WSL distro)?" -Default No) {
            try {
                Set-NetFirewallHyperVVMSetting -Name $WslVmCreatorId -DefaultInboundAction Allow -ErrorAction Stop
                Ok "Hyper-V firewall: default inbound set to Allow for WSL"
            } catch { Warn "That failed too -- $($_.Exception.Message). See turn\README.md." }
        } else {
            Say "Skipped. If remote machines can't allocate, this is the first thing to check."
        }
        return
    }

    try {
        foreach ($r in $rules) {
            Remove-NetFirewallHyperVRule -Name $r.Name -ErrorAction SilentlyContinue
            New-NetFirewallHyperVRule -Name $r.Name -DisplayName $r.Display `
                -VMCreatorId $WslVmCreatorId -Direction Inbound -Action Allow `
                -Protocol $r.Proto -LocalPorts $r.Ports -ErrorAction Stop | Out-Null
        }
        Ok "Hyper-V firewall: opened the same ports for WSL"
    } catch {
        Warn "Could not add Hyper-V firewall rules -- $($_.Exception.Message)"
        Say  "Inbound traffic to WSL may be blocked; see turn\README.md 'Troubleshooting'."
    }
}

function Remove-TurnFirewallRules {
    try { Remove-NetFirewallRule -Group 'FleetHub' -ErrorAction SilentlyContinue } catch { }
    if (Get-Command Remove-NetFirewallHyperVRule -ErrorAction SilentlyContinue) {
        foreach ($n in @('FleetHub-TURN-Control-UDP','FleetHub-TURN-Control-TCP','FleetHub-TURN-Relay-UDP',
                         'FleetHub-TURN-Lan-Control-UDP','FleetHub-TURN-Lan-Control-TCP','FleetHub-TURN-Lan-Relay-UDP')) {
            try { Remove-NetFirewallHyperVRule -Name $n -ErrorAction SilentlyContinue } catch { }
        }
    }
}

function Register-TurnBootTask([string]$Distro) {
    <#
      WSL distros do NOT start at boot. Without this the relay works on install day and is
      silently dead after the next reboot -- the exact class of quiet failure this whole feature
      exists to remove.

      The principal matters more than it looks: WSL distros are registered PER USER under
      HKCU\...\Lxss, so a task running as SYSTEM -- which is what every other service in this
      installer does -- cannot see the distro at all and fails every time with "There is no
      distribution with the supplied name." S4U under the installing user's SID runs whether or
      not that user is logged on, with no stored password.

      The repeating trigger is a watchdog: `wsl --shutdown` gets issued by all sorts of things
      (Docker Desktop's own restart flow among them) and takes the distro down with it, with no
      other recovery path.
    #>
    $sid       = ([Security.Principal.WindowsIdentity]::GetCurrent()).User.Value
    $atStartup = New-ScheduledTaskTrigger -AtStartup
    $atStartup.Delay = 'PT45S'          # let the WSL service and networking settle first
    # NOTE: -RepetitionInterval with no -RepetitionDuration is CORRECT here and means
    # "indefinitely". Do not "fix" it by adding -RepetitionDuration ([TimeSpan]::MaxValue):
    # that serialises to P99999999DT23H59M59S, which the task scheduler rejects outright with
    # "The task XML contains a value which is incorrectly formatted or out of range."
    $watchdog  = New-ScheduledTaskTrigger -Once -At (Get-Date) `
                    -RepetitionInterval (New-TimeSpan -Minutes 5)
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                    -DontStopIfGoingOnBatteries -StartWhenAvailable `
                    -ExecutionTimeLimit ([TimeSpan]::Zero)
    # Invoke a script inside the distro, not an inline bash string: nothing to mis-quote in
    # the task XML, and starting an already-running distro is a harmless no-op.
    $action    = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\wsl.exe" `
                    -Argument "-d $Distro -u root -- /usr/local/sbin/fleethub-turn-boot.sh"

    # S4U first, Interactive as a fallback. S4U registration is refused outright on some
    # accounts -- it needs an S4U logon token for the SID, which not every local/Microsoft
    # account can obtain -- and it surfaces as a bare "Access is denied" that says nothing
    # about which part was denied. Interactive is strictly worse (the relay only comes up
    # once that user logs on) but it is enormously better than no task at all, which is what
    # this function used to leave behind. Seen in the field 2026-07-30: registration was
    # denied, the failure was a Warn that scrolled past, the install reported success, and the
    # relay was dead from the next reboot until someone went looking.
    $attempts = @(
        @{ Label = 'S4U';         Type = 'S4U' },
        @{ Label = 'Interactive'; Type = 'Interactive' }
    )
    $lastError = $null
    foreach ($attempt in $attempts) {
        try {
            $principal = New-ScheduledTaskPrincipal -UserId $sid `
                            -LogonType $attempt.Type -RunLevel Highest
            Register-ScheduledTask -TaskName $TaskTurn -Force -Action $action `
                -Trigger @($atStartup, $watchdog) -Principal $principal -Settings $settings `
                -Description "Starts the FleetHub TURN WSL distro (coturn) at boot and keeps it up." `
                -ErrorAction Stop | Out-Null
        } catch {
            $lastError = $_.Exception.Message
            continue
        }
        # Registering without throwing is not proof it is there. Confirm before claiming it.
        if (-not (Get-ScheduledTask -TaskName $TaskTurn -ErrorAction SilentlyContinue)) {
            $lastError = "Register-ScheduledTask reported success but '$TaskTurn' does not exist."
            continue
        }
        if ($attempt.Type -eq 'S4U') {
            Ok "Registered the boot task '$TaskTurn'"
        } else {
            Ok   "Registered the boot task '$TaskTurn' (logon type: Interactive)"
            Warn "S4U was refused for this account, so the task runs only once $env:USERNAME logs on."
            Say  "On a machine that boots unattended, the relay stays down until that login."
        }
        return $true
    }

    Warn "COULD NOT REGISTER THE BOOT TASK '$TaskTurn' -- $lastError"
    Say  "This is not cosmetic. WSL distros do not start at boot, so coturn is up now and will be"
    Say  "DEAD after the next reboot (and after any 'wsl --shutdown' -- Docker Desktop issues one)."
    Say  "Remote sessions will then fail for every machine that needs the relay."
    Say  "Fix it by re-running this installer from an ELEVATED PowerShell, or create it by hand:"
    Say  "  schtasks /create /tn ""$TaskTurn"" /sc onstart /delay 0000:45 /rl highest /ru $env:USERNAME \"
    Say  "    /tr ""%WINDIR%\System32\wsl.exe -d $Distro -u root -- /usr/local/sbin/fleethub-turn-boot.sh"""
    return $false
}

function Unregister-TurnBootTask {
    if (Get-ScheduledTask -TaskName $TaskTurn -ErrorAction SilentlyContinue) {
        try {
            Unregister-ScheduledTask -TaskName $TaskTurn -Confirm:$false -ErrorAction Stop
            Ok "Removed the scheduled task '$TaskTurn'"
        } catch { Warn "Could not remove '$TaskTurn' -- $($_.Exception.Message)" }
    }
}

function New-TurnUrlList {
    <#
      The TURN URLs to put in Settings -> Remote Control, in priority order. Four of them,
      because there are three shapes of session plus one shape of NETWORK, and no single URL
      covers them all.

        1. turn:<public>:3478   -- both peers out on the internet. The ordinary case.

        2. turn:<public>:3478?transport=tcp -- same listener, reached over TCP. coturn has
           listened on 3478/tcp all along and the firewall rules have always opened it, but
           without this URL nothing ever asked for it: an ICE agent only tries TCP when the URL
           says so. It is the difference between working and not on a guest/corporate network
           that permits 443 and 3478/tcp outbound and drops UDP, which is a common shape for
           exactly the machines that need the relay most. Listed after the UDP form because UDP
           is what you want when you can have it -- TURN-over-TCP adds head-of-line blocking to
           a video stream.

        3. turn:<lan-ip>:3478   -- one peer inside this network, one outside. Same PUBLIC
           listener, just reached by its LAN address so the inside peer does not have to
           hairpin off the router to talk to a relay sitting next to it. It still gets a
           public relay candidate (external-ip rewrites it), which is exactly right: the
           outside peer has to be able to reach it.

        4. turn:<lan-ip>:3479   -- both peers inside this network. The LAN listener, which
           does NOT rewrite, so the relay candidate is a real LAN address and the media stays
           on the switch. Handed a public candidate instead, these two would have to hairpin,
           which most routers refuse or do while rewriting the source port -- and ICE requires
           the peer to receive from exactly the advertised address:port.

      All four share one secret and realm, so the hub's minted credential authenticates against
      any of them with no extra bookkeeping. The hub narrows this list per peer before handing
      it out (hub/remote.py select_urls_for_peer): a machine out on the internet is never given the
      <lan-ip> forms, because it cannot route to them and every allocation against one is
      gathering time spent on a candidate that can never appear.
    #>
    param([string]$PublicHost, [string]$LocalIp)

    $urls = @("turn:${PublicHost}:$TurnPort", "turn:${PublicHost}:${TurnPort}?transport=tcp")
    if ($LocalIp -and $LocalIp -ne $PublicHost) {
        $urls += "turn:${LocalIp}:$TurnPort"
        if ($TurnLanPort -gt 0) { $urls += "turn:${LocalIp}:$TurnLanPort" }
    }
    return $urls
}

function Install-TurnWsl {
    <#
      Create (or reconfigure) the dedicated distro and bring coturn up in it. Returns $true only
      if coturn ended up running. Every exit path is Warn-and-continue; see the house rule above.
    #>
    param([string]$Distro, [string]$Location, [string]$Secret, [string]$PublicHost,
          [string]$Realm, [int]$Port, [int]$MinPort, [int]$MaxPort,
          [bool]$DistroExists, [bool]$ApplyFirewall, [bool]$NeedsWslConfigChange,
          [int]$LanPort = 0, [int]$LanMinPort = 0, [int]$LanMaxPort = 0)

    Step "Setting up the TURN relay (coturn in WSL)"

    # ---- 1. Create the distro (skipped when reconfiguring an existing one) ----
    if (-not $DistroExists) {
        Say "Creating the '$Distro' WSL distro from $TurnDistroImage."
        Say "This downloads roughly 1 GB and can take 5-15 minutes -- leave it running."
        if (-not (Test-Path $Location)) { New-Item -ItemType Directory -Force -Path $Location | Out-Null }

        $mk = Invoke-Wsl @('--install', $TurnDistroImage, '--name', $Distro,
                           '--location', $Location, '--no-launch') -Stream
        if ($mk.ExitCode -ne 0) {
            Say "Store install returned $($mk.ExitCode); retrying with a direct download..."
            $mk = Invoke-Wsl @('--install', $TurnDistroImage, '--name', $Distro,
                               '--location', $Location, '--no-launch', '--web-download') -Stream
        }
        if ($mk.ExitCode -ne 0) {
            Warn "Could not create the WSL distro (exit $($mk.ExitCode))."
            Say  "If it asked for a reboot, reboot and re-run:  install.ps1 -Component Turn"
            return $false
        }
        Ok "Created the '$Distro' distro"
    } else {
        Say "Reusing the existing '$Distro' distro and rewriting its coturn config."
    }

    # ---- 2. Stage the config + provisioning script on the Windows side ----
    # One LF-only bash script, copied in and run with a single wsl call. Building multi-command
    # bash strings inside PowerShell 5.1 means nested quoting across wsl.exe's own re-parsing,
    # which fails in ways that look like coturn bugs rather than quoting bugs.
    $stage = Join-Path $env:TEMP ("fleethub-turn-" + [guid]::NewGuid().ToString("n"))
    New-Item -ItemType Directory -Force -Path $stage | Out-Null
    try {
        $localIp = Get-HostLanIPv4
        Write-LinuxFile (Join-Path $stage "turnserver.conf") `
            (New-TurnServerConfLines -Secret $Secret -ExternalIp $PublicHost -LocalIp $localIp `
                                     -Realm $Realm -Port $Port -MinPort $MinPort -MaxPort $MaxPort)

        # The LAN listener only makes sense if we know which address to bind. Without a detected
        # LAN IP there is nothing to bind to that isn't already the public listener's 0.0.0.0.
        $wantLan = ($LanPort -gt 0) -and $localIp
        if ($LanPort -gt 0 -and -not $localIp) {
            Warn "Could not detect this host's LAN IPv4; skipping the LAN TURN listener."
            Say  "Peers inside this network will fall back to the public relay, which needs NAT hairpinning."
        }
        if ($wantLan) {
            Write-LinuxFile (Join-Path $stage "turnserver-lan.conf") `
                (New-TurnServerConfLines -Secret $Secret -Realm $Realm `
                                         -Port $LanPort -MinPort $LanMinPort -MaxPort $LanMaxPort `
                                         -ListeningIp $localIp -NoExternalIp `
                                         -LogFile "/var/log/coturn/turn-lan.log")
            # A second instance needs its own unit: the packaged coturn.service is hard-wired to
            # /etc/turnserver.conf. An empty --pidfile keeps it from fighting the first instance
            # over /run/turnserver.pid, which would make whichever started second exit at once.
            Write-LinuxFile (Join-Path $stage "coturn-lan.service") @(
                "[Unit]",
                "Description=FleetHub LAN TURN listener (coturn, no external-ip rewrite)",
                "Documentation=file:///etc/turnserver-lan.conf",
                "After=network-online.target",
                "Wants=network-online.target",
                "",
                "[Service]",
                "Type=simple",
                "User=turnserver",
                "Group=turnserver",
                "ExecStart=/usr/bin/turnserver -c /etc/turnserver-lan.conf --pidfile=",
                "Restart=always",
                "RestartSec=5",
                "",
                "[Install]",
                "WantedBy=multi-user.target")
        }

        Write-LinuxFile (Join-Path $stage "wsl.conf") @(
            "[boot]", "systemd=true", "", "[automount]", "enabled=true")
        Write-LinuxFile (Join-Path $stage "coturn.default") @("TURNSERVER_ENABLED=1")
        Write-LinuxFile (Join-Path $stage "fleethub-turn-boot.sh") @(
            "#!/bin/sh",
            "# Started by the '$TaskTurn' scheduled task. Booting the distro is the point;",
            "# starting coturn is belt-and-braces in case systemd hasn't got there yet.",
            "systemctl start coturn >/dev/null 2>&1 || true",
            "systemctl start coturn-lan >/dev/null 2>&1 || true",
            "exit 0")

        $provision = @'
#!/bin/bash
set -euo pipefail
S="$1"
export DEBIAN_FRONTEND=noninteractive
for f in wsl.conf turnserver.conf coturn.default fleethub-turn-boot.sh \
         turnserver-lan.conf coturn-lan.service; do
  if [ -f "$S/$f" ]; then sed -i 's/\r$//' "$S/$f"; fi
done
install -m 0644 "$S/wsl.conf" /etc/wsl.conf
apt-get update -qq
apt-get install -y --no-install-recommends coturn
# Group MUST be turnserver: the systemd unit runs coturn as User=turnserver, and with a
# root:root 0640 config it cannot read this file. coturn does NOT treat that as fatal -- it
# silently falls back to built-in defaults, which means NO authentication (an open relay on
# a forwarded port) and the default 49152-65535 relay range instead of min/max-port. Both
# failures are invisible except that turn.log is never created. Verified 2026-07-28.
install -m 0640 -o root -g turnserver "$S/turnserver.conf" /etc/turnserver.conf
install -m 0644 "$S/coturn.default" /etc/default/coturn
install -m 0755 "$S/fleethub-turn-boot.sh" /usr/local/sbin/fleethub-turn-boot.sh
mkdir -p /var/log/coturn
chown turnserver:turnserver /var/log/coturn 2>/dev/null || true
mkdir -p /etc/systemd/system/coturn.service.d
printf '[Service]\nRestart=always\nRestartSec=5\n' > /etc/systemd/system/coturn.service.d/10-fleethub.conf
# The LAN listener is optional; a re-run without it must remove the previous one rather than
# leave a unit pointing at a config that is no longer written.
if [ -f "$S/turnserver-lan.conf" ]; then
  install -m 0640 -o root -g turnserver "$S/turnserver-lan.conf" /etc/turnserver-lan.conf
  install -m 0644 "$S/coturn-lan.service" /etc/systemd/system/coturn-lan.service
else
  systemctl disable --now coturn-lan >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/coturn-lan.service /etc/turnserver-lan.conf
fi
# systemd is not necessarily up yet -- wsl.conf's [boot] systemd=true only takes effect after the
# --terminate below -- so this is best-effort, like every systemctl call in this script.
systemctl daemon-reload >/dev/null 2>&1 || true
echo FLEETHUB_PROVISION_OK
'@
        Write-LinuxFile (Join-Path $stage "provision.sh") ($provision -split "`r?`n")

        $stageWsl = ConvertTo-WslPath $stage
        if (-not $stageWsl) {
            Warn "Could not map '$stage' to a WSL path -- is TEMP on a network drive?"
            return $false
        }

        # apt runs here, BEFORE the mirrored switch, while WSL is still on plain NAT: that is
        # the well-trodden networking path, so a mirrored-mode problem can never be mistaken
        # for "apt is broken".
        Say "Installing coturn inside the distro (apt)..."
        $prov = Invoke-Wsl @('-d', $Distro, '-u', 'root', '--', 'bash', "$stageWsl/provision.sh", $stageWsl)
        if ($prov.Output -notmatch 'FLEETHUB_PROVISION_OK') {
            Warn "Provisioning the distro failed."
            Say  ($prov.Output.Trim())
            Say  "If this is a DNS failure, check:  wsl -d $Distro -u root -- cat /etc/resolv.conf"
            Say  "The distro was left in place -- re-run  install.ps1 -Component Turn  to retry."
            return $false
        }
        Ok "coturn installed and configured"
    } finally {
        # turnserver.conf carried the shared secret through TEMP; don't leave it lying about.
        Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
    }

    # Distro-scoped restart to apply [boot] systemd=true. Deliberately --terminate, NOT
    # --shutdown: the latter would take down docker-desktop and every container with it.
    Invoke-Wsl @('--terminate', $Distro) | Out-Null

    # ---- 3. Mirrored networking (the one --shutdown in the whole feature) ----
    if ($NeedsWslConfigChange) {
        $wslConfig = Join-Path $env:USERPROFILE ".wslconfig"
        $merge = Set-WslConfigMirrored $wslConfig
        if ($merge.Changed) {
            Ok "Set networkingMode=mirrored in $wslConfig"
            if ($merge.BackupPath) { Say "(previous version backed up to $($merge.BackupPath))" }
            Say "Restarting the WSL virtual machine now -- Docker containers will stop and restart..."
            Invoke-Wsl @('--shutdown') | Out-Null
            Say "Docker Desktop restarts its VM on next use; containers with a restart policy come back on their own."
        } else {
            Ok "WSL is already in mirrored networking mode"
        }
    }

    # ---- 4. Firewalls, then start coturn ----
    if ($ApplyFirewall) {
        if ($wantLan) {
            New-TurnFirewallRules -Port $Port -MinPort $MinPort -MaxPort $MaxPort `
                                  -LanPort $LanPort -LanMinPort $LanMinPort -LanMaxPort $LanMaxPort
        } else {
            New-TurnFirewallRules -Port $Port -MinPort $MinPort -MaxPort $MaxPort
        }
    }
    else { Say "Skipped firewall rules (-SkipTurnFirewall)." }

    $start = Invoke-Wsl @('-d', $Distro, '-u', 'root', '--', 'systemctl', 'enable', '--now', 'coturn')
    if ($start.ExitCode -ne 0) {
        Warn "coturn did not start cleanly."
        Say  ($start.Output.Trim())
    }

    if ($wantLan) {
        $startLan = Invoke-Wsl @('-d', $Distro, '-u', 'root', '--', 'systemctl', 'enable', '--now', 'coturn-lan')
        if ($startLan.ExitCode -ne 0) {
            Warn "The LAN TURN listener did not start cleanly."
            Say  ($startLan.Output.Trim())
            Say  "Peers inside this network will fall back to the public relay (needs NAT hairpinning)."
        } else {
            Ok "LAN TURN listener up on ${localIp}:$LanPort (relay $LanMinPort-$LanMaxPort)"
        }
    }

    # Piped to Out-Null deliberately: the function returns $true/$false and this one returns
    # $true for "the relay is installed". A missing boot task is reported loudly by the
    # function itself and re-checked in Test-TurnServer, so it must not silently become part
    # of this function's return value.
    Register-TurnBootTask -Distro $Distro | Out-Null
    return $true
}

function Test-TurnServer {
    <#
      Prove the relay actually works, rather than assuming it does because nothing threw. Every
      check is Ok or Warn; a failure here never fails the install.

      Known limit, stated in the output too: turnutils_uclient runs INSIDE the distro, so it
      never traverses the Hyper-V firewall inbound path, and the public-IP attempt usually fails
      on routers that don't hairpin. These checks are strong evidence, not proof.
    #>
    param([string]$Distro, [string]$Secret, [string]$PublicHost, [int]$Port, [string]$HubEnvPath)

    Step "Verifying the TURN relay"
    $localIp = Get-HostLanIPv4

    # 1. Is the service actually up?
    $active = Invoke-Wsl @('-d', $Distro, '-u', 'root', '--', 'systemctl', 'is-active', 'coturn')
    if ($active.Output -match 'active') {
        Ok "coturn service is active"
    } else {
        Warn "coturn is not active."
        $log = Invoke-Wsl @('-d', $Distro, '-u', 'root', '--', 'journalctl', '-u', 'coturn', '-n', '30', '--no-pager')
        Say ($log.Output.Trim())
        Say "Re-run  install.ps1 -Component Turn  once the cause is fixed."
        return
    }

    # 2. Bound to the control port?
    $listen = Invoke-Wsl @('-d', $Distro, '-u', 'root', '--', 'ss', '-lun')
    if ($listen.Output -match ":$Port") { Ok "Listening on $Port/udp inside the distro" }
    else { Warn "Nothing is listening on $Port/udp inside the distro." }

    # 2b. Can the service user actually READ its config? coturn runs as User=turnserver, and an
    #     unreadable /etc/turnserver.conf is NOT fatal to it -- it silently starts on built-in
    #     defaults: no authentication and the default 49152-65535 relay range. Everything below
    #     still looks healthy, which is exactly why this needs its own check. Seen 2026-07-28.
    $readable = Invoke-Wsl @('-d', $Distro, '-u', 'root', '--', 'su', '-s', '/bin/bash',
                             '-c', 'head -1 /etc/turnserver.conf', 'turnserver')
    if ($readable.ExitCode -eq 0) {
        Ok "coturn's service user can read /etc/turnserver.conf"
    } else {
        Warn "coturn CANNOT read /etc/turnserver.conf -- it is running on defaults, with NO authentication."
        Say  "That makes this an open relay on a forwarded port. Fix it with:"
        Say  "  wsl -d $Distro -u root -- chown root:turnserver /etc/turnserver.conf"
        Say  "  wsl -d $Distro -u root -- systemctl restart coturn"
        Say  "Confirm afterwards that /var/log/coturn/turn.log exists -- if it does not, the config is still unread."
    }

    # 3. Mirrored CONFIGURED is not mirrored ACTIVE. This catches a missed --shutdown and a
    #    .wslconfig written into a different profile than the one WSL reads.
    $hostname = Invoke-Wsl @('-d', $Distro, '--', 'hostname', '-I')
    if ($localIp -and $hostname.Output -match [regex]::Escape($localIp)) {
        Ok "Mirrored networking is active (distro sees the host address $localIp)"
    } else {
        Warn "Mirrored networking does not look active -- the distro reports: $($hostname.Output.Trim())"
        Say  "Expected it to include the host's LAN address ($localIp)."
        Say  "Check networkingMode=mirrored in $(Join-Path $env:USERPROFILE '.wslconfig'), then run 'wsl --shutdown'."
    }

    # 4. Visible from Windows?
    if ($localIp) {
        try {
            $tnc = Test-NetConnection -ComputerName $localIp -Port $Port -WarningAction SilentlyContinue
            if ($tnc.TcpTestSucceeded) { Ok "Reachable from Windows on $localIp`:$Port/tcp" }
            else { Warn "Could not reach $localIp`:$Port/tcp from Windows -- check the firewall rules." }
        } catch { Warn "Reachability test failed -- $($_.Exception.Message)" }
    }

    # 5. Secret parity. This is the check that would have caught the 2026-07-27 desync, where a
    #    rotation from the console updated the hub and left coturn on the old value.
    #    On a standalone TURN install there may be no hub on this box at all, in which case
    #    there is nothing to compare against -- say so rather than reporting a false mismatch.
    #    BOTH configs are checked, not just the public one: each listener validates against its
    #    own copy, so updating one and not the other leaves LAN sessions failing with 401 while
    #    internet sessions work (or the reverse) -- a genuinely confusing half-broken state.
    $hubSecret = (Read-DotEnv $HubEnvPath)["REMOTE_TURN_SECRET"]
    if (-not $hubSecret) {
        Say "No hub .env on this machine, so the secret can't be cross-checked here."
        Say "Make sure REMOTE_TURN_SECRET on the hub matches the secret printed below."
    } else {
        foreach ($confPath in @('/etc/turnserver.conf', '/etc/turnserver-lan.conf')) {
            $exists = Invoke-Wsl @('-d', $Distro, '-u', 'root', '--', 'test', '-f', $confPath)
            if ($exists.ExitCode -ne 0) { continue }   # no LAN listener on this install
            $conf = Invoke-Wsl @('-d', $Distro, '-u', 'root', '--', 'grep', '-h', 'static-auth-secret', $confPath)
            if ($conf.Output -match 'static-auth-secret\s*=\s*(\S+)') {
                if ($matches[1] -eq $hubSecret) {
                    Ok "Shared secret in $confPath matches the hub's .env"
                } else {
                    Warn "The hub and $confPath hold DIFFERENT secrets -- every allocation there will fail with 401."
                    Say  "hub: $HubEnvPath   coturn: $confPath in '$Distro'"
                    Say  "A rotation from the console updates only the hub. Sync it with:"
                    Say  "  wsl -d $Distro -u root -- sed -i ""s/^static-auth-secret=.*/static-auth-secret=<new>/"" $confPath"
                    Say  "  wsl -d $Distro -u root -- chown root:turnserver $confPath"
                    Say  "  wsl -d $Distro -u root -- systemctl restart coturn coturn-lan"
                }
            } else {
                Warn "No static-auth-secret found in $confPath."
            }
        }
    }

    # 6. The real test: a credential minted exactly as the hub mints one, doing a real Allocate.
    $cred = New-TurnRestCredential -Secret $Secret -SessionId "installer-check"
    $target = if ($localIp) { $localIp } else { "127.0.0.1" }
    $alloc = Invoke-Wsl @('-d', $Distro, '-u', 'root', '--', 'turnutils_uclient',
                          '-u', $cred.Username, '-w', $cred.Password,
                          '-p', "$Port", '-n', '2', '-c', '-e', '8.8.8.8', $target)
    if ($alloc.Output -match 'Total transmit time' -and $alloc.Output -notmatch '401') {
        Ok "TURN Allocate succeeded with a hub-minted credential"
    } else {
        Warn "TURN Allocate did NOT succeed against $target."
        Say  "This is the check that matters -- remote sessions will fail until it passes."
    }

    # Negative control: without this, a coturn accidentally running without use-auth-secret
    # would sail through every check above.
    $bad = Invoke-Wsl @('-d', $Distro, '-u', 'root', '--', 'turnutils_uclient',
                        '-u', $cred.Username, '-w', 'deliberately-wrong',
                        '-p', "$Port", '-n', '1', '-c', '-e', '8.8.8.8', $target)
    if ($bad.Output -match 'Total transmit time' -and $bad.Output -notmatch '401') {
        Warn "A WRONG password was also accepted -- coturn is not enforcing authentication."
        Say  "This is an OPEN RELAY. Do not leave port $Port forwarded while it is in this state."
        Say  "Most likely cause is not a missing directive but an unreadable config -- see check 2b"
        Say  "above. If /var/log/coturn/turn.log does not exist, coturn never parsed the file at all."
        Say  "Otherwise check that 'use-auth-secret' is present in /etc/turnserver.conf."
    } else {
        Ok "A wrong credential is correctly rejected"
    }

    # Public path. Amber, never red: most consumer/SMB routers don't hairpin, so a failure here
    # is a common false negative rather than evidence of a broken relay.
    if ($PublicHost -and $PublicHost -ne $localIp) {
        $pub = Invoke-Wsl @('-d', $Distro, '-u', 'root', '--', 'turnutils_uclient',
                            '-u', $cred.Username, '-w', $cred.Password,
                            '-p', "$Port", '-n', '1', '-c', '-e', '8.8.8.8', $PublicHost)
        if ($pub.Output -match 'Total transmit time' -and $pub.Output -notmatch '401') {
            Ok "TURN Allocate also succeeded via the public address $PublicHost"
        } else {
            Warn "Could not allocate via $PublicHost from this machine."
            Say  "That is often just NAT hairpinning, not a fault -- most routers can't reach their own public IP from inside."
        }
    }

    # 7. Will any of the above survive a reboot? WSL distros do not auto-start, so without the
    #    boot task every check here passes on install day and the relay is dead the next
    #    morning. This check exists because that is exactly what happened on 2026-07-30: the
    #    registration was denied, the warning scrolled past, and nothing afterwards looked.
    $task = Get-ScheduledTask -TaskName $TaskTurn -ErrorAction SilentlyContinue
    if (-not $task) {
        Warn "The boot task '$TaskTurn' does NOT exist."
        Say  "coturn is up now, but WSL distros do not start at boot -- it will be dead after the"
        Say  "next reboot, and after any 'wsl --shutdown' (Docker Desktop issues one)."
        Say  "Re-run this installer from an ELEVATED PowerShell to create it."
    } elseif ($task.State -eq 'Disabled') {
        Warn "The boot task '$TaskTurn' exists but is DISABLED -- it will not run."
        Say  "Enable it with:  Enable-ScheduledTask -TaskName ""$TaskTurn"""
    } else {
        $logon = $task.Principal.LogonType
        if ($logon -eq 'Interactive') {
            Ok   "Boot task '$TaskTurn' is registered"
            Warn "It runs as Interactive, so it only fires once $($task.Principal.UserId) logs on."
            Say  "On a machine that boots unattended, the relay stays down until that login."
        } else {
            Ok "Boot task '$TaskTurn' is registered (logon type: $logon)"
        }
    }

    Say ""
    Say "Only a machine on a genuinely different network proves the cross-NAT relay."
    Say "See turn\README.md 'Troubleshooting' for how to read the coturn log during a real session."
}

function Uninstall-TurnWsl {
    <#
      Teardown for the TURN relay. Entirely silent when it was never installed, so the ordinary
      hub uninstall is unchanged for everyone who skipped TURN.
    #>
    $wslPresent = [bool](Get-Command wsl.exe -ErrorAction SilentlyContinue)
    $hasTask    = [bool](Get-ScheduledTask -TaskName $TaskTurn -ErrorAction SilentlyContinue)
    $hasDistro  = $false
    if ($wslPresent) {
        $list = Invoke-Wsl @('--list', '--quiet')
        $hasDistro = @($list.Output -split "`r?`n" | ForEach-Object { $_.Trim() } |
                       Where-Object { $_ -eq $TurnDistro }).Count -gt 0
    }
    if (-not $hasTask -and -not $hasDistro) { return }

    Step "Removing the TURN relay"
    Unregister-TurnBootTask
    Remove-TurnFirewallRules
    Ok "Removed the FleetHub firewall rules"

    if ($hasDistro) {
        # Always ask: --unregister deletes the distro's virtual disk irreversibly. Default Yes
        # because it holds nothing but coturn -- unlike the hub tree, which we leave alone.
        if (Prompt-YesNo "Also remove the '$TurnDistro' WSL distro (deletes it and its coturn config)?" -Default Yes) {
            Invoke-Wsl @('--terminate', $TurnDistro) | Out-Null
            $rm = Invoke-Wsl @('--unregister', $TurnDistro)
            if ($rm.ExitCode -eq 0) { Ok "Unregistered '$TurnDistro'" }
            else { Warn "Could not unregister '$TurnDistro' -- $($rm.Output.Trim())" }
        } else {
            Say "Left '$TurnDistro' in place. Remove it later with:  wsl --unregister $TurnDistro"
        }
    }

    # .wslconfig is deliberately NOT reverted by default: the operator may now depend on
    # mirrored mode for other things, and reverting means a second wsl --shutdown -- another
    # Docker outage during what is supposed to be a cleanup.
    $wslConfig = Join-Path $env:USERPROFILE ".wslconfig"
    if ((Test-Path $wslConfig) -and (Select-String -Path $wslConfig -Pattern 'networkingMode\s*=\s*mirrored' -Quiet)) {
        Warn "Left networkingMode=mirrored in $wslConfig."
        Say  "Remove that line and run 'wsl --shutdown' if you want NAT networking back."
    }
}

function Uninstall-Turn {
    <#
      Standalone TURN teardown. Uninstall-TurnWsl is deliberately silent when TURN was never
      set up -- correct as a step inside the hub uninstall, but confusing as the whole job here,
      so say so explicitly.
    #>
    $wslPresent = [bool](Get-Command wsl.exe -ErrorAction SilentlyContinue)
    $hasTask    = [bool](Get-ScheduledTask -TaskName $TaskTurn -ErrorAction SilentlyContinue)
    $hasDistro  = $false
    if ($wslPresent) {
        $list = Invoke-Wsl @('--list', '--quiet')
        $hasDistro = @($list.Output -split "`r?`n" | ForEach-Object { $_.Trim() } |
                       Where-Object { $_ -eq $TurnDistro }).Count -gt 0
    }
    if (-not $hasTask -and -not $hasDistro) {
        Say "No TURN relay found on this machine (no '$TurnDistro' distro and no '$TaskTurn' task)."
        return
    }
    Uninstall-TurnWsl
    Write-Host "`n  TURN relay removed.`n" -ForegroundColor Green
}

function Resolve-TurnSetup {
    <#
      Ask everything the TURN relay needs and check every precondition, WITHOUT touching the
      machine. Returns a plan hashtable; .Configure is $false when TURN should be skipped.

      Shared by 'Install Hub' (where TURN is an optional add-on) and 'Install TURN' (where it is
      the whole job), so the two can never drift apart in what they ask or what they refuse.
      -Standalone only changes wording and where the secret default comes from; every
      precondition is identical.

      House rule: this never calls Die. Under Install-Hub it runs before the banner that prints
      the once-only enrollment secret, and losing that secret to a TURN problem is unacceptable.
    #>
    param([string]$SecretDefault, [string]$HostDefault, [switch]$Standalone)

    $plan = @{
        Configure = $false; Secret = ""; Generated = $false; PublicHost = ""
        DistroName = $TurnDistro; DistroExists = $false; NeedsWslConfig = $true
        ControlUrl = ""; StunUrl = ""; StunUrls = @(); TurnUrls = @()
    }
    $plan.WslDir = if ($TurnWslLocation) { $TurnWslLocation }
                   else { Join-Path $env:LOCALAPPDATA "FleetHub\wsl\$TurnDistro" }

    if ($SkipTurn) {
        Say "Skipping TURN setup (-SkipTurn)."
        return $plan
    }
    if ($Standalone) {
        # They picked "Install TURN" off the menu; asking whether they want it would be silly.
        $plan.Configure = $true
    } elseif ($TurnHost) {
        $plan.Configure = $true      # supplying the host implies yes, like -EnrollmentSecret does
    } else {
        $plan.Configure = Prompt-YesNo "Configure this hub as the TURN/STUN server for remote control?" -Default Yes
    }
    if (-not $plan.Configure) { return $plan }

    # ---- Shared secret ----
    if ($TurnSecret) { $SecretDefault = $TurnSecret }
    $gen = $false
    if (-not $SecretDefault) { $SecretDefault = New-RandomSecret; $gen = $true }
    $secretHint = if ($Standalone) {
        "  TURN shared secret (must match REMOTE_TURN_SECRET in the hub's .env)"
    } else {
        "  TURN shared secret (blank = keep generated; paste an existing coturn secret to match it)"
    }
    $plan.Secret    = Prompt-Value $secretHint $SecretDefault -Secret
    $plan.Generated = ($gen -and $plan.Secret -eq $SecretDefault)

    if ($TurnHost) { $HostDefault = $TurnHost }
    $plan.PublicHost = Prompt-Value "  Public IP (or hostname) clients reach the TURN server on" $HostDefault `
                           -Required -ValidateHint "Enter the public IP address (preferred) or a resolvable hostname."
    $planLanIp = Get-HostLanIPv4
    $plan.ControlUrl = "turn:$($plan.PublicHost):$TurnPort"
    # STUN gets the same two-vantage treatment as TURN, and for the same reason: a machine on
    # this LAN cannot ask the PUBLIC hostname what its address looks like from outside without
    # hairpinning off the router, so with only that URL it produces no server-reflexive candidate
    # at all. The LAN entry is answered by the same coturn on its LAN address. The hub hands each
    # peer whichever of the two it can actually reach (hub/remote.py select_urls_for_peer).
    $plan.StunUrls   = @("stun:$($plan.PublicHost):$TurnPort")
    if ($planLanIp -and $planLanIp -ne $plan.PublicHost) {
        $plan.StunUrls += "stun:${planLanIp}:$TurnPort"
    }
    $plan.StunUrl    = $plan.StunUrls[0]
    $plan.TurnUrls   = New-TurnUrlList -PublicHost $plan.PublicHost -LocalIp $planLanIp

    # ---- Preconditions, all asked up front ----
    # Everything the operator needs to decide is settled here, before anything is installed.
    # Nobody should be interrupted twelve minutes into a 1 GB download by a question about
    # restarting their containers.
    $skipReason = $null

    $build = 0
    try { $build = [int](Get-CimInstance Win32_OperatingSystem).BuildNumber } catch { }
    if ($build -lt 22621) {
        # Not [Environment]::OSVersion -- that is subject to manifest-based version lying.
        $skipReason = "this is Windows build $build; mirrored WSL networking needs Windows 11 22H2 (build 22621) or newer"
    }

    $wsl = $null
    if (-not $skipReason) {
        $wsl = Get-WslEnvironment -TargetDistro $plan.DistroName
        if (-not $wsl.ExePresent) {
            $skipReason = "wsl.exe was not found on this machine"
        } elseif (-not $wsl.Version) {
            # wsl.exe exists but --version failed: that is the old inbox component, which has
            # neither --name nor mirrored networking.
            Warn "This looks like the older in-box WSL, which can't do mirrored networking."
            if (Prompt-YesNo "  Run 'wsl --update' now to install the current WSL?" -Default Yes) {
                Invoke-Wsl @('--update') -Stream | Out-Null
                $wsl = Get-WslEnvironment -TargetDistro $plan.DistroName
            }
            if (-not $wsl.Version) { $skipReason = "WSL could not be updated to a version that supports mirrored networking" }
        }
    }
    if (-not $skipReason -and -not $wsl.SupportsNameFlag) {
        $skipReason = "this WSL build's 'wsl --install' has no --name flag; run 'wsl --update' and try again"
    }

    if (-not $skipReason) {
        $plan.DistroExists = $wsl.TargetExists
        if ($plan.DistroExists) {
            Say ""
            Say "A WSL distro named '$($plan.DistroName)' already exists."
            if (-not (Prompt-YesNo "  Reconfigure it in place (keeps the distro, rewrites the coturn config)?" -Default Yes)) {
                $plan.DistroName   = Prompt-Value "  Name for a new TURN distro" "$($TurnDistro)2" -Required
                $plan.WslDir       = Join-Path $env:LOCALAPPDATA "FleetHub\wsl\$($plan.DistroName)"
                $plan.DistroExists = $false
            }
        }
        # Only gate on space when something will actually be downloaded.
        if (-not $plan.DistroExists -and -not (Ensure-FreeSpace $plan.WslDir $TurnMinFreeGB)) {
            $skipReason = "there isn't enough free disk space for the WSL distro"
        }
    }

    if (-not $skipReason) {
        # Is the .wslconfig change even needed? If mirrored is already on, there is no
        # wsl --shutdown and therefore nothing to warn about.
        $wslConfigPath = Join-Path $env:USERPROFILE ".wslconfig"
        $currentMode = $null
        if (Test-Path $wslConfigPath) {
            $m = Select-String -Path $wslConfigPath -Pattern '^\s*networkingMode\s*=\s*(\S+)' -ErrorAction SilentlyContinue |
                 Select-Object -First 1
            if ($m) { $currentMode = $m.Matches[0].Groups[1].Value }
        }
        $plan.NeedsWslConfig = ($currentMode -ne 'mirrored')

        if ($plan.NeedsWslConfig) {
            Say ""
            Warn "Enabling mirrored WSL networking requires 'wsl --shutdown', which restarts the WSL virtual machine."
            Say  "That will stop, right now:"
            foreach ($d in $wsl.OtherDistros) { Say "  - WSL distro: $d" }
            if ($wsl.HasDockerDesktop) { Say "  - Docker Desktop and EVERY running container on this machine" }
            if (-not $wsl.OtherDistros -and -not $wsl.HasDockerDesktop) { Say "  - nothing else; this is the only WSL workload here" }
            Say  "It also changes the networking mode for ALL WSL distros on this box, not just the TURN one."
            Say  "Containers with a restart policy come back on their own; anything else must be started by hand."
            if ($currentMode) { Say "  (networkingMode is currently '$currentMode')" }

            $accepted = $AcceptWslNetworkChange
            if (-not $accepted) {
                $accepted = Prompt-YesNo "Understood -- change WSL networking to mirrored and restart the WSL VM?" -Default No
            }
            if (-not $accepted) { $skipReason = "the WSL networking change was declined" }
        }
    }

    if ($skipReason) {
        Warn "Skipping the TURN relay: $skipReason."
        if (-not $Standalone) { Say "The hub install continues; remote control just won't have a relay yet." }
        Say  "Run coturn on a Linux host or VM instead and point Settings -> Remote Control at it"
        Say  "-- see turn\README.md 'Host-OS notes'."
        $plan.Configure = $false
    }
    return $plan
}

function Install-Turn {
    <#
      Standalone TURN install: this machine becomes the WebRTC relay and nothing else. Useful
      when the hub lives elsewhere (a Linux box, another Windows server) but you want the relay
      on a host with a good public address -- previously that meant installing a hub you did
      not want just to get coturn.

      Unlike the hub path this MAY Die, because there is no once-only secret at risk here: TURN
      is the whole job, so a hard failure with a clear message beats a green "done" that lies.
    #>
    Write-Host @"

  FleetHub - TURN Relay Installer
  Installs coturn in a dedicated Ubuntu WSL2 distro ('$TurnDistro').
  This machine becomes the WebRTC relay for remote view/control. No hub is installed.

"@ -ForegroundColor Cyan

    # If a hub happens to live here too, default to ITS secret. On an all-in-one box that makes
    # the two agree by default, which is the desync this whole feature keeps tripping over.
    $hubEnvPath   = Join-Path $HubInstallDefault ".env"
    $secretDefault = (Read-DotEnv $hubEnvPath)["REMOTE_TURN_SECRET"]
    if ($secretDefault) {
        Say "Found a hub on this machine; defaulting to the shared secret already in its .env."
        Say "  $hubEnvPath"
    } else {
        Say "No hub found on this machine. Copy the secret shown at the end into the hub's"
        Say "REMOTE_TURN_SECRET (or Settings -> Remote Control), or paste the hub's existing one now."
    }

    $plan = Resolve-TurnSetup -SecretDefault $secretDefault -HostDefault (Get-PublicIPv4) -Standalone
    if (-not $plan.Configure) {
        Warn "Nothing was installed."
        return
    }

    $ok = Install-TurnWsl -Distro $plan.DistroName -Location $plan.WslDir `
              -Secret $plan.Secret -PublicHost $plan.PublicHost -Realm $TurnRealm `
              -Port $TurnPort -MinPort $TurnMinPort -MaxPort $TurnMaxPort `
              -DistroExists $plan.DistroExists -ApplyFirewall (-not $SkipTurnFirewall) `
              -NeedsWslConfigChange $plan.NeedsWslConfig `
              -LanPort $TurnLanPort -LanMinPort $TurnLanMinPort -LanMaxPort $TurnLanMaxPort
    if (-not $ok) {
        Die "The TURN relay was not set up. Fix the cause above and re-run: install.ps1 -Component Turn"
    }

    Test-TurnServer -Distro $plan.DistroName -Secret $plan.Secret -PublicHost $plan.PublicHost `
                    -Port $TurnPort -HubEnvPath $hubEnvPath

    Write-Host @"

  Done.

  Relay host  : $($plan.PublicHost)
  Distro      : $($plan.DistroName)   (wsl -d $($plan.DistroName) -u root -- systemctl status coturn)
  Config      : /etc/turnserver.conf      (public listener, port $TurnPort)
                /etc/turnserver-lan.conf  (LAN listener, port $TurnLanPort)
  Logs        : wsl -d $($plan.DistroName) -u root -- tail -f /var/log/coturn/turn.log
                wsl -d $($plan.DistroName) -u root -- tail -f /var/log/coturn/turn-lan.log
  Boot task   : $TaskTurn

  Forward these to this host on your router:
    $TurnPort/udp, $TurnPort/tcp and $TurnMinPort-$TurnMaxPort/udp
  Do NOT forward $TurnLanPort or $TurnLanMinPort-$TurnLanMaxPort -- the LAN listener is for
  machines inside this network and hands out LAN relay addresses that mean nothing outside it.

  Point the hub at this relay -- Settings -> Remote Control:
    TURN URLs: $($plan.TurnUrls -join "`n               ")
    STUN URLs: $($plan.StunUrls -join "`n               ")
    Secret   : $($plan.Secret)

  Add all the TURN URLs, in that order. The first covers peers out on the internet; the
  second is the same listener over TCP, for networks that drop UDP; the third lets a peer
  inside this network reach the relay without hairpinning off the router; and the fourth
  keeps traffic between two machines inside this network on the LAN. The hub hands each
  machine only the ones it can reach, from the address it sees that machine arriving from.

  That secret must match REMOTE_TURN_SECRET in the hub's .env exactly, or every
  allocation fails with 401 and remote sessions never connect.

  Uninstall: powershell -ExecutionPolicy Bypass -File install.ps1 -Component Turn -Uninstall

"@ -ForegroundColor Green
}

function Install-Hub {
    # Resolve where the hub will live, then lay down just the runtime files. No git
    # clone and no Git dependency: self-update now pulls the same zip (see app.py).
    # An explicit -HubInstallDir / -InstallDir is an answer, not a default to re-ask.
    if ($HubInstallDir) {
        $hubDir = $HubInstallDir.TrimEnd('\')
    } else {
        $hubDir = (Prompt-Value "Hub install location" $HubInstallDefault).TrimEnd('\')
    }

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
    & $pythonExe -m pip install -r (Join-Path $hubDir "$HubCodeSubdir\requirements.txt") --quiet
    if ($LASTEXITCODE -ne 0) { Die "pip install failed." }
    Ok "Dependencies installed"

    Step "Configuring .env"
    Say "Press Enter to keep the value shown in [brackets]."
    # Sign-in providers. Either or both; the hub refuses to boot with neither. Google is
    # still the default answer because it is what most existing hubs run, but it is no
    # longer forced -- an organisation on Entra/Okta/Authentik should not have to stand up a
    # Google project just to satisfy the installer.
    $hadGoogle = [bool]$existing["GOOGLE_CLIENT_ID"]
    $hadOidc   = [bool]$existing["OIDC_CLIENT_ID"]
    # Default each answer to what this hub is already configured with, so re-running the
    # installer over an existing .env keeps the current setup on a straight Enter.
    $wantGoogle = Prompt-YesNo "Configure Google sign-in?" -Default $(if ($hadOidc -and -not $hadGoogle) { "No" } else { "Yes" })
    $googleId = ""; $googleSecret = ""
    if ($wantGoogle) {
        $googleId     = Prompt-Value "  Google OAuth client ID" $existing["GOOGLE_CLIENT_ID"] -Required
        $googleSecret = Prompt-Value "  Google OAuth client secret" $existing["GOOGLE_CLIENT_SECRET"] -Secret -Required
    }

    $wantOidc = Prompt-YesNo "Configure another SSO provider (Microsoft Entra, Okta, Authentik, Keycloak)?" `
                    -Default $(if ($hadOidc) { "Yes" } else { "No" })
    $oidcName = ""; $oidcIssuer = ""; $oidcId = ""; $oidcSecret = ""
    if ($wantOidc) {
        $oidcNameDefault = $existing["OIDC_DISPLAY_NAME"]
        if (-not $oidcNameDefault) { $oidcNameDefault = "Microsoft" }
        $oidcName   = Prompt-Value "  Name to show on the sign-in button" $oidcNameDefault -Required
        # The issuer URL, not the discovery document: the hub appends
        # /.well-known/openid-configuration itself, and pasting the full discovery URL also
        # works (OIDC_METADATA_URL). Entra's is
        # https://login.microsoftonline.com/<tenant-id>/v2.0
        $oidcIssuer = Prompt-Value "  Issuer URL" $existing["OIDC_ISSUER"] `
                        -Required -Validate { param($v) $v -match '^https?://' } `
                        -ValidateHint "Enter the issuer URL, e.g. https://login.microsoftonline.com/<tenant-id>/v2.0"
        $oidcId     = Prompt-Value "  Client ID" $existing["OIDC_CLIENT_ID"] -Required
        $oidcSecret = Prompt-Value "  Client secret" $existing["OIDC_CLIENT_SECRET"] -Secret -Required
    }
    if (-not $wantGoogle -and -not $wantOidc) {
        Die "At least one sign-in provider is required -- the hub will not start without one."
    }

    $flaskSecretDefault = $existing["FLASK_SECRET_KEY"]
    if (-not $flaskSecretDefault) { $flaskSecretDefault = New-RandomSecret }
    $flaskSecret   = Prompt-Value "Flask session secret key" $flaskSecretDefault -Secret -Required

    $allowedEmails = Prompt-Value "Allowed Google emails (comma-separated)" $existing["ALLOWED_EMAILS"] `
                        -Required -Validate { param($v) $v -match '@' } `
                        -ValidateHint "Enter at least one email address (must contain '@')."

    $hubUrlDefault = $existing["HUB_URL"]
    if (-not $hubUrlDefault) { $hubUrlDefault = "https://your.domain.com" }
    $hubUrlValue   = Prompt-Value "Public hub URL" $hubUrlDefault `
                        -Required -Validate { param($v) $v -match '^https?://' } `
                        -ValidateHint "Enter a full URL starting with http:// or https://."

    # Printed only once the public URL is known -- a redirect URI is the one setting the
    # operator has to go and paste into the provider's console, and getting it wrong is the
    # single most common reason a fresh sign-in fails.
    $redirectBase = $hubUrlValue.TrimEnd("/")
    if ($wantGoogle) { Say "  Google redirect URI: $redirectBase/auth/callback" }
    if ($wantOidc)   { Say "  $oidcName redirect URI: $redirectBase/auth/oidc/callback" }

    Say ""
    Say "Fleet command channel (optional -- leave blank to keep telemetry-only):"
    $enrollSecretDefault = $existing["AGENT_ENROLLMENT_SECRET"]
    $enrollGen = $false
    if (-not $enrollSecretDefault) {
        if (Prompt-YesNo "No AGENT_ENROLLMENT_SECRET set. Auto-generate one?" -Default Yes) {
            $enrollSecretDefault = New-RandomSecret
            $enrollGen = $true
        }
    }
    $enrollSecret  = Prompt-Value "  Agent enrollment secret" $enrollSecretDefault -Secret

    # Was this run the one that generated the enrollment secret (vs. reused/typed)? Only a
    # freshly generated secret gets shown in the "save these now" box at the end.
    $enrollGenerated = ($enrollGen -and $enrollSecret -and $enrollSecret -eq $enrollSecretDefault)

    Say ""
    # Self-update works in both layouts: a files-only install pulls the branch archive
    # and replaces the runtime file set; a clone still uses git. See perform_hub_update().
    $autoUpdateDefault = $existing["HUB_AUTO_UPDATE"]
    if (-not $autoUpdateDefault) {
        if (Prompt-YesNo "Enable hub self-update from main (downloads and replaces hub files)?" -Default No) {
            $autoUpdateDefault = "1"
        }
    }

    # ---- Remote view/control TURN server (roadmap #2) ----
    # WebRTC needs a relay when agent and browser sit behind different NATs; the hub is that
    # relay (coturn), and the hub app mints its credentials from REMOTE_TURN_SECRET. Opting in
    # here generates/keeps the shared secret and, after the service is up, writes turn\.env,
    # optionally brings coturn up with Docker, and seeds the STUN/TURN URLs into Settings.
    Say ""
    Say "Remote view/control (optional -- this hub can be the WebRTC TURN relay):"
    Say "(Skip this if the relay lives on another machine -- install it there with"
    Say " install.ps1 -Component Turn, then point Settings -> Remote Control at it.)"
    $turnHostDefault = try { ([System.Uri]$hubUrlValue).Host } catch { "" }
    $turnPlan = Resolve-TurnSetup -SecretDefault $existing["REMOTE_TURN_SECRET"] -HostDefault $turnHostDefault

    $configureTurn      = $turnPlan.Configure
    $turnSecret         = $turnPlan.Secret
    $turnGenerated      = $turnPlan.Generated
    $turnHost           = $turnPlan.PublicHost
    $turnControlUrl     = $turnPlan.ControlUrl
    $turnUrlList        = $turnPlan.TurnUrls
    $stunControlUrl     = $turnPlan.StunUrl
    $stunUrlList        = $turnPlan.StunUrls
    $turnDistroName     = $turnPlan.DistroName
    $turnWslDir         = $turnPlan.WslDir
    $turnDistroExists   = $turnPlan.DistroExists
    $turnNeedsWslConfig = $turnPlan.NeedsWslConfig

    $lines = @(
        "FLASK_SECRET_KEY=$flaskSecret"
        "ALLOWED_EMAILS=$allowedEmails"
        "HUB_URL=$hubUrlValue"
    )
    # Only write the keys that were actually configured. A blank GOOGLE_CLIENT_ID= line
    # would read as "Google is configured" to a naive check; the hub tests for a non-empty
    # value, but leaving the key out entirely is what makes the .env honest about what
    # this hub does.
    if ($wantGoogle) {
        $lines += "GOOGLE_CLIENT_ID=$googleId"
        $lines += "GOOGLE_CLIENT_SECRET=$googleSecret"
    }
    if ($wantOidc) {
        $lines += "OIDC_DISPLAY_NAME=$oidcName"
        $lines += "OIDC_ISSUER=$oidcIssuer"
        $lines += "OIDC_CLIENT_ID=$oidcId"
        $lines += "OIDC_CLIENT_SECRET=$oidcSecret"
    }
    # Preserve an operator-tuned session lifetime across re-installs; the hub defaults to 7.
    if ($existing["SESSION_LIFETIME_DAYS"]) {
        $lines += "SESSION_LIFETIME_DAYS=$($existing['SESSION_LIFETIME_DAYS'])"
    }
    if ($enrollSecret)      { $lines += "AGENT_ENROLLMENT_SECRET=$enrollSecret" }
    if ($autoUpdateDefault) { $lines += "HUB_AUTO_UPDATE=$autoUpdateDefault" }
    if ($turnSecret)        { $lines += "REMOTE_TURN_SECRET=$turnSecret" }
    # Write WITHOUT a BOM: PowerShell 5.1's `Set-Content -Encoding UTF8` prepends a UTF-8 BOM,
    # which python-dotenv folds into the first key (﻿GOOGLE_CLIENT_ID) so the hub reads its
    # config as unset and crash-loops. UTF8Encoding($false) = no BOM.
    [System.IO.File]::WriteAllLines($envPath, [string[]]$lines, (New-Object System.Text.UTF8Encoding($false)))
    Ok "Wrote $envPath"

    if ($turnControlUrl) {
        # Seed the STUN/TURN URLs into the settings DB BEFORE the service starts. The hub's
        # settings cache is per-process and has no cross-process versioning, so a seed written
        # while it runs would stay invisible until a restart -- seeding first means the fresh
        # process reads it on boot. Idempotent: settings.init_settings_db + set_many just upsert.
        Step "Seeding remote-control TURN/STUN URLs into Settings"
        $codeDir = Join-Path $hubDir $HubCodeSubdir
        $logDir  = Join-Path $hubDir "logs"
        if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
        $dbPath  = Join-Path $logDir "temp_v2.db"
        $seedPy  = Join-Path $env:TEMP "fleethub_seed_turn.py"
        # Both lists are newline-separated: there are up to four TURN URLs (public UDP / public
        # TCP / LAN-reached public / LAN-only) and up to two STUN, and passing a list through
        # argv as one argument beats trying to make argv count variable across PowerShell and
        # python.
        $seedSrc = @'
import sys
code_dir, db_path, turn_urls, stun_urls = sys.argv[1:5]
sys.path.insert(0, code_dir)
import settings
settings.init_settings_db(db_path)
split = lambda blob: [u.strip() for u in blob.split("\n") if u.strip()]
settings.set_many(db_path, {"remote.turn_urls": split(turn_urls),
                            "remote.stun_urls": split(stun_urls)})
print("ok")
'@
        [System.IO.File]::WriteAllText($seedPy, $seedSrc, (New-Object System.Text.UTF8Encoding($false)))
        try {
            & $pythonExe $seedPy $codeDir $dbPath ($turnUrlList -join "`n") ($stunUrlList -join "`n") | Out-Null
            if ($LASTEXITCODE -eq 0) { Ok "Settings -> Remote Control: TURN=$($turnUrlList -join ', ')  STUN=$($stunUrlList -join ', ')" }
            else { Warn "Could not seed TURN URLs (exit $LASTEXITCODE) -- set them in Settings -> Remote Control." }
        } catch {
            Warn "Could not seed TURN URLs ($($_.Exception.Message)) -- set them in Settings -> Remote Control."
        } finally {
            Remove-Item $seedPy -Force -ErrorAction SilentlyContinue
        }
    }

    Step "Installing the $HubServiceName service"
    # Serve via waitress; prefer its console script, fall back to `python -m waitress`.
    #
    # --threads is NOT optional here, and waitress's default of 4 is far too low for this app.
    # The hub speaks Socket.IO in engineio's `threading` async_mode over the polling transport
    # (app.py: async_mode="threading", transports=["polling"], allow_upgrades=False -- waitress
    # cannot do WebSockets, so that is deliberate). In that mode a poll is served by BLOCKING a
    # worker thread on a queue until a packet arrives or the ping cycle expires (~25s). Both
    # index.html and machine.html open a socket, so every dashboard/machine tab an operator
    # leaves open parks one worker more or less permanently.
    #
    # At the default 4 that left ~3 threads for the entire rest of the hub -- static assets,
    # agent heartbeats, PTY polls (~7 req/s per active terminal) -- and a fast page reload
    # would stall: the reloaded page's new socket connects before the hub notices the old
    # one's parked poll is dead, and everything else queues in the backlog until a poll times
    # out. The threads are blocked on a queue rather than burning CPU, so a generous pool is
    # nearly free; size it by expected concurrent operator TABS, not by request rate.
    #
    # Command PUSH parks a worker the same way, and per MACHINE rather than per tab: an agent
    # holding GET /api/agent/commands open is what lets a command start the moment it is
    # issued instead of at that machine's next poll (see fleet.py's COMMAND PUSH block). The
    # hub caps how many it will hold -- Settings -> Fleet -> "Push commands to at most",
    # default 8 -- and that cap only means anything if the pool it is drawn from has room to
    # spare. Hence 128 rather than 32: it is what makes raising that setting toward the size
    # of a real fleet a supported thing to do rather than a way to hang the console.
    $HubThreads  = 128
    $scriptsDir  = Join-Path (Split-Path $pythonExe -Parent) "Scripts"
    $waitressExe = Join-Path $scriptsDir "waitress-serve.exe"
    if (Test-Path $waitressExe) {
        $hubExec = $waitressExe
        $hubArgs = "--host=0.0.0.0 --port=$HubPort --threads=$HubThreads wsgi:application"
    } else {
        $hubExec = $pythonExe
        $hubArgs = "-m waitress --host=0.0.0.0 --port=$HubPort --threads=$HubThreads wsgi:application"
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

    # WinSW runs as LocalSystem by default (matches the old SYSTEM task). waitress runs with
    # <workingdirectory> at the code dir ($hubDir\hub) so `wsgi:application` imports; app.py then
    # resolves .env and logs\ from the install root one level up (STATE_ROOT), which is $hubDir --
    # exactly where this installer writes them. onfailure=restart is also what the hub self-update
    # relies on: restart_hub() exits non-zero, WinSW relaunches within ~5s.
    $xml = @"
<service>
  <id>$HubServiceId</id>
  <name>$HubServiceName</name>
  <description>FleetHub hub (Flask/Socket.IO via waitress).</description>
  <executable>$([System.Security.SecurityElement]::Escape($hubExec))</executable>
  <arguments>$([System.Security.SecurityElement]::Escape($hubArgs))</arguments>
  <workingdirectory>$([System.Security.SecurityElement]::Escape((Join-Path $hubDir $HubCodeSubdir)))</workingdirectory>
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

    # ---- Bring up the coturn TURN server (roadmap #2) ----
    # Native coturn in a dedicated WSL2 distro. Everything it needs is generated inline, so
    # unlike the old Docker path this works identically for `irm | iex` runs with no $PSScriptRoot.
    $turnOk = $false
    if ($configureTurn) {
        $turnOk = Install-TurnWsl -Distro $turnDistroName -Location $turnWslDir `
                      -Secret $turnSecret -PublicHost $turnHost -Realm $TurnRealm `
                      -Port $TurnPort -MinPort $TurnMinPort -MaxPort $TurnMaxPort `
                      -DistroExists $turnDistroExists -ApplyFirewall (-not $SkipTurnFirewall) `
                      -NeedsWslConfigChange $turnNeedsWslConfig `
                      -LanPort $TurnLanPort -LanMinPort $TurnLanMinPort -LanMaxPort $TurnLanMaxPort
        if ($turnOk) {
            Test-TurnServer -Distro $turnDistroName -Secret $turnSecret -PublicHost $turnHost `
                            -Port $TurnPort -HubEnvPath $envPath
        } else {
            Warn "The TURN relay was not fully set up. The hub itself is fine."
            Say  "Fix the cause above and re-run:  install.ps1 -Component Hub"
        }
        Say ""
        Say "Forward these to this host on your router so remote agents can reach the relay:"
        Say "  $TurnPort/udp, $TurnPort/tcp and $TurnMinPort-$TurnMaxPort/udp"
        Say "Do NOT forward $TurnLanPort or $TurnLanMinPort-$TurnLanMaxPort -- that listener serves"
        Say "machines inside this network and its relay addresses mean nothing outside it."
    }

    Write-Host @"

  Done.

  Hub URL (local) : http://localhost:$HubPort/
  Hub URL (public): $hubUrlValue
  Location        : $hubDir
  Config          : $envPath
  Service         : $HubServiceName  (Get-Service $HubServiceId)

  Uninstall: powershell -ExecutionPolicy Bypass -File install.ps1 -Component Hub -Uninstall

"@ -ForegroundColor Green

    if ($turnOk) {
        Write-Host @"
  TURN relay      : coturn in WSL distro '$turnDistroName'  ($turnControlUrl)
    status        : wsl -d $turnDistroName -u root -- systemctl status coturn coturn-lan
    logs          : wsl -d $turnDistroName -u root -- journalctl -u coturn -u coturn-lan -f
    config        : wsl -d $turnDistroName -u root -- nano /etc/turnserver.conf
                    wsl -d $turnDistroName -u root -- nano /etc/turnserver-lan.conf
    boot task     : $TaskTurn
    TURN URLs     : $($turnUrlList -join "`n                    ")

"@ -ForegroundColor Green
    }

    # Show any secret this run GENERATED, exactly once. They live in .env (masked on re-run), so
    # this is the operator's only chance to copy them somewhere durable -- the enrollment secret
    # is needed to enrol agents, and the TURN secret must be set as coturn's --static-auth-secret.
    $generated = @()
    if ($enrollGenerated -and $enrollSecret) { $generated += ,@("AGENT_ENROLLMENT_SECRET", $enrollSecret, "enrol agents with this") }
    if ($turnGenerated   -and $turnSecret)   { $generated += ,@("REMOTE_TURN_SECRET",      $turnSecret,   "already set in the TURN distro's /etc/turnserver.conf") }
    if ($generated.Count) {
        Write-Host "  Save these now -- generated this run, shown only once:" -ForegroundColor Yellow
        Write-Host "  (also stored in $envPath; masked on any re-run)" -ForegroundColor DarkGray
        foreach ($g in $generated) {
            Write-Host ("    {0} = {1}" -f $g[0], $g[1]) -ForegroundColor Yellow
            Write-Host ("      -> {0}" -f $g[2]) -ForegroundColor DarkGray
        }
        Write-Host ""
    }
}

# ----------------------------------------------------------------------
# Resolve which component to act on, then dispatch
# ----------------------------------------------------------------------
$exitRequested = $false

if (-not $Component) {
    if ($Uninstall) {
        # Back-compat: bare -Uninstall (no -Component) matches the documented
        # legacy behavior of uninstalling the companion.
        $Component = "Companion"
    } else {
        # Resolve the menu through a plain local, NOT through $Component: the param's
        # [ValidateSet] attribute stays attached to $Component for the life of the variable,
        # so assigning a menu-only sentinel ("UninstallMenu", "Exit") to it throws
        # ValidateSetFailure before we ever get to dispatch. Only real component names --
        # the ones the set allows -- are ever copied across.
        $selection = Show-Menu
        if ($selection -eq "UninstallMenu") {
            $selection = Show-UninstallMenu
            $Uninstall = $true
        }
        if ($selection -eq "Exit" -or -not $selection) { $exitRequested = $true }
        else { $Component = $selection }
    }
}

# Exit chosen from a menu: end the script gracefully but DON'T `exit` -- that would kill the
# (elevated) console the user wants to keep open to read the log. `return` at script scope
# stops here and, with the relaunch's -NoExit, leaves them at a live prompt.
if ($exitRequested -or -not $Component) {
    Write-Host "`n  Exiting installer. This window stays open so you can review the log above." -ForegroundColor Cyan
    return
}

try {
    switch ($Component) {
        "Agent"     { if ($Uninstall) { Uninstall-Agent }     else { Install-Agent } }
        # Companion is uninstall-only: the Python agent is gone from the repo, so there is
        # nothing left to install -- but legacy machines still need a way to be cleaned up.
        "Companion" { if ($Uninstall) { Uninstall-Companion } else { Die "The Companion has been removed. Install the Agent instead: -Component Agent" } }
        "Hub"       { if ($Uninstall) { Uninstall-Hub }       else { Install-Hub } }
        "Turn"      { if ($Uninstall) { Uninstall-Turn }      else { Install-Turn } }
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
