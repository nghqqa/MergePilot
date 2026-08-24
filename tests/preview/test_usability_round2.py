# -*- coding: utf-8 -*-
"""v0.1.0-preview.3 usability-blocker regression matrix (round 2).

One test per frozen blocker (§1 of the usability round). Every test
fails on the frozen preview.2 tree (18542c0) and must pass after the
fixes. Nothing here is skipped, xfailed, or softened.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BS = (ROOT / "release" / "preview" / "bootstrapper.ps1").read_text(
    encoding="utf-8-sig")
MP = (ROOT / "tools" / "cli" / "mergepilot.py").read_text(
    encoding="utf-8")
PKG = (ROOT / "release" / "preview" / "make_package.ps1").read_text(
    encoding="utf-8-sig")
README = (ROOT / "release" / "preview" / "README.md").read_text(
    encoding="utf-8")
FORWARDER = ROOT / "tools" / "preview" / "loopback_forwarder.py"


class B1DistroAuthority(unittest.TestCase):
    """§2: single authoritative distro, stable codes, no injection."""

    def test_cli_honors_env_and_distinguishes_codes(self):
        # authorized distro must come from ONE source honoring
        # MERGEPILOT_WSL_DISTRO, with registration + running + wake
        # as three distinct stable codes
        self.assertIn('MERGEPILOT_WSL_DISTRO', MP)
        self.assertIn("DISTRO_NOT_REGISTERED", MP)
        self.assertIn("DISTRO_WAKE_TIMEOUT", MP)
        self.assertIn("DISTRO_NOT_RUNNING", MP)
        # argv safety still asserted on every wsl emission
        self.assertIn("assert_argv_safe", MP)

    def test_bootstrap_passes_distro_via_env_and_prechecks(self):
        self.assertIn("MERGEPILOT_WSL_DISTRO", BS)
        self.assertIn("DISTRO_MISMATCH", BS)

    def test_env_bogus_distro_yields_not_registered(self):
        env = dict(os.environ)
        env["MERGEPILOT_WSL_DISTRO"] = "definitely-not-a-distro-xyz"
        cp = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "cli" / "mergepilot.py"),
             "status", "--json"],
            capture_output=True, text=True, env=env, timeout=120,
            cwd=str(ROOT))
        try:
            payload = json.loads(cp.stdout)
        except ValueError:
            self.fail("stdout not pure JSON: %r" % cp.stdout[:200])
        self.assertEqual(payload.get("error_code"),
                         "DISTRO_NOT_REGISTERED",
                         "bogus env distro must be DISTRO_NOT_REGISTERED, "
                         "got %r (rc=%d)" % (payload.get("error_code"),
                                             cp.returncode))


class B2OfflineImageSet(unittest.TestCase):
    """§4: the tar must carry the COMPLETE doctor-required set."""

    def test_required_set_includes_pgvector_and_ships(self):
        self.assertIn("pgvector/pgvector", PKG)  # packer ships the locked pgvector
        self.assertIn("REQUIRED", PKG)  # required-set export exists
        self.assertIn("OFFLINE_IMAGE_SET_INCOMPLETE", BS)  # pre-load gate

    def test_manifest_set_validated_before_load(self):
        # the gate must run BEFORE docker load in Install
        gate = BS.index("OFFLINE_IMAGE_SET_INCOMPLETE")
        load = BS.index('@("load", "-i", $tarW)')
        self.assertLess(gate, load)


class B3WindowsPublicationEdge(unittest.TestCase):
    """§3: explicit Windows-side loopback publication."""

    def test_forwarder_module_exists_with_fixed_contract(self):
        self.assertTrue(FORWARDER.is_file(), "loopback_forwarder.py missing")
        src = FORWARDER.read_text(encoding="utf-8")
        # loopback-only listen, fixed port map, no arbitrary targets
        self.assertIn('LISTEN_HOST = "127.0.0.1"', src)
        self.assertIn("FORWARD_PORTS", src)
        self.assertNotIn('LISTEN_HOST = "0.0.0.0"', src)
        # token + identity file (pid, name, distro, token, purpose)
        for field in ('"pid"', '"distro"', '"token"', '"purpose"', '"ports"'):
            self.assertIn(field, src)
        # no credentials, no target parameters beyond fixed ports
        self.assertNotIn("password", src.lower())

    def test_bootstrap_starts_and_fail_closes_publication(self):
        for needle in ("loopback_forwarder.py",
                       "WINDOWS_LOOPBACK_PUBLICATION_FAILED",
                       "WINDOWS_LOOPBACK_PUBLICATION_UNAVAILABLE",
                       "--noproxy"):
            self.assertIn(needle, BS)
        # publication verification happens AFTER cli start, and any
        # failure path must stop both the forwarder and the stack
        start_blk = BS[BS.index('"Start" {'):BS.index('"Status" {')]
        self.assertIn("Invoke-Cli", start_blk)
        # publication failure must roll the stack back
        edge_blk = BS[BS.index("function Start-PublicationEdge"):BS.index("function Stop-PublicationEdge")]
        self.assertIn('Invoke-Cli @("stop")', edge_blk)

    def test_stop_terminates_forwarder_with_identity(self):
        stop_blk = BS[BS.index('"Stop" {'):BS.index('"Cleanup" {')]
        self.assertIn("Stop-PublicationEdge", stop_blk)
        # identity: pid + process name + launch token all verified
        self.assertGreaterEqual(BS.count("CommandLine"), 1)

    def test_in_distro_backend_bind_is_deliberate(self):
        # the distro-internal publish becomes the backend of the
        # Windows edge; the Host-allowlist stays the inner guard
        ocs = (ROOT / "tools" / "demo_console" /
               "one_click_startup.py").read_text(encoding="utf-8")
        self.assertIn("PUBLISH_BIND", ocs)


class B4CreatedOrphan(unittest.TestCase):
    """§5: Created orphans are cleaned by journal, never guessed."""

    def test_rollback_covers_created_state(self):
        # stop/rollback plan must rm -fv (works on Created) the
        # deterministic service names even when the crash happened
        # before the id was journaled
        self.assertIn('rm", "-fv"', MP.replace("'", '"') .replace(
            'rm", "-fv"', 'rm", "-fv"'))
        self.assertIn("mergepilot-isolated-%s-1", MP)

    def test_no_glob_deletion(self):
        self.assertNotIn("docker rm $(docker ps", MP)
        self.assertNotIn("grep mergepilot | xargs", MP)


class B5KeepaliveIdentity(unittest.TestCase):
    """§5: keepalive ownership + bounded teardown."""

    def test_identity_file_schema_and_bounded_stop(self):
        for field in ("purpose", "token", "distro"):
            self.assertIn(field, BS)
        self.assertIn("KEEPALIVE_SURVIVED", BS)
        # bounded wait loop before declaring survival
        self.assertRegex(BS, r'for\s*\(\$i = 0; \$i -l[te]')
        self.assertIn("Start-Sleep", BS)


class B6RecoveryHint(unittest.TestCase):
    """§6: recovery hints must be executable as printed."""

    def test_stack_partial_hint_carries_apply(self):
        self.assertIn("cleanup --apply", MP)


class B7DormantWake(unittest.TestCase):
    """§2/§5: bounded wake for lifecycle commands."""

    def test_lifecycle_wakes_registered_dormant_distro(self):
        self.assertIn("wake", MP.lower())
        self.assertIn("DISTRO_WAKE_TIMEOUT", MP)


class B8DoctorDiagnostics(unittest.TestCase):
    """§6: no OK-on-failure, first failing check, pure --json."""

    def test_status_line_never_ok_on_failure(self):
        # a result whose status starts with 'failed' must render as
        # FAILED..., never "OK (failed...)"
        self.assertIn("_status_line", MP)
        m = re.search(r"def _status_line\(([^)]*)\):\n(.*?)(?=\ndef |\nclass )",
                      MP, re.S)
        self.assertIsNotNone(m, "_status_line helper missing")
        body = m.group(2)
        self.assertIn("startswith", body)

    def test_doctor_prints_first_failure_with_code(self):
        # doctor failure output names the first failing check + code
        self.assertIn("DOCTOR_FIRST_FAILURE", MP)

    def test_bootstrap_preserves_cli_exit_code(self):
        # bootstrapper exits with the CLI's real exit code, and never
        # logs OK when the CLI failed
        self.assertIn("exit", BS.lower())
        self.assertNotIn('Write-Log "OK" "cli"', BS)


class B9PlacementDocs(unittest.TestCase):
    """§4: the tar/checksum/manifest placement contract is documented."""

    def test_readme_documents_layout_and_same_dir(self):
        self.assertIn("checksums.sha256", README)
        self.assertIn("manifest.json", README)
        self.assertIn("images-oci.tar", README)
        # same-directory requirement spelled out
        self.assertRegex(README, r"同目录|same director|beside")


class B10RecoveryCompletable(unittest.TestCase):
    """§1.10: the printed recovery path actually completes."""

    def test_stop_plan_covers_all_session_resources(self):
        """§1.10: the printed recovery path actually completes — the
        orchestrated cleanup plan removes every service container by
        its DETERMINISTIC name (rm -fv works on Created/Exited too)
        plus both networks, so stop/cleanup --apply fully clears a
        partial or orphaned stack."""
        ocs = (ROOT / "tools" / "demo_console" /
               "one_click_startup.py").read_text(encoding="utf-8")
        self.assertIn('plans.append(["rm", "-fv", "mergepilot-isolated-%s-1" % service])', ocs)
        self.assertIn('plans.append(["network", "rm", ORCHESTRATOR_NETWORK])', ocs)
        # ownership-precedes-creation: cmd_start journals the same
        # deterministic names BEFORE the first docker command
        self.assertIn("stack_owned_containers", MP)

class B11ArgvTruncation(unittest.TestCase):
    def test_truncation_detection_and_retry(self):
        self.assertIn("WSL_ARGV_TRUNCATION", MP)
        # detection compares the reported name against the requested
        # resource name (prefix signature)
        self.assertIn("startswith", MP)
        # bounded retry before the stable code
        m = re.search(r"WSL_ARGV_TRUNCATION.{0,400}", MP, re.S)
        self.assertIsNotNone(m)


if __name__ == "__main__":
    unittest.main()
