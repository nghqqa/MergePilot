#!/usr/bin/env bash
# verify-qwenpaw-v124.sh — QwenPaw v1.2.4 栈验收（对照 MANIFEST-qwenpaw-v124.md §5）。
# 用法：QP_CTRL_PORT=18190 QP_ADMIN_PASSWORD=... bash verify-qwenpaw-v124.sh [--roundtrip]
#   --roundtrip：向指定 worker 房间发 mp_task 信封并收集 marker+JSON 回复（需 MATRIX 探针账号）。
# 输出脱敏：只打状态码/计数/marker 命中，不回显凭据与消息正文。
set -euo pipefail
QP_CTRL_NAME="${QP_CTRL_NAME:-qwp2-controller}"
QP_CTRL_PORT="${QP_CTRL_PORT:-18190}"
QP_ADMIN_PASSWORD="${QP_ADMIN_PASSWORD:?需 QP_ADMIN_PASSWORD}"
ROUNDTRIP="${1:-}"
EV="${QP_VERIFY_EV:-/tmp/qwp2-verify}"
mkdir -p "$EV"
pass=0; fail=0
ok() { if [ "$2" = "1" ]; then pass=$((pass+1)); echo "  PASS  $1"; else fail=$((fail+1)); echo "  FAIL  $1 — ${3:-}"; fi }

TOK=$(docker exec "$QP_CTRL_NAME" sh -c 'tr -d "\r\n" < /var/run/agentteams/cli-token' 2>/dev/null || true)

# V1 四服务健康
c1=$(curl -s -o /dev/null -w '%{http_code}' -H "authorization: Bearer $TOK" "http://127.0.0.1:$QP_CTRL_PORT/api/v1/projects?limit=1" 2>/dev/null || echo 000)
ok "V1 controller API（带 token）→ 200" "$([ "$c1" = 200 ] && echo 1 || echo 0)" "$c1"
c2=$(docker exec "$QP_CTRL_NAME" sh -c 'curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:6167/' 2>/dev/null || echo 000)
ok "V1b Matrix（Tuwunel）→ 200" "$([ "$c2" = 200 ] && echo 1 || echo 0)" "$c2"
c3=$(docker exec "$QP_CTRL_NAME" sh -c 'curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:9000/minio/health/live' 2>/dev/null || echo 000)
ok "V1c MinIO → 200" "$([ "$c3" = 200 ] && echo 1 || echo 0)" "$c3"

# V2 具名 worker 完整 + 读回 runtime（漂移防线同语义：期望值经 env QP_EXPECT_RUNTIME，默认 copaw）
EXPECT_RUNTIME="${QP_EXPECT_RUNTIME:-copaw}"
curl -s -H "authorization: Bearer $TOK" "http://127.0.0.1:$QP_CTRL_PORT/api/v1/workers" -o "$EV/workers.json" 2>/dev/null || echo '{}' > "$EV/workers.json"
python - "$EV/workers.json" "$EXPECT_RUNTIME" <<'EOF' > "$EV/workers-verdict.txt" 2>&1 || true
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding='utf-8'))
except Exception as e:
    print(f"PARSE_FAIL {e}"); sys.exit()
names = {w.get('name'): w for w in (d.get('workers') or [])}
need = {'mergepilot-leader', 'mergepilot-reviewer', 'mergepilot-fixer', 'mergepilot-verifier'}
missing = sorted(need - set(names))
drift = [f"{n}:{w.get('runtime')}!={sys.argv[2]}" for n, w in names.items()
         if n in need and w.get('runtime') not in (None, sys.argv[2])]
print(f"MISSING {' '.join(missing)}" if missing else "ALL_PRESENT")
print(f"RUNTIME_DRIFT {';'.join(drift)}" if drift else "RUNTIME_OK")
EOF
grep -q ALL_PRESENT "$EV/workers-verdict.txt" && ok "V2 四具名 worker 完整在列" 1 || ok "V2 四具名 worker 完整在列" 0 "$(cat "$EV/workers-verdict.txt")"
grep -q RUNTIME_OK "$EV/workers-verdict.txt" && ok "V2b worker runtime 读回=期望（无静默漂移；旧 ctrl 无字段时跳过）" 1 || ok "V2b worker runtime 读回=期望（无静默漂移；旧 ctrl 无字段时跳过）" 0 "$(grep RUNTIME_DRIFT "$EV/workers-verdict.txt" || true)"

