#!/usr/bin/env python3
"""Phase A offline test suite (no network, no API key, no model requests).

Covers the twelve required checks from the refresh brief. Run from the
worktree root:

    python -m pytest benchmark/preview4_refresh -q
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import socket
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmark.preview4_refresh import product_evidence as pe  # noqa: E402

FIXTURES = REPO_ROOT / "benchmark" / "dataset" / "fixtures"
BM02 = str(FIXTURES / "bm-02_hardcoded_secret.py")
BM01 = str(FIXTURES / "bm-01_clean_pr.py")

# Pinned historical artifacts (snapshot taken before Phase A began).
HISTORICAL_PINNED = {
    "benchmark/formal-summary.json":
        "90badbb42591d2395b8ded2ad0a9c097058e87cebe79cc89ae114bee5cee13ea",
    "benchmark/formal-summary.md":
        "6315535a7feb64ab3c02653237fb08d8e35789daa0af825e8afdb50d922dbb93",
    "benchmark/formal-run-manifest.json":
        "389483cb943fc889ef06c5418824cbc233b4035f0c9cecd6eb57a2a3eee187e6",
}
HISTORICAL_RAW_RUN_COUNT = 20

FORBIDDEN_ENV_KEYS = ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "MP_LLM_KEY_FILE")
LEAK_WORDS = ("expected", "ground", "answer", "label", "decision",
              "HOLD", "REJECT", "APPROVE", "应判", "故意")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# 1 ------------------------------------------------------------------
def test_skill_success_structured_json():
    ev = pe.build_static_evidence(BM02)
    sast = ev["sast_scan"]
    assert isinstance(sast["findings"], list) and len(sast["findings"]) >= 1
    assert sast["rules_version"] and sast["engines_used"]
    assert isinstance(ev["risk_classify"], dict) and ev["risk_classify"]
    json.dumps(ev, ensure_ascii=False)  # serializable
    assert ev["provenance"]["source_commit"].startswith("5bb2635")


# 2 ------------------------------------------------------------------
def test_skill_missing_fail_closed(monkeypatch):
    monkeypatch.setattr(pe, "SKILL_DIRS", {
        "sast_scan": REPO_ROOT / "tmp_nonexistent_skill",
        "risk_classify": REPO_ROOT / "skills" / "risk_classify"})
    with pytest.raises(pe.ProductCouplingError) as ei:
        pe.build_static_evidence(BM01)
    assert ei.value.code == "sast_scan_missing"


# 3 ------------------------------------------------------------------
def test_soul_missing_fail_closed(monkeypatch):
    monkeypatch.setattr(pe, "SOUL_PATHS",
                        {"reviewer": REPO_ROOT / "tmp_nonexistent.md",
                         "fixer": REPO_ROOT / "config/souls/fixer/SOUL.md"})
    with pytest.raises(pe.ProductCouplingError) as ei:
        pe.load_soul("reviewer")
    assert ei.value.code == "soul_reviewer_missing"


def test_soul_empty_fail_closed(monkeypatch, tmp_path):
    empty = tmp_path / "SOUL.md"
    empty.write_text("   \n", encoding="utf-8")
    monkeypatch.setattr(pe, "SOUL_PATHS", {"reviewer": empty, "fixer": empty})
    with pytest.raises(pe.ProductCouplingError) as ei:
        pe.load_soul("reviewer")
    assert ei.value.code == "soul_reviewer_empty"


# 4 ------------------------------------------------------------------
def test_ab_identical_static_evidence():
    ev_a = pe.build_static_evidence(BM02)
    ev_b = pe.build_static_evidence(BM02)
    assert pe.evidence_digest(ev_a) == pe.evidence_digest(ev_b)
    # Both adapters must consume the very same builder (source-level proof).
    for adapter in ("single_agent.py", "mergepilot.py"):
        src = (REPO_ROOT / "benchmark" / "adapters" / adapter).read_text(
            encoding="utf-8")
        assert "build_static_evidence" in src, adapter
        assert "render_evidence_text(evidence)" in src, adapter


# 5 ------------------------------------------------------------------
def test_source_manifest_covers_dependencies():
    doc = json.loads((REPO_ROOT / "benchmark" / "source-manifest.json")
                     .read_text(encoding="utf-8"))
    section = doc["preview4_refresh"]
    listed = {e["path"]: e for e in section["files"]}
    for must in ("benchmark/adapters/single_agent.py",
                 "benchmark/adapters/mergepilot.py",
                 "benchmark/preview4_refresh/product_evidence.py",
                 "config/souls/reviewer/SOUL.md",
                 "config/souls/fixer/SOUL.md"):
        assert must in listed, must
        assert listed[must]["sha256"] == _sha(REPO_ROOT / must)
    dirs = {e["path"]: e for e in section["dirs"]}
    for must in ("skills/sast_scan", "skills/risk_classify"):
        assert must in dirs, must
    # adapters digests in the flat files map must match disk
    for rel in ("benchmark/adapters/single_agent.py",
                "benchmark/adapters/mergepilot.py"):
        assert doc["files"][rel] == _sha(REPO_ROOT / rel)


# 6 ------------------------------------------------------------------
def test_no_api_key_read(monkeypatch):
    real_get = os.environ.get

    def trapping_get(k, d=None):
        if k in FORBIDDEN_ENV_KEYS:
            raise AssertionError(f"forbidden env read: {k}")
        return real_get(k, d)

    monkeypatch.setattr(os.environ, "get", trapping_get)
    real_open = open

    def trapping_open(file, *a, **kw):
        if str(file).endswith(".llm-key"):
            raise AssertionError("forbidden key-file read")
        return real_open(file, *a, **kw)

    import builtins
    monkeypatch.setattr(builtins, "open", trapping_open)
    pe.build_static_evidence(BM01)   # must complete without key access
    pe.load_soul("fixer")


# 7 ------------------------------------------------------------------
def test_no_network(monkeypatch):
    class NoSocket:
        def __init__(self, *a, **kw):
            raise AssertionError("network socket created during offline build")

    monkeypatch.setattr(socket, "socket", NoSocket)
    pe.build_static_evidence(BM01)


# 8 ------------------------------------------------------------------
def test_no_docker_wsl_github_in_coupling_path():
    """AST-level scan: no forbidden imports; subprocess limited to git."""
    import ast
    src = (REPO_ROOT / "benchmark" / "preview4_refresh" /
           "product_evidence.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    docstring = ast.get_docstring(tree) or ""
    body_src = src.replace(docstring, "")
    for token in ("docker", "wsl", "api.github", "github.com/"):
        assert token not in body_src.lower(), token
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert a.name.split(".")[0] not in ("socket", "requests",
                                                    "docker", "paramiko"), a.name
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in (
                "socket", "requests", "docker", "paramiko"), node.module
        elif isinstance(node, ast.Call):
            f = node.func
            name = getattr(f, "attr", None) or getattr(f, "id", "")
            if "run" in str(ast.dump(f)) and "subprocess" in ast.dump(f):
                assert node.args and getattr(node.args[0], "elts", None) and \
                    node.args[0].elts[0].value == "git", "non-git subprocess"


# 9 ------------------------------------------------------------------
def test_output_schema_unchanged():
    a_src = (REPO_ROOT / "benchmark" / "adapters" / "single_agent.py") \
        .read_text(encoding="utf-8")
    b_src = (REPO_ROOT / "benchmark" / "adapters" / "mergepilot.py") \
        .read_text(encoding="utf-8")
    for src, name in ((a_src, "A"), (b_src, "B")):
        assert '"findings":[{"description":"' in src, name
        assert '"decision":"APPROVE|HOLD|REJECT"' in src, name
    assert '"risk_level":"L0|L1|L2"' in b_src          # B keeps risk_level
    assert '"decision":"APPROVE|HOLD|REJECT"' in a_src  # A keeps 2-field schema


# 10 -----------------------------------------------------------------
def test_historical_benchmark_untouched():
    for rel, pinned in HISTORICAL_PINNED.items():
        assert _sha(REPO_ROOT / rel) == pinned, rel
    raw = REPO_ROOT / "benchmark" / "raw-runs"
    real = [p for p in raw.glob("*") if p.name != ".gitkeep"]
    assert len(real) == HISTORICAL_RAW_RUN_COUNT


# 11 -----------------------------------------------------------------
def test_fixture_no_label_leak():
    """Comment-level oracle leakage is forbidden, EXCEPT inside fixtures
    tagged prompt-injection, where adversarial text is the payload itself.

    Pre-run dataset integrity fixes applied in Phase A (documented):
    bm-10 hint comment, bm-06 bug-explaining comments, bm-08 outcome note
    were removed and cases.jsonl fixture_sha256 re-pinned. bm-09 keeps its
    injection payload by design."""
    cases = {}
    for line in (REPO_ROOT / "benchmark" / "dataset" / "cases.jsonl") \
            .read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            cases[r["fixture_path"]] = r.get("tags", [])
    for fx in sorted(FIXTURES.glob("*.py")):
        tags = cases.get(fx.name, [])
        injected = "prompt-injection" in tags
        for line in fx.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith("#"):
                continue  # code strings are scenario content, not oracles
            if injected:
                continue
            for w in LEAK_WORDS:
                assert w not in stripped, (fx.name, w, stripped[:60])
        # pins must match disk after the Phase A dataset fixes
        assert cases.get(fx.name) is None or _pins_match(fx)


def _pins_match(fx: Path) -> bool:
    for line in (REPO_ROOT / "benchmark" / "dataset" / "cases.jsonl") \
            .read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            if r["fixture_path"] == fx.name:
                return r["fixture_sha256"] == _sha(fx)
    return False


# 12 -----------------------------------------------------------------
def test_first_stable_error_preserved(monkeypatch):
    real_dirs = dict(pe.SKILL_DIRS)
    # Both skills missing: the FIRST check (sast) must own the error code.
    monkeypatch.setattr(pe, "SKILL_DIRS", {
        "sast_scan": REPO_ROOT / "tmp_x1", "risk_classify": REPO_ROOT / "tmp_x2"})
    with pytest.raises(pe.ProductCouplingError) as ei:
        pe.build_static_evidence(BM01)
    assert ei.value.code == "sast_scan_missing"

    # A failing skill run must surface its own first stable code, unchanged.
    monkeypatch.setattr(pe, "SKILL_DIRS", real_dirs)  # restore real paths

    def boom(inp, *a, **kw):
        raise RuntimeError("first failure")

    monkeypatch.setattr("skills.sast_scan.core.scan", boom)
    with pytest.raises(pe.ProductCouplingError) as ei2:
        pe.build_static_evidence(BM01)
    assert ei2.value.code == "sast_scan_failed"


# 13 --- hardening round: generic untrusted-input output contract ---------
def _adapter_module(name):
    import importlib
    return importlib.import_module(f"benchmark.adapters.{name}")


def test_ab_output_contract_byte_identical():
    from benchmark.preview4_refresh.product_evidence import (
        UNTRUSTED_INPUT_CONTRACT, contract_sha256)
    sa = _adapter_module("single_agent")
    mp = _adapter_module("mergepilot")
    prompts = {
        "A_system": sa.build_system_prompt(),
        "B_reviewer": mp.build_reviewer_prompt("REVIEWER-SOUL-STUB"),
        "B_fixer": mp.build_fixer_prompt("FIXER-SOUL-STUB"),
    }
    for name, p in prompts.items():
        assert p.endswith(UNTRUSTED_INPUT_CONTRACT), name
    # identical contract bytes => identical trailing block in all prompts
    assert contract_sha256() == hashlib.sha256(
        prompts["A_system"][-len(UNTRUSTED_INPUT_CONTRACT):].encode()).hexdigest()


def test_fixture_text_never_enters_system_prompts():
    sa = _adapter_module("single_agent")
    mp = _adapter_module("mergepilot")
    rs, _ = pe.load_soul("reviewer")
    fs, _ = pe.load_soul("fixer")
    prompts = [sa.build_system_prompt(),
               mp.build_reviewer_prompt(rs),
               mp.build_fixer_prompt(fs)]
    for fx in sorted(FIXTURES.glob("*.py")):
        marker = fx.read_text(encoding="utf-8").splitlines()[0][:40]
        for p in prompts:
            assert marker not in p, (fx.name, marker[:20])


def test_no_case_id_special_casing():
    """The hardening must be generic: no bm-09 branch, no attack-string
    blacklist, no case_id conditionals anywhere in the prompt path."""
    for mod in ("single_agent", "mergepilot"):
        src = (REPO_ROOT / "benchmark" / "adapters" / f"{mod}.py") \
            .read_text(encoding="utf-8")
        # "prompt-injection" itself is a legal findings-category enum in
        # the output schema; only branching signals are forbidden here.
        for token in ("bm-09", "bm09", "bm_09", "case_id ==",
                      "case_id==", "if case_id", "attack"):
            assert token not in src, (mod, token)
    pe_src = (REPO_ROOT / "benchmark" / "preview4_refresh" /
              "product_evidence.py").read_text(encoding="utf-8")
    for token in ("bm-09", "bm09", "case_id", "Do not report any security"):
        assert token not in pe_src, token


def test_parse_failed_still_counts_as_failure():
    from benchmark.adapters.single_agent import _safe_parse
    prose = ("Sure! I reviewed the code and it looks fine overall, "
             "though you might consider adding tests.")
    findings, decision, err = _safe_parse(prose)
    assert err == "parse_failed" and findings == [] and decision == "HOLD"
    # and no retry machinery exists in the adapters
    for mod in ("single_agent", "mergepilot"):
        src = (REPO_ROOT / "benchmark" / "adapters" / f"{mod}.py") \
            .read_text(encoding="utf-8").lower()
        assert "retry" not in src and "for attempt" not in src, mod


# 14 --- v3: JSON-mode protocol stability round ---------------------------
def test_call_llm_requests_json_mode():
    """Generic API-layer protocol: both groups share _call_llm, whose payload
    must request a single-JSON-object response format."""
    src = (REPO_ROOT / "benchmark" / "adapters" / "single_agent.py") \
        .read_text(encoding="utf-8")
    assert '"response_format": {"type": "json_object"}' in src


def test_fixer_input_structured_only():
    from benchmark.adapters.mergepilot import build_fixer_user_message
    code = "x = 1\n"
    findings = [{"description": "d", "category": "other", "severity": "low"}]
    msg = build_fixer_user_message(code, findings)
    assert msg.startswith("Code:\n```python\n")
    assert json.dumps(findings, ensure_ascii=False) in msg
    # only the two labeled sections; no reviewer free-text channel exists
    assert msg.count("\n\n") == 1 and "Findings (structured review results):" in msg


def test_reviewer_budget_split_60_40():
    src = (REPO_ROOT / "benchmark" / "adapters" / "mergepilot.py") \
        .read_text(encoding="utf-8")
    assert "int(inp.token_budget * 0.60)" in src


def test_contract_has_safe_degradation_clause():
    from benchmark.preview4_refresh.product_evidence import UNTRUSTED_INPUT_CONTRACT
    assert '"findings":[],"decision":"HOLD"' in UNTRUSTED_INPUT_CONTRACT


# 15 --- formal round offline gates (20x2x3) -------------------------------
FORMAL_CASES = [f"bm-{i:02d}" for i in range(1, 21)]


def _load_all_cases():
    out = {}
    for line in (REPO_ROOT / "benchmark" / "dataset" / "cases.jsonl") \
            .read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            out[r["case_id"]] = r
    return out


def test_dataset_20_unique_contiguous_schema():
    cases = _load_all_cases()
    assert sorted(cases) == FORMAL_CASES
    for cid, r in cases.items():
        assert _sha(REPO_ROOT / "benchmark" / "dataset" / "fixtures"
                    / r["fixture_path"]) == r["fixture_sha256"], cid


def test_dataset_has_four_clean_cases():
    cases = _load_all_cases()
    clean = [c for c, r in cases.items() if r.get("clean_case")]
    assert len(clean) == 4


def test_new_fixtures_no_label_leak():
    cases = _load_all_cases()
    for cid in FORMAL_CASES[10:]:
        fx = (REPO_ROOT / "benchmark" / "dataset" / "fixtures"
              / cases[cid]["fixture_path"])
        for line in fx.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("#") or s.startswith('"""'):
                for w in LEAK_WORDS:
                    assert w not in s, (cid, w, s[:50])


