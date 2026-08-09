#!/usr/bin/env python3
"""D2B-3 unit tests: deploy-owned production collector validation + anti-forgery.
Pure (no WSL, no production, no network). Uses synthetic raw dicts."""
from __future__ import annotations
import builtins, hashlib, json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import capture_production_live as P

HEAD = "a" * 40
H64 = "f" * 64
RUN_ID = "m5live-prod-run-001"
ROOM_ID = "!room:matrix-local.hiclaw.io"
HS = "matrix-local.hiclaw.io"
# Real Candidate consumer (tools/start-m5-0-candidate.sh:32 default; controller.py:99-100
# forbids the production default "controller" in Candidate mode).
CONSUMER = "m5-0-candidate"
WINDOW = ("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z")


def _agent(role):
    return {"role": role, "container_id": "cid-%s" % role, "image_id": "img-%s" % role,
            "matrix_user_id": "@%s:%s" % (role, HS), "started_at": "2026-01-01T00:00:00Z",
            "command_digest": H64, "log_digest": H64}


def _consistent_provenance(base: dict, run_id: str, room_id: str, window) -> dict:
    """Build provenance whose recomputable digests are correct for `base` (raw
    without provenance): raw_capture_sha256, collector_command_digest,
    collector_script_sha256. Trust-boundary digests use H64 placeholders."""
    command = ["capture_production_live.py", "--run-id", run_id, "--room-id", room_id,
               "--window-start", window[0], "--window-end", window[1]]
    with open(os.path.join(HERE, "capture_production_live.py"), "rb") as stream:
        script_sha = hashlib.sha256(stream.read()).hexdigest()
    return {
        "collector_kind": "deploy-owned-production-tier-c",
        "collector_script_sha256": script_sha,
        "collector_command_digest": hashlib.sha256(P.canonical_bytes(command)).hexdigest(),
        "run_key": run_id,
        "capture_window": {"started_at": window[0], "ended_at": window[1]},
        "collected_at": "2026-01-01T01:01:00Z",
        "raw_capture_sha256": hashlib.sha256(P.canonical_bytes(base)).hexdigest(),
        "db_snapshot_sha256": H64,
        "matrix_sync_sha256": H64,
        "container_snapshot_sha256": H64,
        "gateway_audit_sha256": H64,
        "github_mcp_log_sha256": H64,
        "matrix_event_provenance": {"sync_event_ids": ["$evt1"], "stage_event_ids": ["$evt1"]},
    }


def _raw(consumer: str = CONSUMER):
    """Valid raw production record with internally-consistent, recomputable
    provenance digests. Pass a non-default consumer to exercise rejection paths."""
    base = {
        "matrix_server_name": HS,
        "sync_events": [
            {"sync_batch_id": H64, "event_id": "$evt1", "room_id": ROOM_ID,
             "sender": "@manager:" + HS, "event_type": "M4F_RUN",
             "body_sha256": H64, "received_at": "t1", "consumer_name": consumer}],
        "stage_events": [
            {"stage_event_id": "se1", "matrix_event_id": "$evt1", "room_id": ROOM_ID,
             "sender": "@manager:" + HS, "event_type": "M4F_RUN", "stage": "m4f_snapshot",
             "status": "PROCESSED", "parsed_run_id": RUN_ID, "processed_by": consumer, "error_code": ""}],
        "agent_processes": {r: _agent(r) for r in ("manager", "reviewer", "fixer", "verifier")},
        "task_run": {"run_id": RUN_ID, "room_id": ROOM_ID, "status": "HOLD",
                     "current_stage": "m5_verify_passed", "verdict": "PASS",
                     "consumer_name": consumer, "revision_binding_id": "bnd-1",
                     "base_sha": "b" * 40, "head_sha": "c" * 40,
                     "review_stage_event_id": "se1", "fix_stage_event_id": "se1",
                     "verify_stage_event_id": "se1"},
        "skill_jobs": [{"skill_name": "diff-parse", "job_id": "j%d" % i, "invocation_id": "i%d" % i,
                         "status": "SUCCEEDED", "revision_binding_id": "bnd-1",
                         "output_schema_validated": True} for i in range(1, 7)],
        "mcp_calls": [{"call_id": "c1", "caller_agent": "fixer", "tool": "create_branch",
                        "decision": "ALLOW", "revision_binding_id": "bnd-1",
                        "base_sha": "b" * 40, "head_sha": "c" * 40,
                        "upstream_kind": "github-mcp", "audit_dsn_kind": "postgresql"}],
        "dispatch_rows": [{"dispatch_id": "d1", "role": "reviewer",
                           "stage_event_id": "se1", "source": "controller_reconcile"}],
        "watcher_config": {"excluded_prefixes": ["m5live-"]},
        "injection_scan": {"scanned_sources": ["runner", "send_as", "inject", "logs"],
                           "forbidden_invocations": []},
        "secret_scan": {"scanned_targets": ["cand", "gw", "gh"], "matches": []},
        "residue": {"containers": [], "networks": [], "volumes": [],
                     "temp_dirs": [], "open_prs": [], "branches": []},
    }
    base["provenance"] = _consistent_provenance(base, RUN_ID, ROOM_ID, WINDOW)
    return base


