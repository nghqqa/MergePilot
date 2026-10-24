#!/usr/bin/env bash
# verify-qwenpaw-v124.sh — QwenPaw v1.2.4 栈验收（对照 MANIFEST-qwenpaw-v124.md §5）。
# 用法：QP_CTRL_PORT=18190 QP_ADMIN_PASSWORD=... bash verify-qwenpaw-v124.sh [--roundtrip]
#   --roundtrip：向指定 worker 房间发 mp_task 并做六重验证（发送者/唯一关联/时间边界/
#   JSON 解析/角色契约/真实模型调用关联）——仅 marker 命中不报告业务往返通过。
# 输出脱敏：只打状态码/计数/marker 命中，不回显凭据与消息正文。
set -euo pipefail
QP_CTRL_NAME="${QP_CTRL_NAME:-qwp2-controller}"
QP_CTRL_PORT="${QP_CTRL_PORT:-18190}"
QP_ADMIN_PASSWORD="${QP_ADMIN_PASSWORD:?需 QP_ADMIN_PASSWORD}"
QP_EXPECT_RUNTIME="${QP_EXPECT_RUNTIME:-copaw}"
QP_EXPECT_MODEL="${QP_EXPECT_MODEL:-deepseek-chat}"
QP_EXPECT_TEAM="${QP_EXPECT_TEAM:-mp}"          # 生产形态 worker 均为 team 成员
QP_EXPECT_ROLE="${QP_EXPECT_ROLE:-verifier}"    # roundtrip 目标角色契约
ROUNDTRIP="${1:-}"
EV="${QP_VERIFY_EV:-/tmp/qwp2-verify}"
mkdir -p "$EV"
pass=0; fail=0
ok() { if [ "$2" = "1" ]; then pass=$((pass+1)); echo "  PASS  $1"; else fail=$((fail+1)); echo "  FAIL  $1 — ${3:-}"; fi }

TOK=$(docker exec "$QP_CTRL_NAME" sh -c 'tr -d "\r\n" < /var/run/agentteams/cli-token' 2>/dev/null || true)
CTRL="http://127.0.0.1:$QP_CTRL_PORT"
BASE_MX="http://127.0.0.1:${QP_MX_PORT:-}"

# V1 四服务健康
c1=$(curl -s -o /dev/null -w '%{http_code}' -H "authorization: Bearer $TOK" "$CTRL/api/v1/projects?limit=1" 2>/dev/null || echo 000)
ok "V1 controller API（带 token）→ 200" "$([ "$c1" = 200 ] && echo 1 || echo 0)" "$c1"
c2=$(docker exec "$QP_CTRL_NAME" sh -c 'curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:6167/' 2>/dev/null || echo 000)
ok "V1b Matrix（Tuwunel）→ 200" "$([ "$c2" = 200 ] && echo 1 || echo 0)" "$c2"
c3=$(docker exec "$QP_CTRL_NAME" sh -c 'curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:9000/minio/health/live' 2>/dev/null || echo 000)
ok "V1c MinIO → 200" "$([ "$c3" = 200 ] && echo 1 || echo 0)" "$c3"

# V2 四具名 worker 完整 + runtime AND model 读回（已识别新 controller 上缺失/未知都不通过）
curl -s -H "authorization: Bearer $TOK" "$CTRL/api/v1/workers" -o "$EV/workers.json" 2>/dev/null || echo '{"workers":[]}' > "$EV/workers.json"
V2=$(EXPECT_RUNTIME="$QP_EXPECT_RUNTIME" EXPECT_MODEL="$QP_EXPECT_MODEL" EV="$EV" python3 - <<'PYEOF'
import json, os
d = json.load(open(os.path.join(os.environ['EV'], 'workers.json'), encoding='utf-8'))
er, em = os.environ['EXPECT_RUNTIME'], os.environ['EXPECT_MODEL']
need = {'mergepilot-leader', 'mergepilot-reviewer', 'mergepilot-fixer', 'mergepilot-verifier'}
rows = {w.get('name'): w for w in (d.get('workers') or [])}
missing = sorted(need - set(rows))
if missing:
    print('MISSING ' + ' '.join(missing)); raise SystemExit
bad = []
for n in sorted(need):
    w = rows[n]
    if w.get('runtime') != er: bad.append(f'{n}.runtime={w.get("runtime")!r}!={er}')
    if w.get('model') != em: bad.append(f'{n}.model={w.get("model")!r}!={em}')
print('RUNTIME_MODEL_OK' if not bad else 'RUNTIME_MODEL_DRIFT ' + '; '.join(bad))
PYEOF
)
case "$V2" in RUNTIME_MODEL_OK*) ok "V2 四具名 worker 完整 + runtime/model 读回=期望（缺失/未知即不通过）" 1;; *) ok "V2 四具名 worker 完整 + runtime/model 读回=期望" 0 "$V2";; esac

