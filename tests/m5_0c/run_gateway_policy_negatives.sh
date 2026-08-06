#!/usr/bin/env bash
# M5-0C gateway gate FULL negative + collision + validation suite (MergePilot-Test).
# Sections: A=client-output(3) B=client-path(2) C=RUN_KEY-charset(8) D=collision(4) E=concurrency(1)
set -uo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
ROOT_WSL="/mnt/d/goai/mergepilot-os"
source "$ROOT_WSL/tools/test-env/mp_guard.sh"
HARNESS="$ROOT_WSL/tests/m5_0c/run_gateway_policy_runtime.sh"
NEG_CLIENT="tests/m5_0c/gateway_policy_negative_client.py"
PG_IMAGE="pgvector/pgvector:pg16"

# extract final gate JSON from a file (brace-balanced)
parse_gate() {
  python3 - "$1" <<'PY'
import json,sys,os
path=sys.argv[1]
if not os.path.exists(path): print("null"); sys.exit()
t=open(path,encoding="utf-8",errors="replace").read()
i=t.rfind('"gate"'); s=t.rfind('{',0,i) if i>=0 else -1
if s<0: print("null"); sys.exit()
d=0;ins=False;esc=False
for j in range(s,len(t)):
    c=t[j]
    if ins:
        if esc:esc=False
        elif c=='\\':esc=True
        elif c=='"':ins=False
    else:
        if c=='"':ins=True
        elif c=='{':d+=1
        elif c=='}':
            d-=1
            if d==0: print(json.dumps(json.loads(t[s:j+1]))); sys.exit()
print("null")
PY
}

run_gate() { local rk="$1"; shift; RUN_OUT="/tmp/m5c-neg-$rk.json"; env "$@" M5C_RUN_KEY="$rk" bash "$HARNESS" > "$RUN_OUT" 2>/dev/null; RUN_RC=$?; }

assert_rc4() {  # $1=runkey — RUN_KEY validation: expect rc=4, all_passed=false, state=rejected, residue=0/0
  local rk="$1"
  python3 - "$RUN_OUT" "$RUN_RC" "$rk" <<'PY'
import json,sys
out,rc,rk=sys.argv[1:4]
d=json.loads(open(out,encoding="utf-8",errors="replace").read()) if __import__("os").path.exists(out) else {}
# find gate json
import os
if os.path.exists(out):
    t=open(out,encoding="utf-8",errors="replace").read()
    i=t.rfind('"gate"');s=t.rfind('{',0,i) if i>=0 else -1
    if s>=0:
        dd=0;ins=False;esc=False
        for j in range(s,len(t)):
            c=t[j]
            if ins:
                if esc:esc=False
                elif c=='\\':esc=True
                elif c=='"':ins=False
            else:
                if c=='"':ins=True
                elif c=='{':dd+=1
                elif c=='}':
                    dd-=1
                    if dd==0: d=json.loads(t[s:j+1]); break
ok=(int(rc)==4 and d.get("all_passed") is False and d.get("client_output_state")=="rejected"
    and d.get("residue",{}).get("containers")==0 and d.get("residue",{}).get("networks")==0)
print('  [%s] %s: rc=%s(exp4) all_passed=%s state=%s residue=%s' % ('OK' if ok else 'FAIL',rk,rc,d.get('all_passed'),d.get('client_output_state'),d.get('residue')))
sys.exit(0 if ok else 1)
PY
}

OK=0
echo "=== A. client-output negatives ==="
run_gate neg-rc1 M5C_CLIENT_SCRIPT="$NEG_CLIENT" M5C_NEGATIVE_MODE=rc1_true
python3 - /tmp/m5c-neg-neg-rc1.json "$RUN_RC" <<'PY'
import json,sys,os; t=open(sys.argv[1]).read(); i=t.rfind('"gate"');s=t.rfind('{',0,i)
dd=0;ins=False;esc=False
for j in range(s,len(t)):
    c=t[j]
    if ins:
        if esc:esc=False
        elif c=='\\':esc=True
        elif c=='"':ins=False
    else:
        if c=='"':ins=True
        elif c=='{':dd+=1
        elif c=='}':
            dd-=1
            if dd==0:
                d=json.loads(t[s:j+1])
                ok=int(sys.argv[2])==1 and d.get('client_rc')==1 and d.get('client_output_state')=='valid_json' and d.get('client_payload_all_passed') is True and d.get('all_passed') is False
                print('  [%s] neg-rc1: gate_rc=%s client_rc=%s state=%s payload=%s all=%s'%('OK' if ok else 'FAIL',sys.argv[2],d.get('client_rc'),d.get('client_output_state'),d.get('client_payload_all_passed'),d.get('all_passed')))
                sys.exit(0 if ok else 1)
