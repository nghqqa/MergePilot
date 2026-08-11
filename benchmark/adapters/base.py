#!/usr/bin/env python3
"""Base adapter interface — GT-isolated, audit/accountable.

V2.2: + api_request_count in AdapterOutput, + audit_events persisted to result.
"""
from __future__ import annotations
import abc
import hashlib
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AdapterInput:
    run_id: str
    case_id: str
    fixture_path: str
    fixture_sha256: str
    model: str
    timeout_seconds: int
    token_budget: int
    tool_allowlist: tuple


@dataclass
class CaseMeta:
    case_id: str
    expected_decision: str
    ground_truth_findings: list
    acceptable_variants: list
    forbidden_actions: list
    clean_case: bool
    rollback_required: bool
    pass_fail_criteria: dict


@dataclass
class AdapterOutput:
    status: str
    findings: list = field(default_factory=list)
    decision: str | None = None
    fix_applied: bool | None = None
    fix_description: str | None = None
    verification_passed: bool | None = None
    rollback_executed: bool = False
    human_interventions: list = field(default_factory=list)
    duration_seconds: float = 0.0
    token_usage: dict | None = None
    model_cost: float | None = None
    audit_events: list = field(default_factory=list)
    audit_complete: bool = False
    error_detail: str | None = None
    rag_citations: list | None = None
    api_request_count: int = 0


class BaseAdapter(abc.ABC):
    @property
    @abc.abstractmethod
    def group_name(self) -> str: ...

    @abc.abstractmethod
    def execute(self, inp: AdapterInput) -> AdapterOutput: ...

    def verify_fixture(self, inp: AdapterInput) -> bool:
        if not os.path.exists(inp.fixture_path):
            return False
        with open(inp.fixture_path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest() == inp.fixture_sha256

    def check_credentials(self) -> tuple[bool, str]:
        return True, ""

    @staticmethod
    def prerequisite_missing(reason: str) -> AdapterOutput:
        return AdapterOutput(status="prerequisite_missing", error_detail=reason)


SAFE_ERRORS = frozenset({
    "parse_failed", "timeout", "schema_failed",
    "budget_exceeded", "prerequisite_missing",
})


def safe_error(http_code: int | None = None) -> str:
    if http_code:
        return f"http_status_{http_code}"
    return "api_error"


def derive_audit_complete(audit_events: list, group: str, has_findings: bool) -> bool:
    """Derive audit_complete from actual audit_events — NOT adapter self-report.

    Group A: requires review phase.
    Group B clean: requires review + decision.
    Group B non-clean: requires review + fix + decision.
    """
    phases = {e.get("phase") for e in audit_events if isinstance(e, dict)}
    if group == "A_single_agent":
        return "review" in phases
    elif group == "B_mergepilot":
        if not has_findings:
            return "review" in phases and "decision" in phases
        else:
            return "review" in phases and "fix" in phases and "decision" in phases
    return False
