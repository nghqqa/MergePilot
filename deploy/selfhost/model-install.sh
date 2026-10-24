#!/bin/sh
# model-install.sh — 把 bge-m3 模型目录装入 model-cache 卷并逐文件校验（首次安装预配置步骤）。
#
# 用法：
#   MODEL_SRC=/path/to/bge-m3 ./model-install.sh [卷名]     # 卷名缺省 mergepilot_model-cache
#
# 前提：模型目录含官方安装管线生成的 manifest.json（files[] 内每文件有 sha256/bytes）。
# 行为：
#   1) 宿主侧用 node 按 manifest 生成校验清单（name<hash> 对）；
#   2) docker volume create（幂等）+ alpine 辅助容器复制 模型目录+清单 进卷；
#   3) 卷内 sha256sum -c 全量校验——任一不符即退出 1（fail-closed）。
# 模型获取：见 README「模型与配置说明」——官方 modelscope/HF bge-m3，经 console 安装
# 管线或受信渠道取得；本脚本只负责「入卷+完整性证明」，不做下载。
set -eu

SRC="${MODEL_SRC:?用法: MODEL_SRC=/path/to/bge-m3 ./model-install.sh [卷名]}"
VOL="${1:-mergepilot_model-cache}"
MANIFEST="$SRC/manifest.json"

[ -f "$MANIFEST" ] || { echo "FAILED: 缺少 $MANIFEST（模型须含官方安装管线生成的 manifest.json）" >&2; exit 1; }
command -v docker >/dev/null || { echo "FAILED: docker 不可用" >&2; exit 1; }
command -v node >/dev/null || { echo "FAILED: node >= 20 不可用（preflight 同款前提）" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# 1) manifest → sha256sum 清单（name 两列；含子目录路径）
node - "$MANIFEST" "$TMP/checksums.txt" <<'NODE'
const fs = require('fs');
const [manifest, out] = process.argv.slice(2);
const m = JSON.parse(fs.readFileSync(manifest, 'utf8'));
const lines = (m.files || []).map((f) => `${f.sha256}  ${f.name}`);
if (!lines.length) { console.error('FAILED: manifest.files 为空'); process.exit(1); }
fs.writeFileSync(out, lines.join('\n') + '\n');
NODE

echo "卷: $VOL；源: $SRC（$(( $(node -e "const m=require(process.argv[1]);console.log(m.files.length)" "$MANIFEST") )) 个文件）"

# 2) 复制进卷（模型目录 + 清单；卷内 /models 即 BGE_MODEL_DIR）
docker volume create "$VOL" >/dev/null
docker run --rm -v "$VOL:/models" -v "$(cd "$SRC" && pwd)":/src:ro -v "$TMP":/chk:ro alpine:3 \
  sh -c 'cp -a /src/. /models/ && cp /chk/checksums.txt /models/.install-checksums.txt && cd /models &&     if sha256sum -c .install-checksums.txt >/tmp/chk.log; then echo "verified=$(grep -c ": OK" /tmp/chk.log) files"; else grep FAILED /tmp/chk.log; exit 1; fi'

echo "OK: 模型已入卷并全量校验通过（$VOL）"
echo "回填 .env：RAGTRIAL_EMBED_EXPECTED_MANIFEST=$(node -e "const m=require(process.argv[1]);console.log((m.model_id||'')+'@'+(m.source||'').slice(0,40))" "$MANIFEST")  （或按 README 口径填清单校验值）"
