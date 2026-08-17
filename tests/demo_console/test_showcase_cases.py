"""PR-V2 — deterministic showcase case tests (three merge-governance cases).

Covers the seed module (tools/demo_console/showcase_cases.py), the additive
postgres_source bundle fields, and the renderer's case-fact surface:

  1.  exactly three cases; canonical run ids;
  2.  case_id / run_id unique and well-formed;
  3.  PR / SHA formats legal and conflict-free across cases;
  4.  per-case necessary stages + final results;
  5.  Case A: ALLOW + L2 ticket + verifier PASS + merge result;
  6.  Case B: DENY + fail-closed + failure reason + no merge success;
  7.  Case C: approved SHA + drifted SHA + block + rollback + recovered SHA;
  8.  strictly ordered timelines;
  9.  byte-stable regeneration (seed SQL, case cores, assembled bundles);
  10. every dynamic renderer text path stays escaped;
  11. the 8-page set is unchanged (no 9th page);
  12. the API endpoint set is unchanged;
  13. fetch / setInterval counts are unchanged (one shared engine);
  14. the seed is INSERT-only and table-allowlisted (no backend refactor);
  15. no real credentials, customer names, or production claims;
  16. the seed module never writes files (no evidence/, no verification/);
  17. fail-closed validation on missing/contradictory case fields, with the
      bundle-level validator REUSED (schema.validate_bundle +
      integrity.verify_bundle_integrity), never duplicated.

No WSL/Docker/PostgreSQL here: DB-row shapes are built from the seed and
fed straight into PostgresSnapshotSource._assemble_bundle (a pure function
of its row arguments), which is exactly the code the real stack runs.
"""

from __future__ import annotations

import ast
import copy
import json
import os
import re
import sqlite3
import sys
import unittest
from pathlib import Path

