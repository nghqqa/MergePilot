#!/usr/bin/env python3
"""OTel/SLS-compatible structured observation + competition Demo summary.

Reads the AgentTeams E2E evidence and emits a single artefact with two views:
  - ``otelsls``: a span-based trace (resource + scope + status + spans) that is
    structurally compatible with OpenTelemetry / SLS log consumption.
  - ``demo``: the human-facing competition Demo summary (run, topology, checks,
    six Skills, revision, gateway audit, residue, evidence binding).

Honesty contract: ``hiclaw_live`` is False and the note says so, because the
upstream in the disposable Docker run is the stateful protocol fixture, not a
live HiClaw Matrix event.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib


SKILL_ORDER = (
    "diff-parse",
    "risk-classify",
    "sast-scan",
    "test-runner",
    "case-retrieval",
    "pr-lifecycle",
)

SKILL_NOTES = {
    "diff-parse": "real unified-diff parse over Gateway-sourced diff",
    "risk-classify": "real rule-based classification (only-raise risk floor)",
    "sast-scan": "real inline SAST over changed files",
    "test-runner": "real pytest subprocess under trusted dev policy",
    "case-retrieval": "real pgvector adapter (deterministic embedding shape)",
    "pr-lifecycle": "real Policy Gateway over SSE as fixer role",
}


def _ok(value: bool | None) -> str:
    return "OK" if value else "ERROR"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence")
    parser.add_argument("summary")
    args = parser.parse_args()

    evid = json.loads(pathlib.Path(args.evidence).read_text(encoding="utf-8"))
    jobs = {job["skill"]: job for job in evid.get("jobs", [])}
    details = evid.get("details", {})
    stage_event = details.get("stage_event", {})
    gateway_audit = details.get("gateway_audit", {})
    revision = details.get("revision", {})
    checks = evid.get("checks", {})

    spans = [
        {
            "name": "matrix.ingress.m4f_run",
            "status": _ok(stage_event.get("status") == "PROCESSED"),
            "attributes": {
                "run_id": stage_event.get("run_id"),
                "stage_event.status": stage_event.get("status"),
                "entrypoint": "controller.process_event",
            },
        },
        {
            "name": "policy_gateway.revision_provenance",
            "status": _ok(gateway_audit.get("bound_revision_results", 0) >= 1),
            "attributes": {
                "audit.events": gateway_audit.get("events"),
                "audit.successful_results": gateway_audit.get("successful_results"),
                "audit.bound_revision_results": gateway_audit.get("bound_revision_results"),
                "github_upstream": "stateful protocol fixture (fake GitHub MCP SSE)",
                "control_credential_in_skill": False,
            },
        },
        {
            "name": "snapshot.revision_bound",
            "status": _ok(revision.get("manifest_items") == 6),
            "attributes": {
                "revision.base_sha": revision.get("base_sha"),
                "revision.head_sha": revision.get("head_sha"),
                "snapshot.manifest_items": revision.get("manifest_items"),
            },
        },
    ]
    for skill in SKILL_ORDER:
        job = jobs.get(skill, {})
        spans.append(
            {
                "name": f"skill.{skill}",
                "status": _ok(
                    job.get("job_status") == "SUCCEEDED"
                    and job.get("output_schema_validated")
                ),
                "attributes": {
                    "job_id": job.get("job_id"),
                    "job_status": job.get("job_status"),
                    "invocation_id": job.get("invocation_id"),
                    "output_schema_validated": job.get("output_schema_validated"),
                    "verdict": job.get("verdict"),
                    "risk_level": job.get("summary", {}).get("risk_level"),
                    "outcome": job.get("summary", {}).get("outcome"),
                    "note": SKILL_NOTES.get(skill, ""),
                },
            }
        )

    delivery = evid.get("delivery", {}) or {}
    spans.append(
        {
            "name": "release.delivery_integrity",
            "status": _ok(
                evid.get("secret_leaks") == 0 and bool(delivery.get("digest"))
            ),
            "attributes": {
                "secret_leaks": evid.get("secret_leaks"),
                "delivery_digest": delivery.get("digest"),
                "delivery_files": delivery.get("files"),
            },
        }
    )

    otel = {
        "schema": "mergepilot.otel.v1",
        "resource": {
            "service.name": "mergepilot-m4f-agentteams",
            "telemetry.sdk.language": "python",
        },
        "trace_id": stage_event.get("run_id", ""),
        "scope": "m4f.agentteams.gateway_e2e",
        "status": _ok(evid.get("all_passed")),
        "spans": spans,
    }

    demo = {
        "schema": "mergepilot.competition-demo-summary.v1",
        "produced_at": dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "run": {
            "run_id": stage_event.get("run_id"),
            "entrypoint": "controller.process_event M4F_RUN (real Matrix ingress)",
            "all_passed": evid.get("all_passed"),
        },
        "topology": {
            "policy_gateway": "real gateway.py over SSE",
            "github_upstream": "stateful fake GitHub MCP (protocol-real SSE)",
            "case_retrieval": "real pgvector adapter (deterministic embedding)",
            "pr_lifecycle": "real Policy Gateway as fixer",
            "hiclaw_live": False,
            "hiclaw_note": (
                "no live HiClaw Matrix event was exercised; the upstream is the "
                "stateful protocol fixture. Live HiClaw requires operator-provided "
                "Matrix credentials and is out of scope for this disposable gate."
            ),
        },
        "checks": checks,
        "skills": [
            {
                "skill": skill,
                "status": jobs.get(skill, {}).get("job_status"),
                "schema_validated": jobs.get(skill, {}).get("output_schema_validated"),
                "verdict": jobs.get(skill, {}).get("verdict"),
                "outcome": jobs.get(skill, {}).get("summary", {}).get("outcome"),
            }
            for skill in SKILL_ORDER
        ],
        "revision": revision,
        "gateway_audit": gateway_audit,
        "evidence": {
            "path": str(args.evidence),
            "source_sha256": evid.get("source_sha256", {}),
        },
        "residue": evid.get("residue", {}),
        "secret_leaks": evid.get("secret_leaks"),
        "delivery": evid.get("delivery"),
    }

    out = {"otelsls": otel, "demo": demo}
    pathlib.Path(args.summary).write_text(
        json.dumps(out, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
