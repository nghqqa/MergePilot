# -*- coding: utf-8 -*-
"""v0.1 Preview package contracts (read-only source/artifact tests).

Covers the release-review checklist for the distribution layer:
manifest truth annotations, checksum coverage semantics, installer
source contracts (checksum-before-load, PID ownership, no shell
interpolation, loopback-only), docs presence, and the three real
projections. No test here executes the installer or touches the
stack; they pin the CONTRACTS.
"""
from __future__ import annotations

import json
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs" / "preview"
REL = ROOT / "release" / "preview"


class TestPreviewDocs(unittest.TestCase):
    def test_all_demo_docs_present(self):
        for name in ("QUICKSTART.md", "ARCHITECTURE-SECURITY.md",
                     "DEMO-SCRIPT.md", "PROJECTIONS.md",
                     "SCREENSHOTS.md", "ROLLBACK.md"):
            self.assertTrue((DOCS / name).is_file(), name)

    def test_hard_annotations_in_every_doc(self):
        """transport / direct-routing caveats appear in EVERY doc; the
        five boundary KEY NAMES are enumerated in the three core docs
        (quickstart / architecture / projections), while the demo
        script and indexes carry the collective five-boundary
        statement — the demo can never lose its caveats."""
        core = ("QUICKSTART.md", "ARCHITECTURE-SECURITY.md", "PROJECTIONS.md")
        for name in ("QUICKSTART.md", "ARCHITECTURE-SECURITY.md",
                     "DEMO-SCRIPT.md", "PROJECTIONS.md",
                     "SCREENSHOTS.md", "ROLLBACK.md"):
            src = (DOCS / name).read_text(encoding="utf-8")
            self.assertIn("wsl-user-relay", src, name)
            self.assertIn("direct_routing_verified=false",
                          src.replace(" ", ""), name)
            self.assertIn("NOT_VERIFIED", src, name)
            self.assertIn("五项", src, name)
            if name in core:
                for boundary in ("application_integration_verified",
                                 "database_verified", "production_verified",
                                 "revision_producer_contract",
                                 "audit_producer_contract"):
                    self.assertIn(boundary, src,
                                  "%s: %s" % (name, boundary))


class TestProjections(unittest.TestCase):
    def test_three_projections_parse_and_carry_truth(self):
        complete = json.loads(
            (DOCS / "projections" / "complete.run35.json").read_text(encoding="utf-8"))
        self.assertEqual(complete["run_id"], "b8-e2e-run35")
        self.assertEqual(complete["e2e_stage"], "complete")
        self.assertTrue(complete["journal_complete"])
        self.assertIs(complete["direct_routing_verified"], False)
        self.assertEqual(complete["transport_profile"], "wsl-user-relay")
        self.assertEqual(sorted(set(complete["truth_boundaries"].values())),
                         ["NOT_VERIFIED"])

        failed = json.loads(
            (DOCS / "projections" / "failed.fixture.json").read_text(encoding="utf-8"))
        self.assertEqual(failed["e2e_stage"], "route_probes")
        self.assertEqual(failed["e2e_last_error"]["code"],
                         "E2E_ROUTE_PROBE_FAILED")
        self.assertFalse(failed["route_probes"]["proxy-b-to-winproxy"]["verified"])

        live = json.loads(
            (DOCS / "projections" / "live.showcase.json").read_text(encoding="utf-8"))
        self.assertEqual(live["run_id"], "run-showcase-a")
        self.assertNotIn("e2e_stage", live)  # minimal projection, honestly absent

    def test_projections_carry_no_secrets(self):
        for p in (DOCS / "projections").glob("*.json"):
            src = p.read_text(encoding="utf-8")
            for forbidden in ("ghp_", "github_pat_", "BEGIN PRIVATE KEY",
                              "syt_", "PG_PASS=", "ADMIN_PW=", "Bearer "):
                self.assertNotIn(forbidden, src, "%s: %s" % (p.name, forbidden))


class TestBootstrapperSourceContracts(unittest.TestCase):
    BS = (REL / "bootstrapper.ps1").read_text(encoding="utf-8")
    MP = (REL / "make_package.ps1").read_text(encoding="utf-8")

    def test_checksum_gate_precedes_docker_load(self):
        first_cs = self.BS.index("Assert-TarChecksum")
        called = self.BS.index("Assert-TarChecksum $ImageTar")
        load = self.BS.index("docker load -i")
        self.assertLess(called, load, "checksum gate must run before docker load")
        self.assertLess(first_cs, called)

    def test_refuses_unregistered_or_mismatched_tar(self):
        self.assertIn("no checksums.sha256 entry", self.BS)
        self.assertIn("image tar checksum mismatch", self.BS)

    def test_no_shell_interpolation_into_distro(self):
        """/bin/sh -c with interpolated variables is the injection class
        the release review removed; docker must be exec'd directly."""
        for src in (self.BS, self.MP):
            self.assertNotIn("/bin/sh -c \"docker", src)
            self.assertNotIn("sh -c \"docker", src)
        self.assertIn("--exec docker load -i", self.BS)
        self.assertIn("--exec docker save -o", self.MP)

    def test_keepalive_pid_ownership_guard(self):
        self.assertIn("Stop-OwnedKeepalive", self.BS)
        self.assertIn("ProcessName -match \"^wsl\"", self.BS)
        # every Stop-Process site sits behind a wsl* name check
        guard_count = self.BS.count("ProcessName -match \"^wsl\"")
        stop_count = self.BS.count("Stop-Process")
        self.assertGreaterEqual(guard_count, 2, "guard function + Start catch")
        self.assertLessEqual(stop_count, guard_count + 2)

    def test_manifest_snapshot_is_staged_via_temp(self):
        self.assertIn("install.current.tmp", self.BS)
        self.assertIn("[System.IO.File]::Copy($tmp, $cur, $true)", self.BS)

    def test_run_id_whitelist(self):
        self.assertIn("run-showcase-a/b/c", self.BS)

    def test_no_dangerous_patterns(self):
        for src, name in ((self.BS, "bootstrapper"), (self.MP, "make_package")):
            self.assertNotIn("Invoke-Expression", src, name)
            self.assertNotIn("iex ", src, name)
            self.assertNotIn("0.0.0.0:8600", src, name)
            self.assertNotIn("0.0.0.0:8090", src, name)
            self.assertNotIn("-Recurse -Force", src.replace(
                "Remove-Item $pkg -Recurse -Force", ""), name + " unguarded rm")

    def test_no_secrets_in_scripts(self):
        for src in (self.BS, self.MP):
            for forbidden in ("ghp_", "github_pat_", "PRIVATE KEY",
                              "PG_PASS=", "ADMIN_PW=", "Bearer "):
                self.assertNotIn(forbidden, src)


class TestPackageManifestContract(unittest.TestCase):
    """The shipped manifest (dist/) is a build artifact and not committed;
    these pin the STRUCTURE the builder must produce."""

    def test_make_package_emits_required_fields(self):
        src = self.MP if hasattr(self, "MP") else (
            REL / "make_package.ps1").read_text(encoding="utf-8")
        for field in ("transport_profile", "direct_routing_verified",
                      "truth_boundaries", "loopback_only", "publish_ports",
                      "git_commit", "images_oci_tar", "secrets_included"):
            self.assertIn(field, src, field)

    def test_gitignore_excludes_build_artifacts(self):
        gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for entry in ("dist/", "release/preview/logs/",
                      "release/preview/manifests/"):
            self.assertIn(entry, gi)


if __name__ == "__main__":
    unittest.main()