_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = _HERE.parent.parent
for _p in (str(_HERE), str(ROOT), str(ROOT / "tools" / "demo_console")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import showcase_cases as sc  # noqa: E402
from showcase_cases import (  # noqa: E402
    DISCLOSURE,
    SHOWCASE_CASES,
    ShowcaseSeedError,
    build_showcase_seed_sql,
    case_core,
    validate_showcase_cases,
)
from postgres_source import PostgresSnapshotSource  # noqa: E402
from schema import validate_bundle  # noqa: E402
from integrity import verify_bundle_integrity  # noqa: E402
from bundle_builder import scan_secrets  # noqa: E402
from live_refresh import ALLOWED_URLS, PAGES  # noqa: E402

JS_PATH = ROOT / "tools" / "demo_console" / "live_assets" / "live-refresh.js"
HTML_PATH = ROOT / "tools" / "demo_console" / "live_assets" / "index.html"
SERVE_PATH = ROOT / "tools" / "demo_console" / "serve.py"
SEED_PATH = ROOT / "tools" / "demo_console" / "showcase_cases.py"

JS_SOURCE = JS_PATH.read_text(encoding="utf-8")
HTML_SOURCE = HTML_PATH.read_text(encoding="utf-8")
SERVE_SOURCE = SERVE_PATH.read_text(encoding="utf-8")
SEED_SOURCE = SEED_PATH.read_text(encoding="utf-8")

_RUN_IDS = ("run-showcase-a", "run-showcase-b", "run-showcase-c")


# ── Row projection: seed → DB-row shapes (mirrors the SQL INSERTs) ──────────

def _trace_for(run_id: str) -> str:
    return "trace-%s-0000000000000000000000000000" % run_id


def _task_run_row(core: dict) -> dict:
    return {
        "run_id": core["run_id"],
        "repo": core["repo"],
        "pr_number": core["pr_number"],
        "branch": core["fix_branch"],
        "status": core["final_status"],
        "current_stage": core["stages"][-1]["stage"],
        "attempt": 1,
        "verdict": "PASS" if core["final_status"] == "MERGED" else "FAIL",
        "last_error": core["last_error"],
        "created_at": core["stages"][0]["started_at"],
        "updated_at": core["stages"][-1]["completed_at"],
        "trace_id": _trace_for(core["run_id"]),
    }


def _stage_run_rows(core: dict) -> list:
    return [
        {"id": i + 1, "run_id": core["run_id"], "stage": s["stage"],
         "agent": s["agent"], "attempt": 1, "status": s["status"],
         "started_at": s["started_at"], "completed_at": s["completed_at"],
         "verdict": s["verdict"], "detail": "showcase stage %s" % s["stage"]}
        for i, s in enumerate(core["stages"])
    ]


def _revision_row(core: dict) -> dict:
    return {
        "binding_id": "rev-%s-0000000000000000000000000000" % core["run_id"],
        "run_id": core["run_id"],
        "repo": core["repo"],
        "pr_number": core["pr_number"],
        "base_sha": core["shas"]["base_sha"],
        "head_sha": core["shas"]["head_sha"],
        "recorded_at": core["stages"][0]["started_at"],
        "pr_binding": {
            "repo": core["repo"],
            "pr_number": core["pr_number"],
            "fix_branch": core["fix_branch"],
            "base_branch": core["base_branch"],
            "head_sha": core["shas"]["head_sha"],
            "recorded_at": core["stages"][0]["started_at"],
        },
    }


def _gateway_call_rows(core: dict) -> list:
    return [dict(c, caller_agent="coordinator",
                 correlation_id="corr-%s" % c["request_id"],
                 target_repo=core["repo"], target_branch=core["base_branch"])
            for c in core["mcp_calls"]]


def _rollback_rows(core: dict) -> list:
    if not core["rollback"]:
        return []
    rb = core["rollback"]
    return [{
        "rollback_id": rb["rollback_id"],
        "parent_run_id": core["run_id"],
        "revert_run_id": None,
        "reverted_merge_sha": rb["reverted_merge_sha"],
        "repo": core["repo"],
        "pr_number": core["pr_number"],
        "status": rb["status"],
        "fail_reason": rb["fail_reason"],
        "revert_result_sha": rb["revert_result_sha"],
        "reverify_verdict": rb["reverify_verdict"],
        "created_at": core["stages"][-1]["completed_at"],
        "updated_at": core["stages"][-1]["completed_at"],
    }]


def _audit_summary(core: dict) -> dict:
    by_action = {}
    for ev in core["audit_events"]:
        by_action[ev["action"]] = by_action.get(ev["action"], 0) + 1
    return {"total": len(core["audit_events"]), "by_action": by_action}


def _source_for(run_id: str) -> PostgresSnapshotSource:
    return PostgresSnapshotSource(
        dsn="postgresql://user:pass@127.0.0.1:5432/x",
        run_id=run_id,
        expected_database="mergepilot_audit",
        expected_role="mergepilot_reader",
        expected_environment_id="mergepilot-test-ephemeral",
        expected_server_addresses=["127.0.0.1"],
        expected_server_port=5432,
        expected_application_name="mergepilot_isolated_live_reader",
    )


def _assemble(run_id: str) -> dict:
    """Assemble the case bundle exactly as the real stack would (pure)."""
    core = case_core(run_id)
    return _source_for(run_id)._assemble_bundle(
        task_run=_task_run_row(core),
        stage_runs=_stage_run_rows(core),
        stage_events=[],
        revision=_revision_row(core),
        gateway_calls=_gateway_call_rows(core),
        rollback_events=_rollback_rows(core),
        audit_summary=_audit_summary(core),
    )


def _canonical(bundle: dict) -> str:
    clean = {k: v for k, v in bundle.items()
             if k not in ("bundle_sha256", "generated_at")}
    return json.dumps(clean, sort_keys=True, ensure_ascii=False)


# ── 1/2: registry shape & identifier uniqueness ─────────────────────────────

class TestShowcaseRegistry(unittest.TestCase):

    def test_exactly_three_cases(self):
        self.assertEqual(len(SHOWCASE_CASES), 3)
        self.assertEqual(set(SHOWCASE_CASES.keys()), set(_RUN_IDS))

    def test_case_ids_and_run_ids_unique_and_wellformed(self):
        case_ids = [SHOWCASE_CASES[r]["case_id"] for r in _RUN_IDS]
        self.assertEqual(len(set(case_ids)), 3)
        for run_id in _RUN_IDS:
            self.assertTrue(re.fullmatch(r"[a-zA-Z0-9_-]+", run_id))
            self.assertTrue(
                re.fullmatch(r"[a-z0-9-]+", SHOWCASE_CASES[run_id]["case_id"]))

    def test_case_ids_do_not_collide_with_run_ids(self):
        ids = {SHOWCASE_CASES[r]["case_id"] for r in _RUN_IDS}
        ids |= set(_RUN_IDS)
        self.assertEqual(len(ids), 6)


# ── 3: PR / SHA legality & cross-case conflict-freedom ──────────────────────

class TestIdentifierIntegrity(unittest.TestCase):

    def test_pr_numbers_distinct_positive_ints(self):
        prs = [SHOWCASE_CASES[r]["pr_number"] for r in _RUN_IDS]
        self.assertEqual(len(set(prs)), 3)
        for pr in prs:
            self.assertIsInstance(pr, int)
            self.assertGreater(pr, 0)

    def test_git_shas_legal_40hex(self):
        for run_id in _RUN_IDS:
            case = SHOWCASE_CASES[run_id]
            for key in ("base_sha", "head_sha", "merge_sha", "drifted_sha",
                        "recovered_sha"):
                sha = case.get(key)
                if sha is None:
                    continue
                self.assertRegex(
                    sha, r"^[0-9a-f]{40}$", (run_id, key))

    def test_no_sha_conflicts_across_cases(self):
        seen = set()
        for run_id in _RUN_IDS:
            case = SHOWCASE_CASES[run_id]
            for key in ("base_sha", "head_sha", "merge_sha", "drifted_sha",
                        "recovered_sha"):
                sha = case.get(key)
                if sha is None:
                    continue
                self.assertNotIn(sha, seen, (run_id, key))
                seen.add(sha)


# ── 4/5/6/7: per-case semantics ─────────────────────────────────────────────

class TestCaseSemantics(unittest.TestCase):

    def test_final_statuses_match_story(self):
        self.assertEqual(SHOWCASE_CASES["run-showcase-a"]["final_status"],
                         "MERGED")
        self.assertEqual(SHOWCASE_CASES["run-showcase-b"]["final_status"],
                         "FAIL")
        self.assertEqual(SHOWCASE_CASES["run-showcase-c"]["final_status"],
                         "ROLLED_BACK")

    def test_case_a_allow_l2_verifier_merge(self):
        case = SHOWCASE_CASES["run-showcase-a"]
        allow_l2 = [c for c in case["mcp_calls"]
                    if c[3] == "ALLOW" and c[2] == "merge_pull_request"
                    and c[5]]
        self.assertTrue(allow_l2, "case A needs an ALLOW merge call with "
                                  "an L2 ticket")
        stages = {s[0]: s for s in case["stages"]}
        self.assertEqual(stages["verify"][2], "COMPLETED")
        self.assertEqual(stages["verify"][3], "PASS")
        self.assertEqual(stages["merge"][2], "COMPLETED")
        self.assertEqual(stages["merge"][3], "MERGED")
        self.assertTrue(case["merge_sha"])
        self.assertFalse(any(c[3] == "DENY" for c in case["mcp_calls"]))

    def test_case_b_deny_failclosed_reason_no_merge(self):
        case = SHOWCASE_CASES["run-showcase-b"]
        deny = [c for c in case["mcp_calls"] if c[3] == "DENY"]
        self.assertEqual(len(deny), 1)
        self.assertTrue(deny[0][4], "reason code required")
        self.assertTrue(deny[0][8], "error text required")
        stage_names = [s[0] for s in case["stages"]]
        self.assertNotIn("merge", stage_names)
        self.assertNotIn("verify", stage_names)
        self.assertFalse(case.get("merge_sha"))
        self.assertTrue(case["last_error"])
        failed = [s for s in case["stages"] if s[2] == "FAILED"]
        self.assertTrue(failed)
        self.assertFalse(any(
            c[2] == "merge_pull_request" and c[3] == "ALLOW"
            for c in case["mcp_calls"]))

    def test_case_c_drift_block_rollback_recovered(self):
        case = SHOWCASE_CASES["run-showcase-c"]
        self.assertNotEqual(case["head_sha"], case["drifted_sha"])
        drift = [c for c in case["mcp_calls"]
                 if c[3] == "DENY" and c[4] == "REVISION_DRIFT"]
        self.assertEqual(len(drift), 1)
        self.assertEqual(drift[0][7], "DRIFTED_SHA")
        rb = case["rollback"]
        self.assertEqual(rb["status"], "RECOVERED")
        self.assertEqual(rb["reverify_verdict"], "PASS")
        stage_names = [s[0] for s in case["stages"]]
        self.assertIn("rollback", stage_names)
        self.assertIn("drift-check", stage_names)
        core = case_core("run-showcase-c")
        self.assertEqual(core["rollback"]["reverted_merge_sha"],
                         case["merge_sha"])
        self.assertEqual(core["rollback"]["revert_result_sha"],
                         case["recovered_sha"])


# ── 8: strict timeline ordering ─────────────────────────────────────────────

class TestTimelineOrder(unittest.TestCase):

    def test_each_case_timeline_strictly_ordered(self):
        for run_id in _RUN_IDS:
            stages = SHOWCASE_CASES[run_id]["stages"]
            for stage in stages:
                self.assertLess(stage[4], stage[5], (run_id, stage[0]))
            for prev, nxt in zip(stages, stages[1:]):
                self.assertLessEqual(prev[5], nxt[4],
                                     (run_id, prev[0], nxt[0]))

    def test_case_b_timeline_terminates_at_rejection(self):
        stages = SHOWCASE_CASES["run-showcase-b"]["stages"]
        self.assertEqual(stages[-1][2], "FAILED")


# ── 9: byte-stable determinism ──────────────────────────────────────────────

class TestDeterminism(unittest.TestCase):

    def test_seed_sql_byte_identical_across_builds(self):
        self.assertEqual(build_showcase_seed_sql(),
                         build_showcase_seed_sql())

    def test_case_cores_byte_identical_across_builds(self):
        for run_id in _RUN_IDS:
            a = json.dumps(case_core(run_id), sort_keys=True)
            b = json.dumps(case_core(run_id), sort_keys=True)
            self.assertEqual(a, b, run_id)

    def test_assembled_bundle_canonical_bytes_identical(self):
        for run_id in _RUN_IDS:
            b1, b2 = _assemble(run_id), _assemble(run_id)
            self.assertEqual(_canonical(b1), _canonical(b2), run_id)

    def test_no_clock_or_random_in_seed_module(self):
        tree = ast.parse(SEED_SOURCE)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name, ("random", "time",
                                                  "datetime", "secrets"))
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn(node.module, ("random", "time",
                                               "datetime", "secrets"))

    def test_fixed_iso_timestamps_only(self):
        sql = build_showcase_seed_sql()
        stamps = set(re.findall(
            r"'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00)'", sql))
        self.assertTrue(stamps)
        # Every timestamp is a FIXED literal on the showcase seed day and
        # inside the three case windows (10:04 / 10:05 / 10:06 + audit
        # offsets); no DEFAULT now() value can appear because every
        # surfaced timestamp column is inserted explicitly.
        for stamp in stamps:
            self.assertRegex(stamp, r"^2026-08-17T10:\d{2}:\d{2}\+00:00$")


