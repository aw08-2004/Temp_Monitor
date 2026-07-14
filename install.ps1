<#
    Temp_Monitor - Companion Agent Installer
    https://github.com/aw08-2004/Temp_Monitor

    Installs:
      - Python (via winget) if missing, plus the 'requests' package
      - LibreHardwareMonitor (latest release), configured to run its web server on :8085
      - companion.py, pulled from main
      - Two scheduled tasks that start both at logon with admin rights

    Usage (right-click > Run with PowerShell, or):
        powershell -ExecutionPolicy Bypass -File install.ps1
        powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
#>

param(
    [switch]$Uninstall,
    [string]$InstallDir = "C:\Program Files\TempMonitor",
    [int]$Port = 8085
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Repo           = "aw08-2004/Temp_Monitor"
$InstallerUrl   = "https://raw.githubusercontent.com/$Repo/main/install.ps1"
$CompanionUrl   = "https://raw.githubusercontent.com/$Repo/main/companion.py"
$LhmApi         = "https://api.github.com/repos/LibreHardwareMonitor/LibreHardwareMonitor/releases/latest"
$LhmFallback    = "https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/download/v0.9.6/LibreHardwareMonitor.zip"
$PythonFallback = "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe"
$PawnIoUrl      = "https://raw.githubusercontent.com/LibreHardwareMonitor/LibreHardwareMonitor/refs/heads/master/LibreHardwareMonitor.Windows.Forms/Resources/PawnIO_setup.exe"
$LhmDir         = Join-Path $InstallDir "LibreHardwareMonitor"
$TaskLhm        = "TempMonitor - LibreHardwareMonitor"
$TaskCompanion  = "TempMonitor - Companion"

function Say($msg)  { Write-Host "  $msg" }
function Ok($msg)   { Write-Host "  [ok] $msg"   -ForegroundColor Green }
function Warn($msg) { Write-Host "  [!!] $msg"   -ForegroundColor Yellow }
function Die($msg)  { Write-Host "  [xx] $msg"   -ForegroundColor Red; exit 1 }
function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }

# ----------------------------------------------------------------------
# Elevate: LHM needs admin to read sensors, and tasks need admin to register
# ----------------------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host "Elevating..." -ForegroundColor Yellow

    if ($PSCommandPath) {
        # Running from a local file -- relaunch that same file
        $argList = @("-ExecutionPolicy","Bypass","-File","`"$PSCommandPath`"")
        if ($Uninstall) { $argList += "-Uninstall" }
        Start-Process powershell -Verb RunAs -ArgumentList $argList
    } else {
        # Running via `irm | iex` -- no script file to relaunch, so re-fetch
        # and re-invoke as a scriptblock (preserves param binding) in the
        # elevated process.
        $remoteArgs = foreach ($key in $PSBoundParameters.Keys) {
            $val = $PSBoundParameters[$key]
            if ($val -is [switch]) { if ($val.IsPresent) { "-$key" } }
            else { "-$key"; "`"$val`"" }
        }
        $cmd = "& ([scriptblock]::Create((irm '$InstallerUrl'))) $($remoteArgs -join ' ')"
        Start-Process powershell -Verb RunAs -ArgumentList @("-ExecutionPolicy","Bypass","-Command", $cmd)
    }
    exit
}

# ----------------------------------------------------------------------
# Uninstall
# ----------------------------------------------------------------------
if ($Uninstall) {
    Step "Uninstalling Temp Monitor"

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
    exit
}

Write-Host @"

  Temp Monitor - Companion Agent Installer
  Machine: $env:COMPUTERNAME
  Target : $InstallDir

"@ -ForegroundColor Cyan

# ----------------------------------------------------------------------
# 1. Python
# ----------------------------------------------------------------------
Step "Checking Python"

function Resolve-Python {
    foreach ($cmd in @("py -3", "python")) {
        $exe, $args = $cmd -split " ", 2
        if (Get-Command $exe -ErrorAction SilentlyContinue) {
            try {
                $v = & $exe $args --version 2>&1
                if ($v -match "Python 3") { return @{ Exe = (Get-Command $exe).Source; Args = $args; Version = "$v" } }
            } catch { }
        }
    }
    return $null
}

