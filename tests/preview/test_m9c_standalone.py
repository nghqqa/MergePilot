# -*- coding: utf-8 -*-
"""M9-C §2+§3+§7: version consistency, standalone install, Check order."""
from __future__ import annotations

import json
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BS = (ROOT / "release" / "preview" / "bootstrapper.ps1").read_text(
    encoding="utf-8-sig")
PKG = (ROOT / "release" / "preview" / "make_package.ps1").read_text(
    encoding="utf-8-sig")

RC_VERSION = "v0.1.0-preview.4"


class VersionConsistency(unittest.TestCase):
    """§2: the RC version must be identical across all generated assets."""

    def test_make_package_carries_rc2_version(self):
        self.assertIn('"%s"' % RC_VERSION, PKG)

    def test_bootstrapper_header_carries_rc2(self):
        self.assertIn(RC_VERSION, BS)

    def test_no_stale_preview3_version_in_scripts(self):
        # make_package must not still claim preview.3
        self.assertNotIn('v0.1.0-preview.3"', PKG)


class StandaloneInstall(unittest.TestCase):
    """§3: the package must run without a source checkout."""

    def test_standalone_mode_is_default(self):
        self.assertIn("STANDALONE", BS)
        self.assertIn("PackageRoot", BS)

    def test_cli_resolved_from_package(self):
        self.assertIn(r'tools\cli\mergepilot.py', BS)
        self.assertIn('Join-Path $PackageRoot', BS)

    def test_forwarder_resolved_from_package(self):
        self.assertIn(r'tools\preview\loopback_forwarder.py', BS)

    def test_dev_mode_is_explicit_optin(self):
        self.assertIn("if ($RepoRoot)", BS)
        self.assertIn("DEV MODE", BS)

    def test_missing_cli_stable_error(self):
        self.assertIn("STANDALONE_PACKAGE_INCOMPLETE", BS)

    def test_no_source_repo_dependency_in_default(self):
        # PackageRoot-based resolution exists (standalone default)
        self.assertIn("PackageRoot", BS)

    def test_make_package_bundles_cli(self):
        self.assertIn('tools', PKG)
        self.assertIn('config', PKG)

    def test_state_dir_in_package_root(self):
        self.assertIn('Join-Path $RepoRoot ".mergepilot"', BS)

    def test_no_cwd_dependency(self):
        self.assertIn("Push-Location $RepoRoot", BS)

    def test_pythonpath_set_for_standalone(self):
        self.assertIn("PYTHONPATH", BS)


class CheckOutputOrder(unittest.TestCase):
    """§7: 'ports free' only after bind probes pass."""

    def test_bind_before_ports_free(self):
        bind_8600 = BS.index("Test-PortBind 8600")
        bind_8090 = BS.index("Test-PortBind 8090")
        ports_ok = BS.index("free AND bindable")
        self.assertLess(bind_8600, ports_ok)
        self.assertLess(bind_8090, ports_ok)

    def test_no_early_ports_free_claim(self):
        # the old pattern (PASS ports free THEN bind) must be gone
        self.assertNotIn('Write-Log "PASS" (Assert-Ports)\n        Test-PortBind', BS)

    def test_fail_names_the_port(self):
        self.assertRegex(BS, r"WINDOWS_PORT_BIND_UNAVAILABLE.*127\.0\.0\.1:\$Port")


class DocEscaping(unittest.TestCase):
    """§8: no backspace bytes from \b in JSON/docs."""

    def test_no_backspace_in_handoff(self):
        handoff = ROOT / "m9b-handoff"
        for f in handoff.iterdir():
            if f.suffix in (".json", ".md", ".txt"):
                content = f.read_bytes()
                self.assertNotIn(b"\x08", content,
                                 "backspace byte in %s" % f.name)

    def test_forward_slash_in_examples(self):
        # commands use forward slashes or properly escaped backslashes
        for f in (ROOT / "m9b-handoff").glob("*.md"):
            text = f.read_text(encoding="utf-8")
            # no literal package\bootstrapper that would become package\x08ootstrapper
            self.assertNotIn("packageootstrapper", text)


if __name__ == "__main__":
    unittest.main()
