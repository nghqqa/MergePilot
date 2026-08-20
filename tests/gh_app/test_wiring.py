"""M8-GH-3 compose/CLI wiring tests — fully static/mocked.

Covers: compose gh-webhook security options, service/build counts,
migration chain inclusion, runtime role bootstrap SQL safety, DSN builder,
room-map example contract, cleanup ownership additions.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = HERE.parent.parent
for p in (str(HERE), str(ROOT / "tools" / "gh-app"),
          str(ROOT / "tools" / "cli"), str(ROOT / "tools" / "demo_console")):
    if p not in sys.path:
        sys.path.insert(0, p)

import one_click_startup as oc                     # noqa: E402
import mergepilot as mp                            # noqa: E402

try:
    import yaml
    _HAVE_YAML = True
except ImportError:
    _HAVE_YAML = False

COMPOSE_TEXT = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
DOCKERFILE_TEXT = (ROOT / "Dockerfile.gh-webhook").read_text(
    encoding="utf-8")
EXAMPLE_MAP = (ROOT / "config" / "gh-app" / "room-map.example.yaml") \
    .read_text(encoding="utf-8")


@unittest.skipUnless(_HAVE_YAML, "PyYAML required")
class TestComposeWiring(unittest.TestCase):

    def setUp(self):
        self.yml = yaml.safe_load(COMPOSE_TEXT)

    def test_compose_service_count_still_seven(self):
        """docker-compose stays at 7 services (E2E topology is CLI-only;
        compose does not support --github-e2e)."""
        self.assertEqual(len(self.yml["services"]), 7)

    def test_gh_webhook_security_options(self):
        svc = self.yml["services"]["gh-webhook"]
        self.assertTrue(svc["read_only"])
        self.assertIn("no-new-privileges:true", svc["security_opt"])
        self.assertEqual(svc["cap_drop"], ["ALL"])
        self.assertEqual(svc["tmpfs"], ["/tmp"])
        self.assertNotIn("/var/run/docker.sock", COMPOSE_TEXT)

    def test_gh_webhook_loopback_publish_and_healthcheck(self):
        svc = self.yml["services"]["gh-webhook"]
        self.assertEqual(svc["ports"], ["127.0.0.1:8090:8090"])
        test = svc["healthcheck"]["test"]
        self.assertIn("/healthz", " ".join(test))

    def test_gh_webhook_depends_on_postgres_healthy(self):
        deps = self.yml["services"]["gh-webhook"]["depends_on"]
        self.assertEqual(deps["postgres"]["condition"], "service_healthy")

    def test_gh_webhook_no_secrets_in_compose(self):
        svc_text = yaml.safe_dump(self.yml["services"]["gh-webhook"])
        for forbidden in ("postgresql://", "PASSWORD=", "WEBHOOK_SECRET=",
                          "BEGIN PRIVATE"):
            self.assertNotIn(forbidden, svc_text)

    def test_controller_github_ingress_opt_in(self):
        """Controller wiring stays opt-in: no GITHUB_INGRESS_ENABLED literal
        in the compose controller block (default off)."""
        ctrl = yaml.safe_dump(self.yml["services"]["controller"])
        self.assertNotIn("GITHUB_INGRESS_ENABLED", ctrl)


class TestPlannerWiring(unittest.TestCase):

    def test_service_order_includes_gh_webhook(self):
        self.assertIn("gh-webhook", oc.SERVICE_ORDER)
        self.assertLess(oc.SERVICE_ORDER.index("controller"),
                       oc.SERVICE_ORDER.index("gh-webhook"))
        self.assertLess(oc.SERVICE_ORDER.index("gh-webhook"),
                       oc.SERVICE_ORDER.index("demo-console"))

    def test_e2e_service_order_eleven(self):
        """E2E_SERVICE_ORDER has 11; SERVICE_ORDER stays at 7 (default
        mode unchanged; compose parity)."""
        self.assertEqual(len(oc.E2E_SERVICE_ORDER), 11)
        self.assertEqual(len(oc.SERVICE_ORDER), 7)
        self.assertIn("gh-proxy-r", oc.E2E_SERVICE_ORDER)
        self.assertIn("gh-proxy-b", oc.E2E_SERVICE_ORDER)
        self.assertIn("mcp-bridge", oc.E2E_SERVICE_ORDER)
        self.assertIn("gh-reporter", oc.E2E_SERVICE_ORDER)
        for earlier, later in (("gh-proxy-r", "mcp-bridge"),
                               ("gh-proxy-b", "mcp-bridge"),
                               ("mcp-bridge", "policy-gateway"),
                               ("policy-gateway", "controller"),
                               ("policy-gateway", "gh-reporter")):
            self.assertLess(oc.E2E_SERVICE_ORDER.index(earlier),
                           oc.E2E_SERVICE_ORDER.index(later))

    def test_built_services_eight(self):
        self.assertEqual(len(oc.BUILT_SERVICES), 8)
        self.assertIn("gh-webhook", oc.BUILT_SERVICES)
        self.assertIn("gh-proxy", oc.BUILT_SERVICES)
        self.assertIn("mcp-bridge", oc.BUILT_SERVICES)

    def test_gh_webhook_run_plan_contract(self):
        oc._builtin_registry.clear()
        for service in oc.BUILT_SERVICES:
            hexid = ("".join(format(ord(c) & 0xF, "x")
                             for c in service) * 8)[:64]
            oc.record_built_image_identity(service, "sha256:" + hexid)
        try:
            plan = oc.plan_gh_webhook_run(
                oc.get_built_image_identity("gh-webhook"),
                env_file="gh_webhook.env")
            joined = " ".join(plan)
            self.assertIn("--network %s" % oc.PUBLICATION_NETWORK, joined)
            self.assertIn("--env-file gh_webhook.env", joined)
            self.assertIn("-p 127.0.0.1:8090:8090", joined)
            self.assertNotIn(oc.ORCHESTRATOR_NETWORK, joined)
            oc.assert_argv_safe(plan)
            with self.assertRaises(oc.StartupGateError):
                oc.plan_gh_webhook_run(
                    oc.get_built_image_identity("gh-webhook"),
                    env_file="")
            with self.assertRaises(oc.StartupGateError):
                oc.plan_service_run(
                    "gh-webhook",
                    image_ref=oc.get_built_image_identity("gh-webhook"),
                    env_file="gh_webhook.env")
        finally:
            oc._builtin_registry.clear()


class TestMigrationChain(unittest.TestCase):

    def test_m8gh1_in_cli_chain(self):
        self.assertIn("m8gh1_github_ingress.sql", mp.AUDIT_DB_MIGRATION_CHAIN)
        chain_file = ROOT / "tools" / "audit-db" / "m8gh1_github_ingress.sql"
        self.assertTrue(chain_file.is_file())

    def test_migration_no_secrets(self):
        sql = (ROOT / "tools" / "audit-db" /
               "m8gh1_github_ingress.sql").read_text(encoding="utf-8")
        for forbidden in ("PASSWORD", "postgresql://", "BEGIN PRIVATE"):
            # only comment references allowed
            effective = "\n".join(l for l in sql.splitlines()
                                  if not l.lstrip().startswith("--"))
            self.assertNotIn(forbidden, effective)


class TestRoleBootstrap(unittest.TestCase):

    def test_bootstrap_sql_uses_stdin_pattern(self):
        sql = mp.GH_RUNTIME_ROLE_SQL_TEMPLATE % ("pw1", "pw2")
        self.assertIn("ALTER ROLE github_event_ingress PASSWORD", sql)
        self.assertIn("ALTER ROLE github_check_publisher PASSWORD", sql)

    def test_sql_literal_escaping(self):
        self.assertEqual(mp._sql_literal("plain"), "plain")
        self.assertEqual(mp._sql_literal("o'brien"), "o''brien")
        self.assertEqual(mp._sql_literal("back\\slash"), "back\\\\slash")
        self.assertEqual(mp._sql_literal("both'and\\"), "both''and\\\\")

    def test_bootstrap_never_in_argv(self):
        """The SQL rides psql stdin (psql_exec input_bytes), never argv."""
        import inspect
        source = inspect.getsource(mp.bootstrap_gh_roles)
        self.assertIn("psql_exec", source)
        self.assertNotIn("-e ", source)


class TestDsnBuilder(unittest.TestCase):

    def test_forced_connect_timeout_and_quoting(self):
        dsn = mp.GhWebhookSecretFile.build_ingress_dsn("p@ss word+")
        self.assertIn("connect_timeout=5", dsn)
        self.assertIn(quote_check("p@ss word+"), dsn)

    def test_publisher_dsn_user(self):
        dsn = mp.GhWebhookSecretFile.build_ingress_dsn(
            "pw", user="github_check_publisher")
        self.assertIn("github_check_publisher", dsn)
        self.assertIn("connect_timeout=5", dsn)


def quote_check(password):
    import urllib.parse
    return urllib.parse.quote(password, safe="")


class TestRoomMapExample(unittest.TestCase):

    def test_placeholder_only(self):
        self.assertIn("!replace-with-your-real-matrix-room", EXAMPLE_MAP)

    def test_no_real_room_ids(self):
        """No lines that look like a real (non-placeholder) room mapping."""
        for line in EXAMPLE_MAP.splitlines():
            if "room_id:" in line:
                self.assertIn("replace-with-your-real", line)

    def test_runtime_map_is_gitignored(self):
        rules = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".mergepilot/", rules)

    def test_example_matches_policy_allowlist_repos(self):
        """The example's repo set covers the policy allowlist (1:1 template
        capability)."""
        allowlist = mp._policy_repo_allowlist(ROOT)
        repos_in_example = re.findall(r'^  "([^"]+)":', EXAMPLE_MAP,
                                      re.MULTILINE)
        self.assertEqual(sorted(repos_in_example), sorted(allowlist.split(",")))


class TestCleanupOwnership(unittest.TestCase):

    def test_session_secrets_include_gh_files(self):
        session = mp.new_session("run-x", False)
        self.assertIn("gh_webhook.env", session["secrets"])
        self.assertIn("gh_reporter.env", session["secrets"])

    def test_cleanup_sweeps_secret_dir(self):
        import inspect
        source = inspect.getsource(mp.cmd_cleanup)
        self.assertIn('glob("*.env")', source)

    def test_status_ingress_counts_sanitized(self):
        import inspect
        source = inspect.getsource(mp.cmd_status)
        self.assertIn("github_deliveries", source)
        self.assertIn("github_check_outbox", source)
        # only COUNT(*) queries — no payload/DSN reads
        self.assertIn("SELECT count(*)", source)


class TestControllerOptInUnchanged(unittest.TestCase):

    def test_controller_default_off(self):
        source = (ROOT / "tools" / "workflow-controller" /
                  "controller.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("GITHUB_INGRESS_ENABLED", "") == "1"',
                      source)

    def test_reporter_main_entry(self):
        source = (ROOT / "tools" / "gh-app" / "checks_reporter.py") \
            .read_text(encoding="utf-8")
        self.assertIn('if __name__ == "__main__":', source)


if __name__ == "__main__":
    unittest.main()
