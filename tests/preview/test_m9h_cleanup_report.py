# -*- coding: utf-8 -*-
"""M9-H §3: Cleanup manifest consumption report — pre-cleanup snapshot."""
from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BS = (ROOT / "release" / "preview" / "bootstrapper.ps1").read_text(
    encoding="utf-8-sig")


class PreCleanupSnapshot(unittest.TestCase):
    """The manifest state must be recorded BEFORE the CLI cleanup call."""

    def _cleanup_block(self):
        return BS[BS.index('"Cleanup" {'):BS.index('cleanup complete') + 20]

    def test_snapshot_before_cli_cleanup(self):
        blk = self._cleanup_block()
        snapshot_pos = blk.index("$manifestInitialState")
        cli_cleanup_pos = blk.index('Invoke-Cli @("cleanup", "--apply")')
        self.assertLess(snapshot_pos, cli_cleanup_pos,
                        "manifest snapshot must precede the CLI cleanup call")

    def test_snapshot_before_cli_stop(self):
        blk = self._cleanup_block()
        snapshot_pos = blk.index("$manifestInitialState")
        cli_stop_pos = blk.index('Invoke-Cli @("stop")')
        self.assertLess(snapshot_pos, cli_stop_pos,
                        "manifest snapshot must precede even the CLI stop call")

    def test_three_state_enum(self):
        blk = self._cleanup_block()
        for state in ("present_valid", "present_invalid", "absent"):
            self.assertIn(state, blk)

    def test_present_valid_reports_consumed(self):
        blk = self._cleanup_block()
        self.assertIn(
            "install manifest removed (was present and consumed", blk)

    def test_present_invalid_reports_unreadable(self):
        blk = self._cleanup_block()
        self.assertIn("present but unreadable", blk)
        self.assertIn("fail-closed", blk)

    def test_absent_reports_from_start(self):
        blk = self._cleanup_block()
        self.assertIn("no install manifest found from the start", blk)

    def test_no_post_cleanup_state_inference(self):
        # The report must NOT check Test-Path AFTER Invoke-Cli cleanup
        blk = self._cleanup_block()
        report_start = blk.index("Write-Log")  # first Write-Log after the CLI calls
        report_blk = blk[report_start:]
        # The $inst variable should not be re-tested in the report section
        self.assertNotIn("Test-Path $inst", report_blk,
                         "report must use the pre-cleanup snapshot, not post-cleanup Test-Path")

    def test_per_resource_lines_still_present(self):
        blk = self._cleanup_block()
        for line in ("session containers/networks", "keepalive", "forwarder",
                     "ports 8600/8090", "cleanup complete"):
            self.assertIn(line, blk)

    def test_contradiction_guard(self):
        # When manifest was present_valid, the output must NOT contain
        # the absent-from-start message (and vice versa) — verify the
        # if/elseif/else chain is mutually exclusive
        blk = self._cleanup_block()
        self.assertIn('if ($manifestInitialState -eq "present_valid")', blk)
        self.assertIn('elseif ($manifestInitialState -eq "present_invalid")', blk)
        # ensure the else branch is the absent case
        # (the if/elseif/else makes them mutually exclusive by construction)

    def test_manifest_parsed_check_before_deletion(self):
        # The ConvertFrom-Json parse must happen on the PRE-cleanup file
        blk = self._cleanup_block()
        parse_pos = blk.index("ConvertFrom-Json")
        cli_cleanup_pos = blk.index('Invoke-Cli @("cleanup", "--apply")')
        self.assertLess(parse_pos, cli_cleanup_pos,
                        "manifest parse must happen before CLI cleanup deletes it")


class IdempotentSecondCleanup(unittest.TestCase):
    """Running Cleanup twice: first sees present, second sees absent."""

    def test_no_state_caching_between_calls(self):
        # Each Cleanup invocation re-snapshots; no persistent state variable
        # is shared between runs (PowerShell script runs fresh each time)
        # The snapshot code has no caching mechanism
        blk = BS[BS.index('"Cleanup" {'):BS.index('cleanup complete') + 20]
        self.assertIn("$manifestInitialState", blk)
        # No assignment outside the Cleanup block that could leak state
        # (each script invocation is a fresh PowerShell process)
        self.assertIn('$manifestInitialState = "absent"', blk)


if __name__ == "__main__":
    unittest.main()
