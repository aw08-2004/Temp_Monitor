<#
    FleetHub - Android agent release automation.

    The counterpart of agent/release.ps1 and agent-linux/release.ps1, and deliberately the same
    shape: one command does the whole flow, because the steps that can be skipped are the ones
    that get skipped.

      1. Bumps the version in AgentConfig.cs + BOTH csproj files (this agent's pair is three)
      2. dotnet publish (release-signed APK, needs signing.props)
      3. Creates (or reuses) a GitHub release tagged android-agent-v<version>
      4. Signs it: python sign_release.py --sign-agent, writing agent-android.manifest.json
      5. Uploads the APK to that release as an asset
      6. Commits the manifest + .sig
      7. Pushes (only with -Push, or if you confirm the prompt)

    **Two signatures, and they are not the same signature.** The APK is signed with the fleet's
    Android keystore, which is what lets it replace the installed app at all -- Android refuses
    an update signed by anything else. The MANIFEST is signed with the fleet's offline Ed25519
    release key, the same one that signs the other two agents' manifests, which is what makes
    the download itself untrusted. An attacker who obtained the Android keystore would pass the
    platform's check and still fail the agent's.

    **The APK's signing certificate must not change between releases.** It is also what the
    provisioning QR's checksum covers (see hub/provisioning.py), so re-signing with a different
    key invalidates every printed code AND makes every installed agent refuse the update. There
    is no recovery from that except re-provisioning each device by hand, after a factory reset.

    Requires: gh CLI (authenticated), dotnet SDK with the Android workload, the Android SDK and
    JDK, agent-android/signing.props, Python + cryptography, and the release key.

    Usage:
        .\release.ps1 -Version 0.2.0
        .\release.ps1 -Version 0.2.0 -NotesFile .\release-notes\0.2.0.md -Push
        .\release.ps1 -Version 0.2.0 -DryRun
#>

param(
    [Parameter(Mandatory=$true)][string]$Version,
    [string]$Notes = "",
    [string]$NotesFile = "",
    [switch]$Push,
    [switch]$DryRun,
    [string]$SigningKey,
    [string]$Repo = "aw08-2004/Temp_Monitor",
    [ValidateSet("stable","beta")][string]$Channel = "stable"
)

$ErrorActionPreference = "Stop"
$RepoRoot   = Split-Path -Parent $PSScriptRoot
$AgentDir   = $PSScriptRoot
$CoreCsproj = Join-Path $AgentDir "src\FleetHubAgent.Core\FleetHubAgent.Core.csproj"
$AppCsproj  = Join-Path $AgentDir "src\FleetHubAgent.Android\FleetHubAgent.Android.csproj"
$ConfigCs   = Join-Path $AgentDir "src\FleetHubAgent.Core\AgentConfig.cs"
$DistDir    = Join-Path $AgentDir "dist"
$ApkName    = "net.arkeanos.fleethub.agent-Signed.apk"
$IsBeta     = ($Channel -eq "beta")
# Must match hub/channels.py's _AGENT_MANIFEST and AgentConfig.StableManifestUrl.
$ManifestPath = Join-Path $AgentDir $(if ($IsBeta) { "agent-android.manifest.beta.json" } else { "agent-android.manifest.json" })
$SignScript = Join-Path $RepoRoot "sign_release.py"
$Tag        = $(if ($IsBeta) { "android-agent-v$Version-beta" } else { "android-agent-v$Version" })
$AssetUrl   = "https://github.com/$Repo/releases/download/$Tag/$ApkName"

function Say($msg)  { Write-Host "  $msg" }
function Ok($msg)   { Write-Host "  [ok] $msg"   -ForegroundColor Green }
function Warn($msg) { Write-Host "  [!!] $msg"   -ForegroundColor Yellow }
function Die($msg)  { Write-Host "  [xx] $msg"   -ForegroundColor Red; exit 1 }
function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }

# **Run a native program whose stderr we do not want to be fatal.**
#
# PowerShell 5.1 wraps every line a native program writes to stderr in an ErrorRecord, and
# $ErrorActionPreference = "Stop" then makes that record terminate the script -- even when the
# program exited 0 and the text was a warning nobody asked about. It killed a release here:
# the provisioned JDK 17 started printing "WARNING: A restricted method in java.lang.System has
# been called" and took down the step that merely PRINTS the signing certificate, after a
# successful publish and before anything had been uploaded.
#
# An informational step must not be able to end a release, and `2>$null` does not prevent this
# -- the record is created before the redirection discards it. So the preference is relaxed
# around the call and error records are flattened to plain text.
function Native([scriptblock]$Command) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Command 2>&1 | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.ToString() } else { $_ }
        }
    } finally { $ErrorActionPreference = $prev }
}

