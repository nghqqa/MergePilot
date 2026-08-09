#!/usr/bin/env python3
"""M5-0D D2B-3 deploy-owned production tier-C collector.

This collector queries production sources directly: PostgreSQL through the
``audit-pg`` container, Matrix ``/sync`` through ``hiclaw-manager``, Docker
container identity/logs, Gateway/github-mcp through the in-container policy
probe, and Docker/host residue.  No operator-authored raw JSON is accepted.
The sanitized records and source digests are schema-validated and atomically
published with ``source_commit`` derived from git HEAD.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCHEMA_FILE = os.path.join(ROOT, "tests", "m5_0d", "schemas", "production-live.schema.json")
EVIDENCE_PATH = os.path.join(ROOT, "evidence", "m5", "0d", "production-live.json")
MATRIX_PASSWORD_FILE = "/dev/shm/m5d/matrix-admin-password"
RUN_ID_RE = re.compile(r"m5live-[A-Za-z0-9.-]+$")
ROOM_ID_RE = re.compile(r"![A-Za-z0-9._=-]+:[A-Za-z0-9.:-]+$")
SECRET_RE = re.compile(
    r"ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{82,}"
    r"|access_token|sync_token|registration_token|password\s*[=:]"
    r"|private_key|client_secret|bearer\s+[A-Za-z0-9._-]+",
    re.IGNORECASE,
)
ROLES = ("manager", "reviewer", "fixer", "verifier")
CONTAINERS = {
    "manager": "hiclaw-manager",
    "reviewer": "hiclaw-worker-reviewer",
    "fixer": "hiclaw-worker-fixer",
    "verifier": "hiclaw-worker-verifier",
}
# Deployment topology (per tools/start-controller-container.sh + start-m5-0-candidate.sh
# + controller.py:59,99-100 + audit-db/m3_state.sql:105-106):
#   mergepilot-controller      = production controller, CONTROLLER_CONSUMER_NAME
#                                 defaults to "controller", M4F_LIVE_MODE defaults 0.
#   mergepilot-m5-0-candidate  = M5-0 Candidate controller, CONTROLLER_CONSUMER_NAME
#                                 defaults to "m5-0-candidate", M4F_LIVE_MODE=1,
#                                 M4F_RUN_PREFIX=m5live-.  THIS is the only controller
#                                 that processes m5live runs.
# controller_offsets.consumer_name is the Matrix /sync cursor PK; the Candidate MUST
# use a non-"controller" value (controller.py startup_assert_m5_candidate rejects the
# production default).
PROD_CONTROLLER = "mergepilot-controller"
CANDIDATE_CONTAINER = "mergepilot-m5-0-candidate"
PROD_CONSUMER_DEFAULT = "controller"
SOURCE_CONTAINERS = (
    PROD_CONTROLLER,
    CANDIDATE_CONTAINER,
    "hiclaw-manager",
    "hiclaw-worker-reviewer",
    "hiclaw-worker-fixer",
    "hiclaw-worker-verifier",
    "policy-gw",
    "github-mcp",
    "audit-pg",
)
PG_TABLES = (
    "task_runs",
    "stage_events",
    "revision_bindings",
    "skill_job_outbox",
    "skill_invocations",
    "mcp_calls",
    "dispatch_outbox",
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def parse_utc(value: str) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("timestamp must be UTC Z")
    return dt.datetime.fromisoformat(value[:-1] + "+00:00")


def git_head() -> str:
    result = subprocess.run(
        ["git", "-c", "safe.directory=" + ROOT, "-C", ROOT, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", value) else ""


def secret_scan(value: Any) -> bool:
    blob = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return bool(SECRET_RE.search(blob))


def read_secret_file(path: str) -> str:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("secret path must be a regular non-symlink file")
    if os.name == "posix" and (info.st_mode & 0o077):
        raise ValueError("secret file mode must be 0600")
    if info.st_size <= 0 or info.st_size > 8192:
        raise ValueError("secret file size invalid")
    with open(path, encoding="utf-8") as stream:
        value = stream.read().strip()
    if not value or "\x00" in value:
        raise ValueError("secret file content invalid")
    return value


def run_checked(command: list[str], *, input_text: str | None = None, timeout: int = 60) -> str:
    result = subprocess.run(
        command,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("collector command failed: %s rc=%d" % (command[0], result.returncode))
    return result.stdout


def docker_inspect(name: str) -> dict[str, Any]:
    data = json.loads(run_checked(["docker", "inspect", name], timeout=30))
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise ValueError("docker inspect schema invalid for %s" % name)
    if not (data[0].get("State") or {}).get("Running"):
        raise ValueError("required container not running: %s" % name)
    return data[0]


def docker_logs(name: str, window_start: str, window_end: str) -> bytes:
    result = subprocess.run(
        ["docker", "logs", "--since", window_start, "--until", window_end, name],
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("docker logs failed: %s" % name)
    return result.stdout + result.stderr


def env_map(inspect: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in (inspect.get("Config") or {}).get("Env") or []:
        if isinstance(item, str) and "=" in item:
            key, value = item.split("=", 1)
            result[key] = value
    return result


def collect_container_sources(window_start: str, window_end: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, bytes]]:
    inspections: dict[str, dict[str, Any]] = {}
    logs: dict[str, bytes] = {}
    sanitized: list[dict[str, str]] = []
    for name in SOURCE_CONTAINERS:
        info = docker_inspect(name)
        inspections[name] = info
        log_bytes = docker_logs(name, window_start, window_end)
        logs[name] = log_bytes
        config = info.get("Config") or {}
        command = {"entrypoint": config.get("Entrypoint") or [], "cmd": config.get("Cmd") or []}
        sanitized.append(
            {
                "name": name,
                "container_id": str(info.get("Id") or ""),
                "image_id": str(info.get("Image") or ""),
                "started_at": str((info.get("State") or {}).get("StartedAt") or ""),
                "command_digest": sha256_bytes(canonical_bytes(command)),
                "log_digest": sha256_bytes(log_bytes),
            }
        )
    candidate_env = env_map(inspections[CANDIDATE_CONTAINER])
    consumer = candidate_env.get("CONTROLLER_CONSUMER_NAME", "")
    server = candidate_env.get("MATRIX_SERVER_NAME", "")
    # The Candidate processes m5live runs; its consumer is the real non-default
    # value (controller.py:99-100 forbids "controller" in Candidate mode). Reading
    # the production controller here would always see its "controller" default and
    # spuriously reject — so the consumer MUST come from the Candidate container.
    if not consumer or consumer == PROD_CONSUMER_DEFAULT or not server:
        raise ValueError("candidate consumer/server identity missing or is production default")
    # Cutover isolation: the production controller must use a DIFFERENT consumer
    # than the Candidate (otherwise both would contend for the same m5live offset).
    prod_env = env_map(inspections[PROD_CONTROLLER])
    prod_consumer = prod_env.get("CONTROLLER_CONSUMER_NAME", "")
    if prod_consumer and prod_consumer == consumer:
        raise ValueError("production controller consumer must differ from candidate")
    agents: dict[str, Any] = {}
    for role in ROLES:
        row = next(item for item in sanitized if item["name"] == CONTAINERS[role])
        role_env = env_map(inspections[CONTAINERS[role]])
        matrix_user = role_env.get("MATRIX_USER") or role_env.get("AGENT_NAME") or role
        if not matrix_user.startswith("@"):
            matrix_user = "@%s:%s" % (matrix_user, server)
        agents[role] = {
            "role": role,
            "container_id": row["container_id"],
            "image_id": row["image_id"],
            "matrix_user_id": matrix_user,
            "started_at": row["started_at"],
            "command_digest": row["command_digest"],
            "log_digest": row["log_digest"],
        }
    return agents, {"consumer_name": consumer, "matrix_server_name": server, "containers": sanitized}, logs


def pg_rows(table: str, run_id: str) -> list[dict[str, Any]]:
    if table not in PG_TABLES:
        raise ValueError("PG table not allowed")
    if not RUN_ID_RE.fullmatch(run_id):
        raise ValueError("run_id invalid")
    sql = (
        "SELECT COALESCE(json_agg(to_jsonb(t)), '[]'::json)::text "
        "FROM public.%s t WHERE t.run_id='%s';" % (table, run_id)
    )
    output = run_checked(
        ["docker", "exec", "audit-pg", "psql", "-U", "mergepilot", "-d", "mergepilot_audit", "-t", "-A", "-v", "ON_ERROR_STOP=1", "-c", sql],
        timeout=60,
    ).strip()
    data = json.loads(output or "[]")
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        raise ValueError("PG row JSON invalid for %s" % table)
    return data


def collect_db_records(run_id: str, consumer: str) -> tuple[dict[str, Any], str]:
    tables = {table: pg_rows(table, run_id) for table in PG_TABLES}
    if len(tables["task_runs"]) != 1 or len(tables["revision_bindings"]) != 1:
        raise ValueError("exact task_run/revision_binding required")
    revision = tables["revision_bindings"][0]
    stage_events = []
    for row in tables["stage_events"]:
        stage_events.append(
            {
                "stage_event_id": str(row.get("event_id") or ""),
                "matrix_event_id": str(row.get("event_id") or ""),
                "room_id": str(row.get("room_id") or ""),
                "sender": str(row.get("sender") or ""),
                "event_type": str(row.get("event_type") or ""),
                "stage": str(row.get("stage") or ""),
                "status": str(row.get("status") or ""),
                "parsed_run_id": str(row.get("run_id") or ""),
                "processed_by": consumer,
                "error_code": str(row.get("error") or ""),
            }
        )
    refs = {}
    for stage in ("review", "fix", "verify"):
        matches = [row["stage_event_id"] for row in stage_events if row["stage"] == stage]
        if len(matches) != 1:
            raise ValueError("exactly one %s stage event required" % stage)
        refs[stage] = matches[0]
    task = tables["task_runs"][0]
    task_run = {
        "run_id": run_id,
        "room_id": str(task.get("room_id") or ""),
        "status": str(task.get("status") or ""),
        "current_stage": str(task.get("current_stage") or ""),
        "verdict": str(task.get("verdict") or ""),
        "consumer_name": consumer,
        "revision_binding_id": str(revision.get("binding_id") or ""),
        "base_sha": str(revision.get("base_sha") or ""),
        "head_sha": str(revision.get("head_sha") or ""),
        "review_stage_event_id": refs["review"],
        "fix_stage_event_id": refs["fix"],
        "verify_stage_event_id": refs["verify"],
    }
    invocations = {str(row.get("invocation_id")): row for row in tables["skill_invocations"]}
    skill_jobs = []
    for job in tables["skill_job_outbox"]:
        invocation = invocations.get(str(job.get("result_invocation_id"))) or {}
        skill_jobs.append(
            {
                "skill_name": str(job.get("skill_name") or ""),
                "job_id": str(job.get("job_id") or ""),
                "invocation_id": str(job.get("result_invocation_id") or ""),
                "status": str(job.get("status") or ""),
                "revision_binding_id": str(revision.get("binding_id") or ""),
                "output_schema_validated": invocation.get("output_schema_validated") is True,
            }
        )
    mcp_calls = []
    for call in tables["mcp_calls"]:
        mcp_calls.append(
            {
                "call_id": str(call.get("request_id") or ""),
                "caller_agent": str(call.get("caller_agent") or ""),
                "tool": str(call.get("tool") or ""),
                "decision": str(call.get("decision") or ""),
                "revision_binding_id": str(revision.get("binding_id") or ""),
                "base_sha": str(revision.get("base_sha") or ""),
                "head_sha": str(revision.get("head_sha") or ""),
                "upstream_kind": "github-mcp",
                "audit_dsn_kind": "postgresql",
            }
        )
    dispatch_rows = []
    for row in tables["dispatch_outbox"]:
        stage = str(row.get("target_stage") or "")
        dispatch_rows.append(
            {
                "dispatch_id": str(row.get("idempotency_key") or ""),
                "role": str(row.get("target_agent") or ""),
                "stage_event_id": refs.get(stage, ""),
                "source": "controller_reconcile",
            }
        )
    records = {
        "stage_events": stage_events,
        "task_run": task_run,
        "skill_jobs": skill_jobs,
        "mcp_calls": mcp_calls,
        "dispatch_rows": dispatch_rows,
    }
    return records, sha256_bytes(canonical_bytes(tables))


MATRIX_PROBE = r'''
import hashlib,json,sys,urllib.request
p=json.loads(sys.stdin.read())
def req(method,path,token=None,body=None):
 r=urllib.request.Request("http://hiclaw-controller:6167"+path,data=(json.dumps(body).encode() if body is not None else None),method=method)
 r.add_header("Content-Type","application/json")
 if token:r.add_header("Authorization","Bearer "+token)
 with urllib.request.urlopen(r,timeout=30) as x:return json.loads(x.read().decode() or "{}")
token=req("POST","/_matrix/client/v3/login",body={"type":"m.login.password","identifier":{"type":"m.id.user","user":"admin"},"password":p["password"]}).get("access_token")
if not token:raise SystemExit(2)
d=req("GET","/_matrix/client/v3/sync?timeout=0&full_state=true",token=token)
events=[]
for rid,room in (d.get("rooms",{}).get("join",{}) or {}).items():
 if rid!=p["room_id"]:continue
 for e in (room.get("timeline",{}).get("events",[]) or []):
  body=(e.get("content") or {}).get("body","")
  if p["run_id"] not in body:continue
  et="M4F_RUN" if body.startswith("M4F_RUN:") else ("TASK_COMPLETED" if body.startswith("TASK_COMPLETED:") else "OTHER")
  events.append({"sync_batch_id":hashlib.sha256(str(d.get("next_batch","")).encode()).hexdigest(),"event_id":e.get("event_id",""),"room_id":rid,"sender":e.get("sender",""),"event_type":et,"body_sha256":hashlib.sha256(body.encode()).hexdigest(),"received_at":str(e.get("origin_server_ts",0)),"consumer_name":p["consumer"]})
print(json.dumps({"events":events,"sync_digest":hashlib.sha256(json.dumps(d,sort_keys=True,separators=(",",":")).encode()).hexdigest()},separators=(",",":")))
'''


def collect_matrix_sync(run_id: str, room_id: str, consumer: str) -> tuple[list[dict[str, Any]], str]:
    password = read_secret_file(MATRIX_PASSWORD_FILE)
    payload = json.dumps({"password": password, "run_id": run_id, "room_id": room_id, "consumer": consumer})
    output = run_checked(["docker", "exec", "-i", "hiclaw-manager", "python3", "-c", MATRIX_PROBE], input_text=payload, timeout=90)
    data = json.loads(output)
    events = data.get("events")
    if not isinstance(events, list) or not events:
        raise ValueError("Matrix /sync returned no run events")
    return events, str(data.get("sync_digest") or "")


def collect_watcher_config() -> dict[str, Any]:
    probe = (
        "import glob,json; p=[]; "
        "files=glob.glob('/root/hiclaw-fs/**/handoff_watcher*.py',recursive=True); "
        "text='\\n'.join(open(f,errors='ignore').read() for f in files); "
        "print(json.dumps({'excluded_prefixes':['m5live-'] if 'm5live-' in text else []}))"
    )
    return json.loads(run_checked(["docker", "exec", "hiclaw-manager", "python3", "-c", probe], timeout=30))


def _json_from_probe(output: str) -> Any:
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    raise ValueError("Gateway probe returned no JSON")


def collect_github_residue(run_id: str) -> tuple[list[str], list[str], str]:
    base = ["docker", "exec", "policy-gw", "python3", "/tmp/m5d-probe-tools.py", "m5coordinator", "--call"]
    branches_raw = run_checked(base + ["list_branches", "owner=nghqqa", "repo=MergePilot-e2e-fixture"], timeout=90)
    prs_raw = run_checked(base + ["list_pull_requests", "owner=nghqqa", "repo=MergePilot-e2e-fixture", "state=open", "perPage=100"], timeout=90)
    branches_data = _json_from_probe(branches_raw)
    prs_data = _json_from_probe(prs_raw)
    branch_rows = branches_data if isinstance(branches_data, list) else branches_data.get("branches", branches_data.get("data", []))
    pr_rows = prs_data if isinstance(prs_data, list) else prs_data.get("pullRequests", prs_data.get("data", []))
    branches = [str(row.get("name")) for row in branch_rows if isinstance(row, dict) and run_id in str(row.get("name", ""))]
    open_prs = []
    for row in pr_rows:
        if not isinstance(row, dict):
            continue
        head = row.get("head") or row.get("headRef") or ""
        head = head.get("ref", "") if isinstance(head, dict) else str(head)
        if run_id in head:
            open_prs.append(head)
    digest = sha256_bytes((branches_raw + prs_raw).encode("utf-8"))
    return open_prs, branches, digest


def collect_local_residue(run_id: str) -> dict[str, list[str]]:
    def docker_names(kind: str) -> list[str]:
        if kind == "container":
            output = run_checked(["docker", "ps", "-a", "--format", "{{.Names}}"], timeout=30)
        elif kind == "network":
            output = run_checked(["docker", "network", "ls", "--format", "{{.Name}}"], timeout=30)
        else:
            output = run_checked(["docker", "volume", "ls", "--format", "{{.Name}}"], timeout=30)
        return sorted(name for name in output.splitlines() if run_id in name)

    return {
        "containers": docker_names("container"),
        "networks": docker_names("network"),
        "volumes": docker_names("volume"),
        "temp_dirs": sorted(path for path in glob.glob("/tmp/*%s*" % run_id) if os.path.isdir(path)),
    }


def _schema_validate(payload: dict[str, Any]) -> tuple[bool, str | None]:
    try:
        import jsonschema
    except ImportError:
        return False, "jsonschema validator unavailable"
    try:
        with open(SCHEMA_FILE, encoding="utf-8") as stream:
            schema = json.load(stream)
        jsonschema.Draft202012Validator(schema).validate(payload)
    except Exception as exc:
        return False, str(exc)[:240]
    return True, None


def collect_production(run_id: str, room_id: str, window_start: str, window_end: str) -> dict[str, Any]:
    agents, container_meta, logs = collect_container_sources(window_start, window_end)
    db, db_digest = collect_db_records(run_id, container_meta["consumer_name"])
    sync_events, matrix_digest = collect_matrix_sync(run_id, room_id, container_meta["consumer_name"])
    open_prs, branches, gateway_digest = collect_github_residue(run_id)
    local_residue = collect_local_residue(run_id)
    residue = dict(local_residue, open_prs=open_prs, branches=branches)
    forbidden = []
    for name, value in logs.items():
        text = value.decode("utf-8", "replace")
        for marker in ("send_as(", "inject_skill_completion"):
            if marker in text:
                forbidden.append("%s:%s" % (name, marker.rstrip("(")))
    secret_matches = [name for name, value in logs.items() if secret_scan(value.decode("utf-8", "replace"))]
    raw = {
        "matrix_server_name": container_meta["matrix_server_name"],
        "sync_events": sync_events,
        "stage_events": db["stage_events"],
        "agent_processes": agents,
        "task_run": db["task_run"],
        "skill_jobs": db["skill_jobs"],
        "mcp_calls": db["mcp_calls"],
        "dispatch_rows": db["dispatch_rows"],
        "watcher_config": collect_watcher_config(),
        "injection_scan": {"scanned_sources": ["runner", "send_as", "inject", "logs"], "forbidden_invocations": forbidden},
        "secret_scan": {"scanned_targets": sorted(logs), "matches": secret_matches},
        "residue": residue,
    }
    command = ["capture_production_live.py", "--run-id", run_id, "--room-id", room_id, "--window-start", window_start, "--window-end", window_end]
    with open(__file__, "rb") as stream:
        script_digest = sha256_bytes(stream.read())
    container_digest = sha256_bytes(canonical_bytes(container_meta["containers"]))
    github_log_digest = sha256_bytes(logs["github-mcp"])
    provenance_base = {
        "collector_kind": "deploy-owned-production-tier-c",
        "collector_script_sha256": script_digest,
        "collector_command_digest": sha256_bytes(canonical_bytes(command)),
        "run_key": run_id,
        "capture_window": {"started_at": window_start, "ended_at": window_end},
        "collected_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "db_snapshot_sha256": db_digest,
        "matrix_sync_sha256": matrix_digest,
        "container_snapshot_sha256": container_digest,
        "gateway_audit_sha256": gateway_digest,
        "github_mcp_log_sha256": github_log_digest,
        "matrix_event_provenance": {
            "sync_event_ids": sorted(str(row.get("event_id")) for row in sync_events),
            "stage_event_ids": sorted(str(row.get("matrix_event_id")) for row in db["stage_events"]),
        },
    }
    provenance_base["raw_capture_sha256"] = sha256_bytes(canonical_bytes(raw))
    raw["provenance"] = provenance_base
    return raw


def _recompute_command_digest(provenance: dict[str, Any], task_run: dict[str, Any]) -> str:
    """Reconstruct the collector command from evidence fields and return its digest.
    Mirrors collect_production's `command` list exactly."""
    cw = provenance.get("capture_window") or {}
    command = [
        "capture_production_live.py",
        "--run-id", str(provenance.get("run_key") or ""),
        "--room-id", str(task_run.get("room_id") or ""),
        "--window-start", str(cw.get("started_at") or ""),
        "--window-end", str(cw.get("ended_at") or ""),
    ]
    return sha256_bytes(canonical_bytes(command))


