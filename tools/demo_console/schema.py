#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DemoBundle schema definition and validation.

Defines the mergepilot.demo-bundle.v1 schema as a set of required top-level
fields, type constraints, and authenticity rules. Used by both the builder
and the test suite.
"""
from __future__ import annotations

# Integrity helpers are the single authoritative source for canonical JSON and
# bundle_sha256. Imported here (and re-exported) so callers can keep doing
# ``from schema import VOLATILE_FIELDS`` without creating a circular import.
from integrity import VOLATILE_FIELDS, verify_bundle_integrity  # noqa: F401

BUNDLE_SCHEMA_VERSION = "mergepilot.demo-bundle.v1"
DEMO_MODE = "REPLAY"

# Valid demo_mode values accepted by the schema. ISOLATED_LIVE was added in
# Phase 1 for the read-only live snapshot viewer.
VALID_DEMO_MODES = frozenset({"REPLAY", "ISOLATED_LIVE"})

# Required top-level fields in a DemoBundle
REQUIRED_FIELDS = {
    "schema_version",
    "demo_mode",
    "bundle_sha256",
    "generated_at",
    "source_commit",
    "verification_commit",
    "repo",
    "pr",
    "run",
    "final_status",
    "workflow_stages",
    "agents",
    "findings",
    "fixes",
    "verifier_result",
    "rag_advisories",
    "spans",
    "rollback_events",
    "evidence_files",
    "secret_leaks",
    "residue",
    "benchmark_summary",
    "topology",
}

# Required keys within nested structures
REQUIRED_PR_KEYS = {"number", "title", "base_sha", "head_sha"}
REQUIRED_RUN_KEYS = {"run_id", "trace_id", "entrypoint"}
REQUIRED_STAGE_KEYS = {"stage", "agent_role", "status"}
REQUIRED_AGENT_KEYS = {"role", "skill", "status"}
REQUIRED_FINDING_KEYS = {"finding_id", "category", "severity", "file", "message"}
REQUIRED_RAG_KEYS = {"agent_role", "status", "hit_count", "adopted", "untrusted"}
REQUIRED_SPAN_KEYS = {"trace_id", "span_id", "name", "status", "start_time", "end_time"}
REQUIRED_EVIDENCE_FILE_KEYS = {"path", "sha256", "description"}
REQUIRED_BENCHMARK_KEYS = {
    "dataset_version", "unique_case_count", "quality_gate_pass",
    "confirmatory_all_ok", "runtime_consumes_rag_context",
    "workflow_utility_status", "benchmark_phase",
}


def validate_bundle(bundle: dict, expected_mode: str | None = None) -> list[str]:
    """Validate a DemoBundle. Returns list of error strings (empty = valid).

    If ``expected_mode`` is provided (e.g. "REPLAY" or "ISOLATED_LIVE"), the
    bundle's ``demo_mode`` must match it exactly. This prevents a REPLAY bundle
    from being served in an ISOLATED_LIVE context and vice versa.
    """
    errors = []

    # Top-level required fields
    missing = REQUIRED_FIELDS - set(bundle.keys())
    if missing:
        errors.append(f"missing top-level fields: {sorted(missing)}")

    # schema_version
    if bundle.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        errors.append(f"schema_version must be {BUNDLE_SCHEMA_VERSION}")

    # demo_mode: must be one of the valid modes
    demo_mode = bundle.get("demo_mode")
    if demo_mode not in VALID_DEMO_MODES:
        errors.append(
            f"demo_mode must be one of {sorted(VALID_DEMO_MODES)}, got {demo_mode!r}"
        )

    # If the caller expects a specific mode, enforce it. A REPLAY bundle must
    # not be presented as ISOLATED_LIVE, and an ISOLATED_LIVE bundle must not
    # be presented as REPLAY.
    if expected_mode is not None:
        if expected_mode not in VALID_DEMO_MODES:
            errors.append(
                f"expected_mode must be one of {sorted(VALID_DEMO_MODES)}, "
                f"got {expected_mode!r}"
            )
        elif demo_mode != expected_mode:
            errors.append(
                f"demo_mode mismatch: bundle reports {demo_mode!r} but "
                f"expected_mode is {expected_mode!r}"
            )

    # verification_commit: mode-aware. For REPLAY bundles a verification_commit
    # is still required (the classic contract). For ISOLATED_LIVE bundles,
    # verification_commit may be null PROVIDED the bundle carries a
    # verification_commit_status field that makes the absence explicit (e.g.
    # "NOT_AVAILABLE"). This reflects that the read-only ISOLATED_LIVE viewer
    # does not perform/record a verification build.
    verification_commit = bundle.get("verification_commit")
    if demo_mode == "ISOLATED_LIVE":
        if verification_commit is None:
            if "verification_commit_status" not in bundle:
                errors.append(
                    "verification_commit is null but "
                    "verification_commit_status field is missing (ISOLATED_LIVE "
                    "bundles with a null verification_commit must declare "
                    "verification_commit_status)"
                )
            elif not bundle.get("verification_commit_status"):
                errors.append(
                    "verification_commit is null but "
                    "verification_commit_status is empty (must be a non-empty "
                    "string such as 'NOT_AVAILABLE')"
                )
    # REPLAY (or unknown mode): verification_commit must be present and non-null
    # (the classic contract). Only enforce this when demo_mode is known.
    elif demo_mode == "REPLAY":
        if verification_commit is None:
            errors.append(
                "verification_commit must be a non-null value for REPLAY bundles"
            )

    # Nested structure validation
    pr = bundle.get("pr", {})
    if isinstance(pr, dict):
        pr_missing = REQUIRED_PR_KEYS - set(pr.keys())
        if pr_missing:
            errors.append(f"pr missing keys: {sorted(pr_missing)}")

    run = bundle.get("run", {})
    if isinstance(run, dict):
        run_missing = REQUIRED_RUN_KEYS - set(run.keys())
        if run_missing:
            errors.append(f"run missing keys: {sorted(run_missing)}")

    # workflow_stages
    stages = bundle.get("workflow_stages", [])
    if not isinstance(stages, list):
        errors.append("workflow_stages must be a list")
    else:
        for i, s in enumerate(stages):
            if not isinstance(s, dict):
                errors.append(f"workflow_stages[{i}] must be a dict")
            else:
                s_missing = REQUIRED_STAGE_KEYS - set(s.keys())
                if s_missing:
                    errors.append(f"workflow_stages[{i}] missing: {sorted(s_missing)}")

    # agents
    agents = bundle.get("agents", [])
    if not isinstance(agents, list):
        errors.append("agents must be a list")
    else:
        for i, a in enumerate(agents):
            if isinstance(a, dict):
                a_missing = REQUIRED_AGENT_KEYS - set(a.keys())
                if a_missing:
                    errors.append(f"agents[{i}] missing: {sorted(a_missing)}")

    # findings
    findings = bundle.get("findings", [])
    if not isinstance(findings, list):
        errors.append("findings must be a list")
    else:
        for i, f in enumerate(findings):
            if isinstance(f, dict):
                f_missing = REQUIRED_FINDING_KEYS - set(f.keys())
                if f_missing:
                    errors.append(f"findings[{i}] missing: {sorted(f_missing)}")

    # rag_advisories
    rags = bundle.get("rag_advisories", [])
    if not isinstance(rags, list):
        errors.append("rag_advisories must be a list")
    else:
        for i, r in enumerate(rags):
            if isinstance(r, dict):
                r_missing = REQUIRED_RAG_KEYS - set(r.keys())
                if r_missing:
                    errors.append(f"rag_advisories[{i}] missing: {sorted(r_missing)}")
                # Authenticity: adopted must be False, untrusted must be True
                if r.get("adopted") is not False:
                    errors.append(f"rag_advisories[{i}].adopted must be False")
                if r.get("untrusted") is not True:
                    errors.append(f"rag_advisories[{i}].untrusted must be True")

    # spans
    spans = bundle.get("spans", [])
    if not isinstance(spans, list):
        errors.append("spans must be a list")

    # evidence_files
    ev_files = bundle.get("evidence_files", [])
    if not isinstance(ev_files, list):
        errors.append("evidence_files must be a list")
    else:
        for i, ef in enumerate(ev_files):
            if isinstance(ef, dict):
                ef_missing = REQUIRED_EVIDENCE_FILE_KEYS - set(ef.keys())
                if ef_missing:
                    errors.append(f"evidence_files[{i}] missing: {sorted(ef_missing)}")

    # benchmark_summary
    bench = bundle.get("benchmark_summary", {})
    if isinstance(bench, dict):
        bench_missing = REQUIRED_BENCHMARK_KEYS - set(bench.keys())
        if bench_missing:
            errors.append(f"benchmark_summary missing: {sorted(bench_missing)}")
        if bench.get("runtime_consumes_rag_context") is not False:
            errors.append("benchmark_summary.runtime_consumes_rag_context must be False")
        if bench.get("workflow_utility_status") != "NOT_MEASURABLE_WITH_CURRENT_RUNTIME":
            errors.append("benchmark_summary.workflow_utility_status must be NOT_MEASURABLE_WITH_CURRENT_RUNTIME")

    # safety
    if bundle.get("secret_leaks") != 0:
        errors.append("secret_leaks must be 0")

    # Integrity check
    integrity_errors = verify_bundle_integrity(bundle)
    errors.extend(integrity_errors)

    return errors