# ---------------------------------------------------------------- checks

if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    Die "Version must be MAJOR.MINOR.PATCH with no suffix (VERSIONING.md). Got: $Version"
}
$parts = $Version.Split('.')
if ([int]$parts[0] -ge 3) {
    Die ("$Version is at or above the hub's AGENT_TRAIN_MIN_VERSION (3.0.0). The console's " +
         "version gates would start offering this device a terminal, a process list and a " +
         "file browser, none of which an Android app can implement. See AgentConfig.Version.")
}

foreach ($tool in @("dotnet", "gh", "git", "python")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { Die "$tool is not on PATH" }
}
if (-not (Test-Path (Join-Path $AgentDir "signing.props"))) {
    Die ("No signing.props. An unsigned or debug-signed APK cannot replace the installed one, " +
         "and publishing one would produce a release every device refuses. See signing.props.example.")
}

$notesBody = ""
if ($NotesFile) {
    if (-not (Test-Path $NotesFile)) { Die "Notes file not found: $NotesFile" }
    $notesBody = Get-Content $NotesFile -Raw
} elseif ($Notes) {
    $notesBody = $Notes
} else {
    $notesBody = "FleetHub Android agent v$Version"
}

Step "Plan"
Say "version   : $Version ($Channel)"
Say "tag       : $Tag"
Say "manifest  : $ManifestPath"
Say "asset url : $AssetUrl"
if ($DryRun) {
    Write-Host "`n-- notes --`n$notesBody"
    Ok "dry run: nothing external touched"
    exit 0
}

# ---------------------------------------------------------------- 1. version triple

Step "Bumping the version"
# THREE files, not two. The Android csproj's ApplicationDisplayVersion is the APK's own
# versionName, which is what the phone's app-info screen shows -- a value that disagreed with
# what the agent reports would have the console and the device showing different versions of
# the same thing. ApplicationVersion is deliberately NOT touched: it is the integer Android
# sorts upgrades by, and VersionGateTests pins that it is not tied to this number.
# "Unchanged" has two causes that must not read the same, and this script used to conflate
# them: it died with "could not find" on a re-run of a version that was already applied, which
# sends you looking for a broken regex in a file that is perfectly correct. Either the file
# already carries this version -- normal, on a second run after a later step failed, or when
# the bump was made by hand -- or the pattern no longer matches, in which case the version is
# NOT $Version, the publish below ships whatever the file does say, and the manifest advertises
# a version no APK reports. Told apart by looking for the target text, exactly as
# agent/release.ps1 does.
function SetText($path, $text) {
    # UTF-8 with NO byte order mark. Set-Content -Encoding UTF8 on PowerShell 5.1 writes one,
    # and a BOM appearing at the top of AgentConfig.cs and both csproj files turns a one-line
    # version bump into a whole-file diff on every release.
    [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))
}

