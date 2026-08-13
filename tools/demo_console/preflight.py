#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Preflight checks for ISOLATED_LIVE mode.

Fail-closed: any failure prevents server startup.
All checks are independent — all_ok never hides individual failures.
"""
from __future__ import annotations

import json
import os
import re
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
from postgres_source import CANONICAL_VIEWER_ROLE


VALID_MODES = frozenset({"replay", "isolated_live"})

# IPv4-loopback only. The P1 demo server is a single-machine HTTP server
# bound to an IPv4 loopback address; IPv6 loopback (::1) is NOT implemented.
# Any host not in this set is rejected (including ::1, ::, 0.0.0.0, LAN IPs).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})


# Environment variable that carries the PostgreSQL DSN for source_kind=postgres.
# The DSN is a secret and must NEVER be passed via argv or written to config
# files; it is read from this env var only.
_PG_DSN_ENV = "MERGEPILOT_PG_DSN"

# run_id allowlist (mirrors PostgresSnapshotSource so preflight can validate the
# shape before the source is constructed).
_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _is_loopback(host: str) -> bool:
    return host.lower() in _LOOPBACK_HOSTS


# --- Windows drive type constants (mirror Win32 GetDriveTypeW codes) -------
DRIVE_UNKNOWN = 0
DRIVE_NO_ROOT_DIR = 1
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6

_DRIVE_TYPE_NAMES = {
    DRIVE_UNKNOWN: "DRIVE_UNKNOWN",
    DRIVE_NO_ROOT_DIR: "DRIVE_NO_ROOT_DIR",
    DRIVE_REMOVABLE: "DRIVE_REMOVABLE",
    DRIVE_FIXED: "DRIVE_FIXED",
    DRIVE_REMOTE: "DRIVE_REMOTE",
    DRIVE_CDROM: "DRIVE_CDROM",
    DRIVE_RAMDISK: "DRIVE_RAMDISK",
}


def _is_http_url(source: str) -> bool:
    return source.lower().startswith(("http://", "https://"))


def _is_file_uri(source: str) -> bool:
    return source.lower().startswith("file:")


def _is_unc_path(source: str) -> bool:
    r"""Detect UNC paths: \\server\share or //server/share (Windows network)."""
    # Normalize backslashes to forward slashes for a uniform check.
    norm = source.replace("\\", "/")
    return norm.startswith("//")


def _is_windows_drive_letter_path(source: str) -> bool:
    """Detect a Windows drive-letter path of the form ``X:\\path``."""
    if len(source) < 2:
        return False
    if source[1] not in (":",):  # drive letters are followed by ':'
        return False
    # First char must be an ASCII letter (A-Z, a-z).
    return source[0].isalpha()


def _win32_get_drive_type(root_pathname: str) -> int:
    """Call kernel32.GetDriveTypeW and return its integer code.

    Returns DRIVE_UNKNOWN (0) if the Win32 API is unavailable (e.g. on a
    non-Windows host or a stripped-down runtime). Callers treat any
    DRIVE_UNKNOWN / DRIVE_NO_ROOT_DIR / exception as NOT_MEASURED.
    """
    # Import ctypes lazily so this module imports cleanly on POSIX (where
    # ctypes.windll does not exist).
    import ctypes

    # `windll` only exists on Windows. Guarding keeps the import portable.
    if not hasattr(ctypes, "windll"):
        return DRIVE_UNKNOWN
    try:
        GetDriveTypeW = ctypes.windll.kernel32.GetDriveTypeW
        GetDriveTypeW.restype = ctypes.c_uint
        GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
        return int(GetDriveTypeW(root_pathname))
    except (OSError, AttributeError):
        return DRIVE_UNKNOWN


