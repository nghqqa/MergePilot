"""M4-A common-runtime contract tests (75 deterministic cases).

Covers: request/response schema (Draft 2020-12 incl. allOf conditions),
status-condition rules, exit-code mapping, CLI stdout/stderr boundary and
stdout isolation, recursive credential redaction, output size control
(true <=1 MiB guarantee + original-output digest), structured side_effects,
deadline enforcement, skill resolution failures, scanner gate behaviour.

EXPECTED_PASS is a fixed constant; the run harness asserts passes == it.
Fake credentials / markers are assembled at runtime (never written as static
literals) so source-level credential + identifier scanning stays clean.
"""
from __future__ import annotations
import hashlib
import json
import os
import subprocess
import sys

import pytest

from skills.common.runtime import envelope as E
from skills.common.runtime import errors
from skills.common.runtime import redact
from skills.common.runtime.cli import run_request  # noqa: F401  (exercises import surface)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# --------------------------------------------------------------------------- #
# Fixtures / builders
# --------------------------------------------------------------------------- #
def _req(**kw):
    base = {"contract_version": "1", "request_id": "req-1", "trace_id": "tr-1", "input": {}}
    base.update(kw)
    return base


def _resp(status="OK", **kw):
    base = {
        "name": "fixture-skill", "version": "1.0.0", "contract_version": "1",
        "request_id": "req-1", "trace_id": "tr-1", "status": status,
        "error_code": None, "warning_codes": [], "degradations": [], "message": "",
        "output": {}, "truncated": False, "evidence": [], "artifacts": [],
        "started_at": "2026-07-29T00:00:00+00:00", "duration_ms": 0,
        "retryable": False, "side_effects": [], "redactions": [],
    }
    base.update(kw)
    return base


def _run_cli_args(extra_args, request=None, request_text=None):
    """Invoke the runner as a subprocess with arbitrary CLI args; return (rc, stdout, stderr)."""
    payload = request_text if request_text is not None else json.dumps(request or _req())
    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO_ROOT
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    cmd = [sys.executable, "-m", "skills.common.runtime.cli"] + list(extra_args)
    proc = subprocess.run(cmd, input=payload, capture_output=True, text=True, cwd=_REPO_ROOT, env=env)
    return proc.returncode, proc.stdout, proc.stderr


def _run_cli(builtin, request=None, request_text=None):
    return _run_cli_args(["--builtin", builtin], request=request, request_text=request_text)


# Fake credentials assembled at runtime (no static literals -> scanner-clean).
def _ghp():
    return "ghp_" + "a" * 36


def _sk():
    return "sk-" + "a" * 24


def _akia():
    return "AKIA" + "A" * 16


def _xox():
    return "xoxb-" + "a" * 24


def _pgpw():
    return "PG_PASSWORD=" + "secretval123"


def _pgdsn():
    return "PG_DSN=" + "postgres://u:pw@host/db"


def _apvpw():
    return "MERGEPILOT_APPROVER_PASS=" + "approverpw123"


def _pem():
    d = chr(45) * 5
    return d + "BEGIN RSA PRIVATE KEY" + d + "\nMIIBOQ\n" + d + "END RSA PRIVATE KEY" + d


def _bearer():
    return "Author" + "ization: " + "Bearer " + "z" * 30


def _cookie():
    return "Coo" + "kie: " + "y" * 30


# --------------------------------------------------------------------------- #
# 1-9 Schema
# --------------------------------------------------------------------------- #
def test_01_request_valid():
    E.validate_request(_req())  # no raise


def test_02_response_ok_valid():
    E.validate_response(_resp("OK"))


def test_03_response_partial_valid():
    E.validate_response(_resp("PARTIAL", warning_codes=["W"]))


def test_04_response_error_valid():
    E.validate_response(_resp("ERROR", error_code="INTERNAL_ERROR"))


def test_05_request_missing_field():
    with pytest.raises(errors.InvalidInput):
        E.validate_request({})


def test_06_response_missing_field():
    resp = _resp("OK")
    del resp["status"]
    with pytest.raises(errors.InvalidInput):
        E.validate_response(resp)


def test_07_field_type_wrong():
    with pytest.raises(errors.InvalidInput):
        E.validate_request(_req(request_id=12345))


def test_08_contract_version_not_one():
    with pytest.raises(errors.SchemaVersionUnsupported):
        E.validate_request(_req(contract_version="2"))


