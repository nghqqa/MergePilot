"""console_pg — 正式只读 HTTP 查询 + 审批决策(PG RunStore + PG TicketStore 薄适配层)。

定位: console/ 前端的后端查询+审批适配层;前端不直接连接 PostgreSQL。
认证: 生产 OAuth 未实现(D-9);支持 --allow-test-auth 启用隔离联调测试主体。
边界: data_mode 恒 "fixture";shadow≠真实执行;PARTIAL≠成功。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "0.2.0-pg-approval"
MAX_LIMIT = 200


def _err(code, reason, message):
    return {"error": {"code": code, "reason": reason, "message": message}}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _clamp(raw, default, lo=1, hi=200):
    try:
        return max(lo, min(int(raw), hi))
    except (TypeError, ValueError):
        return default


def _clamp_offset(raw):
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


_binding_cls = None


def _get_canonical_hash():
    global _canonical_hash_fn
    if _canonical_hash_fn is None:
        import importlib
        m = importlib.import_module("approval_pkg.approval")
        _canonical_hash_fn = m.canonical_hash
    return _canonical_hash_fn
_canonical_hash_fn = None


def _get_binding_cls():
    global _binding_cls
    if _binding_cls is None:
        # 从 approval_pkg 包上下文获取(与测试/运行时加载方式一致)
        import importlib
        m = importlib.import_module("approval_pkg.approval")
        _binding_cls = m.Binding
    return _binding_cls


def make_handler(run_store, ticket_store=None, test_auth=False, policy=None):
    """构建 HTTP handler。run_store: PgRunStore;ticket_store: PostgreSQLTicketStore 或 None。"""

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body):
            data = json.dumps(body, ensure_ascii=False, indent=1,
                              default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _fixture(self, body, code=200):
            self._send(code, dict(body, data_mode="fixture"))

        def do_GET(self):
            url = urlparse(self.path)
            path = url.path
            q = parse_qs(url.query)
            try:
                if path == "/healthz":
                    return self._send(200, {
                        "ok": True, "service": "mergepilot-console-pg",
                        "version": VERSION, "data_mode": "fixture",
                        "auth": ("test_principal" if test_auth
                                 else "not_implemented"),
                        "note": "isolated fixture; 127.0.0.1 only"})
                if path == "/api/auth/session":
                    if test_auth:
                        return self._send(200, {
                            "user": {"user_id": "test-principal",
                                     "github_login": "test-principal",
                                     "role": "operator"},
                            "expires_at": None,
                            "capabilities_version": "fixture",
                            "data_mode": "fixture",
                            "note": "test principal (isolated)"})
                    return self._send(401, _err(
                        401, "not_authenticated",
                        "OAuth not implemented; "
                        "use --allow-test-auth for isolated testing"))
                if path == "/api/me/capabilities":
                    if not test_auth:
                        return self._send(401, _err(
                            401, "not_authenticated",
                            "capabilities require session"))
                    return self._fixture({
                        "repo": (q.get("repo") or [""])[0],
                        "principal": "test-principal",
                        "operations": {
                            "approve_tickets": {"allowed": True},
                            "request_merge": {"allowed": False,
                                              "reason": "merge_disabled"},
                            "upload_documents": {"allowed": False,
                                                 "reason": "not_implemented"},
                            "manage_members": {"allowed": False,
                                               "reason": "not_implemented"}}})
                if path == "/api/repos":
                    return self._fixture({"items": run_store.list_repos()})
                if path == "/api/prs":
                    repo = (q.get("repo") or [""])[0]
                    if not repo:
                        return self._send(400, _err(
                            400, "repo_required",
                            "repo query param required (?repo=owner%2Fname)"))
                    return self._fixture(dict(
                        run_store.list_prs(
                            repo, _clamp(q.get("limit", ["50"])[0], 50, 1, 200),
                            _clamp_offset(q.get("offset", ["0"])[0])),
                        repo=repo))
                if path == "/api/runs":
                    repo = (q.get("repo") or [""])[0]
                    pr_raw = (q.get("pr") or [""])[0]
                    limit = _clamp(q.get("limit", ["50"])[0], 50, 1, 200)
                    offset = _clamp_offset(q.get("offset", ["0"])[0])
                    if repo and pr_raw.isdigit():
                        runs = run_store.runs_for_pr(repo, int(pr_raw))
                    elif repo:
                        runs = _runs_by_repo(run_store, repo)
                    else:
                        runs = _all_runs(run_store)
                    total = len(runs)
                    items = [_run_record(r) for r in runs[offset:offset + limit]]
                    return self._fixture({
                        "generated_at": _now_iso(), "total": total,
                        "limit": limit, "offset": offset, "items": items})
                if path.startswith("/api/runs/"):
                    run_id = path[len("/api/runs/"):].strip("/")
                    run = run_store.get_run(run_id)
                    if run is None:
                        return self._send(404, _err(
                            404, "run_not_found", "unknown run: " + run_id))
                    stage_rows = run_store.get_stages(run_id)
                    findings = run_store.get_findings(run_id)
                    validations = run_store.get_validations(run_id)
                    events = run_store.recent_events(run_id, limit=50)
                    return self._fixture({
                        "run_id": run_id,
                        "repo": run["repo_id"], "pr_number": run["pr_number"],
                        "head_sha": run["head_sha"], "base_sha": run.get("base_sha"),
                        "target_id": run["target_id"],
                        "chain": run["chain"], "run_class": run["run_class"],
                        "mode": run["mode"], "status": run["status"],
                        "risk_tier": run["risk_tier"], "outcome": run["outcome"],
                        "superseded_by_run_id": run.get("superseded_by_run_id"),
                        "request_key": run.get("request_key"),
                        "stages": stage_rows, "events": events,
                        "findings": findings,
                        "validations": validations,
                        "knowledge": "not_implemented",
                        "evidence": {"manifest_sha256": run.get("manifest_sha256"),
                                     "evidence_path": run.get("evidence_path"),
                                     "note": "MinIO 证据未接线"},
                        "merge_panel": {"enabled": False,
                                        "reasons": ["merge_disabled",
                                                    "real_merge_not_implemented"]},
                        "note": "fixture 数据:隔离 PG 测试记录,非真实 PR 流程",
                    })
                if path == "/api/approvals" and ticket_store:
                    return self._fixture({"items": _list_pending(ticket_store)})
                if path.startswith("/api/approvals/") and ticket_store:
                    tid = path[len("/api/approvals/"):].strip("/")
                    t = ticket_store.get(tid)
                    if t is None:
                        return self._send(404, _err(404, "ticket_not_found",
                                                    "unknown ticket: " + tid))
                    return self._fixture({"ticket_id": t.ticket_id,
                                          "status": t.status,
                                          "binding": {"run_id": t.binding.run_id,
                                                      "repo": t.binding.repo,
                                                      "head_sha": t.binding.head_sha,
                                                      "action": t.binding.action},
                                          "approved_by": t.approved_by,
                                          "attempt_no": t.attempt_no})
                if path.startswith("/api/approvals"):
                    return self._send(501, _err(
                        501, "not_implemented",
                        "approval endpoints require ticket_store; not configured"))
                return self._send(404, _err(404, "NOT_FOUND", "unknown path"))
            except Exception as conn_err:
                # DB 连接/查询失败 → 503(区别于业务 404/409)
                if isinstance(conn_err, RuntimeError):
                    return self._send(503, _err(
                        503, "backend_unavailable", str(conn_err)[:160]))
                return self._send(500, _err(
                    500, "internal", type(conn_err).__name__ + ":" + str(conn_err)[:160]))

        def do_POST(self):
            url = urlparse(self.path)
            path = url.path
            if not test_auth:
                return self._send(405, _err(
                    405, "read_only", "this service is read-only in non-test mode"))
            principal = self.headers.get("X-Test-Principal", "").strip()
            if not principal:
                return self._send(422, _err(
                    422, "principal_required", "X-Test-Principal header required"))
            if ticket_store is None:
                return self._send(501, _err(
                    501, "not_implemented", "approval store not configured"))
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8")) \
                    if length else {}
                if path == "/api/approvals":
                    return self._create_approval(body, principal)
                if "/approve" in path:
                    # path = /api/approvals/<tid>/approve → tid 是倒数第二段
                    tid = path.rstrip("/").split("/")[-2]
                    return self._transition(tid, "approve", principal)
                if "/reject" in path:
                    tid = path.rstrip("/").split("/")[-2]
                    return self._transition(tid, "reject", principal)
                return self._send(404, _err(404, "NOT_FOUND", "unknown path"))
            except json.JSONDecodeError:
                return self._send(400, _err(400, "bad_json", "invalid JSON"))
            except Exception as e:
                return self._send(500, _err(
                    500, "internal", type(e).__name__ + ":" + str(e)[:160]))

        def _create_approval(self, body, principal):
            if policy and hasattr(policy, 'allows_action') and \
               not policy.allows_action(body.get("action", "")):
                self._send(403, _err(403, "action_not_enabled",
                    "action not in D-1 enabled set"))
                return
            if policy and hasattr(policy, 'can_approve') and \
               not policy.can_approve(principal, body.get("repo", "")):
                self._send(403, _err(403, "not_an_approver",
                    "principal not in D-2 approver map"))
                return
            Binding = _get_binding_cls()
            canonical_hash = _get_canonical_hash()
            raw = body.get("params_hash") or body.get("params") or {}
            ph = raw if (isinstance(raw, str) and
                         len(raw) == 64 and
                         re.fullmatch(r'[0-9a-f]{64}', raw)) \
                else canonical_hash(raw)
            try:
                b = Binding(run_id=body["run_id"], repo=body["repo"],
                            head_sha=body["head_sha"], action=body["action"],
                            params_hash=ph,
                            patch_fingerprint=body.get("patch_fingerprint"),
                            finding_fingerprint=body.get("finding_fingerprint"),
                            finding_id=body.get("finding_id"))
            except ValueError as e:
                self._send(422, _err(422, "invalid_binding", str(e)[:200]))
                return
            t, created = ticket_store.create(
                b, attempt_no=body.get("attempt", 1),
                created_by_run=principal,
                approval_expires_at=body.get("expires_at"))
            self._send(201 if created else 200, {
                "ticket_id": t.ticket_id, "status": t.status,
                "created": created})

        def _transition(self, ticket_id, action, principal):
            import sys as _sys
            print('DEBUG transition:', ticket_id[:16], action, 'now=', _now_iso(), file=_sys.stderr)
            r = ticket_store.transition(ticket_id, action,
                                        now=_now_iso(), actor=principal)
            print('DEBUG result:', r.ok, r.status, r.reason, file=_sys.stderr)
            code = 200 if r.ok else 409
            self._send(code, {"ok": r.ok, "status": r.status, "reason": r.reason})

        def log_message(self, fmt, *args):
            pass

    return Handler


def _list_pending(ticket_store):
    conn = ticket_store._conn
    cur = conn.cursor()
    cur.execute("SELECT ticket_id, run_id, repo_id, head_sha, action, "
                "finding_id, status FROM approval.tickets "
                "WHERE status='PENDING' ORDER BY created_at")
    return [{"ticket_id": r[0], "run_id": r[1], "repo": r[2], "head_sha": r[3],
             "action": r[4], "finding_id": r[5], "status": r[6]}
            for r in cur.fetchall()]


def _runs_by_repo(store, repo):
    cur = store._ensure().cursor()
    cur.execute(
        "SELECT run_id, target_id, delivery_id, exec_seq, chain, run_class, "
        "mode, status, repo_id, pr_number, head_sha, base_sha, risk_tier, "
        "outcome, superseded_by_run_id, created_at, updated_at "
        "FROM run.runs WHERE repo_id=%s ORDER BY created_at DESC", (repo,))
    keys = ("run_id", "target_id", "delivery_id", "exec_seq", "chain",
            "run_class", "mode", "status", "repo_id", "pr_number",
            "head_sha", "base_sha", "risk_tier", "outcome",
            "superseded_by_run_id", "created_at", "updated_at")
    return [dict(zip(keys, r)) for r in cur.fetchall()]


def _all_runs(store):
    cur = store._ensure().cursor()
    cur.execute(
        "SELECT run_id, target_id, delivery_id, exec_seq, chain, run_class, "
        "mode, status, repo_id, pr_number, head_sha, base_sha, risk_tier, "
        "outcome, superseded_by_run_id, created_at, updated_at "
        "FROM run.runs ORDER BY created_at DESC")
    keys = ("run_id", "target_id", "delivery_id", "exec_seq", "chain",
            "run_class", "mode", "status", "repo_id", "pr_number",
            "head_sha", "base_sha", "risk_tier", "outcome",
            "superseded_by_run_id", "created_at", "updated_at")
    return [dict(zip(keys, r)) for r in cur.fetchall()]


def _run_record(r):
    return {
        "run_id": r["run_id"], "target_id": r.get("target_id"),
        "repo": r["repo_id"], "pr_number": r.get("pr_number"),
        "pr_url": ("https://github.com/%s/pull/%d" % (
            r["repo_id"], r["pr_number"])) if r.get("pr_number") else None,
        "head_sha": r.get("head_sha"), "base_sha": r.get("base_sha"),
        "chain": r.get("chain"), "run_class": r.get("run_class"),
        "mode": r.get("mode"), "status": r.get("status"),
        "risk_tier": r.get("risk_tier"), "outcome": r.get("outcome"),
        "superseded_by_run_id": r.get("superseded_by_run_id"),
        "pr_title": None, "verdict": None, "review": None,
        "publish": None, "tasks": None, "rag": None, "usage": None,
        "created_at": str(r.get("created_at") or ""),
        "updated_at": str(r.get("updated_at") or ""),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", required=True, help="RunStore PG DSN")
    ap.add_argument("--approval-dsn", default=None)
    ap.add_argument("--allow-test-auth", action="store_true")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4193)
    a = ap.parse_args(argv)
    # 动态导入(跨域加载由调用方负责)
    from orchestrator_v3.pg_runstore import PgRunStore
    run_store = PgRunStore(a.dsn)
    ticket_store = None
    if a.approval_dsn:
        from approval_pkg.pg_store import PostgreSQLTicketStore
        ticket_store = PostgreSQLTicketStore(a.approval_dsn)
    handler = make_handler(run_store, ticket_store, test_auth=a.allow_test_auth)
    server = ThreadingHTTPServer((a.host, a.port), handler)
    print("console_pg v%s (data_mode=fixture, auth=%s) on http://%s:%d" %
          (VERSION, "test_principal" if a.allow_test_auth else "not_implemented",
           a.host, a.port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        run_store.close()
        if ticket_store:
            ticket_store.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
