#!/usr/bin/env bash
# deploy/rag-prod-staging/scripts/canary-preflight.sh — promote3 切换前自动化检查+回滚演练。
# 用法：bash canary-preflight.sh <candidate_image_tag> <compose_dir> [--rollback-drill]
# 所有检查通过 → exit 0；任一失败 → exit 1（fail-closed，不跳过）。
set -euo pipefail

CANDIDATE="${1:?需提供候选镜像 tag}"
COMPOSE_DIR="${2:?需提供 compose 目录}"
DRILL="${3:-}"

RED='\033[0;31m'; GREEN='\033[0;32m'; NC='\033[0m'
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo -e "  ${GREEN}PASS${NC} $1"; }
fail() { FAIL=$((FAIL+1)); echo -e "  ${RED}FAIL${NC} $1 — $2"; exit 1; }

echo "═══ Canary Preflight: $CANDIDATE ═══"

# ── 1. 镜像存在性+digest ──
DIGEST=$(docker image inspect "$CANDIDATE" --format '{{.Id}}' 2>/dev/null) || fail "镜像不存在" "$CANDIDATE"
ok "镜像存在: ${DIGEST:0:20}…"

# ── 2. Trivy HIGH/CRITICAL = 0 ──
set +e
trivy image --db-repository ghcr.io/aquasecurity/trivy-db \
  --exit-code 1 --severity HIGH,CRITICAL "$CANDIDATE" >/dev/null 2>&1
TRIVY_RC=$?
set -e
[ "$TRIVY_RC" = "0" ] || fail "Trivy HIGH/CRITICAL≠0" "exit_code=$TRIVY_RC"
ok "Trivy HIGH/CRITICAL = 0"

# ── 3. SBOM 可生成 ──
SBOM_TMP=$(mktemp /tmp/sbom-XXXXXX.json)
trivy image --db-repository ghcr.io/aquasecurity/trivy-db \
  --format cyclonedx --output "$SBOM_TMP" "$CANDIDATE" 2>/dev/null || true
SBOM_COUNT=$(python -c "import json,sys;print(len(json.load(open(sys.argv[1])).get('components',[])))" "$SBOM_TMP" 2>/dev/null || echo 0)
[ "$SBOM_COUNT" -gt 0 ] || fail "SBOM 为空" "components=$SBOM_COUNT file=$SBOM_TMP"
ok "SBOM: $SBOM_COUNT 组件"
rm -f "$SBOM_TMP"

# ── 4. compose 文件有效 ──
(cd "$COMPOSE_DIR" && docker compose config --quiet) 2>/dev/null || fail "compose 配置无效" "$COMPOSE_DIR"
ok "compose 配置有效"

# ── 5. 回滚锚点保留 ──
ANCHORS=$(grep -c "sha256:" "$COMPOSE_DIR/docker-compose.yml" 2>/dev/null || echo 0)
[ "$ANCHORS" -ge 1 ] || fail "无回滚锚点（sha256 digest 注释）" "$COMPOSE_DIR"
ok "回滚锚点: $ANCHORS 个 digest 引用"

# ── 6. C 链 enforcement 保持 off ──
ENFORCE=$(grep -o 'MERGEPILOT_CCHAIN_ENFORCE.*"0"' "$COMPOSE_DIR/docker-compose.yml" | head -1)
[ -n "$ENFORCE" ] || fail "C 链 enforcement 非 off" "$COMPOSE_DIR"
ok "C 链 enforcement = off"

# ── 7. 模型 manifest 文件存在 ──
for mf in "$COMPOSE_DIR"/../rag-zh-eval/bge-m3.manifest.json; do
  [ -f "$mf" ] || fail "manifest 不存在" "$mf"
done
ok "bge-m3 manifest 存在"

# ── 8. secret-scan ──
SCAN=$(cd "$COMPOSE_DIR/../.." && bash scripts/secret-scan.sh --path . 2>&1 | tail -1)
echo "$SCAN" | grep -q "PASS" || fail "secret-scan 失败" "$SCAN"
ok "secret-scan: 0 命中"

# ── 9. 依赖就绪状态（BLOCKED 即 BLOCKED——不伪装）──
echo "  ── 依赖状态（信息性——BLOCKED 属预期直至组织 P0 补齐）──"
echo "  keystore: BLOCKED (待 secret manager)"
echo "  attestation: NOT_CONFIGURED (待外部服务)"
echo "  → preflight 不因此失败——但 promote3 切换前置条件不满足"

# ── 10. 回滚演练（可选）──
if [ "$DRILL" = "--rollback-drill" ]; then
  echo "  ── 回滚演练 ──"
  echo "  (仅验证 compose 中锚点 digest 可被 Docker 解析)"
  for digest in $(grep -o 'sha256:[a-f0-9]\{64\}' "$COMPOSE_DIR/docker-compose.yml" | sort -u); do
    docker image inspect "$digest" >/dev/null 2>&1 && ok "回滚锚点可解析: ${digest:0:20}…" || echo "  INFO: 远端锚点 ${digest:0:20}…（本地无缓存——回滚时拉取）"
  done
fi

echo ""
echo "═══ Preflight: $PASS pass / $FAIL fail ═══"
[ "$FAIL" = "0" ] && echo "CANARY_PREFLIGHT_PASS" || exit 1
