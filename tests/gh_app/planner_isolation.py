"""Shared test isolation for the planner's module-global image
identity registry.

`one_click_startup._builtin_registry` is a process-wide singleton
with an IMMUTABLE-once-recorded contract: recording a second,
different identity for a service raises IMAGE_DIGEST_MISMATCH. A
test that runs the real CLI against the real checkout records the
REAL image IDs; any later test recording a synthetic install
(sha256:ab*32) then fails with 'recorded identity changed for X'.

Order-dependent suite failures (maintenance round §3) traced to
exactly this leak. add_planner_registry_isolation(testcase)
snapshots the registry and restores it on cleanup, so every test
observes whatever baseline the suite started with.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = HERE.parent.parent
for p in (str(HERE), str(ROOT), str(ROOT / "tools" / "cli"),
          str(ROOT / "tools" / "demo_console")):
    if p not in sys.path:
        sys.path.insert(0, p)

import one_click_startup as _oc          # noqa: E402


def add_planner_registry_isolation(testcase) -> None:
    """Snapshot + restore _builtin_registry around one test case."""
    snapshot = dict(_oc._builtin_registry)

    def _restore():
        _oc._builtin_registry.clear()
        _oc._builtin_registry.update(snapshot)

    testcase.addCleanup(_restore)