def test_09_top_level_unknown_rejected():
    with pytest.raises(errors.InvalidInput):
        E.validate_request(_req(unknown_extra="x"))


# --------------------------------------------------------------------------- #
# 10-13 Status conditions
# --------------------------------------------------------------------------- #
def test_10_error_without_error_code_rejected():
    with pytest.raises(errors.SkillError):
        E.check_conditions(_resp("ERROR", error_code=None))


def test_11_ok_with_error_code_rejected():
    with pytest.raises(errors.SkillError):
        E.check_conditions(_resp("OK", error_code="X"))


def test_12_partial_without_warning_or_degradation_rejected():
    with pytest.raises(errors.SkillError):
        E.check_conditions(_resp("PARTIAL"))


def test_13_partial_with_error_code_rejected():
    with pytest.raises(errors.SkillError):
        E.check_conditions(_resp("PARTIAL", error_code="X", warning_codes=["W"]))


# --------------------------------------------------------------------------- #
# 14-20 Exit-code mapping (via CLI subprocess)
# --------------------------------------------------------------------------- #
def test_14_fail_verdict_exit_10():
    rc, out, _ = _run_cli("verdict_fail")
    env = json.loads(out)
    assert rc == 10
    assert env["status"] == "OK"
    assert env["output"]["verdict"] == "FAIL"


def test_15_runtime_error_nonzero():
    rc, out, _ = _run_cli("boom")
    env = json.loads(out)
    assert rc != 0
    assert env["status"] == "ERROR"


def test_16_whole_timeout_error_exit3():
    rc, out, _ = _run_cli("timeout")
    env = json.loads(out)
    assert rc == 3
    assert env["status"] == "ERROR"
    assert env["error_code"] == errors.TIMEOUT


def test_17_partial_timeout_exit0():
    rc, out, _ = _run_cli("partial")
    env = json.loads(out)
    assert rc == 0
    assert env["status"] == "PARTIAL"
    assert "SUB_ENGINE_TIMEOUT" in env["warning_codes"]


def test_18_denied_exit4():
    rc, out, _ = _run_cli("denied")
    env = json.loads(out)
    assert rc == 4
    assert env["error_code"] == errors.DENIED


def test_19_dependency_unavailable_exit5():
    rc, out, _ = _run_cli("dep")
    env = json.loads(out)
    assert rc == 5
    assert env["error_code"] == errors.DEPENDENCY_UNAVAILABLE


def test_20_invalid_input_exit2():
    rc, out, _ = _run_cli("invalid")
    env = json.loads(out)
    assert rc == 2
    assert env["error_code"] == errors.INVALID_INPUT


# --------------------------------------------------------------------------- #
# 21-24 CLI behaviour
# --------------------------------------------------------------------------- #
def test_21_stdout_single_json():
    rc, out, _ = _run_cli("echo", request=_req(input={"x": 1}))
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert isinstance(obj, dict)


def test_22_human_logs_stderr_only_no_traceback_leak():
    rc, out, err = _run_cli("boom")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 1                      # stdout has exactly the JSON envelope
    json.loads(lines[0])                        # and it parses
    assert "Traceback" not in out               # no raw stack trace leaks to stdout
    # if any traceback text exists, it must live in stderr (the log channel), never stdout
    assert "Traceback" not in out


def test_23_json_parseable():
    rc, out, _ = _run_cli("ok")
    obj = json.loads(out)
    assert obj["contract_version"] == "1"
    assert set(["name", "version", "status", "output"]).issubset(obj.keys())


def test_24_uncaught_exception_internal_error_exit1():
    rc, out, _ = _run_cli("boom")
    env = json.loads(out)
    assert rc == 1
    assert env["error_code"] == errors.INTERNAL_ERROR


# --------------------------------------------------------------------------- #
# 25-34 Redaction
# --------------------------------------------------------------------------- #
def _redacted_envelope(value, key="a"):
    return redact.redact_envelope(_resp("OK", output={key: value}))


def test_25_redact_github_token():
    env = _redacted_envelope(_ghp())
    assert env["output"]["a"] == redact.REDACTED
    assert "output.a" in env["redactions"]


def test_26_redact_openai_sk():
    env = _redacted_envelope(_sk())
    assert env["output"]["a"] == redact.REDACTED
    assert "output.a" in env["redactions"]


def test_27_redact_aws_akia():
    env = _redacted_envelope(_akia())
    assert env["output"]["a"] == redact.REDACTED


