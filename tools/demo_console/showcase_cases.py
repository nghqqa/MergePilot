#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic showcase seed — three fixed merge-governance cases (PR-V2).

Builds the deterministic SQL INSERTs that seed three showcase runs into the
isolated audit database so the ISOLATED_LIVE console can tell each case
from head to tail through the REAL read-only pipeline (postgres source →
LivePoller validation → /api/live/snapshot → 8 pages).

Cases:
  A  Protected Merge Success    — gateway ALLOW + L2 ticket + verify PASS +
                                  merge; final_status=MERGED
  B  Fail-Closed Rejection      — gateway DENY on a protected path; the run
                                  FAILS before any merge stage; audit kept
  C  Revision Drift Recovery    — merge approved, post-merge head drift
                                  detected, revision-cut rollback, final
                                  state RECOVERED/ROLLED_BACK

Truth boundaries (unchanged by this module):
  - Deterministic showcase seed. Not external customer data. Not
    production evidence.
  - Every value below is synthetic and fixed. No clock, no RNG, no
    network result contributes to the generated SQL.
  - This module NEVER writes files (no evidence/, no verification/, no
    screenshots). It only returns SQL text; the caller pipes it to psql
    over stdin.
  - database_verified / application_integration_verified /
    production_verified stay false. Seeding rows by direct admin INSERT
    does not verify any producer contract; the mcp_calls / rollback_runs
    rows are showcase fixtures, NOT real gateway/controller outputs.

Determinism contract:
  - build_showcase_seed_sql() is a pure function of SHOWCASE_CASES; two
    calls return byte-identical SQL.
  - All surfaced timestamps are fixed ISO-8601 values, strictly ordered
    within each case's story.
  - bundle_sha256 over the assembled snapshot excludes generated_at (the
    only server-regenerated field), so the canonical case content is
    byte-stable across reloads.

