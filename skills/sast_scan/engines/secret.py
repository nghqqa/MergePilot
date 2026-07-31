"""Secret-detection engine: runs the versioned secret regex rules.

Matched text (``evidence_text``) is returned to core for a redacted digest only;
it is never placed in message/output fields.
"""
from __future__ import annotations


def _line_col(content, offset):
    line = content.count("\n", 0, offset) + 1
    last_nl = content.rfind("\n", 0, offset)
    col = offset - (last_nl + 1) + 1
    return line, col


def scan(path, content, secret_rules):
    """secret_rules: list of dicts with _compiled (pre-compiled regex) + metadata."""
    out = []
    for rule in secret_rules:
        rx = rule["_compiled"]
        for m in rx.finditer(content):
            line, col = _line_col(content, m.start())
            out.append({
                "engine": "secret",
                "rule_id": rule["rule_id"],
                "category": "secret",
                "severity": rule["severity"],
                "risk_level": rule["risk_level"],
                "file": path,
                "line": line,
                "column": col,
                "message": rule["label"],
                "remediation": rule["remediation"],
                "evidence_text": m.group(0),
            })
    return out
