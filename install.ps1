<#
    George -- one-line Windows installer.

        irm https://raw.githubusercontent.com/the-priest/George/main/install.ps1 | iex

    The Windows counterpart to install.sh. Same shape: fetch the build,
    put it somewhere sensible, make sure Ollama is there, pull a model,
    leave shortcuts and a way to undo it.

    Piped into iex there is no way to pass parameters, so everything is
    driven by environment variables, exactly like the shell installer:

        $env:GEORGE_REPO   = "the-priest/George"    # owner/name
        $env:GEORGE_MODEL  = "qwen3:4b"             # "" to skip the pull
        $env:GEORGE_PREFIX = "..."                  # install directory
        $env:GEORGE_YES    = "1"                    # never ask
        $env:GEORGE_UNINSTALL = "1"                 # remove it again

    Per-user throughout. Nothing here needs an administrator.
#>

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Repo    = if ($env:GEORGE_REPO)   { $env:GEORGE_REPO }   else { 'the-priest/George' }
$Model   = if ($null -ne $env:GEORGE_MODEL) { $env:GEORGE_MODEL } else { 'qwen3:4b' }
$Prefix  = if ($env:GEORGE_PREFIX) { $env:GEORGE_PREFIX } else { "$env:LOCALAPPDATA\Programs\George" }
$AssumeYes = [bool]$env:GEORGE_YES

function Say  ($m) { Write-Host "  $m" -ForegroundColor Cyan }
function Note ($m) { Write-Host "  $m" -ForegroundColor DarkGray }
function Warn ($m) { Write-Host "  $m" -ForegroundColor Yellow }
function Die  ($m) { Write-Host "  $m" -ForegroundColor Red; exit 1 }

function Ask ($question, $defaultYes = $true) {
    if ($AssumeYes) { return $true }
    $hint = if ($defaultYes) { '[Y/n]' } else { '[y/N]' }
    $answer = Read-Host "  $question $hint"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $defaultYes }
    return $answer -match '^[Yy]'
}

# A destructive question defaults to NO and is never answered by
# GEORGE_YES. The shell installer learned this the hard way: a question
# with nobody there to answer it is not consent.
function AskDestructive ($question) {
    $answer = Read-Host "  $question [y/N]"
    return $answer -match '^[Yy]'
}

