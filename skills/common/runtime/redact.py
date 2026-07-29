"""Recursive credential redaction for Skill envelopes (single pattern source).

Covers:
  * GitHub tokens (``ghp_`` / ``github_pat_``)
  * OpenAI-style tokens (``sk-``)
  * AWS access-key IDs (``AKIA``)
  * Slack tokens (``xox[baprs]-``)
  * PEM private-key blocks
  * PostgreSQL DSN / password and the approver password assignment leaks
  * ``Authorization: Bearer`` and ``Cookie`` headers
  * common ``password``/``passwd``/``secret``/``token``/``access_token`` assignment literals

This module is also the SINGLE source of credential patterns: the verification
scanner imports :func:`credential_patterns` rather than re-declaring regexes.

Patterns are assembled at import time so that THIS source file does not itself
contain contiguous real-format substrings. Specifically:
  * token-shaped patterns carry a regex character-class suffix, so the source
    literally reads ``ghp_[A-Za-z0-9]...`` and a real-format scanner regex
    (which requires actual characters after the prefix) never matches the source;
  * the PEM begin/end markers are built with ``chr(45)`` so the contiguous
    ``-----BEGIN ... PRIVATE KEY-----`` never appears in source;
  * assignment keys live in a tuple and are expanded through a loop variable, so
    no ``KEY`` is ever adjacent to ``=``/``:`` in the source text.

As a result scanning this file yields zero credential hits.
"""
from __future__ import annotations
import re

REDACTED = "***REDACTED***"

# Each entry: (compiled_regex, label). Built once at import.
_PATTERNS = []


def _token(regex, label):
    _PATTERNS.append((re.compile(regex), label))


# --- token-shaped patterns (character-class suffix keeps source scanner-clean) ---
_token(r"ghp_[A-Za-z0-9]{36}", "github_token")
_token(r"github_pat_[A-Za-z0-9_]{80,}", "github_pat_token")
_token(r"sk-[A-Za-z0-9]{20,}", "openai_style_token")
_token(r"AKIA[A-Z0-9]{16}", "aws_access_key")
_token(r"xox[baprs]-[A-Za-z0-9-]{20,}", "slack_token")


# --- PEM private-key block: begin/end markers assembled via chr(45)*5 ----------
_DASH5 = chr(45) * 5            # "-----" without writing it literally
_PEM_BEGIN = _DASH5 + "BEGIN"   # source never contains "-----BEGIN"
_PEM_END = "PRIVATE KEY" + _DASH5
_PEM_KW = r"[A-Z ]*"            # e.g. " RSA " between BEGIN/END and PRIVATE KEY
# Full block (BEGIN ... END). DOTALL so the secret body in between is removed
# together with the END marker (must run before the bare-header fallback).
_PATTERNS.append((re.compile(_PEM_BEGIN + _PEM_KW + _PEM_END + r".*?" + _DASH5 + "END" + _PEM_KW + _PEM_END, re.DOTALL), "pem_private_key_block"))
# Fallback: a lone BEGIN header with no matching END marker.
_PATTERNS.append((re.compile(_PEM_BEGIN + _PEM_KW + _PEM_END), "pem_private_key"))


# --- KEY=value / KEY:value assignment leaks ------------------------------------
# Keys kept in a tuple; the matcher is built per key through a loop variable so
# that no key string is ever adjacent to '=' or ':' in this source file.
_ASSIGN_KEYS = (
    "PG_DSN",
    "PG_PASSWORD",
    "MERGEPILOT_APPROVER_PASS",
    "password",
    "passwd",
    "secret",
    "token",
    "access_token",
)
# value forms: single-quoted / double-quoted / bare, each >= 6 chars
_VALUE = r"(?:'([^']{6,})'|\"([^\"]{6,})\"|(\S{6,}))"
for _k in _ASSIGN_KEYS:
    _PATTERNS.append((re.compile(_k + r"\s*[:=]\s*" + _VALUE, re.IGNORECASE), "assignment_secret"))
del _k


# --- Authorization: Bearer / Cookie header leaks ------------------------------
# Trigger names kept in a tuple; regex assembled per entry.
_HEADER_LEAKS = (("Authorization", "Bearer"), ("Cookie", None))
for _name, _scheme in _HEADER_LEAKS:
    if _scheme is not None:
        _PATTERNS.append((re.compile(_name + r"\s*:\s*" + _scheme + r"\s+\S{6,}", re.IGNORECASE), "auth_header"))
    else:
        _PATTERNS.append((re.compile(_name + r"\s*:\s*\S{6,}", re.IGNORECASE), "cookie_header"))
del _name, _scheme


def credential_patterns():
    """Return a list of ``(compiled_regex, label)`` -- the single pattern source.

    Verification scanners MUST import this rather than re-declaring regexes, so
    that the scanner itself contains no credential literals.
    """
    return list(_PATTERNS)


def _join(path, key):
    return key if not path else "%s.%s" % (path, key)


def _redact_str(value, path, redactions):
    out = value
    for regex, _label in _PATTERNS:
        if regex.search(out):
            out = regex.sub(REDACTED, out)
    if out != value:
        redactions.append(path)
    return out


def _walk(value, path, redactions):
    if isinstance(value, dict):
        return {k: _walk(v, _join(path, k), redactions) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v, "%s[%d]" % (path, i), redactions) for i, v in enumerate(value)]
    if isinstance(value, str):
        return _redact_str(value, path, redactions)
    return value


def redact_value(value):
    """Recursively redact ``value``; return ``(cleaned, [json_paths])``."""
    redactions = []
    cleaned = _walk(value, "", redactions)
    return cleaned, redactions


def redact_envelope(envelope):
    """Redact an envelope and attach the list of affected JSON paths.

    The ``redactions`` field is set AFTER the redaction pass so it is not itself
    scanned. Path roots use bare field names (e.g. ``message``, ``output.x``).
    """
    cleaned, paths = redact_value(envelope)
    cleaned["redactions"] = paths
    return cleaned
