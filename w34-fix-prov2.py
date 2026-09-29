import io
p = 'deploy/agentteams-beta/provision-workers.sh'
s = io.open(p, encoding='utf-8').read()

# move phase-touch INTO the per-role loop right after takeover, keep model patch LAST
old_touch = """# 4) reconcile → phase Running（Sleeping→Running touch；幂等）
for ROLE in leader reviewer fixer verifier; do
  curl -s -m 10 -o /dev/null -X PUT -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \\
    -d '{"spec":{"state":"Sleeping"}}' "http://127.0.0.1:$PORT/api/v1/workers/mergepilot-$ROLE"
  sleep 2
  curl -s -m 10 -o /dev/null -X PUT -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \\
    -d '{"spec":{"state":"Running"}}' "http://127.0.0.1:$PORT/api/v1/workers/mergepilot-$ROLE"
  sleep 2
done
sleep 20
"""
assert s.count(old_touch) == 1, 'touch block: %d' % s.count(old_touch)
s = s.replace(old_touch, """# 4) 最终检查（touch 已在循环内完成；模型配置为最后一步——controller reconcile
#    会重写 openclaw.json，因此 patch 必须在所有 spec 变更之后）
""")

old_loop_tail = """  docker restart $CTR >/dev/null
  sleep 6
done
"""
new_loop_tail = """  # 4) reconcile（Sleeping→Running touch）——必须在模型 patch 之前（touch 触发
  #    controller 重写 openclaw.json，会覆盖 deepseek-direct 配置）
  curl -s -m 10 -o /dev/null -X PUT -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \\
    -d '{"spec":{"state":"Sleeping"}}' "http://127.0.0.1:$PORT/api/v1/workers/mergepilot-$ROLE"
  sleep 2
  curl -s -m 10 -o /dev/null -X PUT -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \\
    -d '{"spec":{"state":"Running"}}' "http://127.0.0.1:$PORT/api/v1/workers/mergepilot-$ROLE"
  sleep 2
done
sleep 15
# 5) 模型直连配置（最后统一施加；controller reconcile 覆盖后可重跑本脚本自愈）
for ROLE in leader reviewer fixer verifier; do
  CTR=$STACK-worker-mergepilot-$ROLE
"""
assert s.count(old_loop_tail) == 1, 'loop tail: %d' % s.count(old_loop_tail)
s = s.replace(old_loop_tail, new_loop_tail)

# the old step-3 block (python patch) now lives in the new loop 5): keep its body, close loop, add restart
old_patch = """  docker exec $CTR sh -c 'python3 - <<PYX
import json, subprocess, os
p = "agentteams/agentteams-storage/agents/" + os.environ["AGENTTEAMS_WORKER_NAME"] + "/openclaw.json"
raw = subprocess.run(["mc","cat",p],capture_output=True,text=True).stdout
d = json.loads(raw)
gw = d["models"]["providers"]["agentteams-gateway"]
d["models"]["providers"]["deepseek-direct"] = {
  "api": "openai-completions", "apiKey": os.environ["DEEPSEEK_API_KEY"],
  "baseUrl": "https://api.deepseek.com/v1",
  "models": [dict(m) for m in gw["models"] if m.get("id")=="deepseek-chat"] or [{"id":"deepseek-chat","name":"deepseek-chat","contextWindow":64000,"maxTokens":8000,"input":["text"]}]}
d["agents"]["defaults"]["model"]["primary"] = "deepseek-direct/deepseek-chat"
subprocess.run(["mc","pipe",p],input=json.dumps(d,ensure_ascii=False,indent=2),capture_output=True,text=True)
PYX' || { echo "model config patch failed for $NAME"; exit 1; }
  docker restart $CTR >/dev/null
  sleep 6
done
"""
# remove the ORIGINAL patch block inside role loop (now superseded) — locate by its unique prefix after takeover section
old3 = """  # 3) 模型直连配置（MinIO 凭据域 openclaw.json；reconcile 覆盖后可重跑本脚本自愈）
  docker exec $CTR sh -c 'python3 - <<PYX
import json, subprocess, os
p = "agentteams/agentteams-storage/agents/" + os.environ["AGENTTEAMS_WORKER_NAME"] + "/openclaw.json"
raw = subprocess.run(["mc","cat",p],capture_output=True,text=True).stdout
d = json.loads(raw)
gw = d["models"]["providers"]["agentteams-gateway"]
d["models"]["providers"]["deepseek-direct"] = {
  "api": "openai-completions", "apiKey": os.environ["DEEPSEEK_API_KEY"],
  "baseUrl": "https://api.deepseek.com/v1",
  "models": [dict(m) for m in gw["models"] if m.get("id")=="deepseek-chat"] or [{"id":"deepseek-chat","name":"deepseek-chat","contextWindow":64000,"maxTokens":8000,"input":["text"]}]}
d["agents"]["defaults"]["model"]["primary"] = "deepseek-direct/deepseek-chat"
subprocess.run(["mc","pipe",p],input=json.dumps(d,ensure_ascii=False,indent=2),capture_output=True,text=True)
PYX' || { echo "model config patch failed for $NAME"; exit 1; }
  docker restart $CTR >/dev/null
  sleep 6
"""
assert s.count(old3) == 1, 'old patch: %d' % s.count(old3)
s = s.replace(old3, "")

io.open(p, 'w', encoding='utf-8', newline='\n').write(s)
print('ok')
