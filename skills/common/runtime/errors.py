"""MergePilot common Skill error codes, exceptions and CLI exit-code mapping.

This module is the SINGLE authority for public error codes. Do not create a
second code list (no separate ``codes.py``). Skill-specific error codes (added
in later milestones) MUST use a ``<SKILL>_`` prefix and reuse the exit-code
mapping below.
"""
from __future__ import annotations

# ---- public error codes (single source of truth) ----------------------------
INVALID_INPUT = "INVALID_INPUT"
SCHEMA_VERSION_UNSUPPORTED = "SCHEMA_VERSION_UNSUPPORTED"
TIMEOUT = "TIMEOUT"
DENIED = "DENIED"
DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
OUTPUT_TOO_LARGE = "OUTPUT_TOO_LARGE"
INTERNAL_ERROR = "INTERNAL_ERROR"

#: ordered tuple of every public code (used by scanners / docs)
ALL_CODES = (
    INVALID_INPUT,
    SCHEMA_VERSION_UNSUPPORTED,
    TIMEOUT,
    DENIED,
    DEPENDENCY_UNAVAILABLE,
    OUTPUT_TOO_LARGE,
    INTERNAL_ERROR,
)

#: error_code -> non-zero CLI exit code (per M4-A contract)
_ERROR_EXIT_CODES = {
    INVALID_INPUT: 2,
    SCHEMA_VERSION_UNSUPPORTED: 2,
    TIMEOUT: 3,
    DENIED: 4,
    DEPENDENCY_UNAVAILABLE: 5,
    # OUTPUT_TOO_LARGE / INTERNAL_ERROR / unknown -> 1
}
_DEFAULT_ERROR_EXIT = 1


def exit_code_for_error(error_code):
    """Return the CLI exit code for a given error_code (default 1)."""
    return _ERROR_EXIT_CODES.get(error_code, _DEFAULT_ERROR_EXIT)


def cli_exit_code(envelope):
    """Map a validated response envelope to its CLI exit code.

    * 0  -> OK or PARTIAL with no business FAIL
    * 10 -> OK with ``output.verdict == FAIL`` (business failure, NOT a runtime error)
    * 2/3/4/5/1 -> runtime ERROR mapped by error_code
    """
    status = envelope.get("status")
    if status == "OK":
        output = envelope.get("output") or {}
        return 10 if output.get("verdict") == "FAIL" else 0
    if status == "PARTIAL":
        return 0
    # status == "ERROR"
    return exit_code_for_error(envelope.get("error_code"))


# ---- runtime exceptions (carry an error_code + message) ---------------------
class SkillError(Exception):
    """Base error raised by a Skill; carries a public ``code`` and ``message``."""

    code = INTERNAL_ERROR

    def __init__(self, message="", code=None):
        super().__init__(message or code or self.code)
        self.message = message or code or self.code
        if code is not None:
            self.code = code


class InvalidInput(SkillError):
    code = INVALID_INPUT

    def __init__(self, message="invalid input"):
        super().__init__(message)


class SchemaVersionUnsupported(SkillError):
    code = SCHEMA_VERSION_UNSUPPORTED

    def __init__(self, message="contract_version not supported"):
        super().__init__(message)


class SkillTimeout(SkillError):
    """Whole-execution timeout (cooperative deadline exceeded)."""

    code = TIMEOUT

    def __init__(self, message="execution timed out"):
        super().__init__(message)


class SkillDenied(SkillError):
    """Action denied by policy / gateway."""

    code = DENIED

    def __init__(self, message="action denied by policy"):
        super().__init__(message)


class DependencyUnavailable(SkillError):
    code = DEPENDENCY_UNAVAILABLE

    def __init__(self, message="required dependency unavailable"):
        super().__init__(message)
