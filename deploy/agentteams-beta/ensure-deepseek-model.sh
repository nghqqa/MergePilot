#!/bin/sh
# Wave 3.9/3.10 二B v2：deepseek-direct 模型配置 keeper（幂等、零重启）。
# 实证（3.10）：manager reconcile 只重写 MinIO 对象，worker 本地文件与运行进程
# 不受影响——故 watch 检测到 MinIO 漂移时静默重补丁即可（无需重启容器）。
# boot=入口点同步补丁+拉起 watch；watch=30s 周期，MinIO 漂移即重补丁。
ROLE="${AGENTTEAMS_WORKER_NAME}"
OBJ="agentteams/agentteams-storage/agents/$ROLE/openclaw.json"
MODE="${1:-boot}"

primary_now() {
  mc cat "$OBJ" 2>/dev/null | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin)["agents"]["defaults"]["model"].get("primary",""))
except Exception:
    print("PARSE_ERR")'
}
apply_patch() {
  mc cat "$OBJ" 2>/dev/null | python3 -c 'import json,sys,os
d=json.load(sys.stdin)
gw=d["models"]["providers"]["agentteams-gateway"]
d["models"]["providers"]["deepseek-direct"]={"api":"openai-completions","apiKey":os.environ["DEEPSEEK_API_KEY"],"baseUrl":"https://api.deepseek.com/v1","models":[dict(m) for m in gw["models"] if m.get("id")=="deepseek-chat"] or [{"id":"deepseek-chat","name":"deepseek-chat","contextWindow":64000,"maxTokens":8000,"input":["text"]}]}
d["agents"]["defaults"]["model"]["primary"]="deepseek-direct/deepseek-chat"
sys.stdout.write(json.dumps(d,ensure_ascii=False,indent=2))' | mc pipe "$OBJ"
  echo "$(date -Is) patched (silent, no restart)"
}

case "$MODE" in
  boot)
    for i in 1 2 3 4 5 6; do
      P=$(primary_now)
      [ "$P" = "PARSE_ERR" ] && { sleep 5; continue; }
      if [ -n "$P" ] && [ "$P" != "deepseek-direct/deepseek-chat" ]; then
        apply_patch
      fi
      break
    done
    [ "${KEEPER_NO_WATCH:-0}" = "1" ] || nohup /bin/sh "$0" watch >>/var/log/model-keeper.log 2>&1 &
    ;;
  watch)
    while :; do
      sleep 30
      P=$(primary_now)
      if [ -n "$P" ] && [ "$P" != "PARSE_ERR" ] && [ "$P" != "deepseek-direct/deepseek-chat" ]; then
        echo "$(date -Is) drift (primary=$P) — silent re-patch"
        apply_patch
      fi
    done
    ;;
esac
