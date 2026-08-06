#!/usr/bin/env python3
"""Pure-local policy tests for the M5-0C real-GitHub policy template
(config/m5-0c-real-github-policy.yaml).

No Gateway, no Docker, no real GitHub I/O. A minimal in-test evaluator models the
policy decision the Gateway would make from this YAML, so the template's stated
boundary (the comment block at the bottom of the YAML) is machine-verified.

Scope (v2.7 §26.4): m5coordinator strictly read-only; fixer writes ONLY via
pr-lifecycle against the fixture repo on fix/ branches; no auto-merge; no
example/project; no production repo; no secrets in the template.

NOTE: this is a STATIC contract test — it verifies the YAML *shape* with a
self-made _Evaluator. It does NOT exercise tools/policy-gateway/gateway.py.
The REAL gateway.py authorization path (HTTP/SSE/MCP) is covered by
test_gateway_runtime.py. The two suites are complementary and NOT substitutable.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
POLICY = ROOT / "config" / "m5-0c" / "real-github-policy.yaml"
FIXTURE_REPO = "nghqqa/MergePilot-e2e-fixture"


@pytest.fixture(scope="module")
def policy():
    return yaml.safe_load(POLICY.read_text(encoding="utf-8"))


class _Evaluator:
    """Minimal local model of the Gateway policy decision (not the Gateway)."""

    def __init__(self, p):
        self.repos = set(p["repos"]["allowlist"])
        self.protected = set(p["branches"]["protected"])
        self.fix_prefix = p["branches"]["fix_prefix"]
        self.classes_of = {r: set(c["classes"]) for r, c in p["roles"].items()}
        self.tools_of = {cls: set(t) for cls, t in p["tool_classes"].items()}

    def classes_for(self, role):
        return self.classes_of[role]

    def allow(self, role, tool, repo, branch=None):
        if repo not in self.repos:
            return False
        role_classes = self.classes_of[role]
        owner_class = next(
            (cls for cls, tools in self.tools_of.items() if tool in tools), None
        )
        if owner_class is None or owner_class not in role_classes:
            return False
        if owner_class == "fix":
            if branch is None:
                return False
            if branch in self.protected:
                return False
            if not branch.startswith(self.fix_prefix):
                return False
        return True


@pytest.fixture(scope="module")
def ev(policy):
    return _Evaluator(policy)


# ── P1: allowlist scope ────────────────────────────────────────────────────────
def test_no_example_project_in_allowlist(policy):
    assert "example/project" not in policy["repos"]["allowlist"]


def test_allowlist_exactly_fixture_repo(policy):
    assert policy["repos"]["allowlist"] == [FIXTURE_REPO]


# ── P2: role ↔ tool_classes resolution matches the documented boundary ─────────
def test_roles_resolve_as_documented(policy, ev):
    assert ev.classes_for("m5coordinator") == {"read"}
    assert ev.classes_for("reviewer") == {"read"}
    assert ev.classes_for("verifier") == {"read"}
    assert ev.classes_for("fixer") == {"read", "fix"}


def test_tool_classes_membership(ev):
    assert "create_branch" in ev.tools_of["fix"]
    assert "push_files" in ev.tools_of["fix"]
    assert "create_pull_request" in ev.tools_of["fix"]
    assert {"create_branch", "push_files", "create_pull_request"} & ev.tools_of["read"] == set()


# ── P3: m5coordinator is strictly read-only (fix DENIED) ───────────────────────
def test_m5coordinator_read_allowed(ev):
    assert ev.allow("m5coordinator", "pull_request_read", FIXTURE_REPO) is True


@pytest.mark.parametrize("tool", ["create_branch", "push_files", "create_pull_request"])
def test_m5coordinator_fix_denied(ev, tool):
    assert ev.allow("m5coordinator", tool, FIXTURE_REPO, "fix/m5live-1") is False


# ── P4: fixer writes allowed only on fixture repo + fix/ branch ────────────────
@pytest.mark.parametrize("tool", ["create_branch", "push_files", "create_pull_request"])
def test_fixer_fix_allowed_on_fixture_fix_branch(ev, tool):
    assert ev.allow("fixer", tool, FIXTURE_REPO, "fix/m5live-1") is True


def test_fixer_denied_on_non_fixture_repo(ev):
    assert ev.allow("fixer", "create_branch", "nghqqa/MergePilot", "fix/x") is False
    assert ev.allow("fixer", "push_files", "example/project", "fix/x") is False


def test_fixer_denied_on_protected_main(ev):
    assert ev.allow("fixer", "push_files", FIXTURE_REPO, "main") is False


def test_fixer_denied_on_non_fix_prefix(ev):
    assert ev.allow("fixer", "create_branch", FIXTURE_REPO, "feat/x") is False
    assert ev.allow("fixer", "create_branch", FIXTURE_REPO, "m5live-1") is False


# ── P5: no secrets in the template ─────────────────────────────────────────────
def test_template_has_no_credentials():
    text = POLICY.read_text(encoding="utf-8")
    for needle in ("ghp_", "github_pat_", "BEGIN PRIVATE KEY", "registration_token="):
        assert needle.lower() not in text.lower(), f"template must not contain {needle!r}"


# ── P6: branch policy structural invariants ────────────────────────────────────
def test_branch_policy_invariants(policy):
    assert policy["branches"]["base_allowlist"] == ["main"]
    assert policy["branches"]["fix_prefix"] == "fix/"
    assert policy["branches"]["protected"] == ["main"]
