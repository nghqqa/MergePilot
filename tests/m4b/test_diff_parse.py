"""M4-B DiffParse tests (deterministic, fixed count).

Exercises the framework-neutral core and the common-runtime entry against real
git-diff fixtures and hand-crafted edge cases: normal/edge/malformed/oversized,
binary, rename/copy, git-quoted paths, multi-hunk, statistics self-consistency,
CRLF, empty diff, prompt-injection inertness, secret non-leakage, determinism
and schema validation.
"""
from __future__ import annotations

import hashlib
import json
import os

import jsonschema
import pytest

from skills.common.runtime import envelope as E
from skills.common.runtime import errors
from skills.diff_parse import core
from skills.diff_parse import run as dp_run

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_FIX = os.path.join(_REPO_ROOT, "tests", "m4b", "fixtures")

SHA = "0" * 40
HEAD = "f" * 40


def _fixture(name):
    with open(os.path.join(_FIX, name), encoding="utf-8") as fh:
        return fh.read()


def _manifest():
    with open(os.path.join(_FIX, "fixtures-manifest.json"), encoding="utf-8") as fh:
        return json.load(fh)["fixtures"]


def _parse(text, **kw):
    return core.parse_diff(repo="o/r", base_sha=SHA, head_sha=HEAD,
                           diff_text=text, diff_format="unified", **kw)


def _output_validator():
    with open(os.path.join(_REPO_ROOT, "skills", "diff_parse", "schema",
                           "output.schema.json"), encoding="utf-8") as fh:
        return jsonschema.Draft202012Validator(json.load(fh))


# --------------------------------------------------------------------------- #
# Real git-diff fixtures
# --------------------------------------------------------------------------- #
def test_01_real_modified_fixture():
    out = _parse(_fixture("real-modified.diff"))
    f = out["files"][0]
    assert f["change_type"] == "M" and f["path"] == "src/app.py"
    assert f["additions"] == 1 and f["deletions"] == 1
    assert out["complete"] is True
    assert out["input_sha256"] == _manifest()["real-modified.diff"]["sha256"]


def test_02_real_new_file_fixture():
    out = _parse(_fixture("real-new-file.diff"))
    f = out["files"][0]
    assert f["change_type"] == "A" and f["path"] == "src/util.py"
    assert f["additions"] == 2 and f["deletions"] == 0


def test_03_real_deleted_fixture():
    out = _parse(_fixture("real-deleted.diff"))
    f = out["files"][0]
    assert f["change_type"] == "D" and f["path"] == "src/legacy.py"
    assert f["additions"] == 0 and f["deletions"] == 4
    assert "deletion" in f["categories"]


def test_04_real_rename_fixture():
    out = _parse(_fixture("real-rename.diff"))
    f = out["files"][0]
    assert f["change_type"] == "R" and f["path"] == "src/util.py"
    assert f["old_path"] == "src/helpers.py"
    assert out["input_sha256"] == _manifest()["real-rename.diff"]["sha256"]


def test_05_all_fixture_sha256_match_manifest():
    man = _manifest()
    for name, info in man.items():
        data = open(os.path.join(_FIX, name), "rb").read()
        assert hashlib.sha256(data).hexdigest() == info["sha256"], name


# --------------------------------------------------------------------------- #
# Structure / aggregation
# --------------------------------------------------------------------------- #
def test_06_multi_file_aggregation():
    out = _parse(_fixture("multi-file.diff"))
    assert len(out["files"]) == 3
    paths = [f["path"] for f in out["files"]]
    assert paths == ["Makefile", "README.md", "src/app.py"]
    assert out["change_categories"] == ["source", "documentation", "config"]
    assert out["stats"]["files_changed"] == 3
    assert out["stats"]["additions"] == 4  # 1 + 2 + 1


def test_07_binary_files_detected():
    out = _parse(_fixture("binary.diff"))
    assert len(out["files"]) == 2
    assert all(f["binary"] for f in out["files"])
    assert [f["change_type"] for f in out["files"]] == ["M", "A"]
    assert out["stats"]["binary_files"] == 2
    assert out["change_categories"] == ["binary"]


def test_08_quoted_path_space_and_unicode():
    out = _parse(_fixture("quoted-path.diff"))
    paths = [f["path"] for f in out["files"]]
    assert "docs/my notes.md" in paths
    assert "src/café.py" in paths  # octal \303\251 unquoted to UTF-8 é


