"""Shared pytest config for M4-E CaseRetrieval tests.

Exposes platform-aware expected (passed, skipped) counts so the verification
gate can assert both numbers exactly per platform instead of reading a single
hard-coded integer via regex.
"""
from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

PLATFORM = os.name


def _expected_for(platform):
    """Return ``(EXPECTED_PASS, EXPECTED_SKIP)`` for the given platform.

    Reflects the current collection of 144 tests.  The Windows-only classes
    (TestWindowsCleanupFalsePositives 6 + TestJobObjectTransaction 3 +
    TestJobConfigExceptionTransaction 2 = 11) skip on POSIX; the POSIX-only
    TestPosixRealTreeStub (3) skips on Windows.  Recompute if the collection
    changes.
    """
    if platform == "nt":
        return 166, 3   # 169 collected - 3 POSIX-only skipped
    return 158, 11      # 169 collected - 11 Windows-only skipped


EXPECTED_PASS, EXPECTED_SKIP = _expected_for(os.name)
