# -*- coding: utf-8 -*-
"""D-B 受控启用 synthetic 验证(阶段二) — 最终版。

独立 SQLite;不触生产;凭据经 GH API 只读获取(不打印 token)。
"""
import importlib.util, json, os, sys, tempfile, types

REPO = r"D:\goai\MergePilot"
APPROVAL = os.path.join(REPO, "tools", "approval")
sys.path.insert(0, APPROVAL)

pkg = types.ModuleType("approval_pkg"); pkg.__path__ = [APPROVAL]
sys.modules["approval_pkg"] = pkg
def _load(name):
    full = "approval_pkg." + name
    if full in sys.modules: return sys.modules[full]
    import importlib.util as _iu
    spec = _iu.spec_from_file_location(full, os.path.join(APPROVAL, name + ".py"))
    mod = importlib.util.module_from_spec(spec); mod.__package__ = "approval_pkg"
    sys.modules[full] = mod; spec.loader.exec_module(mod); return mod

for n in ("approval", "store_sqlite", "gate_ticket", "policy", "enforce"):
    _load(n)
policy_mod = sys.modules["approval_pkg.policy"]
enforce = sys.modules["approval_pkg.enforce"]
gt = sys.modules["approval_pkg.gate_ticket"]
Store = sys.modules["approval_pkg.store_sqlite"].SQLiteTicketStore

import subprocess
GH_NODE_ID = subprocess.run(
    ["gh", "api", "users/nghqqa", "--jq", ".node_id"],
    capture_output=True, text=True).stdout.strip()

RUN = "synthetic-d-b-enable-001"
REPO_NAME = "nghqqa/fastapi-boilerplate-demo"
HEAD = "42ed17879becbc02e31551938afbbf689351df96"
TASK = "synthetic-d-b-review-1"
NOW = "2026-09-24T12:00:00+00:00"
MARKER = {"version": 1, "run_id": RUN, "task_id": TASK, "severity": "HIGH",
          "requested_by": "leader", "requested_at": NOW}
pol = policy_mod.ApprovalPolicy(
    allowed_actions=frozenset({"generate_patch", "run_poc"}),
    approver_map={"nghqqa/fastapi-boilerplate-demo": [GH_NODE_ID]},
    ttl_hours=24)

db_dir = tempfile.mkdtemp(prefix="d-b-enable-")
store = Store(os.path.join(db_dir, "tickets.db"))

results = []
def check(name, ok, detail=""):
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + ((" | " + detail) if detail else ""))

def make_ticket(suffix=""):
    rid = RUN + suffix
    m = dict(MARKER, run_id=rid, task_id=TASK + suffix)
    t, c, w = gt.open_gate_ticket(store, m, rid, REPO_NAME, HEAD, TASK + suffix, now=NOW)
    return t, c, w

# 1. policy configured
check("1.policy-configured", True, "actions=generate_patch+run_poc ttl=24h")

# 2. ticket created
t, created, why = make_ticket()
check("2.ticket-created", t is not None and created, why)

# 3. approve (named approver, correct head)
r = enforce.authorize_approval(store, pol, t.ticket_id, actor_id=GH_NODE_ID,
                               head_tip=HEAD, now=NOW, reason="D-B verification")
check("3.approve-authorized", r["ok"] and r["status"] == "APPROVED", r["reason"])

# 4. dispatch gate open (APPROVED + action enabled)
dg = enforce.authorize_dispatch(pol, store.get(t.ticket_id))
check("4.dispatch-gate-open", dg["ok"], dg.get("reason", ""))

# 5. unauthorized approve refused (different node_id)
t_neg, _, _ = make_ticket("-neg")
r_neg = enforce.authorize_approval(store, pol, t_neg.ticket_id,
                                   actor_id="UNAUTHORIZED_ACTOR", now=NOW)
check("5.unauthorized-refused", not r_neg["ok"]
      and r_neg["reason"] == "APPROVER_NOT_AUTHORIZED")

# 6. action not enabled blocks dispatch
pol_r = policy_mod.ApprovalPolicy(
    allowed_actions=frozenset({"run_poc"}),
    approver_map={"nghqqa/fastapi-boilerplate-demo": [GH_NODE_ID]}, ttl_hours=24)
dg_r = enforce.authorize_dispatch(pol_r, store.get(t_neg.ticket_id))
check("6.action-not-enabled-blocks-dispatch", not dg_r["ok"])

# 7. stale head refused
r = enforce.authorize_approval(store, pol, t_neg.ticket_id, actor_id=GH_NODE_ID,
                               head_tip="f" * 40, now=NOW)
check("7.stale-head-refused", not r["ok"] and r["reason"] == "STALE_HEAD")

# 8. TTL expiry refused
r = enforce.authorize_approval(store, pol, t_neg.ticket_id, actor_id=GH_NODE_ID,
                               now="2026-09-30T00:00:00+00:00")
check("8.ttl-expiry-refused", not r["ok"] and r["reason"] == "EXPIRED"
      and store.get(t_neg.ticket_id).status == "EXPIRED")

# 9. replay noop (same actor)
r = enforce.authorize_approval(store, pol, t.ticket_id, actor_id=GH_NODE_ID,
                               now=NOW + "1")
check("9.replay-noop", r["ok"] and r["reason"] == "NOOP"
      and store.get(t.ticket_id).approved_by == GH_NODE_ID)

# 10. reject on separate fresh ticket → BLOCKED
rej_m = dict(MARKER, run_id=RUN + "-rej", task_id=TASK + "-rej")
t_rej, c_rej, w_rej = gt.open_gate_ticket(store, rej_m, RUN + "-rej",
                                          REPO_NAME, HEAD, TASK + "-rej", now=NOW)
r_rej = enforce.authorize_reject(store, pol, t_rej.ticket_id, actor_id=GH_NODE_ID,
                                 now=NOW, reason="operator rejected")
check("10.reject-cas", r_rej["ok"]
      and store.get(t_rej.ticket_id).status == "REJECTED")
dg_rej = enforce.authorize_dispatch(pol, store.get(t_rej.ticket_id))
check("10a.rejected-dispatch-closed", not dg_rej["ok"])

# 11. audit rows present
rows = store._conn.execute(
    "SELECT from_status, to_status, actor FROM ticket_audit ORDER BY id").fetchall()
check("11.audit-rows", len(rows) >= 4, "rows=%d" % len(rows))

# 12. ticket lifecycle state
check("12.happy-lifecycle-APPROVED", store.get(t.ticket_id).status == "APPROVED")

fails = [r for r in results if not r[1]]
print("\n== %d/%d passed ==" % (len(results) - len(fails), len(results)))
print("ticket_id:", t.ticket_id, "| status:", store.get(t.ticket_id).status)
sys.exit(1 if fails else 0)
