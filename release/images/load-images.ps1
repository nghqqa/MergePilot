# MergePilot offline image bundle LOADER (Windows 11 + Docker Desktop).
#
# Run in PowerShell, from the folder that contains:
#   MergePilot-images-linux-amd64.tar.zst
#   MergePilot-images-manifest.json
#   SHA256SUMS
#
# Uses the Windows 11 built-in tar.exe (bsdtar, zstd-aware) to unpack, then
# docker load + per-image digest verification. Stdout is UTF-8.

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Archive  = Join-Path (Get-Location) "MergePilot-images-linux-amd64.tar.zst"
$Manifest = Join-Path (Get-Location) "MergePilot-images-manifest.json"

function Fail([string]$Msg, [string]$Diag = "") {
    Write-Host "ERROR: $Msg" -ForegroundColor Red
    if ($Diag) { Write-Host "── 诊断: $Diag" -ForegroundColor Yellow }
    exit 1
}

# ---- diagnostics ------------------------------------------------------------------
if (-not (Test-Path $Archive))  { Fail "找不到 $Archive（离线镜像包主文件）" }
if (-not (Test-Path $Manifest)) { Fail "找不到 $Manifest（镜像清单）" }

$dockerOK = $true
try { docker version --format "{{.Server.Os}}/{{.Server.Arch}}" 2>$null | Out-Null } catch { $dockerOK = $false }
if (-not $dockerOK) {
    Fail "Docker 引擎不可达" @(
        "1) 启动 Docker Desktop，等待任务栏鲸鱼图标变为稳定状态；",
        "2) 若报 WSL2 相关错误：以管理员运行 'wsl --update'，再执行 'wsl --set-default-version 2'；",
        "3) 确认 Docker Desktop 设置中 'Use the WSL 2 based engine' 已勾选。" ) -join "`n"
}

$serverPlatform = (docker version --format "{{.Server.Os}}/{{.Server.Arch}}").Trim()
if ($serverPlatform -ne "linux/amd64") {
    Fail "引擎架构为 $serverPlatform；本包为 linux/amd64，架构不匹配。"
}

# ---- 解包（Windows 11 内置 tar.exe 原生支持 zstd） ---------------------------------
$tarExe = Join-Path $env:WINDIR "system32\tar.exe"
if (-not (Test-Path $tarExe)) { $tarExe = "tar" }
Write-Host ">> 解包 $Archive"
& $tarExe -xf $Archive
if ($LASTEXITCODE -ne 0) { Fail "解包失败（tar exit $LASTEXITCODE）" }

$innerTar = Join-Path (Get-Location) "MergePilot-images-linux-amd64.tar"
if (-not (Test-Path $innerTar)) {
    # bsdtar 解出的是同名 .tar（去 .zst）；两种命名都接受
    $alt = Join-Path (Get-Location) "MergePilot-images-linux-amd64.tar.zst.tar"
    if (Test-Path $alt) { $innerTar = $alt } else { Fail "解包后未找到 MergePilot-images-linux-amd64.tar" }
}

# ---- 加载 ---------------------------------------------------------------------------
Write-Host ">> docker load $innerTar"
docker load -i $innerTar
if ($LASTEXITCODE -ne 0) { Fail "docker load 失败" }

# ---- 逐镜像校验数量 / tag / digest / 架构 ---------------------------------------------
$manifest = Get-Content $Manifest -Encoding UTF8 -Raw | ConvertFrom-Json
$problems = @()
foreach ($img in $manifest.images) {
    $id = (docker inspect --format "{{.Id}}" $img.ref 2>$null)
    if (-not $id -or $LASTEXITCODE -ne 0) {
        $problems += "镜像缺失: $($img.ref)"
        continue
    }
    $id = $id.Trim()
    if ($id -ne $img.digest) {
        $problems += "digest 不一致: $($img.ref) 期望 $($img.digest.Substring(0,20))… 实际 $($id.Substring(0, [Math]::Min(20, $id.Length)))…"
    }
    $arch = (docker inspect --format "{{.Os}}/{{.Architecture}}" $img.ref 2>$null).Trim()
    if ($arch -ne $img.architecture) {
        $problems += "架构不匹配: $($img.ref) 期望 $($img.architecture) 实际 $arch"
    }
}
if ($problems.Count -gt 0) {
    $problems | ForEach-Object { Write-Host "PROBLEM: $_" -ForegroundColor Red }
    Fail "镜像校验失败（数量/tag/digest/架构）。请重新下载离线包并核对 SHA256。"
}
Write-Host ("VERIFY_OK: {0}/{0} 镜像与清单 digest 完全一致" -f $manifest.images.Count) -ForegroundColor Green
Write-Host ""
Write-Host "全部就绪。下一步：mergepilot doctor && mergepilot --github-e2e start"
Write-Host "（或用 docker compose up 启动隔离栈；参见 DEPLOY.md）"
