"""Historical verification-attestation schema and fail-closed validator.

Phase 1-H: a SINGLE versioned JSON record (verification/<id>.json) carries
the historical truth of ONE local ephemeral full-E2E execution (Phase 1-G).
It is NOT runtime state — /api/live/status deliberately never serves these
fields — and NOT a persisted evidence bundle (the ephemeral environment and
all original artifacts were cleaned up; evidence_persisted=false).

Relationship to the existing truth sources:
  * evidence_manifest.py (Phase C) froze the six production boundaries
    (database/application/production false, producer contracts
    NOT_VERIFIED). Those are PERSISTED-frozen semantics for evidence
    bundles and are NOT re-derived here — this attestation only RE-STATES
    them as unchanged boundaries.
  * The three UI verification classifications (eight_pages_live_render_,
    dynamic_pages_live_refresh_, mobile_layout_ verified) previously had
    no repository status source at all; this file is their sole carrier.

Pure functions, no side effects: no WSL/Docker/PostgreSQL, no filesystem
writes. Validation is fail-closed (raises VerificationAttestationError
with a stable code).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ATTESTATION_PATH = REPO_ROOT / "verification" / "phase1g-full-e2e.json"

SCHEMA_VERSION = "mergepilot.verification-attestation.v1"

REQUIRED_TOP_KEYS = (
    "schema_version", "verification_id", "phase", "verification_kind",
    "verified_at", "baseline_commit", "run_id", "classifications",
    "retained_artifacts", "boundaries", "semantics",
)

ALLOWED_CLASSIFICATIONS = frozenset({
    "built_images_verified",
    "postgres_bootstrap_verified",
    "gateway_mcp_initialize_verified",
    "gateway_zero_tools_verified",
    "gateway_startup_plumbing_verified",
    "controller_readiness_verified",
    "full_stack_startup_verified",
    "demo_console_isolated_live_verified",
    "full_stack_preflight_verified",
    "eight_pages_live_render_verified",
    "dynamic_pages_live_refresh_verified",
    "mobile_layout_verified",
    "cleanup_verified",
})

RETAINED_ARTIFACT_KEYS = frozenset({
    "evidence_persisted", "screenshots_retained", "logs_retained",
    "database_snapshot_retained",
})

REQUIRED_BOUNDARY_KEYS = frozenset({
    "database_verified", "application_integration_verified",
    "production_verified", "revision_producer_contract",
    "audit_producer_contract", "gateway_classification",
    "dynamic_update_source", "m8",
})

_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_RUN_ID_RE = re.compile(r"^run-[a-z0-9-]+\-[0-9a-f]{8}$")
_ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

VERIFICATION_KIND = "local_ephemeral_full_e2e"


class VerificationAttestationError(Exception):
    """Fail-closed validation failure with a stable code."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__("%s: %s" % (code, detail))
        self.code = code
        self.detail = detail


def _require(condition: bool, code: str, detail: str = "") -> None:
    if not condition:
        raise VerificationAttestationError(code, detail)


