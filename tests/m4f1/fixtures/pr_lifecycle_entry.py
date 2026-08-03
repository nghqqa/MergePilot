#!/usr/bin/env python3
"""PRLifecycle fixture entry using the deterministic Policy Gateway model."""

from skills.pr_lifecycle import run as skill_run
from tests.m4d.conftest import FakeAdapter


def main() -> int:
    skill_run._ADAPTER_FACTORY = FakeAdapter
    return skill_run.main()


if __name__ == "__main__":
    raise SystemExit(main())
