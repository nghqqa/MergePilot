#!/usr/bin/env python3
"""M5-0D D2B-2 deploy-owned OTel/SLS collector and publisher.

The collector queries a deploy-owned HTTP capture sink directly.  Endpoint
and optional authorization are read from fixed tmpfs files; neither is
accepted in argv, environment variables, logs, or evidence.  The published
evidence binds the response bytes, endpoint identity, command, capture
window, and trace run IDs to the current git HEAD.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from typing import Any


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCHEMA_FILE = os.path.join(ROOT, "tests", "m5_0d", "schemas", "otel-sls.schema.json")
EVIDENCE_PATH = os.path.join(ROOT, "evidence", "m5", "0d", "otel-sls.json")
ENDPOINT_FILE = "/dev/shm/m5d/otel-endpoint"
AUTH_FILE = "/dev/shm/m5d/otel-auth"
REQUIRED_SPANS = {"controller.process_event", "skill.pr_lifecycle", "gateway.call_tool"}
SECRET_RE = re.compile(
    r"ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{82,}"
    r"|access_token|sync_token|registration_token|password|private_key"
    r"|client_secret|bearer\s+[A-Za-z0-9._-]+",
    re.IGNORECASE,
)
RUN_ID_RE = re.compile(r"m5live-[A-Za-z0-9.-]+$")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


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


def parse_utc(value: str) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("timestamp must be UTC Z")
    parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.tzinfo is None:
        raise ValueError("timestamp timezone missing")
    return parsed


def read_tmpfs_file(path: str, required: bool = True) -> str:
    """Read a non-symlink, owner-only tmpfs file with a bounded size."""

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if required:
            raise ValueError("required tmpfs file missing: %s" % path)
        return ""
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("tmpfs path must be a regular non-symlink file")
    if os.name == "posix" and (info.st_mode & 0o077):
        raise ValueError("tmpfs file mode must be 0600")
    if info.st_size <= 0 or info.st_size > 8192:
        raise ValueError("tmpfs file size invalid")
    with open(path, encoding="utf-8") as stream:
        value = stream.read().strip()
    if not value or "\x00" in value:
        raise ValueError("tmpfs file content invalid")
    return value


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


def fetch_raw_capture(
    run_id: str,
    window_start: str,
    window_end: str,
    endpoint_file: str = ENDPOINT_FILE,
    auth_file: str = AUTH_FILE,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Query the deploy-owned sink and return raw records plus provenance."""

    endpoint = read_tmpfs_file(endpoint_file)
    auth = read_tmpfs_file(auth_file, required=False)
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("OTel endpoint URL invalid")
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.extend((("run_id", run_id), ("start", window_start), ("end", window_end)))
    request_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), ""))
    request = urllib.request.Request(request_url, headers={"Accept": "application/json"})
    if auth:
        request.add_header("Authorization", "Bearer " + auth)
    with urllib.request.urlopen(request, timeout=60) as response:
        if getattr(response, "status", 200) != 200:
            raise ValueError("OTel sink returned non-200")
        raw_bytes = response.read(5 * 1024 * 1024 + 1)
    if len(raw_bytes) > 5 * 1024 * 1024:
        raise ValueError("OTel response too large")
    if secret_scan(raw_bytes.decode("utf-8", "replace")):
        raise ValueError("secret pattern detected in OTel response")
    raw = json.loads(raw_bytes.decode("utf-8"))
    if not isinstance(raw, dict) or set(raw) != {"spans", "sls_schema"}:
        raise ValueError("OTel response must contain exactly spans and sls_schema")
    observed = sorted({s.get("run_id") for s in raw.get("spans", []) if isinstance(s, dict) and s.get("run_id")})
    command = ["capture_otel_sls.py", "--run-id", run_id, "--window-start", window_start, "--window-end", window_end]
    with open(__file__, "rb") as stream:
        script_digest = sha256_bytes(stream.read())
    provenance = {
        "collector_kind": "deploy-owned-otel-sls",
        "collector_script_sha256": script_digest,
        "collector_command_digest": sha256_bytes(canonical_bytes(command)),
        "collector_endpoint_digest": sha256_bytes(endpoint.encode("utf-8")),
        "capture_window": {"started_at": window_start, "ended_at": window_end},
        "captured_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "raw_capture_sha256": sha256_bytes(raw_bytes),
        "trace_run_binding": {"expected_run_id": run_id, "observed_run_ids": observed},
    }
    return raw, provenance


