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

$Version = "v0.1.0-preview.4-rc.2"
# REQUIRED_IMAGE_SET: everything `doctor`/`start` needs to run with a
# BLANK image cache and NO network pull — the 8 built images plus the
# digest-locked pgvector base. The bootstrapper's Install gate checks
# the manifest against exactly this set before docker load.
$Images = @(
    "mergepilot-isolated-console-edge:local",
    "mergepilot-isolated-demo-console:local",
    "mergepilot-isolated-controller:local",
    "mergepilot-isolated-policy-gateway:local",
    "mergepilot-isolated-gh-webhook:local",
    "mergepilot-isolated-gh-proxy:local",
    "mergepilot-isolated-mcp-bridge:local",
    "mergepilot-isolated-preflight:local",
    "pgvector/pgvector:pg16"
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

Write-Host "== copy package payload (standalone: bundled CLI + forwarder)"
Copy-Item (Join-Path $RepoRoot "release\preview\bootstrapper.ps1") $pkg
Copy-Item (Join-Path $RepoRoot "release\preview\README.md") $pkg
New-Item -ItemType Directory -Force -Path (Join-Path $pkg "docs") | Out-Null
Copy-Item (Join-Path $RepoRoot "docs\preview") (Join-Path $pkg "docs") -Recurse
# Standalone CLI payload: mirror the FULL tools/ + config/ trees so the
# CLI's path resolution AND runtime file reads (policy.yaml, migrations,
# audit-db SQL, room-map) work unchanged from the extracted ZIP.
# m9-f §2 hygiene: Copy-Item -Exclude only filters the TOP level; we
# copy first, then recursively STRIP all cache artifacts and verify.
Copy-Item (Join-Path $RepoRoot "tools") (Join-Path $pkg "tools") -Recurse
New-Item -ItemType Directory -Force -Path (Join-Path $pkg "config") | Out-Null
Copy-Item (Join-Path $RepoRoot "config\gh-app") (Join-Path $pkg "config\gh-app") -Recurse

# ── m9-f §2: package hygiene — recursive cache strip + fail-closed verify ──
$CacheDirs = @("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")
$CacheFiles = @("*.pyc", "*.pyo", "*.coverage", ".coverage")
foreach ($cd in $CacheDirs) {
    Get-ChildItem $pkg -Recurse -Directory -Filter $cd -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force
}
foreach ($cf in $CacheFiles) {
    Get-ChildItem $pkg -Recurse -File -Filter $cf -ErrorAction SilentlyContinue |
        Remove-Item -Force
}
# fail-closed: if any cache still exists after the strip, abort the build
$leftover = @(Get-ChildItem $pkg -Recurse -Force | Where-Object {
    $_.Name -in $CacheDirs -or $_.Name -match '\.py[co]$' -or
    $_.Name -eq '.coverage' -or $_.Name -eq '*.coverage'
})
if ($leftover.Count -gt 0) {
    throw "PACKAGE_HYGIENE_FAILED: $($leftover.Count) cache artifacts remain: $($leftover | Select-Object -First 3 | ForEach-Object { $_.FullName })"
}
# also verify no EMPTY directories remain from the strip
$emptyDirs = @(Get-ChildItem $pkg -Recurse -Directory | Where-Object {
    @(Get-ChildItem $_.FullName -Recurse -Force -ErrorAction SilentlyContinue).Count -eq 0
})
foreach ($ed in $emptyDirs) { Remove-Item $ed.FullName -Force -ErrorAction SilentlyContinue }
Write-Host "== package hygiene: 0 cache artifacts, 0 empty dirs"

Write-Host "== checksums"
$cs = Join-Path $OutDir "checksums.sha256"
Remove-Item $cs -ErrorAction SilentlyContinue
$files = Get-ChildItem $OutDir -Recurse -File | Where-Object { $_.Name -ne "checksums.sha256" -and $_.Name -ne "manifest.json" }
# m9 defect A: `Add-Content` writes CRLF and $rel carries backslashes,
# which breaks `sha256sum -c` on every non-Windows toolchain. Build the
# file with LF newlines and forward-slash paths instead.
$lines = foreach ($f in $files) {
    $h = (Get-FileHash $f.FullName -Algorithm SHA256).Hash.ToLower()
    $rel = $f.FullName.Substring($OutDir.Length + 1).Replace("\", "/")
    "$h  $rel"
}
[System.IO.File]::WriteAllText($cs, ($lines -join "`n") + "`n",
                                [System.Text.UTF8Encoding]::new($false))

Write-Host "== manifest"
$git = (& git -C $RepoRoot rev-parse HEAD | Out-String).Trim()
# Image digests MUST be derivable from the shipped tar itself, otherwise
# a user cannot verify manifest.json against images-oci.tar. We read the
# OCI layout's index.json from the tar and map each image name to its
# manifest digest (the digest a verifier can recompute from the tar).
# System32 tar explicitly: an MSYS/Git tar earlier on PATH
# misparses the drive-letter path as a remote host
$winTar = Join-Path $env:SystemRoot "System32\tar.exe"
$indexJson = (& $winTar -xOf $tarWin "index.json" | Out-String)
if ($LASTEXITCODE -ne 0 -or -not $indexJson) { throw "cannot read index.json from images-oci.tar - digest verification impossible" }
$ociIndex = $indexJson | ConvertFrom-Json
$digests = [ordered]@{}
foreach ($m in $ociIndex.manifests) {
    $name = $m.annotations.'io.containerd.image.name'
    if (-not $name) { throw "OCI manifest entry without io.containerd.image.name annotation" }
    $name = $name -replace '^docker\.io/(library/)?', ''
    $digests[$name] = $m.digest
}
if ($digests.Count -ne $Images.Count) { throw "expected $($Images.Count) image digests from tar index, got $($digests.Count)" }
foreach ($expected in $Images) { if (-not $digests[$expected]) { throw "image '$expected' missing from tar index.json" } }
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
    required_image_set = @($Images)
    seeded_run_ids    = @("run-showcase-a", "run-showcase-b", "run-showcase-c")
    images_oci_tar    = "images-oci.tar"
    images            = $digests
    package_files     = @($files | ForEach-Object { $_.FullName.Substring($OutDir.Length + 1) })
    secrets_included  = "none (package built from allowlisted sources; secrets dir never read)"
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $OutDir "manifest.json") -Encoding UTF8
Write-Host "== package ready: $OutDir"
Write-Host "   verify with: Get-Content $OutDir\checksums.sha256 ; compare via certutil -hashfile <file> SHA256"