# ── bundle assembly through the REAL postgres source (validator reuse) ──────

class TestBundleAssembly(unittest.TestCase):

    def test_all_three_case_bundles_pass_the_existing_validator(self):
        for run_id in _RUN_IDS:
            bundle = _assemble(run_id)
            self.assertEqual(validate_bundle(
                bundle, expected_mode="ISOLATED_LIVE"), [], run_id)
            self.assertEqual(verify_bundle_integrity(bundle), [], run_id)

    def test_bundle_sha256_excludes_only_volatile_fields(self):
        bundle = _assemble("run-showcase-a")
        b2 = copy.deepcopy(bundle)
        b2["generated_at"] = "2030-01-01T00:00:00Z"
        self.assertEqual(b2["bundle_sha256"], bundle["bundle_sha256"])

    def test_gateway_calls_surfaced_with_l2_ticket(self):
        bundle = _assemble("run-showcase-a")
        calls = bundle["gateway_calls"]
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c["decision"] == "ALLOW" for c in calls))
        self.assertTrue(all(c["ticket_id"] == "tkt-showcase-a-l2"
                            for c in calls))
        self.assertEqual(calls[0]["phase"], "INTENT")
        self.assertEqual(calls[1]["phase"], "RESULT")
        self.assertEqual(calls[1]["git_sha"],
                         SHOWCASE_CASES["run-showcase-a"]["merge_sha"])

    def test_case_b_bundle_carries_deny_and_reason(self):
        bundle = _assemble("run-showcase-b")
        deny = [c for c in bundle["gateway_calls"]
                if c["decision"] == "DENY"]
        self.assertEqual(len(deny), 1)
        self.assertEqual(deny[0]["reason_code"], "PROTECTED_PATH_PREFIX")
        self.assertTrue(deny[0]["error"])
        self.assertEqual(bundle["run_failure_reason"],
                         SHOWCASE_CASES["run-showcase-b"]["last_error"])
        self.assertEqual(bundle["final_status"], "FAIL")
        stage_names = [s["stage"] for s in bundle["workflow_stages"]]
        self.assertNotIn("merge", stage_names)

    def test_case_c_bundle_carries_drift_and_rollback(self):
        case = SHOWCASE_CASES["run-showcase-c"]
        bundle = _assemble("run-showcase-c")
        self.assertEqual(bundle["final_status"], "ROLLED_BACK")
        self.assertEqual(len(bundle["rollback_events"]), 1)
        rb = bundle["rollback_events"][0]
        self.assertEqual(rb["status"], "RECOVERED")
        self.assertEqual(rb["reverted_merge_sha"], case["merge_sha"])
        self.assertEqual(rb["revert_result_sha"], case["recovered_sha"])
        self.assertEqual(rb["reverify_verdict"], "PASS")
        drift = [c for c in bundle["gateway_calls"]
                 if c["reason_code"] == "REVISION_DRIFT"]
        self.assertEqual(drift[0]["git_sha"], case["drifted_sha"])
        self.assertEqual(bundle["pr"]["head_sha"], case["head_sha"])

    def test_case_c_recovered_sha_distinct_from_other_shas(self):
        """F2 regression: reverted / drifted / recovered SHAs must all be
        present in the bundle AND never confused with each other."""
        case = SHOWCASE_CASES["run-showcase-c"]
        bundle = _assemble("run-showcase-c")
        rb = bundle["rollback_events"][0]
        recovered = rb["revert_result_sha"]
        reverted = rb["reverted_merge_sha"]
        drift = [c for c in bundle["gateway_calls"]
                 if c["reason_code"] == "REVISION_DRIFT"][0]["git_sha"]
        approved = bundle["pr"]["head_sha"]
        self.assertEqual(recovered, case["recovered_sha"])
        self.assertEqual(reverted, case["merge_sha"])
        self.assertEqual(drift, case["drifted_sha"])
        self.assertEqual(approved, case["head_sha"])
        self.assertEqual(len({recovered, reverted, drift, approved}), 4)

    def test_renderer_has_recovered_sha_paths(self):
        """F2 regression: findings + safety render the recovered SHA from
        the live bundle field, escaped."""
        self.assertIn("e.revert_result_sha", JS_SOURCE)
        findings_seg = JS_SOURCE[JS_SOURCE.index("Drift & Rollback Facts"):]
        self.assertGreaterEqual(findings_seg.count("revert_result_sha"), 1)
        safety_seg = JS_SOURCE[JS_SOURCE.index("Rollback Events"):]
        self.assertGreaterEqual(safety_seg.count("revert_result_sha"), 1)
        self.assertIn("Recovered SHA", JS_SOURCE)
        for raw in ("+ e.revert_result_sha +", "+ c.git_sha +"):
            self.assertNotIn(raw, JS_SOURCE, raw)

    def test_stage_timing_surfaced_for_timeline_sort(self):
        bundle = _assemble("run-showcase-c")
        starts = [s["started_at"] for s in bundle["workflow_stages"]]
        self.assertTrue(all(starts))
        # Sorted by started_at == story order (not alphabetical stage).
        story = [s["stage"] for s in case_core("run-showcase-c")["stages"]]
        by_time = [s["stage"] for s in sorted(
            bundle["workflow_stages"], key=lambda s: s["started_at"])]
        self.assertEqual(by_time, story)