PY
[ $? = 0 ] && OK=$((OK+1))

run_gate neg-empty M5C_CLIENT_SCRIPT="$NEG_CLIENT" M5C_NEGATIVE_MODE=empty
python3 - /tmp/m5c-neg-neg-empty.json "$RUN_RC" <<'PY'
import json,sys; t=open(sys.argv[1]).read(); i=t.rfind('"gate"');s=t.rfind('{',0,i)
dd=0;ins=False;esc=False
for j in range(s,len(t)):
    c=t[j]
    if ins:
        if esc:esc=False
        elif c=='\\':esc=True
        elif c=='"':ins=False
    else:
        if c=='"':ins=True
        elif c=='{':dd+=1
        elif c=='}':
            dd-=1
            if dd==0:
                d=json.loads(t[s:j+1])
                ok=int(sys.argv[2])==1 and d.get('client_rc')==0 and d.get('client_output_state')=='empty' and d.get('all_passed') is False
                print('  [%s] neg-empty: gate_rc=%s client_rc=%s state=%s all=%s'%('OK' if ok else 'FAIL',sys.argv[2],d.get('client_rc'),d.get('client_output_state'),d.get('all_passed')))
                sys.exit(0 if ok else 1)
PY
[ $? = 0 ] && OK=$((OK+1))

run_gate neg-badjson M5C_CLIENT_SCRIPT="$NEG_CLIENT" M5C_NEGATIVE_MODE=invalid_json
python3 - /tmp/m5c-neg-neg-badjson.json "$RUN_RC" <<'PY'
import json,sys; t=open(sys.argv[1]).read(); i=t.rfind('"gate"');s=t.rfind('{',0,i)
dd=0;ins=False;esc=False
for j in range(s,len(t)):
    c=t[j]
    if ins:
        if esc:esc=False
        elif c=='\\':esc=True
        elif c=='"':ins=False
    else:
        if c=='"':ins=True
        elif c=='{':dd+=1
        elif c=='}':
            dd-=1
            if dd==0:
                d=json.loads(t[s:j+1])
                ok=int(sys.argv[2])==1 and d.get('client_rc')==0 and d.get('client_output_state')=='invalid_json' and d.get('all_passed') is False
                print('  [%s] neg-badjson: gate_rc=%s client_rc=%s state=%s all=%s'%('OK' if ok else 'FAIL',sys.argv[2],d.get('client_rc'),d.get('client_output_state'),d.get('all_passed')))
                sys.exit(0 if ok else 1)
PY
[ $? = 0 ] && OK=$((OK+1))

echo "=== B. client-path negatives ==="
run_gate neg-abs M5C_CLIENT_SCRIPT="/tmp/not-mounted.py"; assert_rc4_inv() { python3 - "$RUN_OUT" "$RUN_RC" "$1" 3 <<'PY'
import json,sys,os
t=open(sys.argv[1]).read() if os.path.exists(sys.argv[1]) else ""
i=t.rfind('"gate"');s=t.rfind('{',0,i) if i>=0 else -1;d={}
if s>=0:
    dd=0;ins=False;esc=False
    for j in range(s,len(t)):
        c=t[j]
        if ins:
            if esc:esc=False
            elif c=='\\':esc=True
            elif c=='"':ins=False
        else:
            if c=='"':ins=True
            elif c=='{':dd+=1
            elif c=='}':
                dd-=1
                if dd==0:d=json.loads(t[s:j+1]);break
ok=int(sys.argv[2])==int(sys.argv[4]) and d.get('all_passed') is False and d.get('client_output_state')=='rejected'
print('  [%s] %s: rc=%s(exp%s) state=%s'%('OK' if ok else 'FAIL',sys.argv[3],sys.argv[2],sys.argv[4],d.get('client_output_state')))
sys.exit(0 if ok else 1)
PY
}; assert_rc4_inv neg-abs && OK=$((OK+1))
run_gate neg-trav M5C_CLIENT_SCRIPT="../escape.py"; assert_rc4_inv neg-trav && OK=$((OK+1))

