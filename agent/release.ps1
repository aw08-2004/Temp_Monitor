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
        .\release.ps1 -Version 3.0.1 -NotesFile .\release-notes\3.0.1.md -Push
        .\release.ps1 -Version 3.0.1 -Notes "Fix rename executor"
        .\release.ps1 -Version 3.0.1 -DryRun     # print the plan + the notes body, touch nothing external

    Prefer -NotesFile for anything longer than a sentence. -Notes is fine for a one-liner
    with no quotes or backslashes in it; past that, see the comment at the release-creation
    step on why prose and PowerShell argument binding do not mix.
#>

param(
    [Parameter(Mandatory=$true)][string]$Version,
    [string]$Notes = "",
    [string]$NotesFile = "",       # preferred: agent/release-notes/<version>.md
    [switch]$Push,
    [switch]$DryRun,
    [string]$SigningKey,           # default: ~/.temp_monitor_signing_key (sign_release.py's own default)
    [string]$Repo = "aw08-2004/Temp_Monitor",
    # Release channel (roadmap #21). "beta" writes agent.manifest.beta.json instead, tags the
    # release agent-v<version>-beta, and marks it a prerelease. Only machines pinned to beta
    # in the console read that manifest; everything else is untouched.
    #
    # ONE VERSION SEQUENCE, shared with stable. A beta is simply a number published here
    # first, so promoting it is copying agent.manifest.beta.json over agent.manifest.json --
    # every pilot machine is already at that version and does nothing, and the fleet updates
    # to it. Do NOT invent a separate beta numbering: VERSIONING.md forbids suffixes and four
    # comparators enforce it.
    [ValidateSet("stable","beta")][string]$Channel = "stable"
)

$ErrorActionPreference = "Stop"
$RepoRoot   = Split-Path -Parent $PSScriptRoot   # .../Temp_Monitor (this script lives in agent/)
$AgentDir   = $PSScriptRoot                      # .../Temp_Monitor/agent
$Csproj     = Join-Path $AgentDir "src\TempMonitorAgent\TempMonitorAgent.csproj"
$ConfigCs   = Join-Path $AgentDir "src\TempMonitorAgent\AgentConfig.cs"
$DistDir    = Join-Path $AgentDir "dist"
$ExePath    = Join-Path $DistDir "TempMonitorAgent.exe"
$IsBeta       = ($Channel -eq "beta")
# Must match hub/channels.py's _AGENT_MANIFEST and AgentConfig.BetaManifestUrl. Three copies
# of this filename exist by necessity -- a PowerShell script, a Python module and a C#
# constant cannot share one -- and tests/test_channels.py pins the other two.
$ManifestPath = Join-Path $AgentDir $(if ($IsBeta) { "agent.manifest.beta.json" } else { "agent.manifest.json" })
$SignScript = Join-Path $RepoRoot "sign_release.py"
# The tag differs so the two channels' releases and their assets never collide.
$Tag        = $(if ($IsBeta) { "agent-v$Version-beta" } else { "agent-v$Version" })
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

