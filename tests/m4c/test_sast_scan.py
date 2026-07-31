"""M4-C SASTScan tests (deterministic, fixed count)."""
from __future__ import annotations

import hashlib
import json
import os

import jsonschema
import pytest

from skills.common.runtime import errors
from skills.sast_scan import core
from skills.sast_scan import run as sr

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _scan(files, **kw):
    return core.scan({"mode": "inline", "files": files}, **kw)


def _output_validator():
    with open(os.path.join(_REPO_ROOT, "skills", "sast_scan", "schema", "output.schema.json"), encoding="utf-8") as fh:
        return jsonschema.Draft202012Validator(json.load(fh))


def _ghp():
    return "ghp_" + "a" * 36  # assembled -> source scanner-clean


# --------------------------------------------------------------------------- #
# positive detection
# --------------------------------------------------------------------------- #
def test_01_secret_detected_and_redacted():
    probe = _ghp()
    out = _scan([{"path": "src/c.py", "content": "API_KEY = '" + probe + "'\n"}])
    assert probe not in json.dumps(out)
    secs = [f for f in out["findings"] if f["engine"] == "secret"]
    assert any(f["rule_id"] == "SECRET_GITHUB_PAT" for f in secs)
    assert all(f["evidence_digest"] and probe not in f["message"] for f in secs)


def test_02_ast_dangerous_calls():
    out = _scan([{"path": "a.py", "content": "eval('x')\nexec('y')\nimport pickle\npickle.load(f)\n"}])
    ids = {f["rule_id"] for f in out["findings"]}
    assert {"AST_DANGEROUS_EVAL", "AST_DANGEROUS_EXEC", "AST_DANGEROUS_PICKLE_LOAD"} <= ids


def test_03_sqli_fstring_and_concat():
    out = _scan([{"path": "a.py", "content":
                  "cur.execute(f'SELECT * FROM t WHERE n={name}')\n"
                  "cur.execute('SELECT * FROM t WHERE n=' + name)\n"}])
    sqli = [f for f in out["findings"] if f["rule_id"] == "AST_SQLI_EXECUTE"]
    assert len(sqli) == 2 and all(f["category"] == "injection" for f in sqli)


def test_04_dep_vuln_match_and_miss():
    out = _scan([{"path": "requirements.txt", "content": "cryptography==37.0.0\nflask==2.0.0\n"}])
    deps = {f["rule_id"] for f in out["findings"] if f["engine"] == "dep_vuln"}
    assert "DEP_CVE_2023_50782" in deps
    assert all("flask" not in f["message"] for f in out["findings"])


def test_05_multifile_categories_and_stats():
    out = _scan([
        {"path": "src/a.py", "content": "eval('x')\n"},
        {"path": "requirements.txt", "content": "requests==2.19.0\n"},
    ])
    cats = {f["category"] for f in out["findings"]}
    assert {"dangerous_call", "dependency"} <= cats
    assert out["stats"]["files_scanned"] == 2
    assert out["stats"]["findings"] == sum(out["stats"]["by_engine"].values())


# --------------------------------------------------------------------------- #
# determinism / dedup / schema
# --------------------------------------------------------------------------- #
def test_06_determinism_byte_identical():
    files = [{"path": "a.py", "content": "eval('x')\ncur.execute(f'q{x}')\n"}]
    a = json.dumps(core.scan({"mode": "inline", "files": files}), sort_keys=True)
    b = json.dumps(core.scan({"mode": "inline", "files": files}), sort_keys=True)
    assert a == b


def test_07_finding_id_and_fingerprint_stable():
    out = _scan([{"path": "a.py", "content": "eval('x')\n"}])
    f = out["findings"][0]
    assert f["finding_id"] == "finding-" + f["fingerprint"][:16]
    assert len(f["fingerprint"]) == 64


def test_08_output_validates_against_schema():
    out = _scan([{"path": "a.py", "content": "eval('x')\n"}])
    _output_validator().validate(out)


