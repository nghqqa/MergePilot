"""RiskClassify core -- deterministic, advisory, only-escalate risk aggregator
(framework-neutral).

Consumes a structured change context (DiffParse business output shape) and a
*versioned* declarative ruleset, and returns an L0/L1/L2 advisory
classification plus human-readable reasons.

Hard guarantees:
* **Only-escalate**: ``risk_level = max(risk_floor, highest matched rule)``.
  It can never be below the caller's floor and rules can never lower it.
* **Advisory only**: it never approves, denies or executes anything; the
  Policy Gateway remains the final authorization authority.
* **No Nacos / no network / no LLM / no author trust**: it lowers risk for
  nobody. Rules live in a versioned JSON file, not in code or policy.yaml.
* **Deterministic**: identical (change_context, rules_version) yields identical
  business output; rule/file ordering cannot change the level.
* **Fail-closed**: a missing/corrupt/unknown-version ruleset is an ERROR, never
  a silent fallback that lowers risk.
"""
from __future__ import annotations

import json
import os
import re

SCHEMA_VERSION = "1"
SUPPORTED_RULES_MAJOR = 1

LEVELS = ("L0", "L1", "L2")
LEVEL_RANK = {"L0": 0, "L1": 1, "L2": 2}
RANK_LEVEL = {0: "L0", 1: "L1", 2: "L2"}

CHANGE_TYPES = ("A", "M", "D", "R", "C", "T")
_CATEGORIES = (
    "source", "test", "documentation", "dependency", "workflow",
    "config", "migration", "security_sensitive", "deletion", "binary",
)

# Skill-specific error codes (RISK_CLASSIFY_* prefix; common codes reused by
# run.py for generic invalid input).
RULES_MISSING = "RISK_CLASSIFY_RULES_MISSING"
RULESET_INVALID = "RISK_CLASSIFY_RULESET_INVALID"
RULESET_VERSION_UNSUPPORTED = "RISK_CLASSIFY_RULESET_VERSION_UNSUPPORTED"
INVALID_CONTEXT = "RISK_CLASSIFY_INVALID_CONTEXT"


