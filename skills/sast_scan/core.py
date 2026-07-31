"""SASTScan core -- structured, deduplicated, deterministic SAST findings.

Framework-neutral (stdlib + jsonschema for ruleset validation).

Hard guarantees (this round):
* No raw secret in any digest: ``evidence_digest`` and ``input_digest`` are built
  only from safe normalized material (engine/rule/path/line/column/match_length/
  ordinal). ``input_digest`` first redacts every secret match to a stable token.
* Frozen hard limits (HARD_MAX_*) that the request can only LOWER; exceeding them
  is INVALID_INPUT. Byte limits count UTF-8 bytes (not python chars).
* All three v1 engines (secret/ast_python/dep_vuln) always run -- no caller
  degradation. Required-engine failure -> ERROR; Python SyntaxError -> PARTIAL.
* Component-level path safety (symlink/junction/reparse rejected at every level).
* Fail-closed on corrupt/unknown-version/duplicate-id/duplicate-advisory ruleset.
* Cooperative deadline (``deadline.check()``) checked in bounded loops.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat

SCHEMA_VERSION = "1"
SUPPORTED_RULES_MAJOR = 1

# Frozen hard limits (request options may only lower these).
HARD_MAX_FILES = 2000
HARD_MAX_BYTES_PER_FILE = 256 * 1024
HARD_MAX_TOTAL_BYTES = 2 * 1024 * 1024
HARD_MAX_FINDINGS = 500

RULESET_INVALID = "SAST_SCAN_RULESET_INVALID"
RULESET_VERSION_UNSUPPORTED = "SAST_SCAN_RULESET_VERSION_UNSUPPORTED"
INPUT_INVALID = "SAST_SCAN_INPUT_INVALID"
INPUT_TOO_LARGE = "SAST_SCAN_INPUT_TOO_LARGE"
PATH_ESCAPE = "SAST_SCAN_PATH_ESCAPE"
ENGINE_FAILED = "SAST_SCAN_ENGINE_FAILED"
TRUSTED_CONFIG_MISSING = "SAST_SCAN_TRUSTED_CONFIG_MISSING"

_FILE_ATTR_REPARSE = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT (Windows)


class SASTScanError(Exception):
    def __init__(self, subcode, detail=""):
        super().__init__(subcode)
        self.subcode = subcode
        self.detail = detail


_HERE = os.path.dirname(os.path.abspath(__file__))
_RULES_SCHEMA_PATH = os.path.join(_HERE, "schema", "rules.schema.json")
DEFAULT_RULES_PATH = os.path.join(_HERE, "rules", "sast-rules.v1.json")
_SCHEMA_VALIDATOR = None


def _schema_validator():
    global _SCHEMA_VALIDATOR
    if _SCHEMA_VALIDATOR is None:
        import jsonschema
        with open(_RULES_SCHEMA_PATH, encoding="utf-8") as fh:
            _SCHEMA_VALIDATOR = jsonschema.Draft202012Validator(json.load(fh))
    return _SCHEMA_VALIDATOR


def _check_deadline(deadline):
    if deadline is not None:
        deadline.check()


def _utf8_bytes(s):
    return len(s.encode("utf-8", "replace"))


def _canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(data):
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    return hashlib.sha256(data).hexdigest()


def _is_reparse(path):
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _FILE_ATTR_REPARSE)


def _assert_no_reparse_chain(path):
    """Reject symlink/junction/reparse at the path OR any ancestor, using
    os.path.dirname iteration (correct for POSIX /a/b, Windows C:\\, UNC).
    Called BEFORE realpath so link attributes are not lost."""
    p = os.path.abspath(path)
    ancestors = []
    while True:
        ancestors.append(p)
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    for a in reversed(ancestors):
        if os.path.lexists(a) and _is_reparse(a):
            raise SASTScanError(PATH_ESCAPE, "symlink/reparse in path chain: %s" % a)


def _validate_ruleset(ruleset):
    if not isinstance(ruleset, dict):
        raise SASTScanError(RULESET_INVALID, "ruleset not an object")
    errs = sorted(_schema_validator().iter_errors(ruleset), key=lambda e: list(e.absolute_path))
    if errs:
        path = "/".join(str(p) for p in errs[0].absolute_path) or "<root>"
        raise SASTScanError(RULESET_INVALID, "%s: %s" % (path, errs[0].message))
    if not ruleset.get("secret_rules") or not ruleset.get("ast_rules"):
        raise SASTScanError(RULESET_INVALID, "secret_rules and ast_rules must be non-empty")
    if not ruleset.get("dep_vuln", {}).get("advisories"):
        pass  # advisories may be empty (no known vulns); that is not invalid
    seen = set()
    for r in ruleset["secret_rules"] + ruleset["ast_rules"]:
        if r["rule_id"] in seen:
            raise SASTScanError(RULESET_INVALID, "duplicate rule_id: %s" % r["rule_id"])
        seen.add(r["rule_id"])
    for sr in ruleset["secret_rules"]:
        try:
            re.compile(sr["pattern"])
        except re.error as exc:
            raise SASTScanError(RULESET_INVALID, "rule %s invalid regex: %s" % (sr["rule_id"], exc))
    # duplicate advisory (same package+version+id) or derived rule_id -> fail-closed
    adv_seen = set()
    rid_seen = set()
    for a in ruleset["dep_vuln"]["advisories"]:
        key = (a["package"].lower(), a["ecosystem"], a["version"], a["id"])
        if key in adv_seen:
            raise SASTScanError(RULESET_INVALID, "duplicate advisory: %s" % (key,))
        adv_seen.add(key)
        rid = _dep_rule_id(a)
        if rid in rid_seen:
            raise SASTScanError(RULESET_INVALID, "duplicate derived advisory rule_id: %s" % rid)
        rid_seen.add(rid)
    return ruleset


def _dep_rule_id(a):
    rid = "DEP_" + a["id"].upper().replace("-", "_").replace(".", "_")
    return "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in rid)


def load_ruleset(path):
    if not path or not os.path.isfile(path):
        raise SASTScanError(RULESET_INVALID, "rules file not found: %s" % path)
    try:
        with open(path, encoding="utf-8") as fh:
            ruleset = json.load(fh)
    except (ValueError, OSError) as exc:
        raise SASTScanError(RULESET_INVALID, "rules file not valid JSON: %s" % exc)
    _validate_ruleset(ruleset)
    version = ruleset["rules_version"]
    if int(version.split(".")[0]) != SUPPORTED_RULES_MAJOR:
        raise SASTScanError(RULESET_VERSION_UNSUPPORTED, "rules_version %s" % version)
    # attach compiled regexes
    enriched = []
    for sr in ruleset["secret_rules"]:
        enriched.append(dict(sr, _compiled=re.compile(sr["pattern"])))
    ruleset = dict(ruleset, secret_rules=enriched)
    return ruleset


def _safe_rel_path(rel):
    if not isinstance(rel, str) or not rel:
        raise SASTScanError(INPUT_INVALID, "empty path")
    norm = rel.replace("\\", "/")
    if os.path.isabs(rel) or norm.startswith("/") or norm.startswith("~"):
        raise SASTScanError(PATH_ESCAPE, "absolute/home path rejected")
    parts = [p for p in norm.split("/") if p]
    if any(p == ".." for p in parts):
        raise SASTScanError(PATH_ESCAPE, "'..' segment rejected")
    return "/".join(parts)


def _safe_resolve(root_real, rel):
    """Walk every component; reject symlink/junction/reparse at any level."""
    safe = _safe_rel_path(rel)
    cur = root_real
    for p in safe.split("/"):
        cur = os.path.join(cur, p)
        if _is_reparse(cur):
            raise SASTScanError(PATH_ESCAPE, "symlink/reparse component rejected: %s" % p)
    full = os.path.realpath(cur)
    if not (full == root_real or full.startswith(root_real.rstrip(os.sep) + os.sep)):
        raise SASTScanError(PATH_ESCAPE, "resolved path escapes trusted root")
    return safe, full


def _is_manifest(path):
    base = path.rsplit("/", 1)[-1].lower()
    # any .txt containing "requirements" (incl. dev-requirements.txt), or known pins
    return (("requirements" in base or base in {"constraints.txt", "pinned.txt"})
            and base.endswith(".txt"))


def _manifest_ecosystem(path):
    base = path.rsplit("/", 1)[-1].lower()
    if "requirements" in base or base in {"constraints.txt", "pinned.txt"}:
        return "pypi"
    return None


def _read_under_root(trusted_root, rel, max_bytes):
    root_real = os.path.realpath(trusted_root)
    safe, full = _safe_resolve(root_real, rel)
    if _is_reparse(full) or not os.path.isfile(full):
        raise SASTScanError(PATH_ESCAPE if _is_reparse(full) else INPUT_INVALID,
                            "reparse target" if _is_reparse(full) else "path not a file: %s" % safe)
    with open(full, "rb") as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise SASTScanError(INPUT_TOO_LARGE, "file %s exceeds max_bytes_per_file" % safe)
    return safe, data.decode("utf-8", "replace")


def _redact_for_digest(content, secret_rules):
    """Replace each secret match with a stable token (no raw secret in digest)."""
    spans = []
    for rule in secret_rules:
        for m in rule["_compiled"].finditer(content):
            spans.append((m.start(), m.end(), "<REDACTED:%s:%d>" % (rule["rule_id"], m.end() - m.start())))
    if not spans:
        return content
    spans.sort()
    out = []
    pos = 0
    for s, e, tok in spans:
        if s < pos:
            continue  # overlap; skip
        out.append(content[pos:s])
        out.append(tok)
        pos = e
    out.append(content[pos:])
    return "".join(out)


def _resolve_options(opts):
    """Clamp to hard limits; reject values that exceed hard limits or are invalid."""
    def _opt(name, hard, default):
        v = opts.get(name, default)
        if v is None:
            v = default
        if not isinstance(v, int) or isinstance(v, bool):
            raise SASTScanError(INPUT_INVALID, "option %s must be an integer" % name)
        if v < 1:
            raise SASTScanError(INPUT_INVALID, "option %s must be >= 1" % name)
        if v > hard:
            raise SASTScanError(INPUT_INVALID, "option %s exceeds hard max %d" % (name, hard))
        return v
    files_cap = HARD_MAX_FILES
    return {
        "max_bytes_per_file": _opt("max_bytes_per_file", HARD_MAX_BYTES_PER_FILE, HARD_MAX_BYTES_PER_FILE),
        "max_total_bytes": _opt("max_total_bytes", HARD_MAX_TOTAL_BYTES, HARD_MAX_TOTAL_BYTES),
        "max_findings": _opt("max_findings", HARD_MAX_FINDINGS, HARD_MAX_FINDINGS),
        "max_files": _opt("max_files", HARD_MAX_FILES, HARD_MAX_FILES),
    }


def scan(inp, trusted_workspace=None, ruleset=None, expected_rules_version=None, today=None, deadline=None):
    import datetime as _dt
    from skills.sast_scan.engines import secret as e_secret
    from skills.sast_scan.engines import ast_python as e_ast
    from skills.sast_scan.engines import dep_vuln as e_dep

    _check_deadline(deadline)
    if ruleset is None:
        ruleset = load_ruleset(DEFAULT_RULES_PATH)
    else:
        _validate_ruleset(ruleset)
        ruleset = dict(ruleset, secret_rules=[
            dict(sr, _compiled=re.compile(sr["pattern"])) for sr in ruleset["secret_rules"]])
    version = ruleset["rules_version"]
    # major-version compatibility (also for injected rulesets, not just loaded)
    if int(version.split(".")[0]) != SUPPORTED_RULES_MAJOR:
        raise SASTScanError(RULESET_VERSION_UNSUPPORTED, "rules_version %s unsupported major" % version)
    if expected_rules_version is None:
        expected_rules_version = inp.get("expected_rules_version")
    if expected_rules_version is not None and expected_rules_version != version:
        raise SASTScanError(INPUT_INVALID, "expected_rules_version %s != %s" % (expected_rules_version, version))

    opts = _resolve_options(inp.get("options") or {})
    max_per_file = opts["max_bytes_per_file"]
    max_total = opts["max_total_bytes"]
    max_findings = opts["max_findings"]
    max_files = opts["max_files"]

    mode = inp.get("mode", "inline")
    files = []  # (safe_path, content, is_manifest, ecosystem)
    degraded = []

    def _accumulate(content, label):
        total = sum(_utf8_bytes(c) for _, c, _, _ in files) + _utf8_bytes(content)
        if total > max_total:
            raise SASTScanError(INPUT_TOO_LARGE, "%s pushes total over max_total_bytes" % label)

    seen_paths = set()

    if mode == "inline":
        inline = inp.get("files") or []
        if len(inline) > max_files:
            raise SASTScanError(INPUT_INVALID, "files count exceeds max_files")
        for f in inline:
            _check_deadline(deadline)
            safe = _safe_rel_path(f["path"])
            if safe in seen_paths:
                raise SASTScanError(INPUT_INVALID, "duplicate path: %s" % safe)
            seen_paths.add(safe)
            content = f.get("content", "")
            if _utf8_bytes(content) > max_per_file:
                raise SASTScanError(INPUT_TOO_LARGE, "file %s exceeds max_bytes_per_file" % safe)
            _accumulate(content, safe)
            files.append((safe, content, _is_manifest(safe), _manifest_ecosystem(safe)))
    elif mode == "paths":
        if not trusted_workspace or not os.path.isdir(trusted_workspace):
            raise SASTScanError(TRUSTED_CONFIG_MISSING, "paths mode requires a deploy-provided trusted workspace root")
        _assert_no_reparse_chain(trusted_workspace)  # reject root symlink before realpath
        rels = inp.get("paths") or []
        if len(rels) > max_files:
            raise SASTScanError(INPUT_INVALID, "paths count exceeds max_files")
        for rel in rels:
            _check_deadline(deadline)
            safe, content = _read_under_root(trusted_workspace, rel, max_per_file)
            if safe in seen_paths:
                raise SASTScanError(INPUT_INVALID, "duplicate path: %s" % safe)
            seen_paths.add(safe)
            _accumulate(content, safe)
            files.append((safe, content, _is_manifest(safe), _manifest_ecosystem(safe)))
    else:
        raise SASTScanError(INPUT_INVALID, "unknown mode: %s" % mode)

    if not files:
        # empty scan is never "complete"
        degraded.append({"engine": "core", "reason": "no files scanned"})

    # input digest: redact secret matches first (never hash raw secret content)
    redacted_files = [{"path": p, "content": _redact_for_digest(c, ruleset["secret_rules"])}
                      for p, c, _, _ in files]
    input_digest = _sha(_canonical_json({"mode": mode, "files": redacted_files, "rules_version": version}))

    # run all three engines
    raw = []
    for path, content, is_man, eco in files:
        _check_deadline(deadline)
        raw.extend(e_secret.scan(path, content, ruleset["secret_rules"]))
        if path.endswith(".py"):
            ast_findings, had_syntax = e_ast.scan(path, content, ruleset["ast_rules"])
            raw.extend(ast_findings)
            if had_syntax:
                degraded.append({"engine": "ast_python", "reason": "syntax error in %s" % path})
        if is_man:
            if eco and eco != "pypi":
                degraded.append({"engine": "dep_vuln", "reason": "ecosystem %s not covered" % eco})
            elif eco == "pypi":
                # only match pypi advisories (no cross-ecosystem false positives)
                pypi_adv = [a for a in ruleset["dep_vuln"]["advisories"] if a["ecosystem"] == "pypi"]
                raw.extend(e_dep.scan(path, content, pypi_adv))

    # deterministic order + safe evidence_digest + fingerprint + dedup
    raw.sort(key=lambda f: (f["file"], f["line"], f["column"], f["rule_id"], f["engine"]))
    ordinal = {}
    findings = []
    seen = set()
    dedup_count = 0
    for f in raw:
        key = (f["engine"], f["rule_id"], f["file"], f["line"], f["column"])
        n = ordinal.get(key, 0)
        ordinal[key] = n + 1
        match_len = _utf8_bytes(f.get("evidence_text", ""))
        evidence_digest = _sha(_canonical_json(
            [f["engine"], f["rule_id"], f["file"], f["line"], f["column"], match_len, n]))
        fingerprint = _sha(_canonical_json(
            [f["engine"], f["rule_id"], f["file"], f["line"], f["column"], evidence_digest, n]))
        if fingerprint in seen:
            dedup_count += 1
            continue
        seen.add(fingerprint)
        findings.append({
            "finding_id": "finding-" + fingerprint[:16],
            "fingerprint": fingerprint,
            "engine": f["engine"],
            "rule_id": f["rule_id"],
            "category": f["category"],
            "severity": f["severity"],
            "risk_level": f["risk_level"],
            "file": f["file"],
            "line": f["line"],
            "column": f["column"],
            "message": f["message"],
            "remediation": f["remediation"],
            "evidence_digest": evidence_digest,
        })

    findings_total = len(findings)
    truncated = False
    truncated_digest = ""
    if findings_total > max_findings:
        kept = findings[:max_findings]
        dropped = findings[max_findings:]
        truncated = True
        truncated_digest = _sha(_canonical_json([f["fingerprint"] for f in dropped]))
        findings = kept
        degraded.append({"engine": "core", "reason": "findings truncated to max_findings=%d" % max_findings})

    dv = ruleset["dep_vuln"]
    today = today or _dt.date.today()
    try:
        stale = today > _dt.date.fromisoformat(dv["valid_until"])
    except ValueError:
        stale = True
    if stale:
        degraded.append({"engine": "dep_vuln", "reason": "advisory DB past valid_until (%s)" % dv["valid_until"]})

    by_sev, by_eng = {}, {}
    for f in findings:
        by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1
        by_eng[f["engine"]] = by_eng.get(f["engine"], 0) + 1

    complete = (len(files) > 0) and not degraded
    out = {
        "schema_version": SCHEMA_VERSION,
        "rules_version": version,
        "input_digest": input_digest,
        "complete": complete,
        "findings": findings,
        "stats": {
            "files_scanned": len(files),
            "findings": len(findings),
            "findings_total": findings_total,
            "truncated": truncated,
            "truncated_digest": truncated_digest,
            "dedup_count": dedup_count,
            "by_severity": by_sev,
            "by_engine": by_eng,
        },
        "engines_used": ["ast_python", "dep_vuln", "secret"],
        "dep_vuln_meta": {
            "db_version": dv["db_version"],
            "source": dv["source"],
            "covered_ecosystems": list(dv["covered_ecosystems"]),
            "valid_until": dv["valid_until"],
            "stale": stale,
        },
    }
    if not complete:
        out["degraded"] = degraded
    return out