def test_09_dep_vuln_meta_and_stale():
    out = _scan([{"path": "requirements.txt", "content": "requests==2.19.0\n"}])
    m = out["dep_vuln_meta"]
    assert m["db_version"] and m["source"] and "pypi" in m["covered_ecosystems"]
    assert m["stale"] is False
    # forced stale -> PARTIAL
    out2 = _scan([{"path": "requirements.txt", "content": "requests==2.19.0\n"}],
                 today=__import__("datetime").date(2030, 1, 1))
    assert out2["complete"] is False and out2["dep_vuln_meta"]["stale"] is True


# --------------------------------------------------------------------------- #
# negative / fail-closed
# --------------------------------------------------------------------------- #
def _bad_ruleset(secret_over=None, ast_over=None):
    _ast_default = [{"rule_id": "A", "kind": "dangerous_call", "targets": ["eval"],
                     "severity": "low", "risk_level": "L0", "remediation": "r"}]
    _sec_default = [{"rule_id": "S", "pattern": "x", "label": "l", "severity": "low",
                     "risk_level": "L0", "remediation": "r"}]
    base = {
        "rules_version": "1.0.0",
        "secret_rules": secret_over if secret_over is not None else _sec_default,
        "ast_rules": ast_over if ast_over is not None else _ast_default,
        "dep_vuln": {"db_version": "v", "source": "s", "covered_ecosystems": ["pypi"],
                     "valid_until": "2027-01-30", "advisories": []},
    }
    return base


def test_10_duplicate_rule_id_rejected():
    bad = _bad_ruleset(secret_over=[
        {"rule_id": "DUP", "pattern": "x", "label": "l", "severity": "low", "risk_level": "L0", "remediation": "r"},
        {"rule_id": "DUP", "pattern": "y", "label": "l", "severity": "low", "risk_level": "L0", "remediation": "r"}])
    with pytest.raises(core.SASTScanError) as ei:
        core.scan({"mode": "inline", "files": []}, ruleset=bad)
    assert ei.value.subcode == core.RULESET_INVALID


def test_11_invalid_regex_rejected():
    bad = _bad_ruleset(secret_over=[
        {"rule_id": "R", "pattern": "(", "label": "l", "severity": "low", "risk_level": "L0", "remediation": "r"}])
    with pytest.raises(core.SASTScanError) as ei:
        core.scan({"mode": "inline", "files": []}, ruleset=bad)
    assert ei.value.subcode == core.RULESET_INVALID


def test_12_ruleset_missing_file():
    with pytest.raises(core.SASTScanError) as ei:
        core.load_ruleset(os.path.join(os.path.dirname(_REPO_ROOT), "no_such_rules.json"))
    assert ei.value.subcode == core.RULESET_INVALID


def test_13_expected_rules_version_mismatch():
    with pytest.raises(core.SASTScanError) as ei:
        core.scan({"mode": "inline", "files": [], "expected_rules_version": "9.9.9"})
    assert ei.value.subcode == core.INPUT_INVALID


def test_14_path_escape_rejected():
    with pytest.raises(core.SASTScanError) as ei:
        _scan([{"path": "../etc/passwd", "content": "x"}])
    assert ei.value.subcode == core.PATH_ESCAPE
    with pytest.raises(core.SASTScanError):
        _scan([{"path": "/etc/passwd", "content": "x"}])


def test_15_input_too_large():
    with pytest.raises(core.SASTScanError) as ei:
        core.scan({"mode": "inline", "files": [{"path": "a", "content": "x" * 100}],
                   "options": {"max_total_bytes": 5}})
    assert ei.value.subcode == core.INPUT_TOO_LARGE


def test_16_findings_truncation_is_partial():
    big = [{"path": "requirements.txt",
            "content": "cryptography==37.0.0\nrequests==2.19.0\nloguru==0.5.3\n"}]
    out_one = core.scan({"mode": "inline", "files": big, "options": {"max_findings": 1}})
    assert out_one["complete"] is False
    assert out_one["stats"]["truncated"] is True
    assert out_one["stats"]["findings_total"] >= 3
    assert len(out_one["findings"]) == 1
    assert out_one["stats"]["truncated_digest"]