function Find-Exe ($name) {
    $hit = Get-Command $name -ErrorAction SilentlyContinue
    if ($hit) { return $hit.Source }
    foreach ($dir in @(
        "$env:LOCALAPPDATA\Programs\Ollama",
        "$env:ProgramFiles\Ollama",
        "${env:ProgramFiles(x86)}\Ollama")) {
        $candidate = Join-Path $dir "$name.exe"
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

function New-Shortcut ($linkPath, $target, $iconPath) {
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($linkPath)
    $link.TargetPath = $target
    $link.WorkingDirectory = Split-Path $target
    $link.IconLocation = $iconPath
    $link.Description = 'George - local desktop AI'
    $link.Save()
}

# ---------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------
if ($env:GEORGE_UNINSTALL) {
    Write-Host ''
    Say 'Removing George'
    $startMenu = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\George.lnk"
    foreach ($link in @($startMenu, "$env:USERPROFILE\Desktop\George.lnk")) {
        if (Test-Path $link) { Remove-Item $link -Force; Note "removed $link" }
    }
    if (Test-Path $Prefix) { Remove-Item $Prefix -Recurse -Force; Note "removed $Prefix" }

    # His notes, chats and settings are his. Ask separately, default no.
    $data = "$env:LOCALAPPDATA\George"
    $conf = "$env:APPDATA\George"
    if ((Test-Path $data) -or (Test-Path $conf)) {
        if (AskDestructive 'Also delete your config, memory, notes and saved chats?') {
            foreach ($d in @($data, $conf)) {
                if (Test-Path $d) { Remove-Item $d -Recurse -Force; Note "removed $d" }
            }
        } else {
            Note 'kept your config and notes'
        }
    }
    Say 'Done.'
    exit 0
}

# ---------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------
Write-Host ''
Write-Host '  George' -ForegroundColor Cyan
Write-Host '  local desktop AI, no API keys' -ForegroundColor DarkGray
Write-Host ''

if ([Environment]::Is64BitOperatingSystem -eq $false) {
    Die 'George is built for 64-bit Windows only.'
}
if ([Environment]::OSVersion.Version.Major -lt 10) {
    Warn 'Windows 10 or later is expected; older versions are untested.'
}

# ---- Ollama ----------------------------------------------------------
$ollama = Find-Exe 'ollama'
if (-not $ollama) {
    Say 'Ollama is not installed - George needs it to think.'
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        if (Ask 'Install Ollama with winget now?') {
            winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements
            $ollama = Find-Exe 'ollama'
        }
    }
    if (-not $ollama) {
        Warn 'Install Ollama yourself from https://ollama.com/download/windows'
        Warn 'then run this script again.'
    }
} else {
    Note "found ollama at $ollama"
}

# ---- fetch the build -------------------------------------------------
Say "Looking for the latest release of $Repo"
$api = "https://api.github.com/repos/$Repo/releases/latest"
try {
    $release = Invoke-RestMethod -Uri $api -Headers @{ 'User-Agent' = 'george-installer' }
} catch {
    Die "could not reach $api - is the repo public and does it have a release yet?"
}

$asset = $release.assets | Where-Object { $_.name -like 'George-portable-*win64.zip' } | Select-Object -First 1
if (-not $asset) {
    Die "release $($release.tag_name) has no portable zip attached - has the Windows workflow run?"
}
Note "found $($asset.name)"

$tmp = Join-Path $env:TEMP "george-install-$(Get-Random)"
New-Item -ItemType Directory -Path $tmp -Force | Out-Null
$zip = Join-Path $tmp $asset.name
try {
    Say 'Downloading'
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip -UseBasicParsing

    # Unpack beside the target and swap, so a failed download never
    # leaves a half-replaced installation behind.
    $staging = Join-Path $tmp 'George'
    Expand-Archive -Path $zip -DestinationPath $staging -Force
    if (-not (Test-Path (Join-Path $staging 'George.exe'))) {
        Die 'the downloaded archive has no George.exe in it'
    }

    if (Test-Path $Prefix) {
        Get-Process -Name 'George' -ErrorAction SilentlyContinue |
            ForEach-Object { Warn 'George is running - closing it'; $_.CloseMainWindow() | Out-Null; Start-Sleep 2 }
        Remove-Item $Prefix -Recurse -Force
    }
    New-Item -ItemType Directory -Path (Split-Path $Prefix) -Force | Out-Null
    Move-Item $staging $Prefix
    Say "Installed to $Prefix"
} finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

# ---- shortcuts -------------------------------------------------------
$exe = Join-Path $Prefix 'George.exe'
$startMenuDir = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs"
New-Shortcut (Join-Path $startMenuDir 'George.lnk') $exe $exe
Note 'added a Start Menu entry'
if (Ask 'Put a shortcut on the desktop too?') {
    New-Shortcut "$env:USERPROFILE\Desktop\George.lnk" $exe $exe
    Note 'added a desktop shortcut'
}

# ---- model -----------------------------------------------------------
if ($Model -and $ollama) {
    Say "Pulling $Model (this is the slow part)"
    & $ollama pull $Model
    if ($LASTEXITCODE -ne 0) {
        Warn "the pull failed - you can do it later from inside George, under Menu > Models"
    }
} elseif (-not $Model) {
    Note 'skipping the model pull'
}

Write-Host ''
Say 'George is installed.'
Note "Start him from the Start Menu, or run: $exe"
Note "Uninstall with: `$env:GEORGE_UNINSTALL=1; irm https://raw.githubusercontent.com/$Repo/main/install.ps1 | iex"
Note 'If the window comes up black, use the "safe graphics" launcher: george-safe.cmd'
Write-Host ''
