#!/usr/bin/env python3
"""AgentTeams/HiClaw Matrix event -> M4-F immutable six-Skill run.

The event carries business inputs only.  Repository revision, diff and changed
files are read through Policy Gateway; the event can never self-assert SHAs.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import uuid
from typing import Any, Callable, Mapping

from m4f_controller import StagedRun, stage_six_skill_run


EVENT_MARKER = "M4F_RUN:"
MAX_EVENT_BYTES = 256 * 1024
_RUN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TRACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class M4FIngressError(RuntimeError):
    """Permanent, fail-closed ingress error."""


class M4FRevisionDrift(M4FIngressError):
    """A bound run observed a different authoritative PR revision."""


def _strict_object(raw: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise M4FIngressError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(
                M4FIngressError(f"non-finite JSON number: {token}")
            ),
        )
    except M4FIngressError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise M4FIngressError("M4F_RUN payload is not strict JSON") from exc
    if not isinstance(value, dict):
        raise M4FIngressError("M4F_RUN payload must be an object")
    return value


def parse_event(body: str) -> dict[str, Any]:
    if not isinstance(body, str) or len(body.encode("utf-8")) > MAX_EVENT_BYTES:
        raise M4FIngressError("M4F_RUN event exceeds 256 KiB")
    marker = body.find(EVENT_MARKER)
    if marker < 0:
        raise M4FIngressError("M4F_RUN marker missing")
    raw = body[marker + len(EVENT_MARKER) :].strip()
    payload = _strict_object(raw)
    return validate_event(payload)


def validate_event(payload: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "contract_version",
        "run_id",
        "trace_id",
        "repo",
        "pr_number",
        "test_runner",
        "pr_lifecycle",
    }
    allowed = required | {"case_query", "risk_floor"}
    missing = sorted(required - set(payload))
    extra = sorted(set(payload) - allowed)
    if missing or extra:
        raise M4FIngressError(f"M4F_RUN fields missing={missing} extra={extra}")
    if payload.get("contract_version") != "1":
        raise M4FIngressError("M4F_RUN contract_version must be 1")
    run_id = payload.get("run_id")
    trace_id = payload.get("trace_id")
    repo = payload.get("repo")
    pr_number = payload.get("pr_number")
    if not isinstance(run_id, str) or not _RUN_RE.fullmatch(run_id):
        raise M4FIngressError("invalid run_id")
    if not isinstance(trace_id, str) or not _TRACE_RE.fullmatch(trace_id):
        raise M4FIngressError("invalid trace_id")
    if not isinstance(repo, str) or not _REPO_RE.fullmatch(repo):
        raise M4FIngressError("invalid repo")
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
        raise M4FIngressError("invalid pr_number")
    test_runner = payload.get("test_runner")
    pr_lifecycle = payload.get("pr_lifecycle")
    if not isinstance(test_runner, dict) or not isinstance(pr_lifecycle, dict):
        raise M4FIngressError("test_runner/pr_lifecycle must be objects")
    if not test_runner or not pr_lifecycle:
        raise M4FIngressError("test_runner/pr_lifecycle must not be empty")
    query = payload.get("case_query")
    if query is not None and (
        not isinstance(query, str) or not query.strip() or len(query) > 500
    ):
        raise M4FIngressError("invalid case_query")
    risk_floor = payload.get("risk_floor", "L0")
    if risk_floor not in {"L0", "L1", "L2"}:
        raise M4FIngressError("invalid risk_floor")
    return json.loads(json.dumps(dict(payload), ensure_ascii=False, allow_nan=False))


def _safe_changed_files(files: list[dict[str, Any]], diff_text: str) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if len(files) > 100:
        raise M4FIngressError("changed-file list exceeds 100")
    contexts: list[dict[str, Any]] = []
    sast_files: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in files:
        path = item.get("filename") or item.get("path")
        if not isinstance(path, str) or not path or len(path) > 1024:
            raise M4FIngressError("changed file has invalid path")
        posix = pathlib.PurePosixPath(path.replace("\\", "/"))
        if posix.is_absolute() or ".." in posix.parts or path in seen:
            raise M4FIngressError("changed file path is unsafe or duplicated")
        seen.add(path)
        status = str(item.get("status") or "modified").lower()
        change_type = {
            "added": "A",
            "removed": "D",
            "deleted": "D",
            "renamed": "R",
            "modified": "M",
            "changed": "M",
        }.get(status, "M")
        additions = item.get("additions", 0)
        deletions = item.get("deletions", 0)
        if isinstance(additions, bool) or not isinstance(additions, int) or additions < 0:
            additions = 0
        if isinstance(deletions, bool) or not isinstance(deletions, int) or deletions < 0:
            deletions = 0
        lowered = path.lower()
        categories = ["test" if "/test" in f"/{lowered}" or lowered.startswith("test") else "source"]
        if any(token in lowered for token in ("auth", "secret", "security", "sql", "crypto")):
            categories.append("security_sensitive")
        patch = item.get("patch")
        contexts.append(
            {
                "path": path,
                "old_path": item.get("previous_filename") if status == "renamed" else None,
                "change_type": change_type,
                "additions": additions,
                "deletions": deletions,
                "binary": not isinstance(patch, str),
                "mode_changed": False,
                "categories": sorted(set(categories)),
                "hunks": [],
            }
        )
        if isinstance(patch, str):
            sast_files.append({"path": path, "content": patch})
    if not contexts:
        contexts.append(
            {
                "path": "pull-request.diff",
                "old_path": None,
                "change_type": "M",
                "additions": 0,
                "deletions": 0,
                "binary": False,
                "mode_changed": False,
                "categories": ["source"],
                "hunks": [],
            }
        )
    if not sast_files:
        sast_files = [{"path": "pull-request.diff", "content": diff_text}]
    return contexts, sast_files


def build_skill_inputs(
    payload: Mapping[str, Any],
    pr: Mapping[str, Any],
    diff_text: str,
    files: list[dict[str, Any]],
) -> dict[str, Any]:
    repo = str(payload["repo"])
    base_sha = str(pr.get("base_sha") or "")
    head_sha = str(pr.get("head_sha") or "")
    if not _SHA_RE.fullmatch(base_sha) or not _SHA_RE.fullmatch(head_sha):
        raise M4FIngressError("Policy Gateway returned invalid revision SHA")
    if pr.get("pr_number") != payload["pr_number"]:
        raise M4FIngressError("Policy Gateway PR number mismatch")
    if pr.get("head_repo_full_name") != repo:
        raise M4FIngressError("fork PR head repository is outside current M4-F profile")

    contexts, sast_files = _safe_changed_files(files, diff_text)
    additions = sum(item["additions"] for item in contexts)
    deletions = sum(item["deletions"] for item in contexts)
    modules = sorted({item["path"].split("/", 1)[0] for item in contexts})
    categories = sorted({cat for item in contexts for cat in item["categories"]})
    change_context = {
        "schema_version": "1",
        "source": {
            "repo": repo,
            "pr_number": payload["pr_number"],
            "base_sha": base_sha,
            "head_sha": head_sha,
        },
        "input_sha256": hashlib.sha256(diff_text.encode("utf-8")).hexdigest(),
        "complete": True,
        "files": contexts,
        "modules_touched": modules,
        "change_categories": categories,
        "stats": {
            "files_changed": len(contexts),
            "additions": additions,
            "deletions": deletions,
            "hunks": 0,
            "binary_files": sum(1 for item in contexts if item["binary"]),
        },
    }
    query = payload.get("case_query") or (
        f"Review {repo} PR #{payload['pr_number']} changes in "
        + ", ".join(item["path"] for item in contexts[:8])
    )
    return {
        "diff-parse": {
            "repo": repo,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "diff_format": "unified",
            "pr_number": payload["pr_number"],
            "diff_text": diff_text,
        },
        "risk-classify": {
            "change_context": change_context,
            "risk_floor": payload.get("risk_floor", "L0"),
        },
        "sast-scan": {"mode": "inline", "files": sast_files},
        "test-runner": dict(payload["test_runner"]),
        "case-retrieval": {
            "query": str(query),
            "top_k": 5,
            "expected_embedding_version": "1.0.0",
        },
        "pr-lifecycle": dict(payload["pr_lifecycle"]),
    }


def _canon_str(value: str | None) -> str:
    if value is None:
        return "-1:"
    return f"{len(value.encode('utf-8'))}:{value}"


def _evidence_digest(row: tuple[Any, ...]) -> str:
    material = "".join(_canon_str(None if value is None else str(value)) for value in row)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _load_provenance(conn: Any, run_id: str, repo: str, base_sha: str) -> tuple[str, str]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT request_id,correlation_id,tool,target_repo,run_id,git_sha,result_status
               FROM public.mcp_calls
               WHERE phase='RESULT' AND decision='ALLOW' AND result_status='OK'
                 AND run_id=%s AND target_repo=%s AND git_sha=%s
               ORDER BY ts DESC,request_id DESC LIMIT 1""",
            (run_id, repo, base_sha),
        )
        row = cur.fetchone()
    conn.commit()
    if not row:
        raise M4FIngressError("Policy Gateway revision provenance row missing")
    return str(row[0]), _evidence_digest(tuple(row))