def validate_otel(
    spans: list[dict[str, Any]], sls_schema: dict[str, Any], provenance: dict[str, Any]
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    names: set[str] = set()
    expected_run = ((provenance or {}).get("trace_run_binding") or {}).get("expected_run_id")
    observed_runs: set[str] = set()
    if not RUN_ID_RE.fullmatch(expected_run or ""):
        errors.append("expected run_id invalid")
    if not spans:
        errors.append("spans empty")
    for index, span in enumerate(spans or []):
        if not isinstance(span, dict):
            errors.append("span[%d] not object" % index)
            continue
        names.add(span.get("name", ""))
        run_id = span.get("run_id")
        if isinstance(run_id, str) and run_id:
            observed_runs.add(run_id)
        for field in ("trace_id", "span_id", "name", "run_id"):
            if not isinstance(span.get(field), str) or not span[field]:
                errors.append("span[%d] %s missing" % (index, field))
        if span.get("status") != "OK":
            errors.append("span[%d] status not OK" % index)
        if not isinstance(span.get("attributes"), dict):
            errors.append("span[%d] attributes invalid" % index)
    if not REQUIRED_SPANS.issubset(names):
        errors.append("required span names missing")
    claimed_runs = ((provenance or {}).get("trace_run_binding") or {}).get("observed_run_ids")
    if sorted(observed_runs) != claimed_runs or observed_runs != {expected_run}:
        errors.append("trace run binding mismatch")
    try:
        start = parse_utc(provenance["capture_window"]["started_at"])
        end = parse_utc(provenance["capture_window"]["ended_at"])
        captured = parse_utc(provenance["captured_at"])
        if not start < end or captured < start:
            errors.append("capture window invalid")
    except (KeyError, TypeError, ValueError):
        errors.append("capture timestamps invalid")
    # ── Recomputable digests (Fix 4): real comparison, not just 64-hex format. ──
    cw = (provenance or {}).get("capture_window") or {}
    otel_command = ["capture_otel_sls.py", "--run-id", str(expected_run or ""),
                    "--window-start", str(cw.get("started_at") or ""),
                    "--window-end", str(cw.get("ended_at") or "")]
    if (provenance or {}).get("collector_command_digest") != sha256_bytes(canonical_bytes(otel_command)):
        errors.append("collector_command_digest mismatch (recomputed from evidence)")
    try:
        with open(__file__, "rb") as stream:
            otel_script_digest = sha256_bytes(stream.read())
        if (provenance or {}).get("collector_script_sha256") != otel_script_digest:
            errors.append("collector_script_sha256 mismatch (recomputed from collector source)")
    except OSError:
        errors.append("collector_script_sha256 cannot recompute (source unreadable)")
    # ── Trust-boundary digests: raw_capture_sha256 (raw OTel sink HTTP bytes)
    # and collector_endpoint_digest (endpoint read from tmpfs) are not
    # reconstructable from the sanitized spans/sls_schema. Format-checked only;
    # integrity rests on the verified collector script + deploy-owned sink. ──
    for field in (
        "collector_script_sha256",
        "collector_command_digest",
        "collector_endpoint_digest",
        "raw_capture_sha256",
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", str((provenance or {}).get(field, ""))):
            errors.append("provenance %s invalid" % field)
    digest = (sls_schema or {}).get("sha256")
    if not re.fullmatch(r"[0-9a-f]{64}", digest or ""):
        errors.append("sls_schema sha256 invalid")
    if not isinstance((sls_schema or {}).get("validated_records"), int) or sls_schema["validated_records"] <= 0:
        errors.append("sls_schema validated_records invalid")
    return not errors, errors


def build_payload(raw: dict[str, Any], provenance: dict[str, Any], source_commit: str) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "source_commit": source_commit,
        "provenance": provenance,
        "spans": raw["spans"],
        "sls_schema": raw["sls_schema"],
    }


def publish_otel(
    raw: dict[str, Any], provenance: dict[str, Any], source_commit: str, path: str = EVIDENCE_PATH
) -> tuple[bool, str | None]:
    ok, errors = validate_otel(raw.get("spans"), raw.get("sls_schema"), provenance)
    if not ok:
        return False, "; ".join(errors)
    payload = build_payload(raw, provenance, source_commit)
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


def capture(run_id: str, window_start: str, window_end: str, output_path: str = EVIDENCE_PATH) -> int:
    if not RUN_ID_RE.fullmatch(run_id or ""):
        print("FATAL: run_id must be m5live-*", file=sys.stderr)
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
        raw, provenance = fetch_raw_capture(run_id, window_start, window_end)
    except Exception as exc:
        print("FAIL: OTel collector: %s" % str(exc)[:200], file=sys.stderr)
        return 1
    ok, error = publish_otel(raw, provenance, source_commit, output_path)
    if not ok:
        print("FAIL: publish: %s" % (error or "validation failed"), file=sys.stderr)
        return 1
    print("PASS: OTel/SLS evidence published %s (source_commit=%s)" % (output_path, source_commit))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--window-start", required=True)
    parser.add_argument("--window-end", required=True)
    args = parser.parse_args()
    return capture(args.run_id, args.window_start, args.window_end)


if __name__ == "__main__":
    sys.exit(main())
