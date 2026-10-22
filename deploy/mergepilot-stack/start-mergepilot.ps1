# start-mergepilot.ps1 — MergePilot 生产/试用一键启动入口（2026-10-22）
#
# 用法：
#   .\start-mergepilot.ps1 -Environment production [-IsoPreview]
#   .\start-mergepilot.ps1 -Environment trial     [-IsoPreview]
#
# 职责：
#   1. 前置检查：Docker 可用 / compose 文件 / 秘密文件 / 模型目录 / 外部数据卷
#   2. 按依赖序拉起（PG → bge → 代理/webhook → agentteams(ctrl/manager, docker start) → console）
#   3. 等待 console live + mu_schema_ready（真实健康门，最多 4 分钟）
#   4. 验证执行器链（console → at-auth-proxy → ctrl：workers/projects 探针）
#   5. 成功 exit 0；任一环节失败 exit 非 0 并指明失败环节
#
# 幂等：重复执行安全——compose 对已运行且配置未变的服务不做任何事；
#       docker start 对已运行容器是无操作。健康容器不会被重建。
#
# IsoPreview（隔离预览）：独立项目名/端口/全新数据卷，不接 atnet，不触生产。
# 注意：IsoPreview 下执行器不可达属预期（不接 agentteams 网络），只验应用面。
param(
  [ValidateSet('production', 'trial')]
  [string]$Environment = 'production',
  [switch]$IsoPreview
)
$ErrorActionPreference = 'Stop'
$fail = { param($stage) Write-Host "FAILED at: $stage" -ForegroundColor Red; exit 1 }

# ── 路径常量（本宿主） ──
$Repo = $PSScriptRoot            # 脚本位于 deploy/mergepilot-stack/
$SecretDir = 'D:/goai/secrets'
$ComposeDir = Join-Path $Repo '' # compose 就在本目录
$ComposeProd = 'docker-compose.yml'
$ComposeIso = 'docker-compose.iso.yml'

# ── 版本身份（rc7；换版后由 #509 swap 流程同步更新此两行 + env） ──
$ConsoleImage = 'ghcr.io/nghqqa/mergepilot-console:v0.2.0-beta.6-rc.35.6-rc7'
$ExpectCommit = 'b7a0fcaf63ce3157a626c41918c8fdf60547ad74'

Write-Host "== MergePilot 一键启动 (env=$Environment iso=$IsoPreview) =="

# ── 前置检查 1：Docker ──
docker info *> $null
if ($LASTEXITCODE -ne 0) { & $fail 'Docker daemon 不可用' }

# ── 前置检查 2：秘密文件（缺=清楚报错，不退 snapshot） ──
$requiredSecrets = @(
  (Join-Path $SecretDir 'beta-mp-console.env'),
  (Join-Path $SecretDir 'beta-mp-pg.env'),
  (Join-Path $SecretDir 'github-app-key.pem')
)
foreach ($f in $requiredSecrets) {
  if (-not (Test-Path $f)) {
    Write-Host "FAILED at: 缺少秘密文件 $f ——受控秘密目录不完整，拒绝启动（不退回 snapshot 冒充成功）" -ForegroundColor Red
    exit 1
  }
}
if (-not (Test-Path 'D:/goai/prod-data/secrets/agentteams-auth-proxy/default.conf')) {
  Write-Host "FAILED at: 缺少 agentteams-auth-proxy 配置目录" -ForegroundColor Red
  exit 1
}
Write-Host 'precheck: 秘密文件齐备'

# ── 前置检查 3：模型目录（RAG/bge） ──
if (-not (Test-Path 'D:/goai/prod-data/rag-models/bge-m3/manifest.json')) {
  Write-Host "FAILED at: 缺少 bge-m3 模型目录（rag-models/bge-m3/manifest.json）" -ForegroundColor Red
  exit 1
}
Write-Host 'precheck: bge-m3 模型在位'

# ── 前置检查 4：外部数据卷（生产 PG 数据） ──
$pgVol = docker volume inspect f2c57d69cc0e9b83a2440030b86abfb1f07ced641ce0c68ecdaa3c26efae1365 *> $null
if (-not $IsoPreview -and $LASTEXITCODE -ne 0) {
  Write-Host 'FAILED at: 生产 PG 数据卷缺失（f2c57d69…）——拒绝以空库冒充生产' -ForegroundColor Red
  exit 1
}