def classify_source_locality(path: str) -> dict:
    """Classify a snapshot source path as local vs. network-backed.

    Returns a dict with:

    - ``status``: one of
        ``VERIFIED_LOCAL``          — a local fixed volume (DRIVE_FIXED on
                                      Windows, or a regular file on POSIX)
        ``NETWORK_PATH_REJECTED``   — UNC, mapped network drive, etc.
        ``UNSUPPORTED_DRIVE_TYPE``  — removable / CD-ROM / RAM disk
        ``POSIX_LOCAL_CANDIDATE``   — POSIX path that exists; not verified
                                      by a Win32-style drive-type check
        ``NOT_MEASURED``            — Win32 could not classify (DRIVE_UNKNOWN,
                                      DRIVE_NO_ROOT_DIR, or API failure); the
                                      file does not exist; or POSIX fail-closed
    - ``drive_type``: symbolic name of the Win32 drive type, or ``None``
    - ``drive_type_code``: the integer Win32 code, or ``None``
    - ``failure``: a human-readable failure detail, or ``None`` when the
      source is VERIFIED_LOCAL

    Fail-closed invariants maintained by callers:
    - ``NOT_MEASURED`` NEVER coexists with ``preflight_passed=true``.
    - ``source_is_local_file=true`` ONLY when ``status == VERIFIED_LOCAL``.
    """
    result = {
        "status": "NOT_MEASURED",
        "drive_type": None,
        "drive_type_code": None,
        "failure": None,
    }

    # UNC paths are always network-backed regardless of platform.
    if _is_unc_path(path):
        result["status"] = "NETWORK_PATH_REJECTED"
        result["failure"] = (
            "UNC/network paths (\\\\server\\share or //server/share) are "
            "forbidden; use a local filesystem path only"
        )
        return result

    is_windows = sys.platform == "win32" or os.name == "nt"

    if is_windows and _is_windows_drive_letter_path(path):
        # Query the Win32 drive type for the drive root, e.g. "C:\\".
        drive_root = path[:3]  # e.g. "C:\\"
        code = _win32_get_drive_type(drive_root)
        name = _DRIVE_TYPE_NAMES.get(code, f"UNKNOWN_CODE_{code}")
        result["drive_type"] = name
        result["drive_type_code"] = code

        if code == DRIVE_FIXED:
            result["status"] = "VERIFIED_LOCAL"
            result["failure"] = None
        elif code == DRIVE_REMOTE:
            result["status"] = "NETWORK_PATH_REJECTED"
            result["failure"] = "NETWORK_DRIVE_REJECTED"
        elif code in (DRIVE_REMOVABLE, DRIVE_CDROM, DRIVE_RAMDISK):
            result["status"] = "UNSUPPORTED_DRIVE_TYPE"
            result["failure"] = (
                f"unsupported drive type {name} for source {path!r}; "
                "only fixed local volumes (DRIVE_FIXED) are accepted"
            )
        else:
            # DRIVE_UNKNOWN, DRIVE_NO_ROOT_DIR, or any unmapped code: we
            # could not classify, so fail-closed via NOT_MEASURED.
            result["status"] = "NOT_MEASURED"
            result["failure"] = "SOURCE_LOCALITY_NOT_MEASURED"
        return result

    # POSIX (non-Windows) path. We can only confirm the file exists; we have
    # no portable Win32-style drive-type check, so we mark it as a candidate
    # and fail-closed via NOT_MEASURED (POSIX_LOCALITY_NOT_VERIFIED).
    if os.path.isfile(path):
        result["status"] = "POSIX_LOCAL_CANDIDATE"
        result["failure"] = "POSIX_LOCALITY_NOT_VERIFIED"
    else:
        # File does not exist (or is not a regular file); the caller's own
        # source_exists / source_is_regular_file checks will surface this.
        result["status"] = "NOT_MEASURED"
        result["failure"] = "SOURCE_LOCALITY_NOT_MEASURED"
    return result