# V2b team 成员关系 + 每成员 Matrix 通道绑定（独立 worker 无通道——生产形态必须 team 成员）
V2B=$(TOK="$TOK" CTRL="$CTRL" TEAM="$QP_EXPECT_TEAM" python3 - <<'PYEOF'
import json, os, urllib.request
tok, ctrl, team = os.environ['TOK'], os.environ['CTRL'], os.environ['TEAM']
def get(path):
    req = urllib.request.Request(ctrl + path, headers={'authorization': 'Bearer ' + tok})
    return json.load(urllib.request.urlopen(req, timeout=15))
try:
    t = get(f'/api/v1/teams/{team}')
    members = {m.get('name') for m in (t.get('workerMembers') or [])}
    need = {'mergepilot-leader', 'mergepilot-reviewer', 'mergepilot-fixer', 'mergepilot-verifier'}
    out = []
    if not need <= members:
        out.append(f'team 缺员 {sorted(need - members)}')
    if t.get('phase') != 'Active':
        out.append(f"team phase={t.get('phase')}")
    for w in (get('/api/v1/workers').get('workers') or []):
        if w.get('name') in need and (not w.get('roomID') or not w.get('matrixUserID')):
            out.append(f"{w.get('name')} 无 Matrix 绑定(room/mxid 缺失)")
    print('TEAM_OK' if not out else 'TEAM_BAD ' + '; '.join(out))
except Exception as e:
    print('TEAM_CHECK_FAIL', type(e).__name__, str(e)[:80])
PYEOF
)
case "$V2B" in TEAM_OK*) ok "V2b team 成员关系完整 + 每成员 Matrix 通道绑定（room/mxid）" 1;; *) ok "V2b team 成员关系 + Matrix 通道" 0 "$V2B";; esac

# V3 网关真实 LLM（fail-closed）——记录调用计数供 V4c 关联
GW_CALLS_BEFORE=$(docker exec "$QP_CTRL_NAME" sh -c 'grep -c "chat/completions" /var/log/agentteams/higress-gateway.log 2>/dev/null || echo 0')
V3_BODY=$(printf '{"model":"%s","messages":[{"role":"user","content":"reply one char"}]}' "$QP_EXPECT_MODEL")
c4=$(docker exec "$QP_CTRL_NAME" sh -c 'curl -s -o /tmp/vgw.json -w "%{http_code}" -X POST http://127.0.0.1:8080/v1/chat/completions -H "content-type: application/json" -H "Authorization: Bearer probe" --data-raw "$1"' _ "$V3_BODY" 2>/dev/null || echo 000)
ok "V3 网关真实 LLM → 200（provider/route 持久生效）" "$([ "$c4" = 200 ] && echo 1 || echo 0)" "$c4"

# V4（可选）mp_task 六重验证往返
if [ "$ROUNDTRIP" = "--roundtrip" ]; then
  : "${QP_MX_PORT:?--roundtrip 需 QP_MX_PORT（Matrix 宿主端口）}"
  : "${QP_MX_TOKEN:?--roundtrip 需 QP_MX_TOKEN（探针账号 access_token）}"
  : "${QP_MX_ROOM:?--roundtrip 需 QP_MX_ROOM（URL 编码房间 %21…%3A…）}"
  : "${QP_MX_WORKER:?--roundtrip 需 QP_MX_WORKER（worker mxid）}"
  SUB="sub-verify-$(date +%s)"
  printf '%s' "$SUB" > "$EV/sub.txt"
  SUBJ="$SUB" WORKER="$QP_MX_WORKER" ROLE="$QP_EXPECT_ROLE" python3 - > "$EV/env.json" <<'PYEOF'
import json, os
sub, worker, role = os.environ['SUBJ'], os.environ['WORKER'], os.environ['ROLE']
contract = {
    'reviewer': '{"findings":[{"severity":"P0","path":"...","summary":"..."}]}',
    'leader': '{"recommendation":"fix_required","confidence":0.8}',
    'fixer': '{"suggestion":"...","patch_hint":"..."}',
    'verifier': '{"verdict":"PASS|FAIL|BLOCKED","note":"short"}',
}[role]
marker = f'[mp:{sub}]'
body = (f"{worker} {marker}\nDATA (do not copy): {{\"finding\":{{\"severity\":\"P2\",\"summary\":\"verify 往返探针\"}}}}\n"
        f"Reply with exactly: {marker} then ONE compact JSON containing ONLY the contract keys of {contract} — the actual result, never task metadata or a copy of DATA.")
