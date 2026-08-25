# -*- coding: utf-8 -*-
"""M9-D §2+§3+§4: standalone Doctor dual-mode, Install order, Cleanup text."""
from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BS = (ROOT / "release" / "preview" / "bootstrapper.ps1").read_text(
    encoding="utf-8-sig")
MP = (ROOT / "tools" / "cli" / "mergepilot.py").read_text(encoding="utf-8")


class StandaloneDoctorDualMode(unittest.TestCase):
    """§2: Doctor must not fail on missing source-build files in
    offline-install mode; must check them only with --build-from-source."""

    def test_doctor_has_build_from_source_flag(self):
        self.assertIn("--build-from-source", MP)
        # the flag is on the doctor subparser
        i = MP.index('sub.add_parser("doctor"')
        blk = MP[i:i+500]
        self.assertIn("--build-from-source", blk)

    def test_doctor_distinguishes_runtime_from_source_build(self):
        self.assertIn("_runtime_files", MP)
        self.assertIn("_source_build_files", MP)
        self.assertIn("source_build", MP)

    def test_offline_mode_does_not_require_dockerfiles(self):
        # in offline mode, Dockerfile.* files are NOT in the required list
        self.assertIn("if source_build", MP)
        self.assertIn("_source_build_files if source_build else []", MP)

    def test_offline_mode_check_is_runtime_layout(self):
        self.assertIn("runtime_layout", MP)
        self.assertIn("DOCTOR_RUNTIME_OK", MP)
        self.assertIn("DOCTOR_RUNTIME_MISSING", MP)

    def test_source_build_mode_has_clear_label(self):
        self.assertIn("SOURCE-BUILD mode", MP)
        self.assertIn("DOCTOR_LAYOUT_MISSING", MP)

    def test_source_only_advisory_in_offline(self):
        self.assertIn("DOCTOR_LAYOUT_SOURCE_ONLY", MP)
        self.assertIn("source-build files not required", MP)

    def test_install_status_check_exists(self):
        self.assertIn("install_status", MP)
        self.assertIn("DOCTOR_INSTALLED", MP)
        self.assertIn("DOCTOR_NOT_INSTALLED", MP)

    def test_install_status_message_actionable(self):
        self.assertIn("run bootstrapper Install", MP)


class InstallOrderFix(unittest.TestCase):
    """§3: install.json must be written BEFORE doctor verification."""

    def test_install_json_before_doctor(self):
        install_write = BS.index("install manifest written")
        doctor_call = BS.index('Invoke-Cli @("doctor")')
        # the doctor call inside Install must come after the write
        install_blk = BS[BS.index('"Install" {'):BS.index('"Start" {')]
        write_pos = install_blk.index("install manifest written")
        doctor_pos = install_blk.index('Invoke-Cli @("doctor")')
        self.assertLess(write_pos, doctor_pos,
                        "install.json write must precede doctor in Install")

    def test_install_json_atomic_write(self):
        self.assertIn("install.current.tmp", BS)
        self.assertIn("[System.IO.File]::Copy($tmp, $inst, $true)", BS)

    def test_no_half_written_install_on_failure(self):
        # the tmp file pattern ensures atomicity
        self.assertIn("Remove-Item $tmp", BS)


class CleanupMessaging(unittest.TestCase):
    """§4: Cleanup must report per-resource, not a bare 'cleaned'."""

    def test_no_bare_cleaned_message(self):
        cleanup_blk = BS[BS.index('"Cleanup" {'):]
        self.assertNotIn('Write-Log "OK" "cleaned"', cleanup_blk)

    def test_per_resource_lines_present(self):
        cleanup_blk = BS[BS.index('"Cleanup" {'):]
        for line in ("session containers/networks", "keepalive", "forwarder",
                     "ports 8600/8090"):
            self.assertIn(line, cleanup_blk)

    def test_no_owner_record_explains_retention(self):
        cleanup_blk = BS[BS.index('"Cleanup" {'):]
        self.assertIn("no install.json found", cleanup_blk)
        self.assertIn("fail-closed", cleanup_blk)
        self.assertIn("images retained", cleanup_blk)

    def test_owned_manifest_removal_reported(self):
        cleanup_blk = BS[BS.index('"Cleanup" {'):]
        self.assertIn("install.json removed", cleanup_blk)

    def test_cleanup_complete_summary_line(self):
        cleanup_blk = BS[BS.index('"Cleanup" {'):]
        self.assertIn("cleanup complete", cleanup_blk)
        self.assertIn("per-resource", cleanup_blk)


if __name__ == "__main__":
    unittest.main()
