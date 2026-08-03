#!/usr/bin/env python3
"""Auto-generate the M4-F release verification record.

Reads the run_all.sh gate manifest (``<rc>\\t<name>`` lines) and the AgentTeams
E2E evidence JSON, recomputes the delivery digest, and writes a human- and
machine-readable ``verification.txt``. The file records every gate name with its
rc, the AgentTeams migration / six-Skill / leak / residue detail, the delivery
digest, HEAD, platform, and the final rc.

On any gate failure the file still records every gate that ran (best-effort
complete failure evidence) and the final rc is non-zero. No credential or
environment value is ever written.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import pathlib
import subprocess
import sys

from delivery_digest import compute_digest


def _read_gates(manifest: pathlib.Path) -> list[tuple[int, str]]:
    gates: list[tuple[int, str]] = []
    if not manifest.exists():
        return gates
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        if "\t" in line:
            rc_text, name = line.split("\t", 1)
        else:
            rc_text, name = line, ""
        try:
            rc = int(rc_text.strip())
        except ValueError:
            rc = -1
        gates.append((rc, name.strip()))
    return gates


def _head_sha(root: pathlib.Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main_with_args(argv) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("gate_manifest")
    parser.add_argument("evidence")
    parser.add_argument("output")
    parser.add_argument("repo_root")
    args = parser.parse_args(argv)

    root = pathlib.Path(args.repo_root).resolve()
    gates = _read_gates(pathlib.Path(args.gate_manifest))

    evidence: dict | None = None
    evid_path = pathlib.Path(args.evidence)
    if evid_path.exists():
        try:
            evidence = json.loads(evid_path.read_text(encoding="utf-8"))
        except Exception:
            evidence = None

    digest, file_count = compute_digest(root)
    stored_digest = None
    digest_check = "skipped (no evidence)"
    if evidence is not None and isinstance(evidence.get("delivery"), dict):
        stored_digest = evidence["delivery"].get("digest")
        if stored_digest is None:
            digest_check = "missing in evidence"
        elif stored_digest == digest:
            digest_check = "OK (recomputed == stored)"
        else:
            digest_check = "MISMATCH"

    # hiclaw_live is sourced from the Demo summary (the authoritative honesty
    # statement), not inferred from credential usage.
    summary_path = evid_path.parent / "agentteams-demo-summary.json"
    hiclaw_live = "unknown (summary not generated)"
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            topology = (summary.get("demo", {}) or {}).get("topology", {}) or {}
            hiclaw_live = topology.get("hiclaw_live", "unknown")
        except Exception:
            hiclaw_live = "unknown (summary unreadable)"

    # legacy functional regression status (M4-A~E authoritative platforms).
    # The legacy gate's rc is already in the gate manifest (the authoritative
    # source for final_rc); this read is defence-in-depth plus visibility, so it
    # only fails the run when the report is present and non-green (a missing
    # report is neutral — e.g. isolated counterexample temp dirs).
    legacy_path = evid_path.parent / "legacy-functional-regression.txt"
    legacy_present = legacy_path.exists()
    legacy_rc = "unknown (not generated)"
    legacy_matched = None
    if legacy_present:
        try:
            legacy_text = legacy_path.read_text(encoding="utf-8")
            import re as _re
            m = _re.search(r"^legacy_regression_rc:\s*(\d+)\s*$", legacy_text, _re.M)
            if m:
                legacy_rc = m.group(1)
            mm = _re.search(r"^suites_matched:\s*(\d+)", legacy_text, _re.M)
            mt = _re.search(r"^suites_total:\s*(\d+)", legacy_text, _re.M)
            if mm and mt:
                legacy_matched = "%s/%s" % (mm.group(1), mt.group(1))
        except Exception:
            legacy_rc = "unknown (unreadable)"

    passed = sum(1 for rc, _ in gates if rc == 0)
    failed = sum(1 for rc, _ in gates if rc != 0)

    final_rc = 0
    failing = [g for g in gates if g[0] != 0]
    if failing:
        final_rc = failing[0][0]
    if digest_check == "MISMATCH":
        final_rc = 1
    if evidence is None:
        # cannot prove a release without the AgentTeams evidence → fail-closed
        final_rc = 1
    if evidence is not None and evidence.get("all_passed") is False:
        final_rc = 1
    if legacy_present and legacy_rc != "0":
        # defence-in-depth: a present-but-non-green legacy report fails the run
        final_rc = 1

    lines: list[str] = []
    lines.append("MergePilot M4-F release verification")
    lines.append("generated_at: %s" % _utc_now())
    lines.append("head: %s" % _head_sha(root))
    lines.append("platform: %s" % platform.platform())
    lines.append("delivery_digest: %s" % digest)
    lines.append("delivery_files: %d" % file_count)
    lines.append(
        "delivery_scope: M4-F delivery surface "
        "(schema/runtime/controller/gateway/worker/tests-m4f1)"
    )
    lines.append("")
    lines.append("[m4f1-run_all gates]")
    for rc, name in gates:
        lines.append("%s\t%s" % (rc, name))
    if gates:
        lines.append("gates_total: %d" % len(gates))
        lines.append("gates_passed: %d" % passed)
        lines.append("gates_failed: %d" % failed)
    else:
        lines.append("gates_total: 0 (manifest empty)")
    lines.append("")
    lines.append("[agentteams-e2e]")
    if evidence is not None:
        runner = evidence.get("runner", {}) or {}
        residue = evidence.get("residue", {}) or {}
        lines.append("migration_round_1_rc: %s" % runner.get("migration_round_1_rc"))
        lines.append("migration_round_2_rc: %s" % runner.get("migration_round_2_rc"))
        lines.append("run_rc: %s" % runner.get("run_rc"))
        lines.append("all_passed: %s" % evidence.get("all_passed"))
        lines.append("secret_leaks: %s" % evidence.get("secret_leaks"))
        lines.append(
            "residue: containers=%s networks=%s temp_dirs=%s"
            % (residue.get("containers"), residue.get("networks"), residue.get("temp_dirs"))
        )
        jobs = evidence.get("jobs", []) or []
        verdicts = {j.get("skill"): j for j in jobs if isinstance(j, dict)}
        order = (
            "diff-parse", "risk-classify", "sast-scan",
            "test-runner", "case-retrieval", "pr-lifecycle",
        )
        lines.append("six_skills:")
        for skill in order:
            job = verdicts.get(skill, {})
            extra = ""
            if job.get("verdict"):
                extra = " (verdict=%s)" % job.get("verdict")
            elif isinstance(job.get("summary"), dict) and job["summary"].get("outcome"):
                extra = " (outcome=%s)" % job["summary"].get("outcome")
            lines.append("  %s: %s%s" % (skill, job.get("job_status", "MISSING"), extra))
        lines.append("delivery_digest_check: %s" % digest_check)
        lines.append("hiclaw_live: %s" % hiclaw_live)
        ext_creds = (evidence.get("fixture", {}) or {}).get("external_credentials")
        lines.append("external_credentials: %s" % ext_creds)
    else:
        lines.append("status: evidence not generated (AgentTeams gate did not run)")
        lines.append("delivery_digest_check: %s" % digest_check)
    lines.append("")
    lines.append("[legacy-functional-regression]")
    lines.append("legacy_regression_rc: %s" % legacy_rc)
    if legacy_matched is not None:
        lines.append("legacy_suites_matched: %s" % legacy_matched)
    lines.append("legacy_evidence: %s" % legacy_path)
    lines.append("")
    lines.append("final_rc: %d" % final_rc)
    if final_rc == 0:
        lines.append("ALL GATES PASSED")
    else:
        lines.append("GATES FAILED (see gates above)")

    out = pathlib.Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if final_rc == 0 else 1


def main() -> int:
    # Test-only fault injection: lets the release gate prove that a
    # verification-writer failure is fail-closed (the gate returns non-zero
    # even when every business gate passed). Never writes the output file.
    if os.environ.get("M4F_VFY_FORCE_FAIL", "") == "1":
        sys.stderr.write("M4F_VFY_FORCE_FAIL injected (test-only)\n")
        return 2
    return main_with_args(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