# ── 10: renderer escaping ───────────────────────────────────────────────────

class TestRendererEscaping(unittest.TestCase):

    def test_every_case_fact_interpolation_is_escaped(self):
        # New render paths must wrap every dynamic value in esc() or the
        # status-chip helper (which escapes internally).
        for helper in ("showcaseBadge", "renderFindings", "renderTrace",
                       "renderSafety", "evidenceAuditPanel"):
            self.assertIn(helper, JS_SOURCE)
        # Raw interpolations of case-fact fields without esc( are banned.
        for raw in ("+ c.request_id +", "+ c.tool +", "+ c.reason_code +",
                    "+ c.error +", "+ c.ticket_id +", "+ c.git_sha +",
                    "+ e.rollback_id +", "+ e.fail_reason +",
                    "+ e.reverted_merge_sha +", "+ meta.caseId +",
                    "+ meta.name +", "+ snapshot.run_failure_reason +"):
            self.assertNotIn(raw, JS_SOURCE, raw)

    def test_esc_still_defined_and_used(self):
        self.assertIn("function esc(", JS_SOURCE)
        self.assertGreater(JS_SOURCE.count("esc("), 40)


# ── 11/12/13: page set, API surface, engine invariants ──────────────────────

class TestSurfaceUnchanged(unittest.TestCase):

    def test_eight_pages_unchanged_no_ninth_page(self):
        self.assertEqual(len(PAGES), 8)
        js_pages = re.search(r"var PAGES = \[([^\]]+)\]", JS_SOURCE)
        self.assertIsNotNone(js_pages)
        names = re.findall(r"'([a-z-]+)'", js_pages.group(1))
        self.assertEqual(len(names), 8)
        self.assertEqual(set(names), set(PAGES))
        sections = re.findall(r'<section[^>]*class="page[^"]*"',
                              HTML_SOURCE)
        self.assertEqual(len(sections), 8)

    def test_api_endpoint_set_unchanged(self):
        self.assertEqual(ALLOWED_URLS,
                         {"/api/live/status", "/api/live/snapshot"})
        for literal in re.findall(r"['\"](/api/[^'\"]*)['\"]", JS_SOURCE):
            self.assertIn(literal, ALLOWED_URLS)
        self.assertEqual(SERVE_SOURCE.count('"/api/live/snapshot"'), 1)
        self.assertEqual(SERVE_SOURCE.count('"/api/live/status"'), 1)

    def test_single_timer_and_fetch_wrapper_unchanged(self):
        self.assertEqual(JS_SOURCE.count("setInterval("), 1)
        self.assertIn("clearInterval(", JS_SOURCE)
        self.assertEqual(JS_SOURCE.count("fetch("), 1)
        for marker in ("localStorage", "sessionStorage",
                       "location.reload", "'REPLAY'", '"REPLAY"'):
            self.assertNotIn(marker, JS_SOURCE)

    def test_showcase_js_registry_matches_seed_registry(self):
        block = re.search(r"var SHOWCASE_CASES = \{(.*?)\n  \};",
                          JS_SOURCE, re.S)
        self.assertIsNotNone(block)
        js_case_ids = re.findall(r"caseId: '([^']+)'", block.group(1))
        js_run_ids = re.findall(
            r"'(run-showcase-[a-c])': \{", block.group(1))
        self.assertEqual(sorted(js_case_ids),
                         sorted(SHOWCASE_CASES[r]["case_id"]
                                for r in _RUN_IDS))
        self.assertEqual(sorted(js_run_ids), sorted(_RUN_IDS))
        js_names = re.findall(r"name: '([^']+)'", block.group(1))
        self.assertEqual(sorted(js_names),
                         sorted(SHOWCASE_CASES[r]["case_name"]
                                for r in _RUN_IDS))

    def test_page_bundle_field_reads_unchanged(self):
        from live_refresh import verify_js_contract
        result = verify_js_contract(JS_SOURCE)
        self.assertTrue(result["ok"])


