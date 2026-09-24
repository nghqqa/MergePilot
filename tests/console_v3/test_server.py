"""v3 只读控制台测试:GET-only API / shadow 标记 / 部分完成可见 / 无写端点。"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[2] / "tools" / "console_v3"


def _load_server():
    spec = importlib.util.spec_from_file_location("console_v3_server",
                                                  _TOOLS / "server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["console_v3_server"] = mod
    spec.loader.exec_module(mod)
    return mod


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read()


class ConsoleServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.srv_mod = _load_server()
        cls.store = cls.srv_mod.RunStore(os.path.join(cls.tmp.name, "runs.db"))
        # 播种一条 shadow run(含部分完成 coverage)
        cls.store.save_run({
            "run_id": "shadow-gh-pr2-abcd1234", "repo": "team/demo",
            "pr_number": 2, "head_sha": "a" * 40, "base_sha": "b" * 40,
            "mode": "shadow", "risk_tier": "FULL",
            "risk_json": {"level": "FULL", "human_review_required": True,
                          "reviewers": ["generic", "security"], "reasons": [],
                          "sensitive_hits": ["app/auth/x.py"], "lines_changed": 83,
                          "files_changed": 2},
            "plan_json": [],
            "stages_json": {"risk": {"status": "SUCCEEDED", "attempts": 1},
                            "review:security": {"status": "SKIPPED", "attempts": 0,
                                                "error": "shadow: agent not executed"}},
            "outcome_json": {"outcome": "MANUAL_ATTENTION", "review_complete": False,
                             "coverage_missing": ["review:security"],
                             "critical_failures": ["review:security"],
                             "note": "part"},
            "coverage_missing": ["review:security"],
            "downgrade_reason": "review:security skipped(shadow)",
            "aggregates_json": {"findings": [{"category": "path-traversal",
                                              "sources": [{"reviewer": "generic",
                                                           "finding_id": "f1"}]}],
                                "total": 1, "dropped_duplicates": 0},
            "finding_validation": "NOT_APPLICABLE",
            "patch_validation": "NOT_APPLICABLE", "rag_snapshot": "fd34",
            "manifest_hash": "h" * 64, "created_at": "t0", "updated_at": "t1"})
        cls.port = cls._free_port()
        cls.httpd = cls.srv_mod.ThreadingHTTPServer(
            ("127.0.0.1", cls.port), cls.srv_mod.make_handler(cls.store))
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.store.close()
        cls.tmp.cleanup()

    @staticmethod
    def _free_port():
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def _url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def test_healthz_read_only(self):
        status, body = _get(self._url("/healthz"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["mode"], "read-only")

    def test_runs_list_marks_shadow_and_partial(self):
        status, body = _get(self._url("/api/runs"))
        data = json.loads(body)
        self.assertEqual(data["data_mode"], "shadow/fixture only")
        run = data["runs"][0]
        self.assertEqual(run["mode"], "shadow")            # 模式标签显式
        self.assertFalse(run["review_complete"])           # 部分完成可见
        self.assertEqual(run["coverage_missing"], ["review:security"])

    def test_run_detail_payload_fields(self):
        status, body = _get(self._url("/api/runs/shadow-gh-pr2-abcd1234"))
        payload = json.loads(body)
        for key in ("run_id", "risk_level", "reviewers", "findings",
                    "coverage", "outcome", "mode", "manifest_hash",
                    "downgrade_reason", "rag"):
            self.assertIn(key, payload)
        self.assertEqual(payload["mode"], "shadow")
        self.assertEqual(payload["findings"]["total"], 1)
        self.assertEqual(payload["findings"]["by_source"], {"generic": 1})
        self.assertEqual(payload["rag"]["snapshot_id"], "fd34")
        self.assertFalse(payload["coverage"]["complete"])

    def test_hook_errors_endpoint_readonly(self):
        self.store.record_hook_error("del-x", "RuntimeError: boom", "t3")
        status, body = _get(self._url("/api/hook-errors"))
        data = json.loads(body)
        self.assertEqual(data["errors"][-1]["delivery_id"], "del-x")

    def test_unknown_run_404(self):
        try:
            _get(self._url("/api/runs/nope"))
            self.fail("expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_write_methods_rejected(self):
        for method in ("POST", "PUT", "DELETE"):
            req = urllib.request.Request(self._url("/api/runs"), data=b"{}",
                                         method=method)
            try:
                urllib.request.urlopen(req, timeout=5)
                self.fail("%s should be rejected" % method)
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 405)

    def test_index_page_is_readonly_html(self):
        status, body = _get(self._url("/"))
        html = body.decode("utf-8")
        self.assertIn("READ-ONLY", html)
        self.assertIn("shadow", html)
        self.assertIn("写操作端点", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
