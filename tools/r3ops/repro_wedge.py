# -*- coding: utf-8 -*-
"""复现 reviewer 冷启动卡死:删重建→心跳观测→canary→消费判定;卡死即停,保留现场。"""
import json, subprocess, sys, time
sys.path.insert(0, r"D:\goai\r3work\scripts")
import matrix as mx

REV_ROOM = "!P4fWVoadLfCPTi89ZN:elemiso-matrix:6167"
REV = "@reviewer:elemiso-matrix:6167"

def sh(cmd, t=60):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=t)

def token_stat():
    r = sh(["docker", "exec", "elemiso-worker-reviewer", "sh", "-c",
            "stat -c %Y /root/.copaw-worker/reviewer/.copaw/matrix_sync_token 2>/dev/null || echo MISSING"])
    return r.stdout.strip()

def tcp_6167():
    r = sh(["docker", "exec", "elemiso-worker-reviewer", "sh", "-c",
            "grep -c ':181F' /proc/net/tcp 2>/dev/null || echo 0"])  # 6167=0x181F
    return r.stdout.strip()

def room_recent(n=6):
    r = mx.recent(REV_ROOM, n)
    return [(m['ts'], m['sender'].split(':')[0][1:], m['body'][:60]) for m in r]

CYCLES = int(sys.argv[1]) if len(sys.argv) > 1 else 8
for i in range(1, CYCLES + 1):
    print(f"\n===== CYCLE {i}/{CYCLES} {time.strftime('%H:%M:%S')} =====", flush=True)
    sh(["docker", "rm", "-f", "elemiso-worker-reviewer"])
    sh(["docker", "exec", "elemiso-ctrl", "agt", "worker", "wake", "--name", "reviewer"], t=120)
    # 等容器 + 应用起来
    for _ in range(20):
        st = sh(["docker", "inspect", "elemiso-worker-reviewer", "--format", "{{.State.Status}}"]).stdout.strip()
        if st == "running":
            break
        time.sleep(3)
    time.sleep(50)  # 启动序列(mirror/relogin/通道)
    t1 = token_stat(); c1 = tcp_6167()
    time.sleep(35)  # 一个 sync 长轮询周期
    t2 = token_stat(); c2 = tcp_6167()
    heartbeat = (t1 == t2 == "MISSING") and "TOKEN-MISSING" or ("BEATING" if t1 != t2 else ("PRESENT-STALE" if t1 != "MISSING" else "APPEARED"))
    print(f"token: {t1} -> {t2} = {heartbeat} | tcp6167: {c1}->{c2}", flush=True)
    # canary
    tag = f"canary-c{i}-{int(time.time())%100000}"
    r = mx.send(REV_ROOM, REV, f"[{tag} smoke, not a task] reply with exactly RECEIPT-OK then stop.", txn_prefix="cny")
    print(f"canary sent: {r.get('event_id','FAIL')[:18]}", flush=True)
    consumed = False
    for _ in range(12):
        time.sleep(10)
        msgs = mx.since(REV_ROOM, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time()-130)))
        if any(m['sender'].startswith('@reviewer') and 'RECEIPT-OK' in m['body'] for m in msgs):
            consumed = True; break
    print(f"canary consumed: {consumed}", flush=True)
    if not consumed or heartbeat in ("PRESENT-STALE", "TOKEN-MISSING"):
        print(">>> WEDGE REPRODUCED — preserving scene", flush=True)
        print("token:", token_stat(), "| tcp6167:", tcp_6167(), flush=True)
        print("room recent:", json.dumps(room_recent(), ensure_ascii=False, indent=1), flush=True)
        break
    print("healthy cycle", flush=True)
print("\nREPRO DONE", flush=True)
