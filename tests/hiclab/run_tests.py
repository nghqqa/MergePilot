#!/usr/bin/env python3
"""Run all hiclab storage-hardening tests (host Python; no WSL/Docker/MinIO).

Usage:
    python tests/hiclab/run_tests.py
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
loader = unittest.TestLoader()
suite = loader.discover(HERE, pattern="test_*.py", top_level_dir=HERE)
runner = unittest.TextTestRunner(verbosity=2)
result = runner.run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
