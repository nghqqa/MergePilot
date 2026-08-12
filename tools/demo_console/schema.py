#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DemoBundle schema definition and validation.

Defines the mergepilot.demo-bundle.v1 schema as a set of required top-level
fields, type constraints, and authenticity rules. Used by both the builder
and the test suite.
"""
from __future__ import annotations

BUNDLE_SCHEMA_VERSION = "mergepilot.demo-bundle.v1"
DEMO_MODE = "REPLAY"

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

# Fields excluded from bundle_sha256 computation (volatile or self-referential)
VOLATILE_FIELDS = frozenset({"bundle_sha256", "generated_at"})

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


def validate_bundle(bundle: dict) -> list[str]:
    """Validate a DemoBundle. Returns list of error strings (empty = valid)."""
    errors = []

    # Top-level required fields
    missing = REQUIRED_FIELDS - set(bundle.keys())
    if missing:
        errors.append(f"missing top-level fields: {sorted(missing)}")

    # schema_version
    if bundle.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        errors.append(f"schema_version must be {BUNDLE_SCHEMA_VERSION}")

    # demo_mode
    if bundle.get("demo_mode") != DEMO_MODE:
        errors.append(f"demo_mode must be {DEMO_MODE}")

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

    return errors
