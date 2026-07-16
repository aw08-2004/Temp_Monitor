<#
    Temp Monitor - C# Agent Release Automation

    Runs the whole release flow in one command:
      1. Bumps the version in AgentConfig.cs + TempMonitorAgent.csproj
      2. dotnet publish (self-contained single-file win-x64)
      3. Creates (or reuses) a GitHub release tagged agent-v<version>
      4. Signs the release: python sign_release.py --sign-agent
         (writes + signs agent/agent.manifest.json against the exact asset URL)
      5. Uploads the exe to that release as an asset
      6. Commits agent.manifest.json + .sig
      7. Pushes (only if -Push is given, or you confirm the interactive prompt)

    Requires: gh CLI (authenticated: `gh auth login`), dotnet SDK, Python +
    cryptography (for sign_release.py), and a working git push (this repo's
    signing key must already exist at ~/.temp_monitor_signing_key, or pass -SigningKey).

    Usage:
        .\release.ps1 -Version 3.0.1
        .\release.ps1 -Version 3.0.1 -Notes "Fix rename executor" -Push
        .\release.ps1 -Version 3.0.1 -DryRun     # print the plan, touch nothing external
#>

param(
    [Parameter(Mandatory=$true)][string]$Version,
    [string]$Notes = "",
    [switch]$Push,
    [switch]$DryRun,
    [string]$SigningKey,           # default: ~/.temp_monitor_signing_key (sign_release.py's own default)
    [string]$Repo = "aw08-2004/Temp_Monitor"
)

$ErrorActionPreference = "Stop"
$RepoRoot   = Split-Path -Parent $PSScriptRoot   # .../Temp_Monitor (this script lives in agent/)
$AgentDir   = $PSScriptRoot                      # .../Temp_Monitor/agent
$Csproj     = Join-Path $AgentDir "src\TempMonitorAgent\TempMonitorAgent.csproj"
$ConfigCs   = Join-Path $AgentDir "src\TempMonitorAgent\AgentConfig.cs"
$DistDir    = Join-Path $AgentDir "dist"
$ExePath    = Join-Path $DistDir "TempMonitorAgent.exe"
$ManifestPath = Join-Path $AgentDir "agent.manifest.json"
$SignScript = Join-Path $RepoRoot "sign_release.py"
$Tag        = "agent-v$Version"
$AssetUrl   = "https://github.com/$Repo/releases/download/$Tag/TempMonitorAgent.exe"

function Say($msg)  { Write-Host "  $msg" }
function Ok($msg)   { Write-Host "  [ok] $msg"   -ForegroundColor Green }
function Warn($msg) { Write-Host "  [!!] $msg"   -ForegroundColor Yellow }
function Die($msg)  { Write-Host "  [xx] $msg"   -ForegroundColor Red; exit 1 }
function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }

if ($Version -notmatch '^\d+\.\d+\.\d+$') { Die "Version must look like 3.0.1 (got '$Version')." }

Write-Host @"

  Temp Monitor - Agent Release
  Version : $Version
  Tag     : $Tag
  Asset   : $AssetUrl
  Push    : $($Push.IsPresent)
  DryRun  : $($DryRun.IsPresent)

"@ -ForegroundColor Cyan

# ----------------------------------------------------------------------
# 0. Preflight
# ----------------------------------------------------------------------
Step "Preflight checks"
if (-not $DryRun) {
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        Die "gh CLI not found. Install it (winget install GitHub.cli) and run 'gh auth login' first."
    }
    $ghUser = $null
    try { $ghUser = gh api user --jq .login 2>$null } catch { $ghUser = $null }
    if (-not $ghUser) { Die "gh is not authenticated. Run 'gh auth login' first." }
    Ok "gh authenticated as $ghUser"
}
if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) { Die "dotnet SDK not found." }
$py = $null
foreach ($cmd in @("py","python")) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) { $py = $cmd; break }
}
if (-not $py) { Die "Python not found (needed for sign_release.py)." }
Ok "Tools present (dotnet, python via '$py'$(if(-not $DryRun){", gh"}))"

$keyPath = if ($SigningKey) { $SigningKey } else { Join-Path $env:USERPROFILE ".temp_monitor_signing_key" }
if (-not (Test-Path $keyPath)) { Die "No signing key at $keyPath. Run: python sign_release.py --genkey" }
Ok "Signing key: $keyPath"

# ----------------------------------------------------------------------
# 1. Bump version in AgentConfig.cs + csproj
# ----------------------------------------------------------------------
Step "Bumping version to $Version"

