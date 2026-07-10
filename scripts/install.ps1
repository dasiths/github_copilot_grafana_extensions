<#
.SYNOPSIS
  Bootstrap installer for the GitHub Copilot telemetry + Grafana LGTM stack (PowerShell).

.DESCRIPTION
  Windows / PowerShell counterpart of scripts/install.sh. Run it straight from
  the network:

      irm https://raw.githubusercontent.com/dasiths/github_copilot_grafana_extensions/main/scripts/install.ps1 | iex

  To pass parameters (for example -Uninstall) when running from the network,
  wrap it in a scriptblock:

      & ([scriptblock]::Create((irm https://raw.githubusercontent.com/dasiths/github_copilot_grafana_extensions/main/scripts/install.ps1))) -Uninstall

  It is interactive (Read-Host prompts). With -Yes, or in a non-interactive
  host, it uses defaults and prints the manual steps instead of editing anything.

  Steps:
    1. Checks prerequisites (Docker is optional).
    2. Downloads the repo assets (compose file, Grafana provisioning, otel
       collector config, agent-insights sidecar, CLI scripts) into
       $HOME\.agents\telemetry\copilot-extensions (override with -InstallDir).
    3. Optionally starts the LGTM stack with 'docker compose up -d'.
    4. Prints the VS Code User settings to paste in (does NOT edit settings.json).
    5. Optionally adds '. copilot-cli-otel.ps1' to your PowerShell profile.

.PARAMETER Uninstall
  Remove the profile block, stop the stack, and optionally delete the assets.

.PARAMETER Yes
  Assume "yes"/defaults; do not prompt.

.PARAMETER InstallDir
  Where to place the assets. Defaults to $HOME\.agents\telemetry\copilot-extensions.

.PARAMETER Repo
  Source repository (default dasiths/github_copilot_grafana_extensions).

.PARAMETER Ref
  Git ref / branch to download (default main).
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$Yes,
    [string]$InstallDir,
    [string]$Repo,
    [string]$Ref
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# --- Configuration (params win, then env vars, then defaults) -----------------
if (-not $InstallDir) {
    $InstallDir = if ($env:INSTALL_DIR) { $env:INSTALL_DIR }
    else { Join-Path (Join-Path (Join-Path $HOME ".agents") "telemetry") "copilot-extensions" }
}
if (-not $Repo) { $Repo = if ($env:REPO) { $env:REPO } else { "dasiths/github_copilot_grafana_extensions" } }
if (-not $Ref) { $Ref = if ($env:REF) { $env:REF } else { "main" } }

$MarkerBegin = "# >>> copilot-grafana telemetry >>>"
$MarkerEnd = "# <<< copilot-grafana telemetry <<<"

$script:Interactive = (-not $Yes) -and [Environment]::UserInteractive

# --- Logging ------------------------------------------------------------------
function Write-Info([string]$m) { Write-Host "==> $m" -ForegroundColor Blue }
function Write-Step([string]$m) { Write-Host ""; Write-Host "==> $m" -ForegroundColor Blue }
function Write-Ok([string]$m) { Write-Host "[ok] $m" -ForegroundColor Green }
function Write-Warn2([string]$m) { Write-Host "[!]  $m" -ForegroundColor Yellow }
function Write-Err2([string]$m) { Write-Host "[x]  $m" -ForegroundColor Red }

# --- Prompt helpers -----------------------------------------------------------
function Read-Answer([string]$Prompt, [string]$Default) {
    if (-not $script:Interactive) { return $Default }
    $label = if ($Default) { "$Prompt [$Default]" } else { $Prompt }
    try { $ans = Read-Host $label } catch { return $Default }
    if ([string]::IsNullOrWhiteSpace($ans)) { return $Default }
    return $ans
}

function Confirm-Action([string]$Question, [string]$Default = "y") {
    if ($Yes -or -not $script:Interactive) { return ($Default -eq "y") }
    $hint = if ($Default -eq "y") { "[Y/n]" } else { "[y/N]" }
    try { $ans = Read-Host "$Question $hint" } catch { return ($Default -eq "y") }
    if ([string]::IsNullOrWhiteSpace($ans)) { $ans = $Default }
    return ($ans -match '^(y|yes)$')
}

function Test-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { return $false }
    try { docker compose version *> $null; return ($LASTEXITCODE -eq 0) } catch { return $false }
}

# --- Profile helpers ----------------------------------------------------------
function Remove-Block([string]$File) {
    if (-not (Test-Path -LiteralPath $File)) { return }
    $content = @(Get-Content -LiteralPath $File)
    if ($content -notcontains $MarkerBegin) { return }
    $out = New-Object System.Collections.Generic.List[string]
    $skip = $false
    foreach ($line in $content) {
        if ($line -eq $MarkerBegin) { $skip = $true; continue }
        if ($line -eq $MarkerEnd) { $skip = $false; continue }
        if (-not $skip) { $out.Add($line) }
    }
    Set-Content -LiteralPath $File -Value $out
    Write-Ok "Removed telemetry block from $File"
}

# --- Steps --------------------------------------------------------------------
function Invoke-CheckPrereqs {
    Write-Step "Checking prerequisites"
    if (Test-Docker) {
        Write-Ok "Docker with Compose v2 found."
    }
    else {
        Write-Warn2 "Docker (with 'docker compose') not found - you can still download the"
        Write-Warn2 "assets now and start the stack later with 'docker compose up -d'."
    }
}

function Invoke-DownloadAssets {
    Write-Step "Downloading assets"
    $target = Read-Answer "Install directory" $InstallDir
    $script:InstallDir = $target

    if ((Test-Path -LiteralPath $InstallDir) -and (Get-ChildItem -LiteralPath $InstallDir -Force -ErrorAction SilentlyContinue)) {
        if (-not (Confirm-Action "$InstallDir already exists. Update it in place?" "y")) {
            throw "Aborted."
        }
    }

    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ([System.IO.Path]::GetRandomFileName())
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    $zip = Join-Path $tmp "src.zip"
    $zipUrl = "https://codeload.github.com/$Repo/zip/refs/heads/$Ref"
    Write-Info "Fetching $Repo@$Ref ..."
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    }
    catch { }
    try {
        Invoke-WebRequest -Uri $zipUrl -OutFile $zip -UseBasicParsing
        Expand-Archive -Path $zip -DestinationPath $tmp -Force
    }
    catch {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
        throw "Download or extraction failed from $zipUrl"
    }

    $src = Get-ChildItem -LiteralPath $tmp -Directory | Select-Object -First 1
    if (-not $src) {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
        throw "Unexpected archive layout."
    }

    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    foreach ($item in @('docker-compose.yml', 'grafana', 'otelcol', 'agent-insights', 'scripts')) {
        $s = Join-Path $src.FullName $item
        if (Test-Path -LiteralPath $s) {
            if (Test-Path -LiteralPath $s -PathType Container) {
                $dest = Join-Path $InstallDir $item
                New-Item -ItemType Directory -Force -Path $dest | Out-Null
                Copy-Item -Path (Join-Path $s '*') -Destination $dest -Recurse -Force
            }
            else {
                Copy-Item -Path $s -Destination $InstallDir -Force
            }
        }
        else {
            Write-Warn2 "Asset '$item' missing from download; skipping."
        }
    }
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    Write-Ok "Assets installed to $InstallDir"
}

function Invoke-StartStack {
    Write-Step "Docker stack"
    if (-not (Test-Docker)) {
        Write-Warn2 "Docker not available; skipping. Start later with:"
        Write-Host "    cd `"$InstallDir`"; docker compose up -d"
        return
    }
    if (Confirm-Action "Start the LGTM stack now with 'docker compose up -d'?" "y") {
        Push-Location $InstallDir
        try {
            docker compose up -d
            if ($LASTEXITCODE -eq 0) {
                Write-Ok "Stack started. Grafana: http://localhost:3000 (admin / admin)"
            }
            else {
                Write-Warn2 "Failed to start the stack. Retry with: cd `"$InstallDir`"; docker compose up -d"
            }
        }
        finally { Pop-Location }
    }
    else {
        Write-Info "Skipped. Start it later with: cd `"$InstallDir`"; docker compose up -d"
    }
}

function Show-VSCodeSettings {
    Write-Step "VS Code Copilot Chat settings"
    @'
These settings are APPLICATION-SCOPED - add them to your USER settings.json
(Command Palette -> "Preferences: Open User Settings (JSON)"), not a workspace
file. Merge in the following keys:

  {
    "github.copilot.chat.otel.enabled": true,
    "github.copilot.chat.otel.exporterType": "otlp-http",
    "github.copilot.chat.otel.otlpEndpoint": "http://localhost:4318",

    // Optional: capture full prompt/response/tool content into spans.
    // WARNING: may include source code / sensitive data. Local, trusted use only.
    "github.copilot.chat.otel.captureContent": false
  }
'@ | Write-Host
}

function Set-Profile {
    Write-Step "Copilot CLI telemetry (PowerShell profile)"
    $scriptPath = Join-Path (Join-Path $InstallDir "scripts") "copilot-cli-otel.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath)) {
        Write-Warn2 "CLI script not found at $scriptPath; skipping profile setup."
        return
    }

    Write-Info "To send Copilot CLI telemetry to the stack, this line must run in your session:"
    Write-Host "    . `"$scriptPath`""
    Write-Host ""
    if (-not (Confirm-Action "Add that line to your PowerShell profile so it loads in new sessions?" "y")) {
        Write-Info "Skipped. Dot-source the line above manually, or add it to your profile later."
        return
    }

    $profilePath = Read-Answer "Profile file to edit" $PROFILE

    if ((Test-Path -LiteralPath $profilePath) -and ((Get-Content -LiteralPath $profilePath) -contains $MarkerBegin)) {
        Write-Ok "Profile $profilePath already has the telemetry block; leaving it unchanged."
        return
    }

    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $profilePath) | Out-Null
    $block = @(
        ""
        $MarkerBegin
        "# Added by copilot-grafana telemetry install.ps1 - remove with install.ps1 -Uninstall"
        ". `"$scriptPath`""
        $MarkerEnd
    )
    Add-Content -LiteralPath $profilePath -Value $block
    Write-Ok "Added telemetry block to $profilePath"
    Write-Info "Open a new session (or dot-source the line above) to enable it now."
}

function Show-Summary {
    Write-Step "Done"
    @"
Assets:     $InstallDir
Grafana:    http://localhost:3000  (admin / admin)
Stack:      cd "$InstallDir"; docker compose up -d   |   docker compose down
Uninstall:  & "$InstallDir\scripts\install.ps1" -Uninstall

Next: generate some Copilot activity (VS Code chat/agent, or 'copilot' in a
session where the telemetry script is dot-sourced), then open Grafana.
"@ | Write-Host
}

function Invoke-Uninstall {
    Write-Step "Uninstall"
    Write-Info "This removes the profile block, optionally stops the stack, and"
    Write-Info "optionally deletes the assets at $InstallDir."

    if ((Test-Path -LiteralPath (Join-Path $InstallDir "docker-compose.yml")) -and (Test-Docker)) {
        if (Confirm-Action "Stop and remove the Docker stack (docker compose down)?" "y") {
            Push-Location $InstallDir
            try { docker compose down } catch { Write-Warn2 "docker compose down failed." } finally { Pop-Location }
        }
    }

    foreach ($f in @($PROFILE, $PROFILE.CurrentUserAllHosts, $PROFILE.AllUsersCurrentHost)) {
        if ($f) { Remove-Block $f }
    }

    if ((Test-Path -LiteralPath $InstallDir) -and (Confirm-Action "Delete assets directory $InstallDir?" "n")) {
        Remove-Item -Recurse -Force $InstallDir
        Write-Ok "Deleted $InstallDir"
    }

    Write-Ok "Uninstall complete. Start a new session to clear the OTEL_* env vars."
}

# --- Main ---------------------------------------------------------------------
if ($Uninstall) {
    Invoke-Uninstall
    return
}

Write-Host "GitHub Copilot telemetry -> Grafana LGTM - installer" -ForegroundColor White
Write-Host "Repo $Repo@$Ref"
if (-not $script:Interactive) { Write-Info "Non-interactive mode: using defaults." }

Invoke-CheckPrereqs
Invoke-DownloadAssets
Invoke-StartStack
Show-VSCodeSettings
Set-Profile
Show-Summary
