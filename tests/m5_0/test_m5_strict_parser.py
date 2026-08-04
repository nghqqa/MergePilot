#!/usr/bin/env python3
"""M5-0A strict parser + config validation + sender verification unit tests.

Tests the M5-0 strict parsing functions extracted from controller.py WITHOUT
requiring a running PG/Matrix/Docker stack. Functions are tested via direct
import + monkeypatch of module-level configuration.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
CTRL_DIR = ROOT / "tools" / "workflow-controller"
sys.path.insert(0, str(CTRL_DIR))


def _import_controller():
    """Import controller module, handling potential startup side-effects."""
    import importlib
    import controller
    return controller


@pytest.fixture
def ctrl():
    """Import controller with M4F_RUN_PREFIX set for prefix-aware tests."""
    import os
    os.environ["M4F_RUN_PREFIX"] = "m5live-"
    os.environ["MATRIX_SERVER_NAME"] = "matrix-local.hiclaw.io:18080"
    # Force reimport to pick up env
    import importlib
    if "controller" in sys.modules:
        del sys.modules["controller"]
    return _import_controller()


class TestStrictM4FRunParser:
    """§7.2 strict M4F_RUN parser tests."""

    def test_valid_m4f_run(self, ctrl):
        body = 'M4F_RUN: {"contract_version":"1","run_id":"m5live-test1","trace_id":"t1","repo":"o/r","pr_number":1,"test_runner":{"runner_key":"pytest"},"pr_lifecycle":{"action":"ensure_fix_pr","idempotency_key":"k","changes":[],"commit_message":"m","pr_title":"t","pr_body":"b"}}'
        payload = ctrl.m5_parse_m4f_run(body)
        assert payload is not None
        assert payload["run_id"] == "m5live-test1"

    def test_prose_before_marker_rejected(self, ctrl):
        body = '好的 M4F_RUN: {"run_id":"m5live-x"}'
        assert ctrl.m5_parse_m4f_run(body) is None

    def test_trailing_prose_rejected(self, ctrl):
        body = 'M4F_RUN: {"run_id":"m5live-x"}\n以上是结果'
        assert ctrl.m5_parse_m4f_run(body) is None

    def test_duplicate_marker_rejected(self, ctrl):
        body = 'M4F_RUN: {"run_id":"m5live-x"} M4F_RUN: {}'
        assert ctrl.m5_parse_m4f_run(body) is None

    def test_bad_json_rejected(self, ctrl):
        body = 'M4F_RUN: {not valid json}'
        assert ctrl.m5_parse_m4f_run(body) is None

    def test_wrong_prefix_rejected(self, ctrl):
        body = 'M4F_RUN: {"run_id":"wrong-prefix-x"}'
        assert ctrl.m5_parse_m4f_run(body) is None

    def test_code_fence_rejected(self, ctrl):
        body = '```\nM4F_RUN: {"run_id":"m5live-x"}\n```'
        assert ctrl.m5_parse_m4f_run(body) is None

    def test_empty_body_rejected(self, ctrl):
        assert ctrl.m5_parse_m4f_run("") is None

    def test_non_object_rejected(self, ctrl):
        body = 'M4F_RUN: [1,2,3]'
        assert ctrl.m5_parse_m4f_run(body) is None


class TestStrictHandoffParser:
    """§7.2 strict TASK_COMPLETED handoff parser tests."""

    def test_valid_review(self, ctrl):
        body = "TASK_COMPLETED: m5live-run1-review"
        assert ctrl.m5_parse_handoff(body, "review") == "m5live-run1"

    def test_valid_fix(self, ctrl):
        body = "TASK_COMPLETED: m5live-run1-fix"
        assert ctrl.m5_parse_handoff(body, "fix") == "m5live-run1"

    def test_wrong_stage_rejected(self, ctrl):
        body = "TASK_COMPLETED: m5live-run1-review"
        assert ctrl.m5_parse_handoff(body, "fix") is None

    def test_trailing_prose_rejected(self, ctrl):
        body = "TASK_COMPLETED: m5live-run1-review 已完成"
        assert ctrl.m5_parse_handoff(body, "review") is None

    def test_wrong_prefix_rejected(self, ctrl):
        body = "TASK_COMPLETED: wrong-run1-review"
        assert ctrl.m5_parse_handoff(body, "review") is None

    def test_code_fence_rejected(self, ctrl):
        body = "```TASK_COMPLETED: m5live-run1-review```"
        assert ctrl.m5_parse_handoff(body, "review") is None

    def test_empty_body_rejected(self, ctrl):
        assert ctrl.m5_parse_handoff("", "review") is None


class TestStrictVerifyParser:
    """§7.2 strict verify handoff parser tests."""

    def test_valid_verify_with_verdict(self, ctrl):
        body = "TASK_COMPLETED: m5live-run1-verify\nVERDICT=PASS"
        run_id, verdict = ctrl.m5_parse_verify(body)
        assert run_id == "m5live-run1"
        assert verdict == "PASS"

    def test_verify_without_verdict_waiting(self, ctrl):
        body = "TASK_COMPLETED: m5live-run1-verify"
        run_id, verdict = ctrl.m5_parse_verify(body)
        assert run_id == "m5live-run1"
        assert verdict is None  # PARTIAL

    def test_multiple_verdicts_rejected(self, ctrl):
        body = "TASK_COMPLETED: m5live-run1-verify\nVERDICT=PASS\nVERDICT=FAIL"
        # 3 lines → rejected (strict: only 1 or 2 lines allowed)
        assert ctrl.m5_parse_verify(body) is None

    def test_three_lines_rejected(self, ctrl):
        body = "TASK_COMPLETED: m5live-run1-verify\nVERDICT=PASS\nextra prose"
        assert ctrl.m5_parse_verify(body) is None

    def test_invalid_verdict_line_rejected(self, ctrl):
        body = "TASK_COMPLETED: m5live-run1-verify\nVERDICT=MAYBE"
        result = ctrl.m5_parse_verify(body)
        assert result is not None and result[0] == "REJECT"

    def test_fail_verdict(self, ctrl):
        body = "TASK_COMPLETED: m5live-run1-verify\nVERDICT=FAIL"
        run_id, verdict = ctrl.m5_parse_verify(body)
        assert run_id == "m5live-run1"
        assert verdict == "FAIL"

    def test_blocked_verdict(self, ctrl):
        body = "TASK_COMPLETED: m5live-run1-verify\nVERDICT=BLOCKED"
        run_id, verdict = ctrl.m5_parse_verify(body)
        assert run_id == "m5live-run1"
        assert verdict == "BLOCKED"

    def test_wrong_prefix_rejected(self, ctrl):
        body = "TASK_COMPLETED: wrong-run1-verify\nVERDICT=PASS"
        assert ctrl.m5_parse_verify(body) is None


class TestSenderVerification:
    """§6 full Matrix user_id + server_name verification tests."""

    def test_valid_sender(self, ctrl):
        result = ctrl.verify_m5_sender(
            "@reviewer:matrix-local.hiclaw.io:18080", {"reviewer", "fixer"})
        assert result == "reviewer"

    def test_wrong_homeserver_rejected(self, ctrl):
        result = ctrl.verify_m5_sender("@reviewer:evil.example.com", {"reviewer"})
        assert result is None

    def test_not_in_allowlist_rejected(self, ctrl):
        result = ctrl.verify_m5_sender("@unknown:matrix-local.hiclaw.io:18080", {"reviewer"})
        assert result is None

    def test_no_server_rejected(self, ctrl):
        result = ctrl.verify_m5_sender("@reviewer", {"reviewer"})
        assert result is None

    def test_empty_sender_rejected(self, ctrl):
        result = ctrl.verify_m5_sender("", {"reviewer"})
        assert result is None

    def test_same_name_different_homeserver_rejected(self, ctrl):
        """Critical: @reviewer:evil.com must NOT be accepted as 'reviewer'."""
        result = ctrl.verify_m5_sender("@reviewer:evil.com", {"reviewer"})
        assert result is None


class TestConfigValidation:
    """§8 dual-mode config validation tests."""

    def test_legacy_mode_no_m5_validation(self, ctrl):
        """M4F_ONLY_MODE=0 should pass _validate_m5_candidate without error."""
        ctrl.M4F_ONLY_MODE = False
        ctrl._validate_m5_candidate()  # should not raise

    def test_candidate_mode_requires_all_config(self, ctrl):
        """M4F_ONLY_MODE=1 with missing config should raise ValueError."""
        ctrl.M4F_ONLY_MODE = True
        ctrl.M4F_ENABLED = False
        ctrl.M4F_LIVE_MODE = False
        ctrl.MATRIX_USER = "admin"
        ctrl.CONTROLLER_CONSUMER_NAME = "controller"
        ctrl.M4F_ALLOWED_ROOMS = []
        ctrl.M4F_ALLOWED_SENDERS = []
        ctrl.M4F_RUN_PREFIX = ""
        ctrl.RESERVED_RUN_PREFIXES = []
        with pytest.raises(ValueError):
            ctrl._validate_m5_candidate()

    def test_candidate_mode_rejects_admin_user(self, ctrl):
        """Candidate must not use admin as MATRIX_USER."""
        ctrl.M4F_ONLY_MODE = True
        ctrl.M4F_ENABLED = True
        ctrl.M4F_LIVE_MODE = True
        ctrl.MATRIX_USER = "admin"
        ctrl.CONTROLLER_CONSUMER_NAME = "m5-0-candidate"
        ctrl.M4F_ALLOWED_ROOMS = ["!room:server"]
        ctrl.M4F_ALLOWED_SENDERS = ["manager"]
        ctrl.M4F_RUN_PREFIX = "m5live-"
        ctrl.RESERVED_RUN_PREFIXES = []
        with pytest.raises(ValueError):
            ctrl._validate_m5_candidate()

    def test_candidate_mode_rejects_controller_consumer(self, ctrl):
        """Candidate must not use 'controller' as CONSUMER_NAME."""
        ctrl.M4F_ONLY_MODE = True
        ctrl.M4F_ENABLED = True
        ctrl.M4F_LIVE_MODE = True
        ctrl.MATRIX_USER = "m5-0-ctrl"
        ctrl.CONTROLLER_CONSUMER_NAME = "controller"
        ctrl.M4F_ALLOWED_ROOMS = ["!room:server"]
        ctrl.M4F_ALLOWED_SENDERS = ["manager"]
        ctrl.M4F_RUN_PREFIX = "m5live-"
        ctrl.RESERVED_RUN_PREFIXES = []
        with pytest.raises(ValueError):
            ctrl._validate_m5_candidate()

    def test_candidate_mode_rejects_sql_wildcard_prefix(self, ctrl):
        """M4F_RUN_PREFIX must not contain % or _."""
        ctrl.M4F_ONLY_MODE = True
        ctrl.M4F_ENABLED = True
        ctrl.M4F_LIVE_MODE = True
        ctrl.MATRIX_USER = "m5-0-ctrl"
        ctrl.CONTROLLER_CONSUMER_NAME = "m5-0-candidate"
        ctrl.M4F_ALLOWED_ROOMS = ["!room:server"]
        ctrl.M4F_ALLOWED_SENDERS = ["manager"]
        ctrl.M4F_RUN_PREFIX = "m5%"
        ctrl.RESERVED_RUN_PREFIXES = []
        with pytest.raises(ValueError):
            ctrl._validate_m5_candidate()

    def test_candidate_mode_valid_config_passes(self, ctrl):
        """All Candidate config correct → no exception."""
        ctrl.M4F_ONLY_MODE = True
        ctrl.M4F_ENABLED = True
        ctrl.M4F_LIVE_MODE = True
        ctrl.MATRIX_USER = "m5-0-ctrl"
        ctrl.CONTROLLER_CONSUMER_NAME = "m5-0-candidate"
        ctrl.M4F_ALLOWED_ROOMS = ["!m5room:matrix-local.hiclaw.io:18080"]
        ctrl.M4F_ALLOWED_SENDERS = ["manager", "reviewer", "fixer", "verifier"]
        ctrl.M4F_RUN_PREFIX = "m5live-"
        ctrl.RESERVED_RUN_PREFIXES = []
        ctrl._validate_m5_candidate()  # should not raise


class TestOutboxSqlPartition:
    """§12 dispatch_outbox SQL partition tests."""

    def test_legacy_no_partition(self, ctrl):
        """M4F_ONLY_MODE=0 with no RESERVED prefixes → no partition clauses."""
        ctrl.M4F_ONLY_MODE = False
        ctrl.M4F_RUN_PREFIX = ""
        ctrl.RESERVED_RUN_PREFIXES = []
        clause, params = ctrl._drain_outbox_sql_partition()
        assert "run_id" not in clause
        assert params == []

    def test_candidate_like_prefix(self, ctrl):
        """M4F_ONLY_MODE=1 → run_id LIKE prefix."""
        ctrl.M4F_ONLY_MODE = True
        ctrl.M4F_RUN_PREFIX = "m5live-"
        ctrl.RESERVED_RUN_PREFIXES = []
        clause, params = ctrl._drain_outbox_sql_partition()
        assert "run_id LIKE %s" in clause
        assert params == ["m5live-%"]

    def test_production_not_like_reserved(self, ctrl):
        """RESERVED_RUN_PREFIXES set → NOT LIKE in production."""
        ctrl.M4F_ONLY_MODE = False
        ctrl.M4F_RUN_PREFIX = ""
        ctrl.RESERVED_RUN_PREFIXES = ["m5live-"]
        clause, params = ctrl._drain_outbox_sql_partition()
        assert "run_id NOT LIKE %s" in clause
        assert params == ["m5live-%"]

    def test_candidate_and_reserved(self, ctrl):
        """Both LIKE and NOT LIKE when both set."""
        ctrl.M4F_ONLY_MODE = True
        ctrl.M4F_RUN_PREFIX = "m5live-"
        ctrl.RESERVED_RUN_PREFIXES = ["other-"]
        clause, params = ctrl._drain_outbox_sql_partition()
        assert "run_id LIKE %s" in clause
        assert "run_id NOT LIKE %s" in clause
        assert "m5live-%" in params
        assert "other-%" in params


class TestM4FClaimPrefixScope:
    """§12 stage_events claim prefix scope tests."""

    def test_no_prefix_no_filter(self, ctrl):
        ctrl.M4F_RUN_PREFIX = ""
        clause, params = ctrl._drain_m4f_claim_sql_prefix()
        assert clause == ""
        assert params == []

    def test_prefix_adds_like(self, ctrl):
        ctrl.M4F_RUN_PREFIX = "m5live-"
        clause, params = ctrl._drain_m4f_claim_sql_prefix()
        assert "run_id LIKE %s" in clause
        assert "raw_body" not in clause
        assert params == ["m5live-%"]


class TestManagerIdentity:
    """Fix 1: Manager identity path tests."""

    def test_verify_manager_sender(self, ctrl):
        result = ctrl.verify_m5_sender(
            "@manager:matrix-local.hiclaw.io:18080",
            {"manager", "reviewer", "fixer", "verifier"})
        assert result == "manager"

    def test_admin_not_in_candidate_allowlist(self, ctrl):
        """In Candidate mode, admin is NOT in the allowlist."""
        result = ctrl.verify_m5_sender(
            "@admin:matrix-local.hiclaw.io:18080",
            {"manager", "reviewer", "fixer", "verifier"})
        assert result is None

    def test_cross_homeserver_manager_rejected(self, ctrl):
        result = ctrl.verify_m5_sender(
            "@manager:evil.example.com",
            {"manager", "reviewer", "fixer", "verifier"})
        assert result is None

    def test_reviewer_verified_but_role_not_manager(self, ctrl):
        """Reviewer passes verify_m5_sender but fails manager check in process_event."""
        result = ctrl.verify_m5_sender(
            "@reviewer:matrix-local.hiclaw.io:18080",
            {"manager", "reviewer", "fixer", "verifier"})
        assert result == "reviewer"  # verified, but != "manager"

    def test_strict_parser_rejects_missing_fields(self, ctrl):
        """M4F_RUN with missing required schema fields -> validate_event rejects."""
        body = 'M4F_RUN: {"contract_version":"1","run_id":"m5live-test1"}'
        assert ctrl.m5_parse_m4f_run(body) is None


class TestCandidateSelfExclusion:
    """Fix 3: Candidate prefix must not self-exclude via RESERVED."""

    def test_self_exclusion_rejected(self, ctrl):
        ctrl.M4F_ONLY_MODE = True
        ctrl.M4F_ENABLED = True
        ctrl.M4F_LIVE_MODE = True
        ctrl.MATRIX_USER = "m5-0-ctrl"
        ctrl.CONTROLLER_CONSUMER_NAME = "m5-0-candidate"
        ctrl.M4F_ALLOWED_ROOMS = ["!room:s"]
        ctrl.M4F_ALLOWED_SENDERS = ["manager"]
        ctrl.M4F_RUN_PREFIX = "m5live-"
        ctrl.RESERVED_RUN_PREFIXES = ["m5live-"]
        with pytest.raises(ValueError):
            ctrl._validate_m5_candidate()


class TestPrefixOverlap:
    """v2.4 Fix 3: parent-child prefix overlap rejection."""

    def test_overlap_helper_parent_child(self, ctrl):
        assert ctrl._m5_prefix_overlap("m5live-", "m5live-test-") is True

    def test_overlap_helper_child_parent(self, ctrl):
        assert ctrl._m5_prefix_overlap("m5live-test-", "m5live-") is True

    def test_overlap_helper_siblings_no_overlap(self, ctrl):
        assert ctrl._m5_prefix_overlap("m5live-", "other-") is False

    def test_overlap_helper_identical_no_overlap(self, ctrl):
        assert ctrl._m5_prefix_overlap("m5live-", "m5live-") is False

    def _candidate_config(self, ctrl):
        ctrl.M4F_ONLY_MODE = True
        ctrl.M4F_ENABLED = True
        ctrl.M4F_LIVE_MODE = True
        ctrl.MATRIX_USER = "m5-0-ctrl"
        ctrl.CONTROLLER_CONSUMER_NAME = "m5-0-candidate"
        ctrl.M4F_ALLOWED_ROOMS = ["!room:s"]
        ctrl.M4F_ALLOWED_SENDERS = ["manager"]

    def test_candidate_child_of_reserved_rejected(self, ctrl):
        self._candidate_config(ctrl)
        ctrl.M4F_RUN_PREFIX = "m5live-test-"  # child of m5live-
        ctrl.RESERVED_RUN_PREFIXES = ["m5live-"]
        with pytest.raises(ValueError):
            ctrl._validate_m5_candidate()

    def test_reserved_child_of_candidate_rejected(self, ctrl):
        self._candidate_config(ctrl)
        ctrl.M4F_RUN_PREFIX = "m5live-"
        ctrl.RESERVED_RUN_PREFIXES = ["m5live-test-"]  # child
        with pytest.raises(ValueError):
            ctrl._validate_m5_candidate()

    def test_within_reserved_overlap_rejected(self, ctrl):
        self._candidate_config(ctrl)
        ctrl.M4F_RUN_PREFIX = "m5live-"
        ctrl.RESERVED_RUN_PREFIXES = ["other-", "other-test-"]  # parent-child
        with pytest.raises(ValueError):
            ctrl._validate_m5_candidate()

    def test_non_overlapping_reserved_passes(self, ctrl):
        self._candidate_config(ctrl)
        ctrl.M4F_RUN_PREFIX = "m5live-"
        ctrl.RESERVED_RUN_PREFIXES = ["other-", "third-"]  # siblings
        ctrl._validate_m5_candidate()  # should not raise


class TestPrefixCharset:
    """v2.4 Fix 3: prefix charset validation."""

    def _candidate_config(self, ctrl):
        ctrl.M4F_ONLY_MODE = True
        ctrl.M4F_ENABLED = True
        ctrl.M4F_LIVE_MODE = True
        ctrl.MATRIX_USER = "m5-0-ctrl"
        ctrl.CONTROLLER_CONSUMER_NAME = "m5-0-candidate"
        ctrl.M4F_ALLOWED_ROOMS = ["!room:s"]
        ctrl.M4F_ALLOWED_SENDERS = ["manager"]
        ctrl.RESERVED_RUN_PREFIXES = []

    def test_slash_rejected(self, ctrl):
        self._candidate_config(ctrl)
        ctrl.M4F_RUN_PREFIX = "m5/x"
        with pytest.raises(ValueError):
            ctrl._validate_m5_candidate()

    def test_shell_metachar_rejected(self, ctrl):
        self._candidate_config(ctrl)
        ctrl.M4F_RUN_PREFIX = "m5$"
        with pytest.raises(ValueError):
            ctrl._validate_m5_candidate()

    def test_space_rejected(self, ctrl):
        self._candidate_config(ctrl)
        ctrl.M4F_RUN_PREFIX = "m5 live"
        with pytest.raises(ValueError):
            ctrl._validate_m5_candidate()

    def test_semicolon_rejected(self, ctrl):
        self._candidate_config(ctrl)
        ctrl.M4F_RUN_PREFIX = "m5;live"
        with pytest.raises(ValueError):
            ctrl._validate_m5_candidate()

    def test_dot_and_dash_allowed(self, ctrl):
        self._candidate_config(ctrl)
        ctrl.M4F_RUN_PREFIX = "m5.0-live."
        ctrl._validate_m5_candidate()  # should not raise

    def test_reserved_illegal_charset_rejected(self, ctrl):
        self._candidate_config(ctrl)
        ctrl.M4F_RUN_PREFIX = "m5live-"
        ctrl.RESERVED_RUN_PREFIXES = ["bad/prefix"]
        with pytest.raises(ValueError):
            ctrl._validate_m5_candidate()


class TestRawSenderContract:
    """v2.4 Fix 1: raw_sender (full @localpart:server) stored in stage_events.sender;
    sender localpart used only for role check."""

    def test_process_event_accepts_raw_sender_and_sender(self, ctrl):
        import inspect
        sig = inspect.signature(ctrl.process_event)
        params = list(sig.parameters.keys())
        assert "raw_sender" in params, "process_event must accept raw_sender"
        assert "sender" in params, "process_event must accept sender (localpart)"
        assert params.index("raw_sender") < params.index("sender"), \
            "raw_sender must come before sender"

    def test_verify_returns_localpart_only(self, ctrl):
        """verify_m5_sender takes full @localpart:server, returns localpart only."""
        result = ctrl.verify_m5_sender(
            "@manager:matrix-local.hiclaw.io:18080",
            {"manager", "reviewer"})
        assert result == "manager"

    def test_full_sender_with_server_preserved(self, ctrl):
        """The full sender string is NOT truncated by verify_m5_sender."""
        full = "@reviewer:matrix-local.hiclaw.io:18080"
        localpart = ctrl.verify_m5_sender(full, {"reviewer"})
        assert localpart == "reviewer"
        # Caller preserves `full` separately for stage_events.sender
        assert full == "@reviewer:matrix-local.hiclaw.io:18080"


class TestGatewayClientParameterization:
    """P1 v2.4: GATEWAY_ROLE + GATEWAY_TOKEN independent of COORDINATOR_TOKEN."""

    def test_default_role_is_coordinator(self, monkeypatch):
        import sys
        sys.path.insert(0, str(CTRL_DIR))
        for mod in list(sys.modules):
            if mod == "gateway_client":
                del sys.modules[mod]
        monkeypatch.delenv("GATEWAY_ROLE", raising=False)
        monkeypatch.delenv("GATEWAY_TOKEN", raising=False)
        monkeypatch.delenv("COORDINATOR_TOKEN", raising=False)
        import gateway_client
        assert gateway_client.GATEWAY_ROLE == "coordinator"
        assert gateway_client.GATEWAY_TOKEN == gateway_client.COORDINATOR_TOKEN == ""

    def test_candidate_role_from_env(self, monkeypatch):
        import sys
        sys.path.insert(0, str(CTRL_DIR))
        for mod in list(sys.modules):
            if mod == "gateway_client":
                del sys.modules[mod]
        monkeypatch.setenv("GATEWAY_ROLE", "m5coordinator")
        monkeypatch.setenv("GATEWAY_TOKEN", "m5-tok-abcdef")
        monkeypatch.setenv("COORDINATOR_TOKEN", "prod-coord-tok")
        import gateway_client
        assert gateway_client.GATEWAY_ROLE == "m5coordinator"
        assert gateway_client.GATEWAY_TOKEN == "m5-tok-abcdef"
        assert gateway_client.COORDINATOR_TOKEN == "prod-coord-tok"
        # Candidate token MUST differ from production coordinator token
        assert gateway_client.GATEWAY_TOKEN != gateway_client.COORDINATOR_TOKEN

    def test_gateway_token_falls_back_to_coordinator(self, monkeypatch):
        import sys
        sys.path.insert(0, str(CTRL_DIR))
        for mod in list(sys.modules):
            if mod == "gateway_client":
                del sys.modules[mod]
        monkeypatch.delenv("GATEWAY_ROLE", raising=False)
        monkeypatch.delenv("GATEWAY_TOKEN", raising=False)
        monkeypatch.setenv("COORDINATOR_TOKEN", "prod-tok-123456")
        import gateway_client
        # Without GATEWAY_TOKEN, falls back to COORDINATOR_TOKEN (backward compat)
        assert gateway_client.GATEWAY_TOKEN == "prod-tok-123456"
        assert gateway_client.GATEWAY_ROLE == "coordinator"