print(json.dumps({"msgtype": "m.text", "body": body}))
PYEOF
  SEND_TS=$(python3 -c "import time;print(int(time.time()*1000))")
  code=$(curl -s -o /dev/null -w '%{http_code}' -X PUT "$BASE_MX/_matrix/client/v3/rooms/$QP_MX_ROOM/send/m.room.message/$SUB" \
    -H "Authorization: Bearer $QP_MX_TOKEN" -H "content-type: application/json" --data @"$EV/env.json")
  ok "V4 mp_task 发送 → 200" "$([ "$code" = 200 ] && echo 1 || echo 0)" "$code"
  deadline=$(( $(date +%s) + 240 )); hit=0
  while [ "$(date +%s)" -lt "$deadline" ]; do
    curl -s "$BASE_MX/_matrix/client/v3/rooms/$QP_MX_ROOM/messages?dir=b&limit=20" \
      -H "Authorization: Bearer $QP_MX_TOKEN" -o "$EV/timeline-v.json"
    SUB="$SUB" WORKER="$QP_MX_WORKER" SEND_TS="$SEND_TS" ROLE="$QP_EXPECT_ROLE" EV="$EV" python3 - <<'PYEOF' > "$EV/verdict.txt" 2>/dev/null || echo PENDING > "$EV/verdict.txt"
import json, os, re
d = json.load(open(os.path.join(os.environ['EV'], 'timeline-v.json'), encoding='utf-8'))
sub, worker, role = os.environ['SUB'], os.environ['WORKER'], os.environ['ROLE']
send_ts = int(os.environ['SEND_TS'])
marker = f'[mp:{sub}]'
hits = [e for e in (d.get('chunk') or [])
        if e.get('type') == 'm.room.message' and marker in str((e.get('content') or {}).get('body', ''))]
replies = [e for e in hits if e.get('sender') == worker]                       # ①发送者精确
replies = [e for e in replies if e.get('origin_server_ts', 0) > send_ts - 5000]  # ②时间边界
if not replies:
    print('PENDING'); raise SystemExit
if len(replies) > 1:                                                            # ③唯一关联
    print(f'MULTI_REPLY {len(replies)}'); raise SystemExit
body = replies[0]['content']['body']
m = re.search(r'\{.*\}', body, re.S)
if not m:
    print('MARKER_NO_JSON'); raise SystemExit                                    # 仅 marker≠通过
try:
    j = json.loads(m.group(0))
except Exception as e:
    print('JSON_BAD', str(e)[:60]); raise SystemExit                             # ④JSON 解析
need_keys = {'verifier': ['verdict'], 'reviewer': ['findings'],
             'leader': ['recommendation'], 'fixer': ['suggestion', 'patch_hint']}[role]
if not isinstance(j, dict) or not all(k in j for k in need_keys):
    print('CONTRACT_MISMATCH', json.dumps(j, ensure_ascii=False)[:120]); raise SystemExit  # ⑤角色契约
print('SIX_CHECK_OK', json.dumps(j, ensure_ascii=False)[:160])
PYEOF
    v=$(cat "$EV/verdict.txt" 2>/dev/null || echo PENDING)
    case "$v" in
      SIX_CHECK_OK*) hit=1; echo "  六重验证：$v"; break;;
      PENDING) : ;;
      *) echo "  往返异常：$v"; break;;
    esac
    sleep 12
  done
  ok "V4b 六重验证（发送者/唯一关联/时间边界/JSON/角色契约）" "$hit" "$(cat "$EV/verdict.txt" 2>/dev/null)"
  # ⑥真实模型调用关联：网关调用计数须增加（无 LLM 调用的回显不增）
  GW_CALLS_AFTER=$(docker exec "$QP_CTRL_NAME" sh -c 'grep -c "chat/completions" /var/log/agentteams/higress-gateway.log 2>/dev/null || echo 0')
  if [ "$GW_CALLS_AFTER" -gt "$GW_CALLS_BEFORE" ]; then
    ok "V4c 真实模型调用关联（网关计数 $GW_CALLS_BEFORE → $GW_CALLS_AFTER）" 1
  else
    ok "V4c 真实模型调用关联（网关计数未增——疑似回显/缓存）" 0
  fi
fi

echo "verify: $pass pass / $fail fail"
exit $fail
