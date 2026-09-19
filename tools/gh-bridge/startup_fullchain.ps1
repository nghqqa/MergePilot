# startup_fullchain.ps1 — MergePilot 全链恢复/健康检查(电脑重启后跑这个)
# 用法: powershell -ExecutionPolicy Bypass -File D:\goai\r3work\scripts\startup_fullchain.ps1
#       加 -StartBridge 参数时顺带启动 gh_bridge(跑案例需要)
param([switch]$StartBridge)

$fail = 0
function Step($name, $ok, $detail) {
    if (-not $ok) { $script:fail++ }
    $mark = "[FAIL]"; if ($ok) { $mark = "[OK]  " }
    Write-Host "$mark $name  $detail"
}

Write-Host "=== MergePilot full-chain check ===" -ForegroundColor Cyan

# 1. local stack containers (restart=unless-stopped; start if needed)
$need = @('elemiso-ctrl','elemiso-proxy','elemiso-case-pg','elemiso-element-web')
foreach ($c in $need) {
    $st = docker inspect $c --format '{{.State.Status}}' 2>$null
    if ($st -ne 'running') { docker start $c | Out-Null; Start-Sleep 3; $st = docker inspect $c --format '{{.State.Status}}' 2>$null }
    Step $c ($st -eq 'running') $st
}

# 2. rag-live :4184 (host process, the only non-auto-start component)
$rag = @(Get-CimInstance Win32_Process -Filter "Name='node.exe'" | Where-Object { $_.CommandLine -like '*rag-live*' })
if (-not $rag) {
    Start-Process node -ArgumentList "D:\goai\r3work\rag-live\rag-live-server.mjs" -WindowStyle Hidden
    Start-Sleep 4
    $rag = @(Get-CimInstance Win32_Process -Filter "Name='node.exe'" | Where-Object { $_.CommandLine -like '*rag-live*' })
}
$ragOk = $false
try { $ragOk = (Invoke-RestMethod http://127.0.0.1:4184/health -TimeoutSec 5).ok } catch {}
$ragDetail = "health fail"
if ($ragOk) { $ragDetail = "ready" }
Step "rag-live :4184" ($rag.Count -gt 0 -and $ragOk) $ragDetail

# 3. workers (v4boot self-activating image; wake recreates if missing)
foreach ($w in @('leader','reviewer','fixer','verifier')) {
    $st = docker inspect "elemiso-worker-$w" --format '{{.State.Status}}' 2>$null
    if ($st -ne 'running') {
        docker exec elemiso-ctrl agt worker wake --name $w 2>$null | Out-Null
        Start-Sleep 8
        $st = docker inspect "elemiso-worker-$w" --format '{{.State.Status}}' 2>$null
    }
    Step "worker $w" ($st -eq 'running') $st
}

# 4. activation self-check (v4boot bootstrap pulls from MinIO if not yet enabled)
Start-Sleep 10
foreach ($w in @('leader','reviewer')) {
    $cfgCnt = 0
    foreach ($n in @('otel','skills','rag')) {
        $t = docker exec "elemiso-worker-$w" cat "/etc/agentloop-$n.json" 2>$null | Select-String -SimpleMatch '"enabled": true'
        if ($t) { $cfgCnt++ }
    }
    $boot = docker exec "elemiso-worker-$w" sh -c "tail -1 /tmp/agentloop-bootstrap.log 2>/dev/null || echo fresh"
    Step "$w activation x3" ($cfgCnt -ge 3) $boot
}

# 5. server ingress + reporter
$wh = $false
try { $wh = (Invoke-RestMethod https://mergepilot.nghqqa.cn/healthz -TimeoutSec 10).ok } catch {}
Step "webhook ingress(server)" $wh "mergepilot.nghqqa.cn/healthz"
$rep = ssh -o BatchMode=yes -o ConnectTimeout=10 root@159.75.42.106 "docker inspect mp-checks-reporter --format '{{.State.Status}}'" 2>$null
$repOk = ($rep -match 'running')
Step "check-run reporter(server)" $repOk $rep

# 6. case DB
$cases = docker exec elemiso-case-pg sh -c "psql -U `$POSTGRES_USER -d cases -At -c 'SELECT count(*) FROM knowledge;'" 2>$null
$casesOk = ($cases -match '^\d+$') -and ([int]$cases -gt 0)
Step "pgvector case DB" $casesOk "$cases rows"

# 7. bridge (needed only when running cases)
$bridge = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*gh_bridge*' })
if (-not $bridge -and $StartBridge) {
    Start-Process python -ArgumentList 'D:\goai\r3work\scripts\gh_bridge.py','run','--timeout-min','90' -WorkingDirectory 'D:\goai\r3work\scripts' -WindowStyle Hidden
    Start-Sleep 5
    $bridge = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*gh_bridge*' })
}
$bridgeDetail = "not started (use -StartBridge)"
if ($bridge) { $bridgeDetail = "running" }
Step "gh-bridge" ($bridge.Count -gt 0 -or -not $StartBridge) $bridgeDetail

Write-Host ""
if ($fail -eq 0) {
    Write-Host "=== ALL GREEN: push any commit to a PR branch to trigger the full chain ===" -ForegroundColor Green
} else {
    Write-Host "=== $fail item(s) not ready; if reviewer ignores tasks: docker restart elemiso-worker-reviewer ===" -ForegroundColor Yellow
}