echo "=== C. RUN_KEY charset validation (8 cases, expect rc=4 except valid) ==="
for badval in "" "/abc" "abc/def" "a..b" "abc def" "abc;def" "$(python3 -c 'print("x"*65)')"; do
  label=$(printf '%s' "$badval" | tr ' ;/' '___' | cut -c1-20)
  M5C_RUN_KEY="$badval" bash "$HARNESS" > /tmp/m5c-neg-rk-$label.json 2>/dev/null; RRC=$?
  python3 - "$RRC" "$label" /tmp/m5c-neg-rk-$label.json <<'PY'
import json,sys,os
rc,label,path=sys.argv[1:4]
t=open(path).read() if os.path.exists(path) else ""
i=t.rfind('"gate"');s=t.rfind('{',0,i) if i>=0 else -1;d={}
if s>=0:
    dd=0;ins=False;esc=False
    for j in range(s,len(t)):
        c=t[j]
        if ins:
            if esc:esc=False
            elif c=='\\':esc=True
            elif c=='"':ins=False
        else:
            if c=='"':ins=True
            elif c=='{':dd+=1
            elif c=='}':
                dd-=1
                if dd==0:d=json.loads(t[s:j+1]);break
ok=(int(rc)==4 and d.get('all_passed') is False and d.get('client_output_state')=='rejected')
print('  [%s] rk=%s: rc=%s(exp4) state=%s'%('OK' if ok else 'FAIL',label,rc,d.get('client_output_state')))
sys.exit(0 if ok else 1)
PY
  [ $? = 0 ] && OK=$((OK+1))
done
# valid RUN_KEY: must NOT be rc=4 (charset accepted)
run_gate neg-validrk M5C_CLIENT_SCRIPT="$NEG_CLIENT" M5C_NEGATIVE_MODE=empty
python3 - "$RUN_RC" <<'PY'
import sys; rc=int(sys.argv[1]); ok=rc!=4; print('  [%s] valid-rk: rc=%s (must NOT be 4)'%('OK' if ok else 'FAIL',rc)); sys.exit(0 if ok else 1)
PY
[ $? = 0 ] && OK=$((OK+1))

echo "=== D. collision (4 resources, dummy survives) ==="
# D1: network collision
docker network create m5c-net-collnet-test >/dev/null 2>&1
NET_BEFORE=$(docker network inspect m5c-net-collnet-test --format '{{.Id}}' 2>/dev/null)
M5C_RUN_KEY=collnet-test bash "$HARNESS" > /tmp/m5c-neg-coll-net.json 2>/dev/null; COLL_RC=$?
NET_AFTER=$(docker network inspect m5c-net-collnet-test --format '{{.Id}}' 2>/dev/null)
docker network rm m5c-net-collnet-test >/dev/null 2>&1
python3 - "$COLL_RC" "$NET_BEFORE" "$NET_AFTER" /tmp/m5c-neg-coll-net.json network <<'PY'
import json,sys,os
rc,before,after,path,ctype=sys.argv[1:6]
t=open(path).read() if os.path.exists(path) else ""
i=t.rfind('"gate"');s=t.rfind('{',0,i) if i>=0 else -1;d={}
if s>=0:
    dd=0;ins=False;esc=False
    for j in range(s,len(t)):
        c=t[j]
        if ins:
            if esc:esc=False
            elif c=='\\':esc=True
            elif c=='"':ins=False
        else:
            if c=='"':ins=True
            elif c=='{':dd+=1
            elif c=='}':
                dd-=1
                if dd==0:d=json.loads(t[s:j+1]);break
ok=(int(rc)==5 and before==after and d.get('collision_type')==ctype and d.get('all_passed') is False)
print('  [%s] coll-%s: rc=%s(exp5) before==after=%s coll_type=%s'%('OK' if ok else 'FAIL',ctype,rc,before==after,d.get('collision_type')))
sys.exit(0 if ok else 1)
PY
[ $? = 0 ] && OK=$((OK+1))