# ── 14: INSERT-only, table-allowlisted, no backend refactor ─────────────────

class TestSeedScope(unittest.TestCase):

    def test_seed_sql_insert_only(self):
        sql = build_showcase_seed_sql()
        stripped = re.sub(r"--[^\n]*", "", sql)
        self.assertNotRegex(stripped.upper(),
                            r"\b(ALTER|CREATE|DROP|UPDATE|DELETE|TRUNCATE|"
                            r"GRANT|REVOKE)\b")

    def test_seed_targets_only_read_tables(self):
        sql = build_showcase_seed_sql()
        targets = set(re.findall(r"INSERT INTO ([a-z_]+)", sql))
        self.assertEqual(targets, {
            "task_runs", "stage_runs", "stage_events",
            "revision_bindings", "run_pr_bindings", "mcp_calls",
            "rollback_runs", "audit_events"})
        # Every target is a table the read-only source already queries.
        from postgres_source import SCHEMA_CONTRACT
        for table in targets:
            self.assertIn(table, SCHEMA_CONTRACT)

    def test_seed_module_spawns_no_subprocess_no_docker(self):
        # The seed is a pure SQL-string builder: no docker/compose/psql
        # interaction happens inside the module (the orchestrator pipes
        # the SQL to psql itself).
        tree = ast.parse(SEED_SOURCE)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(
                        alias.name, ("subprocess", "docker", "compose"))
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn(node.module,
                                 ("subprocess", "docker", "compose"))

    def test_postgres_source_still_read_only_contract(self):
        from postgres_source import PRIVILEGE_CHECKED_TABLES
        self.assertEqual(len(PRIVILEGE_CHECKED_TABLES), 9)
        self.assertNotIn("approvals", PRIVILEGE_CHECKED_TABLES)