def _existing_revision(conn: Any, run_id: str) -> dict[str, str] | None:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT binding_id,repo,pr_number,base_sha,head_sha,
                      source_call_id,source_evidence_digest
               FROM public.revision_bindings WHERE run_id=%s""",
            (run_id,),
        )
        row = cur.fetchone()
    conn.commit()
    if not row:
        return None
    keys = (
        "binding_id",
        "repo",
        "pr_number",
        "base_sha",
        "head_sha",
        "source_call_id",
        "source_evidence_digest",
    )
    return {key: row[index] for index, key in enumerate(keys)}


def _child_run_id(run_id: str, repo: str, pr_number: int, head_sha: str) -> str:
    material = f"{run_id}\x00{repo}\x00{pr_number}\x00{head_sha}".encode("utf-8")
    return f"{run_id[:72]}-rev-{hashlib.sha256(material).hexdigest()[:16]}"


def _ensure_task_binding(
    conn: Any,
    payload: Mapping[str, Any],
    pr: Mapping[str, Any],
) -> str:
    run_id = str(payload["run_id"])
    repo = str(payload["repo"])
    pr_number = int(payload["pr_number"])
    head_sha = str(pr["head_sha"])
    with conn.cursor() as cur:
        cur.execute(
            "SELECT repo,trace_id,skill_data_state FROM public.task_runs WHERE run_id=%s FOR UPDATE",
            (run_id,),
        )
        task = cur.fetchone()
        if not task:
            raise M4FIngressError("M4F_RUN references unknown task run")
        if task[0] not in (None, repo):
            raise M4FIngressError("M4F_RUN repository differs from task run")
        if task[1] not in (None, payload["trace_id"]):
            raise M4FIngressError("M4F_RUN trace_id differs from task run")
        if task[2] != "ACTIVE":
            raise M4FIngressError("M4F_RUN task data is not ACTIVE")
        cur.execute(
            "UPDATE public.task_runs SET repo=%s,trace_id=%s,updated_at=now() WHERE run_id=%s",
            (repo, payload["trace_id"], run_id),
        )
        cur.execute(
            "SELECT binding_id,repo,pr_number,head_sha FROM public.run_pr_bindings WHERE run_id=%s FOR UPDATE",
            (run_id,),
        )
        binding = cur.fetchone()
        if binding:
            cur.execute(
                """UPDATE public.run_pr_bindings
                   SET repo=%s,pr_number=%s,fix_branch=%s,base_branch=%s,
                       head_sha=%s,recorded_at=now()
                   WHERE run_id=%s""",
                (
                    repo,
                    pr_number,
                    pr["head_ref"],
                    pr["base"],
                    head_sha,
                    run_id,
                ),
            )
        else:
            cur.execute(
                """INSERT INTO public.run_pr_bindings(
                       binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha)
                   VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                (
                    str(uuid.uuid4()),
                    run_id,
                    repo,
                    pr_number,
                    pr["head_ref"],
                    pr["base"],
                    head_sha,
                ),
            )
    conn.commit()
    return run_id


