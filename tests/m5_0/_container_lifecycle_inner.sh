#!/usr/bin/env bash
# Inner: runs inside the MergePilot-Test WSL2 distro (isolated test daemon).
# M5-0A container lifecycle: build a dedicated Candidate image from the current
# working tree, verify M5-0A code is baked in, and confirm the dedicated test
# daemon does NOT expose any production container (daemon/VHDX isolation — the
# v2.6 replacement for the former same-daemon production-PID gate).
#
# hiclab_live=false: build path + daemon-isolation proof only.
set -euo pipefail

ROOT="/mnt/d/goai/mergepilot-os"
# MergePilot test-env isolation guard (fail-closed: MergePilot-Test daemon only).
source "${ROOT}/tools/test-env/mp_guard.sh"
BUILD_CTX="$ROOT/tools/workflow-controller"
IMG="mergepilot-m5-0-candidate:test-$$-$(date +%s)"

cleanup() { set +e; docker rmi -f "$IMG" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "=== M5-0A container lifecycle (build + daemon isolation) ==="

# ── Gate 1: build dedicated Candidate image from current source ──
echo "--- build $IMG (ctx=$BUILD_CTX) ---"
docker build -q -t "$IMG" "$BUILD_CTX" >/dev/null \
  || { echo "Gate 1 FAIL: image build failed"; exit 1; }
echo "Gate 1 PASS: dedicated Candidate image built from current working tree"

# ── Gate 2: image contains M5-0A code (gateway_client parameterization) ──
if docker run --rm --entrypoint python "$IMG" -c "
import gateway_client as g
assert hasattr(g, 'GATEWAY_ROLE'), 'GATEWAY_ROLE missing'
assert hasattr(g, 'GATEWAY_TOKEN'), 'GATEWAY_TOKEN missing'
assert g.GATEWAY_ROLE == 'coordinator', f'default role wrong: {g.GATEWAY_ROLE}'
import inspect
src = inspect.getsource(g._lifecycle)
assert '{GATEWAY_ROLE}' in src or 'GATEWAY_ROLE}' in src, 'SSE path not parameterized'
print('image gateway_client: GATEWAY_ROLE + GATEWAY_TOKEN present, SSE parameterized')
" 2>/dev/null; then
  echo "Gate 2 PASS: gateway_client v2.4 parameterization baked into image"
else
  echo "Gate 2 FAIL: image missing GATEWAY_ROLE/GATEWAY_TOKEN or SSE not parameterized"; exit 1
fi

# ── Gate 3: image contains Fix 1 (process_event raw_sender param) ──
if docker run --rm --entrypoint python "$IMG" -c "
import controller, inspect
sig = inspect.signature(controller.process_event)
params = list(sig.parameters.keys())
assert 'raw_sender' in params, f'raw_sender missing: {params}'
assert 'sender' in params, f'sender missing: {params}'
assert params.index('raw_sender') < params.index('sender'), 'raw_sender must precede sender'
assert hasattr(controller, '_m5_prefix_overlap'), '_m5_prefix_overlap missing'
assert hasattr(controller, '_M5_PREFIX_CHARSET'), '_M5_PREFIX_CHARSET missing'
print('image controller: process_event(raw_sender, sender) + prefix overlap validation')
" 2>/dev/null; then
  echo "Gate 3 PASS: Fix 1 (raw_sender) + Fix 3 (prefix overlap) baked into image"
else
  echo "Gate 3 FAIL: image missing Fix 1/Fix 3 code"; exit 1
fi

# ── Gate 4: daemon isolation — production controller NOT visible from test daemon ──
# Formerly a same-daemon production-PID gate; under the isolated MergePilot-Test
# daemon (v2.6), the proof is that NO production container is visible at all —
# the build/throwaway runs here cannot perturb production because they are on a
# different dockerd + vhdx.
PROD_VISIBLE=""
for _c in mergepilot-controller policy-gw audit-pg github-mcp hiclaw-manager hiclaw-controller; do
  if docker inspect "$_c" >/dev/null 2>&1; then PROD_VISIBLE="$PROD_VISIBLE $_c"; fi
done
if [ -z "$PROD_VISIBLE" ]; then
  echo "Gate 4 PASS: no production container visible from the isolated test daemon"
else
  echo "Gate 4 FAIL: production containers visible:$PROD_VISIBLE"; exit 1
fi

# ── Gate 5: Candidate image authenticates via GATEWAY_TOKEN, not COORDINATOR_TOKEN ──
if docker run --rm --entrypoint python "$IMG" -c "
import gateway_client as g
src_lines = [l for l in open(g.__file__, encoding='utf-8') if 'GATEWAY_TOKEN' in l and 'raise' in l]
assert len(src_lines) > 0, 'gateway_call does not gate on GATEWAY_TOKEN'
print('image: gateway_call gates on GATEWAY_TOKEN (Candidate needs no COORDINATOR_TOKEN)')
" 2>/dev/null; then
  echo "Gate 5 PASS: Candidate authenticates via GATEWAY_TOKEN, not COORDINATOR_TOKEN"
else
  echo "Gate 5 FAIL: gateway_call not gated on GATEWAY_TOKEN"; exit 1
fi

echo "=== M5-0A container lifecycle: 5/5 PASS ==="
echo "hiclab_live=false (build + daemon-isolation test)"
exit 0
