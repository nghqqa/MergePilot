"""Migration static contract tests (M8-GH-1 §1)."""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

ROOT = Path(os.path.dirname(os.path.abspath(__file__))).parent.parent
SQL = (ROOT / "tools" / "audit-db" / "m8gh1_github_ingress.sql") \
    .read_text(encoding="utf-8")


class TestGithubDeliveriesSchema(unittest.TestCase):

    def test_required_columns_present(self):
        for column in (
                "delivery_id", "event_name", "action", "installation_id",
                "repo", "pr_number", "observed_head_sha", "observed_base_sha",
                "body_sha256", "canonical_payload", "status", "claim_id",
                "claimed_at", "lease_expires_at", "attempt_count",
                "next_retry_at", "error", "derived_run_id", "received_at",
                "processed_at"):
            self.assertRegex(SQL, r"\b%s\b" % column)

    def test_status_check_and_claim_index(self):
        self.assertIn("'PENDING','RUNNING','PROCESSED','IGNORED','ERROR'",
                      SQL)
        self.assertIn("idx_gh_deliveries_claim", SQL)
        self.assertIn("(status, next_retry_at, received_at)", SQL)

    def test_pull_request_envelope_partial_check(self):
        self.assertIn("gh_deliveries_pull_request_envelope", SQL)
        self.assertIn("'opened','synchronize','reopened'", SQL)

    def test_sha_checks_are_40_hex(self):
        # deliveries.head/base + outbox.observed_head_sha
        self.assertEqual(SQL.count("~ '^[0-9a-f]{40}$'"), 3)


class TestGithubCheckOutboxSchema(unittest.TestCase):

    def test_required_columns_present(self):
        for column in (
                "outbox_id", "run_id", "repo", "pr_number",
                "observed_head_sha", "external_id", "check_run_id",
                "desired_status", "desired_conclusion", "published_status",
                "published_conclusion", "publish_state", "claim_id",
                "claimed_at", "lease_expires_at", "desired_version",
                "published_version", "attempt_count", "next_retry_at",
                "last_error", "created_at", "updated_at", "published_at"):
            self.assertRegex(SQL, r"\b%s\b" % column)

    def test_publish_state_and_version_checks(self):
        self.assertIn("'PENDING','LEASED','PUBLISHED','TERMINAL'", SQL)
        self.assertIn("published_version <= desired_version", SQL)
        self.assertIn("uq_gh_check_external", SQL)
        self.assertIn("uq_gh_check_run", SQL)
        self.assertIn("idx_gh_check_claim", SQL)


class TestRolesAndGrants(unittest.TestCase):
    """最小权限: NOLOGIN capability + LOGIN runtime;治理表零授权;零秘密。"""

    def test_nologin_capability_and_login_runtime_roles(self):
        for role in ("github_ingress_writer", "github_checks_publisher"):
            self.assertIn("CREATE ROLE %s NOLOGIN" % role, SQL)
        for role in ("github_event_ingress", "github_check_publisher"):
            self.assertIn("CREATE ROLE %s LOGIN" % role, SQL)
        self.assertIn("GRANT github_ingress_writer TO github_event_ingress",
                      SQL)
        self.assertIn(
            "GRANT github_checks_publisher TO github_check_publisher", SQL)

    def test_receiver_grant_is_insert_only_on_deliveries(self):
        self.assertIn(
            "GRANT INSERT ON public.github_deliveries "
            "TO github_ingress_writer", SQL)
        # 不授予 deliveries 的 SELECT/UPDATE/DELETE
        self.assertNotRegex(
            SQL, r"GRANT (SELECT|UPDATE|DELETE)[^;]*ON "
                 r"public\.github_deliveries")

    def test_reporter_grant_is_select_update_on_outbox_only(self):
        self.assertIn(
            "GRANT SELECT, UPDATE ON public.github_check_outbox "
            "TO github_checks_publisher", SQL)

    def test_no_grants_on_governance_tables(self):
        grants = re.findall(r"GRANT[^;]+;", SQL)
        self.assertTrue(grants)
        for grant in grants:
            for governed in ("task_runs", "stage_runs", "dispatch_outbox",
                             "stage_events", "mcp_calls", "approvals",
                             "revision_bindings"):
                self.assertNotIn(governed, grant,
                                 "governance grant leaked: %s" % grant)

    def test_no_secrets_in_migration(self):
        for forbidden in ("PASSWORD", "password =>", "TOKEN", "PRIVATE KEY",
                          "BEGIN PRIVATE", "ghp_", "postgresql://"):
            self.assertNotIn(forbidden, SQL)


if __name__ == "__main__":
    unittest.main()
