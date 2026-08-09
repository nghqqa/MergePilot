#!/usr/bin/env python3
"""M5-0D D2B-2 OTel/SLS evidence capture.

The deploy-owned OTel/SLS collector writes a raw JSON capture outside the
repository.  This module imports only the raw ``spans`` and ``sls_schema``
objects, derives ``source_commit`` from git, validates the committed schema,
and atomically publishes the sanitized evidence file.  It never accepts a
credential or a caller-supplied source commit.
"""

from __future__ import annotations

import argparse
import hashlib
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
SCHEMA_FILE = os.path.join(ROOT, "tests", "m5_0d", "schemas", "otel-sls.schema.json")
EVIDENCE_PATH = os.path.join(ROOT, "evidence", "m5", "0d", "otel-sls.json")
REQUIRED_SPANS = {"controller.process_event", "skill.pr_lifecycle", "gateway.call_tool"}
RAW_KEYS = {"spans", "sls_schema"}
SECRET_RE = re.compile(
    r"ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{82,}"
    r"|access_token|sync_token|registration_token|password|private_key"
    r"|client_secret|bearer\s+[A-Za-z0-9._-]+",
    re.IGNORECASE,
)


def git_head() -> str:
    """Read the repository HEAD without changing git configuration."""

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
    """Return True when serialized evidence contains a credential pattern."""

    blob = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return bool(SECRET_RE.search(blob))


def _schema_validate(payload: dict[str, Any], schema_path: str = SCHEMA_FILE) -> tuple[bool, str | None]:
    """Validate with Draft 2020-12; missing jsonschema is fail-closed."""

    try:
        import jsonschema
    except ImportError:
        return False, "jsonschema validator unavailable"
    try:
        with open(schema_path, encoding="utf-8") as stream:
            schema = json.load(stream)
        jsonschema.Draft202012Validator(schema).validate(payload)
    except Exception as exc:  # schema and validation errors are both fatal
        return False, str(exc)[:200]
    return True, None


def _outside_root(path: str) -> bool:
    """Require the raw deploy-owned capture to live outside this repository."""

    try:
        root = os.path.realpath(ROOT)
        candidate = os.path.realpath(path)
        return os.path.commonpath([root, candidate]) != root
    except ValueError:
        # On Windows, a repo on D:\ and a capture on C:\ have no common
        # path; different volumes are necessarily outside the repository.
        return True
    except OSError:
        return False


def load_raw_capture(path: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read a strict raw capture from an external file.

    The raw file is intentionally limited to spans and the deploy-owned SLS
    schema object.  Source commit and any other top-level claims are derived
    or rejected rather than trusted.
    """

    if not _outside_root(path):
        raise ValueError("raw capture must be outside repository")
    with open(path, encoding="utf-8") as stream:
        raw = json.load(stream)
    if not isinstance(raw, dict) or set(raw) != RAW_KEYS:
        raise ValueError("raw capture must contain exactly spans and sls_schema")
    spans = raw.get("spans")
    sls_schema = raw.get("sls_schema")
    if not isinstance(spans, list) or not isinstance(sls_schema, dict):
        raise ValueError("raw spans/sls_schema types invalid")
    if secret_scan(raw):
        raise ValueError("secret pattern detected in raw capture")
    return spans, sls_schema


def validate_otel(spans: list[dict[str, Any]], sls_schema: dict[str, Any]) -> tuple[bool, list[str]]:
    """Derive the OTel/SLS gate from raw span and schema records."""

    errors: list[str] = []
    if not spans:
        errors.append("spans empty")
    names: set[str] = set()
    for index, span in enumerate(spans):
        if not isinstance(span, dict):
            errors.append(f"span[{index}] not object")
            continue
        for field in ("trace_id", "span_id", "name", "run_id"):
            if not isinstance(span.get(field), str) or not span[field]:
                errors.append(f"span[{index}] {field} missing")
        if not isinstance(span.get("attributes"), dict):
            errors.append(f"span[{index}] attributes invalid")
        names.add(span.get("name", ""))
        if span.get("status") != "OK":
            errors.append(f"span[{index}] status={span.get('status')!r}")
    if not REQUIRED_SPANS.issubset(names):
        errors.append("required span names missing")
    if not isinstance(sls_schema, dict):
        errors.append("sls_schema not object")
    else:
        digest = sls_schema.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            errors.append("sls_schema sha256 invalid")
        if not isinstance(sls_schema.get("name"), str) or not sls_schema.get("name"):
            errors.append("sls_schema name missing")
        if not isinstance(sls_schema.get("version"), str) or not sls_schema.get("version"):
            errors.append("sls_schema version missing")
        if not isinstance(sls_schema.get("validated_records"), int) or sls_schema["validated_records"] <= 0:
            errors.append("sls_schema validated_records invalid")
    return not errors, errors


def build_payload(
    spans: list[dict[str, Any]], sls_schema: dict[str, Any], source_commit: str
) -> dict[str, Any]:
    """Build the only evidence shape accepted by the committed schema."""

    return {
        "schema_version": "1",
        "source_commit": source_commit,
        "spans": spans,
        "sls_schema": sls_schema,
    }


def publish_otel(
    spans: list[dict[str, Any]],
    sls_schema: dict[str, Any],
    source_commit: str,
    path: str = EVIDENCE_PATH,
) -> tuple[bool, str | None]:
    """Validate and atomically publish sanitized evidence."""

    payload = build_payload(spans, sls_schema, source_commit)
    ok, error = validate_otel(spans, sls_schema)
    if not ok:
        return False, "; ".join(error or ["raw OTel validation failed"])
    ok, error = _schema_validate(payload)
    if not ok:
        return False, error
    blob = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if secret_scan(blob):
        return False, "secret pattern detected in evidence"
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(prefix=".otel-sls-", suffix=".tmp", dir=directory)
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
        spans, sls_schema = load_raw_capture(input_path)
    except Exception as exc:
        print("FAIL: raw capture: %s" % str(exc)[:200], file=sys.stderr)
        return 1
    ok, error = publish_otel(spans, sls_schema, source_commit, output_path)
    if not ok:
        print("FAIL: publish: %s" % (error or "validation failed"), file=sys.stderr)
        return 1
    print("PASS: OTel/SLS evidence published %s (source_commit=%s)" % (output_path, source_commit))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="repo-external raw OTel/SLS JSON capture")
    parser.add_argument("--output", default=EVIDENCE_PATH, help="evidence output path")
    args = parser.parse_args()
    return capture(args.input, args.output)


if __name__ == "__main__":
    sys.exit(main())