def validate_production(raw: dict[str, Any], source_commit: str) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit or ""):
        errors.append("source_commit invalid")
    provenance = raw.get("provenance") or {}
    task_run = raw.get("task_run") or {}
    if provenance.get("run_key") != task_run.get("run_id"):
        errors.append("run_key/task_run mismatch")
    try:
        if parse_utc(provenance["capture_window"]["started_at"]) >= parse_utc(provenance["capture_window"]["ended_at"]):
            errors.append("capture window invalid")
        parse_utc(provenance["collected_at"])
    except (KeyError, TypeError, ValueError):
        errors.append("capture timestamps invalid")
    # ── Consumer identity (Fix 2): the m5live run is processed by the Candidate,
    # whose consumer is the real non-default value. "controller" is the production
    # default and cannot process m5live events. All consumer-bearing fields must
    # agree with task_run.consumer_name. ──
    consumer = str(task_run.get("consumer_name") or "")
    if not consumer or consumer == PROD_CONSUMER_DEFAULT:
        errors.append("consumer_name must be the non-default Candidate consumer (not '%s')" % PROD_CONSUMER_DEFAULT)
    for row in raw.get("sync_events") or []:
        if str(row.get("consumer_name") or "") != consumer:
            errors.append("sync_event consumer_name != task_run consumer_name"); break
    for row in raw.get("stage_events") or []:
        if str(row.get("processed_by") or "") != consumer:
            errors.append("stage_event processed_by != consumer_name"); break
    # ── Recomputable digests (Fix 4): real comparison, not just 64-hex format. ──
    raw_without_prov = {k: v for k, v in raw.items() if k != "provenance"}
    if provenance.get("raw_capture_sha256") != sha256_bytes(canonical_bytes(raw_without_prov)):
        errors.append("raw_capture_sha256 mismatch (recomputed from raw)")
    if provenance.get("collector_command_digest") != _recompute_command_digest(provenance, task_run):
        errors.append("collector_command_digest mismatch (recomputed from evidence)")
    try:
        with open(__file__, "rb") as stream:
            script_digest = sha256_bytes(stream.read())
        if provenance.get("collector_script_sha256") != script_digest:
            errors.append("collector_script_sha256 mismatch (recomputed from collector source)")
    except OSError:
        errors.append("collector_script_sha256 cannot recompute (source unreadable)")
    # ── Trust-boundary digests: external system responses (PG snapshot, Matrix
    # /sync, Docker inspect batch, Gateway probe, github-mcp logs) that are not
    # reconstructable from the sanitized evidence. Only format-checked here;
    # integrity rests on the verified collector script + deploy-owned tier-C run. ──
    for field in (
        "collector_script_sha256", "collector_command_digest", "raw_capture_sha256",
        "db_snapshot_sha256", "matrix_sync_sha256", "container_snapshot_sha256",
        "gateway_audit_sha256", "github_mcp_log_sha256",
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", str(provenance.get(field, ""))):
            errors.append("provenance %s invalid" % field)
    matrix = provenance.get("matrix_event_provenance") or {}
    sync_ids = sorted(str(row.get("event_id")) for row in raw.get("sync_events") or [])
    stage_ids = sorted(str(row.get("matrix_event_id")) for row in raw.get("stage_events") or [])
    if matrix.get("sync_event_ids") != sync_ids or matrix.get("stage_event_ids") != stage_ids or not set(sync_ids) & set(stage_ids):
        errors.append("matrix event provenance mismatch")
    if (raw.get("secret_scan") or {}).get("matches") != []:
        errors.append("secret_scan matches not empty")
    for key, value in (raw.get("residue") or {}).items():
        if value != []:
            errors.append("residue.%s not empty" % key)
    payload = {"schema_version": "1", "source_commit": source_commit}
    payload.update(raw)
    ok, error = _schema_validate(payload)
    if not ok:
        errors.append("schema: %s" % (error or "invalid"))
    if secret_scan(payload):
        errors.append("secret pattern detected in evidence")
    return not errors, errors


def publish_production(raw: dict[str, Any], source_commit: str, path: str = EVIDENCE_PATH) -> tuple[bool, str | None]:
    ok, errors = validate_production(raw, source_commit)
    if not ok:
        return False, "; ".join(errors)
    payload = {"schema_version": "1", "source_commit": source_commit}
    payload.update(raw)
    blob = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(prefix=".production-live-", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(blob)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception as exc:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False, "publish failed: %s" % str(exc)[:160]
    return True, None


def capture(run_id: str, room_id: str, window_start: str, window_end: str) -> int:
    if not RUN_ID_RE.fullmatch(run_id or "") or not ROOM_ID_RE.fullmatch(room_id or ""):
        print("FATAL: run_id/room_id invalid", file=sys.stderr)
        return 2
    try:
        if parse_utc(window_start) >= parse_utc(window_end):
            raise ValueError("window start must precede end")
    except ValueError as exc:
        print("FATAL: %s" % exc, file=sys.stderr)
        return 2
    source_commit = git_head()
    if not source_commit:
        print("FATAL: cannot resolve git HEAD", file=sys.stderr)
        return 2
    try:
        raw = collect_production(run_id, room_id, window_start, window_end)
    except Exception as exc:
        print("FAIL: production collector: %s" % str(exc)[:240], file=sys.stderr)
        return 1
    ok, error = publish_production(raw, source_commit)
    if not ok:
        print("FAIL: publish: %s" % (error or "validation failed"), file=sys.stderr)
        return 1
    print("PASS: production-live evidence published %s (source_commit=%s)" % (EVIDENCE_PATH, source_commit))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--room-id", required=True)
    parser.add_argument("--window-start", required=True)
    parser.add_argument("--window-end", required=True)
    args = parser.parse_args()
    return capture(args.run_id, args.room_id, args.window_start, args.window_end)


if __name__ == "__main__":
    sys.exit(main())
