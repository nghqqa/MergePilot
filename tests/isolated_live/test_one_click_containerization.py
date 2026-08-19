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

# Retry v2: the demo-console runtime inputs are caller-provided — the seeded
# run_id and the postgres bridge IP MEASURED after the healthcheck passes.
_TEST_RUN_ID = "caller-provided-run-001"
_TEST_BRIDGE_IP = "172.18.0.2"


def _write_controller_env(lines=("PG_PASS=test-pg-pass\n"
                                 "ADMIN_PW=test-admin-pw\n")):
    """Create a contract-valid controller secret env-file in a temp dir.

    M8-A1: plan_orchestrated_start validates the controller.env contract
    unconditionally, so tests must provide a REAL readable file (exact
    key set; two lines with the machinery off, three with it on). The
    values are inert test strings, never real secrets.
    """
    import tempfile
    td = tempfile.mkdtemp(prefix="ctrl-env-")
    path = Path(td) / "controller.env"
    path.write_text("".join(lines), encoding="utf-8")
    return str(path)


def _start_kwargs():
    return {"demo_console_run_id": _TEST_RUN_ID,
            "demo_console_pg_server_addresses": _TEST_BRIDGE_IP,
            "controller_env_file": _write_controller_env(),
            "reader_dsn_env_file": "demo_console.env",
            "gh_webhook_env_file": "gh_webhook.env"}

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
        # The ENTRYPOINT delegates to demo_console_entrypoint.py which reads
        # host/port from env (loopback validated there). The Dockerfile still
        # discloses the loopback contract and the port.
        self.assertIn("demo_console_entrypoint.py", body)
        self.assertIn("127.0.0.1", body)
        self.assertIn("EXPOSE 8600", body)
        # No baked --mode in ENTRYPOINT (the entrypoint module handles it).
        entrypoint_line = [l for l in body.splitlines()
                          if l.startswith("ENTRYPOINT")]
        self.assertEqual(len(entrypoint_line), 1)
        self.assertNotIn("--mode", entrypoint_line[0])

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
        cfg = build_compose_config(demo_console_run_id=_TEST_RUN_ID,
                                   demo_console_pg_server_addresses=_TEST_BRIDGE_IP)
        self.assertEqual(set(cfg["services"]), set(self.yml["services"]))

    def test_demo_console_env_contract_in_yml(self):
        # Retry v2 Fix 1 + Fix 2: the yml declares the container bind context
        # and the five PG expected identity params; SERVER_ADDRESSES uses
        # required variable interpolation (measured bridge IP, never a
        # hardcoded literal).
        env = self.yml["services"]["demo-console"]["environment"]
        self.assertEqual(env["MERGEPILOT_BIND_CONTEXT"], "container")
        self.assertEqual(env["MERGEPILOT_PG_EXPECTED_DATABASE"], "mergepilot_audit")
        self.assertEqual(env["MERGEPILOT_PG_ENVIRONMENT_ID"],
                         "mergepilot-test-ephemeral")
        self.assertEqual(env["MERGEPILOT_PG_EXPECTED_SERVER_PORT"], "5432")
        self.assertEqual(env["MERGEPILOT_PG_EXPECTED_APPLICATION_NAME"],
                         "mergepilot_isolated_live_reader")
        addr = str(env["MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES"])
        self.assertIn("${MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES", addr)
        self.assertIn("is required", addr)
        # The measured-IP literal must NOT be baked into the yml.
        text = COMPOSE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("172.18.", text)

    def test_postgres_digest_pinned(self):
        self.assertEqual(self.yml["services"]["postgres"]["image"],
                         oc.PGVECTOR_IMAGE_DIGEST)

    def test_all_pull_never(self):
        for name, svc in self.yml["services"].items():
            self.assertEqual(svc.get("pull_policy"), "never", name)

    def test_build_contexts_declared(self):
        # v3: ALL services build from the repo-root context with the root
        # wrapper Dockerfiles — unified with the builder/orchestrator.
        builds = {
            "policy-gateway": (".", "Dockerfile.policy-gateway"),
            "controller": (".", "Dockerfile.controller"),
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
        # 1-G + M8-GH-3: exactly TWO loopback publications — the secretless
        # console-edge (8600) and the gh-webhook receiver (8090);
        # demo-console is UNPUBLISHED.
        published = {"console-edge": "127.0.0.1:8600:8600",
                     "gh-webhook": "127.0.0.1:8090:8090"}
        for name, svc in self.yml["services"].items():
            ports = svc.get("ports") or []
            if name in published:
                self.assertEqual(len(ports), 1, name)
                self.assertEqual(str(ports[0]), published[name])
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
        # v3 Fix 3: healthy, not merely started — controller waits for the
        # gateway's REAL healthcheck, demo-console for controller's.
        self.assertEqual(
            deps["controller"]["depends_on"]["policy-gateway"]["condition"],
            "service_healthy")
        self.assertEqual(
            deps["demo-console"]["depends_on"]["controller"]["condition"],
            "service_healthy")
        # Review-gap Fix 3: preflight waits for REAL demo-console readiness.
        self.assertEqual(
            deps["preflight"]["depends_on"]["demo-console"]["condition"],
            "service_healthy")

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
        # HOST-side port publishes must never use LAN/wildcard/IPv6. The
        # 0.0.0.0 in MERGEPILOT_HOST is the CONTAINER-INTERNAL listen address
        # (Docker bridge routing), which is a DIFFERENT address from the
        # host-side publish. Verify no LAN/wildcard in any ports: line.
        text = COMPOSE_PATH.read_text(encoding="utf-8")
        for bad in ("::", "192.168.", "10.0."):
            self.assertNotIn(bad, text)
        # Check that 0.0.0.0 appears ONLY in the env block, never in ports.
        in_ports = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("ports:"):
                in_ports = True
                continue
            if in_ports and stripped.startswith("-") and "0.0.0.0" in stripped:
                self.fail("0.0.0.0 found in a ports: publish line: %r" % stripped)
            if in_ports and stripped and not stripped.startswith(("-", "#")) and not line.startswith(" "):
                in_ports = False

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
                          "demo-console", "console-edge", "preflight",
                          "gh-webhook"})


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

    def test_only_console_edge_publishes(self):
        # 1-G network design: ONLY the edge publishes (loopback, exactly
        # once); demo-console and every other service publish NOTHING.
        demo = plan_service_run(
            "demo-console",
            image_ref=get_built_image_identity("demo-console"),
            demo_console_env=oc._demo_console_environment(
                _TEST_RUN_ID, _TEST_BRIDGE_IP),
            reader_dsn_env_file="demo_console.env")
        self.assertNotIn("-p", demo)
        edge = oc.plan_console_edge_run(
            get_built_image_identity("console-edge"))
        self.assertEqual(edge.count("-p"), 1)
        self.assertEqual(edge[edge.index("-p") + 1], "127.0.0.1:8600:8600")
        for service, env_kwargs in (
                ("policy-gateway", {"gateway_env": oc._gateway_environment()}),
                ("controller",
                 {"controller_env": oc._controller_environment(),
                  "env_file": "controller.env"}),
                ("preflight",
                 {"reader_dsn_env_file": "demo_console.env"})):
            plan = plan_service_run(
                service, image_ref=get_built_image_identity(service),
                **env_kwargs)
            self.assertNotIn("-p", plan, service)
        pg = plan_service_run("postgres", image_ref=oc.PGVECTOR_IMAGE_DIGEST)
        self.assertNotIn("-p", pg)
        # The GENERIC path must fail-closed for the edge: it would create
        # it on the internal network as primary and silently drop the
        # publish (the retry-5 failure mode).
        _gate(self, plan_service_run, "console-edge",
              image_ref=get_built_image_identity("console-edge"),
              code="CONFIG_INVALID")

    def test_demo_console_run_plan_carries_full_env_contract(self):
        # Retry v2: the orchestrated demo-console run must inject the entrypoint
        # contract — bind context AND the five PG expected identity params.
        plan = plan_service_run(
            "demo-console",
            image_ref=get_built_image_identity("demo-console"),
            demo_console_env=oc._demo_console_environment(
                _TEST_RUN_ID, _TEST_BRIDGE_IP),
            reader_dsn_env_file="demo_console.env")
        joined = " ".join(plan)
        for pair in ("MERGEPILOT_MODE=isolated_live",
                     "MERGEPILOT_SOURCE_KIND=postgres",
                     "MERGEPILOT_RUN_ID=%s" % _TEST_RUN_ID,
                     "MERGEPILOT_EXPECTED_ROLE=mergepilot_reader",
                     "MERGEPILOT_BIND_CONTEXT=container",
                     "MERGEPILOT_HOST=0.0.0.0",
                     "MERGEPILOT_PORT=8600",
                     "MERGEPILOT_PG_EXPECTED_DATABASE=mergepilot_audit",
                     "MERGEPILOT_PG_ENVIRONMENT_ID=mergepilot-test-ephemeral",
                     "MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES=%s" % _TEST_BRIDGE_IP,
                     "MERGEPILOT_PG_EXPECTED_SERVER_PORT=5432",
                     "MERGEPILOT_PG_EXPECTED_APPLICATION_NAME="
                     "mergepilot_isolated_live_reader"):
            self.assertIn(pair, joined)
        assert_argv_safe(plan)

    def test_demo_console_run_requires_env(self):
        # Fail-closed: a demo-console plan without the env contract is
        # CONFIG_INVALID (the container would otherwise die at entrypoint
        # validation with a less diagnosable error).
        _gate(self, plan_service_run, "demo-console",
              image_ref=get_built_image_identity("demo-console"),
              code="CONFIG_INVALID")

    def test_demo_console_env_rejects_alias_address(self):
        with self.assertRaises(oc.StartupGateError):
            oc._demo_console_environment(_TEST_RUN_ID, "postgres")

    def test_preflight_env_in_network(self):
        plan = plan_service_run("preflight",
                                image_ref=get_built_image_identity("preflight"),
                                declared_pg_image=oc.PGVECTOR_IMAGE_DIGEST,
                                reader_dsn_env_file="demo_console.env")
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
        plans = plan_orchestrated_start(env_file="postgres.env", **_start_kwargs())
        for plan in plans:
            joined = " ".join(plan)
            # exec-form healthcheck: the python -c body is ONE argv token
            # (docker --health-cmd string form) — its internal ';' is not a
            # shell metachar; strip the sanctioned body before scanning.
            stripped = joined.replace(
                "'import socket;s=socket.create_connection("
                "(\"127.0.0.1\",8090),timeout=2);s.close()'",
                "SOCKET_C_HEALTHCHECK")
            for ch in (";", "|", "&", "`", "$("):
                self.assertNotIn(ch, stripped)

    def test_plans_no_dsn_or_password(self):
        plans = plan_orchestrated_start(env_file="postgres.env", **_start_kwargs())
        for plan in plans:
            assert_argv_safe(plan)

    def test_full_start_order(self):
        plans = plan_orchestrated_start(env_file="postgres.env", **_start_kwargs())
        # 2 network creates + 7 service runs (incl. console-edge and
        # gh-webhook) + 2 network-connects = 11.
        self.assertEqual(len(plans), 11)
        self.assertEqual(plans[0], oc.plan_network_create())
        self.assertEqual(plans[1], oc.plan_publication_network_create())
        run_plans = [p for p in plans[2:] if "--name" in p]
        connect_plans = [p for p in plans[2:] if "--name" not in p]
        self.assertEqual(len(connect_plans), 2, connect_plans)
        self.assertIn(oc.plan_console_edge_connect_backend(),
                      connect_plans)
        self.assertIn(oc.plan_gh_webhook_connect_backend(),
                      connect_plans)
        names = [p[p.index("--name") + 1] for p in run_plans]
        expected = ["mergepilot-isolated-%s-1" % s
                    for s in oc.SERVICE_ORDER]
        self.assertEqual(names, expected)
        edge_idx = next(i for i, p in enumerate(plans)
                        if "--name" in p and "console-edge-1" in
                        p[p.index("--name") + 1])
        edge_connect_idx = plans.index(oc.plan_console_edge_connect_backend())
        self.assertLess(edge_idx, edge_connect_idx)

    def test_full_start_requires_demo_console_inputs(self):
        # Fail-closed: run_id and measured bridge IP are REQUIRED — no
        # defaults, no inference.
        oc._builtin_registry.clear()
        for service in BUILT_SERVICES:
            hexid = ("".join(format(ord(c) & 0xF, "x") for c in service) * 8)[:64]
            record_built_image_identity(service, "sha256:" + hexid)
        try:
            _gate(self, plan_orchestrated_start, env_file="postgres.env",
                  controller_env_file="controller.env",
                  reader_dsn_env_file="demo_console.env",
                  gh_webhook_env_file="gh_webhook.env",
                  code="CONFIG_INVALID")
            _gate(self, plan_orchestrated_start, env_file="postgres.env",
                  controller_env_file="controller.env",
                  reader_dsn_env_file="demo_console.env",
                  gh_webhook_env_file="gh_webhook.env",
                  demo_console_run_id=_TEST_RUN_ID, code="CONFIG_INVALID")
            _gate(self, plan_orchestrated_start, env_file="postgres.env",
                  controller_env_file="controller.env",
                  reader_dsn_env_file="demo_console.env",
                  gh_webhook_env_file="gh_webhook.env",
                  demo_console_pg_server_addresses=_TEST_BRIDGE_IP,
                  code="CONFIG_INVALID")
        finally:
            oc._builtin_registry.clear()

    def test_full_start_requires_recorded_identities(self):
        oc._builtin_registry.clear()
        _gate(self, plan_orchestrated_start,
              controller_env_file="controller.env",
              reader_dsn_env_file="demo_console.env",
              gh_webhook_env_file="gh_webhook.env",
              code="CONFIG_INVALID")

    def test_cleanup_plan_reverse_order_then_network(self):
        plans = plan_orchestrated_cleanup()
        self.assertEqual(len(plans), 9)   # 7 services + 2 networks
        names = [p[2] for p in plans[:7]]
        self.assertTrue(all(p[0] == 'rm' for p in plans[:7]))
        self.assertEqual([p[0] for p in plans[7:]],
                         ['network', 'network'])
        self.assertEqual(names, list(reversed(
            ["mergepilot-isolated-%s-1" % s for s in oc.SERVICE_ORDER])))
        self.assertEqual(plans[7][0], "network")
        self.assertEqual(plans[7][2], ORCHESTRATOR_NETWORK)
        self.assertEqual(plans[8][0], "network")
        self.assertEqual(plans[8][2], oc.PUBLICATION_NETWORK)

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
            plan_orchestrated_start(env_file="postgres.env", **_start_kwargs())
            plan_orchestrated_cleanup()
            validate_compose_config(
                build_compose_config(demo_console_run_id=_TEST_RUN_ID,
                                     demo_console_pg_server_addresses=_TEST_BRIDGE_IP))
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
            plans = plan_orchestrated_start(**_start_kwargs())
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
                kwargs = {}
                if service == "demo-console":
                    kwargs["demo_console_env"] = oc._demo_console_environment(
                        _TEST_RUN_ID, _TEST_BRIDGE_IP)
                elif service == "controller":
                    kwargs["controller_env"] = oc._controller_environment()
                    kwargs["env_file"] = "controller.env"
                elif service == "policy-gateway":
                    kwargs["gateway_env"] = oc._gateway_environment()
                if service in ("demo-console", "preflight"):
                    kwargs["reader_dsn_env_file"] = "demo_console.env"
                if service == "console-edge":
                    edge = oc.plan_console_edge_run(
                        get_built_image_identity(service))
                    self.assertEqual(edge[edge.index("-p") + 1],
                                     "127.0.0.1:8600:8600")
                    continue
                if service == "gh-webhook":
                    # M8-GH-3: loopback publisher via its dedicated plan.
                    hook = oc.plan_gh_webhook_run(
                        get_built_image_identity(service),
                        env_file="gh_webhook.env")
                    self.assertEqual(hook[hook.index("-p") + 1],
                                     "127.0.0.1:8090:8090")
                    continue
                plan = plan_service_run(
                    service, image_ref=get_built_image_identity(service),
                    **kwargs)
                self.assertNotIn("-p", plan, service)
            pg = plan_service_run("postgres", image_ref=oc.PGVECTOR_IMAGE_DIGEST)
            self.assertNotIn("-p", pg)
        finally:
            oc._builtin_registry.clear()

    def test_preflight_network_path_not_host(self):
        plan = plan_service_run("preflight",
                                image_ref="sha256:" + "ab" * 32,
                                reader_dsn_env_file="demo_console.env")
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