def test_17_cli_emits_redacted_envelope():
    import subprocess
    import sys
    probe = _ghp()
    req = {"contract_version": "1", "request_id": "r", "trace_id": "t",
           "input": {"mode": "inline", "files": [{"path": "c.py", "content": "x = '" + probe + "'\n"}]}}
    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO_ROOT
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run([sys.executable, "-m", "skills.sast_scan.run"],
                          input=json.dumps(req), capture_output=True, text=True, cwd=_REPO_ROOT, env=env)
    env_out = json.loads(proc.stdout)
    assert probe not in proc.stdout
    assert env_out["status"] == "OK"
    assert env_out["contract_version"] == "1"
    _output_validator().validate(env_out["output"])


def test_18_handle_partial_status_and_error_mapping():
    # ruleset-error path -> INTERNAL_ERROR via handle
    with pytest.raises(errors.SkillError) as ei:
        sr.handle({"input": {"mode": "inline", "files": [{"path": "../x", "content": "y"}]}})
    assert ei.value.code == errors.INVALID_INPUT  # path escape -> INVALID_INPUT


# --------------------------------------------------------------------------- #
# Hardening negatives: no raw-secret digest; hard limits; UTF-8 bytes; empty scan
# --------------------------------------------------------------------------- #
def test_19_evidence_digest_is_not_raw_secret_hash():
    import hashlib as _h
    probe = _ghp()
    out = _scan([{"path": "c.py", "content": "k='" + probe + "'\n"}])
    sec = [f for f in out["findings"] if f["engine"] == "secret" and f["rule_id"] == "SECRET_GITHUB_PAT"][0]
    assert sec["evidence_digest"] != _h.sha256(probe.encode()).hexdigest()


def test_20_input_digest_is_not_raw_secret_hash():
    import hashlib as _h
    probe = _ghp()
    out = _scan([{"path": "c.py", "content": "k='" + probe + "'\n"}])
    assert out["input_digest"] != _h.sha256(("c.py" + probe).encode()).hexdigest()
    assert out["input_digest"] != _h.sha256(probe.encode()).hexdigest()


def test_21_option_above_hard_limit_rejected():
    # request may only LOWER limits; exceeding hard limit -> INVALID_INPUT
    with pytest.raises(core.SASTScanError) as ei:
        core.scan({"mode": "inline", "files": [{"path": "a", "content": "x"}],
                   "options": {"max_findings": 99999}})
    assert ei.value.subcode == core.INPUT_INVALID
    with pytest.raises(core.SASTScanError):
        core.scan({"mode": "inline", "files": [{"path": "a", "content": "x"}],
                   "options": {"max_bytes_per_file": 999999}})


def test_22_utf8_byte_limit_not_char_count():
    # emoji is 4 UTF-8 bytes per char; a 60-char emoji string = 240 bytes > 100-byte cap
    with pytest.raises(core.SASTScanError) as ei:
        core.scan({"mode": "inline", "files": [{"path": "a", "content": "\U0001f600" * 60}],
                   "options": {"max_bytes_per_file": 100}})
    assert ei.value.subcode == core.INPUT_TOO_LARGE


def test_23_empty_scan_not_complete():
    # zero files -> never complete (defensive; schema also requires minItems=1)
    out = core.scan({"mode": "inline", "files": []})
    assert out["complete"] is False
    assert out["stats"]["files_scanned"] == 0


def test_24_single_file_over_limit_errors_not_truncates():
    with pytest.raises(core.SASTScanError) as ei:
        core.scan({"mode": "inline", "files": [{"path": "a", "content": "A" * 100}],
                   "options": {"max_bytes_per_file": 50}})
    assert ei.value.subcode == core.INPUT_TOO_LARGE


def test_25_duplicate_path_rejected():
    with pytest.raises(core.SASTScanError) as ei:
        core.scan({"mode": "inline", "files": [{"path": "a.py", "content": "x"}, {"path": "a.py", "content": "y"}]})
    assert ei.value.subcode == core.INPUT_INVALID


