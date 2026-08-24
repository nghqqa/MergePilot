# MergePilot v0.1 Preview bootstrapper (Windows + WSL2)
# -----------------------------------------------------
# Version : v0.1.0-preview.3
# Scope   : environment check, offline image install (checksum-gated),
#           start (with the explicit Windows loopback publication
#           edge), stop/cleanup with verified keepalive teardown.
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
$StateDir = Join-Path $RepoRoot ".mergepilot"
$KeepaliveFile = Join-Path $StateDir "preview-keepalive.identity.json"
$ForwarderIdentity = Join-Path $StateDir "preview-forwarder.identity.json"
$ForwarderScript = Join-Path $RepoRoot "tools\preview\loopback_forwarder.py"
$LogFile = Join-Path $RepoRoot "release\preview\logs\bootstrapper.log"
$script:LastCliExitCode = 0

function Write-Log([string]$Level, [string]$Msg) {
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Msg
    New-Item -ItemType Directory -Force -Path (Split-Path $LogFile) | Out-Null
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    Write-Host $line
}

function Get-WslText([string[]]$WslArgs) {
    # wsl.exe emits UTF-16LE; strip NULs. NO stderr redirect (the
    # UTF-16 warning deadlocks PS 5.1 native stderr readers).
    $out = (& wsl.exe @WslArgs | Out-String) -replace "`0", ""
    return $out
}

function Assert-DistroRegistered([string]$D) {
    # distro authority (§2): validate -Distro against the registered
    # set up front (DISTRO_MISMATCH), then pass it to the CLI ONLY
    # through the MERGEPILOT_WSL_DISTRO environment variable — one
    # authority, no parallel arguments, no shell injection surface.
    $list = Get-WslText @("-l", "-v")
    if ($list -notmatch [regex]::Escape($D)) {
        Write-Log "FAIL" "DISTRO_MISMATCH: '$D' is not in wsl -l -v"
        throw "DISTRO_MISMATCH: distro '$D' not registered"
    }
    return $true
}

function Invoke-BootstrapperDocker([string[]]$DockerArgs) {
    # docker invoked directly via --exec (no shell) — user input never
    # reaches a command line a shell would re-interpret. Command output
    # goes to the SUCCESS stream and would pollute the return value, so
    # it is discarded here; only the exit code travels.
    $null = & wsl.exe -u root -d $Distro --exec docker @DockerArgs
    return $LASTEXITCODE
}

function Invoke-Cli([string[]]$CliArgs) {
    Write-Log "INFO" ("cli: mergepilot " + ($CliArgs -join " "))
    Push-Location $RepoRoot
    $env:MERGEPILOT_WSL_DISTRO = $Distro
    try {
        & python $Cli @CliArgs 2>&1 | ForEach-Object { Write-Host $_ }
        $script:LastCliExitCode = $LASTEXITCODE
        if ($LASTEXITCODE -ne 0) {
            throw ("mergepilot CLI rc=" + $LASTEXITCODE + ": " + ($CliArgs -join " "))
        }
    }
    finally {
        Remove-Item Env:\MERGEPILOT_WSL_DISTRO -ErrorAction SilentlyContinue
        Pop-Location
    }
}

function Wake-Distro([string]$D) {
    # bounded wake for lifecycle commands: boot a REGISTERED but
    # dormant distro with a trivial --exec, poll its state, give up
    # loudly (the CLI independently wakes with the same bound).
    for ($i = 0; $i -lt 12; $i++) {
        $null = Get-WslText @("-d", $D, "--exec", "/bin/true")
        $lv = Get-WslText @("-l", "-v")
        if ($lv -match [regex]::Escape($D) -and $lv -match "Running") { return $true }
        Start-Sleep -Seconds 2
    }
    Write-Log "FAIL" "DISTRO_WAKE_TIMEOUT: $D did not reach Running in 24s"
    throw "DISTRO_WAKE_TIMEOUT: $D dormant and would not wake"
}

function Wait-DockerReady([string]$D) {
    # a freshly-woken distro needs a few seconds before dockerd
    # answers; bounded poll, then a stable failure
    for ($i = 0; $i -lt 20; $i++) {
        $rc = Invoke-BootstrapperDocker @("info", "--format", "ok")
        if ($rc -eq 0) { return $true }
        Start-Sleep -Seconds 1
    }
    Write-Log "FAIL" "docker not reachable inside '$D' within 20s of wake"
    throw "docker not reachable inside '$D'"
}
function New-Token { return [System.IO.Path]::GetRandomFileName() -replace "\.", "" }