$py = Resolve-Python
if (-not $py) {
    Warn "Python not found."

    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Say "Installing via winget..."
        # Pin --source winget: without it, winget also probes the msstore
        # source, and a bad msstore cert/network on the machine aborts the
        # whole install even though the winget source works fine.
        winget install --id Python.Python.3.12 --source winget --scope machine `
            --silent --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) { Warn "winget install failed (exit $LASTEXITCODE)." }

        $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
                    [Environment]::GetEnvironmentVariable("Path","User")
        $py = Resolve-Python
    } else {
        Warn "winget is unavailable."
    }

    if (-not $py) {
        Say "Falling back to direct download from python.org..."
        $pyInstaller = Join-Path $env:TEMP "python-installer.exe"
        try {
            Invoke-WebRequest -Uri $PythonFallback -OutFile $pyInstaller -UseBasicParsing
            $proc = Start-Process -FilePath $pyInstaller -Wait -PassThru -ArgumentList `
                "/quiet", "InstallAllUsers=1", "PrependPath=1", "Include_test=0"
            if ($proc.ExitCode -ne 0) { Warn "python.org installer exited with code $($proc.ExitCode)." }
        } catch {
            Warn "Direct download failed: $_"
        } finally {
            Remove-Item $pyInstaller -Force -ErrorAction SilentlyContinue
        }

        $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
                    [Environment]::GetEnvironmentVariable("Path","User")
        $py = Resolve-Python
    }

    if (-not $py) {
        Die "Python still not on PATH. Reboot and re-run the installer, or install Python 3 manually from python.org (tick 'Add to PATH')."
    }
}
Ok "Found $($py.Version)"

# Resolve the real interpreter path (so scheduled tasks don't depend on PATH)
$pythonExe = & $py.Exe $py.Args -c "import sys; print(sys.executable)"
$pythonwExe = Join-Path (Split-Path $pythonExe) "pythonw.exe"   # windowless, no console popup
if (-not (Test-Path $pythonwExe)) { $pythonwExe = $pythonExe }
Ok "Interpreter: $pythonExe"

Step "Installing Python packages"
& $pythonExe -m pip install --upgrade pip --quiet
& $pythonExe -m pip install requests --quiet
if ($LASTEXITCODE -ne 0) { Die "pip install failed." }
Ok "requests installed"

# ----------------------------------------------------------------------
# 2. Files
# ----------------------------------------------------------------------
Step "Setting up $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
New-Item -ItemType Directory -Force -Path $LhmDir     | Out-Null
Ok "Directories ready"

# ----------------------------------------------------------------------
# 3. LibreHardwareMonitor
# ----------------------------------------------------------------------
Step "Installing LibreHardwareMonitor"

$lhmExe = Join-Path $LhmDir "LibreHardwareMonitor.exe"

if (Test-Path $lhmExe) {
    Ok "Already present, skipping download"
} else {
    $zipUrl = $LhmFallback
    try {
        $rel = Invoke-RestMethod -Uri $LhmApi -Headers @{ "User-Agent" = "TempMonitor-Installer" } -TimeoutSec 15
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

# ----------------------------------------------------------------------
# 4. PawnIO -- kernel driver LHM needs for sensor access (replaces WinRing0)
# ----------------------------------------------------------------------
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

# ----------------------------------------------------------------------
# 5. LHM config -- web server ON, start minimized, live in the tray
#    LHM reads <exe name>.config from its own folder (PersistentSettings)
# ----------------------------------------------------------------------
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

# ----------------------------------------------------------------------
# 6. companion.py
# ----------------------------------------------------------------------
Step "Downloading companion.py"
$companionPath = Join-Path $InstallDir "companion.py"
Invoke-WebRequest -Uri $CompanionUrl -OutFile $companionPath -UseBasicParsing
$ver = (Select-String -Path $companionPath -Pattern '^VERSION\s*=\s*"([\d.]+)"').Matches.Groups[1].Value
Ok "companion.py v$ver -> $companionPath"

# ----------------------------------------------------------------------
# 7. Scheduled tasks (RunLevel Highest = admin without a UAC prompt every logon)
# ----------------------------------------------------------------------
Step "Registering scheduled tasks"

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
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
    -Description "Hardware sensor daemon for Temp Monitor. Serves JSON on localhost:$Port." | Out-Null
Ok "Task: $TaskLhm"

# Companion 30s later, so LHM's web server is up
$trigger = New-ScheduledTaskTrigger -AtLogOn
$trigger.Delay = "PT30S"
Register-ScheduledTask -TaskName $TaskCompanion -Force `
    -Action    (New-ScheduledTaskAction -Execute $pythonwExe -Argument "`"$companionPath`"" -WorkingDirectory $InstallDir) `
    -Trigger   $trigger `
    -Principal $principal -Settings $settings `
    -Description "Reports CPU temperature to the Temp Monitor hub." | Out-Null
Ok "Task: $TaskCompanion (30s delay)"

# ----------------------------------------------------------------------
# 8. Start and verify
# ----------------------------------------------------------------------
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
    $temps = @()
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
  Hub     : https://temp.arkeanos.net
  Files   : $InstallDir

  companion.py updates itself from GitHub on every start, and weekly if left running.
  Uninstall: powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall

"@ -ForegroundColor Green
