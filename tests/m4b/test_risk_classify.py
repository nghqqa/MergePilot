"""M4-B RiskClassify tests (deterministic, fixed count).

Covers L0/L1/L2 positive samples, only-escalate negatives, fail-closed on
missing/corrupt/unknown-version/duplicate rulesets, deterministic ordering, the
advisory-only contract, recommended-controls mapping and schema validity.
"""
from __future__ import annotations

import json
import os
import tempfile

import jsonschema
import pytest

from skills.common.runtime import errors
from skills.risk_classify import core
from skills.risk_classify import run as rc_run

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ctx(files, complete=True, additions=None, deletions=None):
    """Build a minimal change_context (DiffParse output shape)."""
    add = additions if additions is not None else sum(f["additions"] for f in files)
    dele = deletions if deletions is not None else sum(f["deletions"] for f in files)
    cats = sorted({c for f in files for c in f.get("categories", [])})
    return {
        "schema_version": "1",
        "source": {"repo": "o/r"},
        "input_sha256": "0" * 64,
        "complete": complete,
        "files": files,
        "modules_touched": sorted({f["path"].rsplit("/", 1)[0] if "/" in f["path"] else "."
                                   for f in files}),
        "change_categories": cats,
        "stats": {"files_changed": len(files), "additions": add, "deletions": dele,
                  "hunks": 0, "binary_files": sum(1 for f in files if f.get("binary"))},
    }


def _f(path, categories, change_type="M", additions=1, deletions=0, binary=False):
    return {"path": path, "old_path": None, "change_type": change_type,
            "additions": additions, "deletions": deletions, "binary": binary,
            "mode_changed": False, "categories": categories, "hunks": []}


def _classify(files, floor="L0", complete=True, **kw):
    return core.classify(_ctx(files, complete=complete), risk_floor=floor, **kw)


def _output_validator():
    with open(os.path.join(_REPO_ROOT, "skills", "risk_classify", "schema",
                           "output.schema.json"), encoding="utf-8") as fh:
        return jsonschema.Draft202012Validator(json.load(fh))


# --------------------------------------------------------------------------- #
# Positive level samples
# --------------------------------------------------------------------------- #
def test_01_L0_docs_only_small():
    out = _classify([_f("README.md", ["documentation"], additions=10, deletions=2)])
    assert out["risk_level"] == "L0" and out["risk_rank"] == 0
    assert "DOCS_ONLY_SMALL" in out["matched_rules"]


def test_02_L0_test_only_small():
    out = _classify([_f("tests/test_a.py", ["test"], additions=50)])
    assert out["risk_level"] == "L0"
    assert "TEST_ONLY_SMALL" in out["matched_rules"]


def test_03_L1_source_change():
    out = _classify([_f("src/app.py", ["source"])])
    assert out["risk_level"] == "L1"
    assert "SOURCE_CONFIG_CHANGE" in out["matched_rules"]


def test_04_L1_config_change():
    out = _classify([_f("conf.ini", ["config"])])
    assert out["risk_level"] == "L1"


def test_05_L1_rename():
    out = _classify([_f("src/new.py", ["source"], change_type="R")])
    assert out["risk_level"] == "L1"
    assert "RENAME_OR_COPY" in out["matched_rules"]


def test_06_L1_copy():
    out = _classify([_f("src/copy.py", ["source"], change_type="C")])
    assert out["risk_level"] == "L1"


def test_07_L1_medium_change():
    out = _classify([_f("src/big.py", ["source"], additions=250)])
    assert out["risk_level"] == "L1"
    assert "MEDIUM_CHANGE" in out["matched_rules"]


def test_08_L1_partial_context():
    out = _classify([_f("src/app.py", ["source"])], complete=False)
    assert out["risk_level"] == "L1"
    assert "PARTIAL_CONTEXT" in out["matched_rules"]


def test_09_L1_multi_file():
    files = [_f("src/m%d.py" % i, ["source"]) for i in range(20)]
    out = _classify(files)
    assert out["risk_level"] == "L1"
    assert "MULTI_FILE_CHANGE" in out["matched_rules"]


def test_10_L2_dependency():
    out = _classify([_f("requirements.txt", ["dependency"])])
    assert out["risk_level"] == "L2"
    assert "DEP_MANIFEST" in out["matched_rules"]


