# -*- coding: utf-8 -*-
"""CL-08:真实模型隔离链执行器(一次性;预算硬控;case-pg 只读)。

用法: python tools/iso_chain/run_isolated_chain.py
前置: reviewer 容器 env(MERGEPILOT_CR_*)已注入;网关可达;业务 clone 在
      r3work/case2-push/repo(42ed1787)。全程不输出秘密/DSN。
"""
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = r"D:\goai\MergePilot"
BIZ = r"D:\goai\r3work\case2-push\repo"
RUN = "run-gh-pr2-42ed1787-003205"
HEAD = "42ed17879becbc02e31551938afbbf689351df96"
TASK = "gh-pr2-42ed1787-review-1"
TARGET = "backend/src/interfaces/api/v1/demo_high_risk.py"
TESTF = "backend/tests/unit/test_demo_high_risk_path_traversal.py"
MODEL = "deepseek-flash"
EVID = os.path.join(REPO, "evidence", "rpd-24h", "closure")

sys.path.insert(0, os.path.join(REPO, "tools", "iso_chain"))
sys.path.insert(0, os.path.join(REPO, "tools", "model_gateway"))
sys.path.insert(0, os.path.join(REPO, "tools", "costmeter"))
sys.path.insert(0, os.path.join(REPO, "tools", "approval"))

import client as mg_client  # noqa: E402
from budget import ModelBudget  # noqa: E402
from costmeter.core import BudgetExceeded  # noqa: E402
import dispatch as disp  # noqa: E402
import chain as chain_mod  # noqa: E402
import sandbox  # noqa: E402


def _pkg():
    import types
    pkg = sys.modules.get("approval_pkg")
    if pkg is None:
        pkg = types.ModuleType("approval_pkg")
        pkg.__path__ = [os.path.join(REPO, "tools", "approval")]
        sys.modules["approval_pkg"] = pkg
    return pkg