function Start-Keepalive([string]$D) {
    # single persistent foreground wsl.exe sleep, identity-filed
    $tok = New-Token
    $k = Start-Process -FilePath "wsl.exe" -ArgumentList @("-d", $D, "--exec", "/bin/sleep", "14400") -PassThru -WindowStyle Hidden
    New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    [System.IO.File]::WriteAllText($KeepaliveFile, (ConvertTo-Json @{
        pid = $k.Id; name = "wsl"; distro = $D; token = $tok
        purpose = "mergepilot-preview-keepalive"
    }))
    Write-Log "INFO" ("keepalive wsl.exe pid=" + $k.Id + " token=" + $tok)
    return $k.Id
}

function Stop-OwnedProcess([string]$IdentityFile, [string]$ExpectName) {
    # verified teardown: only terminate a process whose id, process
    # NAME, and (for the forwarder) the launch TOKEN on its command
    # line all match our identity file. A recycled PID pointing at an
    # unrelated process is reported and left alone.
    if (-not (Test-Path $IdentityFile)) { return }
    $id = $null
    try { $id = Get-Content $IdentityFile -Raw | ConvertFrom-Json } catch { }
    if ($id -and $id.pid -match '^\d+$') {
        $p = Get-Process -Id ([int]$id.pid) -ErrorAction SilentlyContinue
        if ($p -and $p.ProcessName -match ("^" + $ExpectName)) {
            if ($IdentityFile -eq $ForwarderIdentity) {
                $cl = (Get-CimInstance Win32_Process -Filter ("ProcessId=" + $id.pid) -ErrorAction SilentlyContinue).CommandLine
                if ($cl -and $id.token -and ($cl -notmatch [regex]::Escape($id.token))) {
                    Write-Log "WARN" ("forwarder pid " + $id.pid + " command line lacks our token — NOT terminating")
                    Remove-Item $IdentityFile -ErrorAction SilentlyContinue
                    return
                }
            }
            Stop-Process -Id ([int]$id.pid) -Force -ErrorAction SilentlyContinue
            Write-Log "OK" ($ExpectName + " pid=" + $id.pid + " terminated (token verified)")
        }
        elseif ($p) {
            Write-Log "WARN" ("pid " + $id.pid + " is '" + $p.ProcessName + "', not " + $ExpectName + " — NOT terminating (stale identity removed)")
        }
    }
    Remove-Item $IdentityFile -ErrorAction SilentlyContinue
}

function Assert-TeardownComplete([string]$IdentityFile) {
    # bounded wait for the identity to be gone; a survivor is the
    # stable KEEPALIVE_SURVIVED blocker, never a silent leftover
    for ($i = 0; $i -lt 10; $i++) {
        if (-not (Test-Path $IdentityFile)) { return }
        Start-Sleep -Milliseconds 500
    }
    Write-Log "FAIL" ("KEEPALIVE_SURVIVED: " + (Split-Path $IdentityFile -Leaf) + " still present after bounded teardown")
    throw "KEEPALIVE_SURVIVED"
}

function Assert-Ports {
    foreach ($p in 8600, 8090) {
        $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
        if ($c) { throw "port $p already listening (stack running? run -Action Stop first)" }
    }
    return "loopback ports 8600/8090 free (Windows edge binds 127.0.0.1 only)"
}

function Test-LoopbackHttp([int]$Port, [string]$Path) {
    # real verification with the user's system proxy settings intact:
    # --noproxy '*' bypasses proxies for LOOPBACK only — we never
    # touch the user's proxy configuration
    & curl.exe -s --noproxy "*" -o NUL -w "%{http_code}" --max-time 8 ("http://127.0.0.1:" + $Port + $Path)
}

function Start-PublicationEdge {
    # explicit Windows-side loopback publication (§3): launch the
    # forwarder (127.0.0.1-only, fixed ports, token-identified), then
    # REALLY verify page + API from the Windows side. Any failure is
    # fail-closed: forwarder down + CLI stop (full rollback).
    if (-not (Test-Path $ForwarderScript)) {
        Write-Log "FAIL" "WINDOWS_LOOPBACK_PUBLICATION_UNAVAILABLE: forwarder script missing"
        throw "WINDOWS_LOOPBACK_PUBLICATION_UNAVAILABLE"
    }
    $tok = New-Token
    $f = Start-Process -FilePath "python" -ArgumentList @(
        "-u", $ForwarderScript, "--distro", $Distro,
        "--token", $tok, "--identity-file", $ForwarderIdentity
    ) -PassThru -WindowStyle Hidden
    $code = $null
    for ($i = 0; $i -lt 20; $i++) {
        if ($f.HasExited) { break }
        $code = Test-LoopbackHttp 8600 "/e2e-status.html"
        if ($code -eq "200") { break }
        Start-Sleep -Milliseconds 500
    }
    $api = Test-LoopbackHttp 8600 "/api/e2e/status"
    if (($code -ne "200") -or ($api -ne "200") -or $f.HasExited) {
        Write-Log "FAIL" ("WINDOWS_LOOPBACK_PUBLICATION_FAILED: page=" + $code + " api=" + $api + " exited=" + $f.HasExited)
        Stop-OwnedProcess $ForwarderIdentity "python"
        try { Invoke-Cli @("stop") } catch { Write-Log "WARN" "rollback stop also failed" }
        throw "WINDOWS_LOOPBACK_PUBLICATION_FAILED (rolled back)"
    }
    Write-Log "PASS" ("Windows-side loopback publication verified: page 200, api 200 (forwarder token " + $tok + ")")
}