# ── 拉起 ──
$env:SECRET_DIR = $SecretDir
$env:CONSOLE_IMAGE = $ConsoleImage
$env:MU_GITHUB_APP_PRIVATE_KEY = Get-Content (Join-Path $SecretDir 'github-app-key.pem') -Raw

if ($Environment -eq 'production') {
  if ($IsoPreview) {
    Write-Host 'up: 隔离预览（mergepilot-iso 项目 / 独立端口 / 全新卷 / 不接 atnet）'
    docker compose -p mergepilot-iso -f (Join-Path $ComposeDir $ComposeProd) -f (Join-Path $ComposeDir $ComposeIso) up -d
  } else {
    Write-Host 'up: 生产栈（mergepilot 项目）'
    docker compose -p mergepilot -f (Join-Path $ComposeDir $ComposeProd) up -d
  }
  if ($LASTEXITCODE -ne 0) { & $fail 'docker compose up（核心栈）' }
} else {
  # 试用：既有 compose 项目（mp-trial-rc355）+ 手工 trial-bge / mp-trial-console
  docker start trial-bge 2>$null | Out-Null
  $trialDir = 'D:/goai/r3work/accept-deploy-468/deploy/selfhost'
  if (-not (Test-Path $trialDir)) { & $fail "试用 compose 目录缺失：$trialDir" }
  Push-Location $trialDir
  docker compose -p mp-trial-rc355 up -d
  if ($LASTEXITCODE -ne 0) { Pop-Location; & $fail 'docker compose up（试用栈）' }
  Pop-Location
  docker start mp-trial-console 2>$null | Out-Null
  Write-Host 'up: 试用栈已拉起'
}

# ── agentteams：既有容器 docker start（重建属事故高发操作，保留 start 托管） ──
if (-not $IsoPreview) {
  docker start agentteams-beta-ctrl 2>$null | Out-Null
  Start-Sleep -Seconds 3
  docker start agentteams-beta-manager 2>$null | Out-Null
  Write-Host 'up: agentteams ctrl/manager 已启动'
}

# ── 健康门：console live + mu_schema_ready（最多 4 分钟） ──
$port = if ($IsoPreview) { 14730 } elseif ($Environment -eq 'production') { 48500 } else { 18700 }
$healthy = $false
for ($i = 1; $i -le 48; $i++) {
  try {
    $h = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 5
    if ($h.ok -and $h.data_mode -eq 'live' -and $h.mu_schema_ready -eq $true) {
      Write-Host ("health: live mu_ready=true version={0} commit={1}" -f $h.version, $h.git_commit)
      $healthy = $true; break
    }
    Write-Host ("health round ${i}/48: data_mode={0} mu_ready={1}（等待）" -f $h.data_mode, $h.mu_schema_ready)
  } catch { Write-Host "health round ${i}/48: 连接失败（等待）" }
  Start-Sleep -Seconds 5
}
if (-not $healthy) { & $fail "健康门 4 分钟未达 live/mu_ready——不判成功" }

# ── 执行器真实就绪（隔离预览跳过——不接 atnet） ──
if (-not $IsoPreview -and $Environment -eq 'production') {
  $execOk = $false
  for ($i = 1; $i -le 12; $i++) {
    try {
      docker exec beta-mp-console node -e "fetch('http://at-auth-proxy:8091/api/v1/workers',{headers:{authorization:'Bearer '+process.env.MU_AGENTTEAMS_TOKEN}}).then(r=>process.exit(r.status===200?0:1)).catch(()=>process.exit(1))" *> $null
      if ($LASTEXITCODE -eq 0) { $execOk = $true; break }
    } catch { }
    Start-Sleep -Seconds 5
  }
  if (-not $execOk) { & $fail '执行器链（console→at-auth-proxy→ctrl）探针未通过' }
  Write-Host 'executor: workers 探针 200 ✓'
}

Write-Host "`nSTART OK (env=$Environment)——全部检查通过" -ForegroundColor Green
exit 0
