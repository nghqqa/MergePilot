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
    taint = [r for r in ast_rules if r["kind"] == "taint_path_join"]
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

        # taint_path_join (m9 E): open(<join/concat of a base and an
        # UNBOUND parameter>) without a resolve/containment guard on
        # the path variable. The pattern is structural — never a
        # function-name match.
        for rule in taint:
            attr = name.rsplit(".", 1)[-1]
            if attr not in rule["targets"] or not node.args:
                continue
            arg = node.args[0]
            path_expr = arg
            if isinstance(arg, ast.Call) and _dotted(arg.func) == "os.path.realpath":
                path_expr = None  # guarded by realpath — checked below
            if path_expr is not None and _is_unconfined_path_expr(path_expr, tree):
                out.append({
                    "engine": "ast_python",
                    "rule_id": rule["rule_id"],
                    "category": "path_traversal",
                    "severity": rule["severity"],
                    "risk_level": rule["risk_level"],
                    "file": path,
                    "line": getattr(node, "lineno", 0),
                    "column": getattr(node, "col_offset", 0) + 1,
                    "message": "path traversal: open() on an unconfined "
                               "join/concat of base + untrusted input",
                    "remediation": rule["remediation"],
                    "evidence_text": _dotted(getattr(arg, "func", arg)) or "open",
                })
                break
    return out, False


def _param_names(node):
    """Names a function's own parameters bind (the untrusted surface)."""
    return {a.arg for a in getattr(node.args, "args", []) or []}


def _is_unconfined_path_expr(expr, tree):
    """True when expr is join(base, <param>) / base+str(param) / param
    and the enclosing function does NOT confine the result (realpath
    with a startswith guard, or basename) before open()."""
    # direct parameter reference: open(name)
    if isinstance(expr, ast.Name):
        if _params_for_use(expr.id, expr, tree):
            return not _guarded(expr.id, expr, tree)
        # local variable: taint flows through assignment
        # (path = os.path.join(BASE, name) ... open(path))
        return _local_tainted(expr.id, expr, tree)
    # os.path.join(BASE, <param[, ...]>)
    if isinstance(expr, ast.Call):
        if _dotted(expr.func) in ("os.path.join", "posixpath.join"):
            for a in expr.args[1:]:
                if isinstance(a, ast.Call) and _dotted(a.func) == "os.path.basename":
                    continue  # confined
                if isinstance(a, ast.Name) and _params_for_use(a.id, expr, tree):
                    return not _guarded(a.id, expr, tree)
                # join(BASE, decode(param)) — decoded untrusted input
                if _is_unconfined_path_expr(a, tree):
                    return True
            return False
        if _dotted(expr.func) in ("urllib.parse.unquote", "unquote"):
            inner = expr.args[0] if expr.args else None
            if isinstance(inner, ast.Name) and _params_for_use(inner.id, expr, tree):
                return not _guarded(inner.id, expr, tree)
        return False
    # BASE + "/" + param  (str concat chain)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        names = _collect_names(expr)
        for n in names:
            if _params_for_use(n, expr, tree):
                return not _guarded(n, expr, tree)
    return False


def _local_tainted(name, use_node, tree):
    """True when `name` is a local assigned an unconfined path expr
    (join/concat/decode of a parameter) and never confined after."""
    fn = _enclosing_function(use_node, tree)
    if fn is None:
        return False
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            continue
        value = node.value
        if isinstance(value, ast.Call) and _dotted(value.func) in (
                "os.path.realpath", "os.path.abspath"):
            return False  # resolved before use (guard pattern upstream)
        if isinstance(value, ast.Call) and _dotted(value.func) == "os.path.basename":
            return False
        if _is_unconfined_path_expr(value, tree):
            return not _guarded(name, use_node, tree)
    return False


def _collect_names(expr, acc=None):
    acc = set() if acc is None else acc
    if isinstance(expr, ast.Name):
        acc.add(expr.id)
    for child in ast.iter_child_nodes(expr):
        _collect_names(child, acc)
    return acc


def _enclosing_function(node, tree):
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(fn):
                if child is node:
                    return fn
    return None


def _params_for_use(name, use_node, tree):
    fn = _enclosing_function(use_node, tree)
    if fn is None:
        return False
    return name in _param_names(fn)


def _guarded(name, use_node, tree):
    """True when the enclosing function confines `name`: a realpath/
    abspath assignment followed by a startswith check, or basename()
    applied before use."""
    fn = _enclosing_function(use_node, tree)
    if fn is None:
        return False
    has_startswith = False
    has_basename = False
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and _dotted(node.func) in (
                "str.startswith", "startswith"):
            has_startswith = True
        if isinstance(node, ast.Call) and _dotted(node.func) == "os.path.basename":
            for a in node.args:
                if isinstance(a, ast.Name) and a.id == name:
                    has_basename = True
    return has_startswith or has_basename
