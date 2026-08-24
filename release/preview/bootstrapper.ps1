# MergePilot v0.1 Preview bootstrapper (Windows + WSL2)
# -----------------------------------------------------
# Version : v0.1.0-preview.1
# Git     : 5e10cca
# Scope   : environment check, image install (OCI tar or source build),
#           start/stop/status/doctor/cleanup for the read-only console
#           staging stack. Loopback-only publishing is enforced by the
#           stack code itself (127.0.0.1:8600 / 127.0.0.1:8090).
#
# Truth boundaries carried by this preview (NEVER flipped by deploying):
#   application_integration_verified = false
#   database_verified                 = false
#   production_verified               = false
#   revision_producer_contract        = NOT_VERIFIED
#   audit_producer_contract           = NOT_VERIFIED
#   transport_profile = wsl-user-relay, direct_routing_verified = false
#
# No secrets are read, printed, or shipped by this script.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Check", "Install", "Start", "Status", "Doctor", "Stop", "Cleanup")]
    [string]$Action,

    [string]$Distro = "MergePilot-Test",
    [string]$RunId = "run-showcase-a",
    [string]$ImageTar = "",
    [switch]$BuildFromSource,
    [string]$RepoRoot = ""
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path }
$Cli = Join-Path $RepoRoot "tools\cli\mergepilot.py"
if (-not (Test-Path $Cli)) { throw "repo layout unexpected: $Cli not found (RepoRoot=$RepoRoot)" }
$PidFile = Join-Path $RepoRoot ".mergepilot\preview-keepalive.pid"
$LogFile = Join-Path $RepoRoot "release\preview\logs\bootstrapper.log"

function Write-Log([string]$Level, [string]$Msg) {
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Msg
    New-Item -ItemType Directory -Force -Path (Split-Path $LogFile) | Out-Null
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    Write-Host $line
}

function Assert-Windows {
    if (-not $env:OS -or $env:OS -ne "Windows_NT") { throw "Windows only" }
    $v = [System.Environment]::OSVersion.Version
    if ($v.Build -lt 19041) { throw "Windows 10 2004+ (build 19041) required, got build $($v.Build)" }
    return "Windows build $($v.Build) OK"
}

function Assert-Wsl2([string]$D) {
    # wsl.exe emits UTF-16LE; strip NULs before matching
    $out = ((& wsl.exe -l -v | Out-String) -replace "`0", "")
    if ($LASTEXITCODE -ne 0) { throw "wsl.exe not usable" }
    if ($out -notmatch [regex]::Escape($D)) { throw "WSL distro '$D' not found in: wsl -l -v" }
    $mode = ((& wsl.exe -d $D --exec /bin/uname -r | Out-String) -replace "`0", "")
    if ($LASTEXITCODE -ne 0) { throw "distro '$D' not bootable" }
    if ($mode -notmatch "microsoft|WSL") { throw "distro '$D' is not WSL2 (kernel: $mode)" }
    return "WSL2 distro '$D' OK (kernel $($mode.Trim()))"
}

function Assert-Docker([string]$D) {
    # --exec runs docker directly (no shell) — user input never
    # reaches a command line that a shell would interpret
    $out = & wsl.exe -u root -d $D --exec docker info --format ok | Out-String
    if ($LASTEXITCODE -ne 0 -or ($out -replace "`0","") -notmatch "ok") { throw "docker daemon not reachable inside '$D' (boot the distro first)" }
    return "docker in '$D' OK"
}

function Assert-TarChecksum([string]$TarPath) {
    # Install gate: the tar's SHA-256 must match its checksums.sha256
    # entry BEFORE any image bytes are loaded. Missing manifest or
    # missing entry -> refuse (fail closed).
    $csFile = Join-Path (Split-Path $TarPath -Parent) "checksums.sha256"
    if (-not (Test-Path $csFile)) { throw "checksums.sha256 not found beside $TarPath — cannot verify image tar" }
    $name = Split-Path $TarPath -Leaf
    $entry = (Get-Content $csFile -Encoding ASCII) | Where-Object { $_ -match ("(?i)^\s*([0-9a-f]{64})\s+\*?" + [regex]::Escape($name) + "\s*$") }
    if (-not $entry) { throw "no checksums.sha256 entry for '$name' — refusing to install an unregistered tar" }
    if (@($entry).Count -gt 1) { throw "duplicate checksums.sha256 entries for '$name' — refusing an ambiguous manifest" }
    # @() guards the single-match case: a bare pipeline result would
    # be a scalar STRING, and indexing it would yield one character
    $expected = ((@($entry))[0] -split '\s+')[0].ToLower()
    $actual = (Get-FileHash $TarPath -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $expected) { throw "image tar checksum mismatch: expected $expected got $actual" }
    return "image tar checksum verified ($($expected.Substring(0,12))…)"
}

function Stop-OwnedKeepalive {
    # PID-ownership guard: only terminate a process we can prove is
    # a wsl.exe keepalive. A recycled PID pointing at an unrelated
    # process must never be killed.
    if (-not (Test-Path $PidFile)) { return }
    $kid = ((Get-Content $PidFile) -join "").Trim()
    if ($kid -notmatch '^\d+$') { Remove-Item $PidFile -ErrorAction SilentlyContinue; return }
    $p = Get-Process -Id ([int]$kid) -ErrorAction SilentlyContinue
    if ($p -and $p.ProcessName -match "^wsl") {
        Stop-Process -Id ([int]$kid) -Force -ErrorAction SilentlyContinue
        Write-Log "OK" "keepalive pid=$kid terminated"
    }
    elseif ($p) {
        Write-Log "WARN" "pid file $kid points at non-wsl process '$($p.ProcessName)' — NOT terminating (stale pid file removed)"
    }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
}

