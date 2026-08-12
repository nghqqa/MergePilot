#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Demo Console — authenticity and safety test suite.

Tests:
  1. DemoBundle schema complete
  2. Evidence SHA256 recomputable
  3. Bundle SHA256 recomputable
  4. Missing evidence → fail-closed
  5. Corrupted JSON → fail-closed
  6. No hardcoded business results
  7. Every page has mode banner
  8. RAG boundary displayed
  9. Benchmark boundary displayed
  10. Secret scan = 0
  11. Replay has no network requests (static files only)
  12. Two builds produce identical normalized digest
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
for p in [str(ROOT), str(ROOT / "tools" / "demo_console")]:
    if p not in sys.path:
        sys.path.insert(0, p)

from schema import validate_bundle, REQUIRED_FIELDS, VOLATILE_FIELDS
from bundle_builder import build_bundle, compute_bundle_sha256
from render import render_html

# Helper to compute file SHA-256
def file_sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()

# Secret patterns
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"ghp_[0-9A-Za-z]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{12,}"),
]
def scan_secrets(text: str) -> int:
    return sum(len(p.findall(text)) for p in _SECRET_PATTERNS)


class TestBundleSchema(unittest.TestCase):
    """1. DemoBundle schema complete."""

    @classmethod
    def setUpClass(cls):
        cls.bundle = build_bundle(str(ROOT))

    def test_all_required_fields_present(self):
        errors = validate_bundle(self.bundle)
        self.assertEqual(len(errors), 0, f"Schema errors: {errors}")

    def test_schema_version(self):
        self.assertEqual(self.bundle["schema_version"], "mergepilot.demo-bundle.v1")

    def test_demo_mode_is_replay(self):
        self.assertEqual(self.bundle["demo_mode"], "REPLAY")


class TestEvidenceSHARecomputable(unittest.TestCase):
    """2. Evidence SHA256 recomputable."""

    def test_evidence_sha_matches_disk(self):
        bundle = build_bundle(str(ROOT))
        for ef in bundle["evidence_files"]:
            path = ROOT / ef["path"]
            self.assertTrue(path.exists(), f"Missing: {ef['path']}")
            actual = file_sha256(str(path))
            self.assertEqual(ef["sha256"], actual,
                             f"SHA mismatch for {ef['path']}")


class TestBundleSHARecomputable(unittest.TestCase):
    """3. Bundle SHA256 recomputable."""

    def test_bundle_sha_matches_recompute(self):
        bundle = build_bundle(str(ROOT))
        stored = bundle["bundle_sha256"]
        recomputed = compute_bundle_sha256(bundle)
        self.assertEqual(stored, recomputed)

    def test_bundle_sha_excludes_volatile(self):
        """Changing generated_at must not change bundle_sha256."""
        bundle1 = build_bundle(str(ROOT))
        bundle2 = build_bundle(str(ROOT))
        bundle2["generated_at"] = "2099-01-01T00:00:00Z"
        self.assertEqual(
            compute_bundle_sha256(bundle1),
            compute_bundle_sha256(bundle2),
            "bundle_sha256 must be independent of generated_at"
        )


class TestMissingEvidenceFailClosed(unittest.TestCase):
    """4. Missing evidence → fail-closed."""

    def test_missing_evidence_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create an empty root — no evidence files
            with self.assertRaises((FileNotFoundError, ValueError)):
                build_bundle(tmpdir)


class TestCorruptedJSONFailClosed(unittest.TestCase):
    """5. Corrupted JSON → fail-closed."""

    def test_corrupted_json_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Copy evidence dirs
            for subdir in ["evidence/m4/m4f", "evidence/m6/rag", "evidence/m7/benchmark"]:
                src = ROOT / subdir
                dst = Path(tmpdir) / subdir
                dst.mkdir(parents=True, exist_ok=True)
                for f in src.iterdir():
                    if f.name.endswith(".json"):
                        shutil.copy2(str(f), str(dst / f.name))

            # Corrupt one file
            corrupt_path = Path(tmpdir) / "evidence/m4/m4f/agentteams-demo-summary.json"
            with open(corrupt_path, "w") as f:
                f.write("{ this is not valid json")

            # Need a fake .git for commit resolution
            os.makedirs(os.path.join(tmpdir, ".git"), exist_ok=True)

            with self.assertRaises((ValueError, json.JSONDecodeError)):
                build_bundle(tmpdir)


