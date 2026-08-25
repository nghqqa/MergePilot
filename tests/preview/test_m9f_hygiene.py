# -*- coding: utf-8 -*-
"""M9-F §2+§3: package cache hygiene + Cleanup install.json timing."""
from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PKG = (ROOT / "release" / "preview" / "make_package.ps1").read_text(
    encoding="utf-8-sig")
BS = (ROOT / "release" / "preview" / "bootstrapper.ps1").read_text(
    encoding="utf-8-sig")


class PackageHygiene(unittest.TestCase):
    """§2: cache artifacts must be recursively stripped from the ZIP."""

    def test_cache_strip_exists(self):
        self.assertIn("$CacheDirs", PKG)
        self.assertIn("__pycache__", PKG)
        self.assertIn(".pytest_cache", PKG)
        self.assertIn(".mypy_cache", PKG)
        self.assertIn(".ruff_cache", PKG)

    def test_cache_file_exclusions(self):
        for pat in ("*.pyc", "*.pyo", ".coverage"):
            self.assertIn(pat, PKG)

    def test_fail_closed_on_leftover(self):
        self.assertIn("PACKAGE_HYGIENE_FAILED", PKG)

    def test_empty_dir_removal(self):
        self.assertIn("emptyDirs", PKG)

    def test_hygiene_log_line(self):
        self.assertIn("package hygiene: 0 cache artifacts", PKG)

    def test_full_tools_still_copied(self):
        self.assertIn('Copy-Item (Join-Path $RepoRoot "tools")', PKG)
        self.assertIn("-Recurse", PKG)

    def test_config_still_copied(self):
        self.assertIn("config\\gh-app", PKG)

    def test_no_selective_exclude_bypass(self):
        # the old -Exclude only filtered top level — must be gone
        self.assertNotIn('-Exclude "__pycache__","*.pyc"', PKG)


class CleanupInstallJsonTiming(unittest.TestCase):
    """§3: manifest state recorded BEFORE cleanup, three-way report."""

    def test_manifest_state_recorded_before(self):
        self.assertIn("$manifestExisted", BS)
        self.assertIn("BEFORE cleanup", BS)

    def test_present_and_consumed_message(self):
        cleanup_blk = BS[BS.index('"Cleanup" {'):]
        self.assertIn("install manifest removed", cleanup_blk)
        self.assertIn("was present and consumed", cleanup_blk)

    def test_absent_from_start_message(self):
        cleanup_blk = BS[BS.index('"Cleanup" {'):]
        self.assertIn("no install manifest found from the start", cleanup_blk)
        self.assertIn("ownership-sensitive image cleanup skipped", cleanup_blk)
        self.assertIn("fail-closed", cleanup_blk)

    def test_unparseable_manifest_message(self):
        cleanup_blk = BS[BS.index('"Cleanup" {'):]
        self.assertIn("present but unparseable", cleanup_blk)
        self.assertIn("ownership not assumed", cleanup_blk)

    def test_parse_check_exists(self):
        cleanup_blk = BS[BS.index('"Cleanup" {'):]
        self.assertIn("ConvertFrom-Json", cleanup_blk)
        self.assertIn("$manifestParsed", cleanup_blk)

    def test_no_ambiguous_no_json_found(self):
        cleanup_blk = BS[BS.index('"Cleanup" {'):]
        # the OLD ambiguous wording must be gone
        self.assertNotIn("no install.json found - no owned image manifest",
                         cleanup_blk)

    def test_per_resource_lines_still_present(self):
        cleanup_blk = BS[BS.index('"Cleanup" {'):]
        for line in ("session containers/networks", "keepalive", "forwarder",
                     "ports 8600/8090"):
            self.assertIn(line, cleanup_blk)

    def test_no_bare_cleaned(self):
        cleanup_blk = BS[BS.index('"Cleanup" {'):]
        self.assertNotIn('Write-Log "OK" "cleaned"', cleanup_blk)


if __name__ == "__main__":
    unittest.main()