def test_11_L2_workflow():
    out = _classify([_f(".github/workflows/ci.yml", ["workflow"])])
    assert out["risk_level"] == "L2"


def test_12_L2_migration():
    out = _classify([_f("migrations/0001.sql", ["migration"])])
    assert out["risk_level"] == "L2"


def test_13_L2_security_sensitive():
    out = _classify([_f("src/auth/login.py", ["source", "security_sensitive"])])
    assert out["risk_level"] == "L2"
    assert "SECURITY_SENSITIVE_PATH" in out["matched_rules"]


def test_14_L2_binary():
    out = _classify([_f("logo.png", ["binary"], binary=True)])
    assert out["risk_level"] == "L2"
    assert "BINARY_FILE" in out["matched_rules"]


def test_15_L2_source_deletion():
    out = _classify([_f("src/legacy.py", ["source", "deletion"], change_type="D")])
    assert out["risk_level"] == "L2"
    assert "SOURCE_DELETION" in out["matched_rules"]


def test_16_doc_deletion_is_L1_not_L2():
    out = _classify([_f("docs/old.md", ["documentation", "deletion"], change_type="D")])
    assert out["risk_level"] == "L1"
    assert "SOURCE_DELETION" not in out["matched_rules"]
    assert "DELETION" in out["matched_rules"]


def test_17_L2_large_change():
    out = _classify([_f("src/big.py", ["source"], additions=1000)])
    assert out["risk_level"] == "L2"
    assert "LARGE_CHANGE" in out["matched_rules"]


# --------------------------------------------------------------------------- #
# Only-escalate (never lower than floor)
# --------------------------------------------------------------------------- #
def test_18_floor_L2_holds_for_any_input():
    out = _classify([_f("README.md", ["documentation"], additions=5)], floor="L2")
    assert out["risk_level"] == "L2"
    assert out["risk_floor"] == "L2"


def test_19_floor_L1_with_L0_rules_holds():
    out = _classify([_f("README.md", ["documentation"], additions=5)], floor="L1")
    assert out["risk_level"] == "L1"


def test_20_low_rule_after_high_does_not_lower():
    # a high-risk rule (dependency=L2) plus an L0 doc rule -> still L2
    files = [_f("requirements.txt", ["dependency"]),
             _f("README.md", ["documentation"], additions=5)]
    out = _classify(files)
    assert out["risk_level"] == "L2"


def test_21_rule_order_does_not_change_level():
    rs = core.load_rules(core.DEFAULT_RULES_PATH)
    cc = _ctx([_f("src/auth/a.py", ["source", "security_sensitive"]),
               _f("requirements.txt", ["dependency"])])
    shuffled = dict(rs)
    shuffled["rules"] = list(reversed(rs["rules"]))
    a = core.classify(cc, ruleset=rs)
    b = core.classify(cc, ruleset=shuffled)
    assert a["risk_level"] == b["risk_level"] == "L2"
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# --------------------------------------------------------------------------- #
# Determinism / ordering
# --------------------------------------------------------------------------- #
def test_22_reasons_sorted_by_level_then_id():
    out = _classify([_f("requirements.txt", ["dependency"]),
                     _f("src/app.py", ["source"])])
    levels = [r["level"] for r in out["reasons"]]
    # L2 reasons come before L1 reasons (sorted by -rank then rule_id)
    assert levels.index("L2") < levels.index("L1")


def test_23_matched_rules_sorted_unique():
    out = _classify([_f("src/auth/a.py", ["source", "security_sensitive"], additions=300)])
    assert out["matched_rules"] == sorted(out["matched_rules"])
    assert len(out["matched_rules"]) == len(set(out["matched_rules"]))


def test_24_determinism_identical_output():
    files = [_f("src/app.py", ["source"], additions=250)]
    a = json.dumps(_classify(files), sort_keys=True)
    b = json.dumps(_classify(files), sort_keys=True)
    assert a == b


# --------------------------------------------------------------------------- #
# Fail-closed on ruleset problems
# --------------------------------------------------------------------------- #
def test_25_missing_rules_file():
    with pytest.raises(core.RiskClassifyError) as ei:
        core.load_rules(os.path.join(tempfile.gettempdir(), "mp_does_not_exist_xyz.json"))
    assert ei.value.code == core.RULES_MISSING


