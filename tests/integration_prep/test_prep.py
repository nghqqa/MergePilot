"""integration_prep 测试:授权闸门 / 脱敏 / 计划完备性。全部本地,零执行。"""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[2] / "tools" / "integration_prep"
_pkg = types.ModuleType("integration_prep_pkg")
_pkg.__path__ = [str(_TOOLS)]
sys.modules.setdefault("integration_prep_pkg", _pkg)


def _load(name):
    if "integration_prep_pkg." + name in sys.modules:
        return sys.modules["integration_prep_pkg." + name]
    spec = importlib.util.spec_from_file_location("integration_prep_pkg." + name,
                                                  _TOOLS / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["integration_prep_pkg." + name] = mod
    spec.loader.exec_module(mod)
    return mod


authgate = _load("authgate")
collect = _load("collect")
steps = _load("steps")


class AuthGateTests(unittest.TestCase):
    def test_default_deny(self):
        self.assertFalse(authgate.is_authorized({}))
        with self.assertRaises(authgate.AuthorizationRequired):
            authgate.assert_authorized("R1", {})

    def test_explicit_flag_grants(self):
        self.assertTrue(authgate.is_authorized({authgate.AUTH_ENV: "1"}))
        authgate.assert_authorized("R1", {authgate.AUTH_ENV: "1"})  # 不抛

    def test_other_values_do_not_grant(self):
        self.assertFalse(authgate.is_authorized({authgate.AUTH_ENV: "yes"}))
        self.assertFalse(authgate.is_authorized({authgate.AUTH_ENV: "true"}))


class RedactTests(unittest.TestCase):
    def test_github_token(self):
        out = collect.redact("token ghp_ABCDEFGHIJK1234567890 end")
        self.assertNotIn("ghp_ABCDEFGHIJK", out)
        self.assertIn("<redacted:gh-token>", out)

    def test_dsn_credentials(self):
        out = collect.redact("postgresql://user:secret123@host:5432/db")
        self.assertNotIn("secret123", out)
        self.assertIn("://<redacted>@", out)

    def test_password_and_api_key_shapes(self):
        out = collect.redact('{"password": "hunter2", "api_key": "abcd12345678"}')
        self.assertNotIn("hunter2", out)
        self.assertNotIn("abcd12345678", out)

    def test_recursive_obj_redaction(self):
        out = collect.redact_obj({"rows": [{"err": "pwd=topsecret line"}], "ok": 1})
        self.assertNotIn("topsecret", out)
        self.assertEqual(out["ok"], 1)

    def test_benign_text_untouched(self):
        self.assertEqual(collect.redact("review:generic SKIPPED shadow"), 
                         "review:generic SKIPPED shadow")


class PlanTests(unittest.TestCase):
    def test_all_five_plans_exist_with_evidence(self):
        for key in ("R1", "R2", "R3", "R4", "R7"):
            plan = steps.PLANS[key]
            self.assertTrue(plan, key)
            for s in plan:
                self.assertIn("op", s)
                self.assertIn("evidence", s)

    def test_r1_covers_three_crash_stages_and_cleanup(self):
        text = " ".join(s["step"] for s in steps.R1_PLAN)
        for frag in ("认领后", "派发后", "发布前", "清场"):
            self.assertIn(frag, text)

    def test_r3_covers_required_scenarios(self):
        text = " ".join(s["step"] for s in steps.R3_PLAN)
        for frag in ("回写成功", "重复 webhook", "PR 更新", "失败恢复", "旧 run 失效"):
            self.assertIn(frag, text)

    def test_plans_are_documentation_only(self):
        """计划不得包含可直接执行的破坏性命令串(声明式步骤,非脚本)。"""
        for key, plan in steps.PLANS.items():
            for s in plan:
                self.assertNotIn("DROP TABLE", s["op"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
