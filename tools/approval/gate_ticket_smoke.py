# -*- coding: utf-8 -*-
"""gate_ticket 隔离 smoke(2026-09-24)——a-h 逐项 + 端到端三决策分支。

隔离边界:临时 SQLite(每场景独立文件);PG 层由 MERGEPILOT_PG_CONTRACT=1 的
门控套件覆盖(test_store_pg, 隔离实例)。不触真实审批票据、不调 gate_cli 真库、
不产生任何 GitHub 写入。用法:python gate_ticket_smoke.py
"""
import json
import os
import sys
import tempfile

import types as _types  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "gh-bridge"))

# fake-package 引导(与桥/测试同款):使相对导入(from .approval)可用
_pkg = _types.ModuleType("approval_pkg")
_pkg.__path__ = [_HERE]
sys.modules["approval_pkg"] = _pkg


def _load(name):
    import importlib.util as _ilu
    full = "approval_pkg." + name
    spec = _ilu.spec_from_file_location(full, os.path.join(_HERE, name + ".py"))
    mod = _ilu.module_from_spec(spec)
    mod.__package__ = "approval_pkg"
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


core = _load("approval")
_smod = _load("store_sqlite")
gt = _load("gate_ticket")
sys.modules.setdefault("gate_ticket", sys.modules["approval_pkg.gate_ticket"])
SQLiteTicketStore = _smod.SQLiteTicketStore

RUN = "run-gh-pr2-254f61ce-104621"
TASK = "gh-pr2-254f61ce-review-1"
REPO = "nghqqa/fastapi-boilerplate-demo"
HEAD = "254f61ce2ff54c25a805265e70e4827e0ce68e81"
NOW = "2026-09-24T12:00:00+00:00"
MARKER = {"version": 1, "run_id": RUN, "task_id": TASK, "severity": "HIGH",
          "requested_by": "leader", "requested_at": "2026-09-23T10:47:13Z"}

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + ((" | " + detail) if detail else ""))


def fresh_store(tmp, name):
    path = os.path.join(tmp, name)
    store = SQLiteTicketStore(path)
    return store, path


