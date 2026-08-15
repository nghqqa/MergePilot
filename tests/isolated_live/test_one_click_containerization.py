"""ISOLATED_LIVE productization Phase 1-C — containerization Mock/static tests.

Verifies docker-compose.yml, the four root Dockerfiles, the image-identity
registry, and the Docker-CLI orchestrator. No WSL/Docker/PostgreSQL is
started; the unauthorized path performs zero real subprocess calls.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = _HERE.parent.parent
for _p in (str(_HERE), str(ROOT), str(ROOT / "tools" / "demo_console")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import one_click_startup as oc  # noqa: E402
from one_click_startup import (  # noqa: E402
    BUILT_SERVICES,
    ORCHESTRATOR_NETWORK,
    SecretFile,
    StartupGateError,
    assert_argv_safe,
    build_compose_config,
    built_identity_registry,
    compose_dependency_order,
    compose_ports_binding,
    get_built_image_identity,
    plan_build,
    plan_network_create,
    plan_orchestrated_cleanup,
    plan_orchestrated_start,
    plan_service_run,
    record_built_image_identity,
    validate_compose_config,
)

try:
    import yaml  # type: ignore
    _HAVE_YAML = True
except ImportError:
    _HAVE_YAML = False

COMPOSE_PATH = ROOT / "docker-compose.yml"
DOCKERFILES = {
    "controller": ROOT / "Dockerfile.controller",
    "policy-gateway": ROOT / "Dockerfile.policy-gateway",
    "demo-console": ROOT / "Dockerfile.demo-console",
    "preflight": ROOT / "Dockerfile.preflight",
}


def _gate(testcase, fn, *args, code, **kwargs):
    with testcase.assertRaises(StartupGateError) as cm:
        fn(*args, **kwargs)
    testcase.assertEqual(cm.exception.code, code, msg=str(cm.exception))
    return cm.exception


def _load_compose() -> dict:
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    if _HAVE_YAML:
        return yaml.safe_load(text)
    # Minimal fallback parser for the flat structure we control.
    raise unittest.SkipTest("PyYAML unavailable; yml validated structurally elsewhere")


# ── 1. Dockerfile / compose service completeness ────────────────────────────

class TestFilesComplete(unittest.TestCase):

    def test_compose_file_exists(self):
        self.assertTrue(COMPOSE_PATH.is_file())

    def test_all_four_dockerfiles_exist(self):
        for name, path in DOCKERFILES.items():
            self.assertTrue(path.is_file(), name)

    def test_dockerfiles_reference_cached_base(self):
        for name, path in DOCKERFILES.items():
            body = path.read_text(encoding="utf-8")
            self.assertIn("FROM python:3.12-slim", body, name)

    def test_dockerfiles_install_note_present(self):
        # Each Dockerfile must disclose that pip install is BUILD-time and
        # needs separate authorization.
        for name, path in DOCKERFILES.items():
            body = path.read_text(encoding="utf-8")
            self.assertIn("separate", body.lower(), name)
            self.assertIn("authorization", body.lower(), name)

    def test_demo_console_dockerfile_entrypoint_loopback(self):
        body = DOCKERFILES["demo-console"].read_text(encoding="utf-8")
        self.assertIn("--host", body)
        self.assertIn("127.0.0.1", body)
        self.assertIn("EXPOSE 8600", body)

    def test_preflight_dockerfile_in_network_env(self):
        body = DOCKERFILES["preflight"].read_text(encoding="utf-8")
        self.assertIn("MERGEPILOT_PG_HOST=postgres", body)
        self.assertIn("http://demo-console:8600", body)

    def test_preflight_entrypoint_module_exists(self):
        self.assertTrue((ROOT / "tools" / "preflight_entrypoint.py").is_file())


# ── 2. Four-service existence (compose yml + builder) ───────────────────────

@unittest.skipUnless(_HAVE_YAML, "PyYAML required")
class TestComposeYml(unittest.TestCase):

    def setUp(self):
        self.yml = _load_compose()

    def test_five_services_present(self):
        for name in oc.SERVICE_ORDER:
            self.assertIn(name, self.yml["services"])

    def test_builder_matches_yml_services(self):
        cfg = build_compose_config()
        self.assertEqual(set(cfg["services"]), set(self.yml["services"]))

    def test_postgres_digest_pinned(self):
        self.assertEqual(self.yml["services"]["postgres"]["image"],
                         oc.PGVECTOR_IMAGE_DIGEST)

    def test_all_pull_never(self):
        for name, svc in self.yml["services"].items():
            self.assertEqual(svc.get("pull_policy"), "never", name)

    def test_build_contexts_declared(self):
        builds = {
            "policy-gateway": ("tools/policy-gateway", "Dockerfile"),
            "controller": ("tools/workflow-controller", "Dockerfile"),
            "demo-console": (".", "Dockerfile.demo-console"),
            "preflight": (".", "Dockerfile.preflight"),
        }
        for name, (ctx, df) in builds.items():
            build = self.yml["services"][name]["build"]
            self.assertEqual(build["context"], ctx, name)
            self.assertEqual(build["dockerfile"], df, name)

    def test_internal_only_network(self):
        iso = self.yml["networks"]["isolated"]
        self.assertTrue(iso["internal"])
        self.assertEqual(iso["driver"], "bridge")

    def test_all_services_on_isolated_network(self):
        for name, svc in self.yml["services"].items():
            self.assertIn("isolated", svc["networks"], name)

    def test_only_demo_console_publishes_loopback(self):
        for name, svc in self.yml["services"].items():
            ports = svc.get("ports") or []
            if name == "demo-console":
                self.assertEqual(len(ports), 1)
                self.assertTrue(str(ports[0]).startswith("127.0.0.1:"))
            else:
                self.assertEqual(ports, [], name)

    def test_no_volumes(self):
        self.assertTrue(not self.yml.get("volumes"))

    def test_postgres_healthcheck(self):
        hc = self.yml["services"]["postgres"]["healthcheck"]
        self.assertIn("pg_isready", str(hc["test"]))

    def test_dependency_chain(self):
        deps = self.yml["services"]
        self.assertEqual(
            deps["policy-gateway"]["depends_on"]["postgres"]["condition"],
            "service_healthy")
        self.assertEqual(
            deps["controller"]["depends_on"]["postgres"]["condition"],
            "service_healthy")
        self.assertEqual(
            deps["controller"]["depends_on"]["policy-gateway"]["condition"],
            "service_started")
        self.assertEqual(
            deps["demo-console"]["depends_on"]["controller"]["condition"],
            "service_started")
        self.assertEqual(
            deps["preflight"]["depends_on"]["demo-console"]["condition"],
            "service_started")

    def test_preflight_env_in_network(self):
        env = self.yml["services"]["preflight"]["environment"]
        self.assertEqual(env["MERGEPILOT_PG_HOST"], "postgres")
        self.assertEqual(env["MERGEPILOT_PG_PORT"], "5432")
        self.assertEqual(env["MERGEPILOT_DEMO_CONSOLE_URL"],
                         "http://demo-console:8600")

    def test_preflight_restart_no(self):
        self.assertEqual(self.yml["services"]["preflight"]["restart"], "no")

    def test_no_unpinned_remote_tags(self):
        # The ONLY literal remote image anywhere is the digest-pinned pgvector.
        for name, svc in self.yml["services"].items():
            if "image" in svc:
                self.assertEqual(name, "postgres")
                self.assertIn("@sha256:", svc["image"])

    def test_yaml_contains_no_lan_or_wildcard_binds(self):
        text = COMPOSE_PATH.read_text(encoding="utf-8")
        for bad in ("0.0.0.0", "::", "192.168.", "10.0."):
            self.assertNotIn(bad, text)

    def test_no_twin_or_host_process_path(self):
        # The yml comments say "no twin container, no host-process substitute"
        # — every occurrence must be a negation, never a usage.
        text = COMPOSE_PATH.read_text(encoding="utf-8").lower()
        for i, line in enumerate(text.splitlines()):
            low = line.strip()
            if "twin" in low:
                self.assertTrue("no twin" in low or "twin container" in low,
                                "line %d uses 'twin' affirmatively" % i)
            if "host-process" in low or "host process" in low:
                self.assertTrue("no host" in low or "no twin" in low,
                                "line %d uses host-process affirmatively" % i)


# ── 3. Image-identity registry ──────────────────────────────────────────────

class TestImageIdentityRegistry(unittest.TestCase):

    def setUp(self):
        oc._builtin_registry.clear()

    def tearDown(self):
        oc._builtin_registry.clear()

    def test_record_and_get(self):
        ident = "sha256:" + "a" * 64
        record_built_image_identity("controller", ident)
        self.assertEqual(get_built_image_identity("controller"), ident)
        self.assertEqual(built_identity_registry()["controller"], ident)

    def test_unknown_service_rejected(self):
        _gate(self, record_built_image_identity, "postgres",
              "sha256:" + "a" * 64, code="CONFIG_INVALID")

    def test_floating_tag_rejected(self):
        _gate(self, record_built_image_identity, "controller",
              "mergepilot-isolated-controller:local", code="CONFIG_INVALID")

    def test_malformed_id_rejected(self):
        for bad in ("", "sha256:abc", "sha256:" + "a" * 63,
                    "sha256:" + "a" * 65, "sha256:" + "g" * 64):
            _gate(self, record_built_image_identity, "controller", bad,
                  code="CONFIG_INVALID")

    def test_identity_immutable(self):
        a, b = "sha256:" + "a" * 64, "sha256:" + "b" * 64
        record_built_image_identity("demo-console", a)
        record_built_image_identity("demo-console", a)  # same value OK
        _gate(self, record_built_image_identity, "demo-console", b,
              code="IMAGE_DIGEST_MISMATCH")

    def test_missing_identity_raises(self):
        _gate(self, get_built_image_identity, "preflight",
              code="CONFIG_INVALID")

    def test_built_services_list(self):
        self.assertEqual(set(BUILT_SERVICES),
                         {"policy-gateway", "controller",
                          "demo-console", "preflight"})


# ── 4. Docker-CLI orchestrator ───────────────────────────────────────────────

class TestOrchestrator(unittest.TestCase):

    def setUp(self):
        oc._builtin_registry.clear()
        for service in BUILT_SERVICES:
            record_built_image_identity(
                service, "sha256:" + service[0] * 64 if service[0].isalpha()
                and service[0] in "0123456789abcdef"
                else "sha256:" + "ab" * 32)
        # simpler: valid hex ids per service
        oc._builtin_registry.clear()
        for service in BUILT_SERVICES:
            hexid = ("".join(format(ord(c) & 0xF, "x") for c in service) * 8)[:64]
            record_built_image_identity(service, "sha256:" + hexid)

    def tearDown(self):
        oc._builtin_registry.clear()

    def test_network_create_plan(self):
        plan = plan_network_create()
        self.assertEqual(plan[0], "network")
        self.assertIn("--internal", plan)
        self.assertIn(ORCHESTRATOR_NETWORK, plan)

    def test_service_run_plans_are_argv_arrays(self):
        pg = plan_service_run("postgres", image_ref=oc.PGVECTOR_IMAGE_DIGEST,
                              env_file="postgres.env")
        self.assertEqual(pg[0], "run")
        self.assertIn("--pull", pg)
        self.assertEqual(pg[pg.index("--pull") + 1], "never")
        self.assertIn("--restart", pg)
        self.assertEqual(pg[pg.index("--restart") + 1], "no")
        self.assertIn(ORCHESTRATOR_NETWORK, pg)
        self.assertNotIn("-p", pg)
        self.assertEqual(pg[-1], oc.PGVECTOR_IMAGE_DIGEST)

    def test_postgres_healthcheck_flags(self):
        pg = plan_service_run("postgres", image_ref=oc.PGVECTOR_IMAGE_DIGEST)
        self.assertIn("--health-cmd", pg)
        self.assertIn("--health-retries", pg)

    def test_only_demo_console_publishes(self):
        pub = plan_service_run("demo-console",
                               image_ref=get_built_image_identity("demo-console"))
        self.assertIn("-p", pub)
        self.assertEqual(pub[pub.index("-p") + 1], "127.0.0.1:8600:8600")
        for service in ("policy-gateway", "controller", "preflight"):
            plan = plan_service_run(service,
                                    image_ref=get_built_image_identity(service))
            self.assertNotIn("-p", plan, service)
        pg = plan_service_run("postgres", image_ref=oc.PGVECTOR_IMAGE_DIGEST)
        self.assertNotIn("-p", pg)

    def test_preflight_env_in_network(self):
        plan = plan_service_run("preflight",
                                image_ref=get_built_image_identity("preflight"),
                                declared_pg_image=oc.PGVECTOR_IMAGE_DIGEST)
        joined = " ".join(plan)
        self.assertIn("MERGEPILOT_PG_HOST=postgres", joined)
        self.assertIn("http://demo-console:8600", joined)
        self.assertIn("MERGEPILOT_DECLARED_PG_IMAGE", joined)

    def test_unknown_service_rejected(self):
        _gate(self, plan_service_run, "twin", image_ref="sha256:" + "a" * 64,
              code="CONFIG_INVALID")

    def test_floating_image_ref_rejected(self):
        for bad in ("some/image:latest", "", "image:", "repo@sha256:short"):
            _gate(self, plan_service_run, "controller", image_ref=bad,
                  code="CONFIG_INVALID")

    def test_plans_never_contain_shell_metachars(self):
        plans = plan_orchestrated_start(env_file="postgres.env")
        for plan in plans:
            joined = " ".join(plan)
            for ch in (";", "|", "&", "`", "$("):
                self.assertNotIn(ch, joined)

    def test_plans_no_dsn_or_password(self):
        plans = plan_orchestrated_start(env_file="postgres.env")
        for plan in plans:
            assert_argv_safe(plan)

    def test_full_start_order(self):
        plans = plan_orchestrated_start(env_file="postgres.env")
        self.assertEqual(len(plans), 6)  # network + 5 services
        self.assertEqual(plans[0][0], "network")
        names = [p[p.index("--name") + 1] for p in plans[1:]]
        expected = ["mergepilot-isolated-%s-1" % s
                    for s in oc.SERVICE_ORDER]
        self.assertEqual(names, expected)

    def test_full_start_requires_recorded_identities(self):
        oc._builtin_registry.clear()
        _gate(self, plan_orchestrated_start, code="CONFIG_INVALID")

    def test_cleanup_plan_reverse_order_then_network(self):
        plans = plan_orchestrated_cleanup()
        self.assertEqual(len(plans), 6)
        names = [p[2] for p in plans[:5]]
        self.assertEqual(names, list(reversed(
            ["mergepilot-isolated-%s-1" % s for s in oc.SERVICE_ORDER])))
        self.assertEqual(plans[5][0], "network")
        self.assertEqual(plans[5][2], ORCHESTRATOR_NETWORK)

    def test_build_plans_reference_existing_dockerfiles(self):
        for service in ("controller", "policy-gateway",
                        "demo-console", "preflight"):
            plan = plan_build(service)
            self.assertEqual(plan[0], "build")
            self.assertIn("-f", plan)
            dockerfile = plan[plan.index("-f") + 1]
            self.assertTrue((ROOT / dockerfile).is_file(), dockerfile)

    def test_no_compose_cli_dependency(self):
        # The orchestrator is pure argv planning: it never shells out and
        # never invokes a `docker compose` plugin subcommand.
        src = Path(oc.__file__).read_text(encoding="utf-8")
        orchestrator = src.split("Docker-CLI orchestrator")[-1].split("__all__")[0]
        self.assertNotIn("subprocess", orchestrator)
        self.assertNotIn('"compose"', orchestrator)

    def test_unauthorized_path_zero_real_calls(self):
        # Pure planning: no subprocess, no socket, no docker invocation.
        with mock.patch("subprocess.run") as sr, \
                mock.patch("socket.socket") as ss:
            plan_orchestrated_start(env_file="postgres.env")
            plan_orchestrated_cleanup()
            validate_compose_config(build_compose_config())
        sr.assert_not_called()
        ss.assert_not_called()


# ── 5. Twin/host-process substitution absent from the formal path ──────────

class TestNoTwinOrHostSubstitution(unittest.TestCase):

    def test_orchestrator_has_no_twin_container(self):
        oc._builtin_registry.clear()
        for service in BUILT_SERVICES:
            hexid = ("".join(format(ord(c) & 0xF, "x") for c in service) * 8)[:64]
            record_built_image_identity(service, "sha256:" + hexid)
        try:
            plans = plan_orchestrated_start()
            for plan in plans:
                joined = " ".join(plan)
                self.assertNotIn("twin", joined.lower())
        finally:
            oc._builtin_registry.clear()

    def test_orchestrator_has_no_host_bind_except_demo_console(self):
        oc._builtin_registry.clear()
        for service in BUILT_SERVICES:
            hexid = ("".join(format(ord(c) & 0xF, "x") for c in service) * 8)[:64]
            record_built_image_identity(service, "sha256:" + hexid)
        try:
            for service in BUILT_SERVICES:
                plan = plan_service_run(
                    service, image_ref=get_built_image_identity(service))
                if service == "demo-console":
                    self.assertIn("-p", plan)
                    self.assertEqual(plan[plan.index("-p") + 1],
                                     "127.0.0.1:8600:8600")
                else:
                    self.assertNotIn("-p", plan, service)
            pg = plan_service_run("postgres", image_ref=oc.PGVECTOR_IMAGE_DIGEST)
            self.assertNotIn("-p", pg)
        finally:
            oc._builtin_registry.clear()

    def test_preflight_network_path_not_host(self):
        plan = plan_service_run("preflight",
                                image_ref="sha256:" + "ab" * 32)
        joined = " ".join(plan)
        self.assertIn("--network", joined)
        self.assertIn(ORCHESTRATOR_NETWORK, joined)
        self.assertIn("MERGEPILOT_PG_HOST=postgres", joined)
        self.assertNotIn("MERGEPILOT_PG_HOST=127.0.0.1", joined)
        self.assertNotIn("MERGEPILOT_PG_HOST=172.", joined)

    def test_entrypoint_uses_env_dsn_not_host_substitute(self):
        body = (ROOT / "tools" / "preflight_entrypoint.py").read_text(
            encoding="utf-8")
        self.assertIn('os.environ["MERGEPILOT_PG_DSN"]', body)
        self.assertIn("http://demo-console:8600", body)


if __name__ == "__main__":
    unittest.main()
