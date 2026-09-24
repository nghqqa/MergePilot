# -*- coding: utf-8 -*-
"""CL-08 解锁:推理参数(官方已证实)+ 隔离网关适配器下的真实链执行。

授权口径: deepseek-flash thinking 默认开启(effort=high);官方关闭参数
{"thinking":{"type":"disabled"}};本轮在隔离请求中禁用 thinking,
单请求输出上限保持 8000(未用 16000)。
新增真实请求 ≤4: 诊断≤1 + fixer≤2 + verifier≤1。
"""
import json
import os
import sys
import tempfile

REPO = r"D:\goai\MergePilot"
BIZ = r"D:\goai\r3work\case2-push\repo"
RUN = "run-gh-pr2-42ed1787-003205"
HEAD = "42ed17879becbc02e31551938afbbf689351df96"
TASK = "gh-pr2-42ed1787-review-1"
TARGET = "backend/src/interfaces/api/v1/demo_high_risk.py"
TESTF = "backend/tests/unit/test_demo_high_risk_path_traversal.py"
EVID = os.path.join(REPO, "evidence", "rpd-24h", "closure")

sys.path.insert(0, os.path.join(REPO, "tools", "iso_chain"))
sys.path.insert(0, os.path.join(REPO, "tools", "model_gateway"))
sys.path.insert(0, os.path.join(REPO, "tools", "costmeter"))
sys.path.insert(0, os.path.join(REPO, "tools", "approval"))

from budget import ModelBudget  # noqa: E402
from budget import BudgetExceeded  # noqa: E402
import dispatch as disp  # noqa: E402
import chain as chain_mod  # noqa: E402
import sandbox  # noqa: E402
import iso_gateway  # noqa: E402

_key = subprocess_key = None


def gateway_key():
    global _key
    if _key is None:
        import subprocess
        _key = subprocess.run(
            ["docker", "exec", "elemiso-worker-reviewer", "python3", "-c",
             'import json;d=json.load(open("/root/.copaw-worker/reviewer/'
             'openclaw.json"));print(d["models"]["providers"]'
             '["agentteams-gateway"]["apiKey"])'],
            capture_output=True, text=True).stdout.strip()
    return _key


def main():
    key = gateway_key()
    client = iso_gateway.IsoGatewayClient(
        base_url="http://elemiso-controller:8080/v1", api_key=key,
        disable_thinking=True)

    # 诊断(≤1): 最小提示验证 thinking 禁用生效
    diag = client.chat("deepseek-flash",
                       [{"role": "user", "content": "Reply with the single word: ok"}],
                       timeout_s=100, max_tokens=2000, temperature=0.2)
    u = diag.get("usage") or {}
    rt = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
    print("DIAG: ok=%s finish=%s reasoning_tokens=%s content=%r"
          % (diag.get("ok"), diag.get("finish_reason"), rt,
             (diag.get("content") or "")[:20]))
    if not diag.get("ok"):
        print("diagnostic failed -> stop")
        return 1

    ledger = os.path.join(EVID, "budget-ledger.json")
    budget = ModelBudget(run_id=RUN, max_requests=40, max_total_tokens=200000,
                         max_output_per_request=8000,
                         est_input_per_request=8000, ledger_path=ledger)

    pkg_dir = os.path.join(REPO, "tools", "approval")
    sys.modules.setdefault(
        "gate_ticket",
        sys.modules.get("approval_pkg.gate_ticket"))

    import types
    pkg = types.ModuleType("approval_pkg")
    pkg.__path__ = [pkg_dir]
    sys.modules["approval_pkg"] = pkg
    import importlib.util
    for n in ("approval", "store_sqlite", "gate_ticket"):
        spec = importlib.util.spec_from_file_location(
            "approval_pkg." + n, os.path.join(pkg_dir, n + ".py"))
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = "approval_pkg"
        sys.modules["approval_pkg." + n] = mod
        spec.loader.exec_module(mod)
    sys.modules.setdefault("gate_ticket",
                           sys.modules["approval_pkg.gate_ticket"])
    SQLiteTicketStore = sys.modules["approval_pkg.store_sqlite"].SQLiteTicketStore

    iso_dir = tempfile.mkdtemp(prefix="cl08-")
    store = SQLiteTicketStore(os.path.join(iso_dir, "iso-tickets.db"))
    outbox = disp.DispatchOutbox(os.path.join(iso_dir, "outbox.db"))

    def fresh_clone():
        import subprocess
        d = tempfile.mkdtemp(prefix="cl08-wt-")
        subprocess.run(["git", "clone", "--quiet", BIZ, d], check=True)
        subprocess.run(["git", "-C", d, "checkout", "--quiet", HEAD], check=True)
        return d

    ws_apply = fresh_clone()
    ev = os.path.join(EVID, "..", "case2b")
    result_text = open(os.path.join(REPO, "evidence", "rpd-24h", "case2b",
                                    "reviewer-result.md"), encoding="utf-8").read()
    audit = [json.loads(l) for l in
             open(os.path.join(REPO, "evidence", "rpd-24h", "case2b",
                               "rag-tool-spans.jsonl"), encoding="utf-8")
             if l.strip() and json.loads(l).get("ts", "") >= "2026-09-24T00:32"]
    target_code = open(os.path.join(BIZ, TARGET), encoding="utf-8").read()
    test_code = open(os.path.join(BIZ, TESTF), encoding="utf-8").read()
    standard_excerpt = (
        "org-standards/file-path-containment.md: 一切以用户输入构造的文件路径必须做"
        "包含性校验。标准模式: resolved = os.path.realpath(os.path.join(base_dir, name));"
        "若 not (resolved == base or resolved.startswith(base + os.sep)): 拒绝。"
        "非法路径参数返回 400(不回显服务器路径),文件不存在返回 404。")

    poc_cases = [
        {"name": "../outside-secret.txt", "expect": "rejected"},
        {"name": "../../../etc/hostname", "expect": "rejected"},
        {"name": "sub/../../escape.txt", "expect": "rejected"},
        {"name": "/etc/hostname", "expect": "rejected"},
    ]

    report = chain_mod.run_chain(
        store=store, outbox=outbox, budget=budget, model_client=client,
        sandbox_runner=sandbox.run_in_sandbox, workspace_clean=ws_apply,
        run={"run_id": RUN, "repo": "nghqqa/fastapi-boilerplate-demo",
             "pr": 2, "head_sha": HEAD},
        task_id=TASK, reviewer_result_text=result_text, audit_records=audit,
        target_path=TARGET, target_code=target_code, test_path=TESTF,
        test_code=test_code,
        finding_block=("- severity: HIGH; CWE-22 arbitrary file read; PoC: "
                       "name=../outside-secret.txt -> HTTP 200 "
                       "TOP-SECRET-OUTSIDE-BASE; ../../../etc/hostname -> "
                       "HTTP 200; no auth dependency"),
        standard_excerpt=standard_excerpt, poc_cases=poc_cases,
        approve_identity="operator-iso-night", tip_sha=HEAD,
        artifacts_dir=os.path.join(EVID, "artifacts2"),
        max_tokens=8000, log=print)

    out = {"report": report, "budget": budget.status(),
           "diag": {"ok": diag.get("ok"), "finish_reason": diag.get("finish_reason"),
                    "reasoning_tokens": rt}}
    with open(os.path.join(EVID, "cl08-report.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(json.dumps(out["budget"]))
    return 0 if report.get("final") == "VERIFIED" else 1


if __name__ == "__main__":
    sys.exit(main())