function Stop-PublicationEdge {
    Stop-OwnedProcess $ForwarderIdentity "python"
    for ($i = 0; $i -lt 10; $i++) {
        $busy = @(Get-NetTCPConnection -LocalPort 8600, 8090 -State Listen -ErrorAction SilentlyContinue).Count
        if ($busy -eq 0) { return }
        Start-Sleep -Milliseconds 500
    }
    Write-Log "FAIL" "KEEPALIVE_SURVIVED: loopback ports still listening after forwarder teardown"
    throw "KEEPALIVE_SURVIVED (ports)"
}

Assert-DistroRegistered $Distro | Out-Null

switch ($Action) {

    "Check" {
        $v = [System.Environment]::OSVersion.Version
        Write-Log "PASS" ("Windows build " + $v.Build)
        $k = Get-WslText @("-d", $Distro, "--exec", "/bin/uname", "-r")
        if ($k -notmatch "microsoft|WSL") { throw "distro is not WSL2" }
        Write-Log "PASS" ("WSL2 distro '$Distro' kernel " + $k.Trim())
        $null = Wake-Distro $Distro
        Wait-DockerReady $Distro | Out-Null
        Write-Log "PASS" "docker in distro OK"
        Write-Log "PASS" (Assert-Ports)
        $free = [math]::Round((Get-PSDrive -Name ($RepoRoot.Substring(0, 1))).Free / 1GB, 1)
        if ($free -lt 8) { throw ("disk free " + $free + " GB < 8 GB") }
        Write-Log "PASS" ("disk free " + $free + " GB >= 8 GB")
        if (-not (Test-Path $ForwarderScript)) {
            Write-Log "FAIL" "WINDOWS_LOOPBACK_PUBLICATION_UNAVAILABLE: forwarder script missing from repo"
            throw "WINDOWS_LOOPBACK_PUBLICATION_UNAVAILABLE"
        }
        Write-Log "OK" "Check: all environment gates green"
    }

    "Install" {
        if ($ImageTar -and (Test-Path $ImageTar)) {
            Write-Log "INFO" "verifying image tar checksum before any import"
            $csFile = Join-Path (Split-Path $ImageTar -Parent) "checksums.sha256"
            $mfFile = Join-Path (Split-Path $ImageTar -Parent) "manifest.json"
            if (-not (Test-Path $csFile)) { throw "checksums.sha256 not found beside tar" }
            if (-not (Test-Path $mfFile)) { throw "manifest.json not found beside tar (placement contract)" }
            $name = Split-Path $ImageTar -Leaf
            $entry = @((Get-Content $csFile -Encoding ASCII) | Where-Object {
                $_ -match ("(?i)^\s*([0-9a-f]{64})\s+\*?" + [regex]::Escape($name) + "\s*$") })
            if ($entry.Count -eq 0) { throw "no checksums entry for '$name' — refusing an unregistered tar" }
            if ($entry.Count -gt 1) { throw "duplicate checksums entries for '$name' — refusing an ambiguous manifest" }
            $expected = ((@($entry))[0] -split '\s+')[0].ToLower()
            $actual = (Get-FileHash $ImageTar -Algorithm SHA256).Hash.ToLower()
            if ($actual -ne $expected) { throw ("image tar checksum mismatch: expected " + $expected + " got " + $actual) }
            Write-Log "PASS" ("checksum verified (" + $expected.Substring(0, 12) + ")")
            # offline completeness (§4): the manifest's image set must
            # be EXACTLY the required set (9 images incl. pgvector)
            $mf = Get-Content $mfFile -Raw | ConvertFrom-Json
            $required = @($mf.required_image_set)
            $shipped = @($mf.images.PSObject.Properties.Name)
            $missing = @($required | Where-Object { $shipped -notcontains $_ })
            $extra = @($shipped | Where-Object { $required -notcontains $_ })
            if (($missing.Count -gt 0) -or ($extra.Count -gt 0) -or ($required.Count -ne 9)) {
                Write-Log "FAIL" ("OFFLINE_IMAGE_SET_INCOMPLETE: missing=[" + ($missing -join ",") + "] extra=[" + ($extra -join ",") + "]")
                throw "OFFLINE_IMAGE_SET_INCOMPLETE — the tar cannot install offline"
            }
            Write-Log "PASS" ("offline image set exact: " + $required.Count + " images incl. pgvector")
            $null = Wake-Distro $Distro
            Wait-DockerReady $Distro | Out-Null
            $tarW = $ImageTar
            if ($tarW -match "^([A-Za-z]):\\(.*)$") {
                $tarW = "/mnt/" + $Matches[1].ToLower() + "/" + ($Matches[2].Replace("\", "/"))
            } else { throw "cannot translate path: $ImageTar" }
            $rc = Invoke-BootstrapperDocker @("load", "-i", $tarW)
            if ($rc -ne 0) { throw ("docker load failed rc=" + $rc) }
            Write-Log "OK" "images imported; verifying via CLI doctor"
            Invoke-Cli @("doctor")
            $inst = Join-Path $RepoRoot ".mergepilot\install.json"
            if ((Test-Path $mfFile) -and -not (Test-Path $inst)) {
                New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
                $json = @{ images = $mf.images } | ConvertTo-Json -Depth 4
                [System.IO.File]::WriteAllText($inst, $json)
                Write-Log "OK" "install manifest materialized from package manifest (9 digests)"
            }
            elseif (Test-Path $inst) {
                $manifests = Join-Path $RepoRoot "release\preview\manifests"
                New-Item -ItemType Directory -Force -Path $manifests | Out-Null
                $cur = Join-Path $manifests "install.current.json"
                $tmp = Join-Path $manifests "install.current.tmp"
                Copy-Item $inst $tmp -Force
                if (Test-Path $cur) { Copy-Item $cur (Join-Path $manifests "install.previous.json") -Force }
                [System.IO.File]::Copy($tmp, $cur, $true)
                Remove-Item $tmp -Force -ErrorAction SilentlyContinue
                Write-Log "OK" "install manifest archived -> install.current.json"
            }
        }
        elseif ($BuildFromSource) {
            Write-Log "INFO" "building images from source (network required)"
            Invoke-Cli @("install")
        }
        else {
            throw "Install requires -ImageTar <path> or -BuildFromSource"
        }
    }

    "Start" {
        if ($RunId -notin @("run-showcase-a", "run-showcase-b", "run-showcase-c")) {
            throw "RunId must be a seeded showcase case: run-showcase-a/b/c"
        }
        Assert-Ports | Out-Null
        $null = Wake-Distro $Distro
        Wait-DockerReady $Distro | Out-Null
        $kaPid = Start-Keepalive $Distro
        try {
            Invoke-Cli @("start", "--run-id", $RunId)
            Start-PublicationEdge
        }
        catch {
            Stop-OwnedProcess $ForwarderIdentity "python"
            Stop-OwnedProcess $KeepaliveFile "wsl"
            throw
        }
        Write-Log "OK" ("stack started; console at http://127.0.0.1:8600/e2e-status.html (keepalive pid " + $kaPid + ")")
    }

    "Status" { Invoke-Cli @("status") }
    "Doctor" { Invoke-Cli @("doctor") }

    "Stop" {
        $null = Wake-Distro $Distro
        Invoke-Cli @("stop")
        Stop-PublicationEdge
        Stop-OwnedProcess $KeepaliveFile "wsl"
        Assert-TeardownComplete $KeepaliveFile
        Assert-TeardownComplete $ForwarderIdentity
        Write-Log "OK" "stopped (images, journals and evidence retained)"
    }

    "Cleanup" {
        $null = Wake-Distro $Distro
        Invoke-Cli @("stop")
        # the CLI's cleanup is dry-run by default; --apply executes
        Invoke-Cli @("cleanup", "--apply")
        Stop-PublicationEdge
        Stop-OwnedProcess $KeepaliveFile "wsl"
        Assert-TeardownComplete $KeepaliveFile
        Assert-TeardownComplete $ForwarderIdentity
        Write-Log "OK" "cleaned (images and install manifest removed; evidence untouched)"
    }
}

# diagnostics (§6): the bootstrapper's exit code IS the last CLI exit
# code (0 when everything succeeded); the log never contains OK on a
# failed path.
exit $script:LastCliExitCode