def load(name):
    pkg = _pkg()
    full = "approval_pkg." + name
    if full in sys.modules:
        return sys.modules[full]
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        full, os.path.join(REPO, "tools", "approval", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "approval_pkg"
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


structured_gate = importlib.import_module("structured_gate") \
    if "structured_gate" in sys.modules else None

# ── 网关凭据(容器内读取,不打印值) ─────────────────────────────────────────
raw = subprocess.run(
    ["docker", "exec", "elemiso-worker-reviewer", "python3", "-c",
     'import json;d=json.load(open("/root/.copaw-worker/reviewer/openclaw.json"));'
     'p=d["models"]["providers"]["agentteams-gateway"];'
     'print(p["baseUrl"]);print(len(p["apiKey"]))'],
    capture_output=True, text=True).stdout.splitlines()
base_url, key_len = raw[0].strip(), int(raw[1])
key = subprocess.run(
    ["docker", "exec", "elemiso-worker-reviewer", "python3", "-c",
     'import json;d=json.load(open("/root/.copaw-worker/reviewer/openclaw.json"));'
     'print(d["models"]["providers"]["agentteams-gateway"]["apiKey"])'],
    capture_output=True, text=True).stdout.strip()
print("gateway:", base_url, "| key bytes:", key_len)

import container_gateway  # noqa: E402
client = container_gateway.ContainerGatewayClient(
    "elemiso-worker-reviewer", base_url, key)
budget = ModelBudget(run_id=RUN, max_requests=40, max_total_tokens=200000,
                     max_output_per_request=8000, est_input_per_request=8000,
                     ledger_path=os.path.join(EVID, "budget-ledger.json"))

# ── 隔离票据库(独立于生产 gate-tickets.db) ───────────────────────────────
iso_dir = tempfile.mkdtemp(prefix="iso-chain-")
load("gate_ticket")   # structured_gate 的回退导入需要该别名
store = load("store_sqlite").SQLiteTicketStore(os.path.join(iso_dir, "iso-tickets.db"))
outbox = disp.DispatchOutbox(os.path.join(iso_dir, "outbox.db"))

# ── 干净工作区(独立两份:apply 与 verify) ────────────────────────────────
def fresh_clone(tag):
    d = tempfile.mkdtemp(prefix="iso-wt-")
    subprocess.run(["git", "clone", "--quiet", BIZ, d], check=True)
    subprocess.run(["git", "-C", d, "checkout", "--quiet", HEAD], check=True)
    return d

ws_apply = fresh_clone(HEAD)
ws_verify = fresh_clone(HEAD)

ev = os.path.join(EVID)
result_text = open(os.path.join(ev, "..", "case2b", "reviewer-result.md"),
                   encoding="utf-8").read()
audit = [json.loads(l) for l in
         open(os.path.join(ev, "..", "case2b", "rag-tool-spans.jsonl"),
              encoding="utf-8") if l.strip()
         and json.loads(l).get("ts", "") >= "2026-09-24T00:32"]
target_code = open(os.path.join(BIZ, TARGET), encoding="utf-8").read()
test_code = open(os.path.join(BIZ, TESTF), encoding="utf-8").read()

standard_excerpt = (
    "org-standards/file-path-containment.md: 一切以用户输入构造的文件路径必须做"
    "包含性校验。标准模式: resolved = os.path.realpath(os.path.join(base_dir, name));"
    "若 not (resolved == base or resolved.startswith(base + os.sep)): 拒绝。"
    "非法路径参数返回 400(不回显服务器路径),文件不存在返回 404。"
    "demo 端点同样适用全部规范;'演示目的'不降低定级。")

poc_cases = [
    {"name": "../outside-secret.txt", "expect": "rejected"},
    {"name": "../../../etc/hostname", "expect": "rejected"},
    {"name": "sub/../../escape.txt", "expect": "rejected"},
    {"name": "/etc/hostname", "expect": "rejected"},
]

log_lines = []


def log(*a):
    line = " ".join(str(x) for x in a)
    log_lines.append(line)
    print(line)


def main():
    report = chain_mod.run_chain(
        store=store, outbox=outbox, budget=budget, model_client=client,
        sandbox_runner=sandbox.run_in_sandbox,
        workspace_clean=ws_apply, run={"run_id": RUN, "repo": "nghqqa/"
                                       "fastapi-boilerplate-demo", "pr": 2,
                                       "head_sha": HEAD},
        task_id=TASK, reviewer_result_text=result_text,
        audit_records=audit, target_path=TARGET, target_code=target_code,
        test_path=TESTF, test_code=test_code,
        finding_block=("- severity: HIGH; CWE-22 arbitrary file read; "
                       "PoC: name=../outside-secret.txt -> HTTP 200 "
                       "TOP-SECRET-OUTSIDE-BASE; ../../../etc/hostname -> "
                       "HTTP 200; no auth dependency"),
        standard_excerpt=standard_excerpt,
        poc_cases=poc_cases, approve_identity="operator-iso-night",
        tip_sha=HEAD, artifacts_dir=os.path.join(EVID, "artifacts"),
        max_tokens=4000, log=log)

    # 独立 verifier 工作区:同一补丁在第二份 checkout 独立应用,内容必须一致
    if report.get("final") == "VERIFIED":
        r = subprocess.run(["git", "apply",
                            os.path.join(EVID, "artifacts", "round1",
                                         "patch.diff")],
                           cwd=ws_verify, capture_output=True, text=True)
        print("independent apply rc:", r.returncode)
        a = open(os.path.join(ws_apply, TARGET), encoding="utf-8").read()
        b = open(os.path.join(ws_verify, TARGET), encoding="utf-8").read()
        print("independent workspaces identical:", a == b)

    out = {"report": report, "budget": budget.status()}
    with open(os.path.join(EVID, "iso-chain-report.json"), "w",
              encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    with open(os.path.join(EVID, "chain-log.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines))
    print(json.dumps(out["budget"]))
    return 0 if report.get("final") == "VERIFIED" else 1


if __name__ == "__main__":
    sys.exit(main())