# ── 15: no real data / credentials / production claims ──────────────────────

class TestNoRealDataOrClaims(unittest.TestCase):

    def test_seed_and_sql_free_of_secrets(self):
        self.assertEqual(scan_secrets(SEED_SOURCE), 0)
        self.assertEqual(scan_secrets(build_showcase_seed_sql()), 0)

    def test_synthetic_repo_only_no_real_org(self):
        sql = build_showcase_seed_sql()
        self.assertIn("'mergepilot/showcase-demo'", sql)
        for real in ("nghqqa", "MergePilot/MergePilot", "github.com/"):
            self.assertNotIn(real, sql)

    def test_no_verified_claims_anywhere(self):
        for text in (SEED_SOURCE, JS_SOURCE):
            for claim in ("production_verified=true",
                          "database_verified=true",
                          "application_integration_verified=true",
                          "revision producer contract verified"):
                self.assertNotIn(claim.lower(), text.lower())

    def test_disclosure_present_in_seed_and_ui(self):
        self.assertIn("Deterministic showcase seed", SEED_SOURCE)
        self.assertIn("Not external customer data".lower() or
                      "not external customer data",
                      (SEED_SOURCE + JS_SOURCE).lower())
        self.assertIn("not production evidence",
                      (SEED_SOURCE + JS_SOURCE).lower())
        self.assertIn("Deterministic showcase seed", JS_SOURCE)


# ── 16: no file writes (no evidence/, no verification/) ─────────────────────

class TestNoEvidenceWrites(unittest.TestCase):

    def test_seed_module_has_no_file_io(self):
        tree = ast.parse(SEED_SOURCE)
        banned_attrs = {"write_text", "write_bytes", "mkdir", "remove",
                        "unlink", "makedirs"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "id", None) or getattr(func, "attr",
                                                            None)
                self.assertNotIn(name, banned_attrs,
                                 "seed module must not do file I/O")
                if isinstance(func, ast.Attribute) and \
                        isinstance(func.value, ast.Name):
                    self.assertNotEqual(
                        (func.value.id, name), ("os", "replace"),
                        "os.replace is a file operation")
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn(node.module, ("evidence_manifest",))

    def test_generated_sql_targets_no_evidence_or_verification_paths(self):
        sql = build_showcase_seed_sql()
        self.assertNotIn("evidence/", sql)
        self.assertNotIn("verification/", sql)
        self.assertNotIn("screenshot", sql.lower())


# ── F1: audit_events replay idempotency ─────────────────────────────────────

