"""Python AST engine: dangerous-call and SQL-injection detection (stdlib ``ast``).

Targets are dotted names (e.g. ``pickle.load``). SQLi: ``execute/executemany``
whose first argument is an f-string (``ast.JoinedStr``) or string concatenation
(``ast.BinOp`` with ``+``).
"""
from __future__ import annotations

import ast


def _dotted(node):
    """Return the dotted callable name for a Call func node, or ''."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return (base + "." + node.attr) if base else node.attr
    return ""


def scan(path, content, ast_rules):
    """Return (findings, had_syntax_error). A syntax error must NOT be reported
    as a clean complete scan; the caller records a degradation for it."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return [], True

    dangerous = [r for r in ast_rules if r["kind"] == "dangerous_call"]
    sqli = [r for r in ast_rules if r["kind"] == "sqli_execute"]
    out = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func)
        if not name:
            continue

        # dangerous_call: match by exact dotted name OR trailing attribute
        for rule in dangerous:
            if name in rule["targets"] or any(name.endswith("." + t) for t in rule["targets"]):
                out.append({
                    "engine": "ast_python",
                    "rule_id": rule["rule_id"],
                    "category": "dangerous_call",
                    "severity": rule["severity"],
                    "risk_level": rule["risk_level"],
                    "file": path,
                    "line": getattr(node, "lineno", 0),
                    "column": getattr(node, "col_offset", 0) + 1,
                    "message": "dangerous call: " + name,
                    "remediation": rule["remediation"],
                    "evidence_text": name,
                })
                break

        # sqli_execute: *.execute/executemany with f-string or concat query
        for rule in sqli:
            attr = name.rsplit(".", 1)[-1]
            if attr in rule["targets"] and node.args:
                first = node.args[0]
                if isinstance(first, (ast.JoinedStr, ast.BinOp)):
                    out.append({
                        "engine": "ast_python",
                        "rule_id": rule["rule_id"],
                        "category": "injection",
                        "severity": rule["severity"],
                        "risk_level": rule["risk_level"],
                        "file": path,
                        "line": getattr(node, "lineno", 0),
                        "column": getattr(node, "col_offset", 0) + 1,
                        "message": "SQL injection: " + attr + "() with dynamic query",
                        "remediation": rule["remediation"],
                        "evidence_text": attr,
                    })
                    break
    return out, False
