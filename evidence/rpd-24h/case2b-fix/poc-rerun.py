# -*- coding: utf-8 -*-
"""CASE2-B 原始 PoC 独立复跑(修复后): 逃逸必须被拒,合法路径必须可用。

对照 CASE2-B 修复前行为(证据: reviewer findings.md):
  name=../outside-secret.txt    -> HTTP 200 body 含 TOP-SECRET-OUTSIDE-BASE (漏洞)
  name=../../../etc/hostname    -> HTTP 200 13 字节 (任意绝对路径读)
修复后预期: 两类逃逸均被 400 拒绝且不泄露; base 内合法文件仍 200。
"""
import asyncio
import json
import os
import sys
import tempfile

# 与原始 PoC(ghpr2g-repro)同法: 按文件路径直载目标模块(绕开包 __init__ 链)
import importlib.util
_spec = importlib.util.spec_from_file_location(
    "demo_high_risk", "/work/backend/src/interfaces/api/v1/demo_high_risk.py")
_mod = importlib.util.module_from_spec(_spec)
sys.modules["demo_high_risk"] = _mod
_spec.loader.exec_module(_mod)
demo_download = _mod.demo_download

loop = asyncio.new_event_loop()
tmp = tempfile.mkdtemp()
base = os.path.join(tmp, "demo-files-base")
os.makedirs(base, exist_ok=True)
inside = os.path.join(base, "inside.txt")
open(inside, "w").write("LEGIT-INSIDE")
_mod.DEMO_FILES_DIR = base   # 与 pytest 夹具同法: 将 base 指向临时目录
outside = os.path.join(tmp, "outside-secret.txt")
open(outside, "w").write("TOP-SECRET-OUTSIDE-BASE")

CASES = [
    {"name": "../outside-secret.txt", "expect": "rejected",
     "before": "HTTP 200, body=TOP-SECRET-OUTSIDE-BASE (leak)"},
    {"name": "../../../etc/hostname", "expect": "rejected",
     "before": "HTTP 200, body=HOSTNAME (arbitrary read)"},
    {"name": "inside.txt", "expect": "served",
     "before": "HTTP 200, body=LEGIT-INSIDE (legitimate)"},
]

results = []
for c in CASES:
    try:
        resp = loop.run_until_complete(demo_download(c["name"]))
        p = getattr(resp, "path", "")
        body = open(p).read()[:40] if p and os.path.isfile(p) else "?"
        results.append({"name": c["name"], "result":
                        "HTTP200 body=%r" % body, "expect": c["expect"]})
    except Exception as e:  # noqa
        results.append({"name": c["name"], "result":
                        "REJECTED: %s (%s)" % (type(e).__name__, str(e)[:60]),
                        "expect": c["expect"]})

met = []
for c, r in zip(CASES, results):
    if c["expect"] == "rejected":
        met.append("REJECTED" in r["result"])
    else:
        met.append("HTTP200" in r["result"] and "LEGIT-INSIDE" in r["result"])

print("POC_RESULTS:")
print(json.dumps({"cases": [
    {"name": c["name"], "expect": c["expect"],
     "before": c["before"], "after": r["result"]}
    for c, r in zip(CASES, results)],
    "all_passed": all(met)}, ensure_ascii=False, indent=1))
sys.exit(0 if all(met) else 1)
