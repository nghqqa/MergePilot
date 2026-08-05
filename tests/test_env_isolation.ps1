# MergePilot Docker test-environment ISOLATION proof (PowerShell native entry).
#
# This script runs entirely from PowerShell — it never invokes `bash` (which on
# Windows may resolve to System32\bash.exe and start the default WSL distro).
# Instead it calls wsl.exe explicitly with -d MergePilot-Test for all test
# operations and `wsl -l -v` (list-only, does not start any distro) for the
# production-stopped check.
#
# Proves: guard fail-closed (rc=2, sentinel=NO), tcp/unix DOCKER_HOST rejected,
# canary isolated, no production/hiclaw containers in test daemon, precise
# cleanup, Ubuntu-22.04 Stopped before AND after.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File tests/test_env_isolation.ps1
param(
  [string]$RepoRoot = "/mnt/d/goai/mergepilot-os",
  [string]$TestDistro = "MergePilot-Test",
  [string]$ProdDistro = "Ubuntu-22.04"
)
$ErrorActionPreference = 'Continue'
$script:Pass = 0
$script:Fail = 0

function Gate($name, $ok) {
  if ($ok) { Write-Host "GATE PASS: $name"; $script:Pass++ }
  else { Write-Host "GATE FAIL: $name"; $script:Fail++ }
}

function Get-DistroState($name) {
  $out = (& wsl.exe -l -v 2>$null) -join "`n"
  $out = $out -replace "`0", ""
  $line = ($out -split "`n" | Where-Object { $_ -match $name }) | Select-Object -First 1
  if ($line -match 'Stopped') { return 'Stopped' }
  if ($line -match 'Running') { return 'Running' }
  return 'Unknown'
}

$RunId = "mp-iso-$([System.Diagnostics.Process]::GetCurrentProcess().Id)-$(Get-Date -Format 'yyyyMMddHHmmss')"
$Canary = "mp-canary-$RunId"
$CanaryLabel = "com.mergepilot.test_run=$RunId"

Write-Host "=== MergePilot test-env isolation proof (PowerShell, run_id=$RunId) ==="

# 0. Ubuntu-22.04 Stopped BEFORE
$stateBefore = Get-DistroState $ProdDistro
Write-Host "$ProdDistro state BEFORE: '$stateBefore'"
Gate "0. $ProdDistro Stopped BEFORE" ($stateBefore -eq 'Stopped')

# 1. fake-docker negative (guard rc=2 + sentinel=NO + tcp/sock rejected)
$negOut = (& wsl.exe -d $TestDistro -u root -- bash "$RepoRoot/tests/m5_0/fixtures/run_neg_guard.sh" "$RepoRoot/tools/test-env/mp_guard.sh" 2>$null) -join "`n"
$negOut = $negOut -replace "`0", ""
$negRc = if ($negOut -match 'GUARD_RC=(\d+)') { $Matches[1] } else { '' }
$negSent = if ($negOut -match 'SENTINEL=(\w+)') { $Matches[1] } else { '' }
$tcpRc = if ($negOut -match 'TCP_HOST_RC=(\d+)') { $Matches[1] } else { '' }
$sockRc = if ($negOut -match 'EVIL_SOCK_RC=(\d+)') { $Matches[1] } else { '' }
Write-Host "fake-docker negative: rc=$negRc sentinel=$negSent tcp_rc=$tcpRc sock_rc=$sockRc"
Gate "1a. guard fail-closed rc=2 on wrong distro" ($negRc -eq '2')
Gate "1b. zero docker calls (sentinel=NO)" ($negSent -eq 'NO')
Gate "1c. tcp:// DOCKER_HOST rejected" ($tcpRc -eq '2')
Gate "1d. arbitrary unix socket rejected" ($sockRc -eq '2')

# 2. guard passes in MergePilot-Test
$guardRc = 0
& wsl.exe -d $TestDistro -u root -- bash -c "source '$RepoRoot/tools/test-env/mp_guard.sh'" 2>$null
$guardRc = $LASTEXITCODE
Write-Host "guard on $TestDistro rc=$guardRc"
Gate "2. guard passes in $TestDistro" ($guardRc -eq 0)

# 3. canary in test daemon (unique name+label, EXIT-trap cleanup)
try {
  & wsl.exe -d $TestDistro -u root -- docker run -d --name $Canary --label $CanaryLabel busybox sleep 120 2>$null | Out-Null
  Start-Sleep -Seconds 1
  $canaryVisible = (& wsl.exe -d $TestDistro -u root -- docker ps -a --filter "name=$Canary" --format '{{.Names}}' 2>$null) -join "" -replace "`0",""
  Write-Host "test sees canary: '$canaryVisible'"
  Gate "3. canary visible in test daemon" ($canaryVisible.Trim() -eq $Canary)
} finally {
  # precise cleanup
  & wsl.exe -d $TestDistro -u root -- docker rm -f $Canary 2>$null | Out-Null
}

# 4. no production / hiclaw containers in test daemon
$prodVisible = @()
foreach ($c in @('mergepilot-controller','policy-gw','audit-pg','github-mcp','hiclaw-manager','hiclaw-controller')) {
  & wsl.exe -d $TestDistro -u root -- docker inspect $c 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) { $prodVisible += $c }
}
$forbidden = (& wsl.exe -d $TestDistro -u root -- docker ps -a --format '{{.Names}}' 2>$null) -join "`n" -replace "`0",""
$forbiddenCount = ($forbidden -split "`n" | Where-Object { $_ -match 'hiclaw-worker|hiclaw-manager|hiclaw-controller' }).Count
Write-Host "prod visible: '$($prodVisible -join ' ')'; forbidden: $forbiddenCount"
Gate "4a. no production container visible from test" ($prodVisible.Count -eq 0)
Gate "4b. no hiclaw-worker/manager/controller in test" ($forbiddenCount -eq 0)

# 5. residue
$canaryResidue = (& wsl.exe -d $TestDistro -u root -- docker ps -a --filter "label=$CanaryLabel" --format '{{.Names}}' 2>$null) -join "" -replace "`0",""
$resCount = if ($canaryResidue.Trim()) { ($canaryResidue.Trim() -split "`n").Count } else { 0 }
Write-Host "canary residue: $resCount (expect 0)"
Gate "5. precise canary cleanup (residue=0)" ($resCount -eq 0)

# 6. Ubuntu-22.04 Stopped AFTER
$stateAfter = Get-DistroState $ProdDistro
Write-Host "$ProdDistro state AFTER: '$stateAfter'"
Gate "6. $ProdDistro Stopped AFTER (never touched)" ($stateAfter -eq 'Stopped')

Write-Host "=== SUMMARY: PASS=$script:Pass FAIL=$script:Fail ==="
if ($script:Fail -gt 0) { exit 1 } else { exit 0 }
