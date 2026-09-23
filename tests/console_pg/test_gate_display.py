# -*- coding: utf-8 -*-
"""gate_display 八态契约测试(RPD-06,有限前端对齐)。"""
import importlib.util
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "gate_display", os.path.join(_HERE, "..", "..", "tools", "console_pg", "gate_display.py"))
gd = importlib.util.module_from_spec(spec)
sys.modules["gate_display"] = gd
spec.loader.exec_module(gd)


class GateDisplayContractTests(unittest.TestCase):
    def test_exact_state_enum(self):
        self.assertEqual(set(gd.GATE_DISPLAY_STATES), {
            "pending", "action_required", "approved_plan_ready", "rejected",
            "blocked", "expired", "backend_unavailable", "scope_missing"})

    def test_pending_vs_action_required(self):
        self.assertEqual(gd.gate_display("PENDING"), "pending")
        self.assertEqual(gd.gate_display("PENDING", published=True), "action_required")

    def test_ticket_mappings(self):
        self.assertEqual(gd.gate_display("APPROVED"), "approved_plan_ready")
        self.assertEqual(gd.gate_display("EXECUTING"), "approved_plan_ready")
        self.assertEqual(gd.gate_display("USED"), "approved_plan_ready")
        self.assertEqual(gd.gate_display("REJECTED"), "rejected")
        self.assertEqual(gd.gate_display("EXPIRED"), "expired")
        self.assertEqual(gd.gate_display("FAILED"), "blocked")
        self.assertEqual(gd.gate_display("INVALIDATED"), "blocked")

    def test_environmental_override(self):
        self.assertEqual(gd.gate_display("PENDING", backend_ok=False),
                         "backend_unavailable")
        self.assertEqual(gd.gate_display("APPROVED", scope_ok=False), "scope_missing")
        # 环境标志优先于票据状态
        self.assertEqual(gd.gate_display("REJECTED", backend_ok=False, published=True),
                         "backend_unavailable")

    def test_unmapped_status_raises(self):
        with self.assertRaises(ValueError):
            gd.gate_display("SOMETHING_ELSE")


if __name__ == "__main__":
    unittest.main()