Fail-closed: validate_showcase_cases() runs before any SQL is built. A
missing field, a malformed SHA/run_id, a cross-case identifier collision,
a contradictory case shape (e.g. Case B carrying a merge ALLOW), or an
unordered timeline raises ShowcaseSeedError BEFORE a single INSERT is
produced. The bundle-level validator is NOT duplicated here — the live
LivePoller still validates every served bundle via schema.validate_bundle
+ integrity.verify_bundle_integrity.
"""

from __future__ import annotations

import hashlib
import re

# run_id shape mirrors postgres_source._RUN_ID_PATTERN (same behaviour,
# single mirrored literal — the demo-console entrypoint and the snapshot
# source both enforce ^[a-zA-Z0-9_-]+$ on the value that reaches them).
_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# Git SHAs in the audit DB are 40-char lowercase hex (git SHA-1 objects;
# cf. rollback_runs chk_rollback_rvsha). 64-hex values are reserved for
# sha256 digests (bundle_sha256), never for git SHAs.
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# The only tables the showcase seed may touch. Every one of them is a
# table the read-only console already queries (SCHEMA_CONTRACT in
# postgres_source.py) or the single-row environment marker. INSERT-only:
# the seed never issues DDL, never UPDATEs/DELETEs, and never touches a
# table outside this allowlist.
_SEEDED_TABLES = frozenset({
    "task_runs", "stage_runs", "stage_events", "revision_bindings",
    "run_pr_bindings", "mcp_calls", "rollback_runs", "audit_events",
})

DISCLOSURE = (
    "Deterministic showcase seed — not external customer data, not "
    "production evidence"
)

_SYNTHETIC_REPO = "mergepilot/showcase-demo"


class ShowcaseSeedError(Exception):
    """Stable, fail-closed showcase seed validation error."""


def _sha40(tag: str) -> str:
    """Deterministic synthetic 40-hex git SHA from a short tag."""
    h = re.sub(r"[^a-z0-9]", "", tag.lower().encode("utf-8").hex())
    h = (h + "0" * 40)[:40]
    return h


def _canon_str(value):
    """Python mirror of PostgreSQL ``public._canon_str`` (m4f1_state.sql):
    NULL → ``-1:``; else ``<utf8_byte_length>:<value>``."""
    if value is None:
        return "-1:"
    encoded = value.encode("utf-8")
    return "%d:%s" % (len(encoded), value)


def _revision_digest(source_call_id, correlation_id, tool, target_repo,
                     run_id, git_sha, result_status) -> str:
    """Mirror of the ``bind_revision`` source_evidence_digest algorithm
    (sha256 over the length-prefixed canonical concatenation), so the
    showcase revision_bindings rows satisfy the m4f1 NOT-NULL contract the
    same way the ephemeral harness seed does."""
    canon = (_canon_str(source_call_id) + _canon_str(correlation_id) +
             _canon_str(tool) + _canon_str(target_repo) +
             _canon_str(run_id) + _canon_str(git_sha) +
             _canon_str(result_status))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# ── The three fixed cases ───────────────────────────────────────────────────
# Field notes (audit-DB legality):
#   task_runs.status ∈ chk_task_status (m3b_b4 final list)
#   mcp_calls.phase ∈ {INTENT,RESULT,ERROR}; decision ∈ {ALLOW,DENY,ERROR}
#   rollback_runs.status ∈ chk_rollback_status ('RECOVERED' is legal);
#   reverify_verdict ∈ {PASS,FAIL}
SHOWCASE_CASES = {
    "run-showcase-a": {
        "case_id": "case-showcase-protected-merge-success",
        "case_name": "Protected Merge Success",
        "story": (
            "Protected-branch change -> Policy Gateway ALLOW on L2 ticket "
            "-> verify PASS -> merge executed"
        ),
        "pr_number": 101,
        "fix_branch": "fix/showcase-a",
        "base_branch": "main",
        "base_sha": _sha40("showcase-a-base"),
        "head_sha": _sha40("showcase-a-head"),
        "merge_sha": _sha40("showcase-a-merge"),
        "final_status": "MERGED",
        "last_error": None,
        "stages": [
            # (stage, agent, status, verdict, started_at, completed_at)
            ("review", "reviewer", "COMPLETED", None,
             "2026-08-17T10:04:01+00:00", "2026-08-17T10:04:11+00:00"),
            ("fix", "fixer", "COMPLETED", None,
             "2026-08-17T10:04:12+00:00", "2026-08-17T10:04:22+00:00"),
            ("verify", "verifier", "COMPLETED", "PASS",
             "2026-08-17T10:04:23+00:00", "2026-08-17T10:04:33+00:00"),
            ("merge", "manager", "COMPLETED", "MERGED",
             "2026-08-17T10:04:34+00:00", "2026-08-17T10:04:40+00:00"),
        ],
        "mcp_calls": [
            # (request_id, phase, tool, decision, reason_code, ticket_id,
            #  result_status, git_sha, error)
            ("mcp-showcase-a-001", "INTENT", "merge_pull_request", "ALLOW",
             "POLICY_PASS_L2_APPROVED", "tkt-showcase-a-l2", None, None,
             None),
            ("mcp-showcase-a-002", "RESULT", "merge_pull_request", "ALLOW",
             "L2_TICKET_APPROVED", "tkt-showcase-a-l2", "OK", "MERGE_SHA",
             None),
        ],
        "audit_events": [
            # (agent, action, detail, sha_key)
            ("reviewer", "review", "showcase review completed", "head_sha"),
            ("fixer", "fix", "showcase fix applied", "head_sha"),
            ("verifier", "verify", "showcase verify passed", "head_sha"),
            ("manager", "merge", "showcase merge on L2 ticket", "merge_sha"),
            ("system", "close_pr", "showcase PR closed", "merge_sha"),
        ],
        "rollback": None,
    },
    "run-showcase-b": {
        "case_id": "case-showcase-failclosed-policy-rejection",
        "case_name": "Fail-Closed Policy Rejection",
        "story": (
            "Write into a protected path prefix -> Policy Gateway DENY "
            "(fail-closed) -> run FAILS before merge; audit evidence kept"
        ),
        "pr_number": 102,
        "fix_branch": "fix/showcase-b",
        "base_branch": "main",
        "base_sha": _sha40("showcase-b-base"),
        "head_sha": _sha40("showcase-b-head"),
        "merge_sha": None,           # NO merge SHA may exist for case B
        "final_status": "FAIL",
        "last_error": (
            "POLICY_DENY: write to protected path prefix (samples/); "
            "run blocked before merge (fail-closed)"
        ),
        "stages": [
            ("review", "reviewer", "COMPLETED", None,
             "2026-08-17T10:05:01+00:00", "2026-08-17T10:05:11+00:00"),
            ("fix", "fixer", "FAILED", "DENIED",
             "2026-08-17T10:05:12+00:00", "2026-08-17T10:05:15+00:00"),
            # Deliberately NO verify/merge stage: the timeline terminates
            # at the rejection point and never continues into success.
        ],
        "mcp_calls": [
            ("mcp-showcase-b-001", "INTENT", "create_or_update_file", "DENY",
             "PROTECTED_PATH_PREFIX", None, None, None,
             "fail-closed: target path under protected prefix samples/"),
        ],
        "audit_events": [
            ("reviewer", "review", "showcase review completed", "head_sha"),
            ("system", "policy_deny",
             "DENY PROTECTED_PATH_PREFIX on create_or_update_file "
             "(protected prefix samples/)", None),
        ],
        "rollback": None,
    },
    "run-showcase-c": {
        "case_id": "case-showcase-revision-drift-recovery",
        "case_name": "Revision Drift Recovery",
        "story": (
            "Merge approved -> post-merge head drift detected (approved "
            "SHA != observed SHA) -> revision-cut rollback -> recovered "
            "consistent state"
        ),
        "pr_number": 103,
        "fix_branch": "fix/showcase-c",
        "base_branch": "main",
        "base_sha": _sha40("showcase-c-base"),
        "head_sha": _sha40("showcase-c-head"),   # the APPROVED head SHA
        "merge_sha": _sha40("showcase-c-merge"),  # approved merge commit
        "drifted_sha": _sha40("showcase-c-drift"),  # observed after drift
        "recovered_sha": _sha40("showcase-c-recovered"),
        "final_status": "ROLLED_BACK",
        "last_error": (
            "REVISION_DRIFT: observed head SHA differs from approved head "
            "SHA after merge; rollback executed"
        ),
        "stages": [
            ("review", "reviewer", "COMPLETED", None,
             "2026-08-17T10:06:01+00:00", "2026-08-17T10:06:11+00:00"),
            ("fix", "fixer", "COMPLETED", None,
             "2026-08-17T10:06:12+00:00", "2026-08-17T10:06:22+00:00"),
            ("verify", "verifier", "COMPLETED", "PASS",
             "2026-08-17T10:06:23+00:00", "2026-08-17T10:06:33+00:00"),
            ("merge", "manager", "COMPLETED", "MERGED",
             "2026-08-17T10:06:34+00:00", "2026-08-17T10:06:40+00:00"),
            ("drift-check", "verifier", "FAILED", "REVISION_DRIFT",
             "2026-08-17T10:06:41+00:00", "2026-08-17T10:06:44+00:00"),
            ("rollback", "manager", "COMPLETED", "RECOVERED",
             "2026-08-17T10:06:45+00:00", "2026-08-17T10:06:55+00:00"),
        ],
        "mcp_calls": [
            ("mcp-showcase-c-001", "INTENT", "merge_pull_request", "ALLOW",
             "POLICY_PASS_L2_APPROVED", "tkt-showcase-c-l2", None, None,
             None),
            ("mcp-showcase-c-002", "RESULT", "merge_pull_request", "ALLOW",
             "L2_TICKET_APPROVED", "tkt-showcase-c-l2", "OK", "MERGE_SHA",
             None),
            ("mcp-showcase-c-003", "RESULT", "get_pull_request", "DENY",
             "REVISION_DRIFT", None, "ERROR", "DRIFTED_SHA",
             "revision drift: observed head SHA != approved head SHA"),
        ],
        "audit_events": [
            ("reviewer", "review", "showcase review completed", "head_sha"),
            ("verifier", "verify", "showcase verify passed", "head_sha"),
            ("manager", "merge", "showcase merge on L2 ticket", "merge_sha"),
            ("system", "drift_detected",
             "observed head SHA differs from approved head SHA", None),
            ("system", "rollback",
             "revision-cut rollback executed; state recovered", None),
        ],
        "rollback": {
            "rollback_id": "rb-showcase-c-1",
            "status": "RECOVERED",
            "fail_reason": (
                "post-merge revision drift: observed head SHA differs "
                "from approved head SHA"
            ),
            "reverify_verdict": "PASS",
        },
    },
}

_RUN_ORDER = ("run-showcase-a", "run-showcase-b", "run-showcase-c")

# sha_key -> resolved SHA inside a case (used by mcp_calls / audit_events).
_SHA_KEYS = ("base_sha", "head_sha", "merge_sha", "drifted_sha",
             "recovered_sha")

_FINAL_STATUS_ALLOWED = frozenset({
    "MERGED", "FAIL", "ROLLED_BACK",
})


def case_core(run_id: str) -> dict:
    """Return a deep, JSON-serializable copy of one case's canonical core.

    The returned dict IS the canonical business content of the case: the
    same input always yields byte-identical ``json.dumps(core,
    sort_keys=True)``. It is used by the test suite to prove seed-level
    determinism (requirement: repeated generation is byte-stable).
    """
    if run_id not in SHOWCASE_CASES:
        raise ShowcaseSeedError("unknown showcase run_id %r" % run_id)
    case = SHOWCASE_CASES[run_id]
    core = {
        "run_id": run_id,
        "case_id": case["case_id"],
        "case_name": case["case_name"],
        "repo": _SYNTHETIC_REPO,
        "pr_number": case["pr_number"],
        "fix_branch": case["fix_branch"],
        "base_branch": case["base_branch"],
        "shas": {k: case.get(k) for k in _SHA_KEYS if case.get(k)},
        "final_status": case["final_status"],
        "last_error": case["last_error"],
        "stages": [
            {"stage": s[0], "agent": s[1], "status": s[2], "verdict": s[3],
             "started_at": s[4], "completed_at": s[5]}
            for s in case["stages"]
        ],
        "mcp_calls": [
            {"request_id": c[0], "phase": c[1], "tool": c[2],
             "decision": c[3], "reason_code": c[4], "ticket_id": c[5],
             "result_status": c[6],
             "git_sha": _resolve_sha(case, c[7]) if c[7] else None,
             "error": c[8]}
            for c in case["mcp_calls"]
        ],
        "audit_events": [
            {"agent": a[0], "action": a[1], "detail": a[2],
             "sha": _resolve_sha(case, a[3]) if a[3] else None}
            for a in case["audit_events"]
        ],
    }
    if case["rollback"]:
        core["rollback"] = {
            "rollback_id": case["rollback"]["rollback_id"],
            "reverted_merge_sha": case["merge_sha"],
            "revert_result_sha": case["recovered_sha"],
            "status": case["rollback"]["status"],
            "fail_reason": case["rollback"]["fail_reason"],
            "reverify_verdict": case["rollback"]["reverify_verdict"],
        }
    else:
        core["rollback"] = None
    return core


def _resolve_sha(case: dict, key_or_value: str | None):
    """Resolve a mcp_calls/audit_events sha_key ('MERGE_SHA', 'head_sha',
    ...) into the case's SHA value, or pass a literal through."""
    if not key_or_value:
        return None
    if key_or_value in case:
        return case[key_or_value]
    if key_or_value == "MERGE_SHA":
        return case.get("merge_sha")
    if key_or_value == "DRIFTED_SHA":
        return case.get("drifted_sha")
    raise ShowcaseSeedError("unknown sha key %r" % key_or_value)