# Can git actually make the manifest commit? Asked HERE, before anything is published.
#
# That commit is the last step of this script and the only one that reaches the fleet: main is
# where SelfUpdater reads agent.manifest.json, so a release whose commit fails has a published
# GitHub release, a published asset, a signed manifest -- and no machine that will ever see
# any of it. Finding that out at the end means unpicking a release that is already public;
# finding it out here costs nothing.
#
# The failure this exists for: released from the FleetHub Terminal, this script runs as SYSTEM,
# whose profile carried commit.gpgsign=true and no keyring to honour it with. git signs nothing
# in this repo -- the manifest's own Ed25519 signature is what the agent verifies, not the
# commit's -- so the fix is to unset it, and the message below says where.
#
# -C $RepoRoot is load-bearing: from that terminal the working directory is C:\Windows\system32,
# and a bare `git config` there reads the ambient profile without the repo's own config, which
# is a different answer to the question being asked.
$signCommits = git -C $RepoRoot config --type=bool --get commit.gpgsign
if ($signCommits -eq "true") {
    $gpgFormat = git -C $RepoRoot config --get gpg.format
    if ($gpgFormat -eq "ssh") {
        # git signs with an ssh key here; gpg is not involved and there is no keyring to check.
        Ok "Commits are signed with an ssh key (not verified here)"
    } else {
        $signer = git -C $RepoRoot config --get user.signingkey
        if (-not $signer) { $signer = git -C $RepoRoot config --get user.email }
        $gpgProgram = git -C $RepoRoot config --get gpg.program
        if (-not $gpgProgram) { $gpgProgram = "gpg" }
        $origin = git -C $RepoRoot config --show-origin --get commit.gpgsign
        # "file:C:/path/.gitconfig\ttrue" -- the path is what an operator needs to unset it.
        $originFile = ($origin -split "\s+")[0] -replace '^file:', ''
        if (-not [System.IO.Path]::IsPathRooted($originFile)) {
            # A repo-local setting reports as the relative ".git/config", which is only a
            # usable instruction from inside the repo -- and the shell this failed in was
            # sitting in system32.
            $originFile = Join-Path $RepoRoot $originFile
        }

        $unsetHint = "git config --file `"$originFile`" --unset commit.gpgsign"
        # git resolves gpg against its OWN bundled tools, which are not on the Windows PATH --
        # so Get-Command alone reports "not installed" for the very gpg git is about to use,
        # and would send an operator installing something they already have.
        if (-not (Get-Command $gpgProgram -ErrorAction SilentlyContinue)) {
            $bundledGpg = Join-Path (Split-Path (Split-Path (Get-Command git).Source)) "usr\bin\gpg.exe"
            if (Test-Path $bundledGpg) { $gpgProgram = $bundledGpg }
        }
        if (-not (Get-Command $gpgProgram -ErrorAction SilentlyContinue)) {
            Die ("commit.gpgsign is on (from $originFile) but $gpgProgram is not installed, so " +
                 "the manifest commit at the end of this script would fail after the release " +
                 "is already published. Either install it or turn signing off:  $unsetHint")
        }
        # --list-secret-keys rather than a test signature: it answers the same question without
        # a passphrase prompt, which in a SYSTEM shell with no tty would hang rather than fail.
        # $ErrorActionPreference is Stop for this whole script, and in PowerShell 5.1 a
        # native command that writes to stderr under Stop raises a terminating
        # NativeCommandError before the exit code can be looked at -- so gpg's own "No
        # secret key" would kill the script with the cryptic message this check exists to
        # replace. Dropped to Continue for exactly this one call.
        $priorEap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & $gpgProgram --batch --no-tty --list-secret-keys $signer 2>$null | Out-Null
        $gpgExit = $LASTEXITCODE
        $ErrorActionPreference = $priorEap
        if ($gpgExit -ne 0) {
            Die ("commit.gpgsign is on (from $originFile) but $gpgProgram holds no secret key " +
                 "for $signer, so the manifest commit at the end of this script would fail " +
                 "after the release is already published. Nothing in this repo's history is " +
                 "gpg-signed and the agent verifies the manifest's own signature rather than " +
                 "the commit's, so the fix is to turn it off:  $unsetHint")
        }
        Ok "Commit signing on, and $gpgProgram has a key for $signer"
    }
}

# ----------------------------------------------------------------------
# 1. Bump version in AgentConfig.cs + csproj
# ----------------------------------------------------------------------
Step "Bumping version to $Version"

# "Unchanged" has two causes that must not read the same. Either the file already carries
# this version -- normal, when the bump was made by hand in the PR that shipped the change --
# or the regex no longer matches the file, in which case the version is NOT $Version, the
# build below ships whatever the file does say, and the manifest will advertise a version no
# binary reports. So the two are told apart by looking for the target version in the text,
# and only the first one is allowed to print [ok].
$configText = Get-Content $ConfigCs -Raw
$newConfigText = $configText -replace 'public const string Version = "[\d.]+";', "public const string Version = `"$Version`";"
if ($newConfigText -eq $configText) {
    if ($configText -notmatch [regex]::Escape("public const string Version = `"$Version`";")) {
        Die "AgentConfig.cs Version line does not match the pattern and is not already $Version -- check it by hand."
    }
    Ok "AgentConfig.cs already at $Version"
} else {
    if (-not $DryRun) { Set-Content -Path $ConfigCs -Value $newConfigText -NoNewline }
    Ok "AgentConfig.cs -> $Version$(if($DryRun){' (dry-run, not written)'})"
}

$csprojText = Get-Content $Csproj -Raw
$newCsprojText = $csprojText -replace '<Version>[\d.]+</Version>', "<Version>$Version</Version>"
if ($newCsprojText -eq $csprojText) {
    if ($csprojText -notmatch [regex]::Escape("<Version>$Version</Version>")) {
        Die "TempMonitorAgent.csproj <Version> does not match the pattern and is not already $Version -- check it by hand."
    }
    Ok "TempMonitorAgent.csproj already at $Version"
} else {
    if (-not $DryRun) { Set-Content -Path $Csproj -Value $newCsprojText -NoNewline }
    Ok "TempMonitorAgent.csproj -> $Version$(if($DryRun){' (dry-run, not written)'})"
}

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

    # Print the body that would be published. A dry run that shows only the plan cannot
    # catch the failure this script exists to prevent -- notes are the one input here
    # nobody can eyeball from a command line, and the two releases that went out wrong
    # both looked fine as an invocation. Resolve the source the same way step 3 does, so
    # what is shown is what gh would read.
    if ($NotesFile) {
        if (-not (Test-Path $NotesFile)) { Die "No notes file at $NotesFile." }
        # ReadAllText(UTF8), not Get-Content -Raw: Get-Content decodes as the system ANSI
        # codepage on 5.1, which would show mojibake for the dashes and arrows the notes
        # use and send you hunting a corruption that is not in the file.
        $notesBody = [System.IO.File]::ReadAllText((Resolve-Path $NotesFile), [System.Text.Encoding]::UTF8)
        $notesFrom = $NotesFile
    } else {
        $notesBody = if ($Notes) { $Notes } else { "Agent v$Version" }
        $notesFrom = if ($Notes) { "-Notes argument" } else { "default (no notes given)" }
    }

    $notesLines = ($notesBody -split "`r?`n").Count
    Say ""
    Say "Notes from: $notesFrom  ($notesLines lines, $($notesBody.Length) chars)"
    Say ("-" * 72)
    # The console codepage is usually 437/1252 on 5.1, which prints '?' for anything
    # non-ASCII. Switch it for the duration of the dump so the preview is the text, not
    # an artifact of the terminal -- then put it back.
    $prevEnc = [Console]::OutputEncoding
    try {
        [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
        foreach ($line in ($notesBody -split "`r?`n")) { Write-Host "  | $line" }
    } finally { [Console]::OutputEncoding = $prevEnc }
    Say ("-" * 72)
    Say ""
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
        # The notes reach gh through a FILE, never as an argument, whatever form they came in.
        #
        # `--notes $Notes` cannot be made safe here. PowerShell re-quotes a string on its way
        # to a native exe, and release prose is exactly the input that breaks it. Two ways,
        # both already suffered: the escapes in `remote-helper.log` and `type` were eaten on
        # the way to the 3.26.0 release, which published as `emote-helper.log` with a literal
        # tab in it -- and a note containing a double-quoted phrase splits into several
        # arguments, which `gh release create` reads as ASSET PATHS, since everything
        # positional after the tag is a file to upload. That one killed the 3.27.0 run
        # outright: `no matches found for `commands``, from the phrase "Push commands to at
        # most" inside the notes. A release whose own prose decides whether it gets created.
        #
        # A file has none of that surface. gh reads the bytes; nothing parses them first.
        $tempNotes = $null
        try {
            if ($NotesFile) {
                if (-not (Test-Path $NotesFile)) { Die "No notes file at $NotesFile." }
                $notesPath = $NotesFile
            } else {
                # -Encoding utf8: Set-Content defaults to the system ANSI codepage on Windows
                # PowerShell 5.1, which would mangle the arrows and dashes these notes use.
                $tempNotes = Join-Path ([System.IO.Path]::GetTempPath()) "agent-release-$Version.md"
                $text = if ($Notes) { $Notes } else { "Agent v$Version" }
                Set-Content -Path $tempNotes -Value $text -Encoding utf8
                $notesPath = $tempNotes
            }
            # --prerelease on beta, so the GitHub releases list says which train a build is
            # on without anybody decoding the tag. Nothing in the fleet reads this flag --
            # the manifest filename is what decides who installs it -- it is for humans.
            $preflag = @(); if ($IsBeta) { $preflag = @("--prerelease") }
            gh release create $Tag --repo $Repo --title "Agent v$Version$(if ($IsBeta) { ' (beta)' })" --notes-file $notesPath @preflag
            if ($LASTEXITCODE -ne 0) { Die "gh release create failed." }
            Ok "Created release $Tag (notes from $notesPath)"
        } finally {
            if ($tempNotes) { Remove-Item $tempNotes -Force -ErrorAction SilentlyContinue }
        }
    }
}

# ----------------------------------------------------------------------
# 4. Sign (writes agent.manifest.json + .sig against the asset URL)
# ----------------------------------------------------------------------
Step "Signing manifest"
if ($DryRun) {
    Say "[dry-run] $py `"$SignScript`" --sign-agent --file `"$ExePath`" --agent-version $Version --agent-url $AssetUrl --key `"$keyPath`" --manifest `"$ManifestPath`""
} else {
    # --manifest picks the channel's output path. sign_release.py needed no change for
    # roadmap #21 -- it already took the path, so beta is the same signer, the same key and
    # the same bytes-exact discipline, writing one file over.
    & $py $SignScript --sign-agent --file $ExePath --agent-version $Version --agent-url $AssetUrl --key $keyPath --manifest $ManifestPath
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
        if ($LASTEXITCODE -ne 0) { Die "git add failed -- nothing was committed." }
        $staged = git diff --cached --name-only
        if (-not $staged) {
            Warn "Nothing staged (already committed?) -- skipping commit."
        } else {
            git commit -m "Release agent v$Version" | Out-Null
            # Checked like every other external call in this script, and for a while it
            # was the one that was not. A commit can fail for reasons that have nothing to
            # do with the release -- a gpg signing key this machine does not hold, a
            # pre-commit hook, an unmerged index -- and git says so on stderr, which
            # Out-Null does not swallow but the eye slides straight past when the next
            # line says [ok]. That is the worst failure this script can have: the release
            # and its asset are already published, the manifest is signed, and the ONLY
            # thing missing is the commit that puts it on main -- which is the one file
            # SelfUpdater reads. A release that stops here looks finished and reaches no
            # machine at all.
            if ($LASTEXITCODE -ne 0) {
                Die ("git commit failed -- the manifest is signed and staged but NOT " +
                     "committed, so no agent will see this release. Fix the cause above, " +
                     "then run:  git commit -m `"Release agent v$Version`"  and push.")
            }
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
  sooner if the hub echoes a newer version in its /api/report reply).

"@ -ForegroundColor Green
