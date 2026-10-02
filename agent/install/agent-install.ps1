<#
    FleetHub - C#/.NET Fleet Agent Installer
    https://github.com/aw08-2004/Temp_Monitor

    Installs the fleet agent as a Windows Service running under LocalSystem:
      - PawnIO kernel driver (needed by the in-process LibreHardwareMonitorLib for
        sensor access; skipped if already present)
      - The self-contained agent exe -> C:\Program Files\TempMonitorAgent
      - A Windows Service "TempMonitorAgent" (Automatic), with SCM failure-recovery
        set to restart -- this is what the agent's self-update relies on when it
        exits with code 17 to swap onto a new binary.
      - The one-time enrollment secret into HKLM\SOFTWARE\TempMonitorAgent

    Unlike the removed Python companion (a per-logon Scheduled Task in a user session),
    this runs in session 0 as SYSTEM, so restart/rename/gpupdate/scripts work with no one
    logged in.

    With neither -AgentUrl nor -AgentExe, the binary is found the way the agent's own
    SelfUpdater finds one: the Ed25519-signed agent.manifest.json, verified against the fleet
    key, then the download checked against the SIGNED sha256. See "signed manifest" below.

    Usage (elevated PowerShell):
        powershell -ExecutionPolicy Bypass -File agent-install.ps1 -EnrollmentSecret <secret>
        powershell -ExecutionPolicy Bypass -File agent-install.ps1 -AgentExe .\TempMonitorAgent.exe -EnrollmentSecret <secret>
        powershell -ExecutionPolicy Bypass -File agent-install.ps1 `
            -AgentUrl <release-asset-url> -EnrollmentSecret <secret>      # NOT verified
        powershell -ExecutionPolicy Bypass -File agent-install.ps1 -Uninstall
#>

param(
    [switch]$Uninstall,
    [string]$InstallDir = "C:\Program Files\FleetHub\Agent",
    [string]$AgentUrl,                       # download URL for the agent exe -- NOT verified
    [string]$AgentExe,                       # OR a local path to the agent exe -- NOT verified
    [string]$ManifestUrl,                    # signed manifest from a mirror; still verified
    [string]$EnrollmentSecret,               # shared secret for POST /api/agent/enroll
    [string]$HubUrl                          # optional hub base override (FLEETHUB_HUB)
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# The Windows service name is deliberately NOT renamed yet. .NET sets ServiceBase.ServiceName
# from AddWindowsService() in Program.cs, and a self-updating agent swaps its binary without
# re-registering the service -- so renaming one side without the other leaves the registered
# name and the binary's name disagreeing on exactly the machines already in the field.
# Both move together when the assembly is renamed.
$ServiceName    = "TempMonitorAgent"
$ExeName        = "TempMonitorAgent.exe"
$ExePath        = Join-Path $InstallDir $ExeName
$RegPath        = "HKLM:\SOFTWARE\FleetHub\Agent"
$LegacyRegPath  = "HKLM:\SOFTWARE\TempMonitorAgent"
$LegacyInstall  = "C:\Program Files\TempMonitorAgent"
# Agent state, as AgentConfig.ProgramDataDir resolves it: the FleetHub path, and the
# pre-rename one it migrates from. Named here so the uninstall can drop the enrollment
# identity -- everything else under them is logs and is deliberately kept.
$StateDir       = Join-Path $env:ProgramData "FleetHub\Agent"
$LegacyStateDir = Join-Path $env:ProgramData "TempMonitorAgent"
$PawnIoUrl   = "https://raw.githubusercontent.com/LibreHardwareMonitor/LibreHardwareMonitor/refs/heads/master/LibreHardwareMonitor.Windows.Forms/Resources/PawnIO_setup.exe"

function Say($msg)  { Write-Host "  $msg" }
function Ok($msg)   { Write-Host "  [ok] $msg"   -ForegroundColor Green }
function Warn($msg) { Write-Host "  [!!] $msg"   -ForegroundColor Yellow }
function Die($msg)  { Write-Host "  [xx] $msg"   -ForegroundColor Red; exit 1 }
function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }

#region signed-manifest
# ----------------------------------------------------------------------
# Signed manifest
# ----------------------------------------------------------------------
# **The installer applies the check the agent's SelfUpdater applies.** Until this existed a
# first install asked the GitHub releases API for the newest agent-v* asset and ran whatever
# came back as LocalSystem -- so the fleet key, which every self-update verifies fail-closed,
# protected every step EXCEPT the one that first runs the agent. install.sh had the same gap
# and closed it in PR #91 (CWE-494); this is the Windows half, mirrored step for step from
# agent/src/TempMonitorAgent/Update/SelfUpdater.cs:
#   1. fetch agent.manifest.json and its detached .sig, as BYTES;
#   2. verify the Ed25519 signature over those exact bytes with the pinned key;
#   3. only then parse the manifest for version, sha256 and url;
#   4. download url and refuse it unless its sha256 equals the SIGNED value.
# Fail closed at every step: nothing here falls back to an unverified download.
#
# **Why a hand-written Ed25519 verifier.** Windows PowerShell 5.1 runs on .NET Framework,
# which has no Ed25519, and CNG exposes Curve25519 for key agreement only. The agent carries
# BouncyCastle, but on a first install there is no agent yet to borrow it from. So this is
# the RFC 8032 verification equation over System.Numerics.BigInteger: slow (a few hundred
# milliseconds, once) and short enough to review against the RFC line by line.
# tests/test_windows_installer.py proves it agrees with a real implementation on random keys
# and on the production signature committed in this repo.
#
# Rejected: shipping BouncyCastle.dll next to this script (a second download, from the same
# place, to verify the first -- and a binary nobody reviews); verifying the binary's
# Authenticode signature instead (the agent is not Authenticode-signed, and the fleet's trust
# root is the offline Ed25519 key, not a CA); keeping the releases-API lookup and verifying
# afterwards (the manifest already names the one allowed URL, and the API could also hand
# back a beta prerelease as "the newest"); a parameter to supply the key (that would make the
# trust root whatever the person running the command types, which is the thing removed).
#
# This region is loaded on its own by tests/test_windows_installer.py. Keep it free of side
# effects and of the Say/Die helpers above -- failures are thrown, and the caller dies.

# The fleet's release-signing public key. **Must equal AgentConfig.UpdatePublicKeyHex** in
# both agents and UPDATE_PUBLIC_KEY_HEX in install.sh; tests/test_windows_installer.py fails
# if it drifts. Deliberately not a parameter -- see above.
$UpdatePublicKeyHex = "9a4f433e0eb82fae121fdeede7d2ce881d50bc80021236f24fdfa4494fc0537c"

# AgentConfig.StableManifestUrl -- the same file, so a machine is installed from exactly what
# it would later update from. Stable, never beta: a first install is not where to opt in.
$StableManifestUrl = "https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/agent/agent.manifest.json"

$Ed25519Source = @'
using System;
using System.Numerics;
using System.Security.Cryptography;

namespace FleetHubInstaller
{
    // RFC 8032 section 5.1.7, cofactorless: accept iff encode([S]B - [k]A) == R, with S < L
    // and A a valid point. Points are extended coordinates (X, Y, Z, T), section 5.1.4.
    // C# 5 only: Windows PowerShell's Add-Type compiles with the .NET Framework compiler.
    public static class Ed25519
    {
        static readonly BigInteger P = BigInteger.Pow(2, 255) - 19;
        static readonly BigInteger L = BigInteger.Pow(2, 252)
            + BigInteger.Parse("27742317777372353535851937790883648493");
        static readonly BigInteger D = Mod(-121665 * Inv(121666));
        static readonly BigInteger SqrtM1 = BigInteger.ModPow(2, (P - 1) / 4, P);
        static readonly BigInteger[] Base = BasePoint();

        static BigInteger Mod(BigInteger a) { BigInteger r = a % P; return r.Sign < 0 ? r + P : r; }
        static BigInteger Inv(BigInteger a) { return BigInteger.ModPow(Mod(a), P - 2, P); }

        // Little-endian unsigned: BigInteger's byte constructor is two's complement, so a
        // zero byte on top keeps a set high bit from reading as negative.
        static BigInteger FromLE(byte[] b, int off, int len)
        {
            byte[] t = new byte[len + 1];
            Array.Copy(b, off, t, 0, len);
            return new BigInteger(t);
        }

        static BigInteger[] BasePoint()
        {
            BigInteger y = Mod(4 * Inv(5));
            return new[] { RecoverX(y, 0), y, BigInteger.One, Mod(RecoverX(y, 0) * y) };
        }

        // Section 5.1.3. Returns -1 when y does not lie on the curve.
        static BigInteger RecoverX(BigInteger y, int sign)
        {
            if (y >= P) return BigInteger.MinusOne;
            BigInteger x2 = Mod((y * y - 1) * Inv(D * y * y + 1));
            if (x2.IsZero) return sign == 1 ? BigInteger.MinusOne : BigInteger.Zero;
            BigInteger x = BigInteger.ModPow(x2, (P + 3) / 8, P);
            if (!Mod(x * x - x2).IsZero) x = Mod(x * SqrtM1);
            if (!Mod(x * x - x2).IsZero) return BigInteger.MinusOne;
            if ((int)(x & 1) != sign) x = P - x;
            return x;
        }

        static BigInteger[] Decode(byte[] s, int off)
        {
            byte[] b = new byte[32];
            Array.Copy(s, off, b, 0, 32);
            int sign = b[31] >> 7;
            b[31] &= 0x7f;
            BigInteger y = FromLE(b, 0, 32);
            BigInteger x = RecoverX(y, sign);
            if (x.Sign < 0) return null;
            return new[] { x, y, BigInteger.One, Mod(x * y) };
        }

        // The unified addition law; complete on this curve, so it also doubles.
        static BigInteger[] Add(BigInteger[] p, BigInteger[] q)
        {
            BigInteger a = Mod((p[1] - p[0]) * (q[1] - q[0]));
            BigInteger b = Mod((p[1] + p[0]) * (q[1] + q[0]));
            BigInteger c = Mod(2 * p[3] * q[3] * D);
            BigInteger d = Mod(2 * p[2] * q[2]);
            BigInteger e = b - a, f = d - c, g = d + c, h = b + a;
            return new[] { Mod(e * f), Mod(g * h), Mod(f * g), Mod(e * h) };
        }

        static BigInteger[] Mul(BigInteger s, BigInteger[] p)
        {
            BigInteger[] q = { BigInteger.Zero, BigInteger.One, BigInteger.One, BigInteger.Zero };
            while (s.Sign > 0)
            {
                if (!s.IsEven) q = Add(q, p);
                p = Add(p, p);
                s >>= 1;
            }
            return q;
        }

        static byte[] Encode(BigInteger[] p)
        {
            BigInteger zi = Inv(p[2]);
            BigInteger x = Mod(p[0] * zi), y = Mod(p[1] * zi);
            byte[] raw = y.ToByteArray();
            byte[] o = new byte[32];
            Array.Copy(raw, 0, o, 0, Math.Min(32, raw.Length));
            if (!x.IsEven) o[31] |= 0x80;
            return o;
        }

        public static bool Verify(byte[] publicKey, byte[] message, byte[] signature)
        {
            if (publicKey == null || message == null || signature == null) return false;
            if (publicKey.Length != 32 || signature.Length != 64) return false;
            BigInteger[] a = Decode(publicKey, 0);
            if (a == null) return false;
            // S >= L is the malleable twin of a valid signature; RFC 8032 requires refusing it.
            BigInteger s = FromLE(signature, 32, 32);
            if (s >= L) return false;
            byte[] buf = new byte[64 + message.Length];
            Array.Copy(signature, 0, buf, 0, 32);
            Array.Copy(publicKey, 0, buf, 32, 32);
            Array.Copy(message, 0, buf, 64, message.Length);
            byte[] h;
            using (SHA512 sha = SHA512.Create()) h = sha.ComputeHash(buf);
            BigInteger k = FromLE(h, 0, 64) % L;
            BigInteger[] negA = { Mod(-a[0]), a[1], a[2], Mod(-a[3]) };
            byte[] check = Encode(Add(Mul(s, Base), Mul(k, negA)));
            for (int i = 0; i < 32; i++)
                if (check[i] != signature[i]) return false;
            return true;
        }
    }
}
'@

function Initialize-Ed25519 {
    if ('FleetHubInstaller.Ed25519' -as [type]) { return }
    # PowerShell 7 compiles against the whole reference set already; 5.1 needs System.Numerics
    # named, and naming it on 7 is an error. install.ps1 may run this script in either.
    if ($PSVersionTable.PSEdition -eq 'Core') {
        Add-Type -TypeDefinition $Ed25519Source
    } else {
        Add-Type -TypeDefinition $Ed25519Source -ReferencedAssemblies System.Numerics
    }
}

# Hex text -> bytes, or $null for anything that is not an even run of hex digits. Whitespace
# is tolerated because a .sig file may end in a newline; nothing else is.
function ConvertFrom-HexString([string]$hex) {
    if ($null -eq $hex) { return $null }
    $clean = $hex -replace '\s', ''
    if ($clean.Length -eq 0 -or $clean.Length % 2 -ne 0 -or $clean -notmatch '^[0-9a-fA-F]+$') { return $null }
    $bytes = New-Object byte[] ($clean.Length / 2)
    for ($i = 0; $i -lt $bytes.Length; $i++) {
        $bytes[$i] = [Convert]::ToByte($clean.Substring($i * 2, 2), 16)
    }
    return ,$bytes
}

# $true only if $SignatureHex is a valid Ed25519 signature by $UpdatePublicKeyHex over the
# exact bytes in $Message.
function Test-ManifestSignature([byte[]]$Message, [string]$SignatureHex) {
    $key = ConvertFrom-HexString $UpdatePublicKeyHex
    $sig = ConvertFrom-HexString $SignatureHex
    if ($null -eq $Message -or $null -eq $key -or $null -eq $sig) { return $false }
    if ($key.Length -ne 32 -or $sig.Length -ne 64) { return $false }
    Initialize-Ed25519
    return [FleetHubInstaller.Ed25519]::Verify($key, $Message, $sig)
}

# Fetch, verify and download into $Destination, or throw with the reason. $Destination exists
# afterwards only if every check passed.
function Get-VerifiedAgent([string]$Destination, [string]$FromManifestUrl) {
    if (-not $FromManifestUrl) { $FromManifestUrl = $StableManifestUrl }
    # The progress bar on 5.1 costs more than the transfer on an agent-sized download.
    $ProgressPreference = 'SilentlyContinue'
    $work = Join-Path ([IO.Path]::GetTempPath()) ("fleethub-manifest-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $work -Force | Out-Null
    try {
        $mPath = Join-Path $work "manifest.json"
        $sPath = Join-Path $work "manifest.json.sig"
        try {
            # -OutFile, never Invoke-RestMethod: the signature covers bytes, and anything that
            # decodes and re-reads the text is checking something other than what was signed.
            Invoke-WebRequest -Uri $FromManifestUrl -OutFile $mPath -UseBasicParsing
            Invoke-WebRequest -Uri "$FromManifestUrl.sig" -OutFile $sPath -UseBasicParsing
        } catch {
            throw "could not fetch the signed agent manifest from $FromManifestUrl (or its .sig): $($_.Exception.Message). Use -AgentExe with a locally built agent, or -AgentUrl for an unverified download."
        }
        $mBytes = [IO.File]::ReadAllBytes($mPath)
        $sigHex = [IO.File]::ReadAllText($sPath)
        if (-not (Test-ManifestSignature $mBytes $sigHex)) {
            throw "the agent manifest at $FromManifestUrl is NOT signed by the fleet's key. Refusing to install. Nothing on this machine was changed."
        }
        # Parsed only AFTER the signature verified, so a missing field is a signing bug.
        $m = [Text.Encoding]::UTF8.GetString($mBytes) | ConvertFrom-Json
        $want = "$($m.sha256)".ToLowerInvariant()
        if (-not $m.version -or -not $m.url -or $want -notmatch '^[0-9a-f]{64}$') {
            throw "the signed manifest is missing its version, url or sha256 -- a release-signing bug."
        }
        Write-Host "  manifest verified: version $($m.version), signed by the fleet key"
        Write-Host "  binary  <- $($m.url)"
        $bPath = Join-Path $work "agent.exe"
        try {
            Invoke-WebRequest -Uri $m.url -OutFile $bPath -UseBasicParsing
        } catch {
            throw "download failed: $($m.url): $($_.Exception.Message)"
        }
        $got = (Get-FileHash -Path $bPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($got -ne $want) {
            throw "the downloaded binary does not match the signed manifest (sha256 $got, expected $want). Refusing to install."
        }
        Write-Host "  binary  sha256 matches the signed manifest"
        Move-Item -Path $bPath -Destination $Destination -Force
        return $m.version
    } finally {
        Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
    }
}
#endregion signed-manifest

# ----------------------------------------------------------------------
# Elevate
# ----------------------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host "Elevating..." -ForegroundColor Yellow
    if ($PSCommandPath) {
        $argList = @("-ExecutionPolicy","Bypass","-File","`"$PSCommandPath`"")
        foreach ($key in $PSBoundParameters.Keys) {
            $val = $PSBoundParameters[$key]
            if ($val -is [switch]) { if ($val.IsPresent) { $argList += "-$key" } }
            else { $argList += "-$key"; $argList += "`"$val`"" }
        }
        Start-Process powershell -Verb RunAs -ArgumentList $argList
    } else {
        Die "Re-run from a local .ps1 file as administrator."
    }
    exit
}

# ----------------------------------------------------------------------
# Uninstall
# ----------------------------------------------------------------------
if ($Uninstall) {
    Step "Uninstalling $ServiceName"

    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        & sc.exe stop $ServiceName | Out-Null
        Start-Sleep -Seconds 2
        & sc.exe delete $ServiceName | Out-Null
        Ok "Removed service $ServiceName"
    } else {
        Say "Service not present."
    }

    if (Test-Path $InstallDir) {
        Remove-Item $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
        Ok "Deleted $InstallDir"
    }

    # The enrollment identity is the one piece of state that must NOT survive, and it is not
    # in $InstallDir -- it lives in %ProgramData% alongside the logs. Leaving it is what makes
    # "uninstall, then reinstall with the same secret" silently do nothing on a machine the
    # hub has since deleted: the agent finds agent.json, decides it is already enrolled, and
    # never asks the hub again. Both paths, because the agent migrates its state dir from the
    # pre-rename location and would otherwise copy a stale identity forward.
    foreach ($stateDir in @($StateDir, $LegacyStateDir)) {
        $identity = Join-Path $stateDir "agent.json"
        if (Test-Path $identity) {
            Remove-Item $identity -Force -ErrorAction SilentlyContinue
            Ok "Removed enrollment identity $identity"
        }
    }

    Warn "Left PawnIO driver and the rest of %ProgramData%\FleetHub\Agent (logs) in place."
    Write-Host "`nDone.`n" -ForegroundColor Green
    exit
}

Write-Host @"

  FleetHub - Fleet Agent Installer
  Machine: $env:COMPUTERNAME
  Target : $InstallDir

"@ -ForegroundColor Cyan

# ----------------------------------------------------------------------
# 1. PawnIO driver (sensor access for the in-process LibreHardwareMonitorLib)
# ----------------------------------------------------------------------
Step "Installing PawnIO driver"
if (Get-Service -Name "PawnIO" -ErrorAction SilentlyContinue) {
    Ok "Already installed, skipping"
} else {
    $pawnioPath = Join-Path $env:TEMP "PawnIO_setup.exe"
    Say "Downloading $PawnIoUrl"
    Invoke-WebRequest -Uri $PawnIoUrl -OutFile $pawnioPath -UseBasicParsing
    Unblock-File -Path $pawnioPath -ErrorAction SilentlyContinue
    $proc = Start-Process -FilePath $pawnioPath -ArgumentList "-install","-silent" -Wait -PassThru -NoNewWindow
    Remove-Item $pawnioPath -Force -ErrorAction SilentlyContinue
    if ($proc.ExitCode -ne 0) { Warn "PawnIO installer exited $($proc.ExitCode). Sensors may be unreadable." }
    else { Ok "PawnIO installed" }
}

# ----------------------------------------------------------------------
# 2. Agent binary
# ----------------------------------------------------------------------
# Fetched and verified BEFORE the running service is stopped, so a failed download or a
# refused signature leaves a working agent running rather than a machine with none. (It used
# to stop the service first and download straight over the exe.)
Step "Fetching agent binary"
$staged = $null
if ($AgentExe) {
    if (-not (Test-Path $AgentExe)) { Die "AgentExe not found: $AgentExe" }
    $source = $AgentExe
    Say "binary  <- $AgentExe (local, not checked against the fleet key)"
} else {
    $staged = Join-Path ([IO.Path]::GetTempPath()) ("fleethub-agent-" + [guid]::NewGuid().ToString('N') + ".exe")
    $source = $staged
    if ($AgentUrl) {
        Say "Downloading $AgentUrl"
        Invoke-WebRequest -Uri $AgentUrl -OutFile $staged -UseBasicParsing
        Warn "-AgentUrl is NOT checked against the fleet's signing key. Omit it to install the signed release."
    } else {
        try {
            $version = Get-VerifiedAgent -Destination $staged -FromManifestUrl $ManifestUrl
            Ok "Agent $version verified against the fleet key"
        } catch {
            Remove-Item $staged -Force -ErrorAction SilentlyContinue
            Die $_.Exception.Message
        }
    }
}

Step "Installing agent binary"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

# Stop an existing service before overwriting its exe.
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    & sc.exe stop $ServiceName | Out-Null
    Start-Sleep -Seconds 2
}

# Pre-rename installs live at C:\Program Files\TempMonitorAgent. Move them under the
# shared FleetHub root so hub and agent sit together, and point the existing service
# registration at the new path (below) rather than leaving a second copy behind.
# %ProgramData% state and the enrollment secret are handled by the agent itself.
if ($InstallDir -ne $LegacyInstall -and (Test-Path $LegacyInstall)) {
    Say "Migrating existing install from $LegacyInstall"
    foreach ($stale in @("$ExeName.old")) {
        Remove-Item (Join-Path $LegacyInstall $stale) -Force -ErrorAction SilentlyContinue
    }
    Get-ChildItem -File $LegacyInstall -ErrorAction SilentlyContinue | ForEach-Object {
        Move-Item $_.FullName (Join-Path $InstallDir $_.Name) -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $LegacyInstall -Recurse -Force -ErrorAction SilentlyContinue
    Ok "Moved agent to $InstallDir"
}

Copy-Item -Path $source -Destination $ExePath -Force
if ($staged) { Remove-Item $staged -Force -ErrorAction SilentlyContinue }
Unblock-File -Path $ExePath -ErrorAction SilentlyContinue
Ok "binary  -> $ExePath"

# ----------------------------------------------------------------------
# 3. Configuration (enrollment secret + optional overrides)
# ----------------------------------------------------------------------
Step "Writing configuration"
New-Item -Path $RegPath -Force | Out-Null

# Carry a secret written by a pre-rename installer forward, so re-running this on an
# enrolled machine without -EnrollmentSecret doesn't silently drop it back to
# telemetry-only. The agent reads both keys, but consolidating here means the legacy
# key can eventually be retired.
$secret = $EnrollmentSecret
if (-not $secret) {
    $secret = (Get-ItemProperty -Path $LegacyRegPath -Name "EnrollmentSecret" -ErrorAction SilentlyContinue).EnrollmentSecret
    if ($secret) { Say "Reusing the enrollment secret from $LegacyRegPath" }
}
if ($secret) {
    New-ItemProperty -Path $RegPath -Name "EnrollmentSecret" -Value $secret -PropertyType String -Force | Out-Null
    Ok "Enrollment secret stored in $RegPath"
} else {
    Warn "No -EnrollmentSecret given; the agent will run telemetry-only until enrolled."
}

if ($HubUrl) {
    [Environment]::SetEnvironmentVariable("FLEETHUB_HUB", $HubUrl, "Machine")
    # Clear the pre-rename variable so the two can't drift apart and leave an operator
    # wondering which one the agent is honouring (it prefers FLEETHUB_HUB).
    if ([Environment]::GetEnvironmentVariable("TEMP_MONITOR_HUB", "Machine")) {
        [Environment]::SetEnvironmentVariable("TEMP_MONITOR_HUB", $null, "Machine")
    }
    Ok "Hub override: $HubUrl"
}
# Commands are no longer signed, so this machine-level key is dead config. Clear a
# stale one left by a pre-1.10 install rather than leaving it to confuse the next
# person who greps the environment for it.
if ([Environment]::GetEnvironmentVariable("COMMAND_SIGNING_PUBLIC_KEY_HEX", "Machine")) {
    [Environment]::SetEnvironmentVariable("COMMAND_SIGNING_PUBLIC_KEY_HEX", $null, "Machine")
    Ok "Removed obsolete COMMAND_SIGNING_PUBLIC_KEY_HEX (commands are no longer signed)"
}

# ----------------------------------------------------------------------
# 4. Service (LocalSystem, Automatic) + failure recovery
# ----------------------------------------------------------------------
Step "Registering Windows Service"
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Say "Service exists; updating binary path."
    # binPath must be re-pointed after a migration moved the exe under the shared root.
    & sc.exe config $ServiceName binPath= "`"$ExePath`"" start= auto DisplayName= "FleetHub Agent" | Out-Null
} else {
    New-Service -Name $ServiceName -BinaryPathName "`"$ExePath`"" `
        -DisplayName "FleetHub Agent" -StartupType Automatic `
        -Description "Reports telemetry and executes fleet commands for FleetHub (RMM)." | Out-Null
    Ok "Created service $ServiceName"
}

# Restart on failure -- the self-update exits with code 17 and relies on this.
& sc.exe failure $ServiceName reset= 86400 actions= restart/60000/restart/60000/restart/60000 | Out-Null
Ok "Failure recovery: restart x3 @ 60s"

# ----------------------------------------------------------------------
# 4b. Allow the service to press Ctrl+Alt+Del
# ----------------------------------------------------------------------
# The secure attention sequence cannot be synthesised with SendInput -- the kernel intercepts
# it, which is the point of a *secure* attention sequence. SendSAS is the supported route, and
# with SoftwareSASGeneration = 1 it is honoured for a caller running as a service. Without this
# the Ctrl+Alt+Del button in the remote viewer is a silent no-op, which matters most on exactly
# the machines the remote viewer is for: a headless box at the logon screen.
#
# Value 1 = "Services" only. Not 2 (ease-of-access apps) or 3 (both) -- this grants the
# narrowest thing that makes the button work.
Step "Allowing the service to send Ctrl+Alt+Del"
try {
    $sasKey = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"
    if (-not (Test-Path $sasKey)) { New-Item -Path $sasKey -Force | Out-Null }
    $existing = (Get-ItemProperty -Path $sasKey -Name SoftwareSASGeneration -ErrorAction SilentlyContinue).SoftwareSASGeneration
    if ($existing -eq 1 -or $existing -eq 3) {
        Ok "SoftwareSASGeneration already permits services ($existing)"
    } else {
        # Don't downgrade a value an admin deliberately set to 3 (services + apps).
        New-ItemProperty -Path $sasKey -Name SoftwareSASGeneration -Value 1 -PropertyType DWord -Force | Out-Null
        Ok "SoftwareSASGeneration = 1 (services may generate Ctrl+Alt+Del)"
    }
} catch {
    # Group Policy may own this key on a domain-joined machine, in which case it is the
    # domain admin's call, not ours. Everything else still works; only the button is dead.
    Warn "Could not set SoftwareSASGeneration ($($_.Exception.Message)). Ctrl+Alt+Del from the remote viewer may not work."
}

# ----------------------------------------------------------------------
# 5. Start
# ----------------------------------------------------------------------
Step "Starting service"
& sc.exe start $ServiceName | Out-Null
Start-Sleep -Seconds 2
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq "Running") { Ok "Service running" }
else { Warn "Service status: $($svc.Status). Check %ProgramData%\FleetHub\Agent\companion.log" }

Write-Host @"

  Done.

  Machine : $env:COMPUTERNAME
  Service : $ServiceName (LocalSystem, Automatic)
  Binary  : $ExePath
  Logs    : $env:ProgramData\FleetHub\Agent\companion.log

  Uninstall: powershell -ExecutionPolicy Bypass -File agent-install.ps1 -Uninstall

"@ -ForegroundColor Green