# Minimal table models for executing the REAL generated seed SQL against a
# real SQL engine in-process (SQLite >= 3.39 understands both
# ``ON CONFLICT DO NOTHING`` and ``IS NOT DISTINCT FROM``). This mirrors the
# migration columns the seed INSERTs touch — the authoritative schema stays
# in tools/audit-db/; this harness only proves the seed's replay behavior.
_REPLAY_DDL = """
CREATE TABLE task_runs (run_id TEXT PRIMARY KEY, room_id TEXT, repo TEXT,
  pr_number INTEGER, branch TEXT, status TEXT, current_stage TEXT,
  attempt INTEGER, verdict TEXT, last_error TEXT, created_at TEXT,
  updated_at TEXT, skill_data_state TEXT);
CREATE TABLE run_pr_bindings (binding_id TEXT PRIMARY KEY, run_id TEXT,
  repo TEXT, pr_number INTEGER, fix_branch TEXT, base_branch TEXT,
  head_sha TEXT, recorded_at TEXT);
CREATE TABLE revision_bindings (binding_id TEXT PRIMARY KEY, run_id TEXT,
  repo TEXT, pr_number INTEGER, base_sha TEXT, head_sha TEXT,
  source_call_id TEXT, source_evidence_digest TEXT, recorded_at TEXT);
CREATE TABLE mcp_calls (request_id TEXT PRIMARY KEY, correlation_id TEXT,
  phase TEXT, ts TEXT NOT NULL, caller_agent TEXT NOT NULL,
  tool TEXT NOT NULL, decision TEXT NOT NULL, reason_code TEXT,
  ticket_id TEXT, target_repo TEXT, target_branch TEXT, result_status TEXT,
  git_sha TEXT, error TEXT, run_id TEXT);
CREATE TABLE stage_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,
  stage TEXT, agent TEXT, attempt INTEGER, status TEXT, started_at TEXT,
  completed_at TEXT, verdict TEXT, detail TEXT,
  UNIQUE(run_id, stage, attempt));
CREATE TABLE stage_events (event_id TEXT PRIMARY KEY, room_id TEXT,
  run_id TEXT, event_type TEXT, stage TEXT, status TEXT, sender TEXT);
CREATE TABLE audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT, agent TEXT, action TEXT, target TEXT, detail TEXT,
  sha TEXT, via TEXT, ts TEXT);
CREATE TABLE rollback_runs (rollback_id TEXT PRIMARY KEY,
  parent_run_id TEXT, reverted_merge_sha TEXT, repo TEXT,
  pr_number INTEGER, trigger_event_id TEXT, status TEXT, fail_reason TEXT,
  revert_result_sha TEXT, reverify_verdict TEXT, created_at TEXT,
  updated_at TEXT);
"""


class TestSeedReplayIdempotency(unittest.TestCase):
    """F1: replaying build_showcase_seed_sql() must not duplicate rows.

    audit_events has no unique constraint, so the seed guards every audit
    INSERT with a full-record WHERE NOT EXISTS (8 columns compared with IS
    NOT DISTINCT FROM so NULL sha rows match). The generated SQL bytes are
    executed verbatim against SQLite here; the real-stack replay is
    additionally verified in the E2E round against PostgreSQL.
    """

    def test_sqlite_supports_required_dialect(self):
        # Fail-closed precondition (no skip): the in-process engine must
        # understand the exact dialect the generated SQL uses.
        self.assertGreaterEqual(sqlite3.sqlite_version_info, (3, 39, 0),
                                "SQLite >= 3.39 required for IS NOT "
                                "DISTINCT FROM / ON CONFLICT DO NOTHING")

    def _replay_db(self):
        import sqlite3 as _s
        if _s.sqlite_version_info < (3, 39, 0):
            self.fail("SQLite >= 3.39 required")
        db = _s.connect(":memory:")
        db.executescript(_REPLAY_DDL)
        return db

    def _apply_seed(self, db):
        sql = build_showcase_seed_sql()
        cleaned = "\n".join(
            line for line in sql.splitlines()
            if not line.lstrip().startswith("--"))
        db.executescript(cleaned)

    def _counts(self, db):
        out = {}
        for table in ("task_runs", "run_pr_bindings", "revision_bindings",
                      "mcp_calls", "stage_runs", "stage_events",
                      "audit_events", "rollback_runs"):
            out[table] = db.execute(
                "SELECT count(*) FROM %s" % table).fetchone()[0]
        out["audit_by_action"] = dict(db.execute(
            "SELECT action, count(*) FROM audit_events "
            "GROUP BY action ORDER BY action").fetchall())
        return out

    def test_first_application_exact_counts(self):
        db = self._replay_db()
        self._apply_seed(db)
        self.assertEqual(self._counts(db), {
            "task_runs": 3, "run_pr_bindings": 3, "revision_bindings": 3,
            "mcp_calls": 6, "stage_runs": 12, "stage_events": 12,
            "audit_events": 12, "rollback_runs": 1,
            "audit_by_action": {
                "close_pr": 1, "drift_detected": 1, "fix": 1, "merge": 2,
                "policy_deny": 1, "review": 3, "rollback": 1, "verify": 2},
        })

    def test_replay_does_not_duplicate_anything(self):
        db = self._replay_db()
        self._apply_seed(db)
        first = self._counts(db)
        self._apply_seed(db)
        second = self._counts(db)
        self._apply_seed(db)
        third = self._counts(db)
        self.assertEqual(second, first)
        self.assertEqual(third, first)
        self.assertEqual(first["task_runs"], 3)
        self.assertEqual(first["audit_events"], 12)

    def test_replayed_audit_summary_matches_canonical_cores(self):
        db = self._replay_db()
        self._apply_seed(db)
        self._apply_seed(db)
        for run_id in _RUN_IDS:
            core = case_core(run_id)
            rows = db.execute(
                "SELECT agent, action FROM audit_events WHERE task_id = ?"
                " ORDER BY action, agent", (run_id,)).fetchall()
            self.assertEqual(len(rows), len(core["audit_events"]), run_id)
            by_action = {}
            for _agent, action in rows:
                by_action[action] = by_action.get(action, 0) + 1
            canonical = _audit_summary(core)["by_action"]
            self.assertEqual(by_action, canonical, run_id)

    def test_audit_inserts_are_full_record_not_exists_guarded(self):
        sql = build_showcase_seed_sql()
        audit_stmts = re.findall(
            r"INSERT INTO audit_events \(.*?\);", sql, re.S)
        self.assertEqual(len(audit_stmts), 12)
        for stmt in audit_stmts:
            self.assertIn("SELECT ", stmt)
            self.assertIn("WHERE NOT EXISTS", stmt)
            self.assertEqual(stmt.count("IS NOT DISTINCT FROM"), 8,
                             stmt[:80])
            for col in ("task_id", "agent", "action", "target", "detail",
                        "sha", "via", "ts"):
                self.assertIn("ex.%s IS NOT DISTINCT FROM" % col, stmt)

    def test_seed_sql_still_insert_only_no_cleanup_verbs(self):
        sql = build_showcase_seed_sql()
        stripped = re.sub(r"--[^\n]*", "", sql)
        for verb in ("DELETE", "UPDATE", "DROP", "TRUNCATE", "ALTER",
                     "CREATE", "GRANT", "REVOKE"):
            self.assertNotRegex(stripped.upper(),
                                r"\b%s\b" % verb)
        self.assertNotIn("subprocess", SEED_SOURCE)




