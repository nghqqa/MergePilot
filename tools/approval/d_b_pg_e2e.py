# -*- coding: utf-8 -*-
"""CL-08 后继/D-B readiness: 一次性隔离 PG 上的 E2E 生命周期试运行。

不使用共享 case-pg;凭据经环境注入,不打印。
票据生命周期: happy / reject / expire / stale / 并发单胜 / 审计。
"""
import importlib.util
import os
import sys

REPO = os.getcwd()
APPROVAL = os.path.join(REPO, "tools", "approval")
DSN = os.environ["CR_SMOKE_ADMIN"]

sys.path.insert(0, APPROVAL)

import types  # noqa: E402
pkg = types.ModuleType("approval_pkg")
pkg.__path__ = [APPROVAL]
sys.modules["approval_pkg"] = pkg


def _load(name):
    full = "approval_pkg." + name
    if full in sys.modules:
        return sys.modules[full]
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        full, os.path.join(APPROVAL, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "approval_pkg"
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_two(name, path):
    full = "approval_pkg." + name
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, path)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "approval_pkg"
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


for n in ("approval", "store_sqlite", "gate_ticket", "policy", "enforce"):
    _load(n)
pg_store = _load("pg_store")
gt = sys.modules["approval_pkg.gate_ticket"]
policy_mod = sys.modules["approval_pkg.policy"]
enforce = sys.modules["approval_pkg.enforce"]

import psycopg2  # noqa: E402

RUN = "iso-run-d-b-pg"
HEAD = "42ed17879becbc02e31551938afbbf689351df96"
REPO_NAME = "nghqqa/fastapi-boilerplate-demo"
TASK = "gh-pr2-42ed1787-review-1"
NOW = "2026-09-24T12:00:00+00:00"
MARKER = {"version": 1, "run_id": RUN, "task_id": TASK, "severity": "HIGH",
          "requested_by": "leader", "requested_at": NOW}
results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + ((" | " + detail) if detail else ""))


def main():
    # 官方迁移 runner(幂等;schema 不存在则建)
    mig = _load_two("apply_migrations", os.path.join(APPROVAL, "pg",
                                                     "apply_migrations.py"))
    mig.apply_all(DSN, [os.path.join(APPROVAL, "pg", "migrations")])

    store = pg_store.PostgreSQLTicketStore(DSN)
    policy = policy_mod.ApprovalPolicy(
        allowed_actions=frozenset({"generate_patch"}),
        approver_map={REPO_NAME: ["alice"]}, ttl_hours=24)

    # happy
    t, created, why = gt.open_gate_ticket(
        store, MARKER, RUN, REPO_NAME, HEAD, TASK, now=NOW)
    check("happy.create", t is not None and created, why)
    r = enforce.authorize_approval(store, policy, t.ticket_id, "alice",
                                   head_tip=HEAD, now=NOW, reason="ok")
    check("happy.approve", r["ok"] and r["status"] == "APPROVED", r["reason"])
    r = store.transition(t.ticket_id, "start_exec", now=NOW)
    check("happy.executing", r.ok)
    r = store.transition(t.ticket_id, "complete",
                         result_fingerprint="c" * 64, now=NOW)
    check("happy.completed", r.ok and store.get(t.ticket_id).status == "USED")

    # reject (unique run_id provides ticket uniqueness)
    rej_m = dict(MARKER, run_id=RUN + "-rej")
    t2, _, _ = gt.open_gate_ticket(
        store, rej_m, RUN + "-rej", REPO_NAME, HEAD, TASK, now=NOW)
    r = enforce.authorize_reject(store, policy, t2.ticket_id, "alice",
                                 now=NOW, reason="operator rejected")
    check("reject.cas", r["ok"] and store.get(t2.ticket_id).status == "REJECTED")

    # expire
    exp_m = dict(MARKER, run_id=RUN + "-exp", task_id=TASK)
    t3, _, _ = gt.open_gate_ticket(
        store, exp_m, RUN + "-exp", REPO_NAME, HEAD, TASK, now=NOW)
    r = enforce.authorize_approval(store, policy, t3.ticket_id, "alice",
                                   now="2026-09-30T00:00:00+00:00")
    check("expire.ttl", (not r["ok"]) and r["reason"] == "EXPIRED"
          and store.get(t3.ticket_id).status == "EXPIRED")

    # stale head
    stale_m = dict(MARKER, run_id=RUN + "-stale", task_id=TASK)
    t4, _, _ = gt.open_gate_ticket(
        store, stale_m, RUN + "-stale", REPO_NAME,
        "f" * 40, TASK, now=NOW)
    r = enforce.authorize_approval(store, policy, t4.ticket_id, "alice",
                                   head_tip="e" * 40, now=NOW)
    check("stale.head", (not r["ok"]) and r["reason"] == "STALE_HEAD")

    # concurrent approve/reject race (dual connections)
    race_m = dict(MARKER, run_id=RUN + "-race", task_id=TASK)
    t5, _, _ = gt.open_gate_ticket(
        store, race_m, RUN + "-race", REPO_NAME, HEAD, TASK, now=NOW)
    s2 = pg_store.PostgreSQLTicketStore(DSN)
    r1 = enforce.authorize_approval(store, policy, t5.ticket_id, "alice",
                                    now=NOW)
    r2 = enforce.authorize_reject(s2, policy, t5.ticket_id, "bob", now=NOW)
    check("race.single-winner", r1["ok"] != r2["ok"]
          and store.get(t5.ticket_id).status in ("APPROVED", "REJECTED"),
          "alice=%s bob=%s" % (r1["ok"], r2["ok"]))

    # 审计行
    conn = psycopg2.connect(DSN)
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM approval.ticket_audit")
    n = cur.fetchone()[0]
    conn.close()
    check("audit.present", n >= 5, "rows=%d" % n)

    store.close(); s2.close()

    fails = [r for r in results if not r[1]]
    print("\n== %d/%d passed ==" % (len(results) - len(fails), len(results)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
