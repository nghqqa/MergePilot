#!/usr/bin/env python3
"""Host worker for M4-F Skill jobs.

Only this process owns the M4-F database connection.  Skill subprocesses get a
fresh allowlisted environment and therefore never inherit the controller DB
credential or the worker role credential.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from jsonschema import Draft202012Validator


RESPONSE_MIME = "application/vnd.mergepilot.skill-response.v1+json"
MAX_ENVELOPE_BYTES = 1024 * 1024
SKILL_MODULES = {
    "diff-parse": "skills.diff_parse.run",
    "risk-classify": "skills.risk_classify.run",
    "sast-scan": "skills.sast_scan.run",
    "test-runner": "skills.test_runner.run",
    "case-retrieval": "skills.case_retrieval.run",
    "pr-lifecycle": "skills.pr_lifecycle.run",
}
SKILL_DIRS = {name: module.split(".")[1] for name, module in SKILL_MODULES.items()}

_BASE_ENV_KEYS = (
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "LANG",
    "LC_ALL",
    "TZ",
    "TMP",
    "TEMP",
)
_CONTROL_SECRET_KEYS = {
    "PGPASSWORD",
    "PGPASSFILE",
    "DATABASE_URL",
    "M4F_DATABASE_DSN",
    "M4F_DB_DSN",
    "SKILL_RUNNER_TOKEN",
    "CONTROLLER_TOKEN",
}


class SkillWorkerError(RuntimeError):
    """A job could not safely produce a completion envelope."""


@dataclass(frozen=True)
class Job:
    job_id: str
    run_id: str
    snapshot_id: str
    trace_id: str
    skill_name: str
    skill_version: str
    request_digest: str
    request_bytes: bytes
    output_schema_digest: str


def _utc_now() -> str:
    import datetime

    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _strict_json(raw: bytes) -> Any:
    if len(raw) > MAX_ENVELOPE_BYTES:
        raise SkillWorkerError("envelope exceeds 1 MiB")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise SkillWorkerError("envelope is not strict UTF-8") from exc

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise SkillWorkerError(f"duplicate JSON key: {key}")
            out[key] = value
        return out

    try:
        return json.loads(
            text,
            object_pairs_hook=no_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(
                SkillWorkerError(f"non-finite JSON number: {value}")
            ),
        )
    except SkillWorkerError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SkillWorkerError("invalid JSON envelope") from exc


def _read_schema(path: pathlib.Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    try:
        schema = json.loads(raw.decode("utf-8", "strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SkillWorkerError(f"invalid schema file: {path}") from exc
    Draft202012Validator.check_schema(schema)
    return schema, hashlib.sha256(raw).hexdigest()


def _validate(validator: Draft202012Validator, value: Any, label: str) -> None:
    errors = sorted(validator.iter_errors(value), key=lambda item: list(item.absolute_path))
    if not errors:
        return
    first = errors[0]
    path = "/".join(str(part) for part in first.absolute_path) or "$"
    raise SkillWorkerError(f"{label} schema mismatch at {path}: {first.message}")


class SkillWorker:
    def __init__(
        self,
        conn: Any,
        *,
        repo_root: str | os.PathLike[str],
        worker_id: str = "skill-worker",
        trusted_skill_env: Mapping[str, Mapping[str, str]] | None = None,
        skill_modules: Mapping[str, str] | None = None,
        observer: Callable[[dict[str, Any]], None] | None = None,
        python_executable: str | None = None,
    ) -> None:
        self.conn = conn
        self.repo_root = pathlib.Path(repo_root).resolve()
        self.worker_id = worker_id
        self.trusted_skill_env = {
            key: dict(value) for key, value in (trusted_skill_env or {}).items()
        }
        self.skill_modules = dict(SKILL_MODULES)
        self.skill_modules.update(skill_modules or {})
        self.observer = observer
        self.python_executable = python_executable or sys.executable

        common_path = self.repo_root / "skills/common/schema/response.envelope.schema.json"
        common_schema, _ = _read_schema(common_path)
        self.common_response_validator = Draft202012Validator(common_schema)

    def _emit(self, event: str, job: Job | None = None, **fields: Any) -> None:
        if self.observer is None:
            return
        payload: dict[str, Any] = {
            "schema": "mergepilot.observation.v1",
            "timestamp": _utc_now(),
            "event": event,
            "worker_id": self.worker_id,
        }
        if job is not None:
            payload.update(
                {
                    "run_id": job.run_id,
                    "trace_id": job.trace_id,
                    "snapshot_id": job.snapshot_id,
                    "job_id": job.job_id,
                    "skill": job.skill_name,
                    "skill_version": job.skill_version,
                }
            )
        payload.update(fields)
        self.observer(payload)

    def _next_candidate(self) -> tuple[str, int] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT j.job_id,
                       LEAST(3570, GREATEST(30,
                         COALESCE((e.content_json->>'timeout_ms')::integer / 1000, 60)))
                FROM public.skill_job_outbox j
                JOIN public.task_runs t ON t.run_id=j.run_id
                JOIN public.envelope_store e ON e.content_digest=j.request_envelope_ref
                WHERE t.skill_data_state='ACTIVE'
                  AND ((j.status='PENDING' AND j.next_retry_at<=now())
                    OR (j.status='LEASED' AND j.lease_expires_at<=now()))
                  AND NOT EXISTS (
                    SELECT 1 FROM public.skill_job_dependencies d
                    JOIN public.skill_job_outbox parent
                      ON parent.job_id=d.depends_on_job_id
                    WHERE d.job_id=j.job_id AND parent.status<>'SUCCEEDED'
                  )
                ORDER BY j.created_at,j.job_id
                LIMIT 1
                """
            )
            row = cur.fetchone()
        self.conn.commit()
        if not row:
            return None
        return str(row[0]), min(3600, int(row[1]) + 30)

    def _claim(self, job_id: str, lease_seconds: int) -> Any | None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT public.claim_skill_job(%s,%s,%s)",
                (job_id, self.worker_id, lease_seconds),
            )
            row = cur.fetchone()
        self.conn.commit()
        return row[0] if row else None

    def _load_job(self, job_id: str) -> Job:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT j.job_id,j.run_id,j.snapshot_id,j.trace_id,j.skill_name,
                       j.skill_version,j.request_envelope_ref,e.content_bytes,
                       r.output_schema_digest
                FROM public.skill_job_outbox j
                JOIN public.envelope_store e
                  ON e.content_digest=j.request_envelope_ref
                JOIN public.skill_version_registry r
                  ON r.skill_name=j.skill_name AND r.skill_version=j.skill_version
                WHERE j.job_id=%s
                """,
                (job_id,),
            )
            row = cur.fetchone()
        self.conn.commit()
        if not row:
            raise SkillWorkerError(f"job disappeared: {job_id}")
        return Job(
            job_id=str(row[0]),
            run_id=str(row[1]),
            snapshot_id=str(row[2]),
            trace_id=str(row[3]),
            skill_name=str(row[4]),
            skill_version=str(row[5]),
            request_digest=str(row[6]),
            request_bytes=bytes(row[7]),
            output_schema_digest=str(row[8]),
        )

    def _schema_validators(
        self, job: Job
    ) -> tuple[Draft202012Validator, Draft202012Validator]:
        directory = SKILL_DIRS.get(job.skill_name)
        if directory is None or job.skill_version != "1.0.0":
            raise SkillWorkerError("unregistered local Skill implementation")
        base = self.repo_root / "skills" / directory / "schema"
        input_schema, _ = _read_schema(base / "input.schema.json")
        output_schema, output_digest = _read_schema(base / "output.schema.json")
        if output_digest != job.output_schema_digest:
            raise SkillWorkerError("local output schema digest differs from registry")
        return Draft202012Validator(input_schema), Draft202012Validator(output_schema)

    def _child_env(self, skill: str) -> dict[str, str]:
        env = {key: os.environ[key] for key in _BASE_ENV_KEYS if key in os.environ}
        # Desktop/embedded runtimes may inject pinned dependencies through the
        # parent interpreter's sys.path instead of installing them into the
        # executable's default site-packages.  Paths are configuration, not
        # credentials; copy only existing import roots, never arbitrary env.
        import_roots = [str(self.repo_root)]
        for entry in sys.path:
            if entry and pathlib.Path(entry).is_dir() and entry not in import_roots:
                import_roots.append(entry)
        env.update(
            {
                "PYTHONPATH": os.pathsep.join(import_roots),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUTF8": "1",
            }
        )
        trusted = self.trusted_skill_env.get(skill, {})
        for key, value in trusted.items():
            if key.upper() in _CONTROL_SECRET_KEYS:
                raise SkillWorkerError(f"control-plane credential forbidden in child env: {key}")
            if not isinstance(key, str) or not isinstance(value, str) or "=" in key or "\x00" in value:
                raise SkillWorkerError("invalid trusted Skill environment")
            env[key] = value
        leaked = _CONTROL_SECRET_KEYS.intersection(key.upper() for key in env)
        if leaked:
            raise SkillWorkerError(f"control-plane child environment leak: {sorted(leaked)}")
        return env

    def _execute(self, job: Job, timeout_seconds: int) -> tuple[bytes, int, str]:
        module = self.skill_modules.get(job.skill_name)
        if module is None:
            raise SkillWorkerError(f"unknown Skill: {job.skill_name}")
        started = time.monotonic()
        try:
            proc = subprocess.run(
                [self.python_executable, "-m", module],
                input=job.request_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.repo_root),
                env=self._child_env(job.skill_name),
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SkillWorkerError("Skill subprocess timeout") from exc
        duration_ms = int((time.monotonic() - started) * 1000)
        stderr_digest = hashlib.sha256(proc.stderr).hexdigest()
        self._emit(
            "skill.process.exited",
            job,
            return_code=proc.returncode,
            duration_ms=duration_ms,
            stdout_bytes=len(proc.stdout),
            stderr_bytes=len(proc.stderr),
            stderr_digest=stderr_digest,
        )
        if not proc.stdout or len(proc.stdout) > MAX_ENVELOPE_BYTES + 1:
            raise SkillWorkerError("Skill produced no bounded response envelope")
        if len(proc.stdout.splitlines()) != 1:
            raise SkillWorkerError("Skill stdout must contain exactly one JSON line")
        return proc.stdout.strip(), proc.returncode, stderr_digest

    def _validate_response(
        self,
        job: Job,
        response_bytes: bytes,
        request: dict[str, Any],
        output_validator: Draft202012Validator,
    ) -> bool:
        response = _strict_json(response_bytes)
        if not isinstance(response, dict):
            raise SkillWorkerError("response envelope is not an object")
        _validate(self.common_response_validator, response, "response envelope")
        expected = {
            "name": job.skill_name,
            "version": job.skill_version,
            "contract_version": "1",
            "request_id": request["request_id"],
            "trace_id": job.trace_id,
        }
        for key, value in expected.items():
            if response.get(key) != value:
                raise SkillWorkerError(f"response binding mismatch: {key}")

        status = response.get("status")
        output = response.get("output")
        output_errors = list(output_validator.iter_errors(output))
        if status in ("OK", "PARTIAL"):
            if output_errors:
                _validate(output_validator, output, "Skill output")
            return True
        if status != "ERROR":
            raise SkillWorkerError(f"unknown response status: {status}")
        if not output_errors:
            return True
        if output != {}:
            _validate(output_validator, output, "structured ERROR output")
        if job.skill_name == "test-runner" and output.get("verdict") is not None:
            raise SkillWorkerError("generic test-runner ERROR must not expose verdict")
        return False

    def _complete(
        self,
        job: Job,
        claim_id: Any,
        response_bytes: bytes,
        output_validated: bool,
    ) -> str:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT public.complete_skill_job(%s,%s,%s,%s,%s)",
                (
                    job.job_id,
                    claim_id,
                    response_bytes,
                    job.output_schema_digest,
                    output_validated,
                ),
            )
            row = cur.fetchone()
        self.conn.commit()
        invocation_id = str(row[0]) if row and row[0] else ""
        if not invocation_id:
            raise SkillWorkerError("complete_skill_job lost its lease CAS")
        return invocation_id

    def _fail(self, job_id: str, claim_id: Any, reason: str) -> None:
        safe = " ".join(str(reason).split())[:300] or "worker failure"
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT public.fail_skill_job(%s,%s,%s)",
                    (job_id, claim_id, safe),
                )
                cur.fetchone()
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def run_once(self) -> bool:
        candidate = self._next_candidate()
        if candidate is None:
            return False
        job_id, lease_seconds = candidate
        claim_id = self._claim(job_id, lease_seconds)
        if claim_id is None:
            return True
        job: Job | None = None
        try:
            job = self._load_job(job_id)
            request = _strict_json(job.request_bytes)
            if not isinstance(request, dict):
                raise SkillWorkerError("request envelope is not an object")
            input_validator, output_validator = self._schema_validators(job)
            _validate(input_validator, request.get("input"), "Skill input")
            if request.get("trace_id") != job.trace_id:
                raise SkillWorkerError("request trace binding mismatch")
            self._emit("skill.claimed", job, claim_id=str(claim_id), lease_seconds=lease_seconds)
            timeout_ms = request.get("timeout_ms", 60000)
            timeout_seconds = min(3570, max(1, int(timeout_ms) // 1000 + 1))
            response_bytes, return_code, _ = self._execute(job, timeout_seconds)
            output_validated = self._validate_response(
                job, response_bytes, request, output_validator
            )
            invocation_id = self._complete(job, claim_id, response_bytes, output_validated)
            self._emit(
                "skill.completed",
                job,
                invocation_id=invocation_id,
                return_code=return_code,
                output_schema_validated=output_validated,
                response_digest=hashlib.sha256(response_bytes).hexdigest(),
            )
            return True
        except Exception as exc:
            self.conn.rollback()
            self._fail(job_id, claim_id, type(exc).__name__ + ": " + str(exc))
            if job is not None:
                self._emit("skill.failed", job, error_type=type(exc).__name__)
            return True

    def drain(self, *, max_jobs: int = 100) -> int:
        handled = 0
        while handled < max_jobs and self.run_once():
            handled += 1
        return handled


def _read_dsn(path: pathlib.Path) -> str:
    mode = path.stat().st_mode & 0o777
    if os.name != "nt" and mode & 0o077:
        raise SystemExit(f"dsn file must not be group/world accessible: {path}")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise SystemExit("dsn file is empty")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn-file", required=True)
    parser.add_argument("--repo-root", default=str(pathlib.Path(__file__).resolve().parents[1]))
    parser.add_argument("--worker-id", default="skill-worker")
    parser.add_argument("--max-jobs", type=int, default=100)
    parser.add_argument("--trusted-env-json")
    parser.add_argument("--forever", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args(argv)

    import psycopg2

    trusted: dict[str, dict[str, str]] = {}
    if args.trusted_env_json:
        trusted_raw = json.loads(pathlib.Path(args.trusted_env_json).read_text(encoding="utf-8"))
        if not isinstance(trusted_raw, dict):
            raise SystemExit("trusted env JSON must be an object")
        trusted = trusted_raw
    conn = psycopg2.connect(_read_dsn(pathlib.Path(args.dsn_file)))
    try:
        worker = SkillWorker(
            conn,
            repo_root=args.repo_root,
            worker_id=args.worker_id,
            trusted_skill_env=trusted,
            observer=lambda event: print(json.dumps(event, ensure_ascii=False), flush=True),
        )
        if args.poll_seconds <= 0 or args.poll_seconds > 60:
            raise SystemExit("poll-seconds must be in (0,60]")
        handled = 0
        if args.forever:
            while True:
                count = worker.drain(max_jobs=max(1, args.max_jobs))
                handled += count
                if count == 0:
                    time.sleep(args.poll_seconds)
        else:
            handled = worker.drain(max_jobs=max(1, args.max_jobs))
            print(json.dumps({"handled": handled, "worker_id": args.worker_id}), flush=True)
            return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