def test_09_no_newline_marker_handled():
    out = _parse(_fixture("no-newline.diff"))
    a, b = out["files"]
    assert (a["additions"], a["deletions"]) == (1, 1)
    assert (b["additions"], b["deletions"]) == (1, 1)
    assert out["stats"]["additions"] == 2 and out["stats"]["deletions"] == 2


def test_10_mode_exec_and_type_change():
    out = _parse(_fixture("mode-change.diff"))
    by_path = {f["path"]: f for f in out["files"]}
    assert by_path["scripts/run.sh"]["change_type"] == "M"
    assert by_path["scripts/run.sh"]["mode_changed"] is True
    assert by_path["entrypoint"]["change_type"] == "T"
    assert by_path["entrypoint"]["mode_changed"] is True


# --------------------------------------------------------------------------- #
# Edge / negative
# --------------------------------------------------------------------------- #
def test_11_prompt_injection_is_inert():
    text = _fixture("prompt-injection.diff")
    out = _parse(text)
    f = out["files"][0]
    assert f["additions"] == 5 and f["deletions"] == 1
    # the malicious instructions are counted as plain added lines and never
    # echoed into the structured output
    blob = json.dumps(out)
    assert "ignore all previous instructions" not in blob
    assert "evil.example" not in blob


def test_12_crlf_input_normalized():
    text = (
        "diff --git a/x.py b/x.py\r\n--- a/x.py\r\n+++ b/x.py\r\n"
        "@@ -1 +1,2 @@\r\n x\r\n+y\r\n"
    )
    out = _parse(text)
    assert out["files"][0]["additions"] == 1
    assert out["files"][0]["deletions"] == 0
    assert out["complete"] is True


def test_13_empty_diff_is_valid_empty_result():
    out = _parse("")
    assert out["files"] == []
    assert out["complete"] is True
    assert out["stats"] == {"files_changed": 0, "additions": 0, "deletions": 0,
                            "hunks": 0, "binary_files": 0}
    assert out["change_categories"] == []


def test_14_whitespace_only_diff_is_empty_result():
    out = _parse("\n\n   \n\n")
    assert out["files"] == []
    assert out["complete"] is True


def test_15_malformed_rejected():
    with pytest.raises(core.DiffParseError) as ei:
        _parse(_fixture("malformed.diff"))
    assert ei.value.code == core.MALFORMED


def test_16_truncated_hunk_rejected():
    text = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        "@@ -1,5 +1,5 @@\n-a\n-b\n"
    )
    with pytest.raises(core.DiffParseError) as ei:
        _parse(text)
    assert ei.value.code == core.MALFORMED


def test_17_oversized_diff_rejected():
    text = "diff --git a/x.py b/x.py\n@@ -1 +1,2 @@\n x\n" + ("+y\n" * 50)
    with pytest.raises(core.DiffParseError) as ei:
        _parse(text, options={"max_diff_bytes": 40})
    assert ei.value.code == core.INPUT_TOO_LARGE


def test_18_max_files_yields_partial():
    base = "diff --git a/f{n}.py b/f{n}.py\n--- a/f{n}.py\n+++ b/f{n}.py\n@@ -1 +1,2 @@\n x\n+y\n"
    text = "".join(base.format(n=i) for i in range(5))
    out = _parse(text, options={"max_files": 3})
    assert out["complete"] is False
    assert len(out["files"]) == 3
    assert "file limit" in out["degradation_reason"]


def test_19_max_total_lines_yields_partial():
    text = "diff --git a/big.py b/big.py\n--- a/big.py\n+++ b/big.py\n@@ -1 +1,11 @@\n x\n" + (
        "+y\n" * 10
    )
    out = _parse(text, options={"max_total_lines": 5})
    assert out["complete"] is False
    assert "line limit" in out["degradation_reason"]


def test_20_unsupported_format_rejected():
    with pytest.raises(core.DiffParseError) as ei:
        core.parse_diff(repo="o/r", base_sha=SHA, head_sha=HEAD,
                        diff_text="x", diff_format="raw")
    assert ei.value.code == core.UNSUPPORTED_FORMAT


# --------------------------------------------------------------------------- #
# Consistency / determinism / security
# --------------------------------------------------------------------------- #
def test_21_stats_self_consistent():
    out = _parse(_fixture("multi-file.diff"))
    assert out["stats"]["additions"] == sum(f["additions"] for f in out["files"])
    assert out["stats"]["deletions"] == sum(f["deletions"] for f in out["files"])
    assert out["stats"]["hunks"] == sum(len(f["hunks"]) for f in out["files"])
    assert out["stats"]["files_changed"] == len(out["files"])


