"""console_pg — 正式只读 HTTP 查询薄适配层(PG RunStore → console 契约)。

**定位**:
- 正式产品入口是 console/(前端);本包是其**后端只读查询适配层**,
  使前端不直接连接 PostgreSQL;
- console_v3(:4190, SQLite)仅作诊断兼容入口,与本服务并存但数据源不同;
- 遵循 console 0.1.0 + API-EXTENSIONS-KB-RUN v2 契约形状:
  data_mode 必带 / 错误={error:{code,reason,message}} / repo 走查询参数 /
  空值=null / 查询失败≠空集合 / 写方法 405。

**边界(如实声明)**:
- 认证未实现:仅绑定 127.0.0.1、显式 --dsn 指向隔离 fixture 库;
  GET /api/auth/session 如实返回 401(not_authenticated),不伪造登录态;
- data_mode 恒 "fixture"(隔离 PG 测试数据),不冒充 live/snapshot;
- findings/validations/knowledge manifest 尚未落库 → 如实返回
  "not_implemented",不伪造空的成功结果;
- GitHub 当前 head 无权威来源 → 仅返回最近记录,不伪造当前结论。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
import importlib.util  # noqa: E402
import types  # noqa: E402

_ORCH_DIR = os.path.normpath(os.path.join(_HERE, "..", "orchestrator"))
_opkg = sys.modules.setdefault("orchestrator_v3",
                               types.ModuleType("orchestrator_v3"))
_opkg.__path__ = [_ORCH_DIR]
sys.modules["orchestrator_v3"] = _opkg


def _load_orch(name):
    full = "orchestrator_v3." + name
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full,
                                                  os.path.join(_ORCH_DIR,
                                                               name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


PgRunStore = _load_orch("pg_runstore").PgRunStore
StorageUnavailable = _load_orch("pg_runstore").StorageUnavailable

VERSION = "0.1.0-pg-fixture"
MAX_LIMIT = 200


def _err(code: int, reason: str, message: str) -> dict:
    return {"error": {"code": code, "reason": reason, "message": message}}


def _clamp_limit(raw, default=50) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(v, MAX_LIMIT))


def _clamp_offset(raw) -> int:
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def make_handler(store: PgRunStore):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict):
            data = json.dumps(body, ensure_ascii=False, indent=1,
                              default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _fixture(self, body: dict, code: int = 200) -> None:
            body = dict(body, data_mode="fixture")
            self._send(code, body)

        def do_GET(self):
            url = urlparse(self.path)
            path = url.path
            q = parse_qs(url.query)
            try:
                if path == "/healthz":
                    # 公开健康检查:仅 {ok,version},无私有信息(AUTH §0 豁免)
                    return self._send(200, {
                        "ok": True, "service": "mergepilot-console-pg",
                        "version": VERSION, "data_mode": "fixture",
                        "auth": "not_implemented",
                        "note": "isolated fixture service; 127.0.0.1 only"})
                if path == "/api/auth/session":
                    # 正式会话入口:认证未实现 → 如实 401(not_authenticated)
                    # (契约 API-AUTH-MERGE-V0 §1:未登录 401 JSON,服务端不 302)
                    return self._send(401, _err(
                        401, "not_authenticated",
                        "auth not implemented in fixture service"))
                if path == "/api/me/capabilities":
                    # 能力查询要求有效会话(契约 §1.3);无认证一律 401
                    return self._send(401, _err(
                        401, "not_authenticated", "capabilities require session"))
                if path == "/api/repos":
                    return self._fixture({"items": store.list_repos()})
                if path == "/api/prs":
                    repo = (q.get("repo") or [""])[0]
                    if not repo:
                        return self._send(400, _err(
                            400, "repo_required",
                            "repo query param required (?repo=owner%2Fname)"))
                    page = store.list_prs(
                        repo, _clamp_limit(q.get("limit", ["50"])[0]),
                        _clamp_offset(q.get("offset", ["0"])[0]))
                    return self._fixture(dict(page, repo=repo))
                if path == "/api/runs":
                    repo = (q.get("repo") or [""])[0]
                    pr_raw = (q.get("pr") or [""])[0]
                    limit = _clamp_limit(q.get("limit", ["50"])[0])
                    offset = _clamp_offset(q.get("offset", ["0"])[0])
                    if repo:
                        runs = _runs_by_repo(store, repo)
                    else:
                        runs = _all_runs(store)
                    if pr_raw.isdigit():
                        runs = [r for r in runs
                                if r["pr_number"] == int(pr_raw)]
                    total = len(runs)
                    items = [_run_record(r) for r in
                             runs[offset:offset + limit]]
                    return self._fixture({
                        "generated_at": _now_iso(), "total": total,
                        "limit": limit, "offset": offset, "items": items})
                if path.startswith("/api/runs/"):
                    run_id = path[len("/api/runs/"):].strip("/")
                    run = store.get_run(run_id)
                    if run is None:
                        return self._send(404, _err(
                            404, "run_not_found", "unknown run: " + run_id))
                    stage_rows = store.get_stages(run_id)
                    detail = _run_detail(store, run, stage_rows)
                    return self._fixture(detail)
                return self._send(404, _err(404, "NOT_FOUND", "unknown path"))
            except StorageUnavailable as e:
                # 查询失败 ≠ 空集合:如实 503,不回退历史快照
                return self._send(503, _err(
                    503, "backend_unavailable", str(e)[:160]))
            except Exception as e:                       # pragma: no cover
                return self._send(500, _err(
                    500, "internal", type(e).__name__ + ":" + str(e)[:160]))

        def do_POST(self):
            self._send(405, _err(
                405, "read_only", "this service exposes read endpoints only"))

        do_PUT = do_PATCH = do_DELETE = do_POST

        def log_message(self, fmt, *args):
            pass

    return Handler


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _all_runs(store: PgRunStore):
    """全仓库 run 列表(console /api/runs 无 repo 过滤时的回退面)。"""
    out = []
    cur = store._conn.cursor()
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


def _runs_by_repo(store: PgRunStore, repo: str):
    cur = store._conn.cursor()
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


def _run_record(r: dict) -> dict:
    """console /api/runs 的 RunRecord 形状(PG 版);
    契约要求缺失字段一律 null,不伪造 verdict/tasks/publish。"""
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
        # 以下 PG RunStore 尚未落库:如实 null,不伪造
        "pr_title": None, "verdict": None, "review": None,
        "publish": None, "tasks": None, "rag": None, "usage": None,
        "created_at": str(r.get("created_at") or ""),
        "updated_at": str(r.get("updated_at") or ""),
    }


def _run_detail(store: PgRunStore, run: dict, stage_rows: dict) -> dict:
    """run 详情:run 字段 + 阶段 + 事件 + 证据关联;缺失能力如实标注。"""
    events = store.recent_events(run["run_id"], limit=50)
    return {
        "run_id": run["run_id"],
        "repo": run["repo_id"], "pr_number": run["pr_number"],
        "head_sha": run["head_sha"], "base_sha": run.get("base_sha"),
        "target_id": run["target_id"],
        "chain": run["chain"], "run_class": run["run_class"],
        "mode": run["mode"], "status": run["status"],
        "risk_tier": run["risk_tier"], "outcome": run["outcome"],
        "superseded_by_run_id": run.get("superseded_by_run_id"),
        "request_key": run.get("request_key"),
        "stages": stage_rows,
        "events": events,
        "evidence": {
            "manifest_sha256": run.get("manifest_sha256"),
            "evidence_path": run.get("evidence_path"),
            "note": "MinIO 证据未接线;关联字段为空表示未记录",
        },
        "findings": "not_implemented",
        "validations": "not_implemented",
        "knowledge": "not_implemented",
        "note": ("fixture 数据:隔离 PG 测试记录,非真实 PR 流程"
                 if run.get("mode") in ("fixture", "shadow") else None),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", required=True,
                    help="隔离 fixture 库 DSN(不输出密钥;可用 env "
                         "MERGEPILOT_PG_FIXTURE_DSN)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4192)
    a = ap.parse_args(argv)
    dsn = a.dsn or os.environ.get("MERGEPILOT_PG_FIXTURE_DSN", "")
    if not dsn:
        print("ERROR: --dsn 或 MERGEPILOT_PG_FIXTURE_DSN 必须提供", flush=True)
        return 2
    store = PgRunStore(dsn)
    server = ThreadingHTTPServer((a.host, a.port), make_handler(store))
    print("console_pg (READ-ONLY, fixture) on http://%s:%d" % (a.host, a.port),
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
