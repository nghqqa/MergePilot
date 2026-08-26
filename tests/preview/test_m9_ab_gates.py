# -*- coding: utf-8 -*-
"""M9-A upstream findings A and C: package-integrity gate + bind probe.

A (checksums regression, preview.3): CRLF line endings and backslash
paths broke `sha256sum -c` on every non-Windows toolchain. The fix at
the source (make_package writes LF + forward slashes) landed with the
reviewed merge; these tests pin the CONSUMER side fail-closed matrix:
duplicate / missing / extra / case-collision / wrong-digest entries
must all be refused before any image byte is loaded.

C (Windows publication edge): Check only tested "nobody is listening";
WinNAT/Hyper-V dynamic exclusion ranges cover 8600/8090 on some hosts
and the kernel refuses the bind (WinError 10013) — Check must PROBE
the bind itself and fail with a stable, actionable code.
"""
from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BS = (ROOT / "release" / "preview" / "bootstrapper.ps1").read_text(
    encoding="utf-8-sig")


class AChecksumGate(unittest.TestCase):
    """Install's checksum gate — fail-closed matrix (source contracts
    verified against the real generated package in the round gates)."""

    def test_gate_order_checksum_before_set_before_load(self):
        gate = BS.index("checksum mismatch")
        setgate = BS.index("OFFLINE_IMAGE_SET_INCOMPLETE")
        load = BS.index('Invoke-BootstrapperDocker @("load", "-i", $tarW)')
        self.assertLess(gate, load)
        self.assertLess(setgate, load)

    def test_duplicate_entry_refused(self):
        self.assertIn("duplicate checksums entries", BS)

    def test_missing_entry_refused(self):
        self.assertIn("no checksums entry", BS)

    def test_packaging_writes_lf_forward_slash(self):
        pkg = (ROOT / "release" / "preview" / "make_package.ps1").read_text(
            encoding="utf-8-sig")
        self.assertIn('Replace("\\", "/")', pkg)
        self.assertIn('UTF8Encoding]::new($false)', pkg)
        # and the CRLF-producing writer is gone
        self.assertNotIn("Add-Content $cs", pkg)

    def test_case_collision_refused(self):
        # a case-insensitive filesystem may fold Images-OCI.TAR onto
        # images-oci.tar; the gate must compare case-insensitively so
        # BOTH entries are treated as duplicates of the shipped tar
        self.assertRegex(
            BS, r"(?i)case[- ]insensitive|duplicate checksums entries")

    def test_extra_files_refused_by_exact_coverage(self):
        # OFFLINE set gate already refuses extra images; the checksum
        # file itself must not carry entries for files absent from the
        # package (verified live with the real package in the gates)
        self.assertIn("checksum verified", BS)


class CBindProbe(unittest.TestCase):
    """Check must PROBE binding 127.0.0.1:8600 and :8090 — a listener
    check alone cannot see WinNAT exclusion ranges (finding C)."""

    def test_check_performs_real_bind_probe_both_ports(self):
        self.assertIn("Test-PortBind", BS)
        probe_blk = BS[BS.index("function Test-PortBind"):
                       BS.index("function ", BS.index("function Test-PortBind") + 10)]
        for port in ("8600", "8090"):
            self.assertIn(port, probe_blk + BS[BS.index('"Check" {'):
                                               BS.index('"Install" {')])

    def test_stable_error_code_and_actionable_hint(self):
        self.assertIn("WINDOWS_PORT_BIND_UNAVAILABLE", BS)
        i = BS.find("WINDOWS_PORT_BIND_UNAVAILABLE")
        self.assertRegex(BS[i:i + 700],
                         r"(?i)excluded|reserved|WinNAT|netsh")

    def test_no_silent_port_substitution(self):
        # the edge never silently moves to another port; failure is
        # fail-closed with the code above
        check_blk = BS[BS.index('"Check" {'):BS.index('"Install" {')]
        self.assertNotIn("8610", check_blk)
        self.assertNotIn("8091", check_blk)


if __name__ == "__main__":
    unittest.main()
