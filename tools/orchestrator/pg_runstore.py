"""PostgreSQL RunStore — run 域最小纵向(创建 target → run → stages → events → 读模型)。

契约基线: DATA-ARCHITECTURE-PG.md @ caf6909 §3/§5.3。要点:
- targets = 逻辑目标 UNIQUE(repo,pr,head);runs = 一次执行(INSERT-only,历史不改写);
- run_id 确定性派生('gh-'+sha256(规范化JSON)[:24],含 chain/class/exec_seq);
- exec_seq 分配 = target 行锁事务内 max+1(request_key 重放返回既有 run,不重复分配);
- 每时刻每 (target,chain,class) 至多一条活跃 run(部分唯一索引兜底);
- 状态/事件同事务;legacy/v3 与 execution/evidence 身份不混淆;
- 默认运行路径不切换到 PG(适配器由 v3/受控案例显式使用)。
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
from typing import Any, Dict, Optional

import psycopg2
import psycopg2.extras

from .stages import RunStages


class StorageUnavailable(RuntimeError):
    """连接/事务基础设施失败(与业务拒绝分离)。"""


class ActiveRunExists(RuntimeError):
    """同 (target,chain,class) 已有活跃 run——重复派发被拒(非错误,业务仲裁)。"""

    def __init__(self, run_id: str):
        super().__init__("active run exists: %s" % run_id)
        self.run_id = run_id


def derive_run_id(chain: str, run_class: str, repo: str, pr: int,
                  head: str, seq: int) -> str:
    """run_id 确定性派生(设计 §3.2):规范化 JSON → sha256 前 24 hex。"""
    canon = json.dumps({"v": 1, "chain": chain, "class": run_class,
                        "repo": repo, "pr": int(pr), "head": head,
                        "seq": int(seq)},
                       sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "gh-" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:24]


def derive_target_id(repo: str, pr: int, head: str) -> str:
    """逻辑目标 ID:'tgt-' + sha256(canon(repo,pr,head))[:20](canon 风格同 §3.2)。"""
    canon = json.dumps({"repo": repo, "pr": int(pr), "head": head},
                       sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "tgt-" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:20]


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _connect(dsn: str):
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = False
        return conn
    except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
        raise StorageUnavailable("PG 连接失败: %s" % str(e)[:160]) from e


class PgRunStore:
    def __init__(self, dsn: str):
        if not dsn:
            raise ValueError("PgRunStore 需要 DSN")
        self.dsn = dsn
        self._conn = None   # 惰性连接:服务可先于 DB 就绪;中断后自动重连

    def _ensure(self):
        if self._conn is None or self._conn.closed:
            self._conn = _connect(self.dsn)
        return self._conn

    def close(self):
        if self._conn is not None:
            with contextlib.suppress(Exception):
                self._conn.close()
            self._conn = None

    # ── target ───────────────────────────────────────────────────────────
    def ensure_target(self, repo: str, pr: int, head: str,
                      base_sha: Optional[str] = None,
                      delivery_id: Optional[str] = None) -> str:
        """幂等确保 target 存在(UNIQUE(repo,pr,head) 收敛),返回 target_id。"""
        target_id = derive_target_id(repo, pr, head)
        with self._ensure():
            cur = self._ensure().cursor()
            cur.execute("INSERT INTO run.repos (repo_id) VALUES (%s) "
                        "ON CONFLICT (repo_id) DO NOTHING", (repo,))
            cur.execute(
                "INSERT INTO run.targets (target_id, repo_id, pr_number, "
                "head_sha, base_sha, first_delivery_id) VALUES (%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (repo_id, pr_number, head_sha) DO NOTHING",
                (target_id, repo, int(pr), head, base_sha, delivery_id))
            cur.execute("SELECT target_id FROM run.targets WHERE target_id=%s",
                        (target_id,))
            row = cur.fetchone()
        return row[0] if row else target_id

    # ── run 创建(分配事务,契约 §5.3 写入规则 a–g) ────────────────────────
    def create_run(self, repo: str, pr: int, head: str, chain: str,
                   run_class: str, request_key: str,
                   delivery_id: Optional[str] = None,
                   base_sha: Optional[str] = None,
                   mode: Optional[str] = None,
                   trigger_kind: str = "webhook",
                   triggered_by: Optional[str] = None,
                   risk_tier: Optional[str] = None,
                   risk_json: Optional[Dict] = None) -> Dict[str, Any]:
        """返回 (run 记录, created)。同 request_key 重放 → 既有 run(False);
        活跃 run 存在且新显式请求 → ActiveRunExists(重复派发被拒)。"""
        if chain not in ("legacy", "v3"):
            raise ValueError("chain 必须 legacy|v3")
        if run_class not in ("execution", "evidence"):
            raise ValueError("run_class 必须 execution|evidence")
        with self._ensure():
            cur = self._ensure().cursor()
            # a. 幂等确保 target(无仲裁器 DO NOTHING:任何唯一约束冲突含
            #    并发 PK 竞争都归为"行已存在",随后按已提交快照读取)
            cur.execute("INSERT INTO run.repos (repo_id) VALUES (%s) "
                        "ON CONFLICT DO NOTHING", (repo,))
            target_id = derive_target_id(repo, pr, head)
            cur.execute(
                "INSERT INTO run.targets (target_id, repo_id, pr_number, "
                "head_sha, base_sha, first_delivery_id) "
                "VALUES (%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (target_id, repo, int(pr), head, base_sha, delivery_id))
            # b. target 行锁(串行化锚点)
            cur.execute("SELECT target_id FROM run.targets "
                        "WHERE target_id=%s FOR UPDATE", (target_id,))
            # c. request_key 幂等:同触发请求重放 → 既有 run
            cur.execute(
                "SELECT run_id FROM run.runs WHERE target_id=%s AND chain=%s "
                "AND run_class=%s AND request_key=%s",
                (target_id, chain, run_class, request_key))
            row = cur.fetchone()
            if row:
                return self._load_run(cur, row[0]), False
            # d. 活跃校验:已有活跃 run → 拒绝重复派发
            cur.execute(
                "SELECT run_id FROM run.runs WHERE target_id=%s AND chain=%s "
                "AND run_class=%s AND status IN ('PENDING','RUNNING')",
                (target_id, chain, run_class))
            active = cur.fetchone()
            if active:
                raise ActiveRunExists(active[0])
            # e. 锁内分配 exec_seq + 确定性 run_id
            cur.execute("SELECT COALESCE(MAX(exec_seq),0)+1 FROM run.runs "
                        "WHERE target_id=%s AND chain=%s AND run_class=%s",
                        (target_id, chain, run_class))
            seq = cur.fetchone()[0]
            run_id = derive_run_id(chain, run_class, repo, pr, head, seq)
            try:
                cur.execute(
                    "INSERT INTO run.runs (run_id, target_id, delivery_id, exec_seq, "
                    "chain, run_class, mode, trigger_kind, triggered_by, repo_id, "
                    "pr_number, head_sha, base_sha, risk_tier, risk_json, "
                    "status, request_key) VALUES "
                    "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s)",
                    (run_id, target_id, delivery_id, seq, chain, run_class, mode,
                     trigger_kind, triggered_by, repo, int(pr), head, base_sha,
                     risk_tier, json.dumps(risk_json, ensure_ascii=False)
                     if risk_json else None, request_key))
            except psycopg2.errors.UniqueViolation:
                # 最终防线:并发漏锁时唯一约束回滚冲突方;按 run_id 或
                # request_key 收敛到既已创建的 run(不重复分配 seq)。
                self._conn.rollback()
                cur.execute("SELECT run_id FROM run.runs WHERE run_id=%s",
                            (run_id,))
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        "SELECT run_id FROM run.runs WHERE target_id=%s AND "
                        "chain=%s AND run_class=%s AND request_key=%s",
                        (target_id, chain, run_class, request_key))
                    row = cur.fetchone()
                if row:
                    return self._load_run(cur, row[0]), False
                raise
            # f. 同事务写 run_events
            cur.execute(
                "INSERT INTO run.run_events (run_id, event_type, payload) "
                "VALUES (%s,'run.created',%s)",
                (run_id, json.dumps({"request_key": request_key,
                                     "exec_seq": seq},
                                    ensure_ascii=False)))
            # g. 提交(唯一约束为最终防线)
        run = self.get_run(run_id)
        return run or {"run_id": run_id}, True

    def _load_run(self, cur, run_id: str) -> Optional[Dict[str, Any]]:
        cur.execute("SELECT run_id, target_id, delivery_id, exec_seq, chain, "
                    "run_class, mode, trigger_kind, repo_id, pr_number, "
                    "head_sha, base_sha, risk_tier, risk_json, plan_json, "
                    "outcome, coverage_missing, status, request_key, "
                    "superseded_by_run_id, created_at, updated_at, "
                    "manifest_sha256, evidence_path "
                    "FROM run.runs WHERE run_id=%s", (run_id,))
        r = cur.fetchone()
        if r is None:
            return None
        return {"run_id": r[0], "target_id": r[1], "delivery_id": r[2],
                "exec_seq": r[3], "chain": r[4], "run_class": r[5],
                "mode": r[6], "trigger_kind": r[7], "repo_id": r[8],
                "pr_number": r[9], "head_sha": r[10], "base_sha": r[11],
                "risk_tier": r[12], "risk_json": r[13], "plan_json": r[14],
                "outcome": r[15], "coverage_missing": r[16], "status": r[17],
                "request_key": r[18], "superseded_by_run_id": r[19],
                "created_at": r[20], "updated_at": r[21],
                "manifest_sha256": r[22], "evidence_path": r[23]}

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        cur = self._ensure().cursor()
        return self._load_run(cur, run_id)

    # ── 阶段状态(当前态 UPSERT;历史取证属 stage_attempts,后续包) ─────────
    def save_stages(self, run_id: str, stages: RunStages,
                    expected_status: Optional[str] = None) -> int:
        """保存维度状态快照。expected_status 给定时受期望状态约束
        (旧执行者不能覆盖新执行者)。返回受影响行数。"""
        with self._ensure():
            cur = self._ensure().cursor()
            n = 0
            for dim, rec in sorted(stages.stages.items()):
                cur.execute(
                    "INSERT INTO run.stages (run_id, stage, status, attempts, "
                    "error, detail, started_at, ended_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (run_id, stage) DO UPDATE SET "
                    "status=EXCLUDED.status, attempts=EXCLUDED.attempts, "
                    "error=EXCLUDED.error, detail=EXCLUDED.detail, "
                    "started_at=EXCLUDED.started_at, ended_at=EXCLUDED.ended_at",
                    (run_id, dim, rec.status, rec.attempts, rec.error,
                     rec.detail, rec.started_at, rec.ended_at))
                n += cur.rowcount
            if expected_status is not None:
                cur.execute(
                    "UPDATE run.runs SET updated_at=%s WHERE run_id=%s "
                    "AND status=%s", (_now_utc(), run_id, expected_status))
                if cur.rowcount == 0:
                    raise ValueError("run %s 状态已变化,拒绝覆盖" % run_id)
            cur.execute("INSERT INTO run.run_events (run_id, event_type, payload) "
                        "VALUES (%s,'stages.saved',%s)",
                        (run_id, json.dumps(
                            {d: r.status for d, r in stages.stages.items()},
                            ensure_ascii=False)))
        return n

    def append_event(self, run_id: str, event_type: str,
                     payload: Optional[Dict] = None) -> None:
        with self._ensure():
            cur = self._ensure().cursor()
            cur.execute("INSERT INTO run.run_events (run_id, event_type, payload) "
                        "VALUES (%s,%s,%s)",
                        (run_id, event_type,
                         json.dumps(payload, ensure_ascii=False) if payload else None))

    def supersede_run(self, old_run_id: str, new_run_id: str) -> bool:
        """显式重跑:旧 run 链接新 run 并标 SUPERSEDED(历史行不改写内容)。"""
        with self._ensure():
            cur = self._ensure().cursor()
            cur.execute(
                "UPDATE run.runs SET superseded_by_run_id=%s, status='SUPERSEDED', "
                "updated_at=%s WHERE run_id=%s "
                "AND status IN ('SUCCEEDED','FAILED','TIMEOUT','CANCELLED')",
                (new_run_id, _now_utc(), old_run_id))
            return cur.rowcount == 1

    def transition_status(self, run_id: str, new_status: str,
                          expected_status: Optional[str] = "PENDING") -> bool:
        """run 状态推进(期望状态守卫:旧执行者不能覆盖新执行者)。"""
        with self._ensure():
            cur = self._ensure().cursor()
            where = ("AND status=%s" if expected_status else "")
            cur.execute(
                "UPDATE run.runs SET status=%s, updated_at=%s "
                "WHERE run_id=%s" + (" AND status=%s" if expected_status else ""),
                ([new_status, _now_utc(), run_id] +
                 ([expected_status] if expected_status else [])))
            return cur.rowcount == 1

    def get_stages(self, run_id: str) -> Dict[str, Dict[str, Any]]:
        cur = self._ensure().cursor()
        cur.execute("SELECT stage, status, attempts, error, detail, "
                    "started_at, ended_at FROM run.stages WHERE run_id=%s "
                    "ORDER BY stage", (run_id,))
        return {r[0]: {"status": r[1], "attempts": r[2], "error": r[3],
                       "detail": r[4], "started_at": r[5], "ended_at": r[6]}
                for r in cur.fetchall()}

    def list_repos(self) -> list:
        """仓库列表(有 run 的仓库;含 pr/run 计数与最近活动)。"""
        cur = self._ensure().cursor()
        cur.execute(
            "SELECT repo_id, COUNT(DISTINCT pr_number), COUNT(*), "
            "MAX(updated_at) FROM run.runs GROUP BY repo_id ORDER BY repo_id")
        return [{"repo_id": r[0], "pr_count": r[1], "run_count": r[2],
                 "latest_activity": r[3]} for r in cur.fetchall()]

    def list_prs(self, repo_id: str, limit: int = 50,
                 offset: int = 0) -> Dict[str, Any]:
        """仓库内 PR 列表(分页;每 PR 汇总 run 数与最新状态)。"""
        cur = self._ensure().cursor()
        cur.execute("SELECT COUNT(DISTINCT pr_number) FROM run.runs "
                    "WHERE repo_id=%s", (repo_id,))
        total = cur.fetchone()[0]
        cur.execute(
            "SELECT pr_number, MIN(head_sha), COUNT(*), "
            "MAX(updated_at) FROM run.runs WHERE repo_id=%s "
            "GROUP BY pr_number ORDER BY MAX(updated_at) DESC "
            "LIMIT %s OFFSET %s", (repo_id, limit, offset))
        items = []
        for pr_number, head, run_count, updated in cur.fetchall():
            cur.execute(
                "SELECT status FROM run.runs WHERE repo_id=%s AND pr_number=%s "
                "ORDER BY updated_at DESC LIMIT 1", (repo_id, pr_number))
            latest_status = cur.fetchone()[0]
            items.append({"repo_id": repo_id, "pr_number": pr_number,
                          "head_sha": head, "run_count": run_count,
                          "latest_status": latest_status,
                          "latest_activity": updated})
        return {"total": total, "limit": limit, "offset": offset,
                "items": items}

    def recent_events(self, run_id: str, limit: int = 50) -> list:
        cur = self._ensure().cursor()
        cur.execute("SELECT event_type, payload, created_at FROM run.run_events "
                    "WHERE run_id=%s ORDER BY id DESC LIMIT %s",
                    (run_id, limit))
        return [{"event_type": r[0],
                 "payload": r[1] if isinstance(r[1], (dict, list))
                 else (json.loads(r[1]) if r[1] else None),
                 "created_at": str(r[2])} for r in cur.fetchall()]

    # ── findings/validations 持久化 ─────────────────────────────────────
    def save_findings(self, run_id: str, findings: List[Dict[str, Any]]) -> int:
        """批量持久化 findings(幂等:同 finding_key 覆盖)。"""
        with self._ensure():
            cur = self._conn.cursor()
            n = 0
            for f in findings:
                cur.execute(
                    "INSERT INTO run.findings (finding_id, run_id, finding_key, "
                    "source_stage, category, severity, confidence, title, "
                    "path, side, line, evidence_sha256, evidence_text, "
                    "sources_json, status, data_mode) VALUES "
                    "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (finding_id) DO UPDATE SET "
                    "severity=EXCLUDED.severity, confidence=EXCLUDED.confidence, "
                    "evidence_text=EXCLUDED.evidence_text, status=EXCLUDED.status",
                    (f["finding_id"], run_id, f["finding_key"],
                     f.get("source_stage", "review"), f.get("category"),
                     f.get("severity"), f.get("confidence"), f["title"],
                     f.get("path"), f.get("side"), f.get("line"),
                     f.get("evidence_sha256"), f.get("evidence_text"),
                     json.dumps(f.get("sources", []), ensure_ascii=False)
                     if f.get("sources") else None,
                     f.get("status", "AGGREGATED"), f.get("data_mode", "fixture")))
                n += 1
            cur.execute(
                "INSERT INTO run.run_events (run_id, event_type, payload) "
                "VALUES (%s,'findings.saved',%s)",
                (run_id, json.dumps({"count": n}, ensure_ascii=False)))
        return n

    def get_findings(self, run_id: str) -> List[Dict[str, Any]]:
        cur = self._ensure().cursor()
        cur.execute(
            "SELECT finding_id, finding_key, source_stage, category, severity, "
            "confidence, title, path, side, line, evidence_text, sources_json, "
            "status, data_mode, created_at FROM run.findings "
            "WHERE run_id=%s ORDER BY created_at", (run_id,))
        keys = ("finding_id", "finding_key", "source_stage", "category",
                "severity", "confidence", "title", "path", "side", "line",
                "evidence_text", "sources_json", "status", "data_mode",
                "created_at")
        out = []
        for r in cur.fetchall():
            d = dict(zip(keys, r))
            if d.get("sources_json") and isinstance(d["sources_json"], str):
                d["sources"] = json.loads(d.pop("sources_json"))
            elif d.get("sources_json"):
                d["sources"] = d.pop("sources_json")
            else:
                d.pop("sources_json", None)
            out.append(d)
        return out

    def save_validation(self, run_id: str, finding_id: str, verdict: str,
                        anchor_status: Optional[str] = None,
                        evidence_path: Optional[str] = None,
                        reason: Optional[str] = None) -> None:
        with self._ensure():
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO run.finding_validations (finding_id, verdict, "
                "anchor_status, evidence_path, reason, decided_at) "
                "VALUES (%s,%s,%s,%s,%s,now()) "
                "ON CONFLICT (finding_id) DO UPDATE SET "
                "verdict=EXCLUDED.verdict, anchor_status=EXCLUDED.anchor_status, "
                "evidence_path=EXCLUDED.evidence_path, reason=EXCLUDED.reason, "
                "decided_at=EXCLUDED.decided_at",
                (finding_id, verdict, anchor_status, evidence_path, reason))
            cur.execute(
                "UPDATE run.findings SET status=%s WHERE finding_id=%s",
                (verdict, finding_id))

    def get_validations(self, run_id: str) -> List[Dict[str, Any]]:
        cur = self._ensure().cursor()
        cur.execute(
            "SELECT fv.finding_id, fv.verdict, fv.anchor_status, "
            "fv.evidence_path, fv.reason, fv.decided_at "
            "FROM run.finding_validations fv "
            "JOIN run.findings f ON f.finding_id = fv.finding_id "
            "WHERE f.run_id=%s ORDER BY fv.decided_at", (run_id,))
        keys = ("finding_id", "verdict", "anchor_status", "evidence_path",
                "reason", "decided_at")
        return [dict(zip(keys, r)) for r in cur.fetchall()]

    def runs_for_pr(self, repo_id: str, pr: int) -> list:
        cur = self._ensure().cursor()
        cur.execute(
            "SELECT run_id, target_id, chain, run_class, exec_seq, mode, status, "
            "risk_tier, outcome, superseded_by_run_id, head_sha, created_at "
            "FROM run.runs WHERE repo_id=%s AND pr_number=%s "
            "ORDER BY created_at DESC", (repo_id, pr))
        keys = ("run_id", "target_id", "chain", "run_class", "exec_seq", "mode",
                "status", "risk_tier", "outcome", "superseded_by_run_id",
                "head_sha", "created_at")
        return [dict(zip(keys, r)) for r in cur.fetchall()]