def load_attestation(path: Path = ATTESTATION_PATH) -> dict:
    """Load and parse the attestation JSON (fail-closed on IO/parse)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise VerificationAttestationError("ATTESTATION_MISSING",
                                           str(path)) from None
    except json.JSONDecodeError as exc:
        raise VerificationAttestationError("ATTESTATION_INVALID_JSON",
                                           str(exc)) from None


def validate_attestation(record: Any) -> None:
    """Fail-closed validation of one attestation record.

    Raises VerificationAttestationError on the first violation. Checks the
    full matrix from the Phase 1-H authorization: required keys, identifier
    formats, exact classification key set with boolean values, cross-field
    consistency (dynamic refresh requires eight pages AND full stack AND
    preflight), frozen boundaries (production may never be true here,
    application integration may not be true while producer contracts are
    NOT_VERIFIED, M8 may not be defined, gateway may never be called
    application integration, the dynamic-update source may never be called
    producer integration), and retained-artifact honesty (nothing may be
    claimed persisted without an artifact manifest — none exists).
    """
    _require(isinstance(record, dict), "ATTESTATION_NOT_DICT")
    for key in REQUIRED_TOP_KEYS:
        _require(key in record, "ATTESTATION_MISSING_KEY", key)

    _require(record["schema_version"] == SCHEMA_VERSION,
             "ATTESTATION_SCHEMA_VERSION", str(record["schema_version"]))
    _require(record["verification_kind"] == VERIFICATION_KIND,
             "ATTESTATION_KIND", str(record.get("verification_kind")))
    for key in ("verification_id", "phase"):
        _require(isinstance(record[key], str) and record[key].strip(),
                 "ATTESTATION_EMPTY", key)

    verified_at = record["verified_at"]
    _require(isinstance(verified_at, str) and _ISO_Z_RE.match(verified_at),
             "ATTESTATION_BAD_VERIFIED_AT", str(verified_at))

    baseline = record["baseline_commit"]
    _require(isinstance(baseline, str) and _SHA40_RE.match(baseline),
             "ATTESTATION_BAD_COMMIT", str(baseline))

    run_id = record["run_id"]
    _require(isinstance(run_id, str) and _RUN_ID_RE.match(run_id),
             "ATTESTATION_BAD_RUN_ID", str(run_id))

    classifications = record["classifications"]
    _require(isinstance(classifications, dict), "CLASSIFICATIONS_NOT_DICT")
    keys = set(classifications)
    _require(keys == set(ALLOWED_CLASSIFICATIONS),
             "CLASSIFICATIONS_KEY_SET",
             "unknown=%s missing=%s" % (
                 sorted(keys - set(ALLOWED_CLASSIFICATIONS)),
                 sorted(set(ALLOWED_CLASSIFICATIONS) - keys)))
    for key, value in classifications.items():
        _require(isinstance(value, bool), "CLASSIFICATION_NOT_BOOL",
                 "%s=%r" % (key, value))

    # Cross-field consistency: the dynamic-refresh claim subsumes the
    # cheaper gates it was built on.
    if classifications.get("dynamic_pages_live_refresh_verified"):
        _require(classifications.get("eight_pages_live_render_verified"),
                 "CONSISTENCY_DYNAMIC_WITHOUT_PAGES")
        _require(classifications.get("full_stack_startup_verified"),
                 "CONSISTENCY_DYNAMIC_WITHOUT_STACK")
        _require(classifications.get("full_stack_preflight_verified"),
                 "CONSISTENCY_DYNAMIC_WITHOUT_PREFLIGHT")

    retained = record["retained_artifacts"]
    _require(isinstance(retained, dict), "RETAINED_NOT_DICT")
    _require(set(retained) == RETAINED_ARTIFACT_KEYS, "RETAINED_KEY_SET",
             str(sorted(set(retained) ^ set(RETAINED_ARTIFACT_KEYS))))
    for key, value in retained.items():
        _require(isinstance(value, bool), "RETAINED_NOT_BOOL",
                 "%s=%r" % (key, value))
    # Nothing may be claimed persisted: this record is an attestation, not
    # an evidence bundle, and no artifact manifest exists to back a claim.
    for key in RETAINED_ARTIFACT_KEYS:
        _require(retained.get(key) is not True,
                 "RETAINED_CLAIM_WITHOUT_MANIFEST", key)

    boundaries = record["boundaries"]
    _require(isinstance(boundaries, dict), "BOUNDARIES_NOT_DICT")
    _require(set(boundaries) == set(REQUIRED_BOUNDARY_KEYS),
             "BOUNDARIES_KEY_SET",
             str(sorted(set(boundaries) ^ set(REQUIRED_BOUNDARY_KEYS))))
    _require(boundaries.get("production_verified") is not True,
             "BOUNDARY_PRODUCTION_TRUE")
    _require(boundaries.get("application_integration_verified") is not True,
             "BOUNDARY_APPLICATION_INTEGRATION_TRUE")
    if boundaries.get("application_integration_verified") is True:
        _require(boundaries.get("revision_producer_contract")
                 != "NOT_VERIFIED",
                 "BOUNDARY_INTEGRATION_WITHOUT_PRODUCER")
    _require(boundaries.get("revision_producer_contract") == "NOT_VERIFIED",
             "BOUNDARY_REVISION_CONTRACT")
    _require(boundaries.get("audit_producer_contract") == "NOT_VERIFIED",
             "BOUNDARY_AUDIT_CONTRACT")
    _require(boundaries.get("m8") == "undefined", "BOUNDARY_M8_DEFINED",
             str(boundaries.get("m8")))
    _require(boundaries.get("gateway_classification")
             == "publication_and_startup_plumbing",
             "BOUNDARY_GATEWAY_CLASSIFICATION",
             str(boundaries.get("gateway_classification")))
    _require(boundaries.get("dynamic_update_source")
             == "admin_verification_seed",
             "BOUNDARY_DYNAMIC_UPDATE_SOURCE",
             str(boundaries.get("dynamic_update_source")))

    semantics = record["semantics"]
    _require(isinstance(semantics, dict), "SEMANTICS_NOT_DICT")
    joined = " ".join(str(v) for v in semantics.values()).lower()
    _require("producer integration" in joined
             and "not" in joined,
             "SEMANTICS_MISSING_SEED_NOTE")
    _require("verification seed" in joined or
             "admin verification seed" in joined,
             "SEMANTICS_MISSING_SEED_NOTE")
    _require("not a persisted evidence bundle" in joined,
             "SEMANTICS_MISSING_ARTIFACT_NOTE")


def load_and_validate(path: Path = ATTESTATION_PATH) -> dict:
    """Convenience: load then validate (the canonical entry point)."""
    record = load_attestation(path)
    validate_attestation(record)
    return record
