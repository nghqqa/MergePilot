# start-mergepilot.ps1 - MergePilot one-click start (thin wrapper).
# Logic lives in start-mergepilot.mjs (deps-injectable, unit-tested in
# test/start-mergepilot.test.mjs). This file only forwards params and checks
# that node is available - zero business logic here to avoid dual-implementation drift.
#
# Usage:
#   .\start-mergepilot.ps1 -Environment production [-IsoPreview] [-DryRun]
#   .\start-mergepilot.ps1 -Environment trial
param(
  [ValidateSet('production', 'trial')]
  [string]$Environment = 'production',
  [switch]$IsoPreview,
  [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$entry = Join-Path $here 'start-mergepilot.mjs'

node --version *> $null
if ($LASTEXITCODE -ne 0) {
  Write-Host 'FAILED at: node not available (entry logic runs on Node 20+)' -ForegroundColor Red
  exit 1
}

$forward = @($entry, '--environment', $Environment)
if ($IsoPreview) { $forward += '--iso-preview' }
if ($DryRun) { $forward += '--dry-run' }
& node @forward
exit $LASTEXITCODE
