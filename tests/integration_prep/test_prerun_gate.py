"""prerun gate 测试(全注入探针,零 IO/零真实执行)。"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[2] / "tools" / "integration_prep"
_pkg = sys.modules.setdefault("integration_prep_pkg", types.ModuleType("integration_prep_pkg"))
_pkg.__path__ = [str(_TOOLS)]


def _load(name):
    if "integration_prep_pkg." + name in sys.modules:
        return sys.modules["integration_prep_pkg." + name]
    spec = importlib.util.spec_from_file_location("integration_prep_pkg." + name,
                                                  _TOOLS / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["integration_prep_pkg." + name] = mod
    spec.loader.exec_module(mod)
    return mod


gate = _load("prerun_gate")


def _base(**over):
    global _TMP, _REPO_F, _RUN_F
    try:
        _TMP
    except NameError:
        _TMP = tempfile.mkdtemp(prefix="gate-test-")
        _REPO_F = os.path.join(_TMP, "repo-bridge.py")
        _RUN_F = os.path.join(_TMP, "run-bridge.py")
        for f in (_REPO_F, _RUN_F):
            open(f, "wb").write(b"bridge-bytes")
    kw = dict(repo_head="p" * 40, git_head="p" * 40, git_clean=True,
              repo_bridge=_REPO_F, run_bridge=_RUN_F,
              corpus_snapshot_bridge="s" * 64, corpus_snapshot_expected="s" * 64,
              v3_mode="off", expected_mode="off", bridge_running=False,
              ledger_running_rows=0,
              containers_present={"elemiso-ctrl": True, "elemiso-worker-reviewer": True},
              rag_live_required=False, rag_health_ok=None,
              budget_state="SET", delivery_head_processed=False,
              delivery_head="n" * 40)
    kw.update(over)
    return gate.run_gate(**kw)


class PrerunGateTests(unittest.TestCase):
    def test_all_green_passes(self):
        report = _base()
        self.assertTrue(gate.gate_passed(report))
        self.assertNotIn("FAIL", gate.format_report(report).replace(
            "FAIL — 存在未满足项,不启动", ""))

    def test_budget_missing_ack_blocks(self):
        """预算硬边界未落实 → 门 FAIL(不得为开跑放宽)。"""
        report = _base(budget_state="MISSING_ACK")
        self.assertFalse(gate.gate_passed(report))
        budget = [r for r in report if r["check"] == "budget"][0]
        self.assertEqual(budget["ok"], "FAIL")

    def test_processed_head_blocks(self):
        report = _base(delivery_head_processed=True)
        self.assertFalse(gate.gate_passed(report))

    def test_second_orchestrator_blocks(self):
        report = _base(bridge_running=True, ledger_running_rows=1)
        self.assertFalse(gate.gate_passed(report))

    def test_sha_mismatch_blocks(self):
        other = os.path.join(_TMP, "other.py")
        open(other, "wb").write(b"different-bytes")
        report = _base(run_bridge=other)
        self.assertFalse(gate.gate_passed(report))

    def test_rag_required_but_down_blocks(self):
        report = _base(rag_live_required=True, rag_health_ok=False)
        self.assertFalse(gate.gate_passed(report))
        report2 = _base(rag_live_required=True, rag_health_ok=True)
        self.assertTrue(gate.gate_passed(report2))

    def test_shadow_mode_requires_matching_expectation(self):
        report = _base(v3_mode="shadow", expected_mode="off")
        self.assertFalse(gate.gate_passed(report))
        report2 = _base(v3_mode="shadow", expected_mode="shadow")
        self.assertTrue(gate.gate_passed(report2))

    def test_budget_state_helper(self):
        self.assertEqual(gate.default_budget_state({}), "MISSING_ACK")
        self.assertEqual(gate.default_budget_state({"MERGEPILOT_RUN_BUDGET_TOKENS": "10"}),
                         "SET")


if __name__ == "__main__":
    unittest.main(verbosity=2)