def _x(cond, msg):
    if not cond: raise AssertionError("FAIL: " + msg)
    print("  PASS:", msg)


def test_validate_success():
    ok, errs = P.validate_production(_raw(), HEAD)
    _x(ok, "valid raw with provenance → PASS (errs=%s)" % errs)


def test_source_commit_not_sha():
    ok, errs = P.validate_production(_raw(), "short")
    _x(not ok and any("source_commit" in e for e in errs), "short SHA → fail")


def test_run_key_mismatch():
    raw = _raw(); raw["provenance"]["run_key"] = "m5live-different"
    ok, errs = P.validate_production(raw, HEAD)
    _x(not ok and any("run_key" in e for e in errs), "run_key mismatch → fail")


def test_capture_window_invalid():
    raw = _raw(); raw["provenance"]["capture_window"] = {"started_at": "2026-01-01T02:00:00Z", "ended_at": "2026-01-01T01:00:00Z"}
    ok, errs = P.validate_production(raw, HEAD)
    _x(not ok and any("window" in e.lower() for e in errs), "window start>end → fail")


def test_provenance_digest_missing():
    raw = _raw(); del raw["provenance"]["db_snapshot_sha256"]
    ok, errs = P.validate_production(raw, HEAD)
    _x(not ok and any("db_snapshot" in e for e in errs), "missing digest → fail")


def test_provenance_digest_not_hex():
    raw = _raw(); raw["provenance"]["collector_script_sha256"] = "xyz"
    ok, errs = P.validate_production(raw, HEAD)
    _x(not ok and any("collector_script" in e for e in errs), "non-hex digest → fail")


def test_matrix_no_intersection():
    raw = _raw()
    raw["sync_events"][0]["event_id"] = "$different"
    raw["provenance"]["matrix_event_provenance"]["sync_event_ids"] = ["$different"]
    ok, errs = P.validate_production(raw, HEAD)
    _x(not ok and any("matrix" in e.lower() for e in errs), "no sync∩stage → fail")


def test_matrix_provenance_claimed_vs_actual():
    raw = _raw()
    raw["provenance"]["matrix_event_provenance"]["sync_event_ids"] = ["$wrong"]
    ok, errs = P.validate_production(raw, HEAD)
    _x(not ok and any("matrix" in e.lower() for e in errs), "claimed≠actual → fail")


def test_secret_matches_not_empty():
    raw = _raw(); raw["secret_scan"]["matches"] = ["leak"]
    ok, errs = P.validate_production(raw, HEAD)
    _x(not ok and any("secret" in e for e in errs), "matches non-empty → fail")


def test_residue_not_empty():
    raw = _raw(); raw["residue"]["containers"] = ["leftover"]
    ok, errs = P.validate_production(raw, HEAD)
    _x(not ok and any("residue" in e for e in errs), "residue non-empty → fail")


def test_schema_validator_unavailable():
    orig = builtins.__import__
    def blocked(name, *a, **kw):
        if name == "jsonschema": raise ImportError("blocked")
        return orig(name, *a, **kw)
    builtins.__import__ = blocked
    try:
        ok, errs = P.validate_production(_raw(), HEAD)
        _x(not ok and any("schema" in e.lower() or "jsonschema" in e.lower() for e in errs), "jsonschema unavailable → fail-closed")
    finally:
        builtins.__import__ = orig


