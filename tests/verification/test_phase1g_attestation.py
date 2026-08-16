"""Phase 1-H: verification-attestation schema tests.

The positive test loads the REAL record from verification/phase1g-full-e2e.json
(no fixture copy) and validates it. The negative mutations derive from that
real record in memory (deep-copied), so the tests always track the actual
status source. No WSL/Docker/PostgreSQL; pure file + function tests.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import unittest
from pathlib import Path

_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
_ROOT = _HERE.parent.parent
for _p in (str(_HERE), str(_ROOT), str(_ROOT / "tools"),
           str(_ROOT / "tools" / "verification")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import phase1g_attestation as att  # noqa: E402

ATTESTATION_PATH = _ROOT / "verification" / "phase1g-full-e2e.json"


def _gate(case, record, code):
    with case.assertRaises(att.VerificationAttestationError) as cm:
        att.validate_attestation(record)
    case.assertEqual(cm.exception.code, code, msg=str(cm.exception))


class TestRealAttestation(unittest.TestCase):

    def test_real_record_loads_and_validates(self):
        # THE positive test: the actual status source on disk.
        record = att.load_and_validate()
        self.assertEqual(att.SCHEMA_VERSION, record["schema_version"])
        self.assertEqual(
            "77d3d7dc71656d134c4203d07a83ae69c9f124fa",
            record["baseline_commit"])
        self.assertEqual("run-1gf-1786855901-fc0a35c8", record["run_id"])
        for key in ("eight_pages_live_render_verified",
                    "dynamic_pages_live_refresh_verified",
                    "mobile_layout_verified"):
            self.assertIs(record["classifications"][key], True, key)
        for key in ("evidence_persisted", "screenshots_retained",
                    "logs_retained", "database_snapshot_retained"):
            self.assertIs(record["retained_artifacts"][key], False, key)

    def test_file_is_pretty_json_and_single_source(self):
        raw = ATTESTATION_PATH.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        self.assertEqual(parsed, att.load_attestation())
        # Keep one Phase 1-G source without blocking future attestations.
        verification_dir = ATTESTATION_PATH.parent
        phase1g_json = sorted(p.name for p in verification_dir.glob("phase1g-*.json"))
        self.assertEqual([ATTESTATION_PATH.name], phase1g_json)

        records = []
        for path in sorted(verification_dir.glob("*.json")):
            records.append((path.name, json.loads(path.read_text(encoding="utf-8"))))

        verification_ids = [record["verification_id"] for _, record in records]
        self.assertEqual(len(verification_ids), len(set(verification_ids)))

        run_keys = [
            (
                record["baseline_commit"],
                record["verification_kind"],
                record["run_id"],
            )
            for _, record in records
        ]
        self.assertEqual(len(run_keys), len(set(run_keys)))

        canonical_twins = sorted(
            path.name
            for path in verification_dir.iterdir()
            if path.stem == ATTESTATION_PATH.stem
        )
        self.assertEqual([ATTESTATION_PATH.name], canonical_twins)

    def test_runtime_status_does_not_carry_these_fields(self):
        # The historical fields must never leak into the runtime contract.
        serve = (_ROOT / "tools" / "demo_console" / "serve.py").read_text(
            encoding="utf-8")
        integration = (_ROOT / "tools" / "demo_console" /
                       "mergepilot_integration.py").read_text(
            encoding="utf-8")
        for field in ("eight_pages_live_render_verified",
                      "dynamic_pages_live_refresh_verified",
                      "mobile_layout_verified"):
            self.assertNotIn(field, serve, field)
            self.assertNotIn(field, integration, field)


class TestNegativeMutations(unittest.TestCase):
    """Every mutation derives from the REAL record (deep copy)."""

    def setUp(self):
        self.base = att.load_attestation()

    def _mut(self, **kwargs):
        record = copy.deepcopy(self.base)
        for path, value in kwargs.items():
            obj = record
            keys = path.split("__")
            for key in keys[:-1]:
                obj = obj[key]
            if value is ...:
                del obj[keys[-1]]
            else:
                obj[keys[-1]] = value
        return record

    def test_missing_required_keys(self):
        for key in ("baseline_commit", "run_id", "verified_at",
                    "classifications", "boundaries"):
            _gate(self, self._mut(**{key.replace("_", "__", 0): ...})
                  if False else self._delete(key),
                  "ATTESTATION_MISSING_KEY")

    def _delete(self, key):
        record = copy.deepcopy(self.base)
        del record[key]
        return record

    def test_bad_sha(self):
        _gate(self, self._mut(baseline_commit="77d3d7d"), "ATTESTATION_BAD_COMMIT")
        _gate(self, self._mut(baseline_commit="XYZ"), "ATTESTATION_BAD_COMMIT")
        _gate(self, self._mut(baseline_commit="77d3d7dc71656d134c4203d07a83ae69c9f124fA"),
              "ATTESTATION_BAD_COMMIT")

    def test_bad_run_id(self):
        _gate(self, self._mut(run_id=""), "ATTESTATION_BAD_RUN_ID")
        _gate(self, self._mut(run_id="fixed-run"), "ATTESTATION_BAD_RUN_ID")
        _gate(self, self._mut(run_id="run-1gf-123-short"), "ATTESTATION_BAD_RUN_ID")

    def test_unknown_classification_key(self):
        record = self._mut()
        record["classifications"]["whatever_verified"] = True
        _gate(self, record, "CLASSIFICATIONS_KEY_SET")

    def test_missing_classification_key(self):
        record = self._mut()
        del record["classifications"]["mobile_layout_verified"]
        _gate(self, record, "CLASSIFICATIONS_KEY_SET")

    def test_non_boolean_classification(self):
        record = self._mut()
        record["classifications"]["built_images_verified"] = "true"
        _gate(self, record, "CLASSIFICATION_NOT_BOOL")

    def test_dynamic_refresh_without_eight_pages(self):
        record = self._mut()
        record["classifications"]["eight_pages_live_render_verified"] = False
        _gate(self, record, "CONSISTENCY_DYNAMIC_WITHOUT_PAGES")

    def test_dynamic_refresh_without_full_stack(self):
        record = self._mut()
        record["classifications"]["full_stack_startup_verified"] = False
        _gate(self, record, "CONSISTENCY_DYNAMIC_WITHOUT_STACK")

    def test_dynamic_refresh_without_preflight(self):
        record = self._mut()
        record["classifications"]["full_stack_preflight_verified"] = False
        _gate(self, record, "CONSISTENCY_DYNAMIC_WITHOUT_PREFLIGHT")

    def test_application_integration_true_with_not_verified_producers(self):
        record = self._mut()
        record["boundaries"]["application_integration_verified"] = True
        _gate(self, record, "BOUNDARY_APPLICATION_INTEGRATION_TRUE")

    def test_evidence_persisted_claim_without_manifest(self):
        record = self._mut()
        record["retained_artifacts"]["evidence_persisted"] = True
        _gate(self, record, "RETAINED_CLAIM_WITHOUT_MANIFEST")
        record = self._mut()
        record["retained_artifacts"]["screenshots_retained"] = True
        _gate(self, record, "RETAINED_CLAIM_WITHOUT_MANIFEST")

    def test_production_verified_true(self):
        record = self._mut()
        record["boundaries"]["production_verified"] = True
        _gate(self, record, "BOUNDARY_PRODUCTION_TRUE")

    def test_m8_defined(self):
        record = self._mut()
        record["boundaries"]["m8"] = "defined"
        _gate(self, record, "BOUNDARY_M8_DEFINED")

    def test_gateway_called_application_integration(self):
        record = self._mut()
        record["boundaries"]["gateway_classification"] = \
            "application_integration"
        _gate(self, record, "BOUNDARY_GATEWAY_CLASSIFICATION")

    def test_seed_called_producer_integration(self):
        record = self._mut()
        record["boundaries"]["dynamic_update_source"] = \
            "revision_producer_integration"
        _gate(self, record, "BOUNDARY_DYNAMIC_UPDATE_SOURCE")

    def test_seed_semantics_rewritten_as_producer(self):
        record = self._mut()
        record["semantics"]["verification_seed_note"] = \
            "the update arrived via producer integration"
        _gate(self, record, "SEMANTICS_MISSING_SEED_NOTE")

    def test_artifact_note_removed(self):
        record = self._mut()
        record["semantics"]["artifacts_note"] = "artifacts kept"
        _gate(self, record, "SEMANTICS_MISSING_ARTIFACT_NOTE")

    def test_bad_schema_version_and_kind(self):
        _gate(self, self._mut(schema_version="v0"), "ATTESTATION_SCHEMA_VERSION")
        _gate(self, self._mut(verification_kind="production_e2e"),
              "ATTESTATION_KIND")

    def test_bad_verified_at(self):
        _gate(self, self._mut(verified_at="2026-08-16"),
              "ATTESTATION_BAD_VERIFIED_AT")
        _gate(self, self._mut(verified_at="not-a-time"),
              "ATTESTATION_BAD_VERIFIED_AT")

    def test_not_a_dict(self):
        _gate(self, [self.base], "ATTESTATION_NOT_DICT")


if __name__ == "__main__":
    unittest.main()