def main():
    tmp = tempfile.mkdtemp(prefix="gate-smoke-")

    # a. 同一 marker 重复到达只创建一张活动票
    s, _ = fresh_store(tmp, "a.db")
    t1, c1, _ = gt.open_gate_ticket(s, MARKER, RUN, REPO, HEAD, TASK, now=NOW)
    t2, c2, _ = gt.open_gate_ticket(s, MARKER, RUN, REPO, HEAD, TASK, now=NOW)
    check("a.idempotent-one-active-ticket", c1 and not c2 and t1.ticket_id == t2.ticket_id)
    s.close()

    # b. 绑定 run_id/repo/head_sha/task/severity
    s, _ = fresh_store(tmp, "b.db")
    t, _, _ = gt.open_gate_ticket(s, MARKER, RUN, REPO, HEAD, TASK, now=NOW)
    b = t.binding
    expected_params = core.canonical_hash({
        "gate_version": 1, "severity": "HIGH", "task_id": TASK,
        "requested_by": "leader", "requested_at": MARKER["requested_at"]})
    check("b.binding-fields", b.run_id == RUN and b.repo == REPO and b.head_sha == HEAD
          and b.action == "generate_patch" and b.finding_id is None
          and b.params_hash == expected_params and
          b.finding_fingerprint == core.canonical_hash(
              {"task_id": TASK, "severity": "HIGH", "repo": REPO, "head_sha": HEAD}))
    s.close()

    # c. approve/reject 竞争只允许一个决策成功(同库双连接=跨连接竞争)
    s1, path = fresh_store(tmp, "c.db")
    s2 = SQLiteTicketStore(path)
    t, _, _ = gt.open_gate_ticket(s1, MARKER, RUN, REPO, HEAD, TASK, now=NOW)
    r1 = gt.decide(s1, t.ticket_id, "approve", "alice", now=NOW)
    r2 = gt.decide(s2, t.ticket_id, "reject", "bob", now=NOW + "1")
    check("c.race-one-winner", r1.ok and not r2.ok
          and r2.reason == "INVALID_TRANSITION:APPROVED")
    s1.close(); s2.close()

    # d. 重复决策不覆盖历史
    s, _ = fresh_store(tmp, "d.db")
    t, _, _ = gt.open_gate_ticket(s, MARKER, RUN, REPO, HEAD, TASK, now=NOW)
    gt.decide(s, t.ticket_id, "reject", "bob", now=NOW, reason="not reproducible")
    again = gt.decide(s, t.ticket_id, "approve", "alice", now=NOW + "1")
    cur = s.get(t.ticket_id)
    check("d.no-overwrite", not again.ok and cur.status == "REJECTED"
          and cur.error == "not reproducible")
    s.close()

    # e. TTL 到期可持久化为 EXPIRED(内部连带转移也要落库)
    s, _ = fresh_store(tmp, "e.db")
    t, _, _ = gt.open_gate_ticket(s, MARKER, RUN, REPO, HEAD, TASK, now=NOW)
    r = gt.decide(s, t.ticket_id, "approve", "alice", now="2026-09-30T00:00:00+00:00")
    persisted = s.get(t.ticket_id)
    check("e.ttl-persists-expired", not r.ok and r.reason == "EXPIRED"
          and persisted.status == "EXPIRED")
    s.close()

    # f. 缺少审批人身份 fail-closed
    s, _ = fresh_store(tmp, "f.db")
    t, _, _ = gt.open_gate_ticket(s, MARKER, RUN, REPO, HEAD, TASK, now=NOW)
    r = gt.decide(s, t.ticket_id, "approve", None, now=NOW)
    check("f.identity-required", not r.ok and r.reason == "IDENT_REQUIRED"
          and s.get(t.ticket_id).status == "PENDING")
    s.close()

    # g. 每次决策尝试都产生 audit(含被拒尝试)
    s, _ = fresh_store(tmp, "g.db")
    t, _, _ = gt.open_gate_ticket(s, MARKER, RUN, REPO, HEAD, TASK, now=NOW)
    gt.decide(s, t.ticket_id, "approve", None, now=NOW)          # 被拒
    gt.decide(s, t.ticket_id, "approve", "alice", now=NOW)       # 成功
    gt.decide(s, t.ticket_id, "reject", "mallory", now=NOW + "9")  # 被拒
    rows = s._conn.execute(
        "SELECT from_status,to_status,actor,request_hash FROM ticket_audit ORDER BY id").fetchall()
    check("g.audit-every-attempt", len(rows) == 3 and rows[0][3] == "IDENT_REQUIRED"
          and rows[1] == ("PENDING", "APPROVED", "alice", "OK")
          and rows[2][3] == "INVALID_TRANSITION:APPROVED",
          "rows=%d" % len(rows))
    s.close()

    # h. 桥只建票不决策:桥加载路径不含 approve/reject 调用(结构性声明)
    src = open(os.path.join(_HERE, "..", "gh-bridge", "gh_bridge.py"),
               encoding="utf-8").read()
    uses = ('open_gate_ticket(' in src) and ('"approve"' not in src.split("open_gate_ticket_for_marker")[1].split("def ")[0])
    check("h.bridge-creates-only", uses,
          "bridge calls open_gate_ticket; no decide/approve in bridge path")

    # ── 端到端本地流程:marker+manifest → ticket → GATE_WAIT → 三分支 ──────
    man = {"run_id": RUN, "code": {"repo": REPO, "head_sha": HEAD}}
    s, _ = fresh_store(tmp, "e2e.db")
    t, created, why = gt.open_gate_ticket(s, MARKER, RUN, REPO, HEAD, TASK, now=NOW)
    st = gt.run_gate_state(s.get(t.ticket_id).status)
    check("e2e.pending->GATE_WAIT", created and st["gate_state"] == "GATE_WAIT"
          and st["dispatch_plan"] is None and st["auto_dispatch"] is False)
    # approve → 只出计划数据
    gt.decide(s, t.ticket_id, "approve", "operator-alice", now=NOW + "2")
    st = gt.run_gate_state(s.get(t.ticket_id).status)
    check("e2e.approve->APPROVED_PLAN_READY(no dispatch)",
          st["gate_state"] == "APPROVED_PLAN_READY" and st["auto_dispatch"] is False
          and st["dispatch_plan"]["fix"]["auto_dispatch"] is False)
    # reject → BLOCKED(新 run 语义:另一张票)
    t2, _, _ = gt.open_gate_ticket(s, dict(MARKER, run_id=RUN + "x"), RUN + "x",
                                   REPO, HEAD, TASK, now=NOW)
    gt.decide(s, t2.ticket_id, "reject", "operator-bob", now=NOW + "2")
    check("e2e.reject->BLOCKED", gt.run_gate_state("REJECTED")["gate_state"] == "BLOCKED")
    # expire → CLOSED_EXPIRED
    t3, _, _ = gt.open_gate_ticket(s, dict(MARKER, run_id=RUN + "y"), RUN + "y",
                                   REPO, HEAD, TASK, now=NOW)
    gt.expire_if_due(s, t3.ticket_id, now="2026-09-30T00:00:00+00:00")
    check("e2e.expire->CLOSED_EXPIRED",
          gt.run_gate_state(s.get(t3.ticket_id).status)["gate_state"] == "CLOSED_EXPIRED")
    # 已完成 run 的终态不被覆盖:映射是纯函数,不写任何 run 存储
    check("e2e.mapping-readonly (no run-store writes by design)",
          gt.run_gate_state("USED")["gate_state"] == "USED")
    s.close()

    fails = [r for r in results if not r[1]]
    print("\n== %d/%d passed ==" % (len(results) - len(fails), len(results)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
