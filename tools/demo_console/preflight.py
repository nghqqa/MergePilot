#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Preflight checks for ISOLATED_LIVE mode.

Fail-closed: any failure prevents server startup.
All checks are independent — all_ok never hides individual failures.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

# Add tools/demo_console to sys.path for schema import
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from schema import validate_bundle
from integrity import verify_bundle_integrity


VALID_MODES = frozenset({"replay", "isolated_live"})

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _is_loopback(host: str) -> bool:
    return host.lower() in _LOOPBACK_HOSTS


def _is_http_url(source: str) -> bool:
    return source.lower().startswith(("http://", "https://"))


def _is_file_uri(source: str) -> bool:
    return source.lower().startswith("file:")


def _is_unc_path(source: str) -> bool:
    """Detect UNC paths: \\server\share or //server/share (Windows network)."""
    # Normalize backslashes to forward slashes for a uniform check.
    norm = source.replace("\\", "/")
    return norm.startswith("//")


def run_preflight(mode: str, host: str, source_file: str | None = None) -> dict:
    """Run preflight checks. Returns structured result dict.

    If preflight_passed is False, the server must NOT start.
    """
    failures = []

    # Mode validation
    if mode not in VALID_MODES:
        failures.append({
            "check": "mode_valid",
            "detail": f"mode must be one of {sorted(VALID_MODES)}, got '{mode}'",
        })

    # Loopback check
    loopback_ok = _is_loopback(host)
    if not loopback_ok:
        failures.append({
            "check": "loopback_only",
            "detail": f"host must be loopback (127.0.0.1/localhost/::1), got '{host}'",
        })

    # Source checks (only for isolated_live)
    source_kind = None
    source_read_only = None
    external_network_required = False

    # New provenance / observation fields. production_resource_accessed is
    # None (not False) because the demo console does not actually measure
    # production access — it only refuses to perform it. The *_status fields
    # record that explicitly so consumers cannot mistake "not measured" for
    # "measured and clean".
    source_path_kind = None
    source_is_local_file = False
    source_is_network_path = False
    source_path_resolved = None
    browser_network_observation_status = "NOT_MEASURED"
    observed_external_network_requests = None

    if mode == "isolated_live":
        if not source_file:
            failures.append({
                "check": "source_configured",
                "detail": "isolated_live mode requires --source-file",
            })
        elif _is_http_url(source_file):
            failures.append({
                "check": "source_not_http",
                "detail": "http/https source URLs are forbidden; use file path only",
            })
            external_network_required = True
            source_is_network_path = True
        elif _is_file_uri(source_file):
            failures.append({
                "check": "source_not_file_uri",
                "detail": "file:// URI schemes are forbidden; use a local filesystem path only",
            })
        elif _is_unc_path(source_file):
            failures.append({
                "check": "source_not_unc",
                "detail": "UNC/network paths (\\\\server\\share or //server/share) are forbidden; "
                          "use a local filesystem path only",
            })
            source_is_network_path = True
        elif not os.path.exists(source_file):
            failures.append({
                "check": "source_exists",
                "detail": f"source file does not exist: {source_file}",
            })
        else:
            # Resolve to an absolute path for provenance reporting.
            resolved = os.path.abspath(source_file)
            source_path_resolved = resolved

            # Reject directories, pipes, sockets, device files, etc. Only a
            # regular file is an acceptable snapshot source.
            try:
                st = os.stat(resolved)
            except OSError as e:
                failures.append({
                    "check": "source_stat",
                    "detail": f"cannot stat source file: {e}",
                })
                st = None

            if st is not None and not stat.S_ISREG(st.st_mode):
                failures.append({
                    "check": "source_is_regular_file",
                    "detail": "source path is not a regular file "
                              "(directory/pipe/socket/device files are forbidden)",
                })
            else:
                source_kind = "FILE_FIXTURE"
                source_read_only = True  # file read is inherently read-only
                source_path_kind = "LOCAL_FILE"
                source_is_local_file = True
                source_is_network_path = False

                # Try to parse as JSON
                try:
                    with open(resolved, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except json.JSONDecodeError as e:
                    failures.append({
                        "check": "source_json_valid",
                        "detail": f"source file is not valid JSON: {e}",
                    })
                except Exception as e:
                    failures.append({
                        "check": "source_readable",
                        "detail": f"cannot read source file: {e}",
                    })
                else:
                    # Validate schema
                    schema_errors = validate_bundle(data)
                    if schema_errors:
                        failures.append({
                            "check": "source_schema_valid",
                            "detail": f"schema validation failed: {schema_errors[:3]}",
                        })

                    # Integrity verification (independent of schema, which now
                    # also checks this — kept explicit here so preflight reports
                    # a dedicated source_integrity failure category).
                    integrity_errors = verify_bundle_integrity(data)
                    if integrity_errors:
                        failures.append({
                            "check": "source_integrity",
                            "detail": f"bundle integrity check failed: {integrity_errors[:3]}",
                        })
    elif mode == "replay":
        source_kind = "PREGENERATED_BUNDLE"
        source_read_only = True

    passed = len(failures) == 0

    return {
        "mode": mode.upper(),
        "preflight_passed": passed,
        "source_kind": source_kind,
        "source_read_only": source_read_only,
        "loopback_only": loopback_ok,
        # production_resource_accessed is intentionally None, not False: the
        # console does not measure production access, it only refuses it. The
        # companion *_status field makes "not measured" explicit.
        "production_resource_accessed": None,
        "production_resource_access_status": "NOT_MEASURED",
        "external_network_required": external_network_required,
        "github_writes_enabled": False,
        "agent_control_enabled": False,
        "runtime_consumes_rag_context": False,
        # Source path provenance
        "source_path_kind": source_path_kind,
        "source_is_local_file": source_is_local_file,
        "source_is_network_path": source_is_network_path,
        "source_path_resolved": source_path_resolved,
        # Network observation provenance (not measured by the read-only console)
        "browser_network_observation_status": browser_network_observation_status,
        "observed_external_network_requests": observed_external_network_requests,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "failures": failures,
    }
