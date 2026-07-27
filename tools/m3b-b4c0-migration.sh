#!/bin/bash
# m3b-b4c0-migration.sh — B4c-0 migration 验收 + 自检(复审 9 条修正的 DB 侧)。
# 覆盖:sha256 一致性断言(复审 #9)/ migration 幂等应用 /
#       task_runs 两列 / 活动票据唯一索引(#4) / l2_ensure_ticket 幂等(#4) /
#       l2_expire_approved(#5) / owner+GRANT 收敛 / APPROVAL_PENDING 仍在 status CHECK。
set -uo pipefail
OUT=/mnt/d/goai/tools/m3b-b4c0-migration.out
: > "$OUT"
log(){ echo "$*" >> "$OUT"; }
PASS=0; FAIL=0
ok(){ echo "  ✅ $1" >> "$OUT"; PASS=$((PASS+1)); }
bad(){ echo "  ❌ $1" >> "$OUT"; FAIL=$((FAIL+1)); }

DIR=/home/ngh/.config/mergepilot
CTRL="$DIR/controller.env"
PG_SU=$(grep '^PG_USER=' "$CTRL" | cut -d= -f2- | tr -d '"'\''[:space:]')
PG_DB=$(grep '^PG_DATABASE=' "$CTRL" | cut -d= -f2- | tr -d '"'\''[:space:]')
PG_DB=${PG_DB:-mergepilot_audit}; PG_SU=${PG_SU:-mergepilot}
SU_PW=$(grep '^PG_PASS=' "$CTRL" | head -1 | cut -d= -f2- | tr -d '"'\''[:space:]')
SU(){ docker exec -e PGPASSWORD="$SU_PW" audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c "$1" 2>&1; }
SU_FILE(){ docker exec -i -e PGPASSWORD="$SU_PW" audit-pg psql -U "$PG_SU" -d "$PG_DB" -v ON_ERROR_STOP=1 2>&1; }

log "═══════════════════════════════════════════════"
log "  B4c-0 migration 验收"
log "═══════════════════════════════════════════════"

# ─── wait for PG ready(WSL suspend/resume 后 PG 走 crash-recovery,需等就绪)───
log ""; log "=== wait for PG ready ==="
PG_READY=0
for i in $(seq 1 30); do
  if docker exec audit-pg pg_isready -U "$PG_SU" -d "$PG_DB" >/dev/null 2>&1; then PG_READY=1; log "  PG ready (poll $i)"; break; fi
  sleep 2
done
if [ "$PG_READY" != "1" ]; then bad "PG 60s 内未就绪——环境不稳,中止"; log "..."; log "  PASS=$PASS FAIL=$FAIL"; [ "$FAIL" -eq 0 ] || exit 1; exit 1; fi

# ─── 0. sha256 一致性(复审 #9:mergepilot-os 为唯一 git 源,镜像必须一致)───
log ""; log "=== 0. 源码 sha256 一致性(mergepilot-os ↔ tools 镜像) ==="
for rel in workflow-controller/controller.py audit-db/m3b_b4c.sql policy-gateway/gateway.py; do
  A=$(sha256sum "/mnt/d/goai/mergepilot-os/tools/$rel" 2>/dev/null | cut -c1-16)
  B=$(sha256sum "/mnt/d/goai/tools/$rel" 2>/dev/null | cut -c1-16)
  if [ -n "$A" ] && [ "$A" = "$B" ]; then ok "$rel 一致($A)"; else bad "$rel 漂移(a=$A b=$B)"; fi
done

# ─── 1. migration 幂等应用(跑两次,第二次应无错)───
log ""; log "=== 1. migration 幂等应用 ==="
R1=$(SU_FILE < /mnt/d/goai/mergepilot-os/tools/audit-db/m3b_b4c.sql | tail -2)
R2=$(SU_FILE < /mnt/d/goai/mergepilot-os/tools/audit-db/m3b_b4c.sql | tail -2)
if echo "$R1" | grep -qiE "ERROR|FATAL"; then bad "首次应用出错: $(echo "$R1"|tr '\n' ' ')"; else ok "首次应用成功"; fi
if echo "$R2" | grep -qiE "ERROR|FATAL"; then bad "二次应用(幂等)出错: $(echo "$R2"|tr '\n' ' ')"; else ok "二次应用幂等(无错)"; fi

# ─── 2. task_runs 两列(复审 #2/#4)───
log ""; log "=== 2. task_runs: approval_required + l2_discovery_attempts ==="
AR=$(SU "SELECT data_type||'|'||column_default FROM information_schema.columns WHERE table_name='task_runs' AND column_name='approval_required';")
[ "$AR" = "boolean|false" ] && ok "approval_required boolean DEFAULT false" || bad "approval_required 异常: '$AR'"
DA=$(SU "SELECT data_type||'|'||column_default FROM information_schema.columns WHERE table_name='task_runs' AND column_name='l2_discovery_attempts';")
[ "$DA" = "integer|0" ] && ok "l2_discovery_attempts integer DEFAULT 0" || bad "l2_discovery_attempts 异常: '$DA'"

# ─── 3. 未终结票据唯一索引(B4c-0.1:阻塞集含 UNKNOWN/USED)───
log ""; log "=== 3. 未终结票据唯一索引 uq_active_ticket_per_binding_action ==="
IDX=$(SU "SELECT indexdef FROM pg_indexes WHERE indexname='uq_active_ticket_per_binding_action';")
echo "$IDX" | grep -qi "UNIQUE INDEX" && ok "唯一索引存在" || bad "唯一索引缺失: '$IDX'"
# B4c-0.1 #1:谓词必须含 PENDING/APPROVED/EXECUTING/UNKNOWN/USED(只有 FAILED/EXPIRED 可建新 attempt)
for st in PENDING APPROVED EXECUTING UNKNOWN USED; do
  echo "$IDX" | grep -qi "$st" && : || bad "索引谓词缺 $st"
done
echo "$IDX" | grep -qiE "PENDING.*APPROVED.*EXECUTING.*UNKNOWN.*USED" && ok "partial(5 未终结态:PENDING/APPROVED/EXECUTING/UNKNOWN/USED)" || bad "索引谓词不完整: '$IDX'"

# ─── 4. 函数 owner + GRANT 收敛(NOLOGIN owner + mergepilot EXECUTE + 无 PUBLIC)───
log ""; log "=== 4. l2_ensure_ticket / l2_expire_approved: owner + GRANT ==="
chk_fn(){ # $1=fn $2=完整签名(用于 regprocedure 定位 + has_function_privilege)
  local fn="$1" sig="$2"
  local own pub mp
  # owner:经 regprocedure::oid 精确定位(避免同名 overload 歧义)
  own=$(SU "SELECT rolname FROM pg_proc p JOIN pg_roles r ON p.proowner=r.oid WHERE p.oid='$sig'::regprocedure::oid;")
  [ "$own" = "mergepilot_l2_owner" ] && ok "$fn owner=mergepilot_l2_owner(NOLOGIN)" || bad "$fn owner 异常: '$own'"
  # PUBLIC:has_function_privilege('public',...) 反映 REVOKE 后的真实态(public 非超管)
  pub=$(SU "SELECT has_function_privilege('public','$sig','EXECUTE');")
  [ "$pub" = "f" ] && ok "$fn 无 PUBLIC EXECUTE" || bad "$fn 仍可被 PUBLIC 执行('$pub')"
  # mergepilot 显式 GRANT:aclexplode 看字面条目(mergepilot 是超管,可调用性由功能测试 step6/8 覆盖)
  mp=$(SU "SELECT count(*) FROM pg_proc p,aclexplode(p.proacl) a WHERE p.oid='$sig'::regprocedure::oid AND a.grantee='mergepilot'::regrole AND a.privilege_type='EXECUTE';")
  [ "${mp:-0}" != "0" ] && ok "$fn GRANT EXECUTE TO mergepilot(字面条目)" || bad "$fn 缺 mergepilot 显式 GRANT"
}
chk_fn l2_ensure_ticket "l2_ensure_ticket(text,text,jsonb,text,integer,integer)"
chk_fn l2_expire_approved "l2_expire_approved(text)"

# ─── 5. setup 测试数据 ───
log ""; log "=== 5. setup 测试数据 ==="
SU "DELETE FROM policy_action_outbox WHERE run_id LIKE 'b4c0test-%'; DELETE FROM approvals WHERE run_id LIKE 'b4c0test-%'; DELETE FROM run_pr_bindings WHERE run_id LIKE 'b4c0test-%'; DELETE FROM task_runs WHERE run_id LIKE 'b4c0test-%';" >/dev/null 2>&1
SU "INSERT INTO task_runs(run_id,status,repo,pr_number,approval_required) VALUES('b4c0test-run','APPROVAL_PENDING','nghqqa/MergePilot',99999,TRUE);" >/dev/null 2>&1
SU "INSERT INTO run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha) VALUES('bnd-b4c0','b4c0test-run','nghqqa/MergePilot',99999,'fix/b4c0-test','main','deadbeef00000000000000000000000000000000');" >/dev/null 2>&1
V=$(SU "SELECT (SELECT count(*) FROM task_runs WHERE run_id='b4c0test-run')||'/'||(SELECT count(*) FROM run_pr_bindings WHERE binding_id='bnd-b4c0');")
[ "$V" = "1/1" ] && ok "测试数据就绪(bnd-b4c0 / b4c0test-run, approval_required=TRUE)" || bad "测试数据写入失败: '$V'(run/binding 未落)"

# ─── 6. l2_ensure_ticket 幂等(复审 #4:重复调返回同票,attempt 不增)───
log ""; log "=== 6. l2_ensure_ticket 幂等 ==="
PAYLOAD='{"owner":"nghqqa","repo":"MergePilot","pullNumber":99999,"commit_title":"b4c0","merge_method":"squash"}'
AH=$(python3 -c "import hashlib,json,sys; d=json.loads(sys.argv[1]); print(hashlib.sha256(json.dumps(d,sort_keys=True,separators=(',',':')).encode()).hexdigest())" "$PAYLOAD")
TKT1=$(SU "SELECT l2_ensure_ticket('bnd-b4c0','merge','$PAYLOAD'::jsonb,'$AH',24,1);")
TKT2=$(SU "SELECT l2_ensure_ticket('bnd-b4c0','merge','$PAYLOAD'::jsonb,'$AH',24,1);")
ATT=$(SU "SELECT attempt_no FROM approvals WHERE ticket_id='$TKT1';")
log "  tkt1=$TKT1 tkt2=$TKT2 attempt=$ATT"
[ "$TKT1" = "$TKT2" ] && ok "重复 ensure 返回同 ticket_id(幂等)" || bad "返回不同票: $TKT1 vs $TKT2"
[ "$ATT" = "1" ] && ok "attempt_no=1(未新建 attempt)" || bad "attempt 异常: $ATT"
CNT=$(SU "SELECT count(*) FROM approvals WHERE binding_id='bnd-b4c0' AND action='merge' AND status='PENDING';")
[ "$CNT" = "1" ] && ok "仅 1 条活动 PENDING 票" || bad "活动票数=$CNT(应=1)"

# ─── 7. 活动票据唯一索引兜底(绕过 ensure 直接插第二条 PENDING → 必被拒)───
log ""; log "=== 7. 活动票据唯一索引兜底(直接插第 2 条 PENDING → 拒) ==="
DUP=$(SU "INSERT INTO approvals(ticket_id,binding_id,run_id,action,repo,status) VALUES('tkt-dup-b4c0','bnd-b4c0','b4c0test-run','merge','nghqqa/MergePilot','PENDING');" 2>&1)
if echo "$DUP" | grep -qi "uq_active_ticket_per_binding_action\|duplicate key"; then ok "第 2 条活动票被唯一索引拒绝"; else bad "唯一索引未生效: $(echo "$DUP"|head -1)"; fi

# ─── 8. l2_expire_approved(复审 #5:APPROVED 执行期过期 → EXPIRED)───
log ""; log "=== 8. l2_expire_approved(APPROVED 过期 → EXPIRED) ==="
SU "SELECT l2_approve('$TKT1','b4c0test@host');" >/dev/null 2>&1
ST_AP=$(SU "SELECT status FROM approvals WHERE ticket_id='$TKT1';")
[ "$ST_AP" = "APPROVED" ] && ok "l2_approve → APPROVED" || bad "approve 异常: $ST_AP"
SU "UPDATE approvals SET expires_at = now() - interval '1 hour' WHERE ticket_id='$TKT1';" >/dev/null 2>&1
EXP=$(SU "SELECT l2_expire_approved('$TKT1');")
ST_EX=$(SU "SELECT status FROM approvals WHERE ticket_id='$TKT1';")
[ "$EXP" = "t" ] && ok "l2_expire_approved 返回 true" || bad "expire_approved 返回: '$EXP'"
[ "$ST_EX" = "EXPIRED" ] && ok "APPROVED 过期 → EXPIRED" || bad "迁移异常: $ST_EX"
# 反向:未过期的 APPROVED 不被迁移(新一张)
TKT3=$(SU "SELECT l2_ensure_ticket('bnd-b4c0','merge','$PAYLOAD'::jsonb,'$AH',24,1);")  # 前张 EXPIRED → 新 attempt
SU "SELECT l2_approve('$TKT3','b4c0test@host');" >/dev/null 2>&1
EXP3=$(SU "SELECT l2_expire_approved('$TKT3');")
[ "$EXP3" = "f" ] && ok "未过期的 APPROVED 不迁移(返回 false)" || bad "误迁移未过期票: '$EXP3'"

# ─── 8.5 USED/UNKNOWN 阻塞新建(B4c-0.1 #1:只有 FAILED/EXPIRED 可建新 attempt)───
log ""; log "=== 8.5 USED/UNKNOWN 态:ensure 不新建(FAILED 才建新 attempt) ==="
SU "INSERT INTO task_runs(run_id,status,repo,pr_number,approval_required) VALUES('b4c0test-run2','APPROVAL_PENDING','nghqqa/MergePilot',99998,TRUE) ON CONFLICT(run_id) DO NOTHING;" >/dev/null 2>&1
SU "INSERT INTO run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha) VALUES('bnd-b4c0-used','b4c0test-run2','nghqqa/MergePilot',99998,'fix/b4c0-used','main','used000000000000000000000000000000000000000') ON CONFLICT (binding_id) DO NOTHING;" >/dev/null 2>&1
UPAYLOAD='{"owner":"nghqqa","repo":"MergePilot","pullNumber":99998,"commit_title":"used","merge_method":"squash"}'
UAH=$(python3 -c "import hashlib,json,sys; d=json.loads(sys.argv[1]); print(hashlib.sha256(json.dumps(d,sort_keys=True,separators=(',',':')).encode()).hexdigest())" "$UPAYLOAD")
UTKT=$(SU "SELECT l2_ensure_ticket('bnd-b4c0-used','merge','$UPAYLOAD'::jsonb,'$UAH',24,1);")
SU "SELECT l2_approve('$UTKT','b4c0test@host');" >/dev/null 2>&1
SU "UPDATE approvals SET status='USED', used_at=now(), result_sha='abc' WHERE ticket_id='$UTKT';" >/dev/null 2>&1
UTKT2=$(SU "SELECT l2_ensure_ticket('bnd-b4c0-used','merge','$UPAYLOAD'::jsonb,'$UAH',24,1);")
UCNT=$(SU "SELECT count(*) FROM approvals WHERE binding_id='bnd-b4c0-used' AND action='merge';")
log "  used_tkt=$UTKT ensure2=$UTKT2 rows=$UCNT"
[ "$UTKT" = "$UTKT2" ] && ok "USED 态:ensure 返回同票(不新建)" || bad "USED 后 ensure 返回不同票"
[ "$UCNT" = "1" ] && ok "USED 态未新建 attempt(仍 1 行)" || bad "USED 后多建行: $UCNT"
SU "UPDATE approvals SET status='UNKNOWN' WHERE ticket_id='$UTKT';" >/dev/null 2>&1
UTKT3=$(SU "SELECT l2_ensure_ticket('bnd-b4c0-used','merge','$UPAYLOAD'::jsonb,'$UAH',24,1);")
UCNT2=$(SU "SELECT count(*) FROM approvals WHERE binding_id='bnd-b4c0-used' AND action='merge';")
[ "$UTKT" = "$UTKT3" ] && ok "UNKNOWN 态:ensure 返回同票(待对账,不新建)" || bad "UNKNOWN 后 ensure 返回不同票"
[ "$UCNT2" = "1" ] && ok "UNKNOWN 态未新建 attempt" || bad "UNKNOWN 后多建行: $UCNT2"
SU "UPDATE approvals SET status='FAILED' WHERE ticket_id='$UTKT';" >/dev/null 2>&1
UTKT4=$(SU "SELECT l2_ensure_ticket('bnd-b4c0-used','merge','$UPAYLOAD'::jsonb,'$UAH',24,1);")
UCNT3=$(SU "SELECT count(*) FROM approvals WHERE binding_id='bnd-b4c0-used' AND action='merge';")
[ "$UTKT4" != "$UTKT" ] && ok "FAILED 态:ensure 建新 attempt(允许重试)" || bad "FAILED 后未建新票"
[ "$UCNT3" = "2" ] && ok "FAILED 后总行数=2(新 attempt)" || bad "FAILED 后行数异常: $UCNT3"

# ─── 8.6 payload/hash 不一致 → ensure 抛错(B4c-0.1 #2:不静默返回不匹配旧票)───
log ""; log "=== 8.6 ensure payload/hash 不一致 → 抛错 ==="
DIFF_PAYLOAD='{"owner":"nghqqa","repo":"MergePilot","pullNumber":99999,"commit_title":"DIFFERENT","merge_method":"squash"}'
DIFF_AH=$(python3 -c "import hashlib,json,sys; d=json.loads(sys.argv[1]); print(hashlib.sha256(json.dumps(d,sort_keys=True,separators=(',',':')).encode()).hexdigest())" "$DIFF_PAYLOAD")
MISMATCH=$(SU "SELECT l2_ensure_ticket('bnd-b4c0','merge','$DIFF_PAYLOAD'::jsonb,'$DIFF_AH',24,1);" 2>&1)
if echo "$MISMATCH" | grep -qiE "payload/hash mismatch|mismatch"; then ok "payload 不一致 → 抛错(不返回旧票)"; else bad "payload 不一致应抛错: $(echo "$MISMATCH"|head -1)"; fi

# ─── 8.6b TTL 不一致 → ensure 抛错(B4c-0.2 P2:双 TTL 比较,不同 TTL 同幂等请求拒绝)───
log ""; log "=== 8.6b ensure TTL 不一致 → 抛错(22023 非重试) ==="
# bnd-b4c0 当前 TKT3(APPROVED,payload=PAYLOAD,TTL=24/1)。同 payload/hash 但改 TTL → 必抛错
TTLM=$(SU "SELECT l2_ensure_ticket('bnd-b4c0','merge','$PAYLOAD'::jsonb,'$AH',24,2);" 2>&1)
if echo "$TTLM" | grep -qiE "TTL mismatch|mismatch"; then ok "exec_ttl 不一致(1→2)→ 抛错"; else bad "exec_ttl 不一致应抛错: $(echo "$TTLM"|head -1)"; fi
TTLM2=$(SU "SELECT l2_ensure_ticket('bnd-b4c0','merge','$PAYLOAD'::jsonb,'$AH',12,1);" 2>&1)
if echo "$TTLM2" | grep -qiE "TTL mismatch|mismatch"; then ok "approval_ttl 不一致(24→12)→ 抛错"; else bad "approval_ttl 不一致应抛错: $(echo "$TTLM2"|head -1)"; fi
# 反向:同 payload/hash/TTL → 仍幂等返回旧票(不抛错)
TTLM3=$(SU "SELECT l2_ensure_ticket('bnd-b4c0','merge','$PAYLOAD'::jsonb,'$AH',24,1);" 2>&1)
echo "$TTLM3" | grep -q "^tkt-" && ok "payload/hash/TTL 全一致 → 幂等返回旧票(不抛错)" || bad "全一致应幂等: $(echo "$TTLM3"|head -1)"

# ─── 8.7 真并发 ensure → 同票(B4c-0.1 #2:advisory 锁串行化)───
log ""; log "=== 8.7 真并发 ensure(两 psql 并行)→ 同票 ==="
SU "INSERT INTO task_runs(run_id,status,repo,pr_number,approval_required) VALUES('b4c0test-run3','APPROVAL_PENDING','nghqqa/MergePilot',99997,TRUE) ON CONFLICT(run_id) DO NOTHING;" >/dev/null 2>&1
SU "INSERT INTO run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha) VALUES('bnd-b4c0-conc','b4c0test-run3','nghqqa/MergePilot',99997,'fix/b4c0-conc','main','conc0000000000000000000000000000000000000000') ON CONFLICT (binding_id) DO NOTHING;" >/dev/null 2>&1
CPAYLOAD='{"owner":"nghqqa","repo":"MergePilot","pullNumber":99997,"commit_title":"conc","merge_method":"squash"}'
CAH=$(python3 -c "import hashlib,json,sys; d=json.loads(sys.argv[1]); print(hashlib.sha256(json.dumps(d,sort_keys=True,separators=(',',':')).encode()).hexdigest())" "$CPAYLOAD")
docker exec -e PGPASSWORD="$SU_PW" audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c "SELECT l2_ensure_ticket('bnd-b4c0-conc','merge','$CPAYLOAD'::jsonb,'$CAH',24,1);" >/tmp/conc_a.txt 2>&1 &
docker exec -e PGPASSWORD="$SU_PW" audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c "SELECT l2_ensure_ticket('bnd-b4c0-conc','merge','$CPAYLOAD'::jsonb,'$CAH',24,1);" >/tmp/conc_b.txt 2>&1 &
wait
CONC_A=$(tr -d '[:space:]' < /tmp/conc_a.txt)
CONC_B=$(tr -d '[:space:]' < /tmp/conc_b.txt)
CONC_CNT=$(SU "SELECT count(*) FROM approvals WHERE binding_id='bnd-b4c0-conc' AND action='merge';")
log "  conc_a=$CONC_A conc_b=$CONC_B total=$CONC_CNT"
[ "$CONC_A" = "$CONC_B" ] && ok "并发 ensure 返回同 ticket_id" || bad "并发返回不同: $CONC_A vs $CONC_B"
[ "$CONC_CNT" = "1" ] && ok "并发只建 1 张票(advisory 锁串行化)" || bad "并发建 $CONC_CNT 张(应 1)"

# ─── 9. 回归:status CHECK 仍含 APPROVAL_PENDING;current_stage 接受 l2_binding ───
log ""; log "=== 9. 回归(APPROVAL_PENDING CHECK / current_stage=l2_binding) ==="
CK=$(SU "SELECT 1 FROM pg_constraint WHERE conname='chk_task_status';")
AP_OK=$(SU "SELECT count(*) WHERE 'APPROVAL_PENDING' = ANY(string_to_array(regexp_replace(pg_get_constraintdef((SELECT oid FROM pg_constraint WHERE conname='chk_task_status')),'[^a-zA-Z_]' ,',','g'),',')) AND 'APPROVAL_PENDING' IN (SELECT regexp_matches(pg_get_constraintdef((SELECT oid FROM pg_constraint WHERE conname='chk_task_status')),'APPROVAL_PENDING','g'));" 2>/dev/null)
AP_OK=$(SU "SELECT (pg_get_constraintdef((SELECT oid FROM pg_constraint WHERE conname='chk_task_status')) LIKE '%APPROVAL_PENDING%')::int;")
[ "${AP_OK:-0}" = "1" ] && ok "chk_task_status 含 APPROVAL_PENDING" || bad "CHECK 缺 APPROVAL_PENDING"
SU "UPDATE task_runs SET current_stage='l2_binding' WHERE run_id='b4c0test-run';" >/dev/null 2>&1
CS=$(SU "SELECT current_stage FROM task_runs WHERE run_id='b4c0test-run';")
[ "$CS" = "l2_binding" ] && ok "current_stage 接受 'l2_binding'(待办标记)" || bad "current_stage 写入失败: '$CS'"

# ─── evidence snapshot ───
log ""; log "=== evidence snapshot ==="
mkdir -p /mnt/d/goai/evidence/m3b-b4c/0-migration
SU "\d+ task_runs" > /mnt/d/goai/evidence/m3b-b4c/0-migration/task_runs-schema.txt 2>/dev/null
SU "SELECT proname, rolname AS owner FROM pg_proc p JOIN pg_roles r ON p.proowner=r.oid WHERE proname LIKE 'l2_%' ORDER BY proname;" > /mnt/d/goai/evidence/m3b-b4c/0-migration/l2-functions-owners.txt 2>/dev/null
SU "SELECT indexdef FROM pg_indexes WHERE indexname='uq_active_ticket_per_binding_action';" > /mnt/d/goai/evidence/m3b-b4c/0-migration/active-ticket-index.txt 2>/dev/null
log "  snapshot: task_runs-schema / l2-functions-owners / active-ticket-index"

# ─── cleanup ───
SU "DELETE FROM policy_action_outbox WHERE run_id LIKE 'b4c0test-%'; DELETE FROM approvals WHERE run_id LIKE 'b4c0test-%'; DELETE FROM run_pr_bindings WHERE run_id LIKE 'b4c0test-%'; DELETE FROM task_runs WHERE run_id LIKE 'b4c0test-%';" >/dev/null 2>&1

log ""
log "═══════════════════════════════════════════════"
log "  B4c-0 验收: PASS=$PASS FAIL=$FAIL"
log "═══════════════════════════════════════════════"
echo "done -> $OUT (PASS=$PASS FAIL=$FAIL)"
[ "$FAIL" -eq 0 ] || exit 1