$configText = Get-Content $ConfigCs -Raw
$newConfigText = $configText -replace 'public const string Version = "[\d.]+";', "public const string Version = `"$Version`";"
if ($newConfigText -eq $configText) { Warn "AgentConfig.cs Version line not found/unchanged -- check the pattern." }
elseif (-not $DryRun) { Set-Content -Path $ConfigCs -Value $newConfigText -NoNewline }
Ok "AgentConfig.cs -> $Version$(if($DryRun){' (dry-run, not written)'})"

$csprojText = Get-Content $Csproj -Raw
$newCsprojText = $csprojText -replace '<Version>[\d.]+</Version>', "<Version>$Version</Version>"
if ($newCsprojText -eq $csprojText) { Warn "TempMonitorAgent.csproj <Version> not found/unchanged." }
elseif (-not $DryRun) { Set-Content -Path $Csproj -Value $newCsprojText -NoNewline }
Ok "TempMonitorAgent.csproj -> $Version$(if($DryRun){' (dry-run, not written)'})"

# ----------------------------------------------------------------------
# 2. Publish
# ----------------------------------------------------------------------
Step "Publishing self-contained single-file exe"
if ($DryRun) {
    Say "[dry-run] dotnet publish `"$Csproj`" -c Release -o `"$DistDir`""
} else {
    dotnet publish $Csproj -c Release -o $DistDir
    if ($LASTEXITCODE -ne 0) { Die "dotnet publish failed." }
    if (-not (Test-Path $ExePath)) { Die "Publish did not produce $ExePath." }
    Ok "Published: $ExePath ($([math]::Round((Get-Item $ExePath).Length / 1MB, 1)) MB)"
}

# ----------------------------------------------------------------------
# 3. GitHub release (create if missing, reuse otherwise)
# ----------------------------------------------------------------------
Step "GitHub release $Tag"
if ($DryRun) {
    Say "[dry-run] gh release view $Tag --repo $Repo   (create if missing)"
} else {
    $exists = $true
    try {
        gh release view $Tag --repo $Repo *> $null
        if ($LASTEXITCODE -ne 0) { $exists = $false }
    } catch {
        # $ErrorActionPreference = Stop promotes gh's stderr output (expected here --
        # this is how we detect "release doesn't exist yet") to a terminating error.
        $exists = $false
    }

    if ($exists) {
        Ok "Release $Tag already exists, reusing it"
    } else {
        $notesArg = if ($Notes) { $Notes } else { "Agent v$Version" }
        gh release create $Tag --repo $Repo --title "Agent v$Version" --notes $notesArg
        if ($LASTEXITCODE -ne 0) { Die "gh release create failed." }
        Ok "Created release $Tag"
    }
}

# ----------------------------------------------------------------------
# 4. Sign (writes agent.manifest.json + .sig against the asset URL)
# ----------------------------------------------------------------------
Step "Signing manifest"
if ($DryRun) {
    Say "[dry-run] $py `"$SignScript`" --sign-agent --file `"$ExePath`" --agent-version $Version --agent-url $AssetUrl --key `"$keyPath`""
} else {
    & $py $SignScript --sign-agent --file $ExePath --agent-version $Version --agent-url $AssetUrl --key $keyPath
    if ($LASTEXITCODE -ne 0) { Die "sign_release.py --sign-agent failed." }
    Ok "Signed -> $ManifestPath (+ .sig)"
}

# ----------------------------------------------------------------------
# 5. Upload the exe as the release asset (must match the signed URL exactly)
# ----------------------------------------------------------------------
Step "Uploading asset"
if ($DryRun) {
    Say "[dry-run] gh release upload $Tag `"$ExePath`" --repo $Repo --clobber"
} else {
    gh release upload $Tag $ExePath --repo $Repo --clobber
    if ($LASTEXITCODE -ne 0) { Die "gh release upload failed." }
    Ok "Uploaded $ExePath -> $AssetUrl"
}

# ----------------------------------------------------------------------
# 6. Commit the manifest + signature
# ----------------------------------------------------------------------
Step "Committing manifest"
Push-Location $RepoRoot
try {
    if ($DryRun) {
        Say "[dry-run] git add agent/agent.manifest.json agent/agent.manifest.json.sig agent/src/.../AgentConfig.cs agent/src/.../TempMonitorAgent.csproj"
        Say "[dry-run] git commit -m `"Release agent v$Version`""
    } else {
        git add $ManifestPath "$ManifestPath.sig" $ConfigCs $Csproj
        $staged = git diff --cached --name-only
        if (-not $staged) {
            Warn "Nothing staged (already committed?) -- skipping commit."
        } else {
            git commit -m "Release agent v$Version" | Out-Null
            Ok "Committed: $($staged -join ', ')"
        }
    }
} finally { Pop-Location }

# ----------------------------------------------------------------------
# 7. Push (only with -Push, or after an interactive confirmation)
# ----------------------------------------------------------------------
Step "Push"
if ($DryRun) {
    Say "[dry-run] would push current branch to origin (only with -Push or after confirmation)"
} else {
    $doPush = $Push.IsPresent
    if (-not $doPush) {
        $branch = git -C $RepoRoot rev-parse --abbrev-ref HEAD
        $answer = Read-Host "Push branch '$branch' to origin now? (y/N)"
        $doPush = $answer -match '^[Yy]'
    }
    if ($doPush) {
        git -C $RepoRoot push
        if ($LASTEXITCODE -ne 0) { Die "git push failed." }
        Ok "Pushed"
    } else {
        Warn "Not pushed. Run 'git push' manually when ready."
    }
}

Write-Host @"

  Done. Agent v$Version released as $Tag.
  Asset : $AssetUrl
  Fleet agents will pick this up on their next manifest check (weekly, or
  sooner if the hub echoes a newer companion_version).

"@ -ForegroundColor Green
