"""Dependency-vulnerability engine: offline local advisory matcher.

This is a small, curated offline advisory set -- NOT a complete or real-time
DepVulnCheck. ``core`` records db provenance (db_version/source/covered/
valid_until) and a ``stale`` flag in the output.
"""
from __future__ import annotations

import re

_REQ_RE = re.compile(r"^([A-Za-z0-9_.-]+)\s*(==)\s*([0-9][0-9A-Za-z.*+!\-]*)")


def scan(path, content, advisories):
    by_pkg = {}
    for a in advisories:
        by_pkg.setdefault(a["package"].lower(), []).append(a)
    out = []
    for lineno, line in enumerate(content.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _REQ_RE.match(stripped)
        if not m:
            continue
        pkg = m.group(1).lower()
        version = m.group(3)
        for a in by_pkg.get(pkg, []):
            if a["version"] == version:
                rule_id = "DEP_" + a["id"].upper().replace("-", "_").replace(".", "_")
                rule_id = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in rule_id)
                out.append({
                    "engine": "dep_vuln",
                    "rule_id": rule_id,
                    "category": "dependency",
                    "severity": a["severity"],
                    "risk_level": "L2",
                    "file": path,
                    "line": lineno,
                    "column": 1,
                    "message": "vulnerable dependency: %s==%s (%s)" % (a["package"], version, a["id"]),
                    "remediation": "Upgrade %s to %s. %s" % (a["package"], a["fixed_version"], a["advisory"]),
                    "evidence_text": "%s==%s" % (a["package"], version),
                })
    return out