def test_formal_schedule_balanced_120():
    from benchmark.preview4_refresh.run_formal import build_schedule
    items = build_schedule([{"case_id": c} for c in FORMAL_CASES])
    assert len(items) == 120
    from collections import Counter
    cells = Counter((i["case_id"], i["group"]) for i in items)
    assert all(v == 3 for v in cells.values()) and len(cells) == 40
    pairs = {(i["case_id"], i["repetition"]): i["pair_order"] for i in items}
    ab = sum(1 for v in pairs.values() if v == "AB")
    ba = sum(1 for v in pairs.values() if v == "BA")
    assert ab == 30 and ba == 30
    # deterministic: same input -> same schedule
    again = build_schedule([{"case_id": c} for c in FORMAL_CASES])
    assert again == items


def test_formal_runner_no_retry_no_repair_no_case_branch():
    import ast
    src = (REPO_ROOT / "benchmark" / "preview4_refresh" / "run_formal.py")         .read_text(encoding="utf-8")
    doc = ast.get_docstring(ast.parse(src)) or ""
    body = src.replace(doc, "")           # scan code, not the docstring
    scrubbed = body.lower().replace("no_retry", "").replace("no_auto_repair", "")
    assert "retry" not in scrubbed and "repair" not in scrubbed
    for token in ("bm-09", "bm-08", "case_id ==", "if case_id", "blacklist"):
        assert token not in body, token