def test_schema_additional_property_rejected():
    raw = _raw(); raw["unexpected_key"] = "bad"
    ok, errs = P.validate_production(raw, HEAD)
    _x(not ok, "additional property → schema reject")


def test_publish_success_mode():
    fd, p = tempfile.mkstemp(suffix=".json"); os.close(fd); os.remove(p)
    ok, err = P.publish_production(_raw(), HEAD, p)
    _x(ok and os.path.exists(p), "publish ok")
    data = json.load(open(p))
    _x(data["source_commit"] == HEAD, "source_commit in evidence")
    _x(data["schema_version"] == "1", "schema_version=1")
    if os.name == "posix":
        _x(oct(os.stat(p).st_mode & 0o777) == "0o644", "mode 100644")
    os.remove(p)


def test_publish_failure_leaves_no_new_file():
    raw = _raw(); raw["secret_scan"]["matches"] = ["leak"]
    target = tempfile.mkstemp(suffix=".json")[1] + "x.json"
    ok, err = P.publish_production(raw, HEAD, target)
    _x(not ok, "invalid raw → publish refused")
    _x(not os.path.exists(target), "no evidence file left")


def test_collector_rejects_input_arg():
    src = open(os.path.join(HERE, "capture_production_live.py"), encoding="utf-8").read()
    _x("--input" not in src, "no --input arg (collector queries sources directly)")
    _x("def load_raw_capture" not in src, "no raw-capture importer")


def test_secret_file_symlink_rejected():
    if os.name != "posix":
        print("  PASS: symlink test skipped on non-POSIX"); return
    d = tempfile.mkdtemp()
    real = os.path.join(d, "real"); link = os.path.join(d, "link")
    open(real, "w").write("secret"); os.symlink(real, link); os.chmod(real, 0o600)
    try:
        P.read_secret_file(link); _x(False, "symlink should be rejected")
    except ValueError:
        print("  PASS: symlink rejected")
    finally:
        os.remove(link); os.remove(real); os.rmdir(d)


def test_secret_file_wrong_mode_rejected():
    if os.name != "posix":
        print("  PASS: mode test skipped on non-POSIX"); return
    d = tempfile.mkdtemp(); p = os.path.join(d, "secret")
    open(p, "w").write("secret"); os.chmod(p, 0o644)
    try:
        P.read_secret_file(p); _x(False, "mode 0644 should be rejected")
    except ValueError:
        print("  PASS: mode 0644 rejected")
    finally:
        os.remove(p); os.rmdir(d)


def test_no_precomputed_booleans_in_schema():
    schema = json.load(open(os.path.join(HERE, "schemas", "production-live.schema.json")))
    blob = json.dumps(schema)
    for banned in ("observed_by_sync", "matrix_sender_bound", "bridged_to_review", "m5live_excluded"):
        _x(banned not in blob, "no %s in schema" % banned)


def test_production_send_as_detection():
    """collect_production scans logs for send_as/inject markers → forbidden_invocations."""
    src = open(os.path.join(HERE, "capture_production_live.py"), encoding="utf-8").read()
    _x("send_as(" in src, "collector scans for send_as( marker")
    _x("inject_skill_completion" in src, "collector scans for inject_skill_completion marker")


# ── Fix 2: production consumer real deployment conflict ──

def test_consumer_default_controller_rejected():
    """consumer_name='controller' (production default) cannot process m5live → reject."""
    raw = _raw(consumer="controller")
    ok, errs = P.validate_production(raw, HEAD)
    _x(not ok and any("consumer_name" in e and "controller" in e for e in errs),
       "controller consumer → fail (errs=%s)" % errs)


def test_consumer_empty_rejected():
    raw = _raw(consumer="")
    ok, errs = P.validate_production(raw, HEAD)
    _x(not ok and any("consumer_name" in e for e in errs), "empty consumer → fail")