def test_26_corrupt_rules_json():
    bad = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
    bad.write("{ not valid json ")
    bad.close()
    try:
        with pytest.raises(core.RiskClassifyError) as ei:
            core.load_rules(bad.name)
        assert ei.value.code == core.RULESET_INVALID
    finally:
        os.unlink(bad.name)


def test_27_ruleset_missing_required_field():
    bad = {"rules_version": "1.0.0", "rules": [{"rule_id": "X", "level": "L1"}]}  # no summary/match
    with pytest.raises(core.RiskClassifyError) as ei:
        core.classify(_ctx([_f("a.py", ["source"])]), ruleset=bad)
    assert ei.value.code == core.RULESET_INVALID


def test_28_unsupported_rules_major_version():
    bad = {"rules_version": "2.0.0", "rules": [
        {"rule_id": "X", "level": "L1", "summary": "x", "match": {"empty": True}}]}
    # _validate_ruleset passes (shape ok) but version major 2 is unsupported
    with pytest.raises(core.RiskClassifyError) as ei:
        core.classify(_ctx([_f("a.py", ["source"])]), ruleset=bad)
    assert ei.value.code == core.RULESET_VERSION_UNSUPPORTED


def test_29_expected_rules_version_mismatch():
    with pytest.raises(core.RiskClassifyError) as ei:
        core.classify(_ctx([_f("a.py", ["source"])]), expected_rules_version="1.2.3")
    assert ei.value.code == core.RULESET_VERSION_UNSUPPORTED


def test_30_duplicate_rule_id_rejected():
    bad = {"rules_version": "1.0.0", "rules": [
        {"rule_id": "DUP", "level": "L1", "summary": "a", "match": {"empty": True}},
        {"rule_id": "DUP", "level": "L2", "summary": "b", "match": {"empty": True}}]}
    with pytest.raises(core.RiskClassifyError) as ei:
        core.classify(_ctx([_f("a.py", ["source"])]), ruleset=bad)
    assert ei.value.code == core.RULESET_INVALID


# --------------------------------------------------------------------------- #
# Advisory contract / controls / schema
# --------------------------------------------------------------------------- #
def test_31_advisory_only_and_no_authorization_words():
    out = _classify([_f("requirements.txt", ["dependency"])], floor="L2")
    assert out["advisory_only"] is True
    blob = json.dumps(out).lower()
    for forbidden in ("approved", "denied", "merge", "author_trust", "nacos"):
        assert forbidden not in blob


def test_32_recommended_controls_per_level():
    assert _classify([_f("README.md", ["documentation"], additions=5)])["recommended_controls"] == ["AUTO_REVIEW_ELIGIBLE"]
    l1 = _classify([_f("src/a.py", ["source"])])["recommended_controls"]
    assert l1 == ["AUTO_REVIEW_ELIGIBLE", "HUMAN_REVIEW"]
    l2 = _classify([_f("requirements.txt", ["dependency"])])["recommended_controls"]
    assert l2 == ["HUMAN_REVIEW", "L2_APPROVAL_RECOMMENDED"]


def test_33_approval_recommended_only_at_L2():
    assert _classify([_f("README.md", ["documentation"], additions=5)])["approval_recommended"] is False
    assert _classify([_f("src/a.py", ["source"])])["approval_recommended"] is False
    assert _classify([_f("requirements.txt", ["dependency"])])["approval_recommended"] is True


def test_34_output_validates_against_schema():
    out = _classify([_f("src/auth/a.py", ["source", "security_sensitive"], additions=300)])
    _output_validator().validate(out)


def test_35_rules_file_is_meta_valid_and_version_one():
    with open(os.path.join(_REPO_ROOT, "skills", "risk_classify", "schema",
                           "rules.schema.json"), encoding="utf-8") as fh:
        rschema = json.load(fh)
    with open(core.DEFAULT_RULES_PATH, encoding="utf-8") as fh:
        ruleset = json.load(fh)
    jsonschema.Draft202012Validator(rschema).validate(ruleset)
    assert ruleset["rules_version"] == "1.0.0"
    assert len(ruleset["rules"]) >= 10


