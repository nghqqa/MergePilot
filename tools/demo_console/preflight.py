#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Preflight checks for ISOLATED_LIVE mode.

Fail-closed: any failure prevents server startup.
All checks are independent — all_ok never hides individual failures.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Add tools/demo_console to sys.path for schema import
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from schema import validate_bundle


VALID_MODES = frozenset({"replay", "isolated_live"})

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _is_loopback(host: str) -> bool:
    return host.lower() in _LOOPBACK_HOSTS


def _is_http_url(source: str) -> bool:
    return source.lower().startswith(("http://", "https://"))


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

    if mode == "isolated_live":
        if not source_file:
            failures.append({
                "check": "source_configured",
                "detail": "isolated_live mode requires --source-file",
            })
        else:
            if _is_http_url(source_file):
                failures.append({
                    "check": "source_not_http",
                    "detail": "http/https source URLs are forbidden; use file path only",
                })
                external_network_required = True
            elif not os.path.exists(source_file):
                failures.append({
                    "check": "source_exists",
                    "detail": f"source file does not exist: {source_file}",
                })
            else:
                source_kind = "FILE_FIXTURE"
                source_read_only = True  # file read is inherently read-only
                # Try to parse as JSON
                try:
                    with open(source_file, "r", encoding="utf-8") as f:
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
        "production_resource_accessed": False,
        "external_network_required": external_network_required,
        "github_writes_enabled": False,
        "agent_control_enabled": False,
        "runtime_consumes_rag_context": False,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "failures": failures,
    }
