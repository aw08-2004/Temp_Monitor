<#
    FleetHub - Linux agent release automation.

    The counterpart of agent/release.ps1, and deliberately the same shape: one command does
    the whole flow, because the steps that can be skipped are the ones that get skipped.

      1. Bumps the version in AgentConfig.cs + FleetHubAgent.csproj (the two-file pair)
      2. dotnet publish (self-contained single-file linux-x64)
      3. Creates (or reuses) a GitHub release tagged linux-agent-v<version>
      4. Signs it: python sign_release.py --sign-agent, writing agent-linux.manifest.json
      5. Uploads the binary to that release as an asset
      6. Commits the manifest + .sig
      7. Pushes (only with -Push, or if you confirm the prompt)

    **Why this exists at all.** Until roadmap #22 the Linux agent had no manifest and no
    self-update: an upgrade meant re-running install.sh on every box by hand, and the reason
    was not effort but safety -- the hub advertised the WINDOWS agent's manifest to anything
    reporting a version at or above 3.0.0, so this agent stayed pinned at 0.1.0 to be ignored.
    The hub now picks a manifest by the platform a machine reports, so this train can exist.

    **The trust root is the fleet's, not GitHub's.** The same offline Ed25519 key signs this
    manifest, the Windows agent's and the client's. That is what makes the download itself
    untrusted: the binary can come from anywhere as long as it hashes to the value inside a
    manifest signed by a key the hub has never held.

    Requires: gh CLI (authenticated), dotnet SDK, Python + cryptography (for sign_release.py),
    and the signing key at ~/.temp_monitor_signing_key unless -SigningKey says otherwise.

    Usage:
        .\release.ps1 -Version 0.2.0
        .\release.ps1 -Version 0.2.0 -NotesFile .\release-notes\0.2.0.md -Push
        .\release.ps1 -Version 0.2.0 -DryRun     # print the plan, touch nothing external
#>

param(
    [Parameter(Mandatory=$true)][string]$Version,
    [string]$Notes = "",
    [string]$NotesFile = "",
    [switch]$Push,
    [switch]$DryRun,
    [string]$SigningKey,
    [string]$Repo = "aw08-2004/Temp_Monitor",
    # Roadmap #21's channels apply here too: beta writes agent-linux.manifest.beta.json and
    # tags the release -beta, and only machines pinned to beta in the console read it.
    #
    # ONE VERSION SEQUENCE, shared with stable -- a beta is a number published here first, so
    # promoting it is copying the beta manifest over the stable one. Do NOT invent a separate
    # beta numbering: VERSIONING.md forbids suffixes and four comparators enforce it.
    [ValidateSet("stable","beta")][string]$Channel = "stable"
)

$ErrorActionPreference = "Stop"
$RepoRoot   = Split-Path -Parent $PSScriptRoot
$AgentDir   = $PSScriptRoot
$Csproj     = Join-Path $AgentDir "src\FleetHubAgent\FleetHubAgent.csproj"
$ConfigCs   = Join-Path $AgentDir "src\FleetHubAgent\AgentConfig.cs"
$DistDir    = Join-Path $AgentDir "dist"
# The asset name install.sh matches on, and the name the unit file's ExecStart expects.
$BinName    = "fleethub-agent"
$BinPath    = Join-Path $DistDir $BinName
$IsBeta     = ($Channel -eq "beta")
# Must match hub/channels.py's _AGENT_MANIFEST and AgentConfig.StableManifestUrl. Three copies
# of this filename exist by necessity -- a PowerShell script, a Python module and a C# constant
# cannot share one -- which is exactly why tests pin the other two.
$ManifestPath = Join-Path $AgentDir $(if ($IsBeta) { "agent-linux.manifest.beta.json" } else { "agent-linux.manifest.json" })
$SignScript = Join-Path $RepoRoot "sign_release.py"
# linux-agent-v<version>, which is the prefix install.sh looks for. The beta suffix keeps the
# two channels' releases and their assets from colliding.
$Tag        = $(if ($IsBeta) { "linux-agent-v$Version-beta" } else { "linux-agent-v$Version" })
$AssetUrl   = "https://github.com/$Repo/releases/download/$Tag/$BinName"

function Say($msg)  { Write-Host "  $msg" }
function Ok($msg)   { Write-Host "  [ok] $msg"   -ForegroundColor Green }
function Warn($msg) { Write-Host "  [!!] $msg"   -ForegroundColor Yellow }
function Die($msg)  { Write-Host "  [xx] $msg"   -ForegroundColor Red; exit 1 }
function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }

# ---------------------------------------------------------------- checks

if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    Die "Version must be MAJOR.MINOR.PATCH with no suffix (VERSIONING.md). Got: $Version"
}

# The floor this agent must stay under until the console's MIN_*_AGENT gates read capabilities
# rather than a version number. Checked HERE as well as in VersionGateTests, because a release
# script is where somebody types a number by hand at the end of a long day.
$parts = $Version.Split('.')
if ([int]$parts[0] -ge 3) {
    Die ("$Version is at or above the hub's AGENT_TRAIN_MIN_VERSION (3.0.0). The console's " +
         "version gates would start offering this machine a terminal and a file browser it " +
         "cannot answer. See AgentConfig.Version.")
}

