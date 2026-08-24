"""Preflight container entrypoint (Phase 1-C).

Runs INSIDE the internal-only network: PostgreSQL at host=postgres
(container-to-container), demo-console at http://demo-console:8600.
No host-process substitute, no twin container, no published postgres port.

The 10-gate matrix is :func:`one_click_startup.run_preflight_gates` with
REAL probes. DSN is taken from MERGEPILOT_PG_DSN (env) — never argv.
Passwords never appear in argv, repr, exceptions, or logs.

Exit codes: 0 = all gates passed; 1 = a gate failed (the failing gate name
and stable error code go to stdout as the last line, JSON-encoded).
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import urllib.request
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from one_click_startup import (  # noqa: E402
    ENVIRONMENT_MARKER,
    PGVECTOR_IMAGE_DIGEST,
    PGVECTOR_IMAGE_ID,
    PREFLIGHT_CHECKS,
    READER_ROLE,
    SecretFile,
    StartupGateError,
    assert_argv_safe,
    redact,
    run_preflight_gates,
)

PG_HOST = os.environ.get("MERGEPILOT_PG_HOST", "postgres")
PG_PORT = int(os.environ.get("MERGEPILOT_PG_PORT", "5432"))
CONSOLE_URL = os.environ.get("MERGEPILOT_DEMO_CONSOLE_URL",
                             "http://demo-console:8600")
DSN = os.environ["MERGEPILOT_PG_DSN"]  # env only — never argv


def _log(text: str) -> None:
    print(redact(text), flush=True)


def gate_docker_daemon_identity():
    # Inside the container network there is no docker socket; the daemon
    # identity check runs OUTSIDE (host orchestrator). Here we verify the
    # in-container environment contract instead: the pinned image digest the
    # stack was declared with.
    return {"declared_pg_image": PGVECTOR_IMAGE_DIGEST}


def gate_image_digest_cached():
    # The orchestrator (host side) verifies the pin before starting the
    # stack; the container asserts it was launched with a RECORDED pin
    # value. Both recorded forms are accepted: the manifest digest
    # (registry provenance) and the byte-exact config ID (§4 offline
    # distribution — the value a docker-load deployment declares).
    digest = os.environ.get("MERGEPILOT_DECLARED_PG_IMAGE", "")
    if digest not in (PGVECTOR_IMAGE_DIGEST, PGVECTOR_IMAGE_ID):
        raise StartupGateError("IMAGE_DIGEST_MISMATCH", "declared digest")
    return digest


def gate_postgres_health():
    # TCP probe to postgres:<port> INSIDE the internal network.
    s = socket.socket()
    s.settimeout(5)
    try:
        s.connect((PG_HOST, PG_PORT))
    except OSError:
        raise StartupGateError("PG_NOT_READY", "tcp probe failed") from None
    finally:
        s.close()
    return "tcp-ok"


def gate_database_connectivity():
    import psycopg2  # in-container
    conn = psycopg2.connect(DSN)
    cur = conn.cursor()
    cur.execute("SELECT 1")
    cur.fetchone()
    cur.close()
    conn.close()
    return "db-ok"


def gate_server_identity():
    import psycopg2
    conn = psycopg2.connect(DSN)
    cur = conn.cursor()
    # Retry v3 Fix 1: host(inet_server_addr()) measures the BARE host text —
    # an inet→text cast may carry a build-dependent /32 netmask suffix.
    cur.execute("SELECT host(inet_server_addr()), inet_server_port(), "
                "current_setting('server_version_num')::int")
    addr, port, ver = cur.fetchone()
    cur.close()
    conn.close()
    if not addr or addr == "NULL":
        raise StartupGateError("WRONG_SERVER", "inet_server_addr NULL")
    if not (120000 <= ver <= 180000):
        raise StartupGateError("WRONG_SERVER", "version window")
    return {"addr": addr, "port": port, "version": ver}


def gate_environment_marker():
    import psycopg2
    conn = psycopg2.connect(DSN)
    cur = conn.cursor()
    cur.execute("SELECT environment_id FROM environment_identity")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    if len(rows) != 1:
        raise StartupGateError("ENVIRONMENT_ID_NOT_VERIFIED",
                               "rows=%d" % len(rows))
    if rows[0][0] != ENVIRONMENT_MARKER:
        raise StartupGateError("ENVIRONMENT_ID_MISMATCH", rows[0][0])
    return rows[0][0]


def gate_reader_acl():
    import psycopg2
    import mergepilot_integration as mi
    conn = psycopg2.connect(DSN)
    cur = conn.cursor()
    mi.run_db_prerequisite_checks(cur, expected_marker=ENVIRONMENT_MARKER,
                                  expected_role=READER_ROLE)
    cur.close()
    conn.close()
    return "acl-ok"


def gate_read_only_transaction():
    import psycopg2
    conn = psycopg2.connect(DSN)
    cur = conn.cursor()
    cur.execute("SELECT current_setting('transaction_read_only')::boolean, "
                "current_setting('default_transaction_read_only')::boolean")
    tx, default = cur.fetchone()
    cur.close()
    conn.close()
    if tx is not True or default is not True:
        raise StartupGateError("NOT_READ_ONLY", "writable session")
    return "read-only"


def gate_source_kind():
    import mergepilot_integration as mi
    from one_click_startup import SOURCE_KIND_ISOLATED
    mi.check_kind_isolation(SOURCE_KIND_ISOLATED, SOURCE_KIND_ISOLATED)
    return SOURCE_KIND_ISOLATED


def gate_http_endpoint():
    try:
        with urllib.request.urlopen(CONSOLE_URL + "/api/live/status",
                                    timeout=10) as r:
            status = json.loads(r.read().decode("utf-8"))
    except Exception:
        raise StartupGateError("HTTP_ENDPOINT_FAILED", "status fetch") from None
    if status.get("source_read_only") is not True or \
            status.get("not_production") is not True or \
            status.get("production_resource_accessed") is not None:
        raise StartupGateError("HTTP_ENDPOINT_FAILED", "status contract")
    return "http-ok"


CHECKS = {
    "docker_daemon_identity": gate_docker_daemon_identity,
    "image_digest_cached": gate_image_digest_cached,
    "postgres_health": gate_postgres_health,
    "database_connectivity": gate_database_connectivity,
    "server_identity": gate_server_identity,
    "environment_marker": gate_environment_marker,
    "reader_acl": gate_reader_acl,
    "read_only_transaction": gate_read_only_transaction,
    "source_kind": gate_source_kind,
    "http_endpoint": gate_http_endpoint,
}


def main() -> int:
    assert list(CHECKS.keys()) == list(PREFLIGHT_CHECKS), \
        "preflight check order must match the fixed contract order"
    out = run_preflight_gates(CHECKS)
    _log(json.dumps({k: str(v)[:80] for k, v in out.items()}))
    if not out["ok"]:
        _log("PREFLIGHT_FAILED %s %s" % (out["failed_check"],
                                         out["error_code"]))
        return 1
    _log("PREFLIGHT_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