def test_36_handle_rejects_invalid_change_context():
    with pytest.raises(errors.SkillError) as ei:
        rc_run.handle({"input": {"change_context": {"files": []}}})  # missing complete/stats/change_categories
    assert ei.value.code == core.INVALID_CONTEXT


def test_37_handle_rejects_author_trust_field():
    # input schema is additionalProperties:false; author trust must not be accepted
    with pytest.raises(errors.InvalidInput):
        rc_run.handle({"input": {
            "change_context": _ctx([_f("a.py", ["source"])]),
            "author_trust_level": "high",
        }})


def test_38_handle_ok_classifies():
    res = rc_run.handle({"input": {"change_context": _ctx([_f("requirements.txt", ["dependency"])])}})
    assert res["status"] == "OK"
    assert res["output"]["risk_level"] == "L2"


# --------------------------------------------------------------------------- #
# Audit-driven negatives: empty/unknown category, fail-closed ruleset/context,
# only_categories empty-set, CLI pre-validation redaction.
# --------------------------------------------------------------------------- #
def test_39_empty_category_file_is_L1_not_L0():
    # CODEOWNERS has no recognized category -> must not drop to L0
    out = _classify([_f("CODEOWNERS", [])])
    assert out["risk_level"] == "L1"
    assert "UNCATEGORIZED_CHANGE" in out["matched_rules"]
    assert "DOCS_ONLY_SMALL" not in out["matched_rules"]


def test_40_bad_regex_rule_rejected():
    bad = {"rules_version": "1.0.0", "rules": [
        {"rule_id": "BADRE", "level": "L2", "summary": "x", "match": {"path_pattern": "["}}]}
    with pytest.raises(core.RiskClassifyError) as ei:
        core.classify(_ctx([_f("a.py", ["source"])]), ruleset=bad)
    assert ei.value.code == core.RULESET_INVALID


def test_41_negative_stats_rejected():
    bad = _ctx([_f("requirements.txt", [])])
    bad["stats"]["additions"] = -5
    bad["files"][0]["additions"] = -5
    with pytest.raises(core.RiskClassifyError) as ei:
        core.classify(bad)
    assert ei.value.code == core.INVALID_CONTEXT


def test_42_aggregation_mismatch_rejected():
    bad = _ctx([_f("a.py", ["source"], additions=3)])
    bad["stats"]["additions"] = 99  # != sum(file additions)
    with pytest.raises(core.RiskClassifyError) as ei:
        core.classify(bad)
    assert ei.value.code == core.INVALID_CONTEXT


def test_43_only_categories_requires_nonempty():
    # empty change_categories must not satisfy only_categories (would otherwise
    # match every allowed set and drop real changes to L0)
    rs = core.load_rules(core.DEFAULT_RULES_PATH)
    cc = _ctx([_f("CODEOWNERS", [])])  # change_categories == []
    out = core.classify(cc, ruleset=rs)
    assert out["risk_level"] != "L0"
    assert "DOCS_ONLY_SMALL" not in out["matched_rules"]


def test_44_cli_direct_entry_redacts_credential_request():
    import subprocess
    import sys
    probe = "ghp_" + "a" * 36  # assembled -> source scanner-clean
    req = {"contract_version": "2", "request_id": probe, "trace_id": "tr-1", "input": {}}
    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO_ROOT
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run([sys.executable, "-m", "skills.risk_classify.run"],
                          input=json.dumps(req), capture_output=True, text=True,
                          cwd=_REPO_ROOT, env=env)
    env_out = json.loads(proc.stdout)
    assert probe not in proc.stdout                      # raw credential never emitted
    assert "request_id" in env_out["redactions"]        # redaction applied + recorded
    assert env_out["status"] == "ERROR"


def test_45_false_boolean_predicates_rejected():
    # complete_false/empty/has_uncategorized are trigger predicates; a `false`
    # value is meaningless and must be rejected (const:true), never silently
    # turn an L2 rule into a no-op that drops risk to L0.
    for key in ("complete_false", "empty", "has_uncategorized"):
        bad = {"rules_version": "1.0.0", "rules": [
            {"rule_id": "X", "level": "L2", "summary": "x", "match": {key: False}}]}
        with pytest.raises(core.RiskClassifyError) as ei:
            core.classify(_ctx([_f("a.py", ["source"])]), ruleset=bad)
        assert ei.value.code == core.RULESET_INVALID
