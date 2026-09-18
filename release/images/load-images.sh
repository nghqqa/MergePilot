#!/usr/bin/env bash
# MergePilot offline image bundle LOADER (WSL2 / Linux).
#
# Verifies the bundle (manifest schema, SHA256SUMS, digests) and loads ALL
# images into the local Docker engine. Requires: docker, zstd, python3.
#
# Usage:
#   ./load-images.sh [DIR]      # DIR contains the .tar.zst + manifest + SHA256SUMS
#
# Expects the release layout:
#   MergePilot-images-linux-amd64.tar.zst
#   MergePilot-images-manifest.json
#   SHA256SUMS

set -uo pipefail

DIR="${1:-.}"
ARCHIVE="$DIR/MergePilot-images-linux-amd64.tar.zst"
MANIFEST="$DIR/MergePilot-images-manifest.json"

fail() { echo "ERROR: $*" >&2; exit 1; }
diag() { echo; echo "── 诊断: $*" >&2; }

# ---- diagnostics ----------------------------------------------------------------
[[ -f "$ARCHIVE" ]] || fail "找不到 $ARCHIVE（离线镜像包主文件）"
[[ -f "$MANIFEST" ]] || fail "找不到 $MANIFEST（镜像清单）"
command -v docker >/dev/null 2>&1 || { diag "未安装 docker CLI"; fail "docker CLI 未找到"; }
docker version --format '{{.Server.Os}}/{{.Server.Arch}}' >/dev/null 2>&1 || {
  diag "Docker 引擎不可达。在 Windows 上：启动 Docker Desktop，等待鲸鱼图标就绪；"
  diag "若 Docker Desktop 报 'WSL2 engine' 错误：以管理员运行 'wsl --update' 并在"
  diag "PowerShell 执行 'wsl --set-default-version 2'，然后重启 Docker Desktop。"
  fail "Docker 引擎不可达"
}
command -v zstd >/dev/null 2>&1 || {
  diag "zstd 未安装。Ubuntu/WSL: sudo apt-get install -y zstd"
  fail "zstd 未找到"
}

SERVER_PLATFORM="$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}')"
[[ "$SERVER_PLATFORM" == "linux/amd64" ]] || {
  diag "引擎架构为 $SERVER_PLATFORM；本包为 linux/amd64。在 ARM/Mac 上无法加载本包。"
  fail "架构不匹配"
}

# ---- 清单与哈希预校验 ------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$SCRIPT_DIR/bundle_verify.py" "$DIR" || fail "包校验未通过（见上方 PROBLEM）"

# ---- 解压并加载 -------------------------------------------------------------------
echo ">> 解压 $ARCHIVE"
zstd -dc "$ARCHIVE" | docker load
[[ ${PIPESTATUS[0]} -eq 0 && ${PIPESTATUS[1]} -eq 0 ]] || fail "解压或 docker load 失败"

# ---- 逐镜像校验数量 / tag / digest ------------------------------------------------
echo ">> 校验已加载镜像（数量 / tag / digest）"
python3 - "$MANIFEST" <<'PYEOF'
import json, subprocess, sys

m = json.load(open(sys.argv[1], encoding="utf-8"))
entries = m["images"]
problems = []
for e in entries:
    ref, want = e["ref"], e["digest"]
    r = subprocess.run(["docker", "inspect", "--format", "{{.Id}}", ref],
                       capture_output=True, text=True)
    if r.returncode != 0:
        problems.append(f"镜像缺失: {ref}")
        continue
    got = r.stdout.strip()
    if got != want:
        problems.append(f"digest 不一致: {ref} 期望 {want[:20]}… 实际 {got[:20]}…")
    arch = subprocess.run(["docker", "inspect", "--format",
                           "{{.Os}}/{{.Architecture}}", ref],
                          capture_output=True, text=True).stdout.strip()
    if arch != e["architecture"]:
        problems.append(f"架构不匹配: {ref} 期望 {e['architecture']} 实际 {arch}")
if problems:
    print("\n".join("PROBLEM: " + p for p in problems))
    sys.exit(1)
print(f"VERIFY_OK: {len(entries)}/{len(entries)} 镜像与清单 digest 完全一致")
PYEOF
VERIFY=$?
[[ $VERIFY -eq 0 ]] || fail "镜像校验失败（digest/架构不一致——请重新下载离线包）"

echo
echo "全部就绪。下一步：mergepilot doctor && mergepilot --github-e2e start"
echo "（或用 docker compose up 启动隔离栈；参见 DEPLOY.md）"
