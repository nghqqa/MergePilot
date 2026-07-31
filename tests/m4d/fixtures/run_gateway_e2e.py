"""Real fixture GitHub E2E for the M4-D PRLifecycle production chain.

All lifecycle actions under test enter through ``python -m
skills.pr_lifecycle.run`` in a Python 3.12 container on the fixture-only
Gateway network. Direct ``gh`` and SQL operations are limited to fixture
precondition reads, L2 ticket setup, and deterministic cleanup.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time


ROOT = Path("/mnt/d/goai/mergepilot-os")
EVIDENCE_PATH = ROOT / "evidence/m4/m4d/gateway-e2e.json"
REPOSITORY = "nghqqa/MergePilot-e2e-fixture"
DENIED_REPOSITORY = "nghqqa/MergePilot-e2e-denied"
OWNER, REPO_NAME = REPOSITORY.split("/", 1)
BASE_BRANCH = "main"
GATEWAY_URL = "http://policy-gw-m4d:8083"
RUNNER_IMAGE = "policy-gateway:latest"
ROLE_FILE = Path("/home/ngh/.config/mergepilot/role-tokens-e2e.json")
CONFIG_DIR = Path("/home/ngh/.config/mergepilot")
RUN_STAMP = "m4d-%d" % int(time.time())
TITLE_PREFIX = "M4-D fixture %s" % RUN_STAMP
ADDED_PATH = "m4d-fixture/%s-added.txt" % RUN_STAMP


class E2EFailure(RuntimeError):
    pass


def run_command(args, *, input_text=None, check=True, timeout=120, env=None):
    proc = subprocess.run(
        args,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
    )
    if check and proc.returncode != 0:
        raise E2EFailure(
            "command failed rc=%d: %s\n%s"
            % (proc.returncode, " ".join(args[:4]), proc.stderr[-1000:])
        )
    return proc


def retry_command(args, *, attempts=5, timeout=60):
    last = None
    for attempt in range(attempts):
        last = run_command(args, check=False, timeout=timeout)
        if last.returncode == 0:
            return last
        if attempt + 1 < attempts:
            time.sleep(1 + attempt)
    raise E2EFailure(
        "command failed after retries: %s\n%s"
        % (" ".join(args[:4]), (last.stderr if last else "")[-1000:])
    )


def gh_json(*args):
    proc = retry_command(["gh.exe", *args], attempts=5, timeout=60)
    try:
        return json.loads(proc.stdout)
    except Exception as exc:
        raise E2EFailure("invalid GitHub JSON response") from exc


def gh_write(*args):
    return retry_command(["gh.exe", *args], attempts=5, timeout=90)


def main_sha():
    data = gh_json(
        "api",
        "repos/%s/git/ref/heads/%s" % (REPOSITORY, BASE_BRANCH),
    )
    value = data.get("object", {}).get("sha")
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise E2EFailure("invalid main SHA")
    return value


def read_file(path, ref=BASE_BRANCH):
    data = gh_json(
        "api",
        "repos/%s/contents/%s?ref=%s" % (REPOSITORY, path, ref),
    )
    encoded = data.get("content")
    blob_sha = data.get("sha")
    if not isinstance(encoded, str) or not isinstance(blob_sha, str):
        raise E2EFailure("invalid file response for %s" % path)
    content = base64.b64decode(encoded.replace("\n", "")).decode("utf-8")
    return content, blob_sha


def list_branches():
    data = gh_json(
        "api",
        "repos/%s/branches?per_page=100" % REPOSITORY,
    )
    return sorted(item["name"] for item in data)


def open_prs():
    data = gh_json(
        "pr",
        "list",
        "--repo",
        REPOSITORY,
        "--state",
        "open",
        "--limit",
        "100",
        "--json",
        "number,title,headRefName",
    )
    return data


def load_env_file(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip("\"'")
    return values


AUDIT_ENV = load_env_file(CONFIG_DIR / "audit-db.env")
CONTROLLER_ENV = load_env_file(CONFIG_DIR / "controller.env")
DB_USER = CONTROLLER_ENV.get("PG_USER", "mergepilot")
DB_NAME = CONTROLLER_ENV.get("PG_DATABASE", "mergepilot_audit")
DB_VALUE = CONTROLLER_ENV.get("PG_PASS", "")
if not DB_VALUE:
    raise E2EFailure("fixture DB credential is unavailable")


def sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def psql(sql):
    child_env = os.environ.copy()
    child_env["PG" + "PASSWORD"] = DB_VALUE
    proc = run_command(
        [
            "docker",
            "exec",
            "-e",
            "PG" + "PASSWORD",
            "audit-pg",
            "psql",
            "-U",
            DB_USER,
            "-d",
            DB_NAME,
            "-t",
            "-A",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        ],
        timeout=60,
        env=child_env,
    )
    return proc.stdout.strip()


def canonical_hash(payload):
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def create_approved_ticket(run_id, pull_number, branch, head_sha, action, payload):
    binding_id = "bnd-" + run_id
    args_hash = canonical_hash(payload)
    statements = [
        (
            "INSERT INTO task_runs"
            "(run_id,status,repo,pr_number,current_stage,approval_required) "
            "VALUES(%s,'APPROVAL_PENDING',%s,%d,'l2_awaiting_approval',TRUE) "
            "ON CONFLICT(run_id) DO UPDATE SET "
            "status='APPROVAL_PENDING',current_stage='l2_awaiting_approval',"
            "repo=EXCLUDED.repo,pr_number=EXCLUDED.pr_number,"
            "approval_required=TRUE"
        )
        % (sql_literal(run_id), sql_literal(REPOSITORY), pull_number),
        (
            "INSERT INTO run_pr_bindings"
            "(binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha) "
            "VALUES(%s,%s,%s,%d,%s,%s,%s) "
            "ON CONFLICT(binding_id) DO UPDATE SET "
            "head_sha=EXCLUDED.head_sha,pr_number=EXCLUDED.pr_number,"
            "fix_branch=EXCLUDED.fix_branch"
        )
        % (
            sql_literal(binding_id),
            sql_literal(run_id),
            sql_literal(REPOSITORY),
            pull_number,
            sql_literal(branch),
            sql_literal(BASE_BRANCH),
            sql_literal(head_sha),
        ),
    ]
    for statement in statements:
        psql(statement + ";")
    ticket = psql(
        "SELECT l2_create_ticket(%s,%s,%s::jsonb,%s,24,1);"
        % (
            sql_literal(binding_id),
            sql_literal(action),
            sql_literal(json.dumps(payload, separators=(",", ":"))),
            sql_literal(args_hash),
        )
    ).splitlines()[-1].strip()
    if not re.fullmatch(
        r"tkt-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        ticket,
    ):
        raise E2EFailure("invalid ticket result")
    approved = psql("SELECT l2_approve(%s);" % sql_literal(ticket)).lower()
    if approved not in ("t", "true"):
        raise E2EFailure("ticket approval failed")
    return ticket


with ROLE_FILE.open(encoding="utf-8") as fh:
    ROLE_VALUES = json.load(fh)
if not all(isinstance(ROLE_VALUES.get(role), str) for role in ("fixer", "coordinator")):
    raise E2EFailure("fixture role credentials unavailable")

HMAC_VALUE = secrets.token_urlsafe(48)
tracked_branches = set()
tracked_prs = set()
ticket_values = []
scenario_rows = []
original_readme = None


def request_envelope(business_input):
    return {
        "contract_version": "1",
        "request_id": "req-" + secrets.token_hex(8),
        "trace_id": "trace-" + secrets.token_hex(8),
        "input": business_input,
        "timeout_ms": 90000,
    }


def run_skill(
    business_input,
    *,
    role,
    run_id,
    risk="L1",
    repository=REPOSITORY,
    expected_base_sha=None,
    bad_sha=None,
    parent_sha=None,
):
    env_values = {
        "PYTHONPATH": "/workspace",
        "PYTHONDONTWRITEBYTECODE": "1",
        "MERGEPILOT_PRL_GATEWAY_URL": GATEWAY_URL,
        "MERGEPILOT_PRL_ROLE": role,
        "MERGEPILOT_PRL_TOKEN": ROLE_VALUES[role],
        "MERGEPILOT_PRL_REPO": repository,
        "MERGEPILOT_PRL_BASE_BRANCH": BASE_BRANCH,
        "MERGEPILOT_PRL_RUN_ID": run_id,
        "MERGEPILOT_PRL_RISK_LEVEL": risk,
        "MERGEPILOT_PRL_HMAC_KEY": HMAC_VALUE,
    }
    if expected_base_sha is not None:
        env_values["MERGEPILOT_PRL_EXPECTED_BASE_SHA"] = expected_base_sha
    if bad_sha is not None:
        env_values["MERGEPILOT_PRL_REVERT_BAD_SHA"] = bad_sha
    if parent_sha is not None:
        env_values["MERGEPILOT_PRL_REVERT_PARENT_SHA"] = parent_sha

    fd, env_path = tempfile.mkstemp(prefix="m4d-prl-", suffix=".env")
    os.close(fd)
    os.chmod(env_path, 0o600)
    try:
        with open(env_path, "w", encoding="utf-8", newline="\n") as fh:
            for key, value in env_values.items():
                if "\n" in value or "\r" in value:
                    raise E2EFailure("invalid environment value")
                fh.write("%s=%s\n" % (key, value))
        container_name = "m4d-prl-%s" % secrets.token_hex(6)
        proc = run_command(
            [
                "docker",
                "run",
                "--rm",
                "-i",
                "--name",
                container_name,
                "--label",
                "mergepilot.m4d-e2e=" + RUN_STAMP,
                "--network",
                "hiclab-net",
                "--env-file",
                env_path,
                "-v",
                str(ROOT) + ":/workspace:ro",
                "-w",
                "/workspace",
                "--entrypoint",
                "python3",
                RUNNER_IMAGE,
                "-m",
                "skills.pr_lifecycle.run",
            ],
            input_text=json.dumps(request_envelope(business_input)),
            check=False,
            timeout=150,
        )
    finally:
        try:
            os.unlink(env_path)
        except FileNotFoundError:
            pass

    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise E2EFailure(
            "production CLI emitted %d stdout lines; stderr=%s"
            % (len(lines), proc.stderr[-1000:])
        )
    try:
        envelope = json.loads(lines[0])
    except Exception as exc:
        raise E2EFailure("production CLI stdout is not JSON") from exc
    if envelope.get("name") != "pr-lifecycle":
        raise E2EFailure("unexpected production envelope")
    return proc.returncode, envelope


def record(name, envelope, passed, **extra):
    raw = json.dumps(envelope, sort_keys=True, ensure_ascii=False)
    protected = [
        ROLE_VALUES["fixer"],
        ROLE_VALUES["coordinator"],
        HMAC_VALUE,
        *ticket_values,
    ]
    hits = sum(1 for value in protected if value and value in raw)
    row = {
        "scenario": name,
        "passed": bool(passed and hits == 0),
        "status": envelope.get("status"),
        "error_code": envelope.get("error_code"),
        "message": envelope.get("message"),
        "outcome": (envelope.get("output") or {}).get("outcome"),
        "effect_state": (envelope.get("output") or {}).get("effect_state"),
        "phases": (envelope.get("output") or {}).get("phases", []),
        "policy_reason": (envelope.get("output") or {}).get("policy_reason"),
        "side_effects": sorted(
            item.get("type") for item in envelope.get("side_effects", [])
        ),
        "credential_hits": hits,
        "envelope_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    }
    row.update(extra)
    scenario_rows.append(row)
    if not row["passed"]:
        raise E2EFailure("scenario failed: %s" % name)
    return row


def merge_with_ticket(pr_output, *, run_suffix, merge_method, commit_title):
    pull_number = pr_output["pull_number"]
    payload = {
        "owner": OWNER,
        "repo": REPO_NAME,
        "pullNumber": pull_number,
        "commit_title": commit_title,
        "merge_method": merge_method,
    }
    ticket = create_approved_ticket(
        RUN_STAMP + "-" + run_suffix,
        pull_number,
        pr_output["head_branch"],
        pr_output["head_sha"],
        "merge",
        payload,
    )
    ticket_values.append(ticket)
    business_input = {
        "action": "merge_pr",
        "idempotency_key": RUN_STAMP + "-" + run_suffix + "-merge",
        "pull_number": pull_number,
        "approval_ticket": ticket,
        "merge_method": merge_method,
        "commit_title": commit_title,
    }
    rc, envelope = run_skill(
        business_input,
        role="coordinator",
        run_id=RUN_STAMP + "-" + run_suffix + "-coord",
    )
    return rc, envelope, business_input, ticket


def close_with_ticket(pr_output, *, run_suffix):
    pull_number = pr_output["pull_number"]
    payload = {
        "owner": OWNER,
        "repo": REPO_NAME,
        "pullNumber": pull_number,
        "state": "closed",
    }
    ticket = create_approved_ticket(
        RUN_STAMP + "-" + run_suffix,
        pull_number,
        pr_output["head_branch"],
        pr_output["head_sha"],
        "close",
        payload,
    )
    ticket_values.append(ticket)
    business_input = {
        "action": "close_pr",
        "idempotency_key": RUN_STAMP + "-" + run_suffix + "-close",
        "pull_number": pull_number,
        "approval_ticket": ticket,
    }
    rc, envelope = run_skill(
        business_input,
        role="coordinator",
        run_id=RUN_STAMP + "-" + run_suffix + "-coord",
    )
    return rc, envelope, business_input, ticket


def direct_restore_readme():
    if original_readme is None:
        return
    current, blob_sha = read_file("README.md")
    if current == original_readme:
        return
    gh_write(
        "api",
        "--method",
        "PUT",
        "repos/%s/contents/README.md" % REPOSITORY,
        "-f",
        "message=M4-D fixture cleanup",
        "-f",
        "content=" + base64.b64encode(original_readme.encode("utf-8")).decode("ascii"),
        "-f",
        "sha=" + blob_sha,
        "-f",
        "branch=" + BASE_BRANCH,
    )


def direct_delete_added():
    try:
        _, blob_sha = read_file(ADDED_PATH)
    except Exception:
        return
    gh_write(
        "api",
        "--method",
        "DELETE",
        "repos/%s/contents/%s" % (REPOSITORY, ADDED_PATH),
        "-f",
        "message=M4-D fixture cleanup",
        "-f",
        "sha=" + blob_sha,
        "-f",
        "branch=" + BASE_BRANCH,
    )


def cleanup():
    try:
        for item in open_prs():
            if str(item.get("title", "")).startswith(TITLE_PREFIX):
                tracked_prs.add(int(item["number"]))
        for branch in list_branches():
            if branch.startswith("fix/" + RUN_STAMP):
                tracked_branches.add(branch)
    except Exception:
        pass
    for number in sorted(tracked_prs):
        proc = run_command(
            [
                "gh.exe",
                "pr",
                "close",
                str(number),
                "--repo",
                REPOSITORY,
            ],
            check=False,
            timeout=60,
        )
        _ = proc
    for branch in sorted(tracked_branches):
        run_command(
            [
                "gh.exe",
                "api",
                "--method",
                "DELETE",
                "repos/%s/git/refs/heads/%s" % (REPOSITORY, branch),
            ],
            check=False,
            timeout=60,
        )
    direct_delete_added()
    direct_restore_readme()
    prefix = sql_literal(RUN_STAMP + "%")
    psql(
        "DELETE FROM policy_action_outbox WHERE run_id LIKE %s;"
        "DELETE FROM approvals WHERE run_id LIKE %s;"
        "DELETE FROM run_pr_bindings WHERE run_id LIKE %s;"
        "DELETE FROM task_runs WHERE run_id LIKE %s;"
        % (prefix, prefix, prefix, prefix)
    )


def db_residue():
    prefix = sql_literal(RUN_STAMP + "%")
    value = psql(
        "SELECT "
        "(SELECT count(*) FROM policy_action_outbox WHERE run_id LIKE %s)+"
        "(SELECT count(*) FROM approvals WHERE run_id LIKE %s)+"
        "(SELECT count(*) FROM run_pr_bindings WHERE run_id LIKE %s)+"
        "(SELECT count(*) FROM task_runs WHERE run_id LIKE %s);"
        % (prefix, prefix, prefix, prefix)
    )
    return int(value.splitlines()[-1])


def audit_counts(ticket):
    claimed = int(
        psql(
            "SELECT count(*) FROM mcp_calls WHERE ticket_id=%s "
            "AND reason_code='L2_CLAIMED';" % sql_literal(ticket)
        ).splitlines()[-1]
    )
    complete = int(
        psql(
            "SELECT count(*) FROM mcp_calls WHERE ticket_id=%s "
            "AND reason_code='L2_COMPLETE';" % sql_literal(ticket)
        ).splitlines()[-1]
    )
    status = psql(
        "SELECT status FROM approvals WHERE ticket_id=%s;" % sql_literal(ticket)
    ).splitlines()[-1]
    return claimed, complete, status


def main():
    global original_readme
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    baseline_open = open_prs()
    baseline_branches = list_branches()
    if baseline_open or baseline_branches != [BASE_BRANCH]:
        raise E2EFailure(
            "fixture baseline is not clean: open=%d branches=%r"
            % (len(baseline_open), baseline_branches)
        )

    original_readme, _ = read_file("README.md")
    initial_sha = main_sha()
    modified_readme = (
        original_readme.rstrip("\n")
        + "\n\nM4-D fixture verification %s\n" % RUN_STAMP
    )
    fix_input = {
        "action": "ensure_fix_pr",
        "idempotency_key": RUN_STAMP + "-fix",
        "changes": [{"path": "README.md", "content": modified_readme}],
        "commit_message": "test: M4-D fixture change",
        "pr_title": TITLE_PREFIX + " fix",
        "pr_body": "Fixture-only production-chain verification.",
    }
    fix_run = RUN_STAMP + "-fixer"
    recovery_count = 0
    for attempt in range(3):
        rc, fix_created = run_skill(
            fix_input,
            role="fixer",
            run_id=fix_run,
            expected_base_sha=initial_sha,
        )
        created_output = fix_created.get("output") or {}
        if rc == 0 and created_output.get("outcome") in ("CREATED", "EXISTING"):
            break
        if (
            fix_created.get("message")
            not in ("PRL_GATEWAY_UNAVAILABLE", "PRL_EFFECT_UNKNOWN")
            or attempt == 2
        ):
            break
        recovery_count += 1
        time.sleep(3 + attempt)
    created_output = fix_created.get("output") or {}
    if created_output.get("head_branch"):
        tracked_branches.add(created_output["head_branch"])
    if created_output.get("pull_number"):
        tracked_prs.add(created_output["pull_number"])
    debug_state = {}
    if rc != 0 or created_output.get("outcome") not in ("CREATED", "EXISTING"):
        matching_branches = [
            branch for branch in list_branches()
            if branch.startswith("fix/" + fix_run + "-")
        ]
        debug_state["matching_branch_count"] = len(matching_branches)
        if len(matching_branches) == 1:
            tracked_branches.add(matching_branches[0])
            branch_ref = gh_json(
                "api",
                "repos/%s/git/ref/heads/%s" % (
                    REPOSITORY, matching_branches[0]
                ),
            )
            branch_sha = branch_ref.get("object", {}).get("sha")
            commit_data = gh_json(
                "api",
                "repos/%s/commits/%s" % (REPOSITORY, branch_sha),
            )
            branch_content, _ = read_file("README.md", matching_branches[0])
            debug_state.update({
                "branch_sha_valid": bool(
                    isinstance(branch_sha, str)
                    and re.fullmatch(r"[0-9a-f]{40}", branch_sha)
                ),
                "branch_advanced": branch_sha != initial_sha,
                "parent_matches_base": (
                    len(commit_data.get("parents", [])) == 1
                    and commit_data["parents"][0].get("sha") == initial_sha
                ),
                "commit_paths": sorted(
                    item.get("filename") for item in commit_data.get("files", [])
                ),
                "content_match": branch_content == modified_readme,
            })
            candidates = gh_json(
                "api",
                (
                    "repos/%s/pulls?state=all&head=%s:%s&base=%s"
                    % (REPOSITORY, OWNER, matching_branches[0], BASE_BRANCH)
                ),
            )
            debug_state["matching_pr_count"] = len(candidates)
            if len(candidates) == 1:
                pr = candidates[0]
                tracked_prs.add(int(pr["number"]))
                files = gh_json(
                    "api",
                    "repos/%s/pulls/%s/files" % (REPOSITORY, pr["number"]),
                )
                debug_state.update({
                    "title_match": pr.get("title") == fix_input["pr_title"],
                    "draft": pr.get("draft"),
                    "body_has_marker_prefix": str(pr.get("body", "")).startswith(
                        "MergePilot-PRL-Marker: v1 "
                    ),
                    "paths": sorted(item.get("filename") for item in files),
                })
    record(
        "fix_create",
        fix_created,
        rc == 0
        and fix_created.get("status") == "OK"
        and created_output.get("outcome") in ("CREATED", "EXISTING"),
        pull_number=created_output.get("pull_number"),
        recovery_count=recovery_count,
        debug_state=debug_state,
    )

    rc, fix_replay = run_skill(
        fix_input,
        role="fixer",
        run_id=fix_run,
        expected_base_sha=initial_sha,
    )
    replay_output = fix_replay.get("output") or {}
    record(
        "fix_replay",
        fix_replay,
        rc == 0
        and replay_output.get("outcome") == "EXISTING"
        and replay_output.get("pull_number") == created_output.get("pull_number"),
    )

    conflict_input = dict(fix_input)
    conflict_input["changes"] = [{
        "path": fix_input["changes"][0]["path"],
        "content": fix_input["changes"][0]["content"] + "conflicting replay\n",
    }]
    branches_before_conflict = list_branches()
    prs_before_conflict = open_prs()
    rc, conflict_env = run_skill(
        conflict_input,
        role="fixer",
        run_id=fix_run,
        expected_base_sha=initial_sha,
    )
    record(
        "idempotency_conflict",
        conflict_env,
        rc == 4
        and conflict_env.get("error_code") == "DENIED"
        and conflict_env.get("message") == "PRL_IDEMPOTENCY_CONFLICT"
        and not any(
            item.get("type") == "github_write"
            for item in conflict_env.get("side_effects", [])
        )
        and list_branches() == branches_before_conflict
        and open_prs() == prs_before_conflict,
    )

    forbidden = dict(fix_input)
    forbidden["idempotency_key"] = RUN_STAMP + "-forbidden"
    forbidden["changes"] = [{"path": "../blocked.txt", "content": "blocked"}]
    rc, forbidden_env = run_skill(
        forbidden,
        role="fixer",
        run_id=RUN_STAMP + "-forbidden",
        expected_base_sha=initial_sha,
    )
    record(
        "forbidden_path",
        forbidden_env,
        rc == 2
        and forbidden_env.get("error_code") == "INVALID_INPUT"
        and forbidden_env.get("side_effects") == [],
    )

    role_denied_input = dict(fix_input)
    role_denied_input["idempotency_key"] = RUN_STAMP + "-role-denied"
    rc, role_denied = run_skill(
        role_denied_input,
        role="coordinator",
        run_id=RUN_STAMP + "-role-denied",
        expected_base_sha=initial_sha,
    )
    record(
        "role_denial",
        role_denied,
        rc == 4
        and role_denied.get("error_code") == "DENIED"
        and role_denied.get("message") == "PRL_ROLE_ACTION_DENIED"
        and role_denied.get("side_effects") == [],
    )

    repo_denied_input = dict(fix_input)
    repo_denied_input["idempotency_key"] = RUN_STAMP + "-repo-denied"
    rc, repo_denied = run_skill(
        repo_denied_input,
        role="fixer",
        run_id=RUN_STAMP + "-repo-denied",
        repository=DENIED_REPOSITORY,
        expected_base_sha=initial_sha,
    )
    record(
        "repo_denial",
        repo_denied,
        rc == 4
        and repo_denied.get("error_code") == "DENIED"
        and repo_denied.get("message") == "PRL_POLICY_DENIED"
        and not any(
            item.get("type") == "github_write"
            for item in repo_denied.get("side_effects", [])
        ),
    )

    denied_merge = {
        "action": "merge_pr",
        "idempotency_key": RUN_STAMP + "-ticket-denied",
        "pull_number": created_output["pull_number"],
        "approval_ticket": "tkt-00000000-0000-4000-8000-000000000099",
        "merge_method": "squash",
        "commit_title": TITLE_PREFIX + " denied",
    }
    rc, ticket_denied = run_skill(
        denied_merge,
        role="coordinator",
        run_id=RUN_STAMP + "-ticket-denied",
    )
    pr_after_denial = gh_json(
        "pr",
        "view",
        str(created_output["pull_number"]),
        "--repo",
        REPOSITORY,
        "--json",
        "state,mergedAt",
    )
    record(
        "ticket_denial",
        ticket_denied,
        rc == 4
        and ticket_denied.get("error_code") == "DENIED"
        and pr_after_denial.get("state") == "OPEN"
        and pr_after_denial.get("mergedAt") is None,
    )

    close_fix_input = {
        "action": "ensure_fix_pr",
        "idempotency_key": RUN_STAMP + "-close-setup",
        "changes": [{
            "path": "README.md",
            "content": original_readme + "\nM4-D close verification %s\n" % RUN_STAMP,
        }],
        "commit_message": "test: prepare M4-D close fixture",
        "pr_title": TITLE_PREFIX + " close",
        "pr_body": "Fixture-only close production-chain verification.",
    }
    close_fix_run = RUN_STAMP + "-close-fixer"
    rc_close_setup, close_created = run_skill(
        close_fix_input,
        role="fixer",
        run_id=close_fix_run,
        expected_base_sha=initial_sha,
    )
    close_created_output = close_created.get("output") or {}
    if (
        rc_close_setup != 0
        or close_created_output.get("outcome") not in ("CREATED", "EXISTING")
    ):
        record(
            "close_once",
            close_created,
            False,
            setup_outcome=close_created_output.get("outcome"),
        )
    tracked_branches.add(close_created_output["head_branch"])
    tracked_prs.add(close_created_output["pull_number"])
    rc_close, close_env, close_business, close_ticket = close_with_ticket(
        close_created_output,
        run_suffix="close",
    )
    close_output = close_env.get("output") or {}
    rc_close_replay, close_replay = run_skill(
        close_business,
        role="coordinator",
        run_id=RUN_STAMP + "-close-coord",
    )
    close_replay_output = close_replay.get("output") or {}
    close_claimed, close_complete, close_ticket_status = audit_counts(close_ticket)
    record(
        "close_once",
        close_env,
        rc_close == 0
        and close_output.get("outcome") == "CLOSED"
        and rc_close_replay == 0
        and close_replay_output.get("outcome") == "ALREADY_CLOSED"
        and close_claimed == 1
        and close_complete == 1
        and close_ticket_status == "USED",
        setup_outcome=close_created_output.get("outcome"),
        replay_outcome=close_replay_output.get("outcome"),
        l2_claimed=close_claimed,
        l2_complete=close_complete,
        ticket_status=close_ticket_status,
    )

    merge_title = TITLE_PREFIX + " merge"
    rc, merge_env, merge_business, merge_ticket = merge_with_ticket(
        created_output,
        run_suffix="fix",
        merge_method="squash",
        commit_title=merge_title,
    )
    merge_output = merge_env.get("output") or {}
    rc_replay, merge_replay = run_skill(
        merge_business,
        role="coordinator",
        run_id=RUN_STAMP + "-fix-coord",
    )
    replay_merge_output = merge_replay.get("output") or {}
    claimed, complete, ticket_status = audit_counts(merge_ticket)
    record(
        "merge_once",
        merge_env,
        rc == 0
        and merge_output.get("outcome") == "MERGED"
        and rc_replay == 0
        and replay_merge_output.get("outcome") == "ALREADY_MERGED"
        and claimed == 1
        and complete == 1
        and ticket_status == "USED",
        replay_outcome=replay_merge_output.get("outcome"),
        l2_claimed=claimed,
        l2_complete=complete,
        ticket_status=ticket_status,
    )

    bad_sha = merge_output["result_sha"]
    revert_input = {
        "action": "ensure_revert_pr",
        "idempotency_key": RUN_STAMP + "-revert-modified",
        "commit_message": "revert: M4-D fixture change",
        "pr_title": TITLE_PREFIX + " revert modified",
        "pr_body": "Restore the verified parent content.",
    }
    revert_run = RUN_STAMP + "-revert"
    rc, revert_created = run_skill(
        revert_input,
        role="fixer",
        run_id=revert_run,
        bad_sha=bad_sha,
        parent_sha=initial_sha,
    )
    revert_output = revert_created.get("output") or {}
    if revert_output.get("head_branch"):
        tracked_branches.add(revert_output["head_branch"])
    if revert_output.get("pull_number"):
        tracked_prs.add(revert_output["pull_number"])
    record(
        "revert_modified",
        revert_created,
        rc == 0
        and revert_output.get("outcome") == "CREATED"
        and revert_output.get("draft") is True
        and revert_output.get("changed_paths") == ["README.md"],
        pull_number=revert_output.get("pull_number"),
    )

    gh_write(
        "pr",
        "ready",
        str(revert_output["pull_number"]),
        "--repo",
        REPOSITORY,
    )
    rc, revert_merge, _, revert_ticket = merge_with_ticket(
        revert_output,
        run_suffix="revert",
        merge_method="merge",
        commit_title=TITLE_PREFIX + " merge revert",
    )
    if rc != 0 or (revert_merge.get("output") or {}).get("outcome") != "MERGED":
        raise E2EFailure("revert PR merge failed")
    ticket_values.append(revert_ticket)
    restored, _ = read_file("README.md")
    if restored != original_readme:
        raise E2EFailure("revert did not restore README content")

    added_parent = main_sha()
    added_input = {
        "action": "ensure_fix_pr",
        "idempotency_key": RUN_STAMP + "-add",
        "changes": [{"path": ADDED_PATH, "content": "fixture added file\n"}],
        "commit_message": "test: add M4-D fixture file",
        "pr_title": TITLE_PREFIX + " add",
        "pr_body": "Fixture-only added-file rollback case.",
    }
    rc, added_created = run_skill(
        added_input,
        role="fixer",
        run_id=RUN_STAMP + "-add",
        expected_base_sha=added_parent,
    )
    added_output = added_created.get("output") or {}
    if rc != 0 or added_output.get("outcome") != "CREATED":
        raise E2EFailure("added-file setup PR failed")
    tracked_branches.add(added_output["head_branch"])
    tracked_prs.add(added_output["pull_number"])
    rc, added_merge, _, _ = merge_with_ticket(
        added_output,
        run_suffix="add",
        merge_method="squash",
        commit_title=TITLE_PREFIX + " merge add",
    )
    added_merge_output = added_merge.get("output") or {}
    if rc != 0 or added_merge_output.get("outcome") != "MERGED":
        raise E2EFailure("added-file setup merge failed")

    rc, added_revert = run_skill(
        {
            "action": "ensure_revert_pr",
            "idempotency_key": RUN_STAMP + "-revert-added",
            "commit_message": "revert: added fixture file",
            "pr_title": TITLE_PREFIX + " reject added revert",
            "pr_body": "This request must fail before a write.",
        },
        role="fixer",
        run_id=RUN_STAMP + "-revert-added",
        bad_sha=added_merge_output["result_sha"],
        parent_sha=added_parent,
    )
    record(
        "revert_added_rejected",
        added_revert,
        rc == 4
        and added_revert.get("error_code") == "DENIED"
        and added_revert.get("message") == "PRL_REVERT_DELETE_UNSUPPORTED"
        and not any(
            item.get("type") in ("network_write", "github_write")
            for item in added_revert.get("side_effects", [])
        ),
    )

    cleanup()
    residue = {
        "open_prs": len(open_prs()),
        "fix_branches": len(
            [branch for branch in list_branches() if branch.startswith("fix/")]
        ),
        "db_rows": db_residue(),
        "runner_containers": int(
            run_command(
                [
                    "docker",
                    "ps",
                    "-a",
                    "--filter",
                    "label=mergepilot.m4d-e2e=" + RUN_STAMP,
                    "--format",
                    "{{.ID}}",
                ]
            ).stdout.count("\n")
        ),
    }
    all_passed = (
        len(scenario_rows) == 11
        and all(item["passed"] for item in scenario_rows)
        and residue == {
            "open_prs": 0,
            "fix_branches": 0,
            "db_rows": 0,
            "runner_containers": 0,
        }
    )
    result = {
        "schema_version": "1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "target_repository": REPOSITORY,
        "fixture_run": RUN_STAMP,
        "production_chain": [
            "python -m skills.pr_lifecycle.run",
            "core.run",
            "PolicyGatewayAdapter",
            "Policy Gateway",
            "github-mcp",
            "GitHub fixture",
        ],
        "runner": {
            "image": RUNNER_IMAGE,
            "python": run_command(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--entrypoint",
                    "python3",
                    RUNNER_IMAGE,
                    "--version",
                ]
            ).stdout.strip(),
            "dependencies": run_command(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--entrypoint",
                    "python3",
                    RUNNER_IMAGE,
                    "-c",
                    (
                        "import importlib.metadata as m;"
                        "print(m.version('mcp'),m.version('httpx'),m.version('anyio'))"
                    ),
                ]
            ).stdout.strip(),
        },
        "scenarios": scenario_rows,
        "residue": residue,
        "all_passed": all_passed,
    }
    with EVIDENCE_PATH.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    if not all_passed:
        raise E2EFailure("final E2E residue or scenario gate failed")
    print("M4-D fixture E2E PASSED: 11 scenarios; residue=0")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        partial = {
            "schema_version": "1",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "target_repository": REPOSITORY,
            "fixture_run": RUN_STAMP,
            "production_chain": [
                "python -m skills.pr_lifecycle.run",
                "core.run",
                "PolicyGatewayAdapter",
                "Policy Gateway",
                "github-mcp",
                "GitHub fixture",
            ],
            "scenarios": scenario_rows,
            "failure_type": type(exc).__name__,
            "residue": {"open_prs": -1, "fix_branches": -1, "db_rows": -1},
            "all_passed": False,
        }
        with EVIDENCE_PATH.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(partial, ensure_ascii=False, indent=2) + "\n")
        try:
            cleanup()
        finally:
            raise
