# -*- coding: utf-8 -*-
"""iso_chain.dispatch — 有效票据驱动的 fixer 派发执行器(CL-04)。

关键机制:
  * **fencing = 票据状态机**:派发前必须 store.transition(ticket, "start_exec")
    (APPROVED→EXECUTING,CAS);第二个执行者 start_exec 得
    INVALID_TRANSITION:EXECUTING——天然互斥;
  * **outbox**:每次派发先落 dispatch 记录(sqlite:SENT 状态),执行完
    回填 EXECUTED/FAILED;进程崩溃后重开,存在 SENT 记录 → 说明上次
    "发送成功但确认丢失"→ 按规则先对账(reconcile)不得直接重派;
  * **幂等**:同 (ticket_id, attempt) 只有一条派发记录;重试沿用票据
    attempt(票据 attempt_no 语义归存储层);
  * 无有效票据/非 APPROVED → 拒绝派发(DISP_NOT_EXECUTABLE)。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Callable, Dict, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dispatch_outbox (
    dispatch_id  TEXT PRIMARY KEY,
    ticket_id    TEXT NOT NULL,
    attempt      INTEGER NOT NULL,
    state        TEXT NOT NULL,           -- SENT | EXECUTED | FAILED | UNKNOWN
    payload_hash TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_dispatch
    ON dispatch_outbox (ticket_id, attempt);
"""


class DispatchOutbox:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def close(self):
        self._conn.close()

    def record_sent(self, dispatch_id: str, ticket_id: str, attempt: int,
                    payload_hash: str, now: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO dispatch_outbox (dispatch_id, ticket_id, attempt, "
                "state, payload_hash, created_at, updated_at) VALUES "
                "(?,?,?,?,?,?,?)",
                (dispatch_id, ticket_id, attempt, "SENT", payload_hash, now, now))
            self._conn.commit()

    def mark(self, dispatch_id: str, state: str, now: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE dispatch_outbox SET state=?, updated_at=? WHERE dispatch_id=?",
                (state, now, dispatch_id))
            self._conn.commit()

    def get(self, dispatch_id: str) -> Optional[Dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT dispatch_id, ticket_id, attempt, state, payload_hash, created_at, "
            "updated_at FROM dispatch_outbox WHERE dispatch_id=?", (dispatch_id,))
        row = cur.fetchone()
        if not row:
            return None
        return dict(zip(("dispatch_id", "ticket_id", "attempt", "state",
                         "payload_hash", "created_at", "updated_at"), row))

    def pending_sent(self) -> list:
        cur = self._conn.execute(
            "SELECT dispatch_id, ticket_id, attempt FROM dispatch_outbox "
            "WHERE state='SENT'")
        return [dict(zip(("dispatch_id", "ticket_id", "attempt"), r))
                for r in cur.fetchall()]


class DispatchError(Exception):
    def __init__(self, subcode: str, detail: str = ""):
        super().__init__(subcode)
        self.subcode = subcode
        self.detail = detail


def dispatch_fixer(store, outbox: DispatchOutbox, ticket_id: str,
                   executor: Callable[[Dict[str, Any]], Dict[str, Any]],
                   payload: Dict[str, Any], now: str,
                   dispatch_id: str = None) -> Dict[str, Any]:
    """票据驱动的 fixer 派发(唯一入口)。

    1. 票据 CAS:start_exec(APPROVED→EXECUTING);失败=不可派发;
    2. outbox 落 SENT 记录(崩溃时表现为"发送成功但确认丢失"的对账源);
    3. 执行 executor(payload)——真实 fixer 或隔离执行器;
    4. 回填 EXECUTED/FAILED。

    返回 {ok, dispatch_id, result?...}。"""
    import uuid
    dispatch_id = dispatch_id or ("dsp-" + uuid.uuid4().hex[:16])
    result = store.transition(ticket_id, "start_exec", now=now)
    if not result.ok:
        raise DispatchError("DISP_NOT_EXECUTABLE:%s" % result.status,
                            result.reason)
    payload_hash = __import__("hashlib").sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    ticket = store.get(ticket_id)
    outbox.record_sent(dispatch_id, ticket_id, ticket.attempt_no,
                       payload_hash, now)
    try:
        out = executor(payload)
    except Exception as e:  # noqa
        outbox.mark(dispatch_id, "FAILED", now)
        raise
    outbox.mark(dispatch_id, "EXECUTED", now)
    return {"ok": True, "dispatch_id": dispatch_id, "result": out,
            "ticket_status_after": store.get(ticket_id).status}


def reconcile_unknown(outbox: DispatchOutbox, store,
                      probe: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]],
                      now: str) -> Dict[str, Any]:
    """崩溃恢复对账:SENT 记录 → probe 查询真实执行状态。

    probe 返回非 None = 执行结果找到了(回填并返回);None = 仍未确认,
    调用方必须保持 UNKNOWN,不得直接重派。"""
    report = {"pending": outbox.pending_sent(), "resolved": []}
    for p in report["pending"]:
        found = probe(p)
        if found:
            outbox.mark(p["dispatch_id"], "EXECUTED", now)
            report["resolved"].append(p["dispatch_id"])
    report["unresolved"] = [p for p in report["pending"]
                            if p["dispatch_id"] not in report["resolved"]]
    return report