function Assert-Ports {
    foreach ($p in 8600, 8090) {
        $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
        if ($c) {
            throw "port $p already listening (stack running? run -Action Stop first)"
        }
    }
    return "loopback ports 8600/8090 free (stack binds 127.0.0.1 only)"
}

function Assert-Disk([string]$Path, [int]$MinGB = 8) {
    $drive = (Get-PSDrive -Name ($Path.Substring(0, 1))).Free / 1GB
    if ($drive -lt $MinGB) { throw "disk free $([math]::Round($drive,1)) GB < required $MinGB GB" }
    return "disk free $([math]::Round($drive,1)) GB >= $MinGB GB"
}

function Invoke-Cli([string[]]$CliArgs) {
    Write-Log "INFO" ("cli: mergepilot " + ($CliArgs -join " "))
    & python $Cli @CliArgs 2>&1 | ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) { throw ("mergepilot CLI rc=" + $LASTEXITCODE + ": " + ($CliArgs -join " ")) }
}

function WslPath([string]$WinPath) {
    # D:\a\b -> /mnt/d/a/b (for paths the distro must see)
    $p = (Resolve-Path $WinPath).Path
    if ($p -match "^([A-Za-z]):\\(.*)$") {
        return ("/mnt/" + $Matches[1].ToLower() + "/" + $Matches[2].Replace("\", "/"))
    }
    throw "cannot translate path: $p"
}

switch ($Action) {

    "Check" {
        $r = @()
        $r += Assert-Windows
        $r += Assert-Wsl2 $Distro
        $r += Assert-Docker $Distro
        $r += Assert-Ports
        $r += Assert-Disk $RepoRoot
        $r | ForEach-Object { Write-Log "PASS" $_ }
        Write-Log "OK" "Check: all environment gates green"
    }

    "Install" {
        if ($ImageTar -and (Test-Path $ImageTar)) {
            Write-Log "INFO" "verifying image tar checksum before any import"
            $csOk = Assert-TarChecksum $ImageTar
            Write-Log "PASS" $csOk
            Write-Log "INFO" "importing OCI images from $ImageTar"
            $tar = WslPath $ImageTar
            & wsl.exe -u root -d $Distro --exec docker load -i $tar
            if ($LASTEXITCODE -ne 0) { throw "docker load failed (rc=" + $LASTEXITCODE + ")" }
            Write-Log "OK" "images imported; verifying via CLI doctor"
            Invoke-Cli @("doctor")
        }
        elseif ($BuildFromSource) {
            Write-Log "INFO" "building images from source (network required)"
            Invoke-Cli @("install")
        }
        else {
            throw "Install requires -ImageTar <path> or -BuildFromSource"
        }
        # record the installed manifest for rollback bookkeeping,
        # archiving the previous snapshot first (ROLLBACK.md §2/§4).
        # Update strategy: stage the new snapshot in a temp file, then
        # overwrite the live copy in one Copy(overwrite) call — a crash
        # can leave (at worst) the previous snapshot still current,
        # never a half-written manifest.
        $inst = Join-Path $RepoRoot ".mergepilot\install.json"
        if (Test-Path $inst) {
            $manifests = Join-Path $RepoRoot "release\preview\manifests"
            New-Item -ItemType Directory -Force -Path $manifests | Out-Null
            $cur = Join-Path $manifests "install.current.json"
            $tmp = Join-Path $manifests "install.current.tmp"
            Copy-Item $inst $tmp -Force
            if (Test-Path $cur) {
                Copy-Item $cur (Join-Path $manifests "install.previous.json") -Force
                Write-Log "INFO" "previous install manifest archived -> install.previous.json"
            }
            [System.IO.File]::Copy($tmp, $cur, $true)
            Remove-Item $tmp -Force -ErrorAction SilentlyContinue
            Write-Log "OK" "install manifest snapshot -> release\preview\manifests\install.current.json"
        }
    }

    "Start" {
        if ($RunId -notin @("run-showcase-a", "run-showcase-b", "run-showcase-c")) {
            throw "RunId must be a seeded showcase case: run-showcase-a/b/c"
        }
        # persistent foreground keepalive process (single long sleep; no
        # scheduled tasks, no short-cycle refreshers)
        $k = Start-Process -FilePath "wsl.exe" -ArgumentList @("-d", $Distro, "--exec", "/bin/sleep", "14400") -PassThru -WindowStyle Hidden
        Set-Content -Path $PidFile -Value $k.Id -Encoding ASCII
        Write-Log "INFO" "keepalive wsl.exe pid=$($k.Id) (4h foreground sleep)"
        try {
            Invoke-Cli @("start", "--run-id", $RunId)
        }
        catch {
            $p = Get-Process -Id $k.Id -ErrorAction SilentlyContinue
            if ($p -and $p.ProcessName -match "^wsl") { Stop-Process -Id $k.Id -Force -ErrorAction SilentlyContinue }
            Remove-Item $PidFile -ErrorAction SilentlyContinue
            throw
        }
        Write-Log "OK" "stack started; console at http://127.0.0.1:8600/e2e-status.html (keepalive pid $($k.Id), ends with -Action Stop)"
    }

    "Status" { Invoke-Cli @("status") }
    "Doctor" { Invoke-Cli @("doctor") }

    "Stop" {
        Invoke-Cli @("stop")
        Stop-OwnedKeepalive
        Write-Log "OK" "stopped (images, journals and evidence retained; residue: session containers/networks/secrets removed)"
    }

    "Cleanup" {
        Invoke-Cli @("stop")
        Invoke-Cli @("cleanup")
        Stop-OwnedKeepalive
        Write-Log "OK" "cleaned (images and install manifest removed; evidence directories untouched)"
    }
}