class RiskClassifyError(Exception):
    """Carries a public skill-specific error ``code`` and ``message``."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# predicate classification
_FILE_PREDICATES = ("category", "change_type", "binary", "path_pattern")
_CTX_PREDICATES = (
    "complete_false", "empty", "min_total_changes", "max_total_changes",
    "min_additions", "min_deletions", "min_files", "only_categories",
    "has_uncategorized",
)

_HERE = os.path.dirname(os.path.abspath(__file__))
_RULES_SCHEMA_PATH = os.path.join(_HERE, "schema", "rules.schema.json")
DEFAULT_RULES_PATH = os.path.join(_HERE, "rules", "risk-rules.v1.json")

_SCHEMA_VALIDATOR = None


def _schema_validator():
    global _SCHEMA_VALIDATOR
    if _SCHEMA_VALIDATOR is None:
        import jsonschema  # the one runtime dependency
        with open(_RULES_SCHEMA_PATH, encoding="utf-8") as fh:
            _SCHEMA_VALIDATOR = jsonschema.Draft202012Validator(json.load(fh))
    return _SCHEMA_VALIDATOR


def _validate_ruleset(ruleset):
    if not isinstance(ruleset, dict):
        raise RiskClassifyError(RULESET_INVALID, "ruleset must be a JSON object")
    errors = sorted(_schema_validator().iter_errors(ruleset),
                    key=lambda e: list(e.absolute_path))
    if errors:
        path = "/".join(str(p) for p in errors[0].absolute_path) or "<root>"
        raise RiskClassifyError(RULESET_INVALID, "%s: %s" % (path, errors[0].message))
    seen = set()
    for rule in ruleset["rules"]:
        rid = rule["rule_id"]
        if rid in seen:
            raise RiskClassifyError(RULESET_INVALID, "duplicate rule_id: %s" % rid)
        seen.add(rid)
        pat = (rule.get("match") or {}).get("path_pattern")
        if pat is not None:
            try:
                re.compile(pat)
            except re.error as exc:
                raise RiskClassifyError(
                    RULESET_INVALID,
                    "rule %s has invalid path_pattern: %s" % (rid, exc),
                )
    return ruleset


def _validate_context(change_context):
    """Deep structural + aggregation check of a change_context (fail-closed).

    Guards against inconsistent input (negative stats, unknown categories,
    stats/files aggregation mismatch, bad file shape). Raises
    ``RiskClassifyError(INVALID_CONTEXT)`` on any problem.
    """
    cc = change_context
    if not isinstance(cc, dict):
        raise RiskClassifyError(INVALID_CONTEXT, "change_context must be an object")
    for key in ("complete", "files", "stats", "change_categories"):
        if key not in cc:
            raise RiskClassifyError(INVALID_CONTEXT, "change_context missing field: %s" % key)
    if not isinstance(cc["complete"], bool):
        raise RiskClassifyError(INVALID_CONTEXT, "change_context.complete must be a boolean")
    if not isinstance(cc["files"], list):
        raise RiskClassifyError(INVALID_CONTEXT, "change_context.files must be an array")
    if not isinstance(cc["stats"], dict):
        raise RiskClassifyError(INVALID_CONTEXT, "change_context.stats must be an object")
    if not isinstance(cc["change_categories"], list):
        raise RiskClassifyError(INVALID_CONTEXT, "change_context.change_categories must be an array")

    stats = cc["stats"]
    files = cc["files"]
    for sk in ("files_changed", "additions", "deletions", "hunks", "binary_files"):
        v = stats.get(sk)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise RiskClassifyError(INVALID_CONTEXT, "stats.%s must be a non-negative integer" % sk)

    vocab = set(_CATEGORIES)
    for c in cc["change_categories"]:
        if c not in vocab:
            raise RiskClassifyError(INVALID_CONTEXT, "unknown change_category")

    for idx, f in enumerate(files):
        if not isinstance(f, dict):
            raise RiskClassifyError(INVALID_CONTEXT, "files[%d] must be an object" % idx)
        if not isinstance(f.get("path"), str) or not f.get("path"):
            raise RiskClassifyError(INVALID_CONTEXT, "files[%d].path required" % idx)
        if f.get("change_type") not in CHANGE_TYPES:
            raise RiskClassifyError(INVALID_CONTEXT, "files[%d].change_type invalid" % idx)
        if not isinstance(f.get("categories"), list):
            raise RiskClassifyError(INVALID_CONTEXT, "files[%d].categories must be an array" % idx)
        for c in f.get("categories"):
            if c not in vocab:
                raise RiskClassifyError(INVALID_CONTEXT, "files[%d] unknown category" % idx)
        if not isinstance(f.get("hunks"), list):
            raise RiskClassifyError(INVALID_CONTEXT, "files[%d].hunks must be an array" % idx)
        if not isinstance(f.get("binary"), bool):
            raise RiskClassifyError(INVALID_CONTEXT, "files[%d].binary must be a boolean" % idx)
        for numkey in ("additions", "deletions"):
            v = f.get(numkey)
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise RiskClassifyError(INVALID_CONTEXT, "files[%d].%s must be a non-negative integer" % (idx, numkey))

    # aggregation consistency between stats and files
    if stats["files_changed"] != len(files):
        raise RiskClassifyError(INVALID_CONTEXT, "stats.files_changed != len(files)")
    if stats["binary_files"] != sum(1 for f in files if f.get("binary")):
        raise RiskClassifyError(INVALID_CONTEXT, "stats.binary_files mismatch")
    if stats["additions"] != sum(f.get("additions", 0) for f in files):
        raise RiskClassifyError(INVALID_CONTEXT, "stats.additions != sum(file additions)")
    if stats["deletions"] != sum(f.get("deletions", 0) for f in files):
        raise RiskClassifyError(INVALID_CONTEXT, "stats.deletions != sum(file deletions)")
    if stats["hunks"] != sum(len(f.get("hunks") or []) for f in files):
        raise RiskClassifyError(INVALID_CONTEXT, "stats.hunks != sum(file hunks)")


def load_rules(path):
    """Read, JSON-parse and validate a ruleset file. Fail-closed on any problem."""
    if not path or not os.path.isfile(path):
        raise RiskClassifyError(RULES_MISSING, "rules file not found: %s" % path)
    try:
        with open(path, encoding="utf-8") as fh:
            ruleset = json.load(fh)
    except (ValueError, OSError) as exc:
        raise RiskClassifyError(RULESET_INVALID, "rules file not valid JSON: %s" % exc)
    _validate_ruleset(ruleset)
    version = ruleset["rules_version"]
    major = int(version.split(".")[0])
    if major != SUPPORTED_RULES_MAJOR:
        raise RiskClassifyError(
            RULESET_VERSION_UNSUPPORTED,
            "rules_version %s not supported (major %d required)" % (version, SUPPORTED_RULES_MAJOR),
        )
    return ruleset


def _ctx_summary(change_context):
    stats = change_context.get("stats") or {}
    additions = int(stats.get("additions", 0) or 0)
    deletions = int(stats.get("deletions", 0) or 0)
    files = change_context.get("files") or []
    cats = set(change_context.get("change_categories") or [])
    complete = bool(change_context.get("complete", False))
    has_uncategorized = any(
        not (f.get("categories") if isinstance(f, dict) else None) for f in files
    )
    return {
        "complete": complete,
        "empty": len(files) == 0,
        "total_changes": additions + deletions,
        "additions": additions,
        "deletions": deletions,
        "files_changed": len(files),
        "change_categories": cats,
        "has_uncategorized": has_uncategorized,
    }


def _as_list(v):
    if isinstance(v, list):
        return v
    return [v]


def _ctx_pred_ok(key, value, s):
    if key == "complete_false":
        return value is True and (not s["complete"])
    if key == "empty":
        return value is True and s["empty"]
    if key == "min_total_changes":
        return s["total_changes"] >= value
    if key == "max_total_changes":
        return s["total_changes"] <= value
    if key == "min_additions":
        return s["additions"] >= value
    if key == "min_deletions":
        return s["deletions"] >= value
    if key == "min_files":
        return s["files_changed"] >= value
    if key == "only_categories":
        allowed = set(value)
        # an EMPTY category set must not satisfy only_categories (it would
        # otherwise match every allowed set); require at least one category.
        return bool(s["change_categories"]) and s["change_categories"].issubset(allowed)
    if key == "has_uncategorized":
        return value is True and s["has_uncategorized"]
    return False


def _file_preds_ok(preds, f):
    cats = set(f.get("categories") or [])
    for key, value in preds.items():
        if key == "category":
            if not (cats & set(_as_list(value))):
                return False
        elif key == "change_type":
            if f.get("change_type") not in _as_list(value):
                return False
        elif key == "binary":
            if bool(f.get("binary", False)) != bool(value):
                return False
        elif key == "path_pattern":
            try:
                if not re.search(value, f.get("path", "")):
                    return False
            except re.error:
                # a bad pattern never matches (does not crash the classifier)
                return False
    return True


def _evaluate_rule(rule, summary, files):
    """Return (matched: bool, contributing_paths: list[str])."""
    match = rule.get("match") or {}
    # context-wise predicates must ALL hold
    for key in _CTX_PREDICATES:
        if key in match and not _ctx_pred_ok(key, match[key], summary):
            return False, []
    file_preds = {k: v for k, v in match.items() if k in _FILE_PREDICATES}
    if file_preds:
        contributing = [f.get("path", "") for f in files if _file_preds_ok(file_preds, f)]
        if not contributing:
            return False, []
        return True, contributing
    # only context-wise predicates -> contributes all files (or none)
    return True, [f.get("path", "") for f in files]


def classify(change_context, risk_floor="L0", ruleset=None,
             expected_rules_version=None):
    """Classify a change context. Returns the advisory output dict.

    ``ruleset`` should be a validated ruleset (use :func:`load_rules`). For
    tests, a hand-built dict is accepted and re-validated here.
    """
    if risk_floor not in LEVEL_RANK:
        raise RiskClassifyError(RULESET_INVALID, "risk_floor must be L0/L1/L2")
    if ruleset is None:
        ruleset = load_rules(DEFAULT_RULES_PATH)
    else:
        _validate_ruleset(ruleset)

    version = ruleset["rules_version"]
    major = int(version.split(".")[0])
    if major != SUPPORTED_RULES_MAJOR:
        raise RiskClassifyError(
            RULESET_VERSION_UNSUPPORTED,
            "rules_version %s not supported (major %d required)" % (version, SUPPORTED_RULES_MAJOR),
        )
    if expected_rules_version is not None and expected_rules_version != version:
        raise RiskClassifyError(
            RULESET_VERSION_UNSUPPORTED,
            "expected rules_version %s but loaded %s" % (expected_rules_version, version),
        )

    _validate_context(change_context)
    summary = _ctx_summary(change_context)
    files = change_context.get("files") or []

    matched = []  # list of (rule_id, level, summary, contributing_paths)
    for rule in ruleset["rules"]:
        ok, contributing = _evaluate_rule(rule, summary, files)
        if ok:
            matched.append((
                rule["rule_id"],
                rule["level"],
                rule["summary"],
                sorted(set(p for p in contributing if p)),
            ))

    floor_rank = LEVEL_RANK[risk_floor]
    highest = floor_rank
    for _rid, level, _summ, _paths in matched:
        if LEVEL_RANK[level] > highest:
            highest = LEVEL_RANK[level]
    risk_level = RANK_LEVEL[highest]

    reasons = sorted(matched, key=lambda t: (-LEVEL_RANK[t[1]], t[0]))
    reasons_out = [
        {"rule_id": rid, "level": level, "summary": summ, "files": paths}
        for rid, level, summ, paths in reasons
    ]
    matched_rules = sorted(rid for rid, _l, _s, _p in matched)

    controls = {
        "L0": ["AUTO_REVIEW_ELIGIBLE"],
        "L1": ["AUTO_REVIEW_ELIGIBLE", "HUMAN_REVIEW"],
        "L2": ["HUMAN_REVIEW", "L2_APPROVAL_RECOMMENDED"],
    }[risk_level]

    return {
        "schema_version": SCHEMA_VERSION,
        "rules_version": version,
        "risk_level": risk_level,
        "risk_rank": LEVEL_RANK[risk_level],
        "risk_floor": risk_floor,
        "advisory_only": True,
        "reasons": reasons_out,
        "matched_rules": matched_rules,
        "recommended_controls": controls,
        "approval_recommended": risk_level == "L2",
    }
