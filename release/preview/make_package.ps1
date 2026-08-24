# MergePilot v0.1 Preview packaging script
# ----------------------------------------
# Builds the distributable preview package under dist\preview-v0.1.0\:
#   images-oci.tar   — docker save of the 8 stack images (OCI tar)
#   package\         — copy of release\preview\ + docs\preview\ + LICENSE docs
#   checksums.sha256 — SHA-256 of every shipped file (including the tar)
#   manifest.json    — version manifest (regenerated fresh by this run)
#
# Secrets NEVER enter the package: the manifest is built from allowlisted
# sources (git rev, install.json image digests, shipped file list) only.

[CmdletBinding()]
param(
    [string]$Distro = "MergePilot-Test",
    [string]$RepoRoot = "",
    [string]$OutDir = ""
)
$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path }
if (-not $OutDir) { $OutDir = Join-Path $RepoRoot "dist\preview-v0.1.0" }

$Version = "v0.1.0-preview.1"
$Images = @(
    "mergepilot-isolated-console-edge:local",
    "mergepilot-isolated-demo-console:local",
    "mergepilot-isolated-controller:local",
    "mergepilot-isolated-policy-gateway:local",
    "mergepilot-isolated-gh-webhook:local",
    "mergepilot-isolated-gh-proxy:local",
    "mergepilot-isolated-mcp-bridge:local",
    "mergepilot-isolated-preflight:local"
)

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$pkg = Join-Path $OutDir "package"
if (Test-Path $pkg) { Remove-Item $pkg -Recurse -Force }
New-Item -ItemType Directory -Force -Path $pkg | Out-Null

Write-Host "== docker save -> images-oci.tar (this can take a few minutes)"
$tarWin = Join-Path $OutDir "images-oci.tar"
if (Test-Path $tarWin) { Remove-Item $tarWin -Force }
# docker invoked directly via --exec (no /bin/sh -c) — the derived
# path can never be re-interpreted by a shell.
$distWsl = $tarWin
if ($distWsl -match "^([A-Za-z]):\\(.*)$") {
    $distWsl = "/mnt/" + $Matches[1].ToLower() + "/" + ($Matches[2].Replace("\", "/"))
} else { throw "cannot translate path: $tarWin" }
& wsl.exe -u root -d $Distro --exec docker save -o $distWsl @Images
if ($LASTEXITCODE -ne 0) { throw "docker save failed" }

Write-Host "== copy package payload"
Copy-Item (Join-Path $RepoRoot "release\preview\bootstrapper.ps1") $pkg
Copy-Item (Join-Path $RepoRoot "release\preview\README.md") $pkg
New-Item -ItemType Directory -Force -Path (Join-Path $pkg "docs") | Out-Null
Copy-Item (Join-Path $RepoRoot "docs\preview") (Join-Path $pkg "docs") -Recurse

Write-Host "== checksums"
$cs = Join-Path $OutDir "checksums.sha256"
Remove-Item $cs -ErrorAction SilentlyContinue
$files = Get-ChildItem $OutDir -Recurse -File | Where-Object { $_.Name -ne "checksums.sha256" -and $_.Name -ne "manifest.json" }
foreach ($f in $files) {
    $h = (Get-FileHash $f.FullName -Algorithm SHA256).Hash.ToLower()
    $rel = $f.FullName.Substring($OutDir.Length + 1)
    Add-Content $cs ("$h  $rel") -Encoding ASCII
}

Write-Host "== manifest"
$git = (& git -C $RepoRoot rev-parse HEAD | Out-String).Trim()
$installJson = Get-Content (Join-Path $RepoRoot ".mergepilot\install.json") -Raw | ConvertFrom-Json
$digests = [ordered]@{}
foreach ($prop in $installJson.images.PSObject.Properties) { $digests[$prop.Name] = $prop.Value }
$manifest = [ordered]@{
    schema            = 1
    version           = $Version
    git_commit        = $git
    created_utc       = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    platform          = "windows-wsl2"
    loopback_only     = $true
    publish_ports     = @("127.0.0.1:8600", "127.0.0.1:8090")
    transport_profile = "wsl-user-relay"
    direct_routing_verified = $false
    truth_boundaries  = [ordered]@{
        application_integration_verified = "NOT_VERIFIED"
        database_verified                = "NOT_VERIFIED"
        production_verified              = "NOT_VERIFIED"
        revision_producer_contract       = "NOT_VERIFIED"
        audit_producer_contract          = "NOT_VERIFIED"
    }
    commands          = @("install", "doctor", "start --run-id RUN-ID", "status", "stop", "cleanup")
    seeded_run_ids    = @("run-showcase-a", "run-showcase-b", "run-showcase-c")
    images_oci_tar    = "images-oci.tar"
    images            = $digests
    package_files     = @($files | ForEach-Object { $_.FullName.Substring($OutDir.Length + 1) })
    secrets_included  = "none (package built from allowlisted sources; secrets dir never read)"
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $OutDir "manifest.json") -Encoding UTF8
Write-Host "== package ready: $OutDir"
Write-Host "   verify with: Get-Content $OutDir\checksums.sha256 ; compare via certutil -hashfile <file> SHA256"