def _create_revision_cut(
    conn: Any,
    payload: Mapping[str, Any],
    pr: Mapping[str, Any],
) -> dict[str, Any]:
    prior = str(payload["run_id"])
    child = _child_run_id(prior, str(payload["repo"]), int(payload["pr_number"]), str(pr["head_sha"]))
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"m4f-cut:{prior}",))
        cur.execute(
            """INSERT INTO public.task_runs(
                   run_id,room_id,repo,pr_number,branch,status,current_stage,
                   attempt,approval_required,trace_id)
               SELECT %s,room_id,%s,%s,branch,'RUNNING','m4f_revision_cut',
                      attempt,approval_required,%s
               FROM public.task_runs WHERE run_id=%s
               ON CONFLICT(run_id) DO NOTHING""",
            (child, payload["repo"], payload["pr_number"], payload["trace_id"], prior),
        )
        cur.execute(
            """INSERT INTO public.run_pr_bindings(
                   binding_id,run_id,repo,pr_number,fix_branch,base_branch,head_sha)
               VALUES(%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(run_id) DO NOTHING""",
            (
                str(uuid.uuid4()),
                child,
                payload["repo"],
                payload["pr_number"],
                pr["head_ref"],
                pr["base"],
                pr["head_sha"],
            ),
        )
        cur.execute(
            """UPDATE public.task_runs
               SET status='HOLD',current_stage='revision_superseded',
                   last_error=%s,updated_at=now()
               WHERE run_id=%s""",
            (f"external head drift cut {child}", prior),
        )
    conn.commit()
    result = dict(payload)
    result["run_id"] = child
    return result


