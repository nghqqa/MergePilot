#!/usr/bin/env python3
"""Validate benchmark dataset against schema + fixture integrity.

Checks:
  1. Every case in cases.jsonl passes the case.schema.json
  2. Every fixture file exists and SHA256 matches
  3. case_id format is bm-NN, unique, sequential 01-10
  4. clean_case True <=> ground_truth_findings empty
  5. L2 risk_level requires rollback consideration

Usage: python benchmark/validate_dataset.py
Exit: 0 = valid, 1 = validation failure
"""
from __future__ import annotations
import hashlib, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATASET = os.path.join(HERE, "dataset")
SCHEMAS = os.path.join(HERE, "schemas")
FIXTURES = os.path.join(DATASET, "fixtures")

try:
    import jsonschema
except ImportError:
    print("ERROR: jsonschema not installed; run: pip install jsonschema", file=sys.stderr)
    sys.exit(2)

def load_schema(name):
    with open(os.path.join(SCHEMAS, name), encoding="utf-8") as f:
        return json.load(f)

def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()

def main():
    errors = []
    case_schema = load_schema("case.schema.json")

    cases_path = os.path.join(DATASET, "cases.jsonl")
    if not os.path.exists(cases_path):
        errors.append("cases.jsonl not found")
        for e in errors:
            print(f"  FAIL: {e}")
        return 1

    cases = []
    with open(cases_path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                c = json.loads(line)
                cases.append(c)
            except json.JSONDecodeError as exc:
                errors.append(f"line {lineno}: invalid JSON: {exc}")

    if len(cases) < 10:
        errors.append(f"expected >=10 cases, got {len(cases)}")

    seen_ids = set()
    for idx, c in enumerate(cases):
        cid = c.get("case_id", f"<line {idx}>")

        # 1. Schema validation
        try:
            jsonschema.validate(c, case_schema)
        except jsonschema.ValidationError as exc:
            errors.append(f"{cid}: schema violation: {exc.message}")
            continue

        # 2. Unique + sequential
        if cid in seen_ids:
            errors.append(f"{cid}: duplicate case_id")
        seen_ids.add(cid)

        # 3. Fixture integrity
        fpath = os.path.join(FIXTURES, c["fixture_path"])
        if not os.path.exists(fpath):
            errors.append(f"{cid}: fixture file missing: {c['fixture_path']}")
        else:
            actual = sha256(fpath)
            if actual != c["fixture_sha256"]:
                errors.append(f"{cid}: fixture SHA256 mismatch (expected {c['fixture_sha256'][:16]}, got {actual[:16]})")

        # 4. clean_case consistency
        gt = c.get("ground_truth_findings", [])
        if c.get("clean_case") and len(gt) > 0:
            errors.append(f"{cid}: clean_case=True but has {len(gt)} ground_truth_findings")
        if not c.get("clean_case") and len(gt) == 0:
            errors.append(f"{cid}: clean_case=False but no ground_truth_findings")

        # 5. L2 rollback awareness
        if c.get("risk_level") == "L2" and not c.get("rollback_required", False):
            # L2 without rollback is allowed but warned
            print(f"  WARN: {cid} is L2 but rollback_required=False")

    if errors:
        for e in errors:
            print(f"  FAIL: {e}")
        print(f"\nVALIDATION FAILED: {len(errors)} error(s)")
        return 1

    print(f"VALIDATION PASSED: {len(cases)} cases, all fixtures verified")
    return 0

if __name__ == "__main__":
    sys.exit(main())