foreach ($tool in @("dotnet", "gh", "git", "python")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { Die "$tool is not on PATH" }
}

$notesBody = ""
if ($NotesFile) {
    if (-not (Test-Path $NotesFile)) { Die "Notes file not found: $NotesFile" }
    $notesBody = Get-Content $NotesFile -Raw
} elseif ($Notes) {
    $notesBody = $Notes
} else {
    $notesBody = "FleetHub Linux agent v$Version"
}

Step "Plan"
Say "version   : $Version ($Channel)"
Say "tag       : $Tag"
Say "binary    : $BinPath"
Say "manifest  : $ManifestPath"
Say "asset url : $AssetUrl"
if ($DryRun) {
    Write-Host "`n-- notes --`n$notesBody"
    Ok "dry run: nothing external touched"
    exit 0
}

# ---------------------------------------------------------------- 1. version pair

Step "Bumping the version pair"
# Both files or neither. A csproj that disagrees with AgentConfig.Version is an agent that
# reports one number and is built as another, and the hub believes the reported one.
$config = Get-Content $ConfigCs -Raw
$configNew = $config -replace '(public const string Version = ")[^"]+(")', "`${1}$Version`${2}"
if ($configNew -eq $config) { Die "Could not find AgentConfig.Version in $ConfigCs" }
Set-Content -Path $ConfigCs -Value $configNew -NoNewline -Encoding UTF8

$proj = Get-Content $Csproj -Raw
$projNew = $proj -replace '(<Version>)[^<]+(</Version>)', "`${1}$Version`${2}"
if ($projNew -eq $proj) { Die "Could not find <Version> in $Csproj" }
Set-Content -Path $Csproj -Value $projNew -NoNewline -Encoding UTF8
Ok "AgentConfig.cs and the csproj both say $Version"

# ---------------------------------------------------------------- 2. publish

Step "Publishing"
if (Test-Path $DistDir) { Remove-Item $DistDir -Recurse -Force }
& dotnet publish $Csproj -c Release -o $DistDir | Out-Host
if ($LASTEXITCODE -ne 0) { Die "dotnet publish failed" }
if (-not (Test-Path $BinPath)) { Die "No binary at $BinPath after publish" }
Ok ("published {0:N1} MB" -f ((Get-Item $BinPath).Length / 1MB))

# ---------------------------------------------------------------- 3. release

Step "Creating the GitHub release"
$existing = & gh release view $Tag --repo $Repo 2>$null
if ($LASTEXITCODE -eq 0) {
    Warn "$Tag already exists; reusing it"
} else {
    $args = @("release", "create", $Tag, "--repo", $Repo, "--title", "Linux agent v$Version",
              "--notes", $notesBody)
    if ($IsBeta) { $args += "--prerelease" }
    & gh @args | Out-Host
    if ($LASTEXITCODE -ne 0) { Die "gh release create failed" }
    Ok "created $Tag"
}

# ---------------------------------------------------------------- 4. sign

Step "Signing the manifest"
# The manifest is signed against the exact bytes just published AND the exact URL the asset
# will live at. Signing before the upload is deliberate: the digest is computed from the local
# file, so an upload that silently truncated would fail the agent's hash check rather than
# being signed as correct.
$signArgs = @($SignScript, "--sign-agent", "--file", $BinPath,
              "--agent-version", $Version, "--agent-url", $AssetUrl,
              "--manifest", $ManifestPath)
if ($SigningKey) { $signArgs += @("--key", $SigningKey) }
& python @signArgs | Out-Host
if ($LASTEXITCODE -ne 0) { Die "sign_release.py failed" }
if (-not (Test-Path "$ManifestPath.sig")) { Die "No signature beside $ManifestPath" }
Ok "manifest + .sig written"

# ---------------------------------------------------------------- 5. upload

Step "Uploading the asset"
& gh release upload $Tag $BinPath --repo $Repo --clobber | Out-Host
if ($LASTEXITCODE -ne 0) { Die "gh release upload failed" }
Ok "uploaded $BinName"

# ---------------------------------------------------------------- 6. commit

Step "Committing"
& git -C $RepoRoot add $ConfigCs $Csproj $ManifestPath "$ManifestPath.sig" | Out-Host
& git -C $RepoRoot commit -m "Release Linux agent v$Version ($Channel)" | Out-Host
if ($LASTEXITCODE -ne 0) { Warn "nothing to commit, or the commit failed" }

# ---------------------------------------------------------------- 7. push

Step "Pushing"
# **Nothing reaches a machine until this push lands.** The agent reads the manifest from the
# repo's main branch, so an unpushed manifest is a release that exists on GitHub, is signed,
# and updates nobody -- which looks exactly like a release that shipped.
if (-not $Push) {
    $answer = Read-Host "Push to main now? [y/N]"
    if ($answer -notmatch '^[Yy]') {
        Warn "not pushed. The release will not reach any machine until you do."
        exit 0
    }
}
& git -C $RepoRoot push | Out-Host
if ($LASTEXITCODE -ne 0) { Die "git push failed" }
Ok "pushed -- machines on $Channel will update within about 15 minutes"