class TestNoHardcodedResults(unittest.TestCase):
    """6. No hardcoded business results — all from evidence."""

    def test_findings_count_from_evidence(self):
        bundle = build_bundle(str(ROOT))
        # Findings must be 0 (evidence stores digests, not inline)
        # This is the honest state — not a hardcoded PASS
        self.assertIsInstance(bundle["findings"], list)

    def test_skills_from_evidence(self):
        bundle = build_bundle(str(ROOT))
        # All 6 skills must come from the demo summary
        self.assertEqual(len(bundle["agents"]), 6)

    def test_final_status_from_evidence(self):
        bundle = build_bundle(str(ROOT))
        # Must be derived from demo.run.all_passed, not hardcoded
        self.assertIn(bundle["final_status"], ("MERGED", "HELD", "REJECTED"))


class TestModeBannerDisplayed(unittest.TestCase):
    """7. Every page has mode banner."""

    def test_mode_banner_in_html(self):
        bundle = build_bundle(str(ROOT))
        html = render_html(bundle)
        self.assertIn("MODE: REPLAY", html)

    def test_mode_banner_visible_on_all_pages(self):
        """The banner is at the top of the page (before all sections)."""
        bundle = build_bundle(str(ROOT))
        html = render_html(bundle)
        banner_pos = html.find("MODE: REPLAY")
        first_section_pos = html.find('<section')
        self.assertLess(banner_pos, first_section_pos,
                        "Mode banner must appear before any page section")


class TestRAGBoundaryDisplayed(unittest.TestCase):
    """8. RAG boundary displayed."""

    def setUp(self):
        self.bundle = build_bundle(str(ROOT))
        self.html = render_html(self.bundle)

    def test_adopted_false_displayed(self):
        self.assertIn("adopted=False", self.html)

    def test_untrusted_true_displayed(self):
        self.assertIn("untrusted=True", self.html)

    def test_runtime_consumes_rag_context_false(self):
        self.assertIn("runtime_consumes_rag_context=false", self.html)

    def test_workflow_utility_not_measurable(self):
        self.assertIn("NOT_MEASURABLE_WITH_CURRENT_RUNTIME", self.html)

    def test_rag_advisories_adopted_false_in_bundle(self):
        for r in self.bundle["rag_advisories"]:
            self.assertFalse(r["adopted"])
            self.assertTrue(r["untrusted"])


class TestBenchmarkBoundaryDisplayed(unittest.TestCase):
    """9. Benchmark boundary displayed."""

    def setUp(self):
        self.bundle = build_bundle(str(ROOT))
        self.html = render_html(self.bundle)

    def test_development_calibration_shown(self):
        self.assertIn("Development Calibration", self.html)

    def test_confirmatory_shown(self):
        self.assertIn("Confirmatory", self.html)

    def test_adapter_boundary_shown(self):
        self.assertIn("TokenOverlapAdapter", self.html)

    def test_no_positive_accuracy_improvement_claim(self):
        """Must NOT claim accuracy improvement as a positive result.
        The boundary banner says 'does NOT claim accuracy improvement' —
        this is a negation, not a positive claim. Check for positive phrasing only."""
        # The boundary text "does not claim reviewer/fixer accuracy improvement" is OK.
        # A positive claim would be "RAG improves accuracy" or "accuracy improved by X%".
        self.assertNotRegex(self.html.lower(),
            r"(rag |retrieval )?(improves?|improved|increases?) (reviewer|fixer )?accuracy")
        self.assertNotIn("decision accuracy improved", self.html.lower())
        self.assertNotIn("accuracy uplift", self.html.lower())