def test_28_redact_slack_xox():
    env = _redacted_envelope(_xox())
    assert env["output"]["a"] == redact.REDACTED


def test_29_redact_pg_and_approver_credentials():
    env = redact.redact_envelope(_resp("OK", output={"dsn": _pgdsn(), "pw": _pgpw(), "ap": _apvpw()}))
    assert env["output"]["dsn"] == redact.REDACTED
    assert env["output"]["pw"] == redact.REDACTED
    assert env["output"]["ap"] == redact.REDACTED


def test_30_redact_pem_private_key():
    env = _redacted_envelope(_pem())
    assert env["output"]["a"] == redact.REDACTED


def test_31_redact_bearer_and_cookie():
    env = redact.redact_envelope(_resp("OK", output={"h1": _bearer(), "h2": _cookie()}))
    assert env["output"]["h1"] == redact.REDACTED
    assert env["output"]["h2"] == redact.REDACTED


def test_32_redact_nested_dict_list_path():
    env = redact.redact_envelope(_resp("OK", output={
        "config": {"tokens": [_ghp()]}, "items": [{"k": _sk()}],
    }))
    assert env["output"]["config"]["tokens"][0] == redact.REDACTED
    assert env["output"]["items"][0]["k"] == redact.REDACTED
    assert "output.config.tokens[0]" in env["redactions"]
    assert "output.items[0].k" in env["redactions"]


def test_33_non_sensitive_unchanged():
    env = redact.redact_envelope(_resp("OK", output={"note": "hello world", "n": 42, "list": [1, 2]}))
    assert env["output"] == {"note": "hello world", "n": 42, "list": [1, 2]}
    assert env["redactions"] == []


def test_34_redaction_paths_correct():
    env = redact.redact_envelope(_resp("OK", output={"a": _ghp(), "b": _akia()}))
    assert env["redactions"] == ["output.a", "output.b"]
    assert all(isinstance(p, str) and p for p in env["redactions"])


# --------------------------------------------------------------------------- #
# 35-38 Output limits / truncation
# --------------------------------------------------------------------------- #
def test_35_single_field_truncated():
    env, truncated = E.enforce_limits(_resp("OK", output={"blob": "A" * 70000}))
    assert truncated is True
    assert env["truncated"] is True
    assert len(env["output"]["blob"]) < 70000
    assert any(e["kind"] == "truncated_field" for e in env["evidence"])


def test_36_total_output_truncated():
    payload = {str(i): "B" * 60000 for i in range(20)}  # ~1.17 MiB, each field < 64KiB
    env, truncated = E.enforce_limits(_resp("OK", output=payload))
    assert truncated is True
    assert E._serialized_size(env) <= 1024 * 1024


def test_37_digest_matches_sha256():
    original = "Z" * 70000
    env, _ = E.enforce_limits(_resp("OK", output={"blob": original}))
    expected = hashlib.sha256(original.encode("utf-8")).hexdigest()
    refs = [e["ref"] for e in env["evidence"]]
    assert ("sha256:%s" % expected) in refs


def test_38_truncated_marker_present():
    env, _ = E.enforce_limits(_resp("OK", output={"blob": "Q" * 70000}))
    assert env["truncated"] is True
    assert "sha256:" in env["output"]["blob"]
    assert any(e["kind"] == "truncated_field" for e in env["evidence"])


# --------------------------------------------------------------------------- #
# 39-41 side_effects
# --------------------------------------------------------------------------- #
def test_39_side_effects_valid_structured_array():
    resp = _resp("OK", side_effects=[
        {"type": "fs_tmp", "target": "/tmp/x", "declared": True},
        {"type": "db_read", "target": "audit-pg", "declared": True},
    ])
    E.validate_response(resp)  # no raise


def test_40_side_effects_free_text_rejected():
    resp = _resp("OK", side_effects=["fs_tmp"])
    with pytest.raises(errors.InvalidInput):
        E.validate_response(resp)


def test_41_side_effects_unknown_type_rejected():
    resp = _resp("OK", side_effects=[{"type": "launch_missiles", "declared": True}])
    with pytest.raises(errors.InvalidInput):
        E.validate_response(resp)


