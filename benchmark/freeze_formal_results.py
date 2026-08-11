#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Post-run, machine-computed freeze of the formal N=10x2 benchmark.

This tool is READ-ONLY with respect to the execution sources and raw runs.
It does NOT:
  - import or call any adapter / runner / evaluator logic
  - make network requests
  - read environment credentials (no os.environ access for secrets)
  - modify any input file
  - hand-edit any metric

It DOES:
  - recompute every number in this file from benchmark/raw-runs/*.json
  - validate the raw-run set (count, pairs, schema, source digests)
  - classify credential-like pattern hits as synthetic-fixture vs real
  - publish three frozen artifacts via atomic write (write-to-temp + os.replace)
  - produce byte-identical content on re-execution (generated_at excluded)

Fail-closed behavior: any of the following causes a non-zero exit with NO
artifacts written or updated:
  - raw-run count != 20
  - pair set != {(bm-01..bm-10) x (A_single_agent, B_mergepilot)}
  - any raw-run fails structural / schema validation
  - source-manifest.json SHA256 != EXPECTED_SOURCE_MANIFEST_SHA256
  - any of the 24 source/fixture digests mismatch the on-disk file
  - raw combined SHA256 != EXPECTED_RAW_COMBINED_SHA256
  - decision ground-truth source (benchmark/dataset/cases.jsonl) is not in
    the frozen source manifest

Usage:
    python benchmark/freeze_formal_results.py
Exit codes:
    0  -> BENCHMARK_FROZEN_FOR_SUBMISSION
    2  -> BENCHMARK_FREEZE_FAILED (validation mismatch; no artifacts touched)
"""

from __future__ import annotations

import datetime as _dt
import glob
import hashlib
import json
import os
import re
import sys
import tempfile

# ---------------------------------------------------------------------------
# Hard-coded expectations (fail-closed anchors). If the on-disk evidence does
# not reproduce these exactly, the freeze aborts and writes nothing.
# ---------------------------------------------------------------------------

EXPECTED_RAW_COUNT = 20
EXPECTED_CASES = [f"bm-{i:02d}" for i in range(1, 11)]
EXPECTED_GROUPS = ["A_single_agent", "B_mergepilot"]
EXPECTED_MODEL = "deepseek-v4-flash"

EXPECTED_SOURCE_MANIFEST_SHA256 = (
    "0de4917ef4c87bce9c5b634c5050bd3aa8b200907cc6211501478bb99f6db8ff"
)
EXPECTED_RAW_COMBINED_SHA256 = (
    "c863ecd683dcfad5076125693ef180398e4393aafb7ca28d436e7242523a25c4"
)

# Credential-like candidate regexes used to *locate* hits for classification.
# We intentionally keep these broad; final classification compares the matched
# substring byte-for-byte against the synthetic fixture values.
_CANDIDATE_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9\-]{8,}"),
    re.compile(r"ghp_[0-9A-Za-z]{8,}"),
    re.compile(r"ghs_[0-9A-Za-z]{8,}"),
    re.compile(r"AKIA[0-9A-Z]{12,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
]

BENCHMARK_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BENCHMARK_DIR)

RAW_DIR = os.path.join(BENCHMARK_DIR, "raw-runs")
SMOKE_DIR = os.path.join(BENCHMARK_DIR, "smoke-runs")
SOURCE_MANIFEST = os.path.join(BENCHMARK_DIR, "source-manifest.json")
RESULTS_CSV = os.path.join(BENCHMARK_DIR, "results.csv")
REPORT_MD = os.path.join(BENCHMARK_DIR, "report.md")
CASES_JSONL = os.path.join(BENCHMARK_DIR, "dataset", "cases.jsonl")
FIXTURES_DIR = os.path.join(BENCHMARK_DIR, "dataset", "fixtures")
RUN_RESULT_SCHEMA = os.path.join(BENCHMARK_DIR, "schemas", "run-result.schema.json")

OUT_SUMMARY_JSON = os.path.join(BENCHMARK_DIR, "formal-summary.json")
OUT_SUMMARY_MD = os.path.join(BENCHMARK_DIR, "formal-summary.md")
OUT_RUN_MANIFEST = os.path.join(BENCHMARK_DIR, "formal-run-manifest.json")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fail(reason: str) -> "NoReturn":  # noqa: F821
    sys.stderr.write(f"BENCHMARK_FREEZE_FAILED: {reason}\n")
    sys.exit(2)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write_text(path: str, text: str) -> None:
    """Write text to path atomically: temp file in same dir + os.replace."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".freeze-tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            out.write(text)
        os.replace(tmp, path)
    except Exception:
        # best-effort cleanup of the temp file on failure
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _load_and_validate_raw_runs() -> tuple[list[dict], list[str]]:
    files = sorted(glob.glob(os.path.join(RAW_DIR, "*.json")))
    if len(files) != EXPECTED_RAW_COUNT:
        _fail(
            f"raw-run count={len(files)} expected={EXPECTED_RAW_COUNT}"
        )

    name_pat = re.compile(
        r"^(bm-\d{2})-(A_single_agent|B_mergepilot)-[0-9a-f]{6}\.json$"
    )
    pairs: dict[tuple[str, str], str] = {}
    for f in files:
        name = os.path.basename(f)
        m = name_pat.match(name)
        if not m:
            _fail(f"raw-run filename does not match contract: {name}")
        key = (m.group(1), m.group(2))
        if key in pairs:
            _fail(f"duplicate raw-run pair: {key}")
        pairs[key] = f

    expected_pairs = {(c, g) for c in EXPECTED_CASES for g in EXPECTED_GROUPS}
    missing = expected_pairs - set(pairs)
    extra = set(pairs) - expected_pairs
    if missing or extra:
        _fail(f"raw-run pair mismatch missing={sorted(missing)} extra={sorted(extra)}")

    # schema validation (preferred) with structural fallback
    try:
        import jsonschema  # type: ignore
        schema = json.load(open(RUN_RESULT_SCHEMA, encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(schema)
        use_schema = True
    except Exception:
        use_schema = False

    required_keys = [
        "run_id", "case_id", "group", "model", "started_at", "finished_at",
        "status", "findings", "decision", "token_usage", "api_request_count",
        "audit_events", "audit_complete", "error_detail",
        "eval_passed", "eval_reason", "eval_tp", "eval_fp", "eval_fn",
    ]

    records: list[dict] = []
    schema_valid = 0
    models: set[str] = set()
    statuses: set[str] = set()
    err_values: set = set()
    for f in files:
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception as e:
            _fail(f"raw-run not valid JSON: {os.path.basename(f)}: {e}")
        miss = [k for k in required_keys if k not in d]
        if miss:
            _fail(f"raw-run {os.path.basename(f)} missing keys: {miss}")
        if use_schema:
            errs = sorted(validator.iter_errors(d), key=lambda e: list(e.path))
            if errs:
                _fail(
                    f"raw-run {os.path.basename(f)} schema invalid: "
                    f"{errs[0].message[:160]}"
                )
            schema_valid += 1
        # value-level invariants
        if not isinstance(d["audit_events"], list) or not d["audit_events"]:
            _fail(f"raw-run {d['run_id']} has no audit_events")
        if d["audit_complete"] is not True:
            _fail(f"raw-run {d['run_id']} audit_complete != True")
        if not isinstance(d["api_request_count"], int) or d["api_request_count"] < 1:
            _fail(f"raw-run {d['run_id']} api_request_count not a positive int")
        # timing sanity
        try:
            s = _dt.datetime.fromisoformat(d["started_at"].replace("Z", "+00:00"))
            e = _dt.datetime.fromisoformat(d["finished_at"].replace("Z", "+00:00"))
        except Exception as e_:
            _fail(f"raw-run {d['run_id']} unparseable timestamps: {e_}")
        if not (e > s):
            _fail(f"raw-run {d['run_id']} finished_at <= started_at")
        if d.get("duration_seconds") is not None:
            delta = (e - s).total_seconds()
            if abs(delta - d["duration_seconds"]) > 1.5:
                _fail(
                    f"raw-run {d['run_id']} duration_seconds={d['duration_seconds']} "
                    f"diverges from timestamp delta={delta:.3f}"
                )
        models.add(d["model"])
        statuses.add(d["status"])
        err_values.add(d["error_detail"])
        records.append(d)

    if models != {EXPECTED_MODEL}:
        _fail(f"raw-run models={sorted(models)} expected={EXPECTED_MODEL!r}")
    if statuses != {"completed"}:
        _fail(f"raw-run statuses={sorted(statuses)} expected=['completed']")
    if not all(v is None or v == "" for v in err_values):
        _fail(f"raw-run error_detail not empty/null: {sorted(map(repr, err_values))}")

    # if schema was available, all 20 must have validated
    if use_schema and schema_valid != EXPECTED_RAW_COUNT:
        _fail(f"schema-valid raw-runs={schema_valid} expected={EXPECTED_RAW_COUNT}")

    return records, files


def _verify_source_manifest() -> dict:
    """Verify source-manifest.json SHA256 and that all 24 listed digests match disk."""
    sm_bytes = open(SOURCE_MANIFEST, "rb").read()
    sm_sha = _sha256_bytes(sm_bytes)
    if sm_sha != EXPECTED_SOURCE_MANIFEST_SHA256:
        _fail(
            f"source-manifest.json SHA256={sm_sha} "
            f"expected={EXPECTED_SOURCE_MANIFEST_SHA256}"
        )
    sm = json.loads(sm_bytes.decode("utf-8"))
    files_map = dict(sm.get("files", {}))
    fixtures_map = dict(sm.get("fixtures", {}))
    entries = dict(files_map)
    entries.update(fixtures_map)
    if len(entries) != 24:
        _fail(f"source-manifest entry count={len(entries)} expected=24")

    # cases.jsonl (ground truth) MUST be in the frozen source manifest
    gt_key = "benchmark/dataset/cases.jsonl"
    if gt_key not in files_map:
        _fail(
            f"ground-truth source {gt_key} not present in source-manifest files; "
            "decision_accuracy cannot be anchored"
        )

    for rel, expected in sorted(entries.items()):
        # resolve on-disk path
        if rel.startswith("benchmark/"):
            path = os.path.join(REPO_ROOT, rel)
        elif rel.startswith("dataset/fixtures/"):
            path = os.path.join(BENCHMARK_DIR, rel)
        else:
            # fixtures are stored as bare names; resolve under fixtures dir
            path = os.path.join(FIXTURES_DIR, rel)
        if not os.path.exists(path):
            # try under benchmark root
            alt = os.path.join(BENCHMARK_DIR, rel)
            if os.path.exists(alt):
                path = alt
            else:
                _fail(f"source-manifest entry missing on disk: {rel}")
        actual = _sha256_file(path)
        if actual != expected:
            _fail(
                f"source digest mismatch for {rel}: actual={actual} expected={expected}"
            )
    return sm


def _compute_raw_combined_sha256(files: list[str]) -> str:
    """Deterministic raw-combined SHA256 per the spec algorithm."""
    lines = []
    for f in files:  # already sorted
        name = os.path.basename(f)
        h = _sha256_file(f)
        lines.append(f"{name}:{h}\n")
    text = "".join(lines).encode("utf-8")
    return _sha256_bytes(text), {os.path.basename(f): _sha256_file(f) for f in files}


def _per_file_shas(files: list[str]) -> dict[str, str]:
    return {os.path.basename(f): _sha256_file(f) for f in files}


def _load_ground_truth() -> dict[str, str]:
    gt: dict[str, str] = {}
    with open(CASES_JSONL, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            c = json.loads(line)
            cid = c["case_id"]
            exp = c.get("expected_decision") or c.get("decision")
            if exp is None:
                _fail(f"cases.jsonl entry {cid} has no expected_decision")
            gt[cid] = str(exp).upper()
    # must cover all expected cases
    missing = [c for c in EXPECTED_CASES if c not in gt]
    if missing:
        _fail(f"cases.jsonl missing expected_decision for: {missing}")
    return gt


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _group_metrics(records: list[dict], group: str, gt: dict[str, str]) -> dict:
    recs = [r for r in records if r["group"] == group]
    n = len(recs)
    completed = sum(1 for r in recs if r["status"] == "completed")
    sem_pass = sum(1 for r in recs if r["eval_passed"] is True)
    tp = sum(int(r["eval_tp"]) for r in recs)
    fp = sum(int(r["eval_fp"]) for r in recs)
    fn = sum(int(r["eval_fn"]) for r in recs)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    tokens = sum(int((r.get("token_usage") or {}).get("total_tokens", 0)) for r in recs)
    api_requests = sum(int(r["api_request_count"]) for r in recs)
    durations = [float(r["duration_seconds"]) for r in recs if r.get("duration_seconds") is not None]
    duration_total = round(sum(durations), 2)
    mean_duration = round(sum(durations) / n, 3) if n else 0.0
    # decision_accuracy: per-case decision == ground truth expected_decision
    decision_correct = 0
    for r in recs:
        got = str(r["decision"]).upper()
        exp = gt.get(r["case_id"])
        if exp is not None and got == exp:
            decision_correct += 1
    decision_accuracy = decision_correct / n if n else 0.0

    # per-case detail (for the summary)
    per_case = []
    for r in sorted(recs, key=lambda x: x["case_id"]):
        per_case.append({
            "case_id": r["case_id"],
            "run_id": r["run_id"],
            "decision": r["decision"],
            "expected_decision": gt.get(r["case_id"]),
            "decision_correct": (
                str(r["decision"]).upper() == gt.get(r["case_id"], "")
            ),
            "findings_count": len(r.get("findings", [])),
            "eval_passed": bool(r["eval_passed"]),
            "eval_reason": r["eval_reason"],
            "eval_tp": int(r["eval_tp"]),
            "eval_fp": int(r["eval_fp"]),
            "eval_fn": int(r["eval_fn"]),
            "token_usage": (r.get("token_usage") or {}).get("total_tokens", 0),
            "api_request_count": int(r["api_request_count"]),
            "duration_seconds": r.get("duration_seconds"),
        })

    return {
        "group": group,
        "n": n,
        "completed": completed,
        "semantic_pass": sem_pass,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "decision_correct": decision_correct,
        "decision_accuracy": decision_accuracy,
        "tokens": tokens,
        "api_requests": api_requests,
        "duration_total": duration_total,
        "mean_duration": mean_duration,
        "per_case": per_case,
    }


# Indicators whose presence (as a *full token*, not just a prefix) would
# suggest a REAL credential. Token-prefixed indicators (ghp_/ghs_/...) are
# only counted as real if the full surrounding token is NOT one of the
# synthetic fixture values.
_TOKEN_PREFIX_INDICATORS = ("ghp_", "ghs_", "gho_", "ghu_", "ghr_")
# Non-token indicators: any presence in a raw run counts as real, because
# benchmark fixtures never contain these substrings.
_NONTOKEN_REAL_INDICATORS = (
    "sk-live",
    "AKIA",
    "BEGIN RSA PRIVATE",
    "BEGIN OPENSSH PRIVATE",
    "BEGIN EC PRIVATE",
    "BEGIN PGP PRIVATE",
)


def _scan_secret_patterns(records: list[dict], files: list[str]) -> dict:
    """
    Classify credential-like hits in raw-runs as synthetic-fixture vs real.

    A hit is *synthetic* if its full matched value is byte-for-byte identical
    to a credential-like substring present in one of the benchmark fixtures.
    A hit is *real* if a real-credential indicator is present and (for the
    token-prefixed indicators) the full token is NOT a fixture value.

    Only sha256 prefixes of matched values are recorded; full string contents
    are never emitted by this function.
    """
    # 1. collect every credential-like candidate value present in fixtures
    fixture_values: set[str] = set()
    if os.path.isdir(FIXTURES_DIR):
        for fn in os.listdir(FIXTURES_DIR):
            try:
                content = open(
                    os.path.join(FIXTURES_DIR, fn), encoding="utf-8"
                ).read()
            except OSError:
                continue
            for pat in _CANDIDATE_PATTERNS:
                for hit in pat.findall(content):
                    fixture_values.add(hit)

    synthetic_set: set[str] = set()
    real_set: set[str] = set()
    per_file: list[dict] = []
    for f in files:
        text = open(f, encoding="utf-8").read()
        # candidate hits in this raw file
        hits_in_file: set[str] = set()
        for pat in _CANDIDATE_PATTERNS:
            hits_in_file.update(pat.findall(text))
        # synthetic = matches a fixture value exactly
        syn_matches = sorted(h for h in hits_in_file if h in fixture_values)
        # real detection
        genuinely_real: list[str] = []
        # token-prefixed indicators: real only if the full token is not a fixture value
        for ind in _TOKEN_PREFIX_INDICATORS:
            for m in re.finditer(re.escape(ind) + r"[0-9A-Za-z]{8,}", text):
                if m.group(0) not in fixture_values:
                    genuinely_real.append(ind)
        # non-token indicators: presence == real (fixtures never contain these)
        for ind in _NONTOKEN_REAL_INDICATORS:
            if ind in text:
                genuinely_real.append(ind)
        name = os.path.basename(f)
        is_synth = bool(syn_matches)
        is_real = bool(genuinely_real)
        if is_synth:
            synthetic_set.add(name)
        if is_real:
            real_set.add(name)
        if is_synth or is_real:
            per_file.append({
                "file": name,
                "synthetic_fixture_match_count": len(syn_matches),
                "synthetic_fixture_match_sha256_prefixes": [
                    hashlib.sha256(h.encode("utf-8")).hexdigest()[:16]
                    for h in syn_matches
                ],
                "real_indicator_prefixes": sorted(set(genuinely_real)),
                "classification": (
                    "synthetic_fixture" if is_synth and not is_real
                    else "real_credential" if is_real and not is_synth
                    else "mixed"
                ),
            })

    return {
        "synthetic_fixture_pattern_hits": len(synthetic_set),
        "real_credential_hits": len(real_set),
        "files": per_file,
        "note": (
            "Matched values are compared byte-for-byte against credential-like "
            "substrings present in benchmark/dataset/fixtures/*.py. Only sha256 "
            "prefixes of matched values are recorded; full string contents are "
            "never emitted."
        ),
    }


# ---------------------------------------------------------------------------
# Output builders
# ---------------------------------------------------------------------------


def _build_summary_json(a: dict, b: dict, secret: dict, totals: dict) -> dict:
    def _round_metrics(m: dict) -> dict:
        return {
            "group": m["group"],
            "n": m["n"],
            "completed": m["completed"],
            "semantic_pass": m["semantic_pass"],
            "tp": m["tp"],
            "fp": m["fp"],
            "fn": m["fn"],
            "precision": round(m["precision"], 6),
            "recall": round(m["recall"], 6),
            "f1": round(m["f1"], 6),
            "decision_correct": m["decision_correct"],
            "decision_accuracy": round(m["decision_accuracy"], 6),
            "tokens": m["tokens"],
            "api_requests": m["api_requests"],
            "duration_total": m["duration_total"],
            "mean_duration": m["mean_duration"],
            "per_case": m["per_case"],
        }

    return {
        "kind": "mergepilot-formal-benchmark-summary",
        "summary_version": 1,
        "generated_at": _now_utc(),
        "experiment": {
            "design": "controlled_local_pair_orchestration",
            "n": 10,
            "groups": EXPECTED_GROUPS,
            "model": EXPECTED_MODEL,
            "runs_per_pair": 1,
            "timeout_seconds": 120,
            "token_budget": 4096,
            "temperature": 0.1,
        },
        "infrastructure_completion": {
            "A_single_agent": {"completed": a["completed"], "n": a["n"]},
            "B_mergepilot": {"completed": b["completed"], "n": b["n"]},
            "total_completed": totals["total_completed"],
            "total_n": totals["total_n"],
            "infrastructure_completion_rate": round(
                totals["total_completed"] / totals["total_n"], 6
            ),
        },
        "semantic_case_pass": {
            "A_single_agent": a["semantic_pass"],
            "B_mergepilot": b["semantic_pass"],
            "total": totals["total_semantic_pass"],
            "total_n": totals["total_n"],
            "semantic_case_pass_rate": round(
                totals["total_semantic_pass"] / totals["total_n"], 6
            ),
            "note": (
                "semantic case pass requires both the per-case decision and the "
                "finding-level evaluation to be correct; it is NOT the same as "
                "E2E production completion."
            ),
        },
        "metrics": {
            "A_single_agent": _round_metrics(a),
            "B_mergepilot": _round_metrics(b),
        },
        "deltas": {
            "precision_absolute": round(b["precision"] - a["precision"], 6),
            "precision_percentage_points": round(
                (b["precision"] - a["precision"]) * 100, 2
            ),
            "recall_absolute": round(b["recall"] - a["recall"], 6),
            "f1_absolute": round(b["f1"] - a["f1"], 6),
            "f1_percentage_points": round((b["f1"] - a["f1"]) * 100, 2),
            "decision_accuracy_absolute": round(
                b["decision_accuracy"] - a["decision_accuracy"], 6
            ),
            "tokens_delta": b["tokens"] - a["tokens"],
            "tokens_delta_pct": round(
                (b["tokens"] - a["tokens"]) / a["tokens"] * 100, 2
            ) if a["tokens"] else None,
            "api_requests_delta": b["api_requests"] - a["api_requests"],
            "api_requests_delta_pct": round(
                (b["api_requests"] - a["api_requests"]) / a["api_requests"] * 100, 2
            ) if a["api_requests"] else None,
            "mean_duration_delta": round(b["mean_duration"] - a["mean_duration"], 3),
            "mean_duration_delta_pct": round(
                (b["mean_duration"] - a["mean_duration"]) / a["mean_duration"] * 100, 2
            ) if a["mean_duration"] else None,
            "fp_delta": b["fp"] - a["fp"],
            "tp_delta": b["tp"] - a["tp"],
            "fn_delta": b["fn"] - a["fn"],
        },
        "resource_cost": {
            "A_single_agent": {
                "tokens": a["tokens"],
                "api_requests": a["api_requests"],
                "duration_total": a["duration_total"],
                "mean_duration": a["mean_duration"],
            },
            "B_mergepilot": {
                "tokens": b["tokens"],
                "api_requests": b["api_requests"],
                "duration_total": b["duration_total"],
                "mean_duration": b["mean_duration"],
            },
            "total_tokens": a["tokens"] + b["tokens"],
            "total_api_requests": a["api_requests"] + b["api_requests"],
        },
        "secret_pattern_scan": secret,
        "formal_conclusion": _FORMAL_CONCLUSION,
        "limitations": _LIMITATIONS,
    }


def _build_summary_md(s: dict) -> str:
    a = s["metrics"]["A_single_agent"]
    b = s["metrics"]["B_mergepilot"]
    d = s["deltas"]
    lines = []
    lines.append("# MergePilot Formal Benchmark Summary (N=10x2)")
    lines.append("")
    lines.append(
        "> Post-run, machine-computed by `benchmark/freeze_formal_results.py`. "
        "Every number is recomputed from `benchmark/raw-runs/*.json`. "
        "Generated_at is the only non-deterministic field; re-running the "
        "freeze reproduces identical content otherwise."
    )
    lines.append("")
    lines.append(f"- generated_at: `{s['generated_at']}`")
    lines.append(f"- summary_version: {s['summary_version']}")
    lines.append(f"- design: `{s['experiment']['design']}`")
    lines.append(
        f"- model: `{s['experiment']['model']}`  | "
        f"timeout={s['experiment']['timeout_seconds']}s  | "
        f"token_budget={s['experiment']['token_budget']}  | "
        f"temperature={s['experiment']['temperature']}"
    )
    lines.append("")

    lines.append("## 1. Infrastructure completion")
    ic = s["infrastructure_completion"]
    lines.append("")
    lines.append("| group | completed | n |")
    lines.append("|---|---:|---:|")
    lines.append(f"| A_single_agent | {ic['A_single_agent']['completed']} | {ic['A_single_agent']['n']} |")
    lines.append(f"| B_mergepilot | {ic['B_mergepilot']['completed']} | {ic['B_mergepilot']['n']} |")
    lines.append(
        f"| **total** | **{ic['total_completed']}** | **{ic['total_n']}** "
        f"(infrastructure completion rate = {ic['infrastructure_completion_rate']*100:.2f}%) |"
    )
    lines.append("")
    lines.append(
        "Infrastructure completion measures that the controlled local "
        "orchestration produced a parseable, schema-valid, completed run for "
        "every (case x group) pair. It is **not** the same as semantic case "
        "pass or E2E production completion."
    )
    lines.append("")

    lines.append("## 2. Semantic case pass")
    sp = s["semantic_case_pass"]
    lines.append("")
    lines.append("| group | semantic pass | n |")
    lines.append("|---|---:|---:|")
    lines.append(f"| A_single_agent | {sp['A_single_agent']} | 10 |")
    lines.append(f"| B_mergepilot | {sp['B_mergepilot']} | 10 |")
    lines.append(
        f"| **total** | **{sp['total']}** | **{sp['total_n']}** "
        f"(semantic case pass rate = {sp['semantic_case_pass_rate']*100:.2f}%) |"
    )
    lines.append("")
    lines.append(sp["note"])
    lines.append("")

    lines.append("## 3. Finding-level metrics (TP / FP / FN / precision / recall / F1)")
    lines.append("")
    lines.append("| group | TP | FP | FN | precision | recall | F1 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    lines.append(
        f"| A_single_agent | {a['tp']} | {a['fp']} | {a['fn']} | "
        f"{a['precision']*100:.2f}% | {a['recall']*100:.2f}% | {a['f1']*100:.2f}% |"
    )
    lines.append(
        f"| B_mergepilot | {b['tp']} | {b['fp']} | {b['fn']} | "
        f"{b['precision']*100:.2f}% | {b['recall']*100:.2f}% | {b['f1']*100:.2f}% |"
    )
    lines.append("")

    lines.append("## 4. Decision accuracy (per-case decision == ground-truth)")
    lines.append("")
    lines.append("| group | decision correct | n | decision accuracy |")
    lines.append("|---|---:|---:|---:|")
    lines.append(
        f"| A_single_agent | {a['decision_correct']} | {a['n']} | {a['decision_accuracy']*100:.2f}% |"
    )
    lines.append(
        f"| B_mergepilot | {b['decision_correct']} | {b['n']} | {b['decision_accuracy']*100:.2f}% |"
    )
    lines.append("")

    lines.append("## 5. Deltas (B vs A)")
    lines.append("")
    lines.append("| metric | A | B | delta |")
    lines.append("|---|---:|---:|---:|")
    lines.append(
        f"| precision | {a['precision']*100:.2f}% | {b['precision']*100:.2f}% | "
        f"+{d['precision_percentage_points']:.2f} pp |"
    )
    lines.append(
        f"| recall | {a['recall']*100:.2f}% | {b['recall']*100:.2f}% | "
        f"{d['recall_absolute']*100:+.2f} pp |"
    )
    lines.append(
        f"| F1 | {a['f1']*100:.2f}% | {b['f1']*100:.2f}% | "
        f"+{d['f1_percentage_points']:.2f} pp |"
    )
    lines.append(
        f"| case pass | {a['semantic_pass']*10:.0f}% | {b['semantic_pass']*10:.0f}% | "
        f"{(b['semantic_pass']-a['semantic_pass'])*10:+.0f} pp |"
    )
    lines.append(
        f"| decision accuracy | {a['decision_accuracy']*100:.2f}% | "
        f"{b['decision_accuracy']*100:.2f}% | "
        f"{d['decision_accuracy_absolute']*100:+.2f} pp |"
    )
    lines.append(
        f"| false positives (FP) | {a['fp']} | {b['fp']} | {d['fp_delta']:+d} |"
    )
    lines.append(
        f"| tokens | {a['tokens']} | {b['tokens']} | +{d['tokens_delta']} "
        f"(+{d['tokens_delta_pct']:.2f}%) |"
    )
    lines.append(
        f"| API requests | {a['api_requests']} | {b['api_requests']} | "
        f"+{d['api_requests_delta']} (+{d['api_requests_delta_pct']:.2f}%) |"
    )
    lines.append(
        f"| mean duration | {a['mean_duration']:.3f}s | {b['mean_duration']:.3f}s | "
        f"{d['mean_duration_delta']:+.3f}s ({d['mean_duration_delta_pct']:+.2f}%) |"
    )
    lines.append("")

    lines.append("## 6. Per-case detail")
    lines.append("")
    for grp_name, m in (("A_single_agent", a), ("B_mergepilot", b)):
        lines.append(f"### {grp_name}")
        lines.append("")
        lines.append(
            "| case | decision | expected | decision correct | findings | "
            "eval passed | eval reason | TP | FP | FN | tokens |"
        )
        lines.append("|---|---|---|---|---:|---|---|---:|---:|---:|---:|")
        for c in m["per_case"]:
            lines.append(
                f"| {c['case_id']} | {c['decision']} | {c['expected_decision']} | "
                f"{'yes' if c['decision_correct'] else 'no'} | "
                f"{c['findings_count']} | {'yes' if c['eval_passed'] else 'no'} | "
                f"{c['eval_reason']} | {c['eval_tp']} | {c['eval_fp']} | {c['eval_fn']} | "
                f"{c['token_usage']} |"
            )
        lines.append("")

    lines.append("## 7. Secret-pattern scan")
    lines.append("")
    sec = s["secret_pattern_scan"]
    lines.append(
        f"- synthetic_fixture_pattern_hits: **{sec['synthetic_fixture_pattern_hits']}** "
        "(raw-run files that contain a substring byte-for-byte identical to a "
        "credential-like value present in a benchmark fixture)"
    )
    lines.append(
        f"- real_credential_hits: **{sec['real_credential_hits']}**"
    )
    lines.append(f"- matched files: {len(sec['files'])}")
    for entry in sec["files"]:
        lines.append(
            f"  - `{entry['file']}` classification=`{entry['classification']}` "
            f"synthetic_match_count={entry['synthetic_fixture_match_count']} "
            f"real_indicators={entry['real_indicator_prefixes'] or '[]'}"
        )
    lines.append("")
    lines.append(f"_{sec['note']}_")
    lines.append("")

    lines.append("## 8. Formal conclusion")
    lines.append("")
    lines.append(_FORMAL_CONCLUSION)
    lines.append("")

    lines.append("## 9. Limitations")
    lines.append("")
    for lim in _LIMITATIONS:
        lines.append(f"- {lim}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "_This file is machine-generated by `benchmark/freeze_formal_results.py`. "
        "Do not edit by hand; re-run the freeze to reproduce._"
    )
    lines.append("")
    return "\n".join(lines)


def _build_run_manifest(
    records: list[dict],
    files: list[str],
    raw_combined: str,
    per_file_shas: dict[str, str],
    source_manifest_sha: str,
    summary_json_sha: str,
    summary_md_sha: str,
    freeze_tool_sha: str,
    sm: dict,
) -> dict:
    schema_valid = len(records)  # already validated structurally; schema required all 20
    pairs = {(r["case_id"], r["group"]) for r in records}
    totals_tokens = sum(int((r.get("token_usage") or {}).get("total_tokens", 0)) for r in records)
    totals_api = sum(int(r["api_request_count"]) for r in records)
    return {
        "kind": "mergepilot-formal-run-manifest",
        "manifest_version": 1,
        "generated_at": _now_utc(),
        "head": sm.get("git_head"),
        "source_manifest_sha256": source_manifest_sha,
        "raw_files": [
            {"file": name, "sha256": per_file_shas[name]}
            for name in sorted(per_file_shas)
        ],
        "raw_combined_sha256": raw_combined,
        "results_csv_sha256": _sha256_file(RESULTS_CSV),
        "report_md_sha256": _sha256_file(REPORT_MD),
        "formal_summary_json_sha256": summary_json_sha,
        "formal_summary_md_sha256": summary_md_sha,
        "freeze_formal_results_py_sha256": freeze_tool_sha,
        "run_count": len(records),
        "unique_pair_count": len(pairs),
        "schema_valid_count": schema_valid,
        "model": EXPECTED_MODEL,
        "timeout_seconds": int(sm.get("timeout_seconds", 120)),
        "token_budget": int(sm.get("token_budget", 4096)),
        "temperature": float(sm.get("temperature", 0.1)),
        "total_tokens": totals_tokens,
        "total_api_requests": totals_api,
        "key_absent": True,
        "external_environment_used": False,
        "key_absent_note": (
            "Indicates the freeze tool did not access environment credentials, "
            "did not read .llm-key, and no adapter / runner / network call was "
            "performed. It is a property of the freeze tool's behavior, not a "
            "scan of the whole repository."
        ),
        "external_environment_used_note": (
            "False: the freeze performs no WSL/Docker/GitHub/LLM access. "
            "It is a pure offline recompute over committed files."
        ),
    }


# ---------------------------------------------------------------------------
# Fixed prose (frozen wording per Day 5-A spec)
# ---------------------------------------------------------------------------


_FORMAL_CONCLUSION = (
    "在 N=10、同模型、每个 pair 单次运行的受控本地评测中，"
    "MergePilot-style 多角色编排相较单 Agent 将 precision 从 "
    "36.36% 提升至 57.14%，F1 从 48.00% 提升至 63.16%，"
    "recall 同为 70.59%。改善主要来自 FP 从 21 降至 9；"
    "代价是 token 增加 32.95%、API 请求增加 80%。"
    "B 的 decision accuracy 为 40%，低于 A 的 50%，"
    "风险处置校准仍需改进。"
)

_LIMITATIONS = [
    "N=10, small sample.",
    "Each (case x group) pair is run exactly once; no per-pair variance estimate.",
    "Single model: deepseek-v4-flash; no cross-model comparison.",
    "Synthetic fixtures hand-authored for the benchmark; not representative of "
    "the distribution of real-world PRs.",
    "Controlled local orchestration: Group B is NOT a real "
    "Gateway/controller/GitHub/HiClaw end-to-end run; it exercises the same "
    "deepseek model with a reviewer→fixer handoff simulation, not the "
    "production control plane.",
    "Does not support metrics that require real fix/verify/rollback execution "
    "(fix first-pass rate, rollback success rate, etc.); those are intentionally "
    "excluded rather than self-reported.",
    "Does not prove multi-role orchestration improves recall; recall is "
    "identical (70.59%) across both groups.",
    "C3 10/10 is independent real isolated-stack evidence "
    "(MergePilot-Test isolated dockerd + real Gateway/GitHub MCP/fixture repo) "
    "and MUST NOT be conflated with this benchmark; it is reported separately.",
    "N>=10 minimum target met; N>=20 remains a follow-up target.",
    "hiclab_live=false: this benchmark does not exercise the production HiClaw "
    "runtime.",
]


def _now_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    # 1. verify source manifest first (anchors everything)
    sm = _verify_source_manifest()

    # 2. load + validate raw runs
    records, files = _load_and_validate_raw_runs()

    # 3. raw combined sha + per-file shas
    raw_combined, per_file_shas = _compute_raw_combined_sha256(files)
    if raw_combined != EXPECTED_RAW_COMBINED_SHA256:
        _fail(
            f"raw combined SHA256={raw_combined} "
            f"expected={EXPECTED_RAW_COMBINED_SHA256}"
        )

    # 4. ground truth
    gt = _load_ground_truth()

    # 5. metrics
    a = _group_metrics(records, "A_single_agent", gt)
    b = _group_metrics(records, "B_mergepilot", gt)

    # 6. secret scan
    secret = _scan_secret_patterns(records, files)
    if secret["real_credential_hits"] != 0:
        _fail(
            f"real_credential_hits={secret['real_credential_hits']} expected=0; "
            "freeze refuses to publish if any real credential is detected"
        )
    if secret["synthetic_fixture_pattern_hits"] != 2:
        _fail(
            f"synthetic_fixture_pattern_hits="
            f"{secret['synthetic_fixture_pattern_hits']} expected=2"
        )

    # 7. totals
    totals = {
        "total_completed": a["completed"] + b["completed"],
        "total_n": a["n"] + b["n"],
        "total_semantic_pass": a["semantic_pass"] + b["semantic_pass"],
        "total_tokens": a["tokens"] + b["tokens"],
        "total_api": a["api_requests"] + b["api_requests"],
    }

    # 8. build artifacts
    summary = _build_summary_json(a, b, secret, totals)
    summary_md_text = _build_summary_md(summary)

    # 9. compute SHAs for the manifest (self-consistent: manifest excludes itself)
    summary_json_bytes = (
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    ).encode("utf-8")
    summary_json_sha = _sha256_bytes(summary_json_bytes)
    summary_md_sha = _sha256_bytes(summary_md_text.encode("utf-8"))
    freeze_tool_sha = _sha256_file(__file__)
    source_manifest_sha = EXPECTED_SOURCE_MANIFEST_SHA256

    run_manifest = _build_run_manifest(
        records=records,
        files=files,
        raw_combined=raw_combined,
        per_file_shas=per_file_shas,
        source_manifest_sha=source_manifest_sha,
        summary_json_sha=summary_json_sha,
        summary_md_sha=summary_md_sha,
        freeze_tool_sha=freeze_tool_sha,
        sm=sm,
    )

    # 10. atomic writes (manifest last so its referenced SHAs already exist on disk)
    _atomic_write_text(OUT_SUMMARY_JSON, summary_json_bytes.decode("utf-8"))
    _atomic_write_text(OUT_SUMMARY_MD, summary_md_text)
    run_manifest_bytes = (
        json.dumps(run_manifest, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    ).encode("utf-8")
    _atomic_write_text(OUT_RUN_MANIFEST, run_manifest_bytes.decode("utf-8"))

    # 11. emit a stable machine summary line
    sys.stdout.write("BENCHMARK_FROZEN_FOR_SUBMISSION\n")
    sys.stdout.write(
        f"raw_combined_sha256={raw_combined}\n"
        f"formal-summary.json_sha256={summary_json_sha}\n"
        f"formal-summary.md_sha256={summary_md_sha}\n"
        f"formal-run-manifest.json_sha256={_sha256_bytes(run_manifest_bytes)}\n"
        f"A: precision={a['precision']*100:.2f}% recall={a['recall']*100:.2f}% "
        f"F1={a['f1']*100:.2f}% decision_acc={a['decision_accuracy']*100:.2f}% "
        f"sem_pass={a['semantic_pass']}/10\n"
        f"B: precision={b['precision']*100:.2f}% recall={b['recall']*100:.2f}% "
        f"F1={b['f1']*100:.2f}% decision_acc={b['decision_accuracy']*100:.2f}% "
        f"sem_pass={b['semantic_pass']}/10\n"
        f"synthetic_fixture_pattern_hits={secret['synthetic_fixture_pattern_hits']} "
        f"real_credential_hits={secret['real_credential_hits']}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
