#!/usr/bin/env python3
"""Authoritative managed-container manifest for guarded startup.

Derived from the production deployment authoritative scripts:

  tools/audit-db/run_audit_pg.sh        -> audit-pg           (PG data store)
  tools/run-github-mcp-bridge.sh        -> github-mcp         (MCP bridge)
  HiClaw install (higress.ai/install.sh) -> hiclaw-controller (Matrix HS +
                                            MinIO mirror + supervisor)
                                            hiclaw-manager      (system Manager)
  tools/run-policy-gateway.sh           -> policy-gw          (Gateway)
  tools/start-controller-container.sh   -> mergepilot-controller (orchestration)

``hiclaw-data`` does NOT exist in this deployment -- MinIO is embedded inside
hiclaw-controller. It is deliberately EXCLUDED (a prior candidate version
listed it incorrectly; this module is the single source of truth).

Startup order (strict, health-gated):

  Phase 1 (foundation) -- start all, wait ALL healthy before Phase 2:
    1. audit-pg           (probe: pg_isready)
    2. github-mcp         (probe: running_uptime, fallback)
    3. hiclaw-controller  (probe: Matrix /sync + MinIO /health/live)

  Phase 2 (dependents) -- start all, wait ALL healthy:
    4. policy-gw              (probe: HTTP :8083)
    5. mergepilot-controller  (probe: running_uptime, fallback)
    6. hiclaw-manager         (probe: running_uptime, fallback)

Any Phase-1 dependency unhealthy -> fail-closed -> stop every container the
supervisor started this round. No WARN-and-continue.

Health probe kinds:
  exec            : docker exec <name> <argv>; rc==0 => healthy (strong,
                    service-specific)
  running_uptime  : docker inspect .State.Running == true AND
                    (now - StartedAt) >= min_seconds (conservative fallback
                    for containers without a known service endpoint)
"""
from __future__ import annotations

PHASE_1 = "foundation"
PHASE_2 = "dependent"

MANAGED = [
    {"name": "audit-pg", "phase": PHASE_1,
     "health": {"kind": "exec",
                "argv": ["pg_isready", "-U", "mergepilot"]}},
    {"name": "github-mcp", "phase": PHASE_1,
     "health": {"kind": "running_uptime", "min_seconds": 5}},
    {"name": "hiclaw-controller", "phase": PHASE_1,
     "health": {"kind": "exec",
                "argv": ["sh", "-c",
                         "curl -sf -o /dev/null "
                         "http://localhost:6167/_matrix/client/versions "
                         "&& curl -sf -o /dev/null "
                         "http://localhost:9000/minio/health/live"]}},
    {"name": "policy-gw", "phase": PHASE_2,
     "health": {"kind": "docker_health", "port": 8083}},
    {"name": "mergepilot-controller", "phase": PHASE_2,
     "health": {"kind": "running_uptime", "min_seconds": 5}},
    {"name": "hiclaw-manager", "phase": PHASE_2,
     "health": {"kind": "running_uptime", "min_seconds": 5}},
]

# hiclaw-data is intentionally absent -- documented for audit clarity.
EXCLUDED = ["hiclaw-data"]


def names():
    """Ordered list of managed container names."""
    return [m["name"] for m in MANAGED]


def phase_members(phase):
    """All manifest members in a phase, in manifest order."""
    return [m for m in MANAGED if m["phase"] == phase]


def check_unique():
    """Raise if any duplicate names. Returns True if all unique."""
    n = names()
    if len(n) != len(set(n)):
        dups = sorted({x for x in n if n.count(x) > 1})
        raise ValueError("duplicate managed container names: %s" % dups)
    return True


def find(name):
    for m in MANAGED:
        if m["name"] == name:
            return m
    return None


def validate_no_excluded():
    """Ensure no excluded name (e.g. hiclaw-data) is in MANAGED."""
    present = set(names()) & set(EXCLUDED)
    if present:
        raise ValueError("excluded containers leaked into MANAGED: %s"
                         % sorted(present))
    return True
