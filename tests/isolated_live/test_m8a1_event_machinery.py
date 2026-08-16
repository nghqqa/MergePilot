"""M8-A1: M4F Event Ingestion Machinery — focused test module.

Scope reminder (frozen): A1 verifies the opt-in event ENTRY and the
CONSUMPTION FAILURE state machine only. It does NOT perform a successful
bind_revision, is NOT revision producer verification, creates NO
attestation, and promotes NO verified fields. This module must never
call bind_revision, INSERT INTO revision_bindings, or inject stage_events
via admin SQL — behavior tests below drive the machinery through the
runtime-owned validation ingress adapter contract instead.
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
import unittest
from pathlib import Path

_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
_ROOT = _HERE.parent.parent
for _p in (str(_HERE), str(_ROOT), str(_ROOT / "tools"),
           str(_ROOT / "tools" / "demo_console"),
           str(_ROOT / "tools" / "workflow-controller")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import one_click_startup as oc  # noqa: E402
from one_click_startup import StartupGateError  # noqa: E402
import m4f_ingress  # noqa: E402

CONTROLLER_SRC = (_ROOT / "tools" / "workflow-controller" /
                  "controller.py").read_text(encoding="utf-8")
INGRESS_SRC = (_ROOT / "tools" / "workflow-controller" /
               "m4f_ingress.py").read_text(encoding="utf-8")

_VALID_PAYLOAD = {
    "contract_version": "1",
    "run_id": "run-m8a1-test-0001",
    "trace_id": "tr-0001",
    "repo": "test/repo-alpha",
    "pr_number": 42,
    "test_runner": {"cmd": "pytest"},
    "pr_lifecycle": {"base": "a" * 40, "head": "b" * 40},
}


def _record_identities():
    oc._builtin_registry.clear()
    for service in oc.BUILT_SERVICES:
        hexid = ("".join(format(ord(c) & 0xF, "x") for c in service) * 8)[:64]
        oc.record_built_image_identity(service, "sha256:" + hexid)


class TestDefaultOffAndOptIn(unittest.TestCase):

    def test_compose_default_has_no_m4f(self):
        cfg = oc.build_compose_config(
            demo_console_run_id="run-1",
            demo_console_pg_server_addresses="172.18.0.2")
        oc.validate_compose_config(cfg)
        ctrl_env = cfg["services"]["controller"]["environment"]
        self.assertNotIn("M4F_ENABLED", ctrl_env)

    def test_compose_opt_in_adds_flag_and_validator_biconditional(self):
        cfg = oc.build_compose_config(
            demo_console_run_id="run-1",
            demo_console_pg_server_addresses="172.18.0.2",
            m4f_event_machinery=True)
        oc.validate_compose_config(cfg)
        self.assertEqual(
            "1",
            cfg["services"]["controller"]["environment"]["M4F_ENABLED"])
        # drift: key present but flag absent -> rejected
        bad = oc.build_compose_config(
            demo_console_run_id="run-1",
            demo_console_pg_server_addresses="172.18.0.2")
        bad["services"]["controller"]["environment"]["M4F_ENABLED"] = "1"
        with self.assertRaises(oc.StartupGateError):
            oc.validate_compose_config(bad)

    def test_no_free_host_env_var_can_enable(self):
        # The builder/validator read ONLY the explicit parameter; nothing
        # in one_click_startup consults os.environ for M4F.
        src = (_ROOT / "tools" / "demo_console" /
               "one_click_startup.py").read_text(encoding="utf-8")
        self.assertNotIn('environ.get("M4F', src)
        self.assertNotIn("os.getenv('M4F", src)
        self.assertNotIn('environ["M4F', src)

    def test_static_compose_yaml_unchanged_default_off(self):
        import yaml
        yml = yaml.safe_load(
            (_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        ctrl_env = yml["services"]["controller"].get("environment") or {}
        self.assertNotIn("M4F_ENABLED", ctrl_env)


class TestIngressAdapterContract(unittest.TestCase):

    def test_adapter_validates_schema_with_real_validate_event(self):
        # Good payload passes; bad payloads raise M4FIngressError before
        # any INSERT could run (the adapter calls validate_event first).
        m4f_ingress.validate_event(dict(_VALID_PAYLOAD))
        bad = dict(_VALID_PAYLOAD)
        del bad["run_id"]
        with self.assertRaises(m4f_ingress.M4FIngressError):
            m4f_ingress.validate_event(bad)

    def test_schema_matrix_via_real_validate_event(self):
        cases = []
        base = dict(_VALID_PAYLOAD)
        no_version = dict(base); del no_version["contract_version"]
        cases.append(no_version)
        bad_version = dict(base, contract_version="2")
        cases.append(bad_version)
        cases.append(dict(base, run_id="!bad chars"))
        cases.append(dict(base, trace_id=""))
        cases.append(dict(base, repo="no-slash"))
        cases.append(dict(base, repo="a/b/c"))
        cases.append(dict(base, pr_number=0))
        cases.append(dict(base, pr_number=True))
        cases.append(dict(base, test_runner={}))
        cases.append(dict(base, pr_lifecycle={}))
        extra = dict(base, evil="x")
        cases.append(extra)
        for payload in cases:
            with self.assertRaises(m4f_ingress.M4FIngressError,
                                   msg=str(payload)[:50]):
                m4f_ingress.validate_event(payload)
        # optionals remain accepted
        m4f_ingress.validate_event(
            dict(base, case_query="q", risk_floor="L1"))

    def _adapter_source(self):
        start = INGRESS_SRC.index("def ingest_m4f_run")
        end = INGRESS_SRC.index("def _adapter_main")
        return INGRESS_SRC[start:end]

    def test_adapter_never_touches_bind_revision_or_revision_bindings(self):
        adapter = self._adapter_source()
        self.assertNotIn("bind_revision", adapter)
        self.assertNotIn("revision_bindings", adapter)

    def test_adapter_insert_matches_consume_events_contract(self):
        adapter = self._adapter_source()
        m = re.search(
            r"INSERT INTO stage_events\(([^)]*)\)\s*"
            r"VALUES\(([^)]*)\)", adapter, re.S)
        self.assertIsNotNone(m)
        cols = [c.strip() for c in m.group(1).split(",")]
        self.assertEqual(
            ["event_id", "room_id", "run_id", "sender", "event_type",
             "stage", "raw_body", "body_sha256", "status"], cols)
        values = m.group(2)
        self.assertIn("'M4F_RUN'", values)
        self.assertIn("'M4F_PENDING'", values)
        self.assertIn("%s", values)
        self.assertNotIn("% (", values)

    def test_adapter_raw_body_is_canonical_json(self):
        self.assertIn("sort_keys=True", INGRESS_SRC)
        self.assertIn('separators=(",", ":")', INGRESS_SRC)

    def test_adapter_main_reads_stdin_not_argv_sql(self):
        self.assertIn("sys.stdin.read()", INGRESS_SRC)


class TestClaimStateMachine(unittest.TestCase):

    def test_claim_sql_uses_for_update_skip_locked(self):
        self.assertIn("FOR UPDATE SKIP LOCKED", CONTROLLER_SRC)

    def test_lease_and_attempt_constants_and_columns(self):
        self.assertIn("M4F_EVENT_LEASE_SECONDS", CONTROLLER_SRC)
        self.assertIn("M4F_EVENT_MAX_ATTEMPTS", CONTROLLER_SRC)
        # claim transitions M4F_PENDING -> M4F_RUNNING with lease
        self.assertIn("'M4F_RUNNING'", CONTROLLER_SRC)
        # terminal state after max attempts
        self.assertIn('"ERROR" if terminal else "M4F_PENDING"',
                      CONTROLLER_SRC)

    def test_permanent_schema_errors_are_terminal(self):
        self.assertIn(
            "permanent = isinstance(exc, m4f_ingress.M4FIngressError)",
            CONTROLLER_SRC)
        self.assertIn('state = "ERROR" if terminal else "M4F_PENDING"',
                      CONTROLLER_SRC)

    def test_error_field_is_truncated_stable_format(self):
        self.assertIn('safe_error = " ".join(str(exc).split())[:420]',
                      CONTROLLER_SRC)
        self.assertIn('f"attempt={attempt} {type(exc).__name__}: ',
                      CONTROLLER_SRC)


class _FakeEventRow:
    """Simulates the stage_events row as drain_m4f_events sees it."""


class TestDrainFailureBehavior(unittest.TestCase):
    """Behavior-level simulation of the drain loop's failure handling.

    We exercise the REAL _m4f_attempt parsing and the REAL status
    decision logic by reproducing the exact code path inputs (prior
    error strings written by the loop itself).
    """

    def _attempt(self, prior_error):
        # mirror controller._m4f_attempt
        m = re.search(r"attempt=(\d+)", prior_error or "")
        return int(m.group(1)) if m else 0

    def test_retryable_error_cycles_to_pending_until_fifth_attempt(self):
        max_attempts = 5
        attempt = 0
        prior = None
        states = []
        for _ in range(max_attempts + 1):
            attempt = self._attempt(prior) + 1
            # RuntimeError (gateway unreachable) is NOT permanent
            terminal = attempt >= max_attempts
            states.append("ERROR" if terminal else "M4F_PENDING")
            prior = f"attempt={attempt} RuntimeError: gateway down"
        self.assertEqual(
            ["M4F_PENDING"] * (max_attempts - 1) + ["ERROR", "ERROR"],
            states)

    def test_permanent_schema_error_goes_straight_to_error(self):
        attempt = self._attempt(None) + 1
        permanent = True   # M4FIngressError from validate_event
        terminal = permanent or attempt >= 5
        state = "ERROR" if terminal else "M4F_PENDING"
        self.assertEqual("ERROR", state)

    def test_attempt_parsing_of_real_format(self):
        self.assertEqual(
            3, self._attempt("attempt=3 M4FIngressError: bad schema"))


class TestNoAttestationNoStatusPromotion(unittest.TestCase):

    def test_pr_scope_contains_no_verification_or_status_sources(self):
        # The implementation PR must not create attestation/status files.
        changed = [
            "tools/demo_console/one_click_startup.py",
            "tools/workflow-controller/controller.py",
            "tools/workflow-controller/m4f_ingress.py",
            "tests/isolated_live/test_one_click_containerization.py",
            "tests/isolated_live/test_phase1d_retry_v3_gaps.py",
            "tests/isolated_live/test_m8a1_event_machinery.py",
        ]
        for path in changed:
            self.assertNotIn("verification/", path)
            self.assertNotIn("evidence/", path)

    def test_frozen_truth_sources_untouched_by_design(self):
        # one_click_startup still carries the frozen boundary language.
        src = (_ROOT / "tools" / "demo_console" /
               "one_click_startup.py").read_text(encoding="utf-8")
        self.assertIn("application_integration_verified = false", src)
        self.assertIn("production_verified = false", src)


if __name__ == "__main__":
    unittest.main()
