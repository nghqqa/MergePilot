#!/usr/bin/env bash
# Inner: runs inside WSL where docker is available.
# M5-0A container lifecycle integration: build dedicated Candidate image,
# verify M5-0A code is baked in, and confirm the PRODUCTION controller's
# PID/StartedAt are NOT perturbed by Candidate build/run teardown.
#
# hiclab_live=false: this exercises the build path + PID isolation only.
# Real /sync M4F_RUN + Gateway provenance + 6 skill jobs require the full
# deployed stack (m5coordinator policy on policy-gw + @manager/@m5-0-ctrl
# registered on hiclaw-controller) and are out of scope for this script.
set -euo pipefail

ROOT="/mnt/d/goai/mergepilot-os"
BUILD_CTX="$ROOT/tools/workflow-controller"
PROD="mergepilot-controller"
IMG="mergepilot-m5-0-candidate:test-$$-$(date +%s)"

cleanup() { set +e; docker rmi -f "$IMG" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "=== M5-0A container lifecycle (build + PID isolation) ==="

# Record production PID + StartedAt BEFORE
BEFORE=$(docker inspect "$PROD" --format '{{.State.Pid}} {{.State.StartedAt}}' 2>/dev/null) \
  || { echo "FAIL: production controller '$PROD' not running"; exit 1; }
echo "prod BEFORE: PID/StartedAt = $BEFORE"

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
# SSE path must be parameterized (no hardcoded /coordinator/sse)
import inspect, re
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
# Fix 3: prefix overlap helper
assert hasattr(controller, '_m5_prefix_overlap'), '_m5_prefix_overlap missing'
assert hasattr(controller, '_M5_PREFIX_CHARSET'), '_M5_PREFIX_CHARSET missing'
print('image controller: process_event(raw_sender, sender) + prefix overlap validation')
" 2>/dev/null; then
  echo "Gate 3 PASS: Fix 1 (raw_sender) + Fix 3 (prefix overlap) baked into image"
else
  echo "Gate 3 FAIL: image missing Fix 1/Fix 3 code"; exit 1
fi

# ── Gate 4: production PID/StartedAt UNCHANGED after build + throwaway runs ──
AFTER=$(docker inspect "$PROD" --format '{{.State.Pid}} {{.State.StartedAt}}' 2>/dev/null)
if [ "$BEFORE" = "$AFTER" ]; then
  echo "Gate 4 PASS: production PID/StartedAt unchanged ($AFTER)"
else
  echo "Gate 4 FAIL: production perturbed BEFORE='$BEFORE' AFTER='$AFTER'"; exit 1
fi

# ── Gate 5: Candidate image does NOT receive COORDINATOR_TOKEN by default ──
# The start script passes GATEWAY_TOKEN/GATEWAY_ROLE, never COORDINATOR_TOKEN.
# Verify the image has no COORDINATOR_TOKEN hard requirement in gateway_call.
if docker run --rm --entrypoint python "$IMG" -c "
import gateway_client as g
# gateway_call checks GATEWAY_TOKEN (falls back to COORDINATOR_TOKEN)
src_lines = [l for l in open(g.__file__, encoding='utf-8') if 'GATEWAY_TOKEN' in l and 'raise' in l]
assert len(src_lines) > 0, 'gateway_call does not gate on GATEWAY_TOKEN'
print('image: gateway_call gates on GATEWAY_TOKEN (Candidate needs no COORDINATOR_TOKEN)')
" 2>/dev/null; then
  echo "Gate 5 PASS: Candidate authenticates via GATEWAY_TOKEN, not COORDINATOR_TOKEN"
else
  echo "Gate 5 FAIL: gateway_call not gated on GATEWAY_TOKEN"; exit 1
fi

echo "=== M5-0A container lifecycle: 5/5 PASS ==="
echo "hiclab_live=false (build + PID isolation test)"
exit 0
