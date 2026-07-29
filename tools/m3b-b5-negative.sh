#!/bin/bash
# m3b-b5-negative.sh — B5 负向证据 ×8(证明现有安全边界;不扩大权限;加固版)。
#
# 覆盖(fixture 隔离,e2e-lib.sh + policy-gw-e2e + e2e_guard;绝不写生产 nghqqa/MergePilot):
#   1. 直连拒:真实 worker(mergepilot-controller,hiclab-net)直连 github-mcp:8082 不可达;
#      核验 worker ∉ mcp-backend-net 且 github-mcp ∈ mcp-backend-net(网络隔离;PAT 不出后端网)。
#   2. list 过滤:各角色 list_tools 完整集合 == policy allowlist ∩ upstream_visible(精确集合比较);
#      非 allowlist repo → REPO_NOT_ALLOWED。
#   3. 跨角色拒:fixer token 上 /coordinator/sse → 401 ROLE_PATH_MISMATCH;fixer 调 merge → TOOL_NOT_ALLOWED。
#   4. fixer 写约束:delete_file→TOOL_NOT_ALLOWED;非 fix/→BRANCH_NOT_FIX_PREFIX;写 main→BRANCH_PROTECTED(精确);
#      .env→PATH_DENIED;update_pr 非 title/body→PR_FIELD_NOT_ALLOWED;被拒分支不存在(0 副作用)。
#   5. 伪造票拒:不存在 ticket→CLAIM_MISMATCH(0 claim);真实 APPROVED 票 + 篡改 args(commit_title)→CLAIM_MISMATCH,
#      票仍 APPROVED、L2_CLAIMED=0、PR 仍 OPEN。
#   6. 过期/重复票拒:expires_at≤now→CLAIM_MISMATCH(票未消耗);USED 票→CLAIM_MISMATCH(status!=APPROVED);PR 仍 OPEN。
#   7. 合法票只执行一次:APPROVED→merge→USED(1×L2_CLAIMED+1×L2_COMPLETE,1 merge commit);再 claim→CLAIM_MISMATCH。
#   8. 完整不可篡改审计:phase='INTENT' AND decision='DENY' 精确计数全部 8 个 DENY reason_code(含 BRANCH_PROTECTED);
#      每个 DENY correlation 不得存在上游 RESULT/ERROR(0 上游调用);UPDATE/DELETE 被触发器拦(超管亦不可);0 行篡改。
#
# 硬门:[ FAIL=0 ] && [ PASS=EXPECTED_PASS ](EXPECTED_PASS 按最终测试项精确计算)。
# 审计窗口:精确 TEST_START/TEST_END;最终验收后再生成完整 transcript。
# 凭据扫描:覆盖 script+evidence,提取真实凭证值逐个精确搜索 + 已知格式扫描(不用 token= 宽泛过滤)。
set -uo pipefail
TOOLS=/mnt/d/goai/mergepilot-os/tools
source "$TOOLS/e2e-lib.sh"
e2e_guard
EV=/mnt/d/goai/mergepilot-os/evidence/m3b-b5
mkdir -p "$EV"; rm -f "$EV"/*.txt "$EV"/*.out "$EV"/*.log 2>/dev/null || true
OUT="$EV/negative-test.out"; : > "$OUT"
log(){ echo "$*" | tee -a "$OUT"; }
logf(){ echo "$*" >> "$OUT"; }
ok(){ log "  ✅ $1"; PASS=$((PASS+1)); }
bad(){ log "  ❌ $1"; FAIL=$((FAIL+1)); }
PASS=0; FAIL=0
TS=$$
EXPECTED_PASS=50   # 按最终测试项精确计算(见结尾硬门;改测试项须同步)

CTRL=/home/ngh/.config/mergepilot/controller.env
PG_SU=$(grep '^PG_USER=' "$CTRL" | cut -d= -f2- | tr -d "\"'[:space:]"); PG_DB=mergepilot_audit
SU_PW=$(grep '^PG_PASS=' "$CTRL" | head -1 | cut -d= -f2- | tr -d "\"'[:space:]")
APV_PW=$(grep '^MERGEPILOT_APPROVER_PASS=' /home/ngh/.config/mergepilot/b4-roles.env | head -1 | cut -d= -f2-)
ECOORD=$(e2e_coordinator_token)
FIXER_TOK=$(python3 -c "import json;print(json.load(open('$E2E_TOKENS_FILE')).get('fixer',''))" 2>/dev/null)
POLICY_YAML="$TOOLS/policy-gateway/policy-e2e-fixture.yaml"
WORKER=mergepilot-controller

PSQL(){ docker exec -e PGPASSWORD="$SU_PW" audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c "$1" 2>/dev/null; }
PSQL_ERR(){ docker exec -e PGPASSWORD="$SU_PW" audit-pg psql -U "$PG_SU" -d "$PG_DB" -t -A -c "$1" 2>&1; }
ah(){ python3 -c "import hashlib,json,sys;print(hashlib.sha256(json.dumps(json.loads(sys.argv[1]),sort_keys=True,separators=(',',':')).encode()).hexdigest())" "$1"; }
GW(){ e2e_GW "$@" 2>&1 || true; }
has(){ echo "$1" | grep -qiE "$2"; }

create_fix_pr(){ local BR="$1" L="$2" R PR attempt
  for attempt in 1 2 3; do
    e2e_GW fixer --call create_branch owner="$E2E_OWNER" repo="$E2E_REPO" branch="$BR" from_branch="$E2E_BASE_BRANCH" >/dev/null 2>&1
    e2e_GW fixer --call create_or_update_file owner="$E2E_OWNER" repo="$E2E_REPO" path="b5-$L-$TS.md" branch="$BR" content="b5$TS-$attempt" message="b5 $L" >/dev/null 2>&1
    R=$(e2e_GW fixer --call create_pull_request owner="$E2E_OWNER" repo="$E2E_REPO" head="$BR" base="$E2E_BASE_BRANCH" title="b5 $L" body=auto 2>&1 || true)
    PR=$(echo "$R" | grep -oE 'pull/[0-9]+' | grep -oE '[0-9]+' | head -1)
    [ -n "$PR" ] && break
    sleep 5
  done
  [ -z "$PR" ] && logf "  (diag) create_fix_pr $L 全 3 次失败;GW 尾: $(echo "$R" | tr -d '\000' | tail -c 200)"
  echo "$PR"; }
read_sha(){ e2e_GW coordinator --call pull_request_read method=get owner="$E2E_OWNER" repo="$E2E_REPO" pullNumber="$1" 2>&1 | python3 -c "import json,sys;print(json.load(sys.stdin)['head']['sha'])" 2>/dev/null; }
# 建一张 APPROVED merge 票。$1=run $2=branch $3=pr $4=label $5=commit_title(默认 b5 <label>)。返 ticket_id。
mk_approved(){ local RUN="$1" BR="$2" PR="$3" L="$4" CT="${5:-b5 $4}" HS BID PAY AH TKT
  HS=$(read_sha "$PR"); PSQL "INSERT INTO task_runs(run_id,status,repo,pr_number,current_stage,approval_required) VALUES('$RUN','APPROVAL_PENDING','$(e2e_repo)',$PR,'l2_awaiting_approval',TRUE) ON CONFLICT(run_id) DO UPDATE SET status='APPROVAL_PENDING',current_stage='l2_awaiting_approval';" >/dev/null
  BID="bnd-$RUN"; PSQL "INSERT INTO run_pr_bindings(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha) VALUES('$BID','$RUN','$(e2e_repo)',$PR,'$BR','main','$HS') ON CONFLICT(binding_id) DO UPDATE SET head_sha=EXCLUDED.head_sha;" >/dev/null
  PAY='{"owner":"'"$E2E_OWNER"'","repo":"'"$E2E_REPO"'","pullNumber":'$PR',"commit_title":"'"$CT"'","merge_method":"squash"}'; AH=$(ah "$PAY")
  TKT=$(PSQL "SELECT l2_create_ticket('$BID','merge','$PAY'::jsonb,'$AH',24,1);")
  docker exec -e PGPASSWORD="$APV_PW" audit-pg psql -U mergepilot_approver -d "$PG_DB" -t -A -c "SELECT l2_approve('$TKT');" >/dev/null 2>&1
  echo "$TKT"; }

cleanup_db(){ PSQL "DELETE FROM policy_action_outbox WHERE run_id LIKE 'b5-%'; DELETE FROM approvals WHERE run_id LIKE 'b5-%'; DELETE FROM run_pr_bindings WHERE run_id LIKE 'b5-%'; DELETE FROM task_runs WHERE run_id LIKE 'b5-%';" >/dev/null 2>&1 || true; }
cleanup_fixture(){ for n in $(gh.exe pr list --repo "$(e2e_repo)" --state open --limit 100 --json number,title -q '.[]|select(.title|test("b5"))|.number' 2>/dev/null); do gh.exe pr close "$n" --repo "$(e2e_repo)" --delete-branch --comment "B5 测试清理" >/dev/null 2>&1 || true; done
  for b in $(gh.exe api "repos/$(e2e_repo)/branches" --jq '.[].name' 2>/dev/null | grep -E '^fix/b5-'); do gh.exe api -X DELETE "repos/$(e2e_repo)/git/refs/heads/$b" >/dev/null 2>&1 || true; done; }
restore(){ docker start policy-gw-e2e >/dev/null 2>&1 || true; docker start "$WORKER" >/dev/null 2>&1 || true; cleanup_db; cleanup_fixture; }
# EXIT trap 须保留触发退出码(否则 restore 末尾命令会掩盖硬门的 exit 1)
trap '_b5_rc=$?; restore; exit $_b5_rc' EXIT

log "═══════════════════════════════════════════════"
log "  B5 负向证据验收 · 加固版(fixture=$(e2e_repo))"
log "═══════════════════════════════════════════════"
for i in $(seq 1 30); do docker exec audit-pg pg_isready -U "$PG_SU" -d "$PG_DB" >/dev/null 2>&1 && break; sleep 2; done
# 审计窗口:精确 start(任何测试动作前;clock_timestamp = 真实时钟,非事务起始 now())
TEST_START=$(PSQL "SELECT clock_timestamp();")
logf "  TEST_START=$TEST_START (审计窗口下界,clock_timestamp)"

# ════════════ 1. 直连拒(真实 worker hiclaw-worker-* + 拓扑核验)════════════
# 真实 worker = hiclab-worker-{fixer,reviewer,verifier}(Agent 容器,hiclab-net);均未运行则 one-shot hiclab-net 代表
log ""; log "=== 1. 直连拒:真实 worker ∉ mcp-backend-net;github-mcp 仅在后端网 → 不可达 ==="
GH_NETS=$(docker inspect github-mcp --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null | tr -d '\000')
BACKEND_MEMBERS=$(docker network inspect mcp-backend-net --format '{{range .Containers}}{{.Name}} {{end}}' 2>/dev/null | tr -d '\000')
PROBE_SRC=""; WK_NETS="hiclab-net"; DIRECT=""
for cand in hiclaw-worker-fixer hiclaw-worker-reviewer hiclaw-worker-verifier; do
  if docker inspect "$cand" >/dev/null 2>&1; then
    PROBE_SRC="$cand"
    # 真实 worker 容器存在但已退出 → 临时启动供探针(不改动其网络配置);探针后保持原状
    [ "$(docker inspect "$cand" --format '{{.State.Status}}' 2>/dev/null | tr -d '\000')" = "running" ] || { docker start "$cand" >/dev/null 2>&1 || true; sleep 2; }
    break
  fi
done
read -r -d '' PROBE_PY <<'PYEOF'
import socket
try:
  s=socket.create_connection(('github-mcp',8082),2); print('REACHABLE'); s.close()
except Exception as e: print('UNREACHABLE: '+type(e).__name__+': '+str(e)[:80])
PYEOF
if [ -n "$PROBE_SRC" ]; then
  WK_NETS=$(docker inspect "$PROBE_SRC" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null | tr -d '\000')
  DIRECT=$(docker exec "$PROBE_SRC" python3 -c "$PROBE_PY" 2>&1 | tr -d '\000' | tail -1)
else
  PROBE_SRC="(one-shot hiclab-net 代表;环境无 hiclaw-worker-* 容器)"
  DIRECT=$(docker run --rm --network hiclab-net mergepilot-controller:latest python3 -c "$PROBE_PY" 2>&1 | tr -d '\000' | tail -1)
fi
logf "  github-mcp 网络: $GH_NETS"
logf "  探针 worker($PROBE_SRC)网络: $WK_NETS"
logf "  mcp-backend-net 成员: $BACKEND_MEMBERS"
logf "  worker 直连探针: $DIRECT"
echo "$GH_NETS" | grep -qw "mcp-backend-net" && ok "github-mcp ∈ mcp-backend-net(PAT 仅在后端网)" || bad "github-mcp 不在 mcp-backend-net: $GH_NETS"
echo "$WK_NETS" | grep -qw "mcp-backend-net" && bad "探针 worker 竟在后端网: $WK_NETS" || ok "真实 worker($PROBE_SRC)∉ mcp-backend-net(无后端直连权)"
has "$DIRECT" "UNREACHABLE" && ok "真实 worker($PROBE_SRC)直连 github-mcp:8082 → UNREACHABLE(网络隔离,PAT 不出后端网)" || bad "直连未拒: $DIRECT"
# 反证:Gateway(mcp-backend-net)可达上游 —— 先起测试 Gateway
bash "$TOOLS/run-policy-gateway-e2e.sh" >>"$OUT" 2>&1 || { bad "测试 Gateway 起不来"; log "PASS=$PASS FAIL=$FAIL"; exit 1; }
docker cp "$TOOLS/policy-gateway/probe-tools.py" policy-gw-e2e:/tmp/probe-tools.py >/dev/null 2>&1
docker logs policy-gw-e2e 2>&1 | grep -q "upstream ready" && ok "Gateway(mcp-backend-net)可达上游(对比:仅 Gateway 能到 github-mcp)" || bad "Gateway 未确认 upstream ready"

# migrations(仅幂等调度加固;基线已在 B4a–B4c 闭合时应用)
for m in m3b_b4c1.sql m3b_b4c1_1.sql; do
  docker cp "$TOOLS/audit-db/$m" audit-pg:/tmp/$m >/dev/null
  docker exec -e PGPASSWORD="$SU_PW" audit-pg psql -U "$PG_SU" -d "$PG_DB" -v ON_ERROR_STOP=1 -f /tmp/$m >>"$OUT" 2>&1 || { bad "migration $m 失败"; log "PASS=$PASS FAIL=$FAIL"; exit 1; }
done
docker stop "$WORKER" >/dev/null 2>&1 || true   # 后续测试防主控制器干扰
cleanup_db; cleanup_fixture

# ════════════ 2. list 过滤(各角色完整集合 == policy allowlist ∩ upstream_visible;精确)════════════
log ""; log "=== 2. list 过滤:各角色完整集合精确比较 + 非 allowlist repo 拒 ==="
# 期望集合:从 policy yaml 展开 role→{classes ∪ extra_tools}
EXPECTED_JSON=$(python3 -c "
import yaml,json
p=yaml.safe_load(open('$POLICY_YAML'))
tc=p.get('tool_classes',{})
out={}
for role,cfg in p.get('roles',{}).items():
    s=set()
    for cls in cfg.get('classes',[]): s|=set(tc.get(cls,[]))
    s|=set(cfg.get('extra_tools',[]))
    out[role]=sorted(s)
print(json.dumps(out))
" 2>/dev/null)
# 各角色实际 list_tools
declare -A ROLE_TOOLS
for R in reviewer fixer verifier coordinator; do
  ROLE_TOOLS[$R]=$(GW "$R" 2>&1 | tr -d '\000' | python3 -c "import json,sys;d=json.loads(sys.stdin.read() or '[]');print(' '.join(sorted(d)))" 2>/dev/null)
done
# upstream_visible 来自**独立**的 upstream github-mcp tools/list(经 mcp-backend-net 直连 github-mcp:8082,
#   非角色返回值并集 —— 避免自证循环:若 Gateway 误藏某工具,并集法会把它从期望集合中也删掉而误通过)
UPSTREAM_TOOLS=$(docker run --rm --network mcp-backend-net mergepilot-controller:latest python3 -c "
import asyncio
from mcp.client.sse import sse_client
from mcp import ClientSession
async def main():
  async with sse_client('http://github-mcp:8082/sse') as (r,w):
    async with ClientSession(r,w) as s:
      await s.initialize()
      t=await s.list_tools()
      print(' '.join(sorted(x.name for x in t.tools)))
asyncio.run(main())
" 2>/dev/null | tr -d '\000')
logf "  独立直连 upstream(github-mcp)工具数: $(echo $UPSTREAM_TOOLS | wc -w)"
# 逐角色精确比较:Gateway 返回 == policy allowlist ∩ 独立 upstream_visible
CMP=$(python3 -c "
import json,sys
exp=json.loads('''$EXPECTED_JSON''')
actual={'reviewer':'''${ROLE_TOOLS[reviewer]}'''.split(),'fixer':'''${ROLE_TOOLS[fixer]}'''.split(),'verifier':'''${ROLE_TOOLS[verifier]}'''.split(),'coordinator':'''${ROLE_TOOLS[coordinator]}'''.split()}
ups='''$UPSTREAM_TOOLS'''.split()
ups_set=set(ups)
bad=[]
for r in ['reviewer','fixer','verifier','coordinator']:
    expected=set(exp.get(r,[])) & ups_set
    got=set(actual[r])
    if got!=expected:
        bad.append(f'{r}: missing={sorted(expected-got)} extra={sorted(got-expected)}')
print('OK' if not bad else 'MISMATCH::'+' | '.join(bad))
" 2>/dev/null)
[ "$CMP" = "OK" ] && ok "各角色 list_tools == policy allowlist ∩ 独立 upstream(精确集合比较,无自证循环)" || bad "list_tools 集合偏差: $CMP"
R_NA=$(GW reviewer --call list_pull_requests owner=evil repo=not-allowed 2>&1 | tr -d '\000')
has "$R_NA" "POLICY_DENIED" && has "$R_NA" "REPO_NOT_ALLOWED" && ok "非 allowlist repo → REPO_NOT_ALLOWED" || bad "REPO_NOT_ALLOWED 异常: $R_NA"

# ════════════ 3. 跨角色拒(ROLE_PATH_MISMATCH + TOOL_NOT_ALLOWED)════════════
log ""; log "=== 3. 跨角色拒 ==="
XROLE=$(docker run --rm --network hiclab-net -e FTOK="$FIXER_TOK" -e GW_HOST=policy-gw-e2e mergepilot-controller:latest python3 -c "
import urllib.request, urllib.error, os
req=urllib.request.Request('http://'+os.environ['GW_HOST']+':8083/coordinator/sse')
req.add_header('Authorization','Bearer '+os.environ['FTOK'])
try:
  r=urllib.request.urlopen(req, timeout=5); print('NO-401 status='+str(r.status))
except urllib.error.HTTPError as e:
  print('HTTP '+str(e.code)+' '+e.read().decode()[:160])
except Exception as e:
  print('ERR '+type(e).__name__+': '+str(e)[:80])
" 2>&1 | tr -d '\000' | tail -1)
logf "  cross-role 探针: $XROLE"
has "$XROLE" "HTTP 401" && has "$XROLE" "ROLE_PATH_MISMATCH" && ok "fixer token 上 /coordinator/sse → 401 ROLE_PATH_MISMATCH" || bad "ROLE_PATH_MISMATCH 异常: $XROLE"
FX_MERGE=$(GW fixer --call merge_pull_request owner="$E2E_OWNER" repo="$E2E_REPO" pullNumber=1 2>&1 | tr -d '\000')
has "$FX_MERGE" "POLICY_DENIED" && has "$FX_MERGE" "TOOL_NOT_ALLOWED" && ok "fixer 调 merge_pull_request → TOOL_NOT_ALLOWED" || bad "TOOL_NOT_ALLOWED 异常: $FX_MERGE"

# ════════════ 4. fixer 写约束(精确 reason code;0 GitHub 副作用)════════════
log ""; log "=== 4. fixer 写约束(精确 reason code;0 GitHub 副作用)==="
FX_DEL=$(GW fixer --call delete_file owner="$E2E_OWNER" repo="$E2E_REPO" path=x.md branch=fix/b5-del-$TS 2>&1 | tr -d '\000')
has "$FX_DEL" "POLICY_DENIED" && has "$FX_DEL" "TOOL_NOT_ALLOWED" && ok "fixer delete_file(l2)→ TOOL_NOT_ALLOWED" || bad "delete_file 异常: $FX_DEL"
FX_BR=$(GW fixer --call create_branch owner="$E2E_OWNER" repo="$E2E_REPO" branch=evil-not-fix from_branch=main 2>&1 | tr -d '\000')
has "$FX_BR" "POLICY_DENIED" && has "$FX_BR" "BRANCH_NOT_FIX_PREFIX" && ok "fixer create_branch 非 fix/ → BRANCH_NOT_FIX_PREFIX" || bad "BRANCH_NOT_FIX_PREFIX 异常: $FX_BR"
FX_MAIN=$(GW fixer --call create_or_update_file owner="$E2E_OWNER" repo="$E2E_REPO" path=b5-main-$TS.md branch=main content=x message=b5main 2>&1 | tr -d '\000')
has "$FX_MAIN" "POLICY_DENIED reason_code=BRANCH_PROTECTED" && ok "fixer 写 main → 精确 BRANCH_PROTECTED(不接受替代)" || bad "写 main reason_code 异常: $FX_MAIN"
FX_ENV=$(GW fixer --call create_or_update_file owner="$E2E_OWNER" repo="$E2E_REPO" path=.env branch=fix/b5-env-$TS content=secret message=b5env 2>&1 | tr -d '\000')
has "$FX_ENV" "POLICY_DENIED" && has "$FX_ENV" "PATH_DENIED" && ok "fixer 写 .env → PATH_DENIED(denylist)" || bad "PATH_DENIED 异常: $FX_ENV"
FX_STATE=$(GW fixer --call update_pull_request owner="$E2E_OWNER" repo="$E2E_REPO" pullNumber=1 base=develop 2>&1 | tr -d '\000')
has "$FX_STATE" "POLICY_DENIED" && has "$FX_STATE" "PR_FIELD_NOT_ALLOWED" && ok "fixer update_pr 带 base(非 title/body)→ PR_FIELD_NOT_ALLOWED" || bad "PR_FIELD_NOT_ALLOWED 异常: $FX_STATE"
EVIL_EXISTS=$(gh.exe api "repos/$(e2e_repo)/branches" --jq '.[].name' 2>/dev/null | grep -cx 'evil-not-fix')
[ "$EVIL_EXISTS" = "0" ] && ok "被拒分支 evil-not-fix 不存在(GitHub 零副作用)" || bad "被拒分支竟被创建(计数=$EVIL_EXISTS)"

# ════════════ 5. 伪造票拒(不存在 + 真实 APPROVED args 篡改)════════════
log ""; log "=== 5. 伪造票拒:不存在 ticket + 真实 APPROVED 票 args 篡改 → CLAIM_MISMATCH(0 merge)==="
FORGED=$(GW coordinator --call merge_pull_request owner="$E2E_OWNER" repo="$E2E_REPO" pullNumber=999 approval_ticket=tkt-forged-deadbeef-deadbeefdeadbeef 2>&1 | tr -d '\000')
has "$FORGED" "POLICY_DENIED" && has "$FORGED" "CLAIM_MISMATCH" && ok "不存在 ticket → CLAIM_MISMATCH(l2_claim_ticket CAS 0 行)" || bad "伪造票异常: $FORGED"
FC=$(PSQL "SELECT count(*) FROM mcp_calls WHERE ticket_id='tkt-forged-deadbeef-deadbeefdeadbeef' AND reason_code='L2_CLAIMED';")
[ "$FC" = "0" ] && ok "伪造票 0 次 L2_CLAIMED(未消耗)" || bad "伪造票竟被 claim: $FC"
# 真实 APPROVED 票 + 篡改 commit_title(→ args_hash 不匹配)
RUN5=b5-tamper-$TS; BR5=fix/$RUN5-x
PR5=$(create_fix_pr "$BR5" "tamper")
if [ -z "$PR5" ]; then bad "篡改测试: PR 建失败(显式)"; else
  TKT5=$(mk_approved "$RUN5" "$BR5" "$PR5" "tamper" "b5-legit-title")
  if [ -z "$TKT5" ]; then bad "篡改测试: 建票失败"; else
    # 用篡改的 commit_title 调 merge(canonical args_hash 与票存的不一致)
    TAMP=$(GW coordinator --call merge_pull_request owner="$E2E_OWNER" repo="$E2E_REPO" pullNumber="$PR5" approval_ticket="$TKT5" commit_title="TAMPERED-HASH" merge_method=squash 2>&1 | tr -d '\000')
    has "$TAMP" "POLICY_DENIED" && has "$TAMP" "CLAIM_MISMATCH" && ok "真实 APPROVED 票 + 篡改 args(commit_title)→ CLAIM_MISMATCH(args_hash CAS 不匹配)" || bad "篡改 args 异常: $TAMP"
    A5=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT5';")
    [ "$A5" = "APPROVED" ] && ok "篡改后票仍 APPROVED(未消耗,EXECUTING 未迁移)" || bad "篡改后票态异常: $A5(应 APPROVED)"
    CC5=$(PSQL "SELECT count(*) FROM mcp_calls WHERE ticket_id='$TKT5' AND reason_code='L2_CLAIMED';")
    [ "$CC5" = "0" ] && ok "篡改票 0 次 L2_CLAIMED(未消耗)" || bad "篡改票竟被 claim: $CC5"
    GH5=$(gh.exe pr view "$PR5" --repo "$(e2e_repo)" --json state -q '.state' 2>/dev/null)
    [ "$GH5" = "OPEN" ] && ok "fixture PR#$PR5 仍 OPEN(篡改票 0 merge,零副作用)" || bad "PR#$PR5 态异常: $GH5(应 OPEN)"
  fi
fi

# ════════════ 6. 过期/重复票拒 ════════════
log ""; log "=== 6a. 过期票拒:expires_at≤now → CLAIM_MISMATCH(票未消耗)==="
RUN6=b5-exp-$TS; BR6=fix/$RUN6-x
PR6=$(create_fix_pr "$BR6" "exp")
if [ -z "$PR6" ]; then bad "过期票: PR 建失败(显式)"; else
  TKT6=$(mk_approved "$RUN6" "$BR6" "$PR6" "exp" "b5-exp-title")
  if [ -z "$TKT6" ]; then bad "过期票: 建票失败"; else
    PSQL "UPDATE approvals SET expires_at=now()-interval '1 hour' WHERE ticket_id='$TKT6';" >/dev/null
    EXP=$(GW coordinator --call merge_pull_request owner="$E2E_OWNER" repo="$E2E_REPO" pullNumber="$PR6" approval_ticket="$TKT6" commit_title="b5-exp-title" merge_method=squash 2>&1 | tr -d '\000')
    has "$EXP" "POLICY_DENIED" && has "$EXP" "CLAIM_MISMATCH" && ok "过期 ticket → CLAIM_MISMATCH" || bad "过期票异常: $EXP"
    A6=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT6';")
    [ "$A6" = "APPROVED" ] && ok "过期票未消耗(status 仍 APPROVED)" || bad "过期票竟被消耗: $A6"
    GH6=$(gh.exe pr view "$PR6" --repo "$(e2e_repo)" --json state -q '.state' 2>/dev/null)
    [ "$GH6" = "OPEN" ] && ok "fixture PR#$PR6 仍 OPEN(过期票 0 merge)" || bad "PR#$PR6 态异常: $GH6(应 OPEN)"
  fi
fi

log ""; log "=== 6b. 重复票拒:USED 票再 claim → CLAIM_MISMATCH ==="
RUN6b=b5-dup-$TS; BR6b=fix/$RUN6b-x
PR6b=$(create_fix_pr "$BR6b" "dup")
if [ -z "$PR6b" ]; then bad "重复票: PR 建失败(显式)"; else
  TKT6b=$(mk_approved "$RUN6b" "$BR6b" "$PR6b" "dup" "b5-dup-title")
  if [ -z "$TKT6b" ]; then bad "重复票: 建票失败"; else
    PSQL "UPDATE approvals SET status='USED', execution_id=gen_random_uuid(), used_at=now(), result_sha='deadbeefcafebabe000000000000000000000000' WHERE ticket_id='$TKT6b';" >/dev/null
    DUP=$(GW coordinator --call merge_pull_request owner="$E2E_OWNER" repo="$E2E_REPO" pullNumber="$PR6b" approval_ticket="$TKT6b" commit_title="b5-dup-title" merge_method=squash 2>&1 | tr -d '\000')
    has "$DUP" "POLICY_DENIED" && has "$DUP" "CLAIM_MISMATCH" && ok "USED ticket 再 claim → CLAIM_MISMATCH(status!=APPROVED)" || bad "重复票异常: $DUP"
    GH6b=$(gh.exe pr view "$PR6b" --repo "$(e2e_repo)" --json state -q '.state' 2>/dev/null)
    [ "$GH6b" = "OPEN" ] && ok "fixture PR#$PR6b 仍 OPEN(重复 claim 0 merge)" || bad "PR#$PR6b 态异常: $GH6b(应 OPEN)"
  fi
fi

# ════════════ 7. 合法票只执行一次 ════════════
log ""; log "=== 7. 合法票只执行一次:APPROVED→merge→USED;再 claim → CLAIM_MISMATCH ==="
RUN7=b5-once-$TS; BR7=fix/$RUN7-x
PR7=$(create_fix_pr "$BR7" "once")
if [ -z "$PR7" ]; then bad "单次执行: PR 建失败(显式)"; else
  TKT7=$(mk_approved "$RUN7" "$BR7" "$PR7" "once" "b5-once-title")
  if [ -z "$TKT7" ]; then bad "单次执行: 建票失败"; else
    OK7=$(GW coordinator --call merge_pull_request owner="$E2E_OWNER" repo="$E2E_REPO" pullNumber="$PR7" approval_ticket="$TKT7" commit_title="b5-once-title" merge_method=squash 2>&1 | tr -d '\000')
    has "$OK7" "is_error=true" && bad "合法票首次竟被拒: $OK7" || ok "合法 APPROVED 票首次 claim → 放行(merge 执行)"
    A7=$(PSQL "SELECT status FROM approvals WHERE ticket_id='$TKT7';")
    SHA7=$(PSQL "SELECT result_sha FROM approvals WHERE ticket_id='$TKT7';")
    CC7=$(PSQL "SELECT count(*) FROM mcp_calls WHERE ticket_id='$TKT7' AND reason_code='L2_CLAIMED';")
    CMP7=$(PSQL "SELECT count(*) FROM mcp_calls WHERE ticket_id='$TKT7' AND reason_code='L2_COMPLETE';")
    GH7=$(gh.exe pr view "$PR7" --repo "$(e2e_repo)" --json state -q '.state' 2>/dev/null)
    logf "  首次: approval=$A7 sha=${SHA7:0:12} claims=$CC7 completes=$CMP7 ghPR=$GH7"
    [ "$A7" = "USED" ] && ok "approval → USED" || bad "approval 应 USED: $A7"
    [ -n "$SHA7" ] && ok "result_sha 固化(merge commit ${SHA7:0:12})" || bad "result_sha 空"
    [ "$CC7" = "1" ] && ok "恰好 1 次 L2_CLAIMED" || bad "L2_CLAIMED 异常: $CC7(应 1)"
    [ "$CMP7" = "1" ] && ok "恰好 1 次 L2_COMPLETE" || bad "L2_COMPLETE 异常: $CMP7(应 1)"
    [ "$GH7" = "MERGED" ] && ok "fixture PR#$PR7 → MERGED(真 GitHub 写,单次)" || bad "PR#$PR7 态异常: $GH7(应 MERGED)"
    RECL=$(GW coordinator --call merge_pull_request owner="$E2E_OWNER" repo="$E2E_REPO" pullNumber="$PR7" approval_ticket="$TKT7" commit_title="b5-once-title" merge_method=squash 2>&1 | tr -d '\000')
    has "$RECL" "POLICY_DENIED" && has "$RECL" "CLAIM_MISMATCH" && ok "再 claim 同一 USED 票 → CLAIM_MISMATCH(只执行一次)" || bad "再 claim 未拒: $RECL"
    CC7b=$(PSQL "SELECT count(*) FROM mcp_calls WHERE ticket_id='$TKT7' AND reason_code='L2_CLAIMED';")
    [ "$CC7b" = "1" ] && ok "再 claim 后 L2_CLAIMED 仍 =1(无第二次 merge)" || bad "L2_CLAIMED 变化: $CC7→$CC7b(应保持 1)"
  fi
fi

# 审计窗口:精确 end(所有测试动作完成后,审计检查前)
TEST_END=$(PSQL "SELECT clock_timestamp();")
logf "  TEST_END=$TEST_END (审计窗口上界,clock_timestamp)"

# ════════════ 8. 完整不可篡改审计(phase=INTENT AND decision=DENY 精确计数;DENY 无上游 RESULT)════════════
log ""; log "=== 8. 完整不可篡改审计(INTENT+DENY 精确覆盖 8 reason_code;DENY 无上游 RESULT)==="
declare -A EXPECT_RC=([REPO_NOT_ALLOWED]=1 [ROLE_PATH_MISMATCH]=1 [TOOL_NOT_ALLOWED]=2 [BRANCH_NOT_FIX_PREFIX]=1 [BRANCH_PROTECTED]=1 [PATH_DENIED]=1 [PR_FIELD_NOT_ALLOWED]=1 [CLAIM_MISMATCH]=5)
for RC in REPO_NOT_ALLOWED ROLE_PATH_MISMATCH TOOL_NOT_ALLOWED BRANCH_NOT_FIX_PREFIX BRANCH_PROTECTED PATH_DENIED PR_FIELD_NOT_ALLOWED CLAIM_MISMATCH; do
  CNT=$(PSQL "SELECT count(*) FROM mcp_calls WHERE phase='INTENT' AND decision='DENY' AND reason_code='$RC' AND ts >= '$TEST_START' AND ts <= '$TEST_END';")
  [ "$CNT" = "${EXPECT_RC[$RC]}" ] && ok "INTENT+DENY $RC = $CNT(精确=${EXPECT_RC[$RC]})" || bad "INTENT+DENY $RC = $CNT(期望精确 ${EXPECT_RC[$RC]})"
done
# DENY 总数 = 13(8 reason_code 之和;防遗漏/多余拒绝)
TOTAL_DENY=$(PSQL "SELECT count(*) FROM mcp_calls WHERE phase='INTENT' AND decision='DENY' AND ts >= '$TEST_START' AND ts <= '$TEST_END';")
[ "$TOTAL_DENY" = "13" ] && ok "窗口内 INTENT+DENY 总数 = $TOTAL_DENY(精确=13,无遗漏/无多余)" || bad "INTENT+DENY 总数 = $TOTAL_DENY(期望 13)"
# DENY 的 correlation 不得有上游 RESULT/ERROR(证明拒绝在调上游之前)
LEAK=$(PSQL "SELECT count(*) FROM mcp_calls a WHERE a.ts >= '$TEST_START' AND a.ts <= '$TEST_END' AND a.phase='INTENT' AND a.decision='DENY' AND EXISTS (SELECT 1 FROM mcp_calls b WHERE b.correlation_id=a.correlation_id AND b.phase IN ('RESULT','ERROR'));")
[ "$LEAK" = "0" ] && ok "所有 INTENT-DENY 的 correlation 无上游 RESULT/ERROR(拒绝在调上游前,0 副作用)" || bad "DENY correlation 竟有上游 RESULT: $LEAK"
# 不可篡改:UPDATE/DELETE 被触发器拦
UPD=$(PSQL_ERR "UPDATE mcp_calls SET reason_code='TAMPER' WHERE ctid IN (SELECT ctid FROM mcp_calls WHERE reason_code='CLAIM_MISMATCH' AND ts >= '$TEST_START' AND ts <= '$TEST_END' LIMIT 1);" 2>&1 | tr -d '\000' | head -1)
has "$UPD" "INSERT-only" && ok "UPDATE mcp_calls → 触发器拒(mcp_calls_immutable,超管亦不可)" || bad "UPDATE 未被拦: $UPD"
DEL=$(PSQL_ERR "DELETE FROM mcp_calls WHERE ctid IN (SELECT ctid FROM mcp_calls WHERE ts >= '$TEST_START' AND ts <= '$TEST_END' LIMIT 1);" 2>&1 | tr -d '\000' | head -1)
has "$DEL" "INSERT-only" && ok "DELETE mcp_calls → 触发器拒(mcp_calls_immutable,超管亦不可)" || bad "DELETE 未被拦: $DEL"
TAMPER=$(PSQL "SELECT count(*) FROM mcp_calls WHERE reason_code='TAMPER' AND ts >= '$TEST_START' AND ts <= '$TEST_END';")
[ "$TAMPER" = "0" ] && ok "反证:窗口内 0 行被篡改为 TAMPER(触发器有效)" || bad "检测到 $TAMPER 行被篡改(窗口内)"

# ════════════ 证据固化 ════════════
log ""; log "=== 证据固化 ==="
PSQL "SELECT reason_code, count(*) FROM mcp_calls WHERE ts >= '$TEST_START' AND ts <= '$TEST_END' GROUP BY reason_code ORDER BY reason_code;" > "$EV/audit-summary.txt" 2>/dev/null
PSQL "SELECT phase,decision,reason_code,count(*) FROM mcp_calls WHERE ts >= '$TEST_START' AND ts <= '$TEST_END' GROUP BY phase,decision,reason_code ORDER BY phase,decision,reason_code;" > "$EV/audit-intent-deny.txt" 2>/dev/null
PSQL "SELECT t.run_id,t.status AS task,a.status AS appr,a.ticket_id,a.result_sha FROM task_runs t LEFT JOIN approvals a ON a.run_id=t.run_id WHERE t.run_id LIKE 'b5-%' ORDER BY t.run_id;" > "$EV/db-snapshot.txt" 2>/dev/null
PSQL "SELECT ts,phase,caller_agent,tool,decision,reason_code,substr(error,1,40) FROM mcp_calls WHERE ts >= '$TEST_START' AND ts <= '$TEST_END' ORDER BY ts;" > "$EV/mcp-calls-window.txt" 2>/dev/null
docker logs policy-gw-e2e 2>&1 | tail -120 > "$EV/gateway-logs.txt" 2>/dev/null || true
{ echo "B5 fixture residue(本测试创建/合并的真实 PR/分支):"; gh.exe pr list --repo "$(e2e_repo)" --state all --limit 50 --json number,state,title,headRefName -q '.[]|select(.title|test("b5"))|"\(.number)\t\(.state)\t\(.title)\t\(.headRefName)"' 2>/dev/null; } > "$EV/github-residue.txt" 2>/dev/null
{ echo "worker nets: $WK_NETS"; echo "github-mcp nets: $GH_NETS"; echo "mcp-backend-net: $BACKEND_MEMBERS"; echo "direct probe: $DIRECT"; echo "cross-role probe: $XROLE"; echo "window: $TEST_START .. $TEST_END"; } > "$EV/probes.txt" 2>/dev/null

# ════════════ 凭据扫描(覆盖 script+evidence;提取真实凭证值逐个精确搜索 + 已知格式;无宽泛过滤)════════════
log ""; log "=== 凭据扫描(script+evidence;真实值逐个搜索 + 已知格式)==="
SCAN_TARGETS="$TOOLS/m3b-b5-negative.sh $EV"
# (1) 真实凭证值逐个搜索(角色 token + DB 密码 + coordinator token)
REAL_SECRETS=$(python3 -c "
import json
vals=[]
try:
  for v in json.load(open('$E2E_TOKENS_FILE')).values(): vals.append(v)
except: pass
print('\n'.join(vals))
" 2>/dev/null)
{ printf '%s\n' "$REAL_SECRETS"; [ -n "$SU_PW" ] && echo "$SU_PW"; [ -n "$APV_PW" ] && echo "$APV_PW"; [ -n "$ECOORD" ] && echo "$ECOORD"; [ -n "$FIXER_TOK" ] && echo "$FIXER_TOK"; } > /tmp/b5-secrets.$$.txt
LEAK1=0
while IFS= read -r sec; do
  [ -z "$sec" ] && continue
  if grep -rIqF "$sec" $SCAN_TARGETS 2>/dev/null; then LEAK1=1; break; fi
done < /tmp/b5-secrets.$$.txt
rm -f /tmp/b5-secrets.$$.txt
[ "$LEAK1" = "0" ] && ok "无真实凭证值泄漏(角色 token + DB 密码逐个精确搜索 script+evidence)" || bad "真实凭证值泄漏到 script/evidence"
# (2) 已知凭证格式扫描(不用 token= 宽泛过滤)
FMT=$(grep -rnoE 'ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{80}|sk-[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{20,}' $SCAN_TARGETS 2>/dev/null | head -3)
[ -z "$FMT" ] && ok "无已知凭证格式(ghp_/github_pat_/sk-/AKIA/xox)" || bad "已知凭证格式: $FMT"

# ════════════ 收尾 + 硬门 ════════════
log ""; log "=== 收尾(fixture 0 open PR / 仅 main)+ 硬门([FAIL=0] && [PASS=EXPECTED_PASS])==="
cleanup_db
cleanup_fixture
OPEN_PRS=$(gh.exe pr list --repo "$(e2e_repo)" --state open --limit 100 --json number -q '.|length' 2>/dev/null || echo "?")
BRANCHES=$(gh.exe api "repos/$(e2e_repo)/branches" --jq '[.[].name]|join(",")' 2>/dev/null || echo "?")
logf "  fixture 终态: openPRs=$OPEN_PRS branches=$BRANCHES"
[ "$OPEN_PRS" = "0" ] && ok "fixture 0 open PR(干净)" || bad "fixture open PR=$OPEN_PRS"
[ "$BRANCHES" = "main" ] && ok "fixture 仅 main(0 fix 分支)" || bad "fixture 分支残留: $BRANCHES"
sed -i "s/[[:space:]]*$//" "$EV"/*.txt "$OUT" 2>/dev/null || true   # 覆盖 .txt + negative-test.out

log ""
log "═══════════════════════════════════════════════"
log "  B5 验收: PASS=$PASS / EXPECTED=$EXPECTED_PASS  FAIL=$FAIL"
log "═══════════════════════════════════════════════"
if [ "$FAIL" -eq 0 ] && [ "$PASS" -eq "$EXPECTED_PASS" ]; then
  log "  硬门:[FAIL=0] && [PASS=$PASS=EXPECTED=$EXPECTED_PASS] → PASS"
  cp "$OUT" "$EV/negative-transcript.txt" 2>/dev/null || true   # 最终验收后再生成完整 transcript
  sed -i "s/[[:space:]]*$//" "$EV/negative-transcript.txt" 2>/dev/null || true
  exit 0
else
  log "  硬门:FAIL=$FAIL PASS=$PASS EXPECTED=$EXPECTED_PASS → FAIL(硬门未过)"
  cp "$OUT" "$EV/negative-transcript.txt" 2>/dev/null || true
  sed -i "s/[[:space:]]*$//" "$EV/negative-transcript.txt" 2>/dev/null || true
  exit 1
fi