$config = Get-Content $ConfigCs -Raw
$configNew = $config -replace '(public const string Version = ")[^"]+(")', "`${1}$Version`${2}"
if ($configNew -eq $config) {
    if ($config -notmatch [regex]::Escape("public const string Version = `"$Version`";")) {
        Die "AgentConfig.cs Version line does not match the pattern and is not already $Version -- check it by hand."
    }
    Ok "AgentConfig.cs already at $Version"
} else {
    SetText $ConfigCs $configNew
    Ok "AgentConfig.cs -> $Version"
}

foreach ($proj in @($CoreCsproj, $AppCsproj)) {
    $name = Split-Path -Leaf $proj
    $text = Get-Content $proj -Raw
    $updated = $text -replace '(<Version>)[^<]+(</Version>)', "`${1}$Version`${2}"
    $updated = $updated -replace '(<ApplicationDisplayVersion>)[^<]+(</ApplicationDisplayVersion>)', "`${1}$Version`${2}"
    if ($updated -eq $text) {
        if ($text -notmatch [regex]::Escape("<Version>$Version</Version>")) {
            Die "$name <Version> does not match the pattern and is not already $Version -- check it by hand."
        }
        Ok "$name already at $Version"
    } else {
        SetText $proj $updated
        Ok "$name -> $Version"
    }
}

# ---------------------------------------------------------------- 2. publish

Step "Publishing"
if (Test-Path $DistDir) { Remove-Item $DistDir -Recurse -Force }
& dotnet publish $AppCsproj -c Release -o $DistDir `
    -p:AndroidSdkDirectory="$env:LOCALAPPDATA\Android\Sdk" `
    -p:JavaSdkDirectory="$env:LOCALAPPDATA\Android\jdk" | Out-Host
if ($LASTEXITCODE -ne 0) { Die "dotnet publish failed" }

$apk = Get-ChildItem -Path $DistDir -Filter "*-Signed.apk" -Recurse | Select-Object -First 1
if (-not $apk) { Die "No signed APK under $DistDir" }
$ApkPath = $apk.FullName
Ok ("published {0:N1} MB" -f ($apk.Length / 1MB))

# The signing certificate the provisioning QR's checksum covers. Printed rather than checked,
# because this script does not know what the previous release was signed with -- but a
# technician about to re-provision a device can compare it against the console.
$apksigner = Join-Path $env:LOCALAPPDATA "Android\Sdk\build-tools\36.0.0\apksigner.bat"
if (Test-Path $apksigner) {
    Say "signing certificate:"
    Native { & $apksigner verify --print-certs $ApkPath } |
        Select-String -Pattern "SHA-256 digest" | ForEach-Object { Say "  $_" }
    Say "If that digest changed, every printed provisioning QR is now invalid AND every"
    Say "installed agent will refuse this update. Stop and check before continuing."
}

# ---------------------------------------------------------------- 3. release

Step "Creating the GitHub release"
$existing = Native { & gh release view $Tag --repo $Repo }
if ($LASTEXITCODE -eq 0) {
    Warn "$Tag already exists; reusing it"
} else {
    $ghArgs = @("release", "create", $Tag, "--repo", $Repo, "--title", "Android agent v$Version",
                "--notes", $notesBody)
    if ($IsBeta) { $ghArgs += "--prerelease" }
    & gh @ghArgs | Out-Host
    if ($LASTEXITCODE -ne 0) { Die "gh release create failed" }
    Ok "created $Tag"
}

# ---------------------------------------------------------------- 4. sign the manifest

Step "Signing the manifest"
$signArgs = @($SignScript, "--sign-agent", "--file", $ApkPath,
              "--agent-version", $Version, "--agent-url", $AssetUrl,
              "--manifest", $ManifestPath)
if ($SigningKey) { $signArgs += @("--key", $SigningKey) }
& python @signArgs | Out-Host
if ($LASTEXITCODE -ne 0) { Die "sign_release.py failed" }
if (-not (Test-Path "$ManifestPath.sig")) { Die "No signature beside $ManifestPath" }
Ok "manifest + .sig written"

# ---------------------------------------------------------------- 5. upload

Step "Uploading the asset"
# --clobber, and the asset name must stay $ApkName: the manifest was signed against that exact
# URL, so a differently-named asset is a signed manifest pointing at a 404.
& gh release upload $Tag "$ApkPath#$ApkName" --repo $Repo --clobber | Out-Host
if ($LASTEXITCODE -ne 0) { Die "gh release upload failed" }
Ok "uploaded $ApkName"

# ---------------------------------------------------------------- 6. commit

Step "Committing"
& git -C $RepoRoot add $ConfigCs $CoreCsproj $AppCsproj $ManifestPath "$ManifestPath.sig" | Out-Host
& git -C $RepoRoot commit -m "Release Android agent v$Version ($Channel)" | Out-Host
if ($LASTEXITCODE -ne 0) { Warn "nothing to commit, or the commit failed" }

# ---------------------------------------------------------------- 7. push

Step "Pushing"
# Nothing reaches a device until this push lands: the agent reads the manifest from main, so an
# unpushed manifest is a release that exists on GitHub, is signed, and updates nobody.
if (-not $Push) {
    $answer = Read-Host "Push to main now? [y/N]"
    if ($answer -notmatch '^[Yy]') {
        Warn "not pushed. The release will not reach any device until you do."
        exit 0
    }
}
& git -C $RepoRoot push | Out-Host
if ($LASTEXITCODE -ne 0) { Die "git push failed" }
Ok "pushed -- managed devices on $Channel will update within about six hours"
