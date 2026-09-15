<#
.SYNOPSIS
    Build hub/static/css/app.css from hub/static/css/tailwind.input.css with the Tailwind
    standalone CLI.

.DESCRIPTION
    Why a script rather than "install Tailwind": the hub has no npm and must not grow one
    (CLAUDE.md). Tailwind ships a single self-contained binary per platform, so this pins one
    version, downloads it once into tools/.bin/ (gitignored), refuses it unless its SHA-256
    matches the digest GitHub published for that release, and runs it.

    **app.css is committed.** Hub self-update mirrors hub/ as plain files; a deployed hub never
    runs this script. Forgetting to rebuild after changing classes means the class silently has
    no CSS. tests/test_tailwind_build.py pins the pipeline's shape (pinned digest, no preflight,
    app.css present and linked), but it cannot know which classes you meant -- rebuild.

    Windows PowerShell 5.1 compatible, like install.ps1: no ternaries, no ??, Get-FileHash
    rather than a .NET API missing on Framework 4.x.

.PARAMETER Watch
    Rebuild on every change while editing. Not minified, so a diff is readable -- but build
    once more without -Watch before committing.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\build_css.ps1
#>
param(
    [switch]$Watch
)

$ErrorActionPreference = 'Stop'

# Bump both together. The digest is the one GitHub publishes on the release asset
# (gh api repos/tailwindlabs/tailwindcss/releases/tags/<tag>), not one computed from whatever
# happened to be downloaded -- trusting the first download would pin a tampered binary as
# readily as a good one.
$TailwindVersion = 'v4.3.3'
$TailwindSha256  = 'e0e260ce048014e9268f6237ff18f8ccf02cef521cbd0ae04e82c2cdf7aa3955'

$repoRoot = Split-Path -Parent $PSScriptRoot
$binDir   = Join-Path $PSScriptRoot '.bin'
$exe      = Join-Path $binDir "tailwindcss-$TailwindVersion.exe"
# Not $input: that is a PowerShell automatic variable (the pipeline enumerator), and assigning
# to it works in a script right up until something reads it back as the pipeline.
$source   = Join-Path $repoRoot 'hub\static\css\tailwind.input.css'
$output   = Join-Path $repoRoot 'hub\static\css\app.css'

function Test-Digest([string]$path) {
    $actual = (Get-FileHash -Algorithm SHA256 -Path $path).Hash.ToLowerInvariant()
    return $actual -eq $TailwindSha256
}

if (-not (Test-Path $exe)) {
    New-Item -ItemType Directory -Force -Path $binDir | Out-Null
    $url = "https://github.com/tailwindlabs/tailwindcss/releases/download/$TailwindVersion/tailwindcss-windows-x64.exe"
    $partial = "$exe.download"
    Write-Host "Downloading Tailwind CLI $TailwindVersion (~110 MB) ..."
    # TLS 1.2 explicitly: PowerShell 5.1 on an older .NET Framework defaults to protocols
    # GitHub refuses, and the failure reads as a generic connection error.
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $ProgressPreference = 'SilentlyContinue'   # the progress bar makes 5.1 downloads ~10x slower
    Invoke-WebRequest -Uri $url -OutFile $partial -UseBasicParsing
    if (-not (Test-Digest $partial)) {
        Remove-Item -Force $partial
        throw "Tailwind CLI digest mismatch for $TailwindVersion -- refusing to run it."
    }
    Move-Item -Force $partial $exe
}
elseif (-not (Test-Digest $exe)) {
    throw "tools/.bin/tailwindcss-$TailwindVersion.exe does not match its pinned digest. Delete it and run again."
}

$arguments = @('--input', $source, '--output', $output, '--cwd', $repoRoot)
if ($Watch) {
    $arguments += '--watch'
} else {
    $arguments += '--minify'
}

& $exe @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Tailwind CLI exited with $LASTEXITCODE"
}