class TestFailClosedValidation(unittest.TestCase):

    def _broken(self, mutate):
        reg = copy.deepcopy(SHOWCASE_CASES)
        mutate(reg)
        validate_showcase_cases(reg)

    def test_missing_case_rejected(self):
        reg = copy.deepcopy(SHOWCASE_CASES)
        del reg["run-showcase-c"]
        with self.assertRaises(ShowcaseSeedError):
            validate_showcase_cases(reg)

    def test_duplicate_run_id_rejected(self):
        def mutate(reg):
            reg["run-showcase-b"]["case_id"] = \
                reg["run-showcase-a"]["case_id"]
        with self.assertRaises(ShowcaseSeedError):
            self._broken(mutate)

    def test_malformed_sha_rejected(self):
        def mutate(reg):
            reg["run-showcase-a"]["head_sha"] = "z" * 40
        with self.assertRaises(ShowcaseSeedError):
            self._broken(mutate)

    def test_pr_collision_rejected(self):
        def mutate(reg):
            reg["run-showcase-b"]["pr_number"] = \
                reg["run-showcase-a"]["pr_number"]
        with self.assertRaises(ShowcaseSeedError):
            self._broken(mutate)

    def test_case_b_with_merge_stage_rejected(self):
        def mutate(reg):
            reg["run-showcase-b"]["stages"].append(
                ("merge", "manager", "COMPLETED", "MERGED",
                 "2026-08-17T10:05:20+00:00",
                 "2026-08-17T10:05:25+00:00"))
        with self.assertRaises(ShowcaseSeedError):
            self._broken(mutate)

    def test_case_b_with_fake_merge_allow_rejected(self):
        def mutate(reg):
            reg["run-showcase-b"]["mcp_calls"].append(
                ("mcp-showcase-b-002", "INTENT", "merge_pull_request",
                 "ALLOW", "POLICY_PASS", "tkt-x", None, None, None))
        with self.assertRaises(ShowcaseSeedError):
            self._broken(mutate)

    def test_unordered_timeline_rejected(self):
        def mutate(reg):
            stages = reg["run-showcase-a"]["stages"]
            stages[0], stages[1] = stages[1], stages[0]
        with self.assertRaises(ShowcaseSeedError):
            self._broken(mutate)

    def test_case_c_approved_equals_drifted_rejected(self):
        def mutate(reg):
            reg["run-showcase-c"]["drifted_sha"] = \
                reg["run-showcase-c"]["head_sha"]
        with self.assertRaises(ShowcaseSeedError):
            self._broken(mutate)

    def test_case_c_missing_rollback_rejected(self):
        def mutate(reg):
            reg["run-showcase-c"]["rollback"] = None
        with self.assertRaises(ShowcaseSeedError):
            self._broken(mutate)

    def test_case_a_missing_l2_ticket_rejected(self):
        def mutate(reg):
            calls = reg["run-showcase-a"]["mcp_calls"]
            reg["run-showcase-a"]["mcp_calls"] = [
                c[:5] + (None,) + c[6:] for c in calls]
        with self.assertRaises(ShowcaseSeedError):
            self._broken(mutate)

    def test_missing_final_status_rejected(self):
        def mutate(reg):
            reg["run-showcase-a"]["final_status"] = "SOMETHING_ELSE"
        with self.assertRaises(ShowcaseSeedError):
            self._broken(mutate)

    def test_validator_reuses_bundle_contract_not_a_copy(self):
        # The bundle-level validation exercised above IS the existing
        # validator (schema.validate_bundle + integrity). The seed module
        # must not import or reimplement bundle validation itself.
        tree = ast.parse(SEED_SOURCE)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn(node.module, ("schema", "integrity",
                                               "live_poller"))
        self.assertFalse(hasattr(sc, "validate_bundle"))
        self.assertFalse(hasattr(sc, "REQUIRED_FIELDS"))


if __name__ == "__main__":
    unittest.main()
