#!/usr/bin/env python3
"""M5-0D final artifact binding — validates evidence + runs evaluator + emits attestation.

Solves the source_commit SHA self-reference problem:
  1. All code commits complete → final code HEAD
  2. Evidence collected at that HEAD (source_commit == HEAD)
  3. Evaluator runs at same HEAD (code_facts derived from committed code, NOT hardcoded)
  4. Attestation generated to repo-external path (not in git)
  5. Annotated tag message binds source_commit + all digests

TOCTOU-safe: each evidence file is opened with O_NOFOLLOW, fstat'd, then read
from the same fd for BOTH JSON parsing AND SHA-256 computation. No re-open.

Semantic validation: after schema validation, the finalizer calls each capture
module's domain-specific validate function (validate_offline, validate_otel,
validate_production). Any failure → fail-closed, no attestation.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile

ROOT = os.environ.get("M5_0D_ROOT", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SCHEMA_DIR = os.path.join(ROOT, "tests", "m5_0d", "schemas")
EVIDENCE_DIR = os.path.join(ROOT, "evidence", "m5", "0d")
OFFLINE_PATH = os.path.join(EVIDENCE_DIR, "offline-regression.json")
OTEL_PATH = os.path.join(EVIDENCE_DIR, "otel-sls.json")
PRODUCTION_PATH = os.path.join(EVIDENCE_DIR, "production-live.json")
C3_PATH = os.path.join(ROOT, "evidence", "m5", "0c", "c3-10x.json")

# Exact evidence artifacts permitted in the working tree (by path). Any other
# path is a dirty tree. Evidence must NOT be staged for commit (index status
# must be ' ' = worktree-only, or '?' = untracked): staged evidence would enter
# the release commit, re-introducing the source_commit self-reference. Evidence
# integrity is bound by the repo-external attestation (digest + source_commit),
# NOT by a commit. C3 is tracked from D1 history; it is permitted as a
# worktree-modified artifact (re-captured at HEAD) OR untracked (post-migration).
EVIDENCE_ARTIFACTS = frozenset({
    "evidence/m5/0c/c3-10x.json",
    "evidence/m5/0d/offline-regression.json",
    "evidence/m5/0d/production-live.json",
    "evidence/m5/0d/otel-sls.json",
})

# Import capture validators + evaluator code_facts loader (same dir)
sys.path.insert(0, os.path.join(ROOT, "tests", "m5_0d"))
import capture_offline_evidence as CO  # noqa: E402
import capture_otel_sls as CT  # noqa: E402
import capture_production_live as CP  # noqa: E402
import capture_c3_evidence as CC  # noqa: E402
import hiclaw_live_runner as H  # noqa: E402


def _git_head():
    r = subprocess.run(
        ["git", "-c", "safe.directory=" + ROOT, "-C", ROOT, "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=15)
    s = r.stdout.strip()
    return s if r.returncode == 0 and len(s) == 40 else ""


def _classify_tree(porcelain_lines):
    """Classify `git status --porcelain -uall` lines. Returns (ok, err).

    Allows ONLY exact evidence artifacts (EVIDENCE_ARTIFACTS), and only when NOT
    staged for commit. Staged evidence (index status not ' '/'?') is rejected
    because it would enter the release commit (source_commit self-reference).
    Any non-evidence path is a dirty tree. Arbitrary tracked modifications are
    never silently ignored — only the named evidence artifacts are permitted,
    and each is bound by digest in the attestation."""
    for ln in porcelain_lines:
        if len(ln) < 4:
            continue
        index_status = ln[0]
        path = ln[3:].strip().strip('"')
        if " -> " in path:  # rename: R  old -> new
            path = path.split(" -> ", 1)[1].strip().strip('"')
        if path not in EVIDENCE_ARTIFACTS:
            return False, "dirty tree (non-evidence path): %s" % ln
        if index_status not in (" ", "?"):
            return False, "evidence must not be staged for commit: %s" % ln
    return True, None


def _check_clean_tree():
    r = subprocess.run(
        ["git", "-c", "safe.directory=" + ROOT, "-C", ROOT,
         "status", "--porcelain", "-uall"],
        capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        return False, "git status failed"
    lines = [ln for ln in r.stdout.strip().splitlines() if ln.strip()]
    return _classify_tree(lines)


def read_evidence_toctou_safe(path):
    """Open with O_NOFOLLOW (POSIX) + lstat/fstat identity check (cross-platform),
    read content from the same fd for BOTH JSON parsing AND SHA-256. No re-open.

    On POSIX, O_NOFOLLOW rejects symlinks at open(2). On Windows O_NOFOLLOW is
    unavailable (getattr → 0), so the primary defense there is the lstat-vs-fstat
    identity comparison: if the path was replaced between lstat and open, the two
    stat results describe different inodes and we fail-closed.
    Returns (content_bytes, parsed_json, sha256_hex). Raises on any anomaly."""
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise ValueError("symlink rejected: %s" % path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("not regular file: %s" % path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode):
            raise ValueError("fstat: not regular after open: %s" % path)
        # Cross-platform TOCTOU identity check. st_ino is 0 on some Windows
        # filesystems but st_dev/st_size/st_mtime_ns still catch replacement.
        for attr in ("st_dev", "st_ino", "st_size", "st_mtime_ns"):
            if getattr(info, attr) != getattr(fst, attr):
                raise ValueError(
                    "TOCTOU identity mismatch %s (lstat!=fstat): %s" % (attr, path))
        content = b""
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            content += chunk
    finally:
        os.close(fd)
    digest = hashlib.sha256(content).hexdigest()
    data = json.loads(content.decode("utf-8"))
    return content, data, digest


def _validate_schema(data, schema_file):
    sp = os.path.join(SCHEMA_DIR, schema_file)
    if not os.path.exists(sp):
        return False, "schema file missing: %s" % schema_file
    try:
        import jsonschema
        schema = json.load(open(sp, encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(schema).validate(data)
        return True, None
    except ImportError:
        return False, "jsonschema validator unavailable"
    except Exception as exc:
        return False, str(exc)[:200]


def finalize():
    sc = _git_head()
    if not sc:
        print("FATAL: cannot resolve git HEAD"); return 2
    clean_ok, clean_err = _check_clean_tree()
    if not clean_ok:
        print("FATAL: %s" % clean_err); return 2

    evidence_specs = [
        ("offline", OFFLINE_PATH, "offline-regression.schema.json"),
        ("otel", OTEL_PATH, "otel-sls.schema.json"),
        ("production", PRODUCTION_PATH, "production-live.schema.json"),
    ]
    parsed = {}
    digests = {}
    for name, path, schema_file in evidence_specs:
        print("  validating %s..." % name)
        try:
            _content, data, digest = read_evidence_toctou_safe(path)
        except Exception as exc:
            print("FATAL: %s TOCTOU read failed: %s" % (name, str(exc)[:120])); return 2
        parsed[name] = data
        digests[name] = digest
        ev_sc = data.get("source_commit", "")
        if ev_sc != sc:
            print("FATAL: %s source_commit=%s != HEAD=%s" % (name, ev_sc[:12], sc[:12])); return 2
        ok, err = _validate_schema(data, schema_file)
        if not ok:
            print("FATAL: %s schema invalid: %s" % (name, err)); return 2

    # Semantic validation via each capture module's domain validator
    print("  semantic validation...")
    off_data = parsed["offline"]
    off_ok, off_errs = CO.validate_offline(
        off_data.get("m4f_gates", []), off_data.get("legacy_runs", []), sc, sc)
    if not off_ok:
        print("FAIL: offline semantic: %s" % "; ".join(off_errs)); return 1

    otel_data = parsed["otel"]
    otel_ok, otel_errs = CT.validate_otel(
        otel_data.get("spans", []), otel_data.get("sls_schema", {}), otel_data.get("provenance", {}))
    if not otel_ok:
        print("FAIL: otel semantic: %s" % "; ".join(otel_errs)); return 1

    prod_data = parsed["production"]
    prod_ok, prod_errs = CP.validate_production(prod_data, sc)
    if not prod_ok:
        print("FAIL: production semantic: %s" % "; ".join(prod_errs)); return 1

    # Load C3 evidence
    try:
        _c3_content, c3_data, c3_digest = read_evidence_toctou_safe(C3_PATH)
    except Exception as exc:
        print("FATAL: C3 evidence read failed: %s" % str(exc)[:120]); return 2
    if c3_data.get("source_commit", "") != sc:
        print("FATAL: C3 source_commit mismatch"); return 2
    digests["c3"] = c3_digest

    # C3 semantic validation (gate/n_runs/n_pass/all_pass/state_stable/per-run
    # residue) — same domain-validator treatment as offline/OTel/production.
    c3_ok, c3_errs = CC.validate_summary(c3_data, c3_data.get("source_commit", ""), sc)
    if not c3_ok:
        print("FAIL: C3 semantic: %s" % "; ".join(c3_errs)); return 1

    # Run evaluator with code_facts from committed code (NOT hardcoded)
    print("\n=== Running hiclaw_live_runner (code_facts from _load_code_facts) ===")
    code_facts = H._load_code_facts()
    hl, results = H.evaluate(c3_data, prod_data, parsed["offline"], parsed["otel"], code_facts, sc)
    off_v, _ = H.check_offline(parsed["offline"])
    otel_v, _ = H.check_otel(parsed["otel"])
    true_count = sum(1 for r in results if r["value"] == "true")
    false_count = sum(1 for r in results if r["value"] == "false")
    unproven_count = sum(1 for r in results if r["value"] == "unproven")
    print("  hiclaw_live=%s true=%d false=%d unproven=%d offline=%s otel=%s" % (
        hl, true_count, false_count, unproven_count, off_v, otel_v))
    print("  code_facts=%s" % code_facts)

    if not hl or true_count != 22 or false_count > 0 or unproven_count > 0:
        print("FAIL: hiclaw_live=true requires 22/22 true, 0 false, 0 unproven"); return 1

    eval_obj = {"hiclaw_live": hl, "formulas_true": true_count,
                "formulas_false": false_count, "formulas_unproven": unproven_count,
                "offline_gate": off_v, "otel_gate": otel_v}
    eval_digest = hashlib.sha256(json.dumps(eval_obj, sort_keys=True).encode()).hexdigest()

    import datetime as _dt
    attestation = {
        "schema_version": "1", "source_commit": sc,
        "created_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "evidence_sha256": digests, "evaluator_sha256": eval_digest,
        "evidence_source_commits": {
            "offline": parsed["offline"].get("source_commit", ""),
            "otel": parsed["otel"].get("source_commit", ""),
            "production": parsed["production"].get("source_commit", ""),
            "c3": c3_data.get("source_commit", ""),
        },
        "hiclaw_live": hl, "formulas_true": true_count,
        "formulas_false": false_count, "formulas_unproven": unproven_count,
    }
    attestation_dir = tempfile.gettempdir()
    attestation_path = os.path.join(attestation_dir, "m5-0d-attestation-%s.json" % sc[:12])
    blob = json.dumps(attestation, indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(dir=attestation_dir, prefix=".attestation-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(blob)
        os.chmod(tmp, 0o644)
        os.replace(tmp, attestation_path)
    except Exception as exc:
        try: os.remove(tmp)
        except OSError: pass
        print("FAIL: attestation publish: %s" % str(exc)[:160]); return 1

    print("\n=== ATTESTATION PUBLISHED ===")
    print("  path: %s" % attestation_path)
    print("  source_commit: %s" % sc)
    print("  hiclaw_live: %s" % hl)
    print("  formulas: 22/22 true")
    for name, d in digests.items():
        print("    %s: %s" % (name, d))
    print("  evaluator: %s" % eval_digest)
    print("\n=== SUGGESTED ANNOTATED TAG MESSAGE ===")
    print("m5-0d-closed: hiclaw_live=true (22/22)")
    print("source_commit=%s" % sc)
    print("offline=%s" % digests["offline"])
    print("otel=%s" % digests["otel"])
    print("production=%s" % digests["production"])
    print("c3=%s" % digests["c3"])
    print("evaluator=%s" % eval_digest)
    return 0


if __name__ == "__main__":
    sys.exit(finalize())
