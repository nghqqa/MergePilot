"""取消范围纯逻辑测试(本轮不做容器操作;容器演练已于上一轮完成)。"""
from __future__ import annotations

import importlib.util
import sys
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


cs = _load("cancel_scope")


class CancelScopeTests(unittest.TestCase):
    def test_cancel_order_stops_leader_first(self):
        plan = cs.cancel_plan("budget breach", exclusivity_confirmed=True)
        actions = [s["action"] for s in plan["steps"]]
        self.assertEqual(actions[0], "stop_bridge")
        self.assertEqual(actions[1], "stop_agents")
        # leader 先于 reviewer(防再委托)
        stop_cmd = plan["steps"][1]["cmd"]
        self.assertLess(stop_cmd.index("leader"), stop_cmd.index("reviewer"))

    def test_reason_required(self):
        with self.assertRaises(ValueError):
            cs.cancel_plan("  ", exclusivity_confirmed=True)

    def test_caveat_upstream_inflight_recorded(self):
        """计划必须记录:上游在途请求可能仍计费,不构成累计消费硬上限。"""
        plan = cs.cancel_plan("timeout", exclusivity_confirmed=True)
        record = [s for s in plan["steps"] if s["action"] == "record_caveat"][0]
        self.assertIn("在途", record["detail"])
        self.assertIn("硬上限", record["detail"])

    def test_preconditions_require_exclusivity_and_no_active_projects(self):
        r1 = cs.validate_preconditions(False, lambda: [])
        self.assertFalse(r1["ok"])
        r2 = cs.validate_preconditions(True, lambda: ["other-project"])
        self.assertFalse(r2["ok"])
        self.assertIn("other-project", r2["active_projects"])
        r3 = cs.validate_preconditions(True, lambda: [])
        self.assertTrue(r3["ok"])

    def test_verify_cancelled(self):
        r = cs.verify_cancelled(
            stop_probe=lambda c: c != "elemiso-worker-reviewer",  # reviewer 还在
            quiet_probe=lambda: True)
        self.assertFalse(r["ok"])
        r2 = cs.verify_cancelled(stop_probe=lambda c: True, quiet_probe=lambda: True)
        self.assertTrue(r2["ok"])
        r3 = cs.verify_cancelled(stop_probe=lambda c: True, quiet_probe=lambda: False)
        self.assertFalse(r3["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
