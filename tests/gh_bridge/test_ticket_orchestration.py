# -*- coding: utf-8 -*-
"""建票所有权冻结轮·桥侧测试(2026-09-24)。

覆盖整改规格 §八 场景 1-3/9/14/15 的桥映射 + review-outcome.v1 合同校验;
approval 侧纯函数与幂等性见 tests/approval/test_orchestration.py。
全部离线:结构化数据构造,零模型、零 GitHub 写。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from .test_publish_semantics import _delivery, _load_bridge

HEAD = "26ed8f1e4ca28692933df15ae6c2fd2cdf633a9b"
HEAD_NEW = "b" * 40
REPO = "nghqqa/fastapi-boilerplate-demo"
RUN = "run-gh-pr2-26ed8f1e-073224"
TASK = "gh-pr2-26ed8f1e-review-1"
NODE_ID = "MDQ6VXNlcjM1OTg3NDg="

MANIFEST = {
    "manifest_version": 1, "run_id": RUN, "project_id": "elemiso-" + RUN[4:],
    "delivery_id": "e077b300-b7e9-11f1-94c5-ff03aa78aa3c",
    "code": {"repo": REPO, "head_sha": HEAD,
             "base_sha": "4cd5bf099f88c3f3f85ee4c06b7adfe9925a6e5c"},
    "prompt": {"task_id": TASK},
}

RESULT_TEXT = (
    "STATUS: SUCCESS\n"
    "SUMMARY: STATUS: FINDING_CONFIRMED; SEVERITY: HIGH; "
    "HUMAN_VERIFICATION_REQUIRED: YES. PR #2 head %s (verified git rev-parse HEAD). "
    "Confirmed path traversal CWE-22 in demo_download; independent PoC -> HTTP 200.\n"
    "DELIVERABLES:\n- shared/tasks/%s/workspace/findings.md\n" % (HEAD, TASK))

FINDINGS_TEXT = (
    "# Independent Security Review — PR #2 (%s)\n\n"
    "## Verdict\n\n"
    "- **STATUS: FINDING_CONFIRMED**\n"
    "- **SEVERITY: HIGH** (my rating)\n"
    "- **CWE-22** — Path Traversal\n"
    "- **HUMAN_VERIFICATION_REQUIRED: YES**\n" % RUN)


def _policy_env(db_path, **over):
    env = {
        "MERGEPILOT_APPROVAL_DB": db_path,
        "MERGEPILOT_APPROVAL_ACTIONS": "generate_patch,run_poc",
        "MERGEPILOT_APPROVERS": json.dumps({REPO: [NODE_ID]}),
        "MERGEPILOT_APPROVAL_TTL_H": "24",
        "MERGEPILOT_APPROVAL_POLICY_VERSION": "db-2026-09-24",
        "MERGEPILOT_TICKET_MODE": "auto",
        "MERGEPILOT_OUTCOME_ENFORCE": "1",
    }
    env.update(over)
    return env


class ContractParseTests(unittest.TestCase):
    """review-outcome.v1 合同:令牌解析 + json 校验 + 绑定锚 = manifest。"""

    @classmethod
    def setUpClass(cls):
        cls.br = _load_bridge()
        cls.ro = cls.br._ro
        cls.man = json.loads(json.dumps(MANIFEST))

    def _mk_outcome(self, text, man=None, pr=2, builder="report"):
        b = (self.ro.build_outcome_from_report if builder == "report"
             else self.ro.build_outcome_from_findings)
        return b(text, self.man if man is None else man, pr)

    def test_report_header_tokens_build_outcome(self):
        out, err = self._mk_outcome(RESULT_TEXT)
        self.assertIsNone(err)
        self.assertEqual(out["finding_validation"], "CONFIRMED")
        self.assertEqual(out["findings"][0]["severity"], "HIGH")
        self.assertEqual(out["head_sha"], HEAD)          # 绑定来自 manifest
        self.assertEqual(out["run_id"], RUN)
        self.assertEqual(out["outcome_source"], "report-header")
        self.assertTrue(self.ro.gate_worthy(out))

    def test_findings_markdown_tokens_build_outcome(self):
        out, err = self._mk_outcome(FINDINGS_TEXT, builder="findings")
        self.assertIsNone(err)
        self.assertTrue(self.ro.gate_worthy(out))
        # 同一规范化结构 → 与报告头路径同一指纹(令牌等价)
        out2, _ = self._mk_outcome(RESULT_TEXT)
        self.assertEqual(out["findings"][0]["fingerprint"],
                         out2["findings"][0]["fingerprint"])

    def test_missing_token_fails_closed(self):
        text = "STATUS: SUCCESS\nSUMMARY: STATUS: FINDING_CONFIRMED only, no severity.\n"
        out, err = self._mk_outcome(text)
        self.assertIsNone(out)
        self.assertIn("missing severity", err)

    def test_conflicting_tokens_fail_closed(self):
        text = ("SUMMARY: STATUS: FINDING_CONFIRMED; SEVERITY: HIGH; "
                "SEVERITY: LOW; HUMAN_VERIFICATION_REQUIRED: YES")
        out, err = self._mk_outcome(text)
        self.assertIsNone(out)
        self.assertIn("conflicting", err)

    def test_prose_severity_is_not_a_token(self):
        """散文里提及 HIGH 而无 SEVERITY: 令牌 → 不建票(不从自由文本回填)。"""
        text = "SUMMARY: STATUS: FINDING_CONFIRMED; overall this is HIGH severity issue"
        out, err = self._mk_outcome(text)
        self.assertIsNone(out)

    def test_minimal_json_doc_without_fingerprint_normalized(self):
        doc = json.dumps({
            "schema_version": "review-outcome.v1", "run_id": RUN, "repo": REPO,
            "pr_number": 2, "head_sha": HEAD,
            "finding_validation": "CONFIRMED",
            "findings": [{"finding_id": "cwe22-demo-download", "severity": "HIGH",
                          "cwe": "CWE-22"}]})
        out, err = self.ro.parse_outcome_json(doc, self.man, 2)
        self.assertIsNone(err)
        self.assertTrue(out["finding_id"] if "finding_id" in out else True)
        self.assertTrue(out["findings"][0]["fingerprint"])     # 控制面重算
        self.assertTrue(self.ro.gate_worthy(out))

    def test_json_old_run_or_stale_head_refused(self):
        good = {"schema_version": "review-outcome.v1", "repo": REPO,
                "pr_number": 2, "finding_validation": "CONFIRMED",
                "findings": [{"finding_id": "x", "severity": "HIGH",
                              "cwe": "CWE-22"}]}
        for over, frag in (
                ({"run_id": "run-gh-pr2-42ed1787-003205", "head_sha": HEAD},
                 "run_id mismatch"),                      # 场景15:旧 CASE2-B run
                ({"run_id": RUN, "head_sha": "4" * 40}, "stale"),
                ({"run_id": RUN, "head_sha": HEAD}, None)):
            doc = dict(good)
            doc.update(over)
            if frag is None:
                out, err = self.ro.parse_outcome_json(json.dumps(doc), self.man, 2)
                self.assertIsNone(err)
                continue
            out, err = self.ro.parse_outcome_json(json.dumps(doc), self.man, 2)
            self.assertIsNone(out, over)
            self.assertIn(frag, err)

    def test_json_unknown_field_and_fingerprint_mismatch_refused(self):
        base = {"schema_version": "review-outcome.v1", "run_id": RUN, "repo": REPO,
                "pr_number": 2, "head_sha": HEAD,
                "finding_validation": "CONFIRMED",
                "findings": [{"finding_id": "x", "severity": "HIGH",
                              "cwe": "CWE-22"}]}
        bad1 = dict(base, leader_note="extra")     # 未知字段
        out, err = self.ro.parse_outcome_json(json.dumps(bad1), self.man, 2)
        self.assertIsNone(out)
        self.assertIn("unknown top-level", err)
        bad2 = json.loads(json.dumps(base))
        bad2["findings"][0]["fingerprint"] = "f" * 64   # 自报错误指纹
        out, err = self.ro.parse_outcome_json(json.dumps(bad2), self.man, 2)
        self.assertIsNone(out)
        self.assertIn("fingerprint mismatch", err)

    def test_validation_must_bind_current_run_and_head(self):
        ro = self.ro
        out, _ = self._mk_outcome(RESULT_TEXT)
        self.assertFalse(ro.has_current_run_validation(out))
        out["validations"] = [{"kind": "poc", "run_id": "old", "head_sha": HEAD,
                               "evidence_refs": ["x"]}]
        self.assertFalse(ro.has_current_run_validation(out))
        out["validations"] = [{"kind": "poc", "run_id": RUN, "head_sha": HEAD,
                               "evidence_refs": ["poc.log"]}]
        self.assertTrue(ro.has_current_run_validation(out))


class FetchOutcomeTests(unittest.TestCase):
    """取数优先级:json(无效即拒,不降级)→ result 头 → findings。"""

    @classmethod
    def setUpClass(cls):
        cls.br = _load_bridge()

    def test_json_present_but_invalid_refused_no_fallback(self):
        br = self.br
        d = _delivery(observed_head_sha=HEAD)
        with mock.patch.object(br, "read_run_manifest", return_value=json.loads(
                json.dumps(MANIFEST))), \
             mock.patch.object(br, "_mc_cat", return_value="{not json"), \
             mock.patch.object(br, "project_result", return_value=RESULT_TEXT):
            out, err = br.fetch_review_outcome("proj-x", d)
        self.assertIsNone(out)
        self.assertIn("not json", err)

    def test_fallback_chain_result_then_findings(self):
        br = self.br
        man = json.loads(json.dumps(MANIFEST))
        d = _delivery(observed_head_sha=HEAD)
        d["task_id"] = TASK
        with mock.patch.object(br, "read_run_manifest", return_value=man), \
             mock.patch.object(br, "_mc_cat", return_value=None), \
             mock.patch.object(br, "project_result", return_value=RESULT_TEXT):
            out, err = br.fetch_review_outcome("proj-x", d)
        self.assertIsNone(err)
        self.assertEqual(out["outcome_source"], "report-header")
        # result 不可读 → findings 交付物令牌
        with mock.patch.object(br, "read_run_manifest", return_value=man), \
             mock.patch.object(br, "_mc_cat",
                               side_effect=lambda p: FINDINGS_TEXT
                               if "findings.md" in p else None), \
             mock.patch.object(br, "project_result", return_value=""):
            out, err = br.fetch_review_outcome("proj-x", d)
        self.assertIsNone(err)
        self.assertEqual(out["outcome_source"], "reviewer-findings")

    def test_manifest_unavailable_refused(self):
        br = self.br
        with mock.patch.object(br, "read_run_manifest", return_value=None):
            out, err = br.fetch_review_outcome("proj-x", _delivery())
        self.assertIsNone(out)
        self.assertIn("run-manifest unavailable", err)


class WatchOutcomeTests(unittest.TestCase):
    """watch:CONFIRMED HIGH 无 marker/无终态 → gate(leader 卡死不丢票)。"""

    @classmethod
    def setUpClass(cls):
        cls.br = _load_bridge()

    def _watch(self, status_seq, marker, fetch, enforce="1"):
        br = self.br
        import itertools
        box = {"pr_number": 2, "task_id": TASK, "outcome": None, "err": None}
        ticks = itertools.count(1)
        with mock.patch.object(br, "project_status", side_effect=status_seq), \
             mock.patch.object(br, "project_result", return_value=""), \
             mock.patch.object(br, "gate_marker",
                               return_value=(marker, "" if marker else "no marker")), \
             mock.patch.object(br, "fetch_review_outcome",
                               side_effect=lambda proj, b: fetch), \
             mock.patch.object(br.mx, "since", return_value=[]), \
             mock.patch.object(br, "WATCH_POLL_S", 0), \
             mock.patch.dict(os.environ, {"MERGEPILOT_OUTCOME_ENFORCE": enforce}), \
             mock.patch.object(br.time, "time", side_effect=ticks):
            st, _ = br.watch_run("run-x", "proj-x", deadline_ts=60,
                                 outcome_box=box)
        return st, box

    def _confirmed_high(self):
        out, _ = self.br._ro.build_outcome_from_report(RESULT_TEXT, MANIFEST, 2)
        return out

    def test_confirmed_high_without_marker_or_terminal_returns_gate(self):
        out = self._confirmed_high()
        st, box = self._watch(iter(["pending"] * 999), None, (out, None))
        self.assertEqual(st, "gate")
        self.assertIs(box["outcome"], out)

    def test_poll_error_keeps_polling_until_deadline(self):
        st, box = self._watch(iter(["pending"] * 999), None,
                              (None, "no outcome doc and no result text"))
        self.assertEqual(st, "timeout")     # 产物未写出 ≠ attention

    def test_terminal_with_invalid_outcome_enforce_attention(self):
        st, box = self._watch(iter(["completed"]), None,
                              (None, "missing severity token"))
        self.assertEqual(st, "attention")

    def test_terminal_with_invalid_outcome_lenient_keeps_terminal(self):
        st, _ = self._watch(iter(["completed"]), None,
                            (None, "missing severity token"), enforce="0")
        self.assertEqual(st, "completed")

    def test_inconclusive_state(self):
        out, _ = self.br._ro.build_outcome_from_report(
            "SUMMARY: STATUS: INCONCLUSIVE; SEVERITY: HIGH", MANIFEST, 2)
        st, _ = self._watch(iter(["pending"] * 999), None, (out, None))
        self.assertEqual(st, "inconclusive")

    def test_not_confirmed_terminal_maps_legacy_pass(self):
        out, _ = self.br._ro.build_outcome_from_report(
            "SUMMARY: STATUS: NOT_CONFIRMED; SEVERITY: LOW", MANIFEST, 2)
        st, _ = self._watch(iter(["completed"]), None, (out, None))
        self.assertEqual(st, "completed")


class ConcludeGateTests(unittest.TestCase):
    """conclude gate 分支:ensure 建票 → action_required + GATE_WAIT;
    建票拒绝 → MANUAL_ATTENTION。全程真实 SQLite(临时库)。"""

    @classmethod
    def setUpClass(cls):
        cls.br = _load_bridge()

    def _run_conclude(self, outcome, marker=None, env_over=None, mode="auto"):
        br = self.br
        d = _delivery(observed_head_sha=HEAD)
        finishes, published, run_ends = [], [], []
        db_path = (env_over or {}).get(
            "MERGEPILOT_APPROVAL_DB", tempfile.mkdtemp() + os.sep + "t.db")
        env = _policy_env(db_path, MERGEPILOT_TICKET_MODE=mode)
        env.update(env_over or {})
        with mock.patch.object(br, "project_result", return_value="res"), \
             mock.patch.object(br, "gate_record", return_value=""), \
             mock.patch.object(br, "read_run_context", return_value=None), \
             mock.patch.object(br, "gate_marker",
                               return_value=(marker, "" if marker else "no marker")), \
             mock.patch.object(br, "publish_with_retry",
                               side_effect=lambda *a, **k:
                               (published.append(a + (k.get("detail"),)) or
                                {"ok": True, "check_run_id": 424242})), \
             mock.patch.object(br, "finish",
                               side_effect=lambda d, ok, note, cid=None:
                               finishes.append((ok, note))), \
             mock.patch.object(br, "emit_run_end",
                               side_effect=lambda ctx, st: run_ends.append(st)), \
             mock.patch.dict(os.environ, env, clear=False):
            br.conclude(d, "gate", "leader report", RUN, "proj-x", "cid-1",
                        lambda *a: None, outcome=outcome)
        return finishes, published, run_ends, db_path

    def _confirmed_high(self):
        out, _ = self.br._ro.build_outcome_from_report(RESULT_TEXT, MANIFEST, 2)
        return out

    def test_gate_creates_pending_ticket_and_action_required(self):
        out = self._confirmed_high()
        finishes, published, run_ends, db_path = self._run_conclude(out, marker=None)
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0][1], "gate")
        detail = published[0][6]
        self.assertIn("ticket PENDING", detail)
        self.assertIn("creator=control-plane", detail)
        ok, note = finishes[0]
        self.assertFalse(ok)
        self.assertIn("GATE_WAIT", note)
        self.assertIn("ticket=", note)
        self.assertEqual(run_ends, ["gate"])
        # 票据事实:控制面创建,PENDING,绑定完整
        tid = detail.split("ticket PENDING ")[1].split(" ")[0]
        store = self.br.sys.modules[
            "mp_approval_pkg.store_sqlite"].SQLiteTicketStore(db_path)
        try:
            t = store.get(tid)
            self.assertEqual(t.status, "PENDING")
            self.assertIsNone(t.approved_by)                 # 创建≠批准
            self.assertEqual(t.binding.repo, REPO)
            self.assertEqual(t.binding.head_sha, HEAD)
            self.assertEqual(t.binding.action, "run_poc")
        finally:
            store.close()

    def test_gate_idempotent_on_conclude_replay(self):
        out = self._confirmed_high()
        shared_db = tempfile.mkdtemp() + os.sep + "t.db"   # 同库两次 conclude(崩溃恢复)
        f1, p1, _, db1 = self._run_conclude(out, env_over=_policy_env(shared_db))
        f2, p2, _, db2 = self._run_conclude(out, env_over=_policy_env(shared_db))
        self.assertEqual(db1, db2)
        tid1 = p1[0][6].split("ticket PENDING ")[1].split(" ")[0]
        tid2 = p2[0][6].split("ticket PENDING ")[1].split(" ")[0]
        self.assertEqual(tid1, tid2)                         # 重跑不重复建票

    def test_marker_conflict_recorded_outcome_wins(self):
        out = self._confirmed_high()
        marker = {"version": 1, "run_id": RUN, "task_id": TASK, "severity": "LOW",
                  "requested_by": "leader", "requested_at": "2026-09-24T07:00:00Z"}
        finishes, published, _, _db = self._run_conclude(out, marker=marker)
        detail = published[0][6]
        self.assertIn("ticket PENDING", detail)
        self.assertIn("marker=CONFLICT:", detail)
        self.assertIn("GATE_WAIT", finishes[0][1])

    def test_ticket_refused_manual_attention(self):
        out = self._confirmed_high()
        env = _policy_env(tempfile.mkdtemp() + os.sep + "t.db")
        env["MERGEPILOT_APPROVAL_ACTIONS"] = "publish_result"   # run_poc 未启用
        finishes, published, _, _db = self._run_conclude(out, env_over=env)
        ok, note = finishes[0]
        self.assertFalse(ok)
        self.assertIn("MANUAL_ATTENTION", note)
        self.assertIn("ACTION_NOT_ENABLED:run_poc", note)
        self.assertIn("ticket REFUSED", published[0][6])

    def test_observe_mode_writes_no_state(self):
        out = self._confirmed_high()
        finishes, published, _, _db = self._run_conclude(out, mode="observe")
        self.assertIn("MANUAL_ATTENTION", finishes[0][1])
        self.assertIn("OBSERVE_MODE", published[0][6])

    def test_marker_only_fallback_without_outcome(self):
        """outcome=None(旧调用方/模块缺失)→ marker 兼容路径,GATE_WAIT。"""
        marker = {"version": 1, "run_id": RUN, "task_id": TASK, "severity": "HIGH",
                  "requested_by": "leader", "requested_at": "2026-09-24T07:00:00Z"}
        finishes, published, run_ends, _db = self._run_conclude(None, marker=marker)
        self.assertEqual(published[0][1], "gate")
        self.assertIn("GATE_WAIT", finishes[0][1])
        self.assertEqual(run_ends, ["gate"])


class CheckRunSemanticsTests(unittest.TestCase):
    """场景14:三事实分离。HIGH+PENDING → action_required;
    INCONCLUSIVE/attention/timeout → neutral;不发布最终 success。"""

    @classmethod
    def setUpClass(cls):
        cls.br = _load_bridge()

    def test_gate_action_required_with_ticket_detail(self):
        p = self.br.post_check(_delivery(), "gate", "r", "run-x",
                               detail="ticket PENDING tkt-x (creator=control-plane)")
        self.assertEqual(p["body"]["conclusion"], "action_required")
        self.assertIn("human gate", p["body"]["output"]["title"])
        self.assertIn("ticket PENDING tkt-x", p["body"]["output"]["title"])
        self.assertIn("control-plane:", p["body"]["output"]["summary"])

    def test_inconclusive_neutral(self):
        p = self.br.post_check(_delivery(), "inconclusive", "r", "run-x")
        self.assertEqual(p["body"]["conclusion"], "neutral")
        self.assertIn("no conclusion", p["body"]["output"]["title"])

    def test_attention_neutral_with_error(self):
        p = self.br.post_check(_delivery(), "attention", "r", "run-x",
                               detail="manual attention: missing severity token")
        self.assertEqual(p["body"]["conclusion"], "neutral")
        self.assertIn("manual attention", p["body"]["output"]["title"])

    def test_timeout_still_neutral(self):
        p = self.br.post_check(_delivery(), "timeout", "r", "run-x")
        self.assertEqual(p["body"]["conclusion"], "neutral")

    def test_no_final_success_mapping_for_gate_states(self):
        for v in ("gate", "inconclusive", "attention", "timeout"):
            p = self.br.post_check(_delivery(), v, "r", "run-x")
            self.assertNotEqual(p["body"]["conclusion"], "success", v)


class OutcomeBoxWiringTests(unittest.TestCase):
    """process/resume 只在结构化通道可用时启用 box(否则旧语义)。"""

    def test_box_requires_review_outcome_module(self):
        br = _load_bridge()
        self.assertIsNotNone(br._ro)     # 本仓自带 review_outcome.py → 必须可用
        self.assertIsNotNone(br._orch)


if __name__ == "__main__":
    unittest.main()
