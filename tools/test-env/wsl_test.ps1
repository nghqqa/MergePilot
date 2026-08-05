# MergePilot unified safe entry (PowerShell) — official Windows entry point.
#
# Forces wsl.exe -d MergePilot-Test -u root (never the default Ubuntu-22.04
# distro). Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools/test-env/wsl_test.ps1 `
#           -RepoRoot /mnt/d/goai/mergepilot-os `
#           -InnerScript tests/m5_0/run_m5_0b_integration.sh
#
# Exit codes (frozen):
#   64  missing required parameter
#   65  RepoRoot is not an absolute /mnt WSL path
#   66  InnerScript is an absolute path (must be relative)
#   67  RepoRoot or InnerScript contains '..'
#   68  launcher cannot cd to RepoRoot (reported by mp_launch.sh)
#   69  RepoRoot is not a directory or InnerScript file does not exist
#   inner's own non-zero exit code is propagated as-is
param(
  [string]$RepoRoot,
  [string]$InnerScript,
  [Parameter(ValueFromRemainingArguments=$true)][string[]]$InnerArgs
)

# NOTE: do NOT use Write-Error here — under $ErrorActionPreference='Stop' it
# throws a terminating error that makes the subsequent `exit N` unreachable
# (the process would exit rc=1 instead of the intended N). Use
# [Console]::Error.WriteLine + explicit exit.

if (-not $RepoRoot -or -not $InnerScript) {
  [Console]::Error.WriteLine("wsl_test.ps1: RepoRoot and InnerScript are required")
  exit 64
}
if ($InnerScript -match '\.\.' -or $RepoRoot -match '\.\.') {
  [Console]::Error.WriteLine("wsl_test.ps1: '..' is forbidden in RepoRoot or InnerScript")
  exit 67
}
if ($InnerScript -match '^/|^[A-Za-z]:') {
  [Console]::Error.WriteLine("wsl_test.ps1: InnerScript must be relative (got '$InnerScript')")
  exit 66
}
if (-not ($RepoRoot -match '^/mnt/')) {
  [Console]::Error.WriteLine("wsl_test.ps1: RepoRoot must be an absolute /mnt WSL path (got '$RepoRoot')")
  exit 65
}

# Existence check on the Windows side (before calling wsl.exe) so a fake
# RepoRoot or missing inner script returns rc=69, not a wsl.exe internal error.
$winRoot = $RepoRoot -replace '^/mnt/([a-zA-Z])/', '$1:\' -replace '/', '\'
if (-not (Test-Path $winRoot -PathType Container)) {
  [Console]::Error.WriteLine("wsl_test.ps1: RepoRoot '$RepoRoot' is not a directory")
  exit 69
}
$winInner = Join-Path $winRoot ($InnerScript -replace '/', '\')
if (-not (Test-Path $winInner -PathType Leaf)) {
  [Console]::Error.WriteLine("wsl_test.ps1: inner script '$RepoRoot/$InnerScript' does not exist")
  exit 69
}

# Launch via a script FILE (not bash -c) because wsl.exe drops positional args
# after `bash -c '...'` on Windows. mp_launch.sh cd's into the repo + verifies
# paths exist + execs the inner script with remaining args.
$launcher = "$RepoRoot/tools/test-env/mp_launch.sh"

$wslArgs = @('-d', 'MergePilot-Test', '-u', 'root', '--', 'bash', $launcher, $RepoRoot, $InnerScript)
if ($InnerArgs) { $wslArgs += $InnerArgs }

& wsl.exe @wslArgs
exit $LASTEXITCODE
