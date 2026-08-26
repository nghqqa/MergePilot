# -*- coding: utf-8 -*-
"""M9 finding E: path-traversal coverage in sast-scan and risk-classify.

The controlled real PR (MergePilot-Demo PR#1/PR#2, download() joining
an unvalidated name under BASE_DIR) produced ZERO sast findings and a
bare L1 risk classification. These tests pin the fix: a generic
taint-style rule (function names NEVER matched — the pattern is
join(base, untrusted) flowing into open() without a resolve/prefix
guard), plus a risk-classify rule that recognizes a NEW file-content
read/download surface in the change.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]

TRAV_OK_BASE = '''
import os
BASE = os.path.dirname(os.path.abspath(__file__))
def read(name):
    candidate = os.path.realpath(os.path.join(BASE, name))
    if not candidate.startswith(BASE + os.sep):
        raise ValueError("escapes")
    with open(candidate) as fh:
        return fh.read()
'''

TRAV_VULN_JOIN = '''
import os
BASE = "/srv/data"
def download(name):
    path = os.path.join(BASE, name)
    with open(path, "rb") as fh:
        return fh.read()
'''

TRAV_VULN_CONCAT = '''
BASE = "/srv/data"
def fetch(n):
    f = open(BASE + "/" + n, "rb")
    return f.read()
'''

TRAV_VULN_ABS = '''
def load(p):
    with open(p) as fh:
        return fh.read()
'''

TRAV_VULN_ENCODED = '''
import os, urllib.parse
BASE = "/srv/data"
def get(raw):
    name = urllib.parse.unquote(raw)
    with open(os.path.join(BASE, name)) as fh:
        return fh.read()
'''

SAFE_SAME_DIR = '''
def read_local():
    with open("config.json") as fh:
        return fh.read()
'''

SAFE_BASENAME = '''
import os
BASE = "/srv/data"
def read(name):
    with open(os.path.join(BASE, os.path.basename(name))) as fh:
        return fh.read()
'''


def _scan(code: str, tmpdir: pathlib.Path):
    f = tmpdir / "svc.py"
    f.write_text(code, encoding="utf-8")
    sys.path.insert(0, str(ROOT / "skills" / "sast_scan"))
    try:
        import importlib
        eng = importlib.import_module("skills.sast_scan.engines.ast_python")
        rules = json.loads(
            (ROOT / "skills" / "sast_scan" / "rules" /
             "sast-rules.v1.json").read_text(encoding="utf-8"))
        import importlib.util
        import shutil
        t = tmpdir / "iso"
        shutil.copytree(ROOT / "skills" / "sast_scan", t,
                        ignore=shutil.ignore_patterns("__pycache__"))
        spec = importlib.util.spec_from_file_location(
            "iso_ast", t / "engines" / "ast_python.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        findings, _syntax_err = mod.scan("svc.py", code, rules["ast_rules"])
        return findings
    finally:
        sys.path.pop(0)


class PathTraversalSast(unittest.TestCase):
    def _assert_flagged(self, code, why):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            findings = _scan(code, pathlib.Path(td))
        ids = [f["rule_id"] for f in findings]
        self.assertIn("AST_PATH_TRAVERSAL", ids, why)

    def _assert_clean(self, code, why):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            findings = _scan(code, pathlib.Path(td))
        ids = [f["rule_id"] for f in findings]
        self.assertNotIn("AST_PATH_TRAVERSAL", ids, why)

    def test_vulnerable_join_flagged(self):
        self._assert_flagged(TRAV_VULN_JOIN, "join(base, untrusted)->open")

    def test_vulnerable_concat_flagged(self):
        self._assert_flagged(TRAV_VULN_CONCAT, "base+str concat -> open")

    def test_vulnerable_absolute_flagged(self):
        self._assert_flagged(TRAV_VULN_ABS, "open(untrusted) directly")

    def test_vulnerable_encoded_flagged(self):
        self._assert_flagged(TRAV_VULN_ENCODED, "decoded input into join->open")

    def test_safe_realpath_guard_clean(self):
        self._assert_clean(TRAV_OK_BASE, "realpath+prefix guard is safe")

    def test_safe_literal_clean(self):
        self._assert_clean(SAFE_SAME_DIR, "literal open is safe")

    def test_safe_basename_clean(self):
        self._assert_clean(SAFE_BASENAME, "basename confinement is safe")

    def test_rule_not_function_name_matched(self):
        rules = json.loads(
            (ROOT / "skills" / "sast_scan" / "rules" /
             "sast-rules.v1.json").read_text(encoding="utf-8"))
        rule = next(r for r in rules["ast_rules"]
                    if r["rule_id"] == "AST_PATH_TRAVERSAL")
        self.assertEqual(rule["kind"], "taint_path_join")
        self.assertNotIn("download", json.dumps(rule))
        self.assertNotIn("fileservice", json.dumps(rule))


class RiskClassifyFileSurface(unittest.TestCase):
    def _classify(self, patch_body: str, path="fileservice.py"):
        sys.path.insert(0, str(ROOT))
        try:
            import importlib
            rc = importlib.import_module("skills.risk_classify.core")
            ctx = {
                "complete": True,
                "files": [{"path": path, "change_type": "M",
                           "categories": ["source"],
                           "additions": 12, "deletions": 0, "hunks": [[]],
                           "binary": False}],
                "stats": {"files_changed": 1, "additions": 12,
                          "deletions": 0, "hunks": 1, "binary_files": 0},
                "change_categories": ["source"],
                "patch_by_file": {path: patch_body},
            }
            return rc.classify(ctx, risk_floor="L0")
        finally:
            sys.path.pop(0)

    def test_new_download_endpoint_raises_classification(self):
        patch = ("+def download(name: str) -> bytes:\n"
                 '+    path = os.path.join(BASE_DIR, name)\n'
                 '+    with open(path, "rb") as fh:\n'
                 "+        return fh.read()\n")
        out = self._classify(patch)
        rules_hit = out.get("matched_rules", [])
        self.assertIn("FILE_CONTENT_SURFACE", rules_hit,
                      "a NEW file-content read/download endpoint must be "
                      "flagged; matched: %r" % rules_hit)
        self.assertGreaterEqual(out.get("risk_rank", 0), 1)

    def test_plain_change_not_flagged(self):
        patch = "+x = 1\n+y = compute(x)\n"
        out = self._classify(patch)
        self.assertNotIn("FILE_CONTENT_SURFACE",
                         out.get("matched_rules", []))


if __name__ == "__main__":
    unittest.main()
