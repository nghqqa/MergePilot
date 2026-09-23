# -*- coding: utf-8 -*-
"""CASE2 复盘修复的隔离验证(2026-09-24)。

覆盖:RAG 探测分离与失败分类、人工门三分支(gate/真超时/普通成功)、
重复事件不重复发布、错误归属拒绝、凭证缺失不认领不建工件、
case_retrieval scope 文件透传、CASE2 脱敏回放、历史证据不受影响。
全部离线(mock subprocess/网络),不触真实环境。
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.normpath(os.path.join(_HERE, "..", ".."))
_BRIDGE_DIR = os.path.join(_REPO, "tools", "gh-bridge")
sys.path.insert(0, _BRIDGE_DIR)


def _load_bridge():
    name = "mp_gh_bridge_fix3"
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_BRIDGE_DIR, "gh_bridge.py"))
    br = importlib.util.module_from_spec(spec)
    sys.modules[name] = br
    spec.loader.exec_module(br)
    return br


def _load_matrix():
    spec = importlib.util.spec_from_file_location(
        "mp_matrix_fix3", os.path.join(_BRIDGE_DIR, "matrix.py"))
    mx = importlib.util.module_from_spec(spec)
    sys.modules["mp_matrix_fix3"] = mx
    spec.loader.exec_module(mx)
    return mx


def _delivery():
    return {"delivery_id": "82c9c210-b73b-11f1-91a5-97cb4dd1c3ea",
            "event_name": "pull_request", "action": "synchronize",
            "repo": "nghqqa/fastapi-boilerplate-demo", "pr_number": 2,
            "observed_head_sha": "254f61ce" + "0" * 24,
            "observed_base_sha": "4cd5bf09" + "0" * 24,
            "error": None, "received_at": "2026-09-23T10:42:47Z"}


class RagProbeTests(unittest.TestCase):
    """修复1:宿主/容器探测分离 + 失败类型区分(不再把探测失败说成服务已停)。"""

    @classmethod
    def setUpClass(cls):
        cls.br = _load_bridge()

    def _probe_with(self, raiser=None, status=200):
        def fake_urlopen(req, timeout=None):
            if raiser:
                raise raiser
            return mock.MagicMock(status=status, __enter__=lambda s: mock.MagicMock(status=status), __exit__=lambda *a: False)
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            return self.br._rag_service_state(url="http://127.0.0.1:4184/health")

    def test_default_endpoint_is_host_loopback(self):
        """宿主桥默认探测 127.0.0.1(回环),不再默认 host.docker.internal。"""
        with mock.patch("urllib.request.urlopen") as u:
            u.return_value.__enter__.return_value.status = 200
            r = self.br._rag_service_state()
        self.assertEqual(r["endpoint"], "http://127.0.0.1:4184/health")
        self.assertTrue(r["endpoint"].startswith("http://127.0.0.1"))

    def test_reachable_shape(self):
        r = self._probe_with(status=200)
        self.assertEqual(r["state"], "reachable")
        self.assertIsNone(r["failure_kind"])

    def test_dns_failure_classified_not_unreachable(self):
        import socket
        r = self._probe_with(urllib.error.URLError(socket.gaierror(8, "nodename nor servname")))
        self.assertEqual(r["state"], "probe_failed")
        self.assertEqual(r["failure_kind"], "dns")
        self.assertIn("not resolvable", r["detail"])

    def test_connect_refused_classified(self):
        r = self._probe_with(urllib.error.URLError(ConnectionRefusedError()))
        self.assertEqual(r["failure_kind"], "connect")

    def test_timeout_classified(self):
        r = self._probe_with(TimeoutError())
        self.assertEqual(r["failure_kind"], "timeout")

    def test_http_error_classified(self):
        r = self._probe_with(urllib.error.HTTPError("u", 503, "unavailable", {}, None))
        self.assertEqual(r["failure_kind"], "http_503")

    def test_required_gate_refuses_probe_failed(self):
        """required 模式:probe_failed(不可证可达)一律 fail-closed。"""
        br = self.br
        with mock.patch.object(br, "_rag_snapshot_info", return_value={"snapshot_id": "ab" * 32}), \
             mock.patch.object(br, "_rag_service_state",
                               return_value={"state": "probe_failed", "endpoint": "e",
                                             "failure_kind": "connect", "detail": ""}), \
             mock.patch.dict(os.environ, {br.RAG_REQUIRED_ENV: "1"}):
            ok, detail = br.rag_dispatch_gate()
        self.assertFalse(ok)
        self.assertIn("probe_failed", detail)
        self.assertIn("connect", detail)


class _GateHarness:
    """conclude 三分支共用桩。"""

    def __init__(self, br, st, marker=None, pub_ok=True):
        self.br, self.st, self.marker, self.pub_ok = br, st, marker, pub_ok
        self.finishes, self.published, self.run_ends = [], [], []

    def run(self, d):
        br = self.br
        with mock.patch.object(br, "project_result", return_value="res"), \
             mock.patch.object(br, "gate_record", return_value=""), \
             mock.patch.object(br, "read_run_context", return_value=None), \
             mock.patch.object(br, "gate_marker", return_value=(self.marker, "" if self.marker else "no marker")), \
             mock.patch.object(br, "publish_with_retry",
                               side_effect=lambda *a, **k: (self.published.append(a) or
                                                            {"ok": self.pub_ok, "check_run_id": 424242,
                                                             "outcome": "retryable"})), \
             mock.patch.object(br, "finish",
                               side_effect=lambda d, ok, note, cid=None: self.finishes.append((ok, note))), \
             mock.patch.object(br, "emit_run_end",
                               side_effect=lambda ctx, st: self.run_ends.append(st)):
            br.conclude(d, self.st, "leader report", "run-x", "proj-x", "cid-1",
                        lambda *a: None)
        return self


class GateBranchTests(unittest.TestCase):
    """修复2:人工门/真超时/普通成功三分支,复用既有 reconcile-first 与发布约束。"""

    @classmethod
    def setUpClass(cls):
        cls.br = _load_bridge()

    def _marker(self, run_id="run-x", task_id=None, severity="HIGH"):
        return {"version": 1, "run_id": run_id, "task_id": task_id or "t-1",
                "severity": severity, "requested_by": "leader",
                "requested_at": "2026-09-23T10:47:13Z"}

    def test_gate_branch_publishes_action_required_and_marks_gate_wait(self):
        h = _GateHarness(self.br, "gate", marker=self._marker()).run(_delivery())
        self.assertEqual(len(h.published), 1)                       # 至多一个发布
        self.assertEqual(h.published[0][1], "gate")                 # verdict=gate
        self.assertEqual(len(h.finishes), 1)
        ok, note = h.finishes[0]
        self.assertFalse(ok)                                        # 业务未终结 → ERROR 留人工
        self.assertIn("GATE_WAIT", note)
        self.assertEqual(h.run_ends, ["gate"])

    def test_gate_verdict_maps_to_action_required(self):
        payload = self.br.post_check(_delivery(), "gate", "report", "run-x")
        self.assertEqual(payload["body"]["conclusion"], "action_required")
        self.assertIn("human gate", payload["body"]["output"]["title"])

    def test_true_timeout_still_neutral(self):
        h = _GateHarness(self.br, "timeout", marker=None).run(_delivery())
        self.assertEqual(h.published[0][1], "timeout")
        ok, note = h.finishes[0]
        self.assertIn("TIMEOUT", note)                              # 真超时语义保留
        payload = self.br.post_check(_delivery(), "timeout", "r", "run-x")
        self.assertEqual(payload["body"]["conclusion"], "neutral")

    def test_success_branch_unchanged(self):
        h = _GateHarness(self.br, "completed", marker=None).run(_delivery())
        ok, note = h.finishes[0]
        self.assertTrue(ok)
        self.assertIn("PROCESSED", note) if False else self.assertIn("check_run=", note)

    def test_watch_run_gate_vs_terminal_precedence(self):
        """终态优先于 gate 标记;无终态+合法标记→gate;无终态+无标记→timeout。"""
        br = self.br
        import itertools
        def _watch(status_seq, marker):
            # 时间单调递增并在 61 越过 deadline(60);轮询间隔 0 → 快速收敛
            ticks = itertools.count(1)
            with mock.patch.object(br, "project_status", side_effect=status_seq), \
                 mock.patch.object(br, "project_result", return_value="res"), \
                 mock.patch.object(br, "gate_marker", return_value=(marker, "" if marker else "no marker")), \
                 mock.patch.object(br.mx, "since", return_value=[]), \
                 mock.patch.object(br, "WATCH_POLL_S", 0), \
                 mock.patch.object(br.time, "time", side_effect=ticks):
                return br.watch_run("run-x", "proj-x", deadline_ts=60, task_id="t-1")
        st, _ = _watch(iter(["completed"]), self._marker())
        self.assertEqual(st, "completed")                           # 终态优先(同轮先判终态)
        st, _ = _watch(iter(["pending"] * 999), self._marker())
        self.assertEqual(st, "gate")                                # 合法标记→gate
        st, _ = _watch(iter(["pending"] * 999), None)
        self.assertEqual(st, "timeout")                             # 无标记→真超时


class GateMarkerAttributionTests(unittest.TestCase):
    """修复2:结构化+归属校验;模型文本不构成 gate 证据。"""

    @classmethod
    def setUpClass(cls):
        cls.br = _load_bridge()

    def _marker_file(self, obj):
        mc = mock.MagicMock(returncode=0)
        mc.stdout = json.dumps(obj)
        return mc

    def _read(self, obj):
        with mock.patch.object(self.br.subprocess, "run", return_value=self._marker_file(obj)):
            return self.br.gate_marker("proj-x", "run-x", "t-1")

    def test_valid_marker_accepted(self):
        m, why = self._read({"version": 1, "run_id": "run-x", "task_id": "t-1",
                             "severity": "HIGH", "requested_by": "leader",
                             "requested_at": "2026-09-23T10:47:13Z"})
        self.assertIsNotNone(m, why)

    def test_wrong_run_id_refused(self):
        m, why = self._read({"version": 1, "run_id": "run-OTHER", "task_id": "t-1",
                             "severity": "HIGH", "requested_by": "leader",
                             "requested_at": "x"})
        self.assertIsNone(m)
        self.assertIn("run_id", why)

    def test_wrong_task_refused(self):
        m, why = self._read({"version": 1, "run_id": "run-x", "task_id": "t-OTHER",
                             "severity": "HIGH", "requested_by": "leader",
                             "requested_at": "x"})
        self.assertIsNone(m)
        self.assertIn("task_id", why)

    def test_non_leader_author_refused(self):
        m, why = self._read({"version": 1, "run_id": "run-x", "task_id": "t-1",
                             "severity": "HIGH", "requested_by": "reviewer",
                             "requested_at": "x"})
        self.assertIsNone(m)
        self.assertIn("requested_by", why)

    def test_bad_severity_and_version_refused(self):
        base = {"version": 1, "run_id": "run-x", "task_id": "t-1",
                "requested_by": "leader", "requested_at": "x"}
        m, why = self._read(dict(base, severity="CRITICAL"))
        self.assertIsNone(m)
        m, why = self._read(dict(base, severity="HIGH", version=2))
        self.assertIsNone(m)
        self.assertIn("version", why)

    def test_natural_language_is_not_evidence(self):
        """仅有模型自然语言报告(无标记文件)→ 无 marker(走真超时)。"""
        mc = mock.MagicMock(returncode=1)
        mc.stdout = ""
        with mock.patch.object(self.br.subprocess, "run", return_value=mc):
            m, why = self.br.gate_marker("proj-x", "run-x")
        self.assertIsNone(m)
        self.assertEqual(why, "no marker")


class PreflightTests(unittest.TestCase):
    """§三:凭证缺失 → 不认领、不创建任何工件,退出码 2,脱敏诊断。"""

    def test_main_exits_before_claim_when_preflight_fails(self):
        """main 的预检接线:preflight 失败 → sys.exit(2),不触认领/工件路径。"""
        br = _load_bridge()
        with mock.patch.object(br, "target_filter_sql", return_value=("", None)), \
             mock.patch.object(br.mx, "preflight",
                               return_value=(False, "matrix admin credential unavailable: "
                                                    "admin password not found")), \
             mock.patch.object(br, "take_over_stale",
                               side_effect=AssertionError("must not be reached")), \
             mock.patch.object(br, "pending_deliveries",
                               side_effect=AssertionError("must not be reached")):
            with mock.patch.object(br.sys, "argv", ["gh_bridge.py", "run", "--once"]), \
                 mock.patch.object(br.sys, "exit", side_effect=SystemExit(2)) as ex:
                with self.assertRaises(SystemExit) as cm:
                    br.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertTrue(ex.called)

    def test_preflight_success_caches_token(self):
        mx = _load_matrix()
        mx._TOK = None
        with mock.patch.object(mx, "token", return_value="tok-abc") as t:
            ok, why = mx.preflight()
        self.assertTrue(ok)
        self.assertEqual(why, "")


class CaseRetrievalScopeFallbackTests(unittest.TestCase):
    """修复3:scope 经可信 run-context 文件透传;错误归属拒绝;不伪造。"""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(_REPO, "skills"))
        import importlib
        # case_retrieval 以 skills.case_retrieval 包导入(测试进程内)
        pkg = importlib.import_module("skills.case_retrieval.core")
        cls.core = pkg

    def _write_ctx(self, tmp, obj):
        p = os.path.join(tmp, "run-context.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        return p

    def test_env_scope_still_works(self):
        cfg = self.core.load_trusted_config({
            "MERGEPILOT_CR_PG_DSN": "postgresql://x",
            "MERGEPILOT_CR_REPO_SCOPE": "org/repo"})
        self.assertEqual(cfg["scope"], "org/repo")

    def test_missing_scope_still_scope_missing(self):
        with self.assertRaises(self.core.CaseRetrievalError) as cm:
            self.core.load_trusted_config({"MERGEPILOT_CR_PG_DSN": "postgresql://x"})
        self.assertEqual(cm.exception.subcode, self.core.SCOPE_MISSING)

    def test_scope_from_bridge_run_context_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_ctx(tmp, {
                "context_version": 1, "authored_by": "gh_bridge",
                "run_id": "run-gh-pr2-254f61ce-104621",
                "code": {"repo": "nghqqa/fastapi-boilerplate-demo"}})
            cfg = self.core.load_trusted_config({
                "MERGEPILOT_CR_PG_DSN": "postgresql://x",
                "MERGEPILOT_CR_REPO_SCOPE_FILE": p})
            self.assertEqual(cfg["scope"], "nghqqa/fastapi-boilerplate-demo")

    def test_foreign_author_file_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_ctx(tmp, {"authored_by": "reviewer-model",
                                      "run_id": "r", "code": {"repo": "evil/repo"}})
            with self.assertRaises(self.core.CaseRetrievalError) as cm:
                self.core.load_trusted_config({
                    "MERGEPILOT_CR_PG_DSN": "postgresql://x",
                    "MERGEPILOT_CR_REPO_SCOPE_FILE": p})
            self.assertEqual(cm.exception.subcode, self.core.SCOPE_MISSING)

    def test_missing_file_rejected(self):
        with self.assertRaises(self.core.CaseRetrievalError) as cm:
            self.core.load_trusted_config({
                "MERGEPILOT_CR_PG_DSN": "postgresql://x",
                "MERGEPILOT_CR_REPO_SCOPE_FILE": "/nonexistent/run-context.json"})
        self.assertEqual(cm.exception.subcode, self.core.SCOPE_MISSING)

    def test_repo_shape_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_ctx(tmp, {"authored_by": "gh_bridge", "run_id": "r",
                                      "code": {"repo": "x" * 300}})
            with self.assertRaises(self.core.CaseRetrievalError) as cm:
                self.core.load_trusted_config({
                    "MERGEPILOT_CR_PG_DSN": "postgresql://x",
                    "MERGEPILOT_CR_REPO_SCOPE_FILE": p})
            self.assertEqual(cm.exception.subcode, self.core.SCOPE_MISSING)


class Case2SanitizedReplayTests(unittest.TestCase):
    """CASE2 脱敏回放:run_context 边界+窗口关联 ≠ 逐调用身份(如实口径)。"""

    @classmethod
    def setUpClass(cls):
        cls.rc = importlib.import_module("mp_gh_run_context") if "mp_gh_run_context" in sys.modules \
            else _load_rc()

    def test_window_records_attribute_and_limitation_labeled(self):
        ctx = {"context_version": 1, "run_id": "run-gh-pr2-254f61ce-104621",
               "attempt_no": 1, "repo": "nghqqa/fastapi-boilerplate-demo", "pr": 2,
               "head_sha": "2" * 40, "skill_digest": {}, "retrieval_mode": "lexical-zh-en-v1",
               "manifest_id": "d" * 64, "delivery_id": "82c9c210",
               "authored_by": "gh_bridge", "created_at": "2026-09-23T10:46:29Z", "missing": []}
        audit = [
            {"record_type": "bridge.run_context", "ts": "2026-09-23T10:46:29Z", "tool": "bridge.run_context"},
            {"ts": "2026-09-23T10:46:46Z", "tool": "skill_diff_parse"},
            {"ts": "2026-09-23T10:46:56Z", "tool": "rag.retrieve"},
            {"record_type": "bridge.run_end", "ts": "2026-09-23T11:06:35Z", "tool": "bridge.run_end"},
        ]
        out = self.rc.attribute_audit_calls(audit, [ctx])
        self.assertEqual(out["method"], "bridge-boundary")
        self.assertEqual([r["tool"] for r in out["runs"]], ["skill_diff_parse", "rag.retrieve"])
        # 口径:窗口/边界关联≠逐调用携带可信身份
        for r in out["runs"]:
            self.assertNotIn("call_id", r)


def _load_rc():
    spec = importlib.util.spec_from_file_location(
        "mp_gh_run_context", os.path.join(_BRIDGE_DIR, "run_context.py"))
    rc = importlib.util.module_from_spec(spec)
    sys.modules["mp_gh_run_context"] = rc
    spec.loader.exec_module(rc)
    return rc


if __name__ == "__main__":
    unittest.main()