def test_consumer_sync_field_mismatch():
    """sync_events.consumer_name diverging from task_run.consumer_name → fail."""
    raw = _raw()
    raw["sync_events"][0]["consumer_name"] = "m5-0-other"
    ok, errs = P.validate_production(raw, HEAD)
    _x(not ok and any("consumer_name" in e for e in errs), "sync consumer mismatch → fail")


def test_consumer_stage_processed_by_mismatch():
    """stage_events.processed_by diverging from task_run.consumer_name → fail."""
    raw = _raw()
    raw["stage_events"][0]["processed_by"] = "m5-0-other"
    ok, errs = P.validate_production(raw, HEAD)
    _x(not ok and any("processed_by" in e or "consumer" in e for e in errs),
       "processed_by mismatch → fail")


def test_consumer_schema_rejects_controller():
    """Schema $defs/consumer_name rejects 'controller' and empty."""
    schema = json.load(open(os.path.join(HERE, "schemas", "production-live.schema.json")))
    import jsonschema
    cn = schema["$defs"]["consumer_name"]
    _x(cn.get("not", {}).get("const") == "controller", "schema forbids const controller")
    _x(cn.get("minLength") == 1, "schema minLength 1")


def test_collector_reads_candidate_consumer():
    """Collector must read CONTROLLER_CONSUMER_NAME from the Candidate container
    (mergepilot-m5-0-candidate), NOT the production controller whose default is
    'controller'. Static check grounded in tools/start-m5-0-candidate.sh."""
    src = open(os.path.join(HERE, "capture_production_live.py"), encoding="utf-8").read()
    _x('CANDIDATE_CONTAINER = "mergepilot-m5-0-candidate"' in src,
       "defines CANDIDATE_CONTAINER = mergepilot-m5-0-candidate")
    _x("env_map(inspections[CANDIDATE_CONTAINER])" in src,
       "reads CONTROLLER_CONSUMER_NAME env from Candidate container")
    _x("mergepilot-m5-0-candidate" in src, "candidate container name present in SOURCE_CONTAINERS")


# ── Fix 4: recomputable provenance digests (real comparison) ──

def test_digest_raw_capture_recompute():
    """raw_capture_sha256 is recomputed from raw-minus-provenance; tampering → fail."""
    raw = _raw()
    ok, errs = P.validate_production(raw, HEAD)
    _x(ok, "consistent raw_capture_sha256 → pass (errs=%s)" % errs)
    raw["task_run"]["verdict"] = "FAIL"  # mutate raw-without-provenance
    ok2, errs2 = P.validate_production(raw, HEAD)
    _x(not ok2 and any("raw_capture_sha256" in e for e in errs2),
       "tampered raw → raw_capture_sha256 mismatch")


def test_digest_command_recompute():
    """collector_command_digest is recomputed from evidence fields; tampering → fail."""
    raw = _raw()
    raw["provenance"]["collector_command_digest"] = "0" * 64  # wrong digest
    ok, errs = P.validate_production(raw, HEAD)
    _x(not ok and any("collector_command_digest" in e for e in errs),
       "wrong command digest → fail")


def test_digest_script_recompute():
    """collector_script_sha256 is recomputed from the collector source file."""
    raw = _raw()
    raw["provenance"]["collector_script_sha256"] = "0" * 64  # wrong digest
    ok, errs = P.validate_production(raw, HEAD)
    _x(not ok and any("collector_script_sha256" in e for e in errs),
       "wrong script digest → fail")


def test_digest_trust_boundary_documented():
    """Trust-boundary digests (external responses) are only format-checked, not
    recomputed; their names must be identifiable in the validator."""
    src = open(os.path.join(HERE, "capture_production_live.py"), encoding="utf-8").read()
    _x("Trust-boundary" in src or "trust-boundary" in src.lower(),
       "validator documents trust-boundary digests")
    for external in ("db_snapshot_sha256", "matrix_sync_sha256", "gateway_audit_sha256"):
        _x(external in src, "trust-boundary digest %s named" % external)


def main():
    for n, fn in sorted(globals().items()):
        if n.startswith("test_") and callable(fn):
            print("=== %s ===" % n); fn()
    print("\nALL UNIT TESTS PASSED")


if __name__ == "__main__":
    main()