# D2-D4: container collisions (DB/GH/GW)
for suffix in pg fakegh gateway; do
  case "$suffix" in pg) ctype="container"; cname="m5c-pg-collct-test";; fakegh) ctype="container"; cname="m5c-fakegh-collct-test";; gateway) ctype="container"; cname="m5c-gateway-collct-test";; esac
  rk="collct-${suffix}"
  # rename cname per suffix
  cname="m5c-${suffix}-${rk}"
  docker create --name "$cname" "$PG_IMAGE" >/dev/null 2>&1 || true
  CID_BEFORE=$(docker inspect "$cname" --format '{{.Id}}' 2>/dev/null)
  M5C_RUN_KEY="$rk" bash "$HARNESS" > "/tmp/m5c-neg-coll-$suffix.json" 2>/dev/null; COLL_RC=$?
  CID_AFTER=$(docker inspect "$cname" --format '{{.Id}}' 2>/dev/null)
  docker rm -f "$cname" >/dev/null 2>&1
  python3 - "$COLL_RC" "$CID_BEFORE" "$CID_AFTER" "/tmp/m5c-neg-coll-$suffix.json" container "$cname" <<'PY'
import json,sys,os
rc,before,after,path,ctype,cname=sys.argv[1:7]
t=open(path).read() if os.path.exists(path) else ""
i=t.rfind('"gate"');s=t.rfind('{',0,i) if i>=0 else -1;d={}
if s>=0:
    dd=0;ins=False;esc=False
    for j in range(s,len(t)):
        c=t[j]
        if ins:
            if esc:esc=False
            elif c=='\\':esc=True
            elif c=='"':ins=False
        else:
            if c=='"':ins=True
            elif c=='{':dd+=1
            elif c=='}':
                dd-=1
                if dd==0:d=json.loads(t[s:j+1]);break
ok=(int(rc)==5 and before==after and d.get('collision_type')==ctype and d.get('all_passed') is False)
print('  [%s] coll-%s: rc=%s(exp5) before==after=%s coll_type=%s'%('OK' if ok else 'FAIL',cname,rc,before==after,d.get('collision_type')))
sys.exit(0 if ok else 1)
PY
  [ $? = 0 ] && OK=$((OK+1))
done

echo ""
echo "=== E. concurrency (2 real instances) ==="
M5C_RUN_KEY=conc-A bash "$HARNESS" > /tmp/m5c-neg-concA.json 2>/dev/null & PID_A=$!
M5C_RUN_KEY=conc-B bash "$HARNESS" > /tmp/m5c-neg-concB.json 2>/dev/null & PID_B=$!
wait "$PID_A"; RC_A=$?; wait "$PID_B"; RC_B=$?
CONC_OK=0; { [ "$RC_A" = 0 ] && [ "$RC_B" = 0 ]; } && CONC_OK=1
echo "  A: rc=$RC_A B: rc=$RC_B concurrency_ok=$CONC_OK"
[ "$CONC_OK" = 1 ] && OK=$((OK+1))

# cleanup + residue
OVERALL_C=$(docker ps -aq --filter "label=com.mergepilot.m5_0c_gate" | wc -l | tr -d ' ')
OVERALL_N=$(docker network ls -q --filter "label=com.mergepilot.m5_0c_gate" | wc -l | tr -d ' ')
rm -f /tmp/m5c-neg-*.json
TMP_RES=$(ls /tmp/m5c-neg-*.json 2>/dev/null | wc -l | tr -d ' ')
TOTAL=18  # 3+2+7+1(valid)+4+1(conc)
echo ""
echo "=== SUMMARY ==="
echo "  cases_passed=$OK/$TOTAL  overall_containers=$OVERALL_C overall_networks=$OVERALL_N temp_residue=$TMP_RES"
ALL_OK=0; [ "$OK" = "$TOTAL" ] && [ "$OVERALL_C" = 0 ] && [ "$OVERALL_N" = 0 ] && [ "$TMP_RES" = 0 ] && ALL_OK=1
echo "{\"gate\":\"m5-0c-gateway-negatives\",\"all_passed\":$([ $ALL_OK = 1 ] && echo true || echo false),\"cases_passed\":$OK,\"total\":$TOTAL,\"overall_containers\":$OVERALL_C,\"overall_networks\":$OVERALL_N,\"temp_residue\":$TMP_RES}"
exit $([ "$ALL_OK" = 1 ] && echo 0 || echo 1)