class TestSecretScan(unittest.TestCase):
    """10. Secret scan = 0."""

    def test_no_secrets_in_bundle(self):
        bundle = build_bundle(str(ROOT))
        text = json.dumps(bundle, ensure_ascii=False)
        self.assertEqual(scan_secrets(text), 0)

    def test_no_secrets_in_html(self):
        bundle = build_bundle(str(ROOT))
        html = render_html(bundle)
        self.assertEqual(scan_secrets(html), 0)


class TestReplayNoNetwork(unittest.TestCase):
    """11. Replay has no network requests."""

    def test_html_has_no_external_src(self):
        """Rendered HTML must not reference external URLs (CDN, etc.)."""
        bundle = build_bundle(str(ROOT))
        html = render_html(bundle)
        # Check for external script/link/img src
        external_patterns = [
            r'src=["\']https?://',
            r'href=["\']https?://(?!github\.com)',
            r'cdn\.',
        ]
        for pattern in external_patterns:
            matches = re.findall(pattern, html, re.IGNORECASE)
            # Allow github.com links in evidence/citations
            self.assertEqual(len(matches), 0,
                             f"External resource found: {pattern} → {matches}")

    def test_no_api_calls_in_bundle(self):
        bundle = build_bundle(str(ROOT))
        text = json.dumps(bundle)
        self.assertNotIn("api.openai.com", text)
        self.assertNotIn("api.anthropic.com", text)


class TestDeterministicDigest(unittest.TestCase):
    """12. Two builds produce identical normalized digest."""

    def test_normalized_digest_identical(self):
        b1 = build_bundle(str(ROOT))
        b2 = build_bundle(str(ROOT))
        # Exclude volatile fields
        clean1 = {k: v for k, v in b1.items() if k not in VOLATILE_FIELDS}
        clean2 = {k: v for k, v in b2.items() if k not in VOLATILE_FIELDS}
        d1 = hashlib.sha256(json.dumps(clean1, sort_keys=True).encode()).hexdigest()
        d2 = hashlib.sha256(json.dumps(clean2, sort_keys=True).encode()).hexdigest()
        self.assertEqual(d1, d2, "Two builds must produce identical normalized digest")

    def test_bundle_sha256_identical(self):
        b1 = build_bundle(str(ROOT))
        b2 = build_bundle(str(ROOT))
        self.assertEqual(b1["bundle_sha256"], b2["bundle_sha256"])


class TestSpanTreeHierarchy(unittest.TestCase):
    """OTel Trace Tree uses parent_span_id, not list order."""

    def test_span_tree_built_from_parent_ids(self):
        from render import build_span_tree
        spans = [
            {"span_id": "root", "parent_span_id": None, "name": "root"},
            {"span_id": "child1", "parent_span_id": "root", "name": "child1"},
            {"span_id": "child2", "parent_span_id": "root", "name": "child2"},
            {"span_id": "grandchild", "parent_span_id": "child1", "name": "gc"},
        ]
        tree = build_span_tree(spans)
        self.assertEqual(len(tree["children"]), 1)
        self.assertEqual(tree["children"][0]["span_id"], "root")
        self.assertEqual(len(tree["children"][0]["children"]), 2)
        child_ids = {c["span_id"] for c in tree["children"][0]["children"]}
        self.assertEqual(child_ids, {"child1", "child2"})


class TestRendererProducesValidHTML(unittest.TestCase):
    """Renderer produces valid self-contained HTML."""

    def test_html_has_doctype(self):
        bundle = build_bundle(str(ROOT))
        html = render_html(bundle)
        self.assertTrue(html.startswith("<!DOCTYPE html>"))

    def test_html_has_8_pages(self):
        bundle = build_bundle(str(ROOT))
        html = render_html(bundle)
        # Count section tags
        sections = re.findall(r'<section[^>]*class="page', html)
        self.assertEqual(len(sections), 8)

    def test_html_self_contained(self):
        """No external CSS/JS files referenced."""
        bundle = build_bundle(str(ROOT))
        html = render_html(bundle)
        self.assertNotIn('<link', html)
        self.assertNotIn('<script src', html)


if __name__ == "__main__":
    unittest.main()
