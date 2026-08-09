#!/usr/bin/env python3
"""M5-0D D2B-3 production tier-C raw evidence capture.

An operator-authorized production collector writes a raw JSON record outside
the repository.  This module deliberately does not invent Matrix, Agent,
Gateway, or GitHub observations: it imports the raw records, derives
``source_commit`` from git, validates the committed strict schema, rejects
secrets and non-empty cleanup residue, and atomically publishes evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from typing import Any


ROOT = os.environ.get(
    "M5_0D_ROOT",
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)
SCHEMA_FILE = os.path.join(ROOT, "tests", "m5_0d", "schemas", "production-live.schema.json")
EVIDENCE_PATH = os.path.join(ROOT, "evidence", "m5", "0d", "production-live.json")
REQUIRED_KEYS = {
    "sync_events",
    "stage_events",
    "agent_processes",
    "task_run",
    "skill_jobs",
    "mcp_calls",
    "dispatch_rows",
    "watcher_config",
    "injection_scan",
    "secret_scan",
    "residue",
}
ALLOWED_KEYS = REQUIRED_KEYS | {"matrix_server_name"}
SECRET_RE = re.compile(
    r"ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{82,}"
    r"|access_token|sync_token|registration_token|password|private_key"
    r"|client_secret|bearer\s+[A-Za-z0-9._-]+",
    re.IGNORECASE,
)


def git_head() -> str:
    try:
        result = subprocess.run(
            ["git", "-c", "safe.directory=" + ROOT, "-C", ROOT, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    value = result.stdout.strip()
    return value if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", value) else ""


def secret_scan(value: Any) -> bool:
    blob = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return bool(SECRET_RE.search(blob))


def _schema_validate(payload: dict[str, Any], schema_path: str = SCHEMA_FILE) -> tuple[bool, str | None]:
    """Draft 2020-12 validation is mandatory; unavailable validator fails closed."""

    try:
        import jsonschema
    except ImportError:
        return False, "jsonschema validator unavailable"
    try:
        with open(schema_path, encoding="utf-8") as stream:
            schema = json.load(stream)
        jsonschema.Draft202012Validator(schema).validate(payload)
    except Exception as exc:
        return False, str(exc)[:200]
    return True, None


def _outside_root(path: str) -> bool:
    try:
        root = os.path.realpath(ROOT)
        candidate = os.path.realpath(path)
        return os.path.commonpath([root, candidate]) != root
    except ValueError:
        return True
    except OSError:
        return False


def load_raw_capture(path: str) -> dict[str, Any]:
    """Load only raw production records; source_commit is never accepted."""

    if not _outside_root(path):
        raise ValueError("raw capture must be outside repository")
    with open(path, encoding="utf-8") as stream:
        raw = json.load(stream)
    if not isinstance(raw, dict):
        raise ValueError("raw capture must be an object")
    if "source_commit" in raw or "schema_version" in raw:
        raise ValueError("raw capture must not contain caller-supplied source metadata")
    if set(raw) - ALLOWED_KEYS or not REQUIRED_KEYS.issubset(raw):
        missing = sorted(REQUIRED_KEYS - set(raw))
        extra = sorted(set(raw) - ALLOWED_KEYS)
        raise ValueError("raw keys invalid missing=%s extra=%s" % (missing[:3], extra[:3]))
    if secret_scan(raw):
        raise ValueError("secret pattern detected in raw capture")
    return raw


def build_payload(raw: dict[str, Any], source_commit: str) -> dict[str, Any]:
    payload = {"schema_version": "1", "source_commit": source_commit}
    payload.update(raw)
    return payload


def validate_production(raw: dict[str, Any], source_commit: str) -> tuple[bool, list[str]]:
    """Validate raw production shape and clean-up/security invariants."""

    errors: list[str] = []
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit or ""):
        errors.append("source_commit invalid")
    payload = build_payload(raw, source_commit)
    ok, error = _schema_validate(payload)
    if not ok:
        errors.append("schema: %s" % (error or "invalid"))
    scan = raw.get("secret_scan", {})
    if not isinstance(scan, dict) or scan.get("matches") != []:
        errors.append("secret_scan matches not empty")
    residue = raw.get("residue", {})
    if not isinstance(residue, dict):
        errors.append("residue not object")
    else:
        for key in ("containers", "networks", "volumes", "temp_dirs", "open_prs", "branches"):
            if residue.get(key) != []:
                errors.append("residue.%s not empty" % key)
    if secret_scan(payload):
        errors.append("secret pattern detected in evidence")
    return not errors, errors


def publish_production(raw: dict[str, Any], source_commit: str, path: str = EVIDENCE_PATH) -> tuple[bool, str | None]:
    ok, errors = validate_production(raw, source_commit)
    if not ok:
        return False, "; ".join(errors)
    blob = json.dumps(build_payload(raw, source_commit), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
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


def capture(input_path: str, output_path: str = EVIDENCE_PATH) -> int:
    source_commit = git_head()
    if not source_commit:
        print("FATAL: cannot resolve git HEAD", file=sys.stderr)
        return 2
    try:
        raw = load_raw_capture(input_path)
    except Exception as exc:
        print("FAIL: raw capture: %s" % str(exc)[:200], file=sys.stderr)
        return 1
    ok, error = publish_production(raw, source_commit, output_path)
    if not ok:
        print("FAIL: publish: %s" % (error or "validation failed"), file=sys.stderr)
        return 1
    print("PASS: production-live evidence published %s (source_commit=%s)" % (output_path, source_commit))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="repo-external raw production capture")
    parser.add_argument("--output", default=EVIDENCE_PATH, help="evidence output path")
    args = parser.parse_args()
    return capture(args.input, args.output)


if __name__ == "__main__":
    sys.exit(main())
