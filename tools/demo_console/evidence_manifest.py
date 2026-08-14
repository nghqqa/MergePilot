"""ISOLATED_LIVE Phase C Evidence Manifest builder (pure functions, no side effects).

This module implements the Phase C evidence-manifest design as PURE functions:
building, validating, redacting, and (optionally) atomically writing a manifest.
It never starts WSL/Docker/PostgreSQL, never accesses any database, and never
writes to the real ``evidence/`` directory unless the caller explicitly invokes
the no-clobber atomic writer with an allowlisted target (which itself refuses
to overwrite anything and refuses any path outside
``evidence/isolated-live/phase-c/``).

Design invariants (see the Phase C design review):

* Identifiers: commit/tag/tree SHAs must be full 40-char lowercase hex; image
  digest must be ``repo@sha256:<64-hex>``; image ID must be ``sha256:<64-hex>``.
  Ellipsis (``...`` / ``\\u2026``), truncated SHAs, or abbreviated digests are
  REJECTED → ``EVIDENCE_GATE_FAILED:IDENTIFIER_INVALID``.
* Execution provenance: ``execution_commit`` + ``execution_tree_oid`` (must
  match ``<commit>^{tree}`` when verified against git), clean-worktree proof,
  ref + remote-ref consistency (or explicit detached-HEAD record).
  → ``EVIDENCE_GATE_FAILED:EXECUTION_TREE_MISMATCH``.
* Provenance mode: exactly one of ``HISTORICAL_PHASE_B_RECORD`` or
  ``FRESH_PHASE_C_REEXECUTION`` with mode-consistent fields
  → ``EVIDENCE_GATE_FAILED:PROVENANCE_MISMATCH``.
* Boundary classifications: the six frozen truth boundaries must hold; any
  upgraded value → ``EVIDENCE_GATE_FAILED:BOUNDARY_MISMATCH``.
* Command records: each carries command/shell_type/started_at/ended_at/
  exit_summary; secrets (passwords, DSNs, tokens, SQL PASSWORD literals, raw
  subprocess output) are FORBIDDEN → ``EVIDENCE_GATE_FAILED:SECRET_FOUND``.
* Protected-path validation: the only permitted target is
  ``evidence/isolated-live/phase-c/<evidence_id>.json``; no overwrite, no path
  escape, no symlink escape; existing evidence must be unchanged after write.
* No-clobber atomic write: temp file in the same directory (named with a
  session random token, mode 0600 where the platform allows), full validation
  of the written bytes, flush+fsync, no-clobber publish (target exists →
  fail), final SHA-256 verification, and failure cleanup of ONLY this
  session's temp files.

Truth boundaries preserved by this module (never upgradable here):
  ephemeral_postgres_verified=true (Phase B record only),
  MergePilot-Test_database_verified=false,
  MergePilot-Test_application_integration_verified=false,
  production_verified=false,
  revision_producer_contract=NOT_VERIFIED,
  audit_producer_contract=NOT_VERIFIED,
  M8=undefined.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets as _secrets
import subprocess
import tempfile
from pathlib import Path
from typing import Any

# ── Constants ────────────────────────────────────────────────────────────────

SCHEMA_VERSION = "1.0"
EVIDENCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_DIGEST_RE = re.compile(r"^[a-z0-9][a-z0-9/._-]*@sha256:[0-9a-f]{64}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
# Ellipsis markers that must never appear in a real manifest.
ELLIPSIS_MARKERS = ("...", "\u2026")

# The ONLY allowlisted evidence target directory (repo-relative, POSIX style).
EVIDENCE_ALLOWLIST_DIR = "evidence/isolated-live/phase-c"

# The execution commit for HISTORICAL_PHASE_B_RECORD mode.
PHASE_B_EXECUTION_COMMIT = "c3838707eb9c1c5db38d4bd77aa0a54653d04a14"
PHASE_B_DOC_REF = "docs/ISOLATED-LIVE-PG-Ephemeral-Verification-PhaseB.md"

PROVENANCE_MODES = ("HISTORICAL_PHASE_B_RECORD", "FRESH_PHASE_C_REEXECUTION")

# Frozen boundary classifications (value must EQUAL these exactly).
BOUNDARY_CLASSIFICATIONS = {
    "ephemeral_postgres_verified": True,
    "MergePilot-Test_database_verified": False,
    "MergePilot-Test_application_integration_verified": False,
    "production_verified": False,
    "revision_producer_contract": "NOT_VERIFIED",
    "audit_producer_contract": "NOT_VERIFIED",
    "M8": "undefined",
}

# Protected paths that must show ZERO diff in any evidence-writing round.
PROTECTED_PATH_PREFIXES = ("samples/", "benchmark/", "tools/audit-db/")

# Secret patterns forbidden anywhere in a manifest (checked on serialized form).
SECRET_PATTERNS = (
    re.compile(r"postgresql?://[^/\s@]+:[^/\s@]+@"),   # full DSN with creds
    re.compile(r"password\s*=\s*['\"]?[^\s;&'\"]{4,}", re.IGNORECASE),
    re.compile(r"PASSWORD\s+'[^']*'", re.IGNORECASE),   # SQL PASSWORD literal
    re.compile(r"ghp_[0-9a-zA-Z]{36}"),
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"sk-[a-zA-Z0-9]{40}"),
    re.compile(r"xox[baprs]-[a-zA-Z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


class EvidenceGateError(Exception):
    """Stable, redacted error raised by any evidence gate failure.

    ``code`` is one of the stable ``EVIDENCE_GATE_FAILED:<REASON>`` strings.
    The message never contains secrets, raw subprocess output, or full DSNs.
    """

    def __init__(self, reason: str, detail: str = ""):
        self.code = "EVIDENCE_GATE_FAILED:%s" % reason
        super().__init__(self.code + ((" (%s)" % detail) if detail else ""))


# ── Identifier validation ────────────────────────────────────────────────────

def _check_no_ellipsis(value: str, field: str) -> None:
    for marker in ELLIPSIS_MARKERS:
        if marker in value:
            raise EvidenceGateError(
                "IDENTIFIER_INVALID",
                "field %s contains ellipsis marker" % field)


def _check_commit_sha(value: Any, field: str) -> None:
    if not isinstance(value, str):
        raise EvidenceGateError("IDENTIFIER_INVALID", "field %s not a string" % field)
    _check_no_ellipsis(value, field)
    if not COMMIT_SHA_RE.fullmatch(value):
        raise EvidenceGateError(
            "IDENTIFIER_INVALID",
            "field %s is not a full 40-char lowercase hex SHA" % field)


def validate_identifiers(manifest: dict) -> None:
    """Validate every identifier field; raise IDENTIFIER_INVALID on any issue."""
    ep = manifest.get("execution_provenance", {})
    _check_commit_sha(ep.get("execution_commit"), "execution_commit")
    _check_commit_sha(ep.get("execution_tree_oid"), "execution_tree_oid")
    _check_commit_sha(ep.get("execution_remote_ref_oid"),
                      "execution_remote_ref_oid")
    _check_commit_sha(manifest.get("merge_commit"), "merge_commit")
    for pc in manifest.get("parent_commits", []):
        _check_commit_sha(pc, "parent_commits[]")
    m7 = manifest.get("m7_closed", {})
    _check_commit_sha(m7.get("object"), "m7_closed.object")
    _check_commit_sha(m7.get("peeled"), "m7_closed.peeled")
    image_digest = manifest.get("image_digest")
    if not isinstance(image_digest, str):
        raise EvidenceGateError("IDENTIFIER_INVALID", "image_digest not a string")
    _check_no_ellipsis(image_digest, "image_digest")
    if not IMAGE_DIGEST_RE.fullmatch(image_digest):
        raise EvidenceGateError(
            "IDENTIFIER_INVALID", "image_digest not repo@sha256:<64-hex>")
    image_id = manifest.get("local_image_id")
    if not isinstance(image_id, str):
        raise EvidenceGateError("IDENTIFIER_INVALID", "local_image_id not a string")
    _check_no_ellipsis(image_id, "local_image_id")
    if not IMAGE_ID_RE.fullmatch(image_id):
        raise EvidenceGateError(
            "IDENTIFIER_INVALID", "local_image_id not sha256:<64-hex>")
    # Serialized form must never contain ellipsis markers anywhere.
    serialized = json.dumps(manifest, ensure_ascii=False)
    for marker in ELLIPSIS_MARKERS:
        if marker in serialized:
            raise EvidenceGateError(
                "IDENTIFIER_INVALID",
                "manifest contains ellipsis marker %r" % marker)


# ── Execution provenance validation ─────────────────────────────────────────

def validate_execution_provenance(manifest: dict, *, git_runner=None) -> None:
    """Validate execution provenance fields.

    ``git_runner`` (optional) is a callable ``fn(args: list[str]) -> str`` that
    executes a git command in the repository and returns stdout; when provided,
    ``execution_tree_oid`` is cross-checked against
    ``git rev-parse <execution_commit>^{tree}``. When omitted (pure validation,
    e.g. for HISTORICAL records built from recorded values), the recorded tree
    OID is only checked for format + presence.
    """
    ep = manifest.get("execution_provenance", {})
    required = ("execution_commit", "execution_tree_oid",
                "execution_worktree_clean", "execution_worktree_porcelain",
                "execution_ref", "execution_remote_ref_oid", "captured_at")
    for field in required:
        if field not in ep:
            raise EvidenceGateError(
                "EXECUTION_TREE_MISMATCH", "missing field %s" % field)
    # porcelain must be EMPTY (and must never carry arbitrary raw content).
    if ep["execution_worktree_porcelain"] != "":
        raise EvidenceGateError(
            "EXECUTION_TREE_MISMATCH", "worktree porcelain is not empty")
    if ep["execution_worktree_clean"] is not True:
        raise EvidenceGateError(
            "EXECUTION_TREE_MISMATCH", "worktree was not clean at execution")
    # ref consistency: remote ref OID must equal execution commit, OR the
    # record must explicitly note a detached HEAD.
    if ep["execution_remote_ref_oid"] != ep["execution_commit"]:
        ref = ep.get("execution_ref", "")
        if not (isinstance(ref, str) and ref.startswith("detached:")):
            raise EvidenceGateError(
                "EXECUTION_TREE_MISMATCH",
                "remote ref OID != execution commit and no detached-HEAD record")
    if git_runner is not None:
        commit = ep["execution_commit"]
        try:
            actual_tree = git_runner(
                ["rev-parse", "%s^{tree}" % commit]).strip()
        except Exception:
            raise EvidenceGateError(
                "EXECUTION_TREE_MISMATCH",
                "could not resolve %s^{tree}" % commit[:0]) from None  # no sha in msg
        if actual_tree != ep["execution_tree_oid"]:
            raise EvidenceGateError(
                "EXECUTION_TREE_MISMATCH",
                "execution_tree_oid does not match commit^{tree}")


# ── Provenance mode validation ───────────────────────────────────────────────

def validate_provenance_mode(manifest: dict) -> None:
    mode = manifest.get("evidence_provenance_mode")
    if mode not in PROVENANCE_MODES:
        raise EvidenceGateError(
            "PROVENANCE_MISMATCH", "unknown provenance mode %r" % (mode,))
    fresh = manifest.get("phase_c_fresh_execution_performed")
    if mode == "HISTORICAL_PHASE_B_RECORD":
        ep = manifest.get("execution_provenance", {})
        if ep.get("execution_commit") != PHASE_B_EXECUTION_COMMIT:
            raise EvidenceGateError(
                "PROVENANCE_MISMATCH",
                "HISTORICAL mode requires the Phase B execution commit")
        if fresh is not False:
            raise EvidenceGateError(
                "PROVENANCE_MISMATCH",
                "HISTORICAL mode requires phase_c_fresh_execution_performed=false")
        refs = manifest.get("referenced_documents", [])
        if PHASE_B_DOC_REF not in refs:
            raise EvidenceGateError(
                "PROVENANCE_MISMATCH",
                "HISTORICAL mode must reference the Phase B document")
    elif mode == "FRESH_PHASE_C_REEXECUTION":
        if fresh is not True:
            raise EvidenceGateError(
                "PROVENANCE_MISMATCH",
                "FRESH mode requires phase_c_fresh_execution_performed=true")
        # FRESH mode must carry its own timing/commands/WSL snapshots/results.
        env = manifest.get("execution_environment", {})
        if not env.get("command_records"):
            raise EvidenceGateError(
                "PROVENANCE_MISMATCH", "FRESH mode requires command_records")
        if "wsl_state_snapshots" not in manifest:
            raise EvidenceGateError(
                "PROVENANCE_MISMATCH", "FRESH mode requires WSL state snapshots")


# ── Boundary classification validation ───────────────────────────────────────

def validate_boundary_classifications(manifest: dict) -> None:
    vc = manifest.get("verification_classifications", {})
    for key, expected in BOUNDARY_CLASSIFICATIONS.items():
        if key not in vc:
            raise EvidenceGateError(
                "BOUNDARY_MISMATCH", "missing classification %s" % key)
        actual = vc[key]
        if type(actual) is not type(expected) or actual != expected:
            raise EvidenceGateError(
                "BOUNDARY_MISMATCH",
                "classification %s must equal %r (got %r)"
                % (key, expected, actual))


# ── Command record validation + secret redaction ────────────────────────────

_COMMAND_REQUIRED_FIELDS = ("command", "shell_type", "started_at",
                            "ended_at", "exit_summary")


def validate_command_records(manifest: dict) -> None:
    records = manifest.get("execution_environment", {}).get("command_records", [])
    if not isinstance(records, list):
        raise EvidenceGateError("SCHEMA_INVALID", "command_records not a list")
    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            raise EvidenceGateError(
                "SCHEMA_INVALID", "command_records[%d] not an object" % i)
        for field in _COMMAND_REQUIRED_FIELDS:
            if not isinstance(rec.get(field), str) or not rec.get(field):
                raise EvidenceGateError(
                    "SCHEMA_INVALID",
                    "command_records[%d] missing field %s" % (i, field))
    # Secret scan over the serialized manifest.
    serialized = json.dumps(manifest, ensure_ascii=False)
    for pattern in SECRET_PATTERNS:
        m = pattern.search(serialized)
        if m:
            raise EvidenceGateError(
                "SECRET_FOUND", "forbidden secret pattern detected")


def redact_manifest_secrets(manifest: dict) -> dict:
    """Return a copy of the manifest with known secret patterns redacted.

    This is a BEST-EFFORT redaction used only for safe logging/diagnostics.
    The authoritative gate is ``validate_command_records`` (which REJECTS
    rather than silently redacts), because a manifest that needed redaction
    must never be published.
    """
    serialized = json.dumps(manifest, ensure_ascii=False)
    redacted = serialized
    redacted = re.sub(r"(postgresql?://[^/\s@]+:)[^/\s@]+(@)",
                      r"\1***REDACTED***\2", redacted)
    redacted = re.sub(r"(password\s*=\s*)['\"]?[^\s;&'\"]+",
                      r"\1***REDACTED***", redacted, flags=re.IGNORECASE)
    redacted = re.sub(r"(PASSWORD\s+)'[^']*'",
                      r"\1'***REDACTED***'", redacted, flags=re.IGNORECASE)
    redacted = re.sub(r"ghp_[0-9a-zA-Z]{36}", "***REDACTED***", redacted)
    redacted = re.sub(r"github_pat_[A-Za-z0-9_]+", "***REDACTED***", redacted)
    redacted = re.sub(r"AKIA[0-9A-Z]{16}", "***REDACTED***", redacted)
    redacted = re.sub(r"sk-[a-zA-Z0-9]{40}", "***REDACTED***", redacted)
    return json.loads(redacted)


# ── Full manifest validation ─────────────────────────────────────────────────

_REQUIRED_TOP_LEVEL = (
    "evidence_id", "schema_version", "generated_at", "evidence_provenance_mode",
    "phase_c_fresh_execution_performed", "execution_provenance",
    "merge_commit", "parent_commits", "m7_closed", "image_digest",
    "local_image_id", "verification_classifications", "explicit_limitations",
    "redaction_policy",
)


def validate_manifest(manifest: dict, *, git_runner=None) -> None:
    """Run every gate in order; raise EvidenceGateError on the first failure."""
    if not isinstance(manifest, dict):
        raise EvidenceGateError("SCHEMA_INVALID", "manifest not an object")
    for field in _REQUIRED_TOP_LEVEL:
        if field not in manifest:
            raise EvidenceGateError(
                "SCHEMA_INVALID", "missing top-level field %s" % field)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise EvidenceGateError(
            "SCHEMA_INVALID", "unsupported schema_version")
    if not EVIDENCE_ID_RE.fullmatch(manifest.get("evidence_id", "")):
        raise EvidenceGateError(
            "SCHEMA_INVALID", "evidence_id format invalid")
    validate_identifiers(manifest)
    validate_execution_provenance(manifest, git_runner=git_runner)
    validate_provenance_mode(manifest)
    validate_boundary_classifications(manifest)
    validate_command_records(manifest)


# ── Manifest builder ─────────────────────────────────────────────────────────

def build_manifest(
    *,
    evidence_id: str,
    generated_at: str,
    evidence_provenance_mode: str,
    execution_provenance: dict,
    merge_commit: str,
    parent_commits: list,
    m7_closed: dict,
    image_digest: str,
    local_image_id: str,
    daemon_fingerprint: dict | None = None,
    test_results: dict | None = None,
    migration_counts: dict | None = None,
    option_a_bind_revision: dict | None = None,
    negative_matrix: dict | None = None,
    cleanup_result: dict | None = None,
    wsl_state_snapshots: dict | None = None,
    secret_scan_result: dict | None = None,
    protected_paths_diff: dict | None = None,
    execution_environment: dict | None = None,
    referenced_documents: list | None = None,
    explicit_limitations: list | None = None,
) -> dict:
    """Build a manifest dict from validated inputs.

    Pure function: no git, no subprocess, no filesystem access. The frozen
    boundary classifications are injected here and CANNOT be overridden by
    callers (they are re-validated anyway).
    """
    return {
        "evidence_id": evidence_id,
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "evidence_provenance_mode": evidence_provenance_mode,
        "phase_c_fresh_execution_performed":
            evidence_provenance_mode == "FRESH_PHASE_C_REEXECUTION",
        "execution_provenance": dict(execution_provenance),
        "merge_commit": merge_commit,
        "parent_commits": list(parent_commits),
        "m7_closed": dict(m7_closed),
        "image_digest": image_digest,
        "local_image_id": local_image_id,
        "daemon_fingerprint": dict(daemon_fingerprint or {}),
        "test_results": dict(test_results or {}),
        "migration_counts": dict(migration_counts or {}),
        "option_a_bind_revision": dict(option_a_bind_revision or {}),
        "negative_matrix": dict(negative_matrix or {}),
        "cleanup_result": dict(cleanup_result or {}),
        "wsl_state_snapshots": dict(wsl_state_snapshots or {}),
        "secret_scan_result": dict(secret_scan_result or {}),
        "protected_paths_diff": dict(protected_paths_diff or {}),
        "execution_environment": dict(execution_environment or {}),
        "referenced_documents": list(referenced_documents or []),
        "verification_classifications": dict(BOUNDARY_CLASSIFICATIONS),
        "explicit_limitations": list(explicit_limitations or [
            "One-shot synthetic disposable PostgreSQL container only",
            "Real MergePilot-Test application database NOT accessed",
            "Production database NOT verified",
            "Controller audit-event producer NOT verified",
            "PostgreSQL production scale, concurrency, PolarDB-PG "
            "compatibility NOT verified",
            "M8 NOT implemented / remains undefined",
        ]),
        "redaction_policy": {
            "excluded": [
                "passwords", "full DSNs", "tokens",
                "raw SQL PASSWORD literals", "raw subprocess output",
            ],
            "exceptions_stored": [
                "stable error codes", "fingerprint fields",
                "redacted log lines",
            ],
        },
    }


# ── Protected-path validation ───────────────────────────────────────────────

# Windows reserved base names (case-insensitive): CON PRN AUX NUL COM1-9 LPT1-9.
_WIN_RESERVED = (
    {"con", "prn", "aux", "nul"}
    | {"com%d" % i for i in range(1, 10)}
    | {"lpt%d" % i for i in range(1, 10)}
)

_TARGET_SUFFIX = ".json"


def _validate_evidence_id_strict(evidence_id: str) -> None:
    """Strict evidence_id checks: regex + Windows filename hardening (Fix 8)."""
    if not evidence_id:
        raise EvidenceGateError("PROTECTED_PATH", "empty evidence_id basename")
    if not EVIDENCE_ID_RE.fullmatch(evidence_id):
        raise EvidenceGateError(
            "PROTECTED_PATH", "evidence_id format invalid")
    # Windows reserved device names (case-insensitive) and their variants.
    stem = evidence_id.split(".")[0].lower()
    if stem in _WIN_RESERVED:
        raise EvidenceGateError(
            "PROTECTED_PATH", "evidence_id uses a Windows reserved name")
    # Trailing dot or trailing space would be silently stripped by Windows.
    if evidence_id.endswith(".") or evidence_id.endswith(" "):
        raise EvidenceGateError(
            "PROTECTED_PATH", "evidence_id ends with dot or space")
    # Windows normalization collision: a name that differs only by
    # case/spacing/dots from an existing sibling resolves to the SAME file.
    # (The caller checks collisions against allowlist-root entries below.)
    if evidence_id != evidence_id.strip():
        raise EvidenceGateError(
            "PROTECTED_PATH", "evidence_id has leading whitespace")


def validate_evidence_target(target: str, repo_root: str) -> tuple:
    """Validate an evidence write target; return (resolved_path, evidence_id).

    Only ``evidence/isolated-live/phase-c/<evidence_id>.json`` is allowed
    (Fix 1: the filename MUST end with the EXACT lowercase ``.json`` suffix;
    ``.JSON``, no suffix, or a double extension is rejected). The evidence_id
    is extracted ONLY from before the exact ``.json`` suffix and additionally
    hardened against Windows reserved names / trailing dot or space /
    case-insensitive collisions with existing files (Fix 8). Rejects path
    escape, absolute paths, drive letters, backslashes, path separators in
    evidence_id, and symlink escape. Fails when the target already exists.
    """
    repo = Path(repo_root).resolve()
    # Structural checks on the POSIX-style relative target string.
    if "\\" in target:
        raise EvidenceGateError("PATH_ESCAPE", "backslash in target")
    if re.match(r"^[A-Za-z]:", target) or target.startswith("/"):
        raise EvidenceGateError("PATH_ESCAPE", "absolute or drive-letter target")
    parts = target.split("/")
    if len(parts) != 4:
        raise EvidenceGateError(
            "PROTECTED_PATH", "target must have exactly 4 path components")
    if parts[:3] != EVIDENCE_ALLOWLIST_DIR.split("/"):
        raise EvidenceGateError(
            "PROTECTED_PATH", "target outside the allowlisted directory")
    filename = parts[3]
    if filename == ".." or ".." in parts:
        raise EvidenceGateError("PATH_ESCAPE", "'..' in target")
    # Fix 1: EXACT .json suffix required (case-sensitive).
    if not filename.endswith(_TARGET_SUFFIX):
        raise EvidenceGateError(
            "PROTECTED_PATH",
            "target filename must end with the exact '.json' suffix")
    evidence_id = filename[: -len(_TARGET_SUFFIX)]
    if not evidence_id:
        raise EvidenceGateError(
            "PROTECTED_PATH", "empty basename before '.json'")
    if "/" in evidence_id or "\\" in evidence_id:
        raise EvidenceGateError(
            "PATH_ESCAPE", "path separator inside evidence_id")
    # Double extension (e.g. "id.json.json") leaves "id.json" as the id which
    # fails the id regex (dot is allowed but ".json" suffix pattern is fine)
    # — reject the obvious double-extension form explicitly.
    if evidence_id.endswith(_TARGET_SUFFIX):
        raise EvidenceGateError(
            "PROTECTED_PATH", "double .json extension rejected")
    _validate_evidence_id_strict(evidence_id)
    allow_root = (repo / EVIDENCE_ALLOWLIST_DIR).resolve()
    candidate = repo / target
    resolved = candidate.resolve()
    # resolve() must remain inside the allowlist root (no symlink escape).
    try:
        resolved.relative_to(allow_root)
    except ValueError:
        raise EvidenceGateError(
            "SYMLINK_REJECTED", "resolved target escapes the allowlist") from None
    # Any parent component being a symlink is rejected outright.
    probe = repo
    for part in parts:
        probe = probe / part
        if probe.is_symlink():
            raise EvidenceGateError(
                "SYMLINK_REJECTED", "symlink component in target path")
    if resolved.exists():
        raise EvidenceGateError("TARGET_EXISTS", "target already exists")
    # Fix 8: Windows-normalization collision — an existing sibling that is
    # case-insensitively equal (ignoring trailing dots/spaces) would map to
    # the same on-disk file on Windows, so publishing would overwrite it.
    if allow_root.is_dir():
        for sibling in allow_root.iterdir():
            sib_norm = sibling.name.lower().rstrip(". ")
            tgt_norm = filename.lower().rstrip(". ")
            if sib_norm == tgt_norm and sibling.name != filename:
                raise EvidenceGateError(
                    "TARGET_EXISTS",
                    "Windows-normalized collision with existing sibling")
    return resolved, evidence_id


def snapshot_existing_evidence(repo_root: str) -> dict:
    """Record {relative_path: sha256} for every existing evidence/ file.

    Fix 4+7 (reviews): root type check FIRST —
      - evidence/ absent            → empty snapshot
      - evidence/ is a regular dir  → continue
      - evidence/ is a symlink      → SYMLINK_REJECTED (never followed)
      - evidence/ exists, not a dir → PROTECTED_PATH
    Any symlink INSIDE evidence/ → SYMLINK_REJECTED. Enumeration failures
    and per-file read/hash failures → IO_ERROR. Only regular files recorded.
    """
    repo = Path(repo_root).resolve()
    evidence_root = repo / "evidence"
    if evidence_root.is_symlink():
        raise EvidenceGateError(
            "SYMLINK_REJECTED", "evidence/ root is a symlink")
    if not evidence_root.exists():
        return {}  # absent → empty snapshot
    if not evidence_root.is_dir():
        # Exists but is a regular file / other non-directory entry.
        raise EvidenceGateError(
            "PROTECTED_PATH", "evidence/ exists but is not a directory")
    snap: dict = {}
    try:
        entries = sorted(evidence_root.rglob("*"))
    except OSError:
        raise EvidenceGateError(
            "IO_ERROR", "failed to enumerate evidence/") from None
    for p in entries:
        if p.is_symlink():
            raise EvidenceGateError(
                "SYMLINK_REJECTED", "symlink inside evidence/ rejected")
        if p.is_dir():
            continue
        if not p.is_file():
            continue
        rel = p.relative_to(repo).as_posix()
        try:
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            raise EvidenceGateError(
                "IO_ERROR", "failed to read existing evidence file") from None
        snap[rel] = digest
    return snap


def verify_existing_evidence_unchanged(
        repo_root: str, before: dict, *,
        new_target_rel: str | None = None,
        new_target_sha256: str | None = None) -> dict:
    """Verify the post-publish evidence set is EXACTLY ``before ∪ {target}``.

    Fix 4 (second review): beyond checking that every ``before`` path/hash is
    unchanged, this now enforces the EXACT expected set:

      - ``after_paths == before_paths ∪ {new_target_rel}`` (when publishing)
      - the new target is the ONLY added file (anything else — including
        Git-ignored extra files — fails with ``EXISTING_EVIDENCE_CHANGED``)
      - the new target's hash equals the published payload hash

    Returns the ``after`` snapshot dict.
    """
    after = snapshot_existing_evidence(repo_root)
    # Every pre-existing file must still be present with an identical hash.
    for rel, sha in before.items():
        if rel not in after:
            raise EvidenceGateError(
                "EXISTING_EVIDENCE_CHANGED", "existing evidence removed")
        if after[rel] != sha:
            raise EvidenceGateError(
                "EXISTING_EVIDENCE_CHANGED", "existing evidence hash changed")
    if new_target_rel is not None:
        expected = dict(before)
        expected[new_target_rel] = new_target_sha256 or ""
        # Exact set equality: no extra additions, no deletions.
        if set(after.keys()) != set(expected.keys()):
            extra = sorted(set(after.keys()) - set(expected.keys()))
            if extra:
                # Even a Git-ignored extra evidence file fails here.
                raise EvidenceGateError(
                    "EXISTING_EVIDENCE_CHANGED",
                    "unexpected additional evidence file(s) present")
            raise EvidenceGateError(
                "EXISTING_EVIDENCE_CHANGED",
                "expected new target missing from evidence set")
        if new_target_sha256 is not None:
            if after.get(new_target_rel) != new_target_sha256:
                raise EvidenceGateError(
                    "CONTENT_HASH_MISMATCH",
                    "new target hash != published payload hash")
    else:
        if set(after.keys()) != set(before.keys()):
            raise EvidenceGateError(
                "EXISTING_EVIDENCE_CHANGED", "evidence set changed")
    return after


def verify_allowed_evidence_diff(repo_root: str, new_target_rel: str,
                                  status_lines: list) -> str:
    """Verify the git diff is EXACTLY one new entry for the target (Fix 4).

    ``status_lines`` are ``git status --porcelain`` output lines. Two publish
    states are allowed for the single new target file:

      - ``??  <target>``  → normal generation, not yet staged (UNTRACKED)
      - ``A   <target>``  → explicitly staged (STAGED)

    Everything else is rejected: ``M``/``D``/``R``/``AM``, additional ``??``
    entries, other evidence files, and ANY change under protected paths.
    Returns the classification string ``"UNTRACKED"`` or ``"STAGED"``.

    The builder itself NEVER runs ``git add`` or otherwise touches the index.
    """
    entries = []
    for line in status_lines:
        line = line.rstrip("\n")
        if not line:
            continue
        change = line[:2]
        path = line[3:].strip().strip('"')
        entries.append((change, path))
    # Protected paths must show zero diff.
    for _change, path in entries:
        for prefix in PROTECTED_PATH_PREFIXES:
            if path == prefix or path.startswith(prefix):
                raise EvidenceGateError(
                    "PROTECTED_PATH", "protected path changed: %s" % prefix)
    # Exactly one entry, and it must be the target in an allowed state.
    if len(entries) != 1:
        raise EvidenceGateError(
            "PROTECTED_PATH",
            "diff must contain exactly one entry (got %d)" % len(entries))
    change, path = entries[0]
    if path != new_target_rel:
        raise EvidenceGateError(
            "PROTECTED_PATH", "the single change is not the new target")
    x, y = change[0], change[1] if len(change) > 1 else " "
    if x == "A" and y == " ":
        return "STAGED"
    if x == "?" and y == "?":
        return "UNTRACKED"
    raise EvidenceGateError(
        "PROTECTED_PATH",
        "target change state %r not allowed (only '?? ' untracked or 'A  ' staged)" % change)


# ── Publish internals (third review: single public write entry point) ────────

def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class PublishResult:
    """Structured result of a successful evidence publish.

    Honest records throughout: the permission capability records what the
    platform actually allowed (never a claimed-but-unverified 0600); the
    directory-fsync durability classification is one of
    ``SUPPORTED_AND_VERIFIED`` / ``UNSUPPORTED_BY_PLATFORM``; and
    ``verification_dependency_mode`` is ``"REAL"`` only when every dependency
    (git plumbing, git status, directory fsync) was the REAL implementation.
    Test-double publishes (``"TEST_DOUBLE"``) must NEVER be treated as
    official evidence.
    """

    def __init__(self, path: Path, content_sha256: str,
                 requested_mode: str, applied_permission_capability: str,
                 git_status_classification: str,
                 directory_fsync_capability: str = "FAILED",
                 directory_fsync_verified: bool = False,
                 verification_dependency_mode: str = "REAL"):
        self.path = path
        self.content_sha256 = content_sha256
        self.requested_mode = requested_mode
        self.applied_permission_capability = applied_permission_capability
        self.git_status_classification = git_status_classification
        self.directory_fsync_capability = directory_fsync_capability
        self.directory_fsync_verified = directory_fsync_verified
        self.verification_dependency_mode = verification_dependency_mode
        self.published = True

    def as_dict(self) -> dict:
        return {
            "path": str(self.path),
            "content_sha256": self.content_sha256,
            "requested_mode": self.requested_mode,
            "applied_permission_capability":
                self.applied_permission_capability,
            "git_status_classification": self.git_status_classification,
            "directory_fsync_capability": self.directory_fsync_capability,
            "directory_fsync_verified": self.directory_fsync_verified,
            "verification_dependency_mode":
                self.verification_dependency_mode,
            "published": self.published,
        }


# Errno values that legitimately mean "fsync not supported on this object".
import errno as _errno  # noqa: E402
_FSYNC_UNSUPPORTED_ERRNOS = {
    getattr(_errno, name)
    for name in ("ENOTSUP", "EOPNOTSUPP", "EINVAL", "ENOSYS")
    if hasattr(_errno, name)
}


def _directory_fsync_classify(target_dir: Path) -> str:
    """Classify directory-fsync durability using the REAL adapter only.

    Accuracy rules (third review Fix 4):
      - ``SUPPORTED_AND_VERIFIED`` — open + fsync both succeeded.
      - ``UNSUPPORTED_BY_PLATFORM`` — ONLY when the platform explicitly
        reports not-supported for directory fsync: POSIX open/fsync fails
        with ENOTSUP/EOPNOTSUPP/ENOSYS/EINVAL, or the platform is Windows
        (directory fsync is not a meaningful durability primitive there).
      - ``FAILED`` — any OTHER error (permission denied, missing path, I/O
        failure). A permission/open failure is NEVER misreported as
        unsupported.

    No injection: test doubles exercise this only via the private publish
    entry point's ``directory_fsync_fn``, and their results are marked
    TEST_DOUBLE (never official).
    """
    if os.name == "nt":
        # Windows: opening a directory for fsync is not a supported durability
        # primitive; record honestly without attempting a misleading call.
        return "UNSUPPORTED_BY_PLATFORM"
    try:
        dfd = os.open(str(target_dir), os.O_RDONLY)
    except OSError as exc:
        if exc.errno in _FSYNC_UNSUPPORTED_ERRNOS:
            return "UNSUPPORTED_BY_PLATFORM"
        # Permission denied / missing path / other I/O → REAL failure.
        return "FAILED"
    try:
        os.fsync(dfd)
    except OSError as exc:
        if exc.errno in _FSYNC_UNSUPPORTED_ERRNOS:
            return "UNSUPPORTED_BY_PLATFORM"
        return "FAILED"
    finally:
        try:
            os.close(dfd)
        except OSError:
            pass
    return "SUPPORTED_AND_VERIFIED"


def _real_git_status_lines(repo_root: str) -> list:
    """Obtain real ``git status --porcelain`` lines (no output retained on failure).

    Uses ``--untracked-files=all`` so files inside brand-new (fully
    untracked) directories are reported INDIVIDUALLY — git otherwise
    collapses them to a single ``?? dir/`` entry, which would break the
    exact-one-entry diff gate.

    Raises ``EvidenceGateError("GIT_STATUS_FAILED")`` on timeout, non-zero
    exit, OSError, or decode failure. stdout/stderr text is NEVER included
    in the error.
    """
    try:
        cp = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=str(repo_root), capture_output=True,
            text=True, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        raise EvidenceGateError("GIT_STATUS_FAILED", "git status timed out") from None
    except OSError:
        raise EvidenceGateError("GIT_STATUS_FAILED", "git status failed to run") from None
    if cp.returncode != 0:
        raise EvidenceGateError("GIT_STATUS_FAILED", "git status non-zero exit")
    try:
        return cp.stdout.splitlines()
    except UnicodeDecodeError:
        raise EvidenceGateError("GIT_STATUS_FAILED", "git status decode failed") from None


class _WriterFailure(Exception):
    """Structured INTERNAL exception from the low-level writer (Fix 5).

    Carries the full publish state so the publisher can perform unified
    ownership-aware rollback WITHOUT the low-level writer swallowing
    anything:
      - primary_error_code / primary_detail (stable codes only)
      - target / payload_sha / published_by_this_session
      - cleanup_error_code (stable code from an already-attempted cleanup,
        or "")
    """

    def __init__(self, primary_error_code: str, primary_detail: str,
                 target=None, payload_sha: str = "",
                 published_by_this_session: bool = False,
                 cleanup_error_code: str = ""):
        self.primary_error_code = primary_error_code
        self.primary_detail = primary_detail
        self.target = target
        self.payload_sha = payload_sha
        self.published_by_this_session = published_by_this_session
        self.cleanup_error_code = cleanup_error_code
        super().__init__("%s (%s)" % (primary_error_code, primary_detail))


def _safe_cleanup_code(fn) -> str:
    """Run a cleanup step converting ANY BaseException to a stable code.

    Returns "" when the step succeeded, else a stable reason string
    (never lets the cleanup exception escape or mask a primary error).
    """
    try:
        fn()
        return ""
    except BaseException:
        return "ROLLBACK_FAILED"


def _no_clobber_atomic_write(
    manifest: dict,
    *,
    repo_root: str,
    evidence_id: str,
    additional_validate_fn=None,
) -> tuple:
    """LOW-LEVEL writer. PRIVATE (third review Fix 1): not in ``__all__``.

    Performs steps 1-6 of the publish order:
      1. validate target/path
      2. validate manifest ID consistency
      3. (snapshot is taken by the publisher, BEFORE this call)
      4. mandatory manifest validation (in-memory + written bytes)
      5. temp write + file fsync + no-clobber link
      6. verify target hash

    NEVER swallows post-link errors (Fix 5): after ``os.link`` succeeds, any
    failure (final-hash mismatch, read failure) raises ``_WriterFailure``
    carrying ``published_by_this_session=True`` plus any cleanup code from
    its own best-effort target removal — the PUBLISHER owns final rollback.
    Before ``os.link``, failures raise ``_WriterFailure`` with
    ``published_by_this_session=False`` (no target exists to roll back).

    Returns ``(target_path, payload_sha256, permission_capability)``.
    """
    target_rel = "%s/%s.json" % (EVIDENCE_ALLOWLIST_DIR, evidence_id)
    try:
        target, parsed_id = validate_evidence_target(target_rel, repo_root)
    except EvidenceGateError as exc:
        raise _WriterFailure(exc.code, "target validation failed") from None

    # Step 2: manifest identity must match the target id exactly.
    if manifest.get("evidence_id") != parsed_id or \
            manifest.get("evidence_id") != evidence_id:
        raise _WriterFailure(
            "EVIDENCE_GATE_FAILED:SCHEMA_INVALID",
            "manifest evidence_id does not match the target evidence_id")

    payload = json.dumps(manifest, indent=2, sort_keys=True,
                         ensure_ascii=False).encode("utf-8")
    payload_sha = _sha256_bytes(payload)
    # Step 4 (part 1): MANDATORY in-memory validation.
    try:
        validate_manifest(manifest)
    except EvidenceGateError as exc:
        raise _WriterFailure(exc.code, "in-memory manifest validation failed") from None
    if additional_validate_fn is not None:
        try:
            additional_validate_fn(manifest)
        except BaseException:
            raise _WriterFailure(
                "EVIDENCE_GATE_FAILED:SCHEMA_INVALID",
                "additional validator rejected the manifest") from None

    target_dir = target.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    token = _secrets.token_hex(8)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".evidence-tmp-%s-" % token, suffix=".json", dir=str(target_dir))
    tmp_path = Path(tmp_name)
    permission_capability = {"requested_mode": "0600",
                             "platform_applied": None}
    published = False
    cleanup_code = ""
    try:
        os.close(fd)
        if os.name == "posix":
            try:
                os.chmod(tmp_path, 0o600)
                permission_capability["platform_applied"] = "0600"
            except OSError:
                permission_capability["platform_applied"] = "chmod-failed"
        else:
            permission_capability["platform_applied"] = (
                "windows-default (POSIX 0600 not enforceable)")
        with open(tmp_path, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        # Step 4 (part 2): re-validate the WRITTEN bytes.
        written = tmp_path.read_bytes()
        try:
            reparsed = json.loads(written.decode("utf-8"))
        except Exception:
            raise _WriterFailure(
                "EVIDENCE_GATE_FAILED:SCHEMA_INVALID",
                "written file is not valid JSON",
                target=None, payload_sha=payload_sha,
                published_by_this_session=False) from None
        try:
            validate_manifest(reparsed)
        except EvidenceGateError as exc:
            raise _WriterFailure(
                exc.code, "written manifest validation failed",
                payload_sha=payload_sha) from None
        if _sha256_bytes(written) != payload_sha:
            raise _WriterFailure(
                "EVIDENCE_GATE_FAILED:CONTENT_HASH_MISMATCH",
                "written bytes differ from payload",
                payload_sha=payload_sha)
        # Step 5: no-clobber publish.
        if target.exists():
            raise _WriterFailure(
                "EVIDENCE_GATE_FAILED:TARGET_EXISTS",
                "target exists at publish", payload_sha=payload_sha)
        try:
            os.link(tmp_path, target)
            published = True
        except FileExistsError:
            raise _WriterFailure(
                "EVIDENCE_GATE_FAILED:TARGET_EXISTS",
                "target appeared during publish",
                payload_sha=payload_sha) from None
        except OSError:
            raise _WriterFailure(
                "EVIDENCE_GATE_FAILED:ATOMIC_PUBLISH_FAILED",
                "no-clobber publish failed",
                payload_sha=payload_sha) from None
        # Step 6: verify target hash. A mismatch after publish triggers
        # best-effort self-removal, but the code + cleanup result are BOTH
        # carried in the structured failure (nothing is swallowed).
        try:
            final_hash = _sha256_bytes(target.read_bytes())
        except BaseException:
            cleanup_code = _safe_cleanup_code(
                lambda: target.unlink() if target.exists() else None)
            raise _WriterFailure(
                "EVIDENCE_GATE_FAILED:CONTENT_HASH_MISMATCH",
                "failed to read target for hash verification",
                target=target, payload_sha=payload_sha,
                published_by_this_session=published,
                cleanup_error_code=cleanup_code or "ROLLBACK_FAILED") from None
        if final_hash != payload_sha:
            cleanup_code = _safe_cleanup_code(
                lambda: target.unlink() if target.exists() else None)
            raise _WriterFailure(
                "EVIDENCE_GATE_FAILED:CONTENT_HASH_MISMATCH",
                "final file hash mismatch",
                target=target, payload_sha=payload_sha,
                published_by_this_session=published,
                cleanup_error_code=cleanup_code)
        return target, payload_sha, permission_capability
    except _WriterFailure:
        raise
    except BaseException as exc:
        # Unexpected low-level failure: wrap WITHOUT swallowing; if the
        # target was published, attempt best-effort removal and record it.
        cleanup_code = ""
        if published and target.exists():
            cleanup_code = _safe_cleanup_code(lambda: target.unlink())
        raise _WriterFailure(
            "EVIDENCE_GATE_FAILED:IO_ERROR",
            "unexpected writer failure: %s" % type(exc).__name__,
            target=target if published else None,
            payload_sha=payload_sha,
            published_by_this_session=published,
            cleanup_error_code=cleanup_code) from None
    finally:
        # Delete ONLY this session's temp file (matched by our token prefix).
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


# ── Full publish orchestration ────────────────────────────────────────────────

class EvidencePublishError(Exception):
    """Primary + cleanup error, both as stable codes (never masked).

    ``primary_error_code`` is the gate that failed; ``cleanup_error_code``
    is the stable reason of a rollback/verification failure (empty when the
    rollback was clean). The message contains ONLY stable codes — never raw
    subprocess output, manifest content, or secrets.
    """

    def __init__(self, primary_reason: str, cleanup_reason: str = ""):
        self.primary_error_code = "EVIDENCE_GATE_FAILED:%s" % primary_reason
        self.cleanup_error_code = ("EVIDENCE_GATE_FAILED:%s" % cleanup_reason
                                   if cleanup_reason else "")
        super().__init__("primary=%s cleanup=%s"
                         % (self.primary_error_code,
                            self.cleanup_error_code or "none"))

    @property
    def primary_code(self):  # backwards-compatible alias
        return self.primary_error_code

    @property
    def cleanup_code(self):  # backwards-compatible alias
        return self.cleanup_error_code


def _publish_evidence_with_dependencies(
    manifest: dict,
    *,
    repo_root: str,
    additional_validate_fn=None,
    git_status_fn=None,
    directory_fsync_fn=None,
) -> PublishResult:
    """PRIVATE test entry point (third review Fix 2): allows injecting test
    doubles for git status / directory fsync.

    Every publish produced through this entry point is marked
    ``verification_dependency_mode="TEST_DOUBLE"`` (unless no doubles were
    injected) and must NEVER be treated as official evidence. The official
    path is :func:`publish_evidence`, which accepts NO injection parameters
    and always uses the REAL git plumbing, git status, and fsync adapter.

    Publish order (Fix 3, third review):
      1. validate target/path          (writer)
      2. validate manifest ID          (writer)
      3. snapshot existing evidence    (publisher, before writer)
      4. mandatory manifest validation (writer)
      5. write + file fsync + link     (writer)
      6. verify target hash            (writer)
      7. verify exact evidence set     (publisher)
      8. obtain git status             (publisher; REAL or injected double)
      9. verify allowed diff           (publisher)
     10. directory fsync (LAST gate)   (publisher)
     11. return PublishResult
    """
    evidence_id = manifest.get("evidence_id", "")
    target_rel = "%s/%s.json" % (EVIDENCE_ALLOWLIST_DIR, evidence_id)
    mode = "TEST_DOUBLE" if (git_status_fn is not None
                             or directory_fsync_fn is not None) else "REAL"

    # Step 3: snapshot BEFORE any write.
    before = snapshot_existing_evidence(repo_root)

    # Steps 1,2,4,5,6.
    try:
        target, payload_sha, permission_capability = _no_clobber_atomic_write(
            manifest, repo_root=repo_root, evidence_id=evidence_id,
            additional_validate_fn=additional_validate_fn)
    except _WriterFailure as wf:
        # Low-level failure: unify primary + any writer-attempted cleanup.
        cleanup = wf.cleanup_error_code
        if wf.published_by_this_session and wf.target is not None:
            extra = _rollback_and_verify(wf.target, wf.payload_sha,
                                         repo_root, before, target_rel)
            cleanup = cleanup or extra
        reason = wf.primary_error_code.split(":", 1)[1] \
            if ":" in wf.primary_error_code else wf.primary_error_code
        raise EvidencePublishError(reason, cleanup) from None

    def _fail(primary_reason: str) -> None:
        cleanup = _rollback_and_verify(target, payload_sha, repo_root,
                                       before, target_rel)
        raise EvidencePublishError(primary_reason, cleanup) from None

    try:
        # Step 7: EXACT evidence snapshot set.
        try:
            verify_existing_evidence_unchanged(
                repo_root, before,
                new_target_rel=target_rel, new_target_sha256=payload_sha)
        except BaseException as exc:
            reason = (exc.code.split(":", 1)[1]
                      if isinstance(exc, EvidenceGateError) else "IO_ERROR")
            _fail(reason)
        # Step 8: git status (REAL or injected double).
        try:
            if git_status_fn is not None:
                status_lines = git_status_fn()
            else:
                status_lines = _real_git_status_lines(repo_root)
        except BaseException as exc:
            reason = (exc.code.split(":", 1)[1]
                      if isinstance(exc, EvidenceGateError)
                      else "GIT_STATUS_FAILED")
            _fail(reason)
        # Step 9: allowed diff.
        try:
            classification = verify_allowed_evidence_diff(
                repo_root, target_rel, status_lines)
        except EvidenceGateError as exc:
            _fail(exc.code.split(":", 1)[1])
        # Step 10: directory fsync — the LAST success gate.
        if directory_fsync_fn is not None:
            try:
                outcome = directory_fsync_fn(target.parent)
            except BaseException:
                outcome = "FAILED"
            if outcome == "UNSUPPORTED":
                fsync_capability = "UNSUPPORTED_BY_PLATFORM"
            elif outcome == "FAILED":
                fsync_capability = "FAILED"
            else:
                fsync_capability = "SUPPORTED_AND_VERIFIED"
        else:
            fsync_capability = _directory_fsync_classify(target.parent)
        if fsync_capability == "FAILED":
            _fail("IO_ERROR")  # rollback + cleanup codes preserved
        # Step 11: success.
        return PublishResult(
            path=target,
            content_sha256=payload_sha,
            requested_mode=permission_capability["requested_mode"],
            applied_permission_capability=(
                permission_capability["platform_applied"]),
            git_status_classification=classification,
            directory_fsync_capability=fsync_capability,
            directory_fsync_verified=(
                fsync_capability == "SUPPORTED_AND_VERIFIED"),
            verification_dependency_mode=mode)
    except EvidencePublishError:
        raise
    except BaseException:
        # Unexpected post-publish failure (KeyboardInterrupt etc.).
        _fail("IO_ERROR")


def _rollback_and_verify(target: Path, payload_sha: str, repo_root: str,
                         before: dict, target_rel: str) -> str:
    """Unified ownership-aware rollback; returns a stable cleanup code.

    Order (Fix 3/6, third review):
      1. remove the target ONLY when provably ours (content hash match)
      2. after deletion, run a rollback directory fsync and record its
         result (ROLLBACK_FSYNC_FAILED on failure — never masks primary)
      3. re-verify the existing-evidence snapshot is intact
    Returns "" on a fully clean rollback, else one of ROLLBACK_FAILED /
    TARGET_STILL_PRESENT / ROLLBACK_FSYNC_FAILED / CLEANUP_SNAPSHOT_FAILED
    (first failure wins; subsequent steps still attempted where safe).
    """
    code = ""
    # 1. Ownership-checked removal.
    try:
        if not target.exists():
            pass  # already gone
        else:
            content = target.read_bytes()
            if _sha256_bytes(content) != payload_sha:
                code = code or "TARGET_STILL_PRESENT"  # not provably ours
            else:
                target.unlink()
                if target.exists():
                    code = code or "TARGET_STILL_PRESENT"
    except BaseException:
        code = code or "ROLLBACK_FAILED"
    # 2. Rollback directory fsync (only when we actually deleted something).
    if not code and not target.exists():
        try:
            fsync_result = _directory_fsync_classify(target.parent)
            if fsync_result == "FAILED":
                code = "ROLLBACK_FSYNC_FAILED"
        except BaseException:
            code = "ROLLBACK_FSYNC_FAILED"
    # 3. Snapshot re-verification.
    try:
        verify_existing_evidence_unchanged(repo_root, before)
    except BaseException:
        code = code or "CLEANUP_SNAPSHOT_FAILED"
    return code


def publish_evidence(manifest: dict, *, repo_root: str,
                     additional_validate_fn=None) -> PublishResult:
    """The ONLY public write entry point (third review Fix 1).

    Accepts NO dependency-injection parameters: the REAL git plumbing, REAL
    ``git status --porcelain``, and the REAL directory-fsync adapter are
    always used. Test doubles are only reachable via the PRIVATE
    ``_publish_evidence_with_dependencies`` and produce TEST_DOUBLE results
    that must never be published as official evidence.

    The full gate chain cannot be bypassed through this entry point: target
    validation, snapshot, mandatory manifest validation, exact evidence-set
    verification, git-status diff, and directory durability all run.
    """
    return _publish_evidence_with_dependencies(
        manifest, repo_root=repo_root,
        additional_validate_fn=additional_validate_fn)


__all__ = [
    "BOUNDARY_CLASSIFICATIONS",
    "EVIDENCE_ALLOWLIST_DIR",
    "EVIDENCE_ID_RE",
    "EvidenceGateError",
    "EvidencePublishError",
    "PHASE_B_DOC_REF",
    "PHASE_B_EXECUTION_COMMIT",
    "PROTECTED_PATH_PREFIXES",
    "PROVENANCE_MODES",
    "PublishResult",
    "SCHEMA_VERSION",
    "build_manifest",
    "publish_evidence",
    "redact_manifest_secrets",
    "snapshot_existing_evidence",
    "validate_boundary_classifications",
    "validate_command_records",
    "validate_evidence_target",
    "validate_execution_provenance",
    "validate_identifiers",
    "validate_manifest",
    "validate_provenance_mode",
    "verify_allowed_evidence_diff",
    "verify_existing_evidence_unchanged",
]