# ── Fail-closed validation ─────────────────────────────────────────────────

def validate_showcase_cases(cases: dict | None = None) -> None:
    """Validate the showcase case registry. Raises ShowcaseSeedError.

    Checks (all fail-closed, before any SQL is generated):
      1. exactly 3 cases with the canonical run_ids;
      2. case_id / run_id unique, well-formed;
      3. PR numbers distinct; all git SHAs 40-hex and cross-case distinct;
      4. per-case: final status in the allowed showcase set; timeline
         strictly ordered (started_at < completed_at, monotonic across
         stages); necessary stages present per case semantics;
      5. Case A: an ALLOW merge call with an L2 ticket, a verify PASS
         stage, a merge stage and merge_sha; final MERGED;
      6. Case B: a DENY call with an error/reason, NO merge ALLOW call,
         NO merge/verify-success stage, NO merge_sha; final FAIL;
      7. Case C: approved head SHA + drifted SHA + recovered SHA, a
         DENY/REVISION_DRIFT call, a rollback block with RECOVERED status,
         reverted == merge_sha and revert_result == recovered_sha;
         final ROLLED_BACK;
      8. every seeded table is inside the INSERT allowlist (checked at
         SQL-build time too).
    """
    reg = cases if cases is not None else SHOWCASE_CASES
    if set(reg.keys()) != set(_RUN_ORDER) or len(reg) != 3:
        raise ShowcaseSeedError(
            "exactly the three canonical showcase runs are required "
            "(got %s)" % sorted(reg.keys()))

    seen_case_ids, seen_prs, seen_shas = set(), set(), set()
    for run_id in _RUN_ORDER:
        case = reg[run_id]
        if not _RUN_ID_RE.fullmatch(run_id):
            raise ShowcaseSeedError("run_id %r fails ^[a-zA-Z0-9_-]+$" % run_id)
        case_id = case.get("case_id")
        if not case_id or not re.fullmatch(r"[a-z0-9-]+", case_id):
            raise ShowcaseSeedError("bad case_id for %s" % run_id)
        if case_id in seen_case_ids:
            raise ShowcaseSeedError("duplicate case_id %r" % case_id)
        seen_case_ids.add(case_id)

        pr = case.get("pr_number")
        if not isinstance(pr, int) or isinstance(pr, bool) or pr <= 0:
            raise ShowcaseSeedError("bad pr_number for %s" % run_id)
        if pr in seen_prs:
            raise ShowcaseSeedError("PR collision across cases: %s" % pr)
        seen_prs.add(pr)

        for key in ("base_sha", "head_sha"):
            sha = case.get(key)
            if not _GIT_SHA_RE.fullmatch(sha or ""):
                raise ShowcaseSeedError(
                    "%s.%s must be 40-hex git SHA" % (run_id, key))
            if sha in seen_shas:
                raise ShowcaseSeedError("SHA collision across cases")
            seen_shas.add(sha)
        for key in ("merge_sha", "drifted_sha", "recovered_sha"):
            sha = case.get(key)
            if sha is None:
                continue
            if not _GIT_SHA_RE.fullmatch(sha):
                raise ShowcaseSeedError(
                    "%s.%s must be 40-hex git SHA" % (run_id, key))
            if sha in seen_shas:
                raise ShowcaseSeedError("SHA collision across cases")
            seen_shas.add(sha)

        if case["final_status"] not in _FINAL_STATUS_ALLOWED:
            raise ShowcaseSeedError(
                "%s.final_status %r outside showcase set" %
                (run_id, case["final_status"]))

        # Timeline: strictly ordered per stage, monotonic across stages.
        prev_end = None
        stage_names = []
        for stage in case["stages"]:
            name, _agent, status, _verdict, started, completed = stage
            if started >= completed:
                raise ShowcaseSeedError(
                    "%s stage %s: started_at must precede completed_at"
                    % (run_id, name))
            if prev_end is not None and started < prev_end:
                raise ShowcaseSeedError(
                    "%s stage %s breaks timeline ordering" % (run_id, name))
            prev_end = completed
            if name in stage_names:
                raise ShowcaseSeedError(
                    "%s duplicate stage %s" % (run_id, name))
            stage_names.append(name)
            if status not in ("COMPLETED", "FAILED"):
                raise ShowcaseSeedError(
                    "%s stage %s: invalid status %r" % (run_id, name, status))

        decisions = [(c[2], c[3], c[4], c[5], c[8]) for c in case["mcp_calls"]]
        for req, phase, _tool, dec, *_rest in case["mcp_calls"]:
            if phase not in ("INTENT", "RESULT", "ERROR"):
                raise ShowcaseSeedError(
                    "%s mcp %s: illegal phase %r" % (run_id, req, phase))
            if dec not in ("ALLOW", "DENY", "ERROR"):
                raise ShowcaseSeedError(
                    "%s mcp %s: illegal decision %r" % (run_id, req, dec))

        if run_id == "run-showcase-a":
            _validate_case_a(case, stage_names, decisions)
        elif run_id == "run-showcase-b":
            _validate_case_b(case, stage_names, decisions)
        else:
            _validate_case_c(case, stage_names, decisions)


