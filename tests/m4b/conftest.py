"""Shared pytest configuration for the M4-B test suite.

Puts the repository root on ``sys.path`` so ``skills.*`` imports resolve, and
exposes a fixed ``EXPECTED_PASS`` constant that the gate runner asserts the
collected pass count equals. The constant is a single source of truth -- it is
never derived dynamically from collection.
"""
from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))  # tests/m4b -> repo root
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

#: fixed expected pass count; the run harness asserts collected passes == this.
EXPECTED_PASS = 96