def test_26_dep_only_exact_pin():
    # >= / ~= / > must NOT be treated as the vulnerable pinned version
    out = _scan([{"path": "requirements.txt", "content": "cryptography>=37.0.0\ncryptography~=37.0.0\n"}])
    assert len(out["findings"]) == 0


def test_27_paths_mode_total_bytes_accumulate(tmp_path, monkeypatch):
    import os
    ws = tmp_path / "ws"; ws.mkdir()
    (ws / "f1").write_text("x" * 60, encoding="utf-8")
    (ws / "f2").write_text("y" * 60, encoding="utf-8")
    # total 120 bytes > 100 -> INPUT_TOO_LARGE (paths mode also accumulates)
    with pytest.raises(core.SASTScanError) as ei:
        core.scan({"mode": "paths", "paths": ["f1", "f2"], "options": {"max_total_bytes": 100}},
                  trusted_workspace=str(ws))
    assert ei.value.subcode == core.INPUT_TOO_LARGE


# --------------------------------------------------------------------------- #
# Round-2 hardening: manifest detection, unknown major version, cross-ecosystem,
# non-empty rules
# --------------------------------------------------------------------------- #
def test_28_dev_requirements_detected_as_manifest():
    out = _scan([{"path": "dev-requirements.txt", "content": "cryptography==37.0.0\n"}])
    assert any(f["engine"] == "dep_vuln" for f in out["findings"])


def test_29_unknown_major_version_injected_rejected():
    bad = _bad_ruleset()
    bad["rules_version"] = "2.0.0"
    with pytest.raises(core.SASTScanError) as ei:
        core.scan({"mode": "inline", "files": [{"path": "a.py", "content": "x"}]}, ruleset=bad)
    assert ei.value.subcode == core.RULESET_VERSION_UNSUPPORTED


def test_30_cross_ecosystem_no_false_positive():
    # an npm advisory for "requests" must NOT match a pypi requirements.txt line
    bad = _bad_ruleset()
    bad["dep_vuln"]["advisories"] = [
        {"package": "requests", "ecosystem": "npm", "version": "2.19.0", "id": "X",
         "severity": "high", "fixed_version": "2.20.0", "advisory": "npm only"}]
    out = core.scan({"mode": "inline", "files": [{"path": "requirements.txt", "content": "requests==2.19.0\n"}]}, ruleset=bad)
    assert not any(f["engine"] == "dep_vuln" for f in out["findings"])


def test_31_empty_secret_rules_rejected():
    bad = _bad_ruleset(secret_over=[])  # empty -> fail-closed
    with pytest.raises(core.SASTScanError) as ei:
        core.scan({"mode": "inline", "files": [{"path": "a.py", "content": "x"}]}, ruleset=bad)
    assert ei.value.subcode == core.RULESET_INVALID


def test_32_duplicate_advisory_rejected():
    bad = _bad_ruleset()
    bad["dep_vuln"]["advisories"] = [
        {"package": "x", "ecosystem": "pypi", "version": "1.0", "id": "CVE-1",
         "severity": "high", "fixed_version": "2.0", "advisory": "a"},
        {"package": "x", "ecosystem": "pypi", "version": "1.0", "id": "CVE-1",
         "severity": "high", "fixed_version": "2.0", "advisory": "a"}]
    with pytest.raises(core.SASTScanError) as ei:
        core.scan({"mode": "inline", "files": []}, ruleset=bad)
    assert ei.value.subcode == core.RULESET_INVALID


# --------------------------------------------------------------------------- #
# Round-4: SAST trusted-root symlink rejection
# --------------------------------------------------------------------------- #
def test_33_sast_root_symlink_rejected(tmp_path):
    real = tmp_path / "real"; real.mkdir(); (real / "f.py").write_text("x=1\n", encoding="utf-8")
    link = tmp_path / "link"
    try:
        import os as _os; _os.symlink(str(real), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported")
    with pytest.raises(core.SASTScanError) as ei:
        core.scan({"mode": "paths", "paths": ["f.py"]}, trusted_workspace=str(link))
    assert ei.value.subcode == core.PATH_ESCAPE