def _validate_case_a(case, stage_names, decisions) -> None:
    if case["final_status"] != "MERGED" or not case.get("merge_sha"):
        raise ShowcaseSeedError("case A must end MERGED with a merge SHA")
    if "verify" not in stage_names or "merge" not in stage_names:
        raise ShowcaseSeedError("case A needs verify + merge stages")
    verify = [s for s in case["stages"] if s[0] == "verify"][0]
    merge = [s for s in case["stages"] if s[0] == "merge"][0]
    if verify[3] != "PASS" or verify[2] != "COMPLETED":
        raise ShowcaseSeedError("case A verify stage must be COMPLETED/PASS")
    if merge[2] != "COMPLETED":
        raise ShowcaseSeedError("case A merge stage must be COMPLETED")
    allow_merge_l2 = [
        d for d in decisions
        if d[0] == "merge_pull_request" and d[1] == "ALLOW" and d[3]
    ]
    if not allow_merge_l2:
        raise ShowcaseSeedError(
            "case A needs an ALLOW merge call carrying an L2 ticket")
    if any(d[1] == "DENY" for d in decisions):
        raise ShowcaseSeedError("case A must not contain a DENY call")


def _validate_case_b(case, stage_names, decisions) -> None:
    if case["final_status"] != "FAIL" or not case.get("last_error"):
        raise ShowcaseSeedError("case B must end FAIL with a failure reason")
    if case.get("merge_sha"):
        raise ShowcaseSeedError("case B must not carry a merge SHA")
    deny = [d for d in decisions if d[1] == "DENY"]
    if not deny or not deny[0][4]:
        raise ShowcaseSeedError("case B needs a DENY call with an error")
    if "merge" in stage_names:
        raise ShowcaseSeedError("case B timeline must terminate at the "
                                "rejection (no merge stage)")
    if any(d[0] == "merge_pull_request" and d[1] == "ALLOW" for d in decisions):
        raise ShowcaseSeedError("case B must not fake a merge ALLOW")
    verify = [s for s in case["stages"] if s[0] == "verify"]
    if verify and verify[0][3] == "PASS":
        raise ShowcaseSeedError("case B must not fake verifier success")
    failing = [s for s in case["stages"] if s[2] == "FAILED"]
    if not failing:
        raise ShowcaseSeedError("case B needs a visibly failed stage")


