"""Subprocess executor -- TRUSTED-DEV ONLY (isolation='process').

``shell=False`` with an argv list prevents shell interpolation but does NOT
provide a strong sandbox (no CPU/memory/network hard isolation). It must never
be the default production path for untrusted code; core only selects it when the
deploy explicitly sets ``MERGEPILOT_TR_TRUSTED_DEV=true``.
"""
from __future__ import annotations

from skills.test_runner.executors import _common


def run(plan):
    return _common.run_captured(
        plan["argv"], plan["cwd"], plan["env"],
        plan["timeout_ms"], plan["max_output_bytes"],
        executor="subprocess", isolation="process",
    )
