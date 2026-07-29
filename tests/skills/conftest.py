"""Shared pytest configuration for the M4-A common-runtime contract tests.

Puts the repository root on ``sys.path`` so ``skills.common.*`` imports whether
pytest is launched via ``python -m pytest`` (cwd already on path) or directly.
"""
from __future__ import annotations
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))  # tests/skills -> repo root
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

#: fixed expected pass count; the run harness asserts collected passes == this.
EXPECTED_PASS = 75
