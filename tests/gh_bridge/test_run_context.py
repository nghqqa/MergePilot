# -*- coding: utf-8 -*-
"""可信 run 上下文契约测试(R5 缺口修复)。

验收点:字段只来自编排器输入;authored_by 契约;write-once 语义;
审计记录形状;窗口相关回放(含毫秒时间戳与不可解析时间戳边界)。
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(_HERE, "..", "..", "tools", "gh-bridge")))

import importlib.util
_rc_path = os.path.normpath(os.path.join(_HERE, "..", "..", "tools", "gh-bridge", "run_context.py"))
_spec = importlib.util.spec_from_file_location("mp_gh_run_context", _rc_path)
rc = importlib.util.module_from_spec(_spec)
sys.modules["mp_gh_run_context"] = rc
_spec.loader.exec_module(rc)


def _manifest():
    return {
        "run_id": "run-gh-pr9-89c65a47-052152",
        "code": {"repo": "nghqqa/fastapi-boilerplate-demo",
                 "head_sha": "89c65a47" + "0" * 32,
                 "base_sha": "fdde4f41" + "0" * 32},
        "skills": {"names": ["skill_diff_parse"],
                   "content_sha256": {"skill_diff_parse": "aa" * 32}},
        "rag": {"retrieval_mode": "lexical-zh-en-v1", "snapshot_id": "fd34"},
        "delivery_id": "90cf0420-b70d-11f1-831e-d062c23f6de2",
    }


def _delivery():
    return {"delivery_id": "90cf0420-b70d-11f1-831e-d062c23f6de2",
            "repo": "nghqqa/fastapi-boilerplate-demo", "pr_number": 9,
            "observed_head_sha": "89c65a47" + "0" * 32}


MANIFEST_ID = "1b" * 32


class TestBuildRunContext(unittest.TestCase):
    def test_fields_derive_only_from_orchestrator_inputs(self):
        ctx = rc.build_run_context(_manifest(), _delivery(), attempt_no=1,
                                   manifest_id=MANIFEST_ID)
        self.assertEqual(ctx["run_id"], "run-gh-pr9-89c65a47-052152")
        self.assertEqual(ctx["attempt_no"], 1)
        self.assertEqual(ctx["repo"], "nghqqa/fastapi-boilerplate-demo")
        self.assertEqual(ctx["pr"], 9)                     # 从投递行来(manifest 无 pr)
        self.assertEqual(ctx["head_sha"], _delivery()["observed_head_sha"])
        self.assertEqual(ctx["skill_digest"], {"skill_diff_parse": "aa" * 32})
        self.assertEqual(ctx["retrieval_mode"], "lexical-zh-en-v1")
        self.assertEqual(ctx["manifest_id"], MANIFEST_ID)
        self.assertEqual(ctx["authored_by"], "gh_bridge")
        self.assertEqual(ctx["missing"], [])

    def test_attempt_no_must_be_positive_int(self):
        with self.assertRaises(ValueError):
            rc.build_run_context(_manifest(), _delivery(), attempt_no=0,
                                 manifest_id=MANIFEST_ID)
        with self.assertRaises(ValueError):
            rc.build_run_context(_manifest(), _delivery(), attempt_no=-2,
                                 manifest_id=MANIFEST_ID)

    def test_missing_fields_are_recorded_not_fabricated(self):
        man = _manifest()
        man["skills"]["content_sha256"] = None
        man["rag"]["retrieval_mode"] = None
        ctx = rc.build_run_context(man, _delivery(), attempt_no=3,
                                   manifest_id=MANIFEST_ID)
        self.assertIn("skill_digest", ctx["missing"])
        self.assertIn("retrieval_mode", ctx["missing"])
        self.assertIsNone(ctx["skill_digest"])
        self.assertIsNone(ctx["retrieval_mode"])

    def test_no_model_output_or_request_params_accepted(self):
        """契约:构造签名不接受任何"模型输出/请求参数"形参——多传即 TypeError。"""
        with self.assertRaises(TypeError):
            rc.build_run_context(_manifest(), _delivery(), attempt_no=1,
                                 manifest_id=MANIFEST_ID,
                                 model_reply="TASK_COMPLETED: ...")
        with self.assertRaises(TypeError):
            rc.build_run_context(_manifest(), _delivery(), attempt_no=1,
                                 manifest_id=MANIFEST_ID,
                                 user_param={"run_id": "forged"})


class TestValidateAndRecords(unittest.TestCase):
    def test_validate_roundtrip(self):
        ctx = rc.build_run_context(_manifest(), _delivery(), 1, MANIFEST_ID)
        ok, why = rc.validate_run_context(ctx)
        self.assertTrue(ok, why)

    def test_validate_rejects_foreign_author(self):
        ctx = rc.build_run_context(_manifest(), _delivery(), 1, MANIFEST_ID)
        ctx["authored_by"] = "reviewer-model"
        ok, why = rc.validate_run_context(ctx)
        self.assertFalse(ok)
        self.assertIn("authored_by", why)

    def test_validate_rejects_missing_required_field(self):
        ctx = rc.build_run_context(_manifest(), _delivery(), 1, MANIFEST_ID)
        del ctx["manifest_id"]
        ok, why = rc.validate_run_context(ctx)
        self.assertFalse(ok)
        self.assertIn("manifest_id", why)

    def test_context_and_end_records_shape(self):
        ctx = rc.build_run_context(_manifest(), _delivery(), 1, MANIFEST_ID)
        rec = rc.context_record(ctx, now="2026-09-23T05:21:59Z")
        self.assertEqual(rec["record_type"], "bridge.run_context")
        self.assertEqual(rec["tool"], "bridge.run_context")
        self.assertEqual(rec["data_mode"], "ORCHESTRATION")
        self.assertEqual(rec["run_context"]["run_id"], ctx["run_id"])
        end = rc.end_record(ctx, "completed", now="2026-09-23T05:23:39Z")
        self.assertEqual(end["record_type"], "bridge.run_end")
        self.assertEqual(end["terminal_status"], "completed")
        with self.assertRaises(ValueError):
            rc.context_record({"run_id": "x"})   # 非法上下文不许进审计流


class TestReplayAttribution(unittest.TestCase):
    def _ctx(self, run_id, created_at):
        c = rc.build_run_context(_manifest(), _delivery(), 1, MANIFEST_ID)
        c["run_id"] = run_id
        c["created_at"] = created_at
        return c

    def test_window_correlation_basic(self):
        ctxs = [self._ctx("run-a", "2026-09-23T05:21:59Z"),
                self._ctx("run-b", "2026-09-23T06:30:00Z")]
        audit = [
            {"ts": "2026-09-23T05:22:17.908Z", "tool": "skill_diff_parse"},
            {"ts": "2026-09-23T05:22:19.913Z", "tool": "skill_sast_scan"},
            {"ts": "2026-09-23T06:31:02.000Z", "tool": "skill_diff_parse"},
            {"ts": "2026-09-23T05:20:00.000Z", "tool": "skill_diff_parse"},  # 窗口前
        ]
        out = rc.attribute_audit_calls(audit, ctxs)
        self.assertEqual(out["method"], "window-correlation")
        self.assertIn("single-flight", out["note"])
        by_run = {}
        for r in out["runs"]:
            by_run.setdefault(r["run_id"], []).append(r["tool"])
        self.assertEqual(by_run["run-a"], ["skill_diff_parse", "skill_sast_scan"])
        self.assertEqual(by_run["run-b"], ["skill_diff_parse"])
        self.assertEqual(out["unattributed"], 1)   # 窗口前那条

    def test_millisecond_boundary_not_misordered(self):
        """毫秒时间戳与整秒边界:同秒内 .500Z 不得排到边界之前(字典序陷阱)。"""
        ctxs = [self._ctx("run-a", "2026-09-23T05:21:59Z"),
                self._ctx("run-b", "2026-09-23T05:22:30Z")]
        audit = [{"ts": "2026-09-23T05:21:59.500Z", "tool": "skill_diff_parse"}]
        out = rc.attribute_audit_calls(audit, ctxs)
        self.assertEqual(len(out["runs"]), 1)
        self.assertEqual(out["runs"][0]["run_id"], "run-a")

    def test_bridge_boundary_records_excluded_and_detected(self):
        ctxs = [self._ctx("run-a", "2026-09-23T05:21:59Z")]
        audit = [
            {"record_type": "bridge.run_context", "ts": "2026-09-23T05:21:59Z",
             "tool": "bridge.run_context"},
            {"ts": "2026-09-23T05:22:17.908Z", "tool": "skill_diff_parse"},
            {"record_type": "bridge.run_end", "ts": "2026-09-23T05:23:39Z",
             "tool": "bridge.run_end"},
        ]
        out = rc.attribute_audit_calls(audit, ctxs)
        self.assertEqual(out["method"], "bridge-boundary")
        self.assertEqual(len(out["runs"]), 1)   # 边界记录不归属

    def test_strict_mode_refuses_without_boundaries(self):
        ctxs = [self._ctx("run-a", "2026-09-23T05:21:59Z")]
        audit = [{"ts": "2026-09-23T05:22:17.908Z", "tool": "skill_diff_parse"}]
        out = rc.attribute_audit_calls(audit, ctxs, strict_windows=True)
        self.assertEqual(out["runs"], [])
        self.assertEqual(out["unattributed"], 1)

    def test_unparseable_timestamp_never_attributed(self):
        ctxs = [self._ctx("run-a", "2026-09-23T05:21:59Z")]
        audit = [{"ts": "not-a-time", "tool": "skill_diff_parse"},
                 {"ts": "", "tool": "skill_sast_scan"}]
        out = rc.attribute_audit_calls(audit, ctxs)
        self.assertEqual(out["runs"], [])
        self.assertEqual(out["unattributed"], 2)

    def test_invalid_context_in_input_rejected(self):
        bad = self._ctx("run-a", "2026-09-23T05:21:59Z")
        bad["authored_by"] = "someone-else"
        with self.assertRaises(ValueError):
            rc.attribute_audit_calls([], [bad])


class TestIsolationReplayAgainstCase1ShapedData(unittest.TestCase):
    """隔离回放演示:CASE1 形状的数据(审计流 + 单一 context)。"""

    def test_case1_shape_replay(self):
        ctx = rc.build_run_context(_manifest(), _delivery(), 1, MANIFEST_ID)
        ctx["created_at"] = "2026-09-23T05:21:59Z"
        audit = [
            {"ts": "2026-09-23T05:22:17.908Z", "tool": "skill_diff_parse",
             "result_status": "OK", "data_mode": "DETERMINISTIC_SKILL"},
            {"ts": "2026-09-23T05:22:19.913Z", "tool": "skill_sast_scan",
             "result_status": "OK", "data_mode": "DETERMINISTIC_SKILL"},
        ]
        out = rc.attribute_audit_calls(audit, [ctx])
        self.assertEqual(out["method"], "window-correlation")
        self.assertEqual([r["tool"] for r in out["runs"]],
                         ["skill_diff_parse", "skill_sast_scan"])
        for r in out["runs"]:
            self.assertEqual(r["run_id"], "run-gh-pr9-89c65a47-052152")
            self.assertEqual(r["manifest_id"], MANIFEST_ID)


if __name__ == "__main__":
    unittest.main()
