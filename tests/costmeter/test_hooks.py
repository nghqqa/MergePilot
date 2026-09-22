"""dispatch budget hook 工厂测试(成本计量接入点准备)。"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

_COST = Path(__file__).resolve().parents[2] / "tools" / "costmeter"


def _load(name):
    spec = importlib.util.spec_from_file_location("costmeter_" + name,
                                                  _COST / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["costmeter_" + name] = mod
    spec.loader.exec_module(mod)
    return mod


core = _load("core")
hooks = _load("hooks")


class DispatchHookFactoryTests(unittest.TestCase):
    def test_unconfigured_returns_none(self):
        """预算未拍板:不配置即无预算检查(不产生授权,也不阻止工作)。"""
        self.assertIsNone(hooks.make_dispatch_budget_hook(environ={}))

    def test_invalid_limit_rejected(self):
        with self.assertRaises(ValueError):
            hooks.make_dispatch_budget_hook(environ={hooks.BUDGET_ENV: "-5"})

    def test_configured_hook_enforces_limit(self):
        with tempfile.TemporaryDirectory() as t:
            hook = hooks.make_dispatch_budget_hook(
                environ={hooks.BUDGET_ENV: "3"}, run_id="run-x", ledger_dir=t)
            self.assertIsNotNone(hook)
            for _ in range(3):
                hook()                       # 3 次派发在预算内
            # 独立加载副本的异常类经 hook 暴露,保证调用方能精确捕获
            with self.assertRaises(hook.BudgetExceeded):
                hook()                       # 第 4 次超限
            self.assertEqual(hook.guard_status()["remaining"], 0)

    def test_ledger_survives_restart(self):
        import os
        with tempfile.TemporaryDirectory() as t:
            hook1 = hooks.make_dispatch_budget_hook(
                environ={hooks.BUDGET_ENV: "2"}, run_id="run-y", ledger_dir=t)
            hook1()
            hook2 = hooks.make_dispatch_budget_hook(
                environ={hooks.BUDGET_ENV: "2"}, run_id="run-y", ledger_dir=t)
            self.assertIsNotNone(hook2)
            hook2()                          # 台账恢复:只剩 1 次额度
            with self.assertRaises(hook2.BudgetExceeded):
                hook2()
            self.assertEqual(os.listdir(t), ["budget-run-y.json"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