def stage_agentteams_event(
    controller_conn: Any,
    snapshot_conn: Any,
    payload: Mapping[str, Any],
    *,
    gateway: Any,
    snapshot_worker_id: str = "agentteams-snapshot-worker",
    observer: Callable[[dict[str, Any]], None] | None = None,
) -> StagedRun:
    event = validate_event(payload)
    owner, repo_name = event["repo"].split("/", 1)
    status, pr = gateway.gateway_read_pr(
        owner,
        repo_name,
        event["pr_number"],
        run_id=event["run_id"],
    )
    if status != "OK" or not isinstance(pr, dict):
        raise RuntimeError("Policy Gateway revision read is retryable")
    if pr.get("state", "").lower() != "open" or pr.get("merged") is not False:
        raise M4FIngressError("M4F_RUN requires an open, unmerged PR")

    existing = _existing_revision(controller_conn, event["run_id"])
    if existing and (
        existing["repo"] != event["repo"]
        or int(existing["pr_number"]) != event["pr_number"]
        or existing["base_sha"] != pr.get("base_sha")
        or existing["head_sha"] != pr.get("head_sha")
    ):
        event = _create_revision_cut(controller_conn, event, pr)
        status, pr = gateway.gateway_read_pr(
            owner,
            repo_name,
            event["pr_number"],
            run_id=event["run_id"],
        )
        if status != "OK" or not isinstance(pr, dict):
            raise RuntimeError("Policy Gateway revision-cut read is retryable")
        existing = _existing_revision(controller_conn, event["run_id"])

    _ensure_task_binding(controller_conn, event, pr)
    diff_text = gateway.gateway_get_pr_diff(owner, repo_name, event["pr_number"])
    files = gateway.gateway_get_pr_files(owner, repo_name, event["pr_number"])
    skill_inputs = build_skill_inputs(event, pr, diff_text, files)

    if existing:
        source_call_id = str(existing["source_call_id"])
        source_evidence_digest = str(existing["source_evidence_digest"])
    else:
        source_call_id, source_evidence_digest = _load_provenance(
            controller_conn,
            event["run_id"],
            event["repo"],
            str(pr["base_sha"]),
        )

    return stage_six_skill_run(
        controller_conn,
        snapshot_conn,
        run_id=event["run_id"],
        trace_id=event["trace_id"],
        repo=event["repo"],
        pr_number=event["pr_number"],
        base_sha=str(pr["base_sha"]),
        head_sha=str(pr["head_sha"]),
        source_call_id=source_call_id,
        source_evidence_digest=source_evidence_digest,
        skill_inputs=skill_inputs,
        snapshot_worker_id=snapshot_worker_id,
        observer=observer,
    )