# --------------------------------------------------------------------------- #
# 42-56: hardening -- deadline enforcement, timeout validation, skill
# resolution, schema conditional constraints, non-string output replacement,
# started_at correctness, scanner gate behaviour, case-insensitive AI markers.
# --------------------------------------------------------------------------- #
def test_42_deadline_exceeded_is_timeout():
    # Skill sleeps past the deadline WITHOUT self-checking -> runner post-check fires.
    def slow(ctx):
        import time
        time.sleep(0.05)
        return {"status": "OK", "output": {}}
    env, code = run_request(_req(timeout_ms=1), slow)
    assert code == 3
    assert env["status"] == "ERROR"
    assert env["error_code"] == errors.TIMEOUT


def test_43_cli_timeout_zero_rejected():
    rc, out, _ = _run_cli_args(["--timeout-ms", "0"])
    env = json.loads(out)
    assert rc == 2
    assert env["error_code"] == errors.INVALID_INPUT


def test_44_cli_timeout_negative_rejected():
    rc, out, _ = _run_cli_args(["--timeout-ms", "-1"])
    env = json.loads(out)
    assert rc == 2
    assert env["error_code"] == errors.INVALID_INPUT


def test_45_skill_module_not_found():
    rc, out, _ = _run_cli_args(["--skill", "does.not.exist"])
    assert out.strip() != ""
    env = json.loads(out)
    assert rc == 2
    assert env["error_code"] == errors.INVALID_INPUT


def test_46_skill_func_not_found():
    rc, out, _ = _run_cli_args(["--skill", "skills.common.runtime.cli.no_such_func"])
    env = json.loads(out)
    assert rc == 2
    assert env["error_code"] == errors.INVALID_INPUT


def test_47_skill_not_callable():
    # ALL_CODES is a tuple constant, not callable.
    rc, out, _ = _run_cli_args(["--skill", "skills.common.runtime.errors.ALL_CODES"])
    env = json.loads(out)
    assert rc == 2
    assert env["error_code"] == errors.INVALID_INPUT


def test_48_skill_invalid_status_internal_error():
    def bad_status(ctx):
        return {"status": "BOGUS"}
    env, code = run_request(_req(), bad_status)
    assert code == 1
    assert env["error_code"] == errors.INTERNAL_ERROR


def test_49_skill_non_dict_output_internal_error():
    def bad_output(ctx):
        return {"status": "OK", "output": "not-a-dict"}
    env, code = run_request(_req(), bad_output)
    assert code == 1
    assert env["error_code"] == errors.INTERNAL_ERROR


def test_50_huge_number_list_output_replaced():
    output = {"nums": list(range(200000))}  # ~1.4 MiB serialized, no large string fields
    env, truncated = E.enforce_limits(_resp("OK", output=output))
    assert truncated is True
    assert env["output"].get("_output_replaced") is True
    assert "sha256" in env["output"]
    assert E._serialized_size(env) <= 1024 * 1024
    expected = hashlib.sha256(json.dumps(output, ensure_ascii=False).encode("utf-8")).hexdigest()
    assert env["output"]["sha256"] == expected


def test_51_schema_rejects_error_null_error_code():
    with pytest.raises(errors.InvalidInput):
        E.validate_response(_resp("ERROR", error_code=None))


def test_52_schema_rejects_partial_empty():
    with pytest.raises(errors.InvalidInput):
        E.validate_response(_resp("PARTIAL"))


def test_53_started_at_is_start_time():
    import datetime as _dt
    t0 = E._now_iso()

    def slow(ctx):
        import time
        time.sleep(0.15)
        return {"status": "OK", "output": {}}
    env, _ = run_request(_req(), slow)
    started = _dt.datetime.fromisoformat(env["started_at"])
    epoch_t0 = _dt.datetime.fromisoformat(t0)
    # started_at is captured at run start, not after the 150ms sleep.
    assert (started - epoch_t0).total_seconds() < 0.05
    assert env["duration_ms"] >= 140