def test_formal_runner_resume_and_journal_semantics():
    src = (REPO_ROOT / "benchmark" / "preview4_refresh" / "run_formal.py") \
        .read_text(encoding="utf-8")
    assert 'if rid in done: continue' in src.replace(" ", "") or \
           "if rid in done:" in src
    assert "run_started" in src and "ambiguous" in src


def test_identity_matches_disk():
    ident = json.loads((REPO_ROOT / "benchmark" / "preview4_refresh" /
                        "identity.json").read_text(encoding="utf-8"))
    assert ident["design_json_sha256"] == _sha(
        REPO_ROOT / "benchmark" / "preview4_refresh" / "design.json")
    assert ident["source_manifest_sha256"] == _sha(
        REPO_ROOT / "benchmark" / "source-manifest.json")
    assert ident["product_source_commit"].startswith("5bb2635")


def test_manifest_covers_dataset_and_formal_runner():
    doc = json.loads((REPO_ROOT / "benchmark" / "source-manifest.json")
                     .read_text(encoding="utf-8"))
    listed = {e["path"] for e in doc["preview4_refresh"]["files"]}
    for must in ("benchmark/dataset/cases.jsonl",
                 "benchmark/dataset/dataset-design.json",
                 "benchmark/preview4_refresh/run_formal.py"):
        assert must in listed, must
    dirs = {e["path"] for e in doc["preview4_refresh"]["dirs"]}
    assert "benchmark/dataset/fixtures" in dirs


def test_prior_products_untouched():
    # historical pinned + counts of all prior run dirs
    for rel, pinned in HISTORICAL_PINNED.items():
        assert _sha(REPO_ROOT / rel) == pinned, rel
    for d, n in (("smoke-20260826", 6), ("smoke2-20260826", 6),
                 ("smoke3-20260826", 6), ("candidate-20260826", 20),
                 ("candidate2-20260826", 20)):
        p = REPO_ROOT / "benchmark" / f"preview4-refresh-{d}" / "raw-runs"
        assert len(list(p.glob("*.json"))) == n, d