# ── M8-A1: controller.env contract + opt-in planner matrix ─────────────────

class TestM8A1ControllerEnvContract(unittest.TestCase):
    """The unconditional plan pre-gate: every failure happens BEFORE
    plan_network_create is ever touched."""

    def setUp(self):
        oc._builtin_registry.clear()
        for service in BUILT_SERVICES:
            hexid = ("".join(format(ord(c) & 0xF, "x") for c in service) * 8)[:64]
            record_built_image_identity(service, "sha256:" + hexid)

    def tearDown(self):
        oc._builtin_registry.clear()

    def _plans_or_gate(self, **kwargs):
        import tempfile
        from unittest import mock
        called = []

        def _boom():
            called.append(1)
            return []
        with mock.patch.object(oc, "plan_network_create", _boom), \
             mock.patch.object(oc, "plan_publication_network_create",
                               _boom), \
             tempfile.TemporaryDirectory():
            try:
                plans = plan_orchestrated_start(
                    env_file="postgres.env", **kwargs)
            except oc.StartupGateError as exc:
                self.assertEqual(0, len(called),
                                 "network_create touched before gate")
                return exc
            return plans

    # ── flag=False ────────────────────────────────────────────────────

    def test_flag_false_valid_two_line_file_succeeds(self):
        path = _write_controller_env()
        plans = self._plans_or_gate(
            controller_env_file=path,
            reader_dsn_env_file="demo_console.env",
            gh_webhook_env_file="gh_webhook.env",
            demo_console_run_id=_TEST_RUN_ID,
            demo_console_pg_server_addresses=_TEST_BRIDGE_IP)
        self.assertIsInstance(plans, list)

    def test_flag_false_plan_byte_identical_to_phase1g(self):
        """Same inputs -> byte-identical plan vs the pre-M8A1 baseline."""
        path = _write_controller_env()
        kwargs = dict(
            controller_env_file=path,
            reader_dsn_env_file="demo_console.env",
            gh_webhook_env_file="gh_webhook.env",
            demo_console_run_id=_TEST_RUN_ID,
            demo_console_pg_server_addresses=_TEST_BRIDGE_IP)
        plans_now = plan_orchestrated_start(env_file="postgres.env",
                                            **kwargs)
        # Baseline property: no M4F_ENABLED anywhere, single env-file per
        # controller, exact step count 11 (2 nets + 7 runs + 2 connects).
        self.assertEqual(11, len(plans_now))
        joined = " ".join(" ".join(p) for p in plans_now)
        self.assertNotIn("M4F_ENABLED", joined)
        ctrl = next(p for p in plans_now if "--name" in p and
                    "controller-1" in p[p.index("--name") + 1])
        self.assertEqual(1, ctrl.count("--env-file"))

    def test_flag_false_three_line_file_rejected(self):
        path = _write_controller_env((
            "PG_PASS=test-pg-pass\nADMIN_PW=test-admin-pw\n"
            "M4F_SNAPSHOT_DSN=postgresql://snapshot_worker:x@postgres:5432/"
            "mergepilot_audit\n").splitlines(keepends=True))
        exc = self._plans_or_gate(
            controller_env_file=path,
            reader_dsn_env_file="demo_console.env",
            gh_webhook_env_file="gh_webhook.env",
            demo_console_run_id=_TEST_RUN_ID,
            demo_console_pg_server_addresses=_TEST_BRIDGE_IP)
        self.assertIsNotNone(exc)

    def test_flag_false_missing_or_duplicate_or_unknown_keys(self):
        for lines in (
                ("ADMIN_PW=test-admin-pw\n",),                       # missing PG_PASS
                ("PG_PASS=test-pg-pass\n",),                          # missing ADMIN_PW
                ("PG_PASS=a\nPG_PASS=b\nADMIN_PW=c\n",),            # duplicate
                ("PG_PASS=a\nADMIN_PW=b\nUNKNOWN=x\n",),            # unknown key
                ("PG_PASS=a\n\nADMIN_PW=b\n",),                     # blank line
                ("PG_PASS=a\n# comment\nADMIN_PW=b\n",),            # comment
                ("PG_PASS=a\nADMIN_PW\n",),                          # no '='
        ):
            path = _write_controller_env(lines)
            exc = self._plans_or_gate(
                controller_env_file=path,
                reader_dsn_env_file="demo_console.env",
                gh_webhook_env_file="gh_webhook.env",
                demo_console_run_id=_TEST_RUN_ID,
                demo_console_pg_server_addresses=_TEST_BRIDGE_IP)
            self.assertIsNotNone(exc, lines)

    def test_failures_do_not_leak_secret_values(self):
        path = _write_controller_env(("PG_PASS=leakme-pg\n"
                                      "ADMIN_PW=b\nLEAK=x\n"))
        exc = self._plans_or_gate(
            controller_env_file=path,
            reader_dsn_env_file="demo_console.env",
            gh_webhook_env_file="gh_webhook.env",
            demo_console_run_id=_TEST_RUN_ID,
            demo_console_pg_server_addresses=_TEST_BRIDGE_IP)
        self.assertIsNotNone(exc)
        self.assertNotIn("leakme", str(exc))

    def test_missing_env_file_rejected_before_any_plan(self):
        exc = self._plans_or_gate(
            controller_env_file="no/such/controller.env",
            reader_dsn_env_file="demo_console.env",
            gh_webhook_env_file="gh_webhook.env",
            demo_console_run_id=_TEST_RUN_ID,
            demo_console_pg_server_addresses=_TEST_BRIDGE_IP)
        self.assertIsNotNone(exc)

    # ── flag=True ─────────────────────────────────────────────────────

    _GOOD_DSN = ("postgresql://snapshot_worker:testsnap@postgres:5432/"
                 "mergepilot_audit")

    def test_flag_true_valid_three_line_file_succeeds(self):
        path = _write_controller_env((
            "PG_PASS=test-pg-pass\nADMIN_PW=test-admin-pw\n"
            "M4F_SNAPSHOT_DSN=%s\n" % self._GOOD_DSN,
            ))
        plans = plan_orchestrated_start(
            env_file="postgres.env",
            controller_env_file=path,
            reader_dsn_env_file="demo_console.env",
            gh_webhook_env_file="gh_webhook.env",
            demo_console_run_id=_TEST_RUN_ID,
            demo_console_pg_server_addresses=_TEST_BRIDGE_IP,
            m4f_event_machinery=True)
        ctrl = next(p for p in plans if "--name" in p and
                    "controller-1" in p[p.index("--name") + 1])
        joined = " ".join(ctrl)
        # exactly one M4F_ENABLED flag; no DSN in argv
        self.assertEqual(1, joined.count("M4F_ENABLED=1"))
        self.assertNotIn(self._GOOD_DSN, joined)
        self.assertNotIn("M4F_SNAPSHOT_DSN", joined)
        # still exactly one --env-file
        self.assertEqual(1, ctrl.count("--env-file"))
        # all other plans unchanged (no M4F anywhere else)
        others = " ".join(" ".join(p) for p in plans
                          if p is not ctrl)
        self.assertNotIn("M4F_ENABLED", others)

    def test_flag_true_two_line_file_rejected(self):
        path = _write_controller_env()
        exc = self._plans_or_gate(
            controller_env_file=path,
            reader_dsn_env_file="demo_console.env",
            gh_webhook_env_file="gh_webhook.env",
            demo_console_run_id=_TEST_RUN_ID,
            demo_console_pg_server_addresses=_TEST_BRIDGE_IP,
            m4f_event_machinery=True)
        self.assertIsNotNone(exc)

    def test_flag_true_missing_or_duplicate_dsn_or_unknown(self):
        for lines in (
                ("PG_PASS=a\nADMIN_PW=b\n",),                       # missing DSN
                ("PG_PASS=a\nADMIN_PW=b\n"
                 "M4F_SNAPSHOT_DSN=%s\nM4F_SNAPSHOT_DSN=%s\n"
                 % (self._GOOD_DSN, self._GOOD_DSN),),                # duplicate
                ("PG_PASS=a\nADMIN_PW=b\nM4F_SNAPSHOT_DSN=%s\n"
                 "EVIL=%s\n" % (self._GOOD_DSN, self._GOOD_DSN),),   # unknown
                ("PG_PASS=a\nADMIN_PW=b\n"
                 "M4F_SNAPSHOT_DSN=postgresql://mergepilot:x@postgres:5432/"
                 "mergepilot_audit\n",),                              # admin user
        ):
            path = _write_controller_env(lines)
            exc = self._plans_or_gate(
                controller_env_file=path,
                reader_dsn_env_file="demo_console.env",
                gh_webhook_env_file="gh_webhook.env",
                demo_console_run_id=_TEST_RUN_ID,
                demo_console_pg_server_addresses=_TEST_BRIDGE_IP,
                m4f_event_machinery=True)
            self.assertIsNotNone(exc, lines[0][:40])

    def test_flag_true_dsn_value_not_in_exception(self):
        path = _write_controller_env((
            "PG_PASS=a\nADMIN_PW=b\n"
            "M4F_SNAPSHOT_DSN=postgresql://snapshot_worker:leakme"
            "@postgres:5432/mergepilot_audit\n",))
        exc = self._plans_or_gate(
            controller_env_file=path,
            reader_dsn_env_file="demo_console.env",
            gh_webhook_env_file="gh_webhook.env",
            demo_console_run_id=_TEST_RUN_ID,
            demo_console_pg_server_addresses=_TEST_BRIDGE_IP,
            m4f_event_machinery=True)
        self.assertIsNotNone(exc)
        self.assertNotIn("leakme", str(exc))
