#!/usr/bin/env python3
"""MinIO storage cleanup tool (dry-run by default; fail-closed).

P1/P2 fixes vs the initial candidate:

  * ENUMERATION IS FAIL-CLOSED: ``mc find`` failures RAISE and abort the
    whole plan. Errors are NEVER converted to empty results.

  * PRECONDITIONS before ``--apply``:
      - no active production runs (injectable ``is_idle`` callback; None
        or False -> abort)
      - no active multipart uploads younger than MP_MULTIPART_MIN_AGE_HOURS
        (injectable ``no_recent_uploads`` callback; None or False -> abort)

  * MINIMUM AGE on incomplete multipart: configurable via
    MP_MULTIPART_MIN_AGE_HOURS (default 24). Young uploads are never cleaned.

  * IMMUTABLE PLAN DIGEST: ``compute_plan_digest`` returns a SHA-256 over the
    canonical plan. ``execute`` with ``expected_digest`` recomputes and aborts
    on any drift.

  * mc-ONLY: never touches ``.minio.sys`` directly. Only multipart +
    precise ``agents/<W>/.codex/tmp/`` + precise ``manager/.npm/_npx/``.
    SOUL/skills/config/sessions/shared-tasks/credentials are deny-listed.

Default mode is dry-run. ``--apply`` is required to remove. Executor is
injectable for unit tests (no real mc/MinIO).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys

DEFAULT_AGENTS = ("reviewer", "fixer", "verifier")
DEFAULT_MULTIPART_MIN_AGE_HOURS = 24

ALLOWED_PREFIXES = [
    re.compile(r"^agents/[^/]+/\.codex/tmp/"),
    re.compile(r"^manager/\.npm/_npx/"),
]
FORBIDDEN_PATTERNS = [
    re.compile(r"SOUL\.md$", re.IGNORECASE),
    re.compile(r"/skills/"),
    re.compile(r"/config/"),
    re.compile(r"mcporter\.json$", re.IGNORECASE),
    re.compile(r"/sessions/"),
    re.compile(r"/shared/tasks/"),
    re.compile(r"\.minio\.sys/"),
    re.compile(r"credentials", re.IGNORECASE),
    re.compile(r"\.git/"),
]


class McError(Exception):
    """Raised on any mc enumeration failure (fail-closed)."""


def is_allowed_target(key):
    """Return (allowed, reason). Allowed prefix AND not deny-listed."""
    if not key or not isinstance(key, str):
        return (False, "empty/non-string key")
    for pat in FORBIDDEN_PATTERNS:
        if pat.search(key):
            return (False, "denied pattern %s" % pat.pattern)
    for pat in ALLOWED_PREFIXES:
        if pat.search(key):
            return (True, "allowed precise temp/cache target")
    return (False, "out of scope (no allowed prefix matched)")


def _default_runner(argv, env=None):
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=180,
                          env=env)
    if proc.returncode != 0:
        raise McError("mc rc=%d stderr=%s" % (proc.returncode, proc.stderr[:200]))
    return proc.stdout


def _strip_alias_bucket(line, alias, bucket):
    prefix = "%s/%s/" % (alias, bucket)
    s = line.strip()
    return s[len(prefix):] if s.startswith(prefix) else s


def enumerate_prefix(alias, bucket, rel_prefix, runner=None):
    """Run ``mc find``. RAISES McError on any failure (never returns []).

    Fail-closed: a missing mc, a connection error, or a non-zero rc all
    abort the plan rather than silently producing an empty result.
    """
    runner = runner or _default_runner
    full = "%s/%s/%s" % (alias, bucket, rel_prefix)
    out = runner(["mc", "find", full])  # raises on failure
    keys = []
    for line in out.splitlines():
        line = line.strip()
        if line:
            keys.append(_strip_alias_bucket(line, alias, bucket))
    return keys


def plan_codex_tmp(alias, bucket, agents, runner=None):
    """Enumerate + validate agents/<W>/.codex/tmp/. Raises on mc failure."""
    targets = []
    for agent in agents:
        prefix = "agents/%s/.codex/tmp/" % agent
        for key in enumerate_prefix(alias, bucket, prefix, runner):
            allowed, reason = is_allowed_target(key)
            if allowed:
                targets.append(_rm_target(alias, bucket, key, "codex_tmp"))
            else:
                sys.stderr.write("SKIP (denied) %s -- %s\n" % (key, reason))
    return targets


def plan_npm_npx(alias, bucket, runner=None):
    """Enumerate + validate manager/.npm/_npx/. Raises on mc failure."""
    targets = []
    for key in enumerate_prefix(alias, bucket, "manager/.npm/_npx/", runner):
        allowed, reason = is_allowed_target(key)
        if allowed:
            targets.append(_rm_target(alias, bucket, key, "npm_npx"))
        else:
            sys.stderr.write("SKIP (denied) %s -- %s\n" % (key, reason))
    return targets


def plan_multipart(alias, bucket):
    """Multipart cleanup target (mc rm --incomplete --recursive).

    Age-gated at execute time via preconditions; the command itself is
    filtered by mc to incomplete uploads only.
    """
    return {
        "category": "multipart",
        "argv": ["mc", "rm", "--incomplete", "--recursive", "--force",
                 "%s/%s/" % (alias, bucket)],
        "desc": "incomplete multipart across bucket",
    }


def _rm_target(alias, bucket, key, category):
    return {
        "category": category,
        "key": key,
        "argv": ["mc", "rm", "--force", "%s/%s/%s" % (alias, bucket, key)],
        "desc": "%s %s" % (category, key),
    }


def build_plan(alias, bucket, agents, runner=None,
               include_multipart=True, include_codex=True, include_npx=True):
    """Build the full plan. Raises McError on enumeration failure (abort)."""
    plan = []
    if include_codex:
        plan.extend(plan_codex_tmp(alias, bucket, agents, runner))
    if include_npx:
        plan.extend(plan_npm_npx(alias, bucket, runner))
    if include_multipart:
        plan.append(plan_multipart(alias, bucket))
    return plan


def canonicalize(plan):
    """Return a canonical (sorted, JSON-serializable) representation."""
    canon = []
    for t in plan:
        canon.append({"category": t.get("category"),
                      "key": t.get("key", ""),
                      "argv": list(t.get("argv", [])),
                      "desc": t.get("desc", "")})
    canon.sort(key=lambda x: json.dumps(x, sort_keys=True))
    return canon


def compute_plan_digest(plan):
    """SHA-256 hex of the canonical plan. Immutable fingerprint."""
    payload = json.dumps(canonicalize(plan), sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def check_preconditions(is_idle_fn=None, no_recent_uploads_fn=None,
                        min_age_hours=DEFAULT_MULTIPART_MIN_AGE_HOURS):
    """Return (ok, reasons). Fail-closed: uncertainty aborts.

    is_idle_fn() -> True|False|None : True = no active production runs.
    no_recent_uploads_fn(min_age_hours) -> True|False|None : True = no
        multipart uploads younger than the threshold.
    None (cannot determine) is treated as fail-closed.
    """
    reasons = []
    if is_idle_fn is None or no_recent_uploads_fn is None:
        reasons.append("precondition callbacks not provided (fail-closed)")
        return (False, reasons)
    idle = is_idle_fn()
    if idle is not True:
        reasons.append(
            "active production runs not ruled out (is_idle=%r)" % idle)
    recent = no_recent_uploads_fn(min_age_hours)
    if recent is not True:
        reasons.append(
            "recent multipart uploads not ruled out within %dh (result=%r)"
            % (min_age_hours, recent))
    return (len(reasons) == 0, reasons)


def execute(plan, runner=None, apply=False, env=None, expected_digest=None,
            is_idle_fn=None, no_recent_uploads_fn=None,
            min_age_hours=DEFAULT_MULTIPART_MIN_AGE_HOURS):
    """Execute the plan.

    Dry-run: prints each target, verifies digest if given.
    Apply: checks preconditions + digest drift, then runs each target.

    Returns a list of result dicts. Raises on digest drift or failed
    preconditions (apply mode only).
    """
    runner = runner or _default_runner
    results = []

    if expected_digest is not None:
        actual = compute_plan_digest(plan)
        if actual != expected_digest:
            raise ValueError(
                "plan digest drift: expected %s got %s -- aborting" %
                (expected_digest, actual))

    if apply:
        ok, reasons = check_preconditions(
            is_idle_fn, no_recent_uploads_fn, min_age_hours)
        if not ok:
            raise ValueError(
                "preconditions failed (fail-closed): %s" % "; ".join(reasons))

    for target in plan:
        entry = {"desc": target["desc"], "argv": target["argv"],
                 "applied": False, "ok": False, "error": ""}
        if not apply:
            sys.stdout.write("[dry-run] %s -> %s\n"
                             % (target["desc"], " ".join(target["argv"])))
            entry["ok"] = True
            results.append(entry)
            continue
        try:
            runner(target["argv"], env=env)
            entry["applied"] = True
            entry["ok"] = True
        except Exception as exc:
            entry["applied"] = True
            entry["error"] = str(exc)
        results.append(entry)
    return results


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    apply = "--apply" in argv
    alias = os.environ.get("MP_MC_ALIAS", "local")
    bucket = os.environ.get("MP_MC_BUCKET", "hiclaw-storage")
    agents_csv = os.environ.get("MP_AGENTS", ",".join(DEFAULT_AGENTS))
    agents = tuple(a.strip() for a in agents_csv.split(",") if a.strip())
    min_age = int(os.environ.get(
        "MP_MULTIPART_MIN_AGE_HOURS", DEFAULT_MULTIPART_MIN_AGE_HOURS))

    # This candidate ships NO authoritative idle/recent-upload probe. Until one
    # exists, --apply is fail-closed: stable non-zero status, clear message,
    # no deletion, no traceback.
    if apply:
        sys.stderr.write(
            "minio_cleanup: --apply is not supported in this candidate: no "
            "authoritative idle/recent-upload probe is implemented. Refusing "
            "to delete (fail-closed, status 3). Re-run without --apply for a "
            "dry-run plan.\n")
        return 3

    try:
        plan = build_plan(alias, bucket, agents, runner=None)
    except McError as exc:
        sys.stderr.write("minio_cleanup: enumeration FAILED (fail-closed): %s\n"
                         % exc)
        return 3  # enumeration failure is a hard stop

    digest = compute_plan_digest(plan)
    sys.stdout.write("minio_cleanup DRY-RUN (default) alias=%s bucket=%s "
                     "targets=%d\n" % (alias, bucket, len(plan)))
    sys.stdout.write("minio_cleanup plan_digest=%s\n" % digest)
    sys.stdout.write("  (--apply is currently fail-closed: no probe)\n")
    sys.stdout.write("  (precondition intent: no active runs, no recent "
                     "uploads >=%dh)\n" % min_age)
    results = execute(plan, apply=False, min_age_hours=min_age)
    failures = [r for r in results if not r["ok"]]
    if failures:
        sys.stderr.write("minio_cleanup: %d target(s) failed\n" % len(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
