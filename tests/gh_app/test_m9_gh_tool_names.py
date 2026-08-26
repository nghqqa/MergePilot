# -*- coding: utf-8 -*-
"""M9 finding F: gh agent-team helpers target the PINNED github-mcp-server.

The pinned server (ghcr.io/github/github-mcp-server v1.9.0, frozen in
Dockerfile.mcp-bridge) renamed its tools: get_pull_request became
pull_request_read(method=get), and pull_number became pullNumber. The
production helpers still emit the old names — every real Fixer/Reviewer
MCP call fails with 'unknown tool'. These tests pin the exact tool
names and parameter spellings against the pinned server's signatures
(v1.9.0, recorded from the live server's list-tools output).
"""
from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIX = (ROOT / "tools" / "agentteams" / "gh_fix_branch.py").read_text(
    encoding="utf-8")
READ = (ROOT / "tools" / "agentteams" / "gh_read.py").read_text(
    encoding="utf-8")


class PinnedToolNames(unittest.TestCase):
    """v1.9.0 signature contract (from the live pinned server)."""

    def test_fixer_uses_pull_request_read(self):
        self.assertIn('github.pull_request_read', FIX)
        self.assertNotIn('github.get_pull_request', FIX)

    def test_fixer_passes_method_get(self):
        # pull_request_read requires method="get" for metadata reads
        self.assertIn('"method=get"', FIX.replace("'method=get'",
                                                 '"method=get"'))

    def test_fixer_uses_pullNumber(self):
        # the pinned server spells it pullNumber (camelCase), not
        # pull_number
        self.assertIn("pullNumber=%d", FIX)
        self.assertNotIn("pull_number=%d", FIX)

    def test_reader_uses_pull_request_read(self):
        self.assertIn("github.pull_request_read", READ)
        self.assertNotIn("github.get_pull_request", READ)

    def test_file_tools_unchanged(self):
        # get_file_contents / create_or_update_file kept their names
        self.assertIn("github.get_file_contents", FIX)
        self.assertIn("github.create_or_update_file", FIX)
        self.assertIn("github.get_file_contents", READ)

    def test_fork_guard_still_parses_head_section(self):
        # the JSON shape of pull_request_read(get) still carries
        # head.ref / head.repo.full_name — the fork guard must keep
        # working against those fields
        self.assertIn('"head"', FIX)
        self.assertIn("full_name", FIX)

    def test_sha_extraction_unchanged(self):
        # get_file_contents still emits "SHA: <hex>" lines
        self.assertIn("SHA:", FIX)


if __name__ == "__main__":
    unittest.main()


class IdempotencyGuard(unittest.TestCase):
    """m9 finding G: a retry with identical content must be a no-op,
    never a duplicate commit."""

    def test_fixer_has_idempotency_precheck(self):
        self.assertIn("idempotent no-op", FIX)
        self.assertIn("content.strip() in cur", FIX)
        # the no-op returns BEFORE any create_or_update_file call
        guard = FIX.index("idempotent no-op")
        write = FIX.index('"github.create_or_update_file"')
        self.assertLess(guard, write)
