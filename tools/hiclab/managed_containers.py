#!/usr/bin/env python3
"""Authoritative managed-container manifest for guarded startup.

D2B-3 v1.2.2 upgrade: container names are now derived from the upstream
prefix (``AGENTTEAMS_RESOURCE_PREFIX``, default ``agentteams-`` for v1.2.2).
Set ``HICLAB_LEGACY_PREFIX=hiclaw-`` for v1.1.2 deployments that still use
the old naming.

Derived from the production deployment authoritative scripts:

  tools/audit-db/run_audit_pg.sh        -> audit-pg           (PG data store)
  tools/run-github-mcp-bridge.sh        -> github-mcp         (MCP bridge)
  AgentTeams install (v1.2.2)            -> {prefix}controller (Matrix HS +
                                            MinIO mirror + supervisor)
                                            {prefix}manager     (system Manager)
  tools/run-policy-gateway.sh           -> policy-gw          (Gateway)
  tools/start-controller-container.sh   -> mergepilot-controller (orchestration)

``{prefix}data`` does NOT exist in this deployment -- MinIO is embedded inside
the controller. It is deliberately EXCLUDED.

Startup order (strict, health-gated):

  Phase 1 (foundation) -- start all, wait ALL healthy before Phase 2:
    1. audit-pg           (probe: pg_isready)
    2. github-mcp         (probe: running_uptime, fallback)
    3. {prefix}controller (probe: Matrix /sync + MinIO /health/live)

  Phase 2 (dependents) -- start all, wait ALL healthy:
    4. policy-gw              (probe: HTTP :8083)
    5. mergepilot-controller  (probe: running_uptime, fallback)
    6. {prefix}manager        (probe: running_uptime, fallback)
"""
from __future__ import annotations

import os

PHASE_1 = "foundation"
PHASE_2 = "dependent"

# D2B-3 v1.2.2 upgrade: the upstream resource prefix determines the container
# names. v1.2.2 defaults to "agentteams-"; v1.1.2 used "hiclaw-". The env
# var HICLAB_LEGACY_PREFIX overrides for v1.1.2 deployments.
_LEGACY = os.environ.get("HICLAB_LEGACY_PREFIX", "")
if _LEGACY:
    _PREFIX = _LEGACY if _LEGACY.endswith("-") else _LEGACY + "-"
else:
    _PREFIX = "agentteams-"  # v1.2.2 default

CONTROLLER_NAME = _PREFIX + "controller"
MANAGER_NAME = _PREFIX + "manager"
DATA_NAME = _PREFIX + "data"  # excluded

MANAGED = [
    {"name": "audit-pg", "phase": PHASE_1,
     "health": {"kind": "exec",
                "argv": ["pg_isready", "-U", "mergepilot"]}},
    {"name": "github-mcp", "phase": PHASE_1,
     "health": {"kind": "running_uptime", "min_seconds": 5}},
    {"name": CONTROLLER_NAME, "phase": PHASE_1,
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
    {"name": MANAGER_NAME, "phase": PHASE_2,
     "health": {"kind": "running_uptime", "min_seconds": 5}},
]

# {prefix}data is intentionally absent -- documented for audit clarity.
EXCLUDED = [DATA_NAME]


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