def run_preflight(mode: str, host: str, source_file: str | None = None,
                  source_kind: str = "file", pg_config: dict | None = None) -> dict:
    """Run preflight checks. Returns structured result dict.

    If preflight_passed is False, the server must NOT start.

    ``source_kind`` selects the snapshot source type:

    - ``"file"`` (default): the classic local-file fixture path. Runs the full
      file-locality classification (Win32 drive type / POSIX fail-closed),
      JSON parse, schema, and integrity checks against ``source_file``.
    - ``"postgres"``: a read-only PostgreSQL source. The file-locality checks
      do NOT apply (the source is a database, not a file); instead preflight
      verifies that the DSN env var is present, ``run_id`` is well-formed, and
      the expected database/role/environment-id/server-addresses/server-port/
      application-name are configured (all mandatory and fail-closed). The
      actual DB identity/catalog/role gate is enforced by
      ``PostgresSnapshotSource`` at read time (the "startup probe"); preflight
      (the "config preflight") only checks that the required configuration is
      present and well-shaped WITHOUT a DB connection.
    """
    failures = []

    # Mode validation
    if mode not in VALID_MODES:
        failures.append({
            "check": "mode_valid",
            "detail": f"mode must be one of {sorted(VALID_MODES)}, got '{mode}'",
        })

    # source_kind validation. Only "file" and "postgres" are supported.
    if source_kind not in ("file", "postgres"):
        failures.append({
            "check": "source_kind_valid",
            "detail": (
                f"source_kind must be 'file' or 'postgres', got {source_kind!r}"
            ),
        })

    # Loopback check (IPv4-loopback only; IPv6 ::1 is NOT implemented).
    loopback_ok = _is_loopback(host)
    if not loopback_ok:
        host_lower = host.lower() if isinstance(host, str) else host
        if host_lower == "::1":
            detail = (
                "P1 server is IPv4-loopback only; IPv6 ::1 not implemented"
            )
        else:
            detail = (
                f"host must be IPv4 loopback (127.0.0.1/localhost), got '{host}'; "
                "the P1 server is IPv4-loopback only and never binds off-machine"
            )
        failures.append({
            "check": "loopback_only",
            "detail": detail,
        })

    # Source checks (only for isolated_live)
    # NOTE: ``source_kind`` here is the REPORTED kind written to the result
    # dict (e.g. FILE_FIXTURE / POSTGRES_ISOLATED). It is distinct from the
    # ``source_kind`` PARAMETER (file vs postgres) which selects the path.
    source_kind_reported = None
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

    # source_locality_status records how confidently we classified the
    # snapshot source as a local (non-network) path:
    #   VERIFIED_LOCAL          — a local fixed volume (DRIVE_FIXED)
    #   NETWORK_PATH_REJECTED   — UNC / mapped network drive / URI / URL
    #   UNSUPPORTED_DRIVE_TYPE  — removable / CD-ROM / RAM disk
    #   POSIX_LOCAL_CANDIDATE   — POSIX path exists but not Win32-verified
    #   NOT_MEASURED            — Win32 could not classify, or POSIX fail-closed
    source_locality_status = None
    source_drive_type = None
    source_drive_type_code = None
    source_locality_measurement_status = "NOT_MEASURED"

    if mode == "isolated_live":
        if source_kind == "postgres":
            # ── PostgreSQL source preflight ────────────────────────────────
            # The file-locality checks do NOT apply: the source is a database,
            # not a file. Preflight only verifies the required configuration
            # is present and well-formed. The actual database identity gate
            # (read-only, expected db/role/server, environment marker) is
            # enforced by PostgresSnapshotSource at read time.
            source_kind_reported = "POSTGRES_ISOLATED"
            source_read_only = True
            source_path_kind = "POSTGRESQL"
            # A DB source is not a local file and not a network PATH (it is a
            # service reached via a DSN). Locality classification is N/A.
            source_is_local_file = False
            source_is_network_path = False
            source_locality_status = "NOT_APPLICABLE"
            source_locality_measurement_status = "NOT_APPLICABLE"

            if not pg_config:
                failures.append({
                    "check": "pg_source_configured",
                    "detail": (
                        "isolated_live + source_kind=postgres requires a "
                        "pg_config dict (run_id, expected_database, "
                        "expected_role, expected_environment_id, "
                        "expected_server_addresses, expected_server_port, "
                        "expected_application_name)"
                    ),
                })
            else:
                run_id = pg_config.get("run_id")
                dsn_env = os.environ.get(_PG_DSN_ENV)
                expected_database = pg_config.get("expected_database")
                expected_role = pg_config.get("expected_role")
                expected_environment_id = pg_config.get(
                    "expected_environment_id"
                )
                expected_server_addresses = pg_config.get(
                    "expected_server_addresses"
                )
                expected_server_port = pg_config.get("expected_server_port")
                expected_application_name = pg_config.get(
                    "expected_application_name"
                )

                # run_id must match the strict allowlist (mirror the source).
                if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
                    failures.append({
                        "check": "pg_run_id_valid",
                        "detail": (
                            "pg_config.run_id must match ^[a-zA-Z0-9_-]+$ "
                            f"(got {run_id!r})"
                        ),
                    })
                # DSN must come from the env var, NEVER from argv/config (it is
                # a secret). Absence is a hard preflight failure.
                if not dsn_env:
                    failures.append({
                        "check": "pg_dsn_env_present",
                        "detail": (
                            f"DSN env var {_PG_DSN_ENV} is not set; the "
                            "PostgreSQL DSN must be supplied via this env var "
                            "(never via argv)"
                        ),
                    })
                if not expected_database:
                    failures.append({
                        "check": "pg_expected_database",
                        "detail": "pg_config.expected_database is required",
                    })
                if not expected_role:
                    failures.append({
                        "check": "pg_expected_role",
                        "detail": (
                            "pg_config.expected_role is required (the "
                            "canonical viewer role is mergepilot_reader; the "
                            "role is mandatory and verified against "
                            "current_user at read time)"
                        ),
                    })
                else:
                    # Import canonical role from the single contract source
                    from postgres_source import CANONICAL_VIEWER_ROLE
                    if expected_role != CANONICAL_VIEWER_ROLE:
                        failures.append({
                            "check": "pg_expected_role",
                            "detail": (
                                "pg_config.expected_role must be exactly "
                                "'%s'; got '%s'" % (CANONICAL_VIEWER_ROLE, expected_role)
                            ),
                        })
                # expected_environment_id MUST be a non-empty string. The
                # environment marker is mandatory; the source never guesses the
                # environment identity from hostname.
                if not isinstance(expected_environment_id, str) or not expected_environment_id.strip():
                    failures.append({
                        "check": "pg_expected_environment_id",
                        "detail": (
                            "pg_config.expected_environment_id must be a "
                            "non-empty string (environment marker is "
                            "mandatory; never guessed)"
                        ),
                    })
                # expected_server_addresses MUST be a non-empty list.
                if not isinstance(expected_server_addresses, list) or not expected_server_addresses:
                    failures.append({
                        "check": "pg_expected_server_addresses",
                        "detail": (
                            "pg_config.expected_server_addresses must be a "
                            "non-empty list (e.g. ['127.0.0.1'])"
                        ),
                    })
                # expected_server_port MUST be a non-zero int (bool rejected).
                if (not isinstance(expected_server_port, int)
                        or isinstance(expected_server_port, bool)
                        or expected_server_port == 0):
                    failures.append({
                        "check": "pg_expected_server_port",
                        "detail": (
                            "pg_config.expected_server_port must be a "
                            "non-zero int"
                        ),
                    })
                # expected_application_name MUST be a non-empty string.
                if not isinstance(expected_application_name, str) or not expected_application_name.strip():
                    failures.append({
                        "check": "pg_expected_application_name",
                        "detail": (
                            "pg_config.expected_application_name must be a "
                            "non-empty string"
                        ),
                    })
        else:
            # ── File source preflight (classic path) ───────────────────────
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
                source_locality_status = "NETWORK_PATH_REJECTED"
            elif _is_file_uri(source_file):
                failures.append({
                    "check": "source_not_file_uri",
                    "detail": "file:// URI schemes are forbidden; use a local filesystem path only",
                })
                source_locality_status = "NETWORK_PATH_REJECTED"
            elif _is_unc_path(source_file):
                failures.append({
                    "check": "source_not_unc",
                    "detail": "UNC/network paths (\\\\server\\share or //server/share) are forbidden; "
                              "use a local filesystem path only",
                })
                source_is_network_path = True
                source_locality_status = "NETWORK_PATH_REJECTED"
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
                    source_kind_reported = "FILE_FIXTURE"
                    source_read_only = True  # file read is inherently read-only
                    source_path_kind = "LOCAL_FILE"
                    source_is_network_path = False

                    # Classify source locality via Win32 drive-type check (or
                    # POSIX fallback). The classifier is fail-closed: only
                    # VERIFIED_LOCAL yields source_is_local_file=true; any other
                    # status (including NOT_MEASURED) is recorded as a preflight
                    # failure so the server never starts on an unverified source.
                    locality = classify_source_locality(resolved)
                    source_locality_status = locality["status"]
                    source_drive_type = locality["drive_type"]
                    source_drive_type_code = locality["drive_type_code"]

                    if locality["status"] == "VERIFIED_LOCAL":
                        source_is_local_file = True
                        source_locality_measurement_status = "MEASURED"
                    else:
                        # Fail-closed: any non-VERIFIED_LOCAL status blocks
                        # startup. NOT_MEASURED must NEVER coexist with
                        # preflight_passed=true.
                        source_is_local_file = False
                        source_locality_measurement_status = "NOT_MEASURED"
                        failures.append({
                            "check": "source_locality",
                            "detail": (
                                f"source locality not verified: status="
                                f"{locality['status']} "
                                f"drive_type={locality['drive_type']} "
                                f"code={locality['drive_type_code']}; "
                                f"{locality['failure'] or 'only DRIVE_FIXED local volumes are accepted'}"
                            ),
                        })

                    # Try to parse as JSON (only attempted for regular files).
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
                        # Validate schema with mode isolation: a REPLAY bundle
                        # must not be served in an ISOLATED_LIVE context. This
                        # block only runs under mode == "isolated_live", so the
                        # expected mode is fixed to ISOLATED_LIVE.
                        schema_errors = validate_bundle(
                            data, expected_mode="ISOLATED_LIVE")
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
        source_kind_reported = "PREGENERATED_BUNDLE"
        source_read_only = True

    passed = len(failures) == 0

    return {
        "mode": mode.upper(),
        "preflight_passed": passed,
        "source_kind": source_kind_reported,
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
        # Source locality classification. VERIFIED_LOCAL for a DRIVE_FIXED
        # local volume; NETWORK_PATH_REJECTED for refused UNC/URI/URL/mapped
        # network drives; UNSUPPORTED_DRIVE_TYPE for removable/CD/RAM;
        # POSIX_LOCAL_CANDIDATE for POSIX paths (fail-closed to NOT_MEASURED);
        # NOT_MEASURED when Win32 could not classify. NOT_MEASURED never
        # coexists with preflight_passed=true.
        "source_locality_status": source_locality_status,
        "source_drive_type": source_drive_type,
        "source_drive_type_code": source_drive_type_code,
        "source_locality_measurement_status": source_locality_measurement_status,
        # Network observation provenance (not measured by the read-only console)
        "browser_network_observation_status": browser_network_observation_status,
        "observed_external_network_requests": observed_external_network_requests,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "failures": failures,
    }
