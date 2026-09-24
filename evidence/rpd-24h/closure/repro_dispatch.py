# -*- coding: utf-8 -*-
"""DispatchFencing 直接复现(定位 None ticket)。"""
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

_REPO = r"D:\goai\MergePilot"
_APPROVAL_DIR = Path(_REPO) / "tools" / "approval"
sys.path.insert(0, str(_APPROVAL_DIR))
sys.path.insert(0, os.path.join(_REPO, "tools", "iso_chain"))

pkg = types.ModuleType("approval_pkg")
pkg.__path__ = [str(_APPROVAL_DIR)]
sys.modules["approval_pkg"] = pkg


def _load(name, path):
    spec = importlib.util.spec_from_file_location("approval_pkg." + name, path)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "approval_pkg"
    sys.modules["approval_pkg." + name] = mod
    spec.loader.exec_module(mod)
    return mod


_load("approval", _APPROVAL_DIR / "approval.py")
_load("store_sqlite", _APPROVAL_DIR / "store_sqlite.py")
gt = _load("gate_ticket", _APPROVAL_DIR / "gate_ticket.py")
SQLiteTicketStore = sys.modules["approval_pkg.store_sqlite"].SQLiteTicketStore
import dispatch as disp  # noqa: E402

tmp = tempfile.mkdtemp()
store = SQLiteTicketStore(os.path.join(tmp, "t.db"))
outbox = disp.DispatchOutbox(os.path.join(tmp, "o.db"))
t, created, why = gt.open_gate_ticket(
    store, {"version": 1, "severity": "HIGH", "task_id": "TASK",
            "requested_by": "leader", "requested_at": "NOW"},
    "iso-run", "o/r", "h" * 40, "TASK", now="2026-09-24T12:00:00+00:00")
print("ticket:", t.ticket_id if t else None, "| created:", created, "| why:", why)
if t:
    r1 = store.transition(t.ticket_id, "start_exec",
                          now="2026-09-24T12:00:00+00:00")
    print("start_exec:", r1.ok, r1.status, r1.reason)
    r2 = store.transition(t.ticket_id, "start_exec",
                          now="2026-09-24T12:00:01+00:00")
    print("second:", r2.ok, r2.reason)
    outbox.record_sent("dsp-1", t.ticket_id, 1, "h", "NOW")
    rep = disp.reconcile_unknown(outbox, store, lambda p: None, now="NOW")
    print("reconcile unresolved:", len(rep["unresolved"]))
