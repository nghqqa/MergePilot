#!/usr/bin/env python3
"""Run PRLifecycle through the real CLI envelope path with its in-memory fixture."""
from __future__ import annotations

import json
import os
import sys

REPO = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
M4D = os.path.join(REPO, "tests", "m4d")
for path in (REPO, M4D, os.path.join(REPO, "tools", "rag")):
    if path not in sys.path:
        sys.path.insert(0, path)

from conftest import FakeAdapter, trusted_env  # noqa: E402
from skills.common.runtime.cli import run_request  # noqa: E402
from skills.pr_lifecycle import run as pr_run  # noqa: E402


def main() -> int:
    os.environ.update(trusted_env())
    adapter = FakeAdapter()
    pr_run._ADAPTER_FACTORY = lambda: adapter
    request = json.loads(sys.stdin.read())
    envelope, exit_code = run_request(
        request,
        pr_run.handle,
        name=pr_run.SKILL_NAME,
        version=pr_run.SKILL_VERSION,
    )
    print(json.dumps({"envelope": envelope, "fixture_calls": adapter.calls},
                     separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