def _validate_case_c(case, stage_names, decisions) -> None:
    if case["final_status"] != "ROLLED_BACK":
        raise ShowcaseSeedError("case C must end ROLLED_BACK")
    for key in ("merge_sha", "drifted_sha", "recovered_sha"):
        if not case.get(key):
            raise ShowcaseSeedError("case C needs %s" % key)
    if case["head_sha"] == case["drifted_sha"]:
        raise ShowcaseSeedError(
            "case C approved and drifted SHAs must differ")
    drift = [d for d in decisions
             if d[1] == "DENY" and d[2] == "REVISION_DRIFT"]
    if not drift:
        raise ShowcaseSeedError("case C needs a DENY/REVISION_DRIFT call")
    rb = case.get("rollback")
    if not rb or rb["status"] != "RECOVERED":
        raise ShowcaseSeedError("case C needs a RECOVERED rollback record")
    if "rollback" not in stage_names:
        raise ShowcaseSeedError("case C timeline needs a rollback stage")
    if rb["reverify_verdict"] != "PASS":
        raise ShowcaseSeedError("case C re-verify after rollback must PASS")


# ── SQL generation (INSERT-only, deterministic) ────────────────────────────

def _sql_lit(value) -> str:
    """Render a Python value as a SQL literal (NULL / int / quoted str)."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    return "'" + text.replace("'", "''") + "'"


def build_showcase_seed_sql() -> str:
    """Return the deterministic INSERT-only SQL for the three showcase runs.

    Pure function of SHOWCASE_CASES: byte-identical across calls. The SQL
    touches only tables in _SEEDED_TABLES (asserted), uses explicit fixed
    timestamps for every column the console surfaces, and carries the
    showcase disclosure as a leading comment.
    """
    validate_showcase_cases()
    parts = [
        "-- %s" % DISCLOSURE,
        "-- Synthetic showcase fixtures (PR-V2). Direct-admin INSERT seed;",
        "-- producer contracts remain NOT_VERIFIED. Idempotent per run_id.",
    ]
    for run_id in _RUN_ORDER:
        case = SHOWCASE_CASES[run_id]
        core = case_core(run_id)
        room_id = "room-%s" % run_id
        first_start = case["stages"][0][4]
        last_end = case["stages"][-1][5]

        parts.append("\n-- ── %s (%s) ──" % (case["case_name"], run_id))
        parts.append(
            "INSERT INTO task_runs (run_id, room_id, repo, pr_number, "
            "branch, status, current_stage, attempt, verdict, last_error, "
            "created_at, updated_at, skill_data_state)\n"
            "VALUES (%s, %s, %s, %d, %s, %s, %s, 1, %s, %s, %s, %s, "
            "'ACTIVE')\n"
            "ON CONFLICT (run_id) DO NOTHING;" % (
                _sql_lit(run_id), _sql_lit(room_id), _sql_lit(_SYNTHETIC_REPO),
                case["pr_number"], _sql_lit(case["fix_branch"]),
                _sql_lit(case["final_status"]),
                _sql_lit(case["stages"][-1][0]),
                _sql_lit("PASS" if case["final_status"] == "MERGED"
                         else ("FAIL" if case["final_status"] == "FAIL"
                               else "ROLLED_BACK")),
                _sql_lit(case["last_error"]),
                _sql_lit(first_start), _sql_lit(last_end),
            ))

        binding = "prb-%s" % run_id
        parts.append(
            "INSERT INTO run_pr_bindings (binding_id, run_id, repo, "
            "pr_number, fix_branch, base_branch, head_sha, recorded_at)\n"
            "VALUES (%s, %s, %s, %d, %s, %s, %s, %s)\n"
            "ON CONFLICT DO NOTHING;" % (
                _sql_lit(binding), _sql_lit(run_id),
                _sql_lit(_SYNTHETIC_REPO), case["pr_number"],
                _sql_lit(case["fix_branch"]), _sql_lit(case["base_branch"]),
                _sql_lit(case["head_sha"]), _sql_lit(first_start)))

        # mcp_calls: gateway audit rows. ts fixed at the case's first stage
        # start + index minutes to stay ordered and deterministic. These are
        # inserted BEFORE revision_bindings (its source_call_id has an FK
        # into mcp_calls.request_id).
        for idx, call in enumerate(case["mcp_calls"]):
            (req, phase, tool, decision, reason, ticket, result_status,
             sha_key, error) = call
            git_sha = _resolve_sha(case, sha_key) if sha_key else None
            ts = _minute_offset(first_start, idx)
            parts.append(
                "INSERT INTO mcp_calls (request_id, correlation_id, phase, "
                "ts, caller_agent, tool, decision, reason_code, ticket_id, "
                "target_repo, target_branch, result_status, git_sha, "
                "error, run_id)\n"
                "VALUES (%s, %s, %s, %s, 'coordinator', %s, %s, %s, %s, "
                "%s, %s, %s, %s, %s, %s)\n"
                "ON CONFLICT (request_id) DO NOTHING;" % (
                    _sql_lit(req), _sql_lit("corr-%s" % req),
                    _sql_lit(phase), _sql_lit(ts), _sql_lit(tool),
                    _sql_lit(decision), _sql_lit(reason),
                    _sql_lit(ticket), _sql_lit(_SYNTHETIC_REPO),
                    _sql_lit(case["base_branch"]),
                    _sql_lit(result_status), _sql_lit(git_sha),
                    _sql_lit(error), _sql_lit(run_id)))

        rev_binding = "rev-%s-0000000000000000000000000000" % run_id
        # source_call_id/evidence digest: back the binding with the case's
        # first gateway audit row (m4f1 NOT-NULL contract; digest mirrors
        # the bind_revision canonical algorithm).
        first_call = case["mcp_calls"][0]
        first_correlation = "corr-%s" % first_call[0]
        rev_digest = _revision_digest(
            first_call[0], first_correlation, first_call[2],
            _SYNTHETIC_REPO, run_id,
            _resolve_sha(case, first_call[7]) if first_call[7] else None,
            first_call[6])
        parts.append(
            "INSERT INTO revision_bindings (binding_id, run_id, repo, "
            "pr_number, base_sha, head_sha, source_call_id, "
            "source_evidence_digest, recorded_at)\n"
            "VALUES (%s, %s, %s, %d, %s, %s, %s, %s, %s)\n"
            "ON CONFLICT DO NOTHING;" % (
                _sql_lit(rev_binding), _sql_lit(run_id),
                _sql_lit(_SYNTHETIC_REPO), case["pr_number"],
                _sql_lit(case["base_sha"]), _sql_lit(case["head_sha"]),
                _sql_lit(first_call[0]), _sql_lit(rev_digest),
                _sql_lit(first_start)))

        # stage_runs: fixed timing, unique (run_id, stage, attempt).
        values = []
        for stage in case["stages"]:
            name, agent, status, verdict, started, completed = stage
            values.append(
                "(%s, %s, %s, 1, %s, %s, %s, %s, %s)" % (
                    _sql_lit(run_id), _sql_lit(name), _sql_lit(agent),
                    _sql_lit(status), _sql_lit(started),
                    _sql_lit(completed), _sql_lit(verdict),
                    _sql_lit("showcase stage %s" % name)))
        parts.append(
            "INSERT INTO stage_runs (run_id, stage, agent, attempt, "
            "status, started_at, completed_at, verdict, detail)\n"
            "VALUES\n    %s\nON CONFLICT DO NOTHING;" %
            (",\n    ".join(values)))

        # stage_events: dispatch provenance, fixed received_at ordering.
        evt_values = []
        for idx, stage in enumerate(case["stages"]):
            name = stage[0]
            evt_values.append(
                "(%s, %s, %s, %s, %s, %s, %s)" % (
                    _sql_lit("evt-%s-%02d" % (run_id, idx)),
                    _sql_lit(room_id), _sql_lit(run_id),
                    _sql_lit("SHOWCASE_%s_DISPATCH" % name.upper()
                             .replace("-", "_")),
                    _sql_lit(name), _sql_lit("PROCESSED"),
                    _sql_lit("controller")))
        parts.append(
            "INSERT INTO stage_events (event_id, room_id, run_id, "
            "event_type, stage, status, sender)\n"
            "VALUES\n    %s\nON CONFLICT DO NOTHING;" %
            (",\n    ".join(evt_values)))

        # audit_events: immutable closed-loop audit rows (counts surface on
        # the evidence page); ts fixed and ordered. The table has NO unique
        # constraint (only a BIGSERIAL PK), so plain INSERTs would duplicate
        # on seed replay. Replay idempotency is achieved INSERT-only via
        # INSERT ... SELECT ... WHERE NOT EXISTS matching the FULL stable
        # record (all 8 inserted columns compared with IS NOT DISTINCT FROM
        # so NULL sha rows match correctly — plain = NULL never matches).
        for idx, ev in enumerate(case["audit_events"]):
            agent, action, detail, sha_key = ev
            lits = [
                _sql_lit(run_id),
                _sql_lit(agent),
                _sql_lit(action),
                _sql_lit("%s#%d" % (_SYNTHETIC_REPO, case["pr_number"])),
                _sql_lit("%s (%s)" % (detail,
                                      DISCLOSURE.split(" — ")[0])),
                _sql_lit(_resolve_sha(case, sha_key) if sha_key else None),
                _sql_lit("pg"),
                _sql_lit(_minute_offset(first_start, idx)),
            ]
            cols = ("task_id", "agent", "action", "target", "detail",
                    "sha", "via", "ts")
            not_exists = " AND ".join(
                "ex.%s IS NOT DISTINCT FROM %s" % (c, lit)
                for c, lit in zip(cols, lits))
            parts.append(
                "INSERT INTO audit_events (%s)\n"
                "SELECT %s\n"
                "WHERE NOT EXISTS (\n"
                "    SELECT 1 FROM audit_events ex WHERE %s\n"
                ");" % (", ".join(cols), ", ".join(lits), not_exists))

        # rollback_runs (case C only).
        rb = case.get("rollback")
        if rb:
            parts.append(
                "INSERT INTO rollback_runs (rollback_id, parent_run_id, "
                "reverted_merge_sha, repo, pr_number, trigger_event_id, "
                "status, fail_reason, revert_result_sha, reverify_verdict, "
                "created_at, updated_at)\n"
                "VALUES (%s, %s, %s, %s, %d, %s, %s, %s, %s, %s, %s, %s)\n"
                "ON CONFLICT DO NOTHING;" % (
                    _sql_lit(rb["rollback_id"]), _sql_lit(run_id),
                    _sql_lit(case["merge_sha"]), _sql_lit(_SYNTHETIC_REPO),
                    case["pr_number"],
                    _sql_lit("evt-%s-%02d" % (run_id,
                                              len(case["stages"]) - 2)),
                    _sql_lit(rb["status"]), _sql_lit(rb["fail_reason"]),
                    _sql_lit(case["recovered_sha"]),
                    _sql_lit(rb["reverify_verdict"]),
                    _sql_lit(last_end), _sql_lit(last_end)))

    sql = "\n".join(parts) + "\n"
    _assert_sql_scope(sql)
    return sql


def _minute_offset(iso_start: str, idx: int) -> str:
    """Deterministic +N-minutes offset on a fixed ISO timestamp.

    Carries into the hour correctly (10:59 + 1 -> 11:00) and stays within
    the same fixed-day assumption the seed uses; a day rollover would be a
    seed-shape bug and fails closed.
    """
    m = re.fullmatch(
        r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\+00:00", iso_start)
    if not m:
        raise ShowcaseSeedError("bad fixed timestamp %r" % iso_start)
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hour, minute, second = int(m.group(4)), int(m.group(5)), int(m.group(6))
    total_minutes = hour * 60 + minute + idx
    if total_minutes >= 24 * 60:
        raise ShowcaseSeedError(
            "timestamp offset crosses midnight; fix the seed shape")
    return "%04d-%02d-%02dT%02d:%02d:%02d+00:00" % (
        year, month, day, total_minutes // 60, total_minutes % 60, second)


def _assert_sql_scope(sql: str) -> None:
    """Fail-closed: the generated SQL is INSERT-only and allowlist-scoped."""
    stripped = re.sub(r"--[^\n]*", "", sql)
    for kw in ("ALTER ", "CREATE ", "DROP ", "UPDATE ", "DELETE ",
               "TRUNCATE ", "GRANT ", "REVOKE "):
        if kw in stripped.upper():
            raise ShowcaseSeedError(
                "showcase seed SQL must be INSERT-only (found %r)" % kw)
    for target in re.findall(r"INSERT INTO ([a-z_]+)", stripped):
        if target not in _SEEDED_TABLES:
            raise ShowcaseSeedError(
                "showcase seed targets non-allowlisted table %r" % target)


def main() -> int:
    """Emit the deterministic showcase seed SQL to stdout.

    The SQL is piped to psql over stdin by the orchestrator (never argv,
    never a file):  python showcase_cases.py | docker exec -i <pg> psql ...
    """
    import sys
    sys.stdout.write(build_showcase_seed_sql())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