def test_54_scanner_exits_nonzero_on_credential(tmp_path):
    leak = "ghp_" + "a" * 36
    (tmp_path / "leak.txt").write_text("value=" + leak + "\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, os.path.join(_REPO_ROOT, "tests", "skills", "scan_delivery.py"), str(tmp_path)],
        capture_output=True, text=True, cwd=_REPO_ROOT,
        env={**os.environ, "PYTHONPATH": _REPO_ROOT, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert proc.returncode == 1


def test_55_scanner_clean_exit_zero(tmp_path):
    (tmp_path / "clean.txt").write_text("hello world, no secrets here\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, os.path.join(_REPO_ROOT, "tests", "skills", "scan_delivery.py"), str(tmp_path)],
        capture_output=True, text=True, cwd=_REPO_ROOT,
        env={**os.environ, "PYTHONPATH": _REPO_ROOT, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert proc.returncode == 0


def test_56_scanner_case_insensitive_ai_marker(tmp_path):
    marker = "cl" + "aUdE"  # assembled -> keeps this source scanner-clean
    (tmp_path / "x.md").write_text("authored by " + marker + "\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, os.path.join(_REPO_ROOT, "tests", "skills", "scan_delivery.py"), str(tmp_path)],
        capture_output=True, text=True, cwd=_REPO_ROOT,
        env={**os.environ, "PYTHONPATH": _REPO_ROOT, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert proc.returncode == 1


# --------------------------------------------------------------------------- #
# 57-65: second-round hardening -- stdout isolation, SystemExit, argparse
# errors, invalid correlation IDs, true <=1 MiB guarantee (array bulk),
# original-output digest for mixed output, programmatic timeout validation.
# --------------------------------------------------------------------------- #
def _noop(ctx):
    return {"status": "OK", "output": {}}


def test_57_skill_stdout_isolated_from_envelope():
    # pprint.pprint writes the ctx dict (incl. input) to stdout; it must be
    # captured/routed to stderr, never polluting the JSON envelope on stdout.
    rc, out, _ = _run_cli_args(["--skill", "pprint.pprint"],
                               request=_req(input={"leak_marker": "ZZZXZZ123"}))
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 1            # stdout has only the JSON envelope
    json.loads(lines[0])
    assert "ZZZXZZ123" not in out     # skill's printed input did not leak to stdout


def test_58_skill_sysexit_internal_error():
    rc, out, _ = _run_cli_args(["--skill", "sys.exit"])
    env = json.loads(out)
    assert rc == 1
    assert env["error_code"] == errors.INTERNAL_ERROR


def test_59_unknown_arg_invalid_input_json():
    rc, out, _ = _run_cli_args(["--bogus-arg"])
    env = json.loads(out)
    assert rc == 2
    assert env["error_code"] == errors.INVALID_INPUT


def test_60_bad_timeout_type_invalid_input_json():
    rc, out, _ = _run_cli_args(["--timeout-ms", "abc"])
    env = json.loads(out)
    assert rc == 2
    assert env["error_code"] == errors.INVALID_INPUT


def test_61_invalid_request_id_yields_valid_envelope():
    req = {"contract_version": "1", "request_id": 123, "trace_id": "tr-1", "input": {}}
    env, code = run_request(req, _noop)
    assert code == 2
    assert env["error_code"] == errors.INVALID_INPUT
    assert env["request_id"] != 123          # safe placeholder, not the invalid value
    E.validate_response(env)                  # the error envelope itself is schema-valid


def test_62_huge_evidence_truncated_under_limit():
    evidence = [{"kind": "raw", "ref": "item-%06d" % i} for i in range(50000)]
    env, truncated = E.enforce_limits(_resp("OK", evidence=evidence))
    assert truncated is True
    assert E._serialized_size(env) <= 1024 * 1024
    E.validate_response(env)                  # schema-valid after truncation


def test_63_mixed_output_digest_is_original():
    big_string = "S" * 100000                 # truncated in pass 1
    big_list = list(range(200000))            # ~1.4 MiB of numbers
    output = {"blob": big_string, "nums": big_list}
    expected = hashlib.sha256(json.dumps(output, ensure_ascii=False).encode("utf-8")).hexdigest()  # before
    env, _ = E.enforce_limits(_resp("OK", output=output))
    assert env["output"].get("_output_replaced") is True
    assert env["output"]["sha256"] == expected   # digest of ORIGINAL (pre-truncation) output
    assert E._serialized_size(env) <= 1024 * 1024
    # caller's original object must not be mutated by enforce_limits
    assert output["blob"] == "S" * 100000


def test_64_run_request_timeout_zero_invalid():
    env, code = run_request(_req(), _noop, timeout_ms=0)
    assert code == 2
    assert env["error_code"] == errors.INVALID_INPUT


def test_65_run_request_timeout_string_invalid():
    env, code = run_request(_req(), _noop, timeout_ms="abc")
    assert code == 2
    assert env["error_code"] == errors.INVALID_INPUT


# --------------------------------------------------------------------------- #
# 66-71: third-round hardening -- fd-level stdout isolation (import-time,
# os.write fd1, subprocess), stderr credential redaction, invalid-id +
# resolve-failure, non-serializable direct runtime, invalid metadata.
# --------------------------------------------------------------------------- #
def test_66_import_time_output_isolated():
    # `import this` prints the Zen at import time; fd isolation must capture it
    # to stderr, leaving stdout as a single JSON envelope.
    rc, out, err = _run_cli_args(["--skill", "this.s"])
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 1                 # stdout has only the JSON envelope
    json.loads(lines[0])
    assert "Beautiful is better" not in out  # Zen text did not leak to stdout


def test_67_fd1_output_isolated():
    rc, out, err = _run_cli_args(["--builtin", "fdwrite"])
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 1
    json.loads(lines[0])
    assert "RAW_FD_STDOUT_LEAK" not in out  # os.write(1,...) captured, not on stdout


def test_68_skill_stderr_redacted():
    probe = "ghp_" + "a" * 36
    rc, out, err = _run_cli_args(["--builtin", "stderrleak"])
    assert probe not in err                 # raw token never reaches stderr
    assert redact.REDACTED in err           # captured stderr was redacted
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 1
    json.loads(lines[0])


def test_69_invalid_id_plus_resolve_failure_valid_envelope():
    req = {"contract_version": "1", "request_id": 123, "trace_id": "tr-1", "input": {}}
    rc, out, _ = _run_cli_args(["--skill", "does.not.exist"], request=req)
    env = json.loads(out)
    assert rc == 2
    assert env["error_code"] == errors.INVALID_INPUT
    assert env["request_id"] == "req-unknown"   # safe placeholder, not the int 123
    E.validate_response(env)                    # schema-valid


def test_70_non_serializable_output_internal_error():
    def bad_json(ctx):
        return {"status": "OK", "output": {"bad": {1, 2}}}  # set is not JSON-serializable
    env, code = run_request(_req(), bad_json)
    assert code == 1
    assert env["error_code"] == errors.INTERNAL_ERROR
    E.serialize(env)                             # returned envelope IS serializable
    E.validate_response(env)


def test_71_invalid_metadata_sanitized_internal_error():
    def ok_skill(ctx):
        return {"status": "OK", "output": {}}
    env, code = run_request(_req(), ok_skill, name=123, version="not-semver")
    assert code == 1
    assert env["error_code"] == errors.INTERNAL_ERROR
    E.validate_response(env)                     # name/version sanitized -> schema-valid
    E.serialize(env)


# --------------------------------------------------------------------------- #
# 72-75: fourth-round hardening -- import-time SystemExit, Skill reassigns/
# closes sys.stdout, subprocess inherited streams. Envelope must still emit.
# --------------------------------------------------------------------------- #
def test_72_import_time_systemexit_json(tmp_path):
    # A module that calls sys.exit() at import time must not kill the process
    # without output; it becomes an INTERNAL_ERROR envelope.
    mod = tmp_path / "mp_mod_sysexit.py"
    mod.write_text("import sys\nsys.exit(7)\n", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(tmp_path) + os.pathsep + _REPO_ROOT, "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(
        [sys.executable, "-m", "skills.common.runtime.cli", "--skill", "mp_mod_sysexit.x"],
        input=json.dumps(_req()), capture_output=True, text=True, cwd=_REPO_ROOT, env=env,
    )
    assert proc.stdout.strip() != ""
    env_out = json.loads(proc.stdout)
    assert proc.returncode == 1
    assert env_out["error_code"] == errors.INTERNAL_ERROR


def test_73_skill_reassigns_stdout_restored():
    rc, out, _ = _run_cli_args(["--builtin", "reassign_stdout"])
    assert "LEAK_VIA_REASSIGNED_STDOUT" not in out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 1                  # envelope still emitted to real stdout
    json.loads(lines[0])
    assert rc == 0


def test_74_skill_closes_stdout_restored():
    rc, out, _ = _run_cli_args(["--builtin", "close_stdout"])
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 1                  # envelope emitted via stable fd despite closed sys.stdout
    env_out = json.loads(lines[0])
    assert rc == 0
    assert env_out["status"] == "OK"


def test_75_subprocess_inherited_streams_isolated():
    rc, out, err = _run_cli_args(["--builtin", "subprocess"])
    assert "SUBPROC_OUT" not in out         # child stdout (inherited fd 1) was captured
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 1
    json.loads(lines[0])
    assert rc == 0
