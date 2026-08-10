#!/usr/bin/env python3
"""RUN_ID-scoped end-of-run cleanup for HiClaw task artifacts (DRY-RUN ONLY
in this candidate).

Cleans ONLY paths scoped to ONE specific RUN_ID. NEVER uses broad globs,
container-name wildcards, or ``docker prune`` / ``docker rm -aq``.

Targets (all scoped by the validated RUN_ID):
  * MinIO:  <bucket>/shared/tasks/<RUN_ID>-review
            <bucket>/shared/tasks/<RUN_ID>-fix
            <bucket>/shared/tasks/<RUN_ID>-verify
  * Local mirror (controller): /root/hiclaw-fs/shared/tasks/<RUN_ID>-{review,fix,verify}

This tool is SEPARATE from minio_cleanup.py: it specifically owns the
RUN_ID-scoped shared/tasks lifecycle (after evidence has been archived).
minio_cleanup.py owns temp/cache (.codex/tmp, .npm/_npx, multipart) and
explicitly EXCLUDES shared/tasks.

DRY-RUN ONLY in this candidate. ``--apply`` is FAIL-CLOSED: this tool ships
no authoritative precondition probe (no verifiable source for RUN_ID-in-
production-records / run-ended / evidence-source_commit-binding), so
``--apply`` returns a stable non-zero status (3) WITHOUT building a plan or
calling mc/docker. See ``check_preconditions`` for the required gate.

The executor is injectable for unit tests (no Docker / mc / WSL).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
STAGES = ("review", "fix", "verify")
CONTROLLER = "hiclaw-controller"
CONTROLLER_MIRROR_BASE = "/root/hiclaw-fs/shared/tasks"


def validate_run_id(run_id):
    """Validate RUN_ID charset/format. Raises ValueError on invalid input.

    The charset ``[A-Za-z0-9._-]`` excludes ``/`` and ``..`` so path
    traversal is structurally impossible.
    """
    if not run_id or not isinstance(run_id, str):
        raise ValueError("RUN_ID is required")
    if "/" in run_id or "\\" in run_id or ".." in run_id:
        raise ValueError("RUN_ID must not contain path separators or '..': %r"
                         % run_id)
    if not RUN_ID_RE.match(run_id):
        raise ValueError("RUN_ID charset must be [A-Za-z0-9._-] (1-64 chars): %r"
                         % run_id)
    return run_id


def check_preconditions(run_id, run_exists_fn=None, run_ended_fn=None,
                        evidence_verified_fn=None):
    """Fail-closed authoritative precondition gate for ``--apply``.

    All three injectable probes must return exactly True for deletion to
    proceed:
      run_exists_fn(run_id)        -> RUN_ID present in authoritative
                                      production records
      run_ended_fn(run_id)         -> the run has ended
      evidence_verified_fn(run_id) -> evidence exists AND source_commit /
                                      binding relationship verified

    Any callback that is None (not wired), False, or None (cannot determine)
    -> (False, reasons). Never raises; never guesses. This is the gate that
    turns "evidence archived" from a text hint into a programmatic fail-closed
    requirement.
    """
    reasons = []
    if (run_exists_fn is None or run_ended_fn is None
            or evidence_verified_fn is None):
        reasons.append("authoritative precondition probes not wired "
                       "(fail-closed)")
        return (False, reasons)
    exists = run_exists_fn(run_id)
    if exists is not True:
        reasons.append("RUN_ID not confirmed in authoritative production "
                       "records (result=%r)" % exists)
    ended = run_ended_fn(run_id)
    if ended is not True:
        reasons.append("run not confirmed ended (result=%r)" % ended)
    verified = evidence_verified_fn(run_id)
    if verified is not True:
        reasons.append("evidence not confirmed present + source_commit/binding "
                       "verified (result=%r)" % verified)
    return (len(reasons) == 0, reasons)


def build_plan(run_id, alias="local", bucket="hiclaw-storage",
               include_local_mirror=True):
    """Return an ordered list of cleanup targets scoped to ``run_id``.

    Each target is a dict:
      {"kind": "minio"|"exec", "argv": [...], "desc": str}
    """
    run_id = validate_run_id(run_id)
    plan = []
    for stage in STAGES:
        prefix = "%s-%s" % (run_id, stage)
        plan.append({
            "kind": "minio",
            "argv": ["mc", "rm", "--recursive", "--force",
                     "%s/%s/shared/tasks/%s" % (alias, bucket, prefix)],
            "desc": "minio shared/tasks/%s" % prefix,
        })
        if include_local_mirror:
            plan.append({
                "kind": "exec",
                "argv": ["docker", "exec", CONTROLLER, "rm", "-rf",
                         "%s/%s" % (CONTROLLER_MIRROR_BASE, prefix)],
                "desc": "controller mirror shared/tasks/%s" % prefix,
            })
    return plan


def _default_runner(argv):
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError("rc=%d stderr=%s" % (proc.returncode, proc.stderr[:200]))
    return proc.stdout


def execute(plan, runner=None, apply=False):
    """Execute the plan. If ``apply`` is False, only print (dry-run).

    Returns a list of result dicts: {"desc", "applied", "ok", "error"}.
    """
    runner = runner or _default_runner
    results = []
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
            runner(target["argv"])
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
    run_id = None
    for a in argv:
        if not a.startswith("-"):
            run_id = a
            break
    if not run_id:
        sys.stderr.write("usage: cleanup_run.py <RUN_ID> [--apply]\n")
        return 2
    try:
        run_id = validate_run_id(run_id)
    except ValueError as exc:
        sys.stderr.write("cleanup_run: invalid RUN_ID: %s\n" % exc)
        return 2

    # --apply is FAIL-CLOSED: this candidate ships no authoritative
    # precondition probe. Refuse with a stable non-zero status, clear reason,
    # NO plan built, NO mc/docker call, NO traceback. The dry-run path below
    # is the only supported mode.
    if apply:
        _ok, reasons = check_preconditions(run_id)  # all None -> fail-closed
        sys.stderr.write(
            "cleanup_run: --apply is not supported in this candidate: %s. "
            "Refusing to delete (fail-closed, status 3). Re-run without "
            "--apply for a dry-run plan.\n" % "; ".join(reasons))
        return 3

    alias = os.environ.get("MP_MC_ALIAS", "local")
    bucket = os.environ.get("MP_MC_BUCKET", "hiclaw-storage")
    include_local = os.environ.get(
        "MP_CLEANUP_LOCAL_MIRROR", "1").lower() in ("1", "true", "yes")
    plan = build_plan(run_id, alias=alias, bucket=bucket,
                      include_local_mirror=include_local)
    sys.stdout.write("cleanup_run DRY-RUN (default) run_id=%s targets=%d\n"
                     % (run_id, len(plan)))
    sys.stdout.write("  (--apply is currently fail-closed: no authoritative "
                     "precondition probe)\n")
    results = execute(plan, apply=False)
    failures = [r for r in results if not r["ok"]]
    if failures:
        sys.stderr.write("cleanup_run: %d target(s) failed\n" % len(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