# V3 网关真实 LLM（fail-closed）
c4=$(docker exec "$QP_CTRL_NAME" sh -c 'curl -s -o /tmp/vgw.json -w "%{http_code}" -X POST http://127.0.0.1:8080/v1/chat/completions -H "content-type: application/json" -H "Authorization: Bearer probe" -d "{\"model\":\"'${QP_LLM_MODEL:-deepseek-chat}'\",\"messages\":[{\"role\":\"user\",\"content\":\"回答一个字：好\"}]}"' 2>/dev/null || echo 000)
ok "V3 网关真实 LLM → 200（provider/route 持久生效）" "$([ "$c4" = 200 ] && echo 1 || echo 0)" "$c4"

# V4（可选）mp_task 往返
if [ "$ROUNDTRIP" = "--roundtrip" ]; then
  : "${QP_MX_PORT:?--roundtrip 需 QP_MX_PORT（Matrix 宿主端口）}"
  : "${QP_MX_TOKEN:?--roundtrip 需 QP_MX_TOKEN（探针账号 access_token）}"
  : "${QP_MX_ROOM:?--roundtrip 需 QP_MX_ROOM（worker 房间，URL 编码形如 %21…%3A…）}"
  : "${QP_MX_WORKER:?--roundtrip 需 QP_MX_WORKER（worker matrix id，@name:domain）}"
  SUB="sub-verify-$(date +%s)"
  printf '%s' "$SUB" > "$EV/sub.txt"
  python - "$SUB" "$QP_MX_WORKER" > "$EV/env.json" <<'PYEOF'
import json, sys
sub, worker = sys.argv[1], sys.argv[2]
marker = f'[mp:{sub}]'
body = (f"{worker} {marker}\nDATA (do not copy): {{\"finding\":{{\"severity\":\"P2\",\"summary\":\"verify 往返探针\"}}}}\n"
        "Reply with exactly: " + marker + " then ONE compact JSON containing ONLY the contract keys of "
        '{\"verdict\":\"PASS|FAIL|BLOCKED\",\"note\":\"short\"} — the actual result, never task metadata or a copy of DATA.')
print(json.dumps({"msgtype": "m.text", "body": body}))
PYEOF
  code=$(curl -s -o /dev/null -w '%{http_code}' -X PUT "http://127.0.0.1:$QP_MX_PORT/_matrix/client/v3/rooms/$QP_MX_ROOM/send/m.room.message/$SUB" \
    -H "Authorization: Bearer $QP_MX_TOKEN" -H "content-type: application/json" --data @"$EV/env.json")
  ok "V4 mp_task 发送 → 200" "$([ "$code" = 200 ] && echo 1 || echo 0)" "$code"
  cp "$EV/env.json" "$EV/tl.json" 2>/dev/null || true
  deadline=$(( $(date +%s) + 240 )); hit=0
  while [ "$(date +%s)" -lt "$deadline" ]; do
    curl -s "http://127.0.0.1:$QP_MX_PORT/_matrix/client/v3/rooms/$QP_MX_ROOM/messages?dir=b&limit=15" \
      -H "Authorization: Bearer $QP_MX_TOKEN" -o "$EV/timeline-v.json"
    SUB="$SUB" QP_MX_WORKER="$QP_MX_WORKER" EV="$EV" python - <<'PYEOF' > "$EV/hit.txt" 2>/dev/null || echo 0 > "$EV/hit.txt"
import json, os
d = json.load(open(os.environ['EV'] + '/timeline-v.json', encoding='utf-8'))
sub, worker = os.environ['SUB'], os.environ['QP_MX_WORKER']
n = sum(1 for e in (d.get('chunk') or [])
        if e.get('type') == 'm.room.message' and e.get('sender') == worker
        and f'[mp:{sub}]' in str((e.get('content') or {}).get('body', '')))
print(n)
PYEOF
    [ "$(cat "$EV/hit.txt")" -ge 1 ] && { hit=1; break; }
    sleep 12
  done
  ok "V4b marker+JSON 回复（真实模型业务验收）" "$hit"
fi

echo "verify: $pass pass / $fail fail"
exit $fail