def test_22_modules_touched_root_dot_and_subdir():
    text = (
        "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1,2 @@\n t\n+u\n"
        "diff --git a/src/a/b.py b/src/a/b.py\n--- a/src/a/b.py\n+++ b/src/a/b.py\n@@ -1 +1,2 @@\n x\n+y\n"
    )
    out = _parse(text)
    assert out["modules_touched"] == [".", "src/a"]


def test_23_path_normalization_strips_prefix():
    out = _parse(_fixture("real-modified.diff"))
    for p in [f["path"] for f in out["files"]] + [f["old_path"] for f in out["files"] if f["old_path"]]:
        assert not p.startswith("a/") and not p.startswith("b/")
        assert "\\" not in p


def test_24_determinism_byte_identical():
    text = _fixture("multi-file.diff")
    a = json.dumps(_parse(text), sort_keys=True)
    b = json.dumps(_parse(text), sort_keys=True)
    assert a == b


def test_25_input_sha256_matches_diff_text():
    text = _fixture("real-modified.diff")
    out = _parse(text)
    assert out["input_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_26_change_categories_vocabulary_and_mappings():
    cases = {
        "requirements.txt": "dependency",
        ".github/workflows/ci.yml": "workflow",
        "migrations/0001.sql": "migration",
        "src/auth/login.py": "security_sensitive",
        "tests/test_x.py": "test",
        "docs/guide.md": "documentation",
        "Makefile": "config",
        "src/app.py": "source",
    }
    for path, expected in cases.items():
        cats = core.categorize(path, "M", False)
        assert expected in cats, (path, cats)
    # binary + deletion overlays
    assert "binary" in core.categorize("a.png", "M", True)
    assert "deletion" in core.categorize("src/x.py", "D", False)


def test_27_secret_shaped_text_not_in_output():
    probe = "ghp_" + "a" * 36  # assembled -> keeps source scanner-clean
    text = (
        "diff --git a/src/c.py b/src/c.py\n--- a/src/c.py\n+++ b/src/c.py\n"
        "@@ -1 +1,2 @@\n x\n+" + probe + "\n"
    )
    out = _parse(text)
    assert probe not in json.dumps(out)
    assert out["files"][0]["additions"] == 1


def test_28_path_traversal_text_is_inert():
    text = (
        "diff --git a/../../etc/passwd b/a/../../etc/passwd\n"
        "--- a/../../etc/passwd\n+++ a/../../etc/passwd\n@@ -1 +1,2 @@\n x\n+y\n"
    )
    out = _parse(text)  # must not attempt to read the local path
    assert out["complete"] is True
    assert "../../etc/passwd" in out["files"][0]["path"]


def test_29_output_validates_against_schema():
    out = _parse(_fixture("multi-file.diff"))
    _output_validator().validate(out)  # raises on violation


def test_30_pr_number_optional_and_source_carried():
    out = core.parse_diff(repo="o/r", base_sha=SHA, head_sha=HEAD, pr_number=42,
                          diff_text=_fixture("real-modified.diff"), diff_format="unified")
    assert out["source"] == {"repo": "o/r", "base_sha": SHA, "head_sha": HEAD, "pr_number": 42}
    out2 = _parse(_fixture("real-modified.diff"))
    assert "pr_number" not in out2["source"]


# --------------------------------------------------------------------------- #
# Common-runtime entry (input schema, error mapping)
# --------------------------------------------------------------------------- #
def test_31_handle_ok_returns_result():
    res = dp_run.handle({"input": {
        "repo": "o/r", "base_sha": SHA, "head_sha": HEAD,
        "diff_format": "unified", "diff_text": _fixture("real-modified.diff"),
    }})
    assert res["status"] == "OK"
    assert res["output"]["files"][0]["change_type"] == "M"


def test_32_handle_partial_status():
    base = "diff --git a/f{n}.py b/f{n}.py\n--- a/f{n}.py\n+++ b/f{n}.py\n@@ -1 +1,2 @@\n x\n+y\n"
    text = "".join(base.format(n=i) for i in range(4))
    res = dp_run.handle({"input": {
        "repo": "o/r", "base_sha": SHA, "head_sha": HEAD,
        "diff_format": "unified", "diff_text": text, "options": {"max_files": 2},
    }})
    assert res["status"] == "PARTIAL"
    assert core.PARTIAL_CONTEXT in res["warning_codes"]
    assert res["degradations"]


def test_33_handle_malformed_raises_skill_error():
    with pytest.raises(errors.SkillError) as ei:
        dp_run.handle({"input": {
            "repo": "o/r", "base_sha": SHA, "head_sha": HEAD,
            "diff_format": "unified", "diff_text": _fixture("malformed.diff"),
        }})
    assert ei.value.code == core.MALFORMED


def test_34_handle_rejects_invalid_input():
    with pytest.raises(errors.InvalidInput):
        dp_run.handle({"input": {"repo": "not-a-slug", "base_sha": "x", "head_sha": HEAD,
                                  "diff_format": "unified", "diff_text": ""}})


# --------------------------------------------------------------------------- #
# Audit-driven negatives: hunk over-count, plain multi-file, quoted paths,
# error-message no-echo, CLI pre-validation redaction.
# --------------------------------------------------------------------------- #
def test_35_hunk_overcount_rejected():
    # old_count declares 1 old line but the body has two '-' lines
    text = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n-b\n+c\n"
    with pytest.raises(core.DiffParseError) as ei:
        _parse(text)
    assert ei.value.code == core.MALFORMED


def test_36_plain_multi_file_not_merged():
    # no 'diff --git' (plain unified diff) with two files must yield two files
    text = (
        "--- a/f1.py\n+++ b/f1.py\n@@ -1 +1,2 @@\n x\n+y\n"
        "--- a/f2.py\n+++ b/f2.py\n@@ -1 +1,2 @@\n x\n+z\n"
    )
    out = _parse(text)
    assert [f["path"] for f in out["files"]] == ["f1.py", "f2.py"]


def test_37_quoted_rename_and_copy_paths_unquoted():
    text = (
        'diff --git "a/old name.py" "b/new name.py"\n'
        'similarity index 88%\nrename from "old name.py"\nrename to "new name.py"\n'
    )
    out = _parse(text)
    f = out["files"][0]
    assert f["change_type"] == "R" and f["path"] == "new name.py" and f["old_path"] == "old name.py"
    text2 = (
        'diff --git "a/orig.py" "b/copy.py"\n'
        'similarity index 100%\ncopy from "orig.py"\ncopy to "copy.py"\n'
    )
    f2 = _parse(text2)["files"][0]
    assert f2["change_type"] == "C" and f2["path"] == "copy.py" and f2["old_path"] == "orig.py"


def test_38_quoted_binary_path_unquoted():
    text = 'diff --git "a/my bin.png" "b/my bin.png"\nBinary files "a/my bin.png" and "b/my bin.png" differ\n'
    out = _parse(text)
    f = out["files"][0]
    assert f["path"] == "my bin.png" and f["binary"] is True


def test_39_error_message_does_not_echo_secret():
    probe = "ghp_" + "a" * 36  # assembled -> source scanner-clean
    text = "diff --git a/x.py b/x.py\n" + probe + "\n"
    with pytest.raises(core.DiffParseError) as ei:
        _parse(text)
    assert probe not in ei.value.message


def test_40_cli_direct_entry_redacts_credential_request():
    import subprocess
    import sys
    probe = "ghp_" + "a" * 36  # assembled -> source scanner-clean
    req = {"contract_version": "2", "request_id": probe, "trace_id": "tr-1",
           "input": {}}
    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO_ROOT
    env["PYTHONDONTBYTECODE"] = "1"
    proc = subprocess.run([sys.executable, "-m", "skills.diff_parse.run"],
                          input=json.dumps(req), capture_output=True, text=True,
                          cwd=_REPO_ROOT, env=env)
    env_out = json.loads(proc.stdout)
    assert probe not in proc.stdout
    assert "request_id" in env_out["redactions"]
    assert env_out["status"] == "ERROR"


def test_41_quoted_path_with_escaped_quote():
    # git quotes a path containing a literal double-quote + space as "foo\" bar.bin"
    BS = chr(92)  # backslash
    Q = chr(34)   # double quote
    tok_a = Q + "a/foo" + BS + Q + " bar.bin" + Q
    tok_b = Q + "b/foo" + BS + Q + " bar.bin" + Q
    text = ("diff --git " + tok_a + " " + tok_b + "\n"
            "Binary files " + tok_a + " and " + tok_b + " differ\n")
    out = _parse(text)
    f = out["files"][0]
    assert f["path"] == 'foo" bar.bin'
    assert f["binary"] is True
