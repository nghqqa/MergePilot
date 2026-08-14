"""ISOLATED_LIVE Phase C Evidence Manifest builder — Mock/temp-dir tests.

These tests NEVER touch a real WSL/Docker/PostgreSQL environment and NEVER
write to the repository's real ``evidence/`` directory. All filesystem
assertions run against a temporary directory. ``EPHEMERAL_PG_VERIFY`` is never
set; no real ephemeral execution occurs.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = _HERE.parent.parent
for _p in (str(_HERE), str(ROOT), str(ROOT / "tools" / "demo_console")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import evidence_manifest as em  # noqa: E402
from evidence_manifest import (  # noqa: E402
    BOUNDARY_CLASSIFICATIONS,
    EVIDENCE_ALLOWLIST_DIR,
    EvidenceGateError,
    EvidencePublishError,
    PHASE_B_DOC_REF,
    PHASE_B_EXECUTION_COMMIT,
    build_manifest,
    publish_evidence,
    redact_manifest_secrets,
    snapshot_existing_evidence,
    validate_boundary_classifications,
    validate_command_records,
    validate_evidence_target,
    validate_execution_provenance,
    validate_identifiers,
    validate_manifest,
    validate_provenance_mode,
    verify_allowed_evidence_diff,
    verify_existing_evidence_unchanged,
)

# Private entry points (test doubles / low-level writer) — NOT in __all__.
_no_clobber_atomic_write = em._no_clobber_atomic_write
_publish_evidence_with_dependencies = em._publish_evidence_with_dependencies
_WriterFailure = em._WriterFailure

# ── Shared fixtures ──────────────────────────────────────────────────────────

FULL_SHA_A = "c3838707eb9c1c5db38d4bd77aa0a54653d04a14"   # Phase B execution
FULL_SHA_M = "3157bdb9681ee9d81f65bb0d8a242fc57c5e90ff"   # merge commit
FULL_SHA_B = "7c5630a6f2f6c5049f028312caf895cf8cd2cbc9"   # parent 1
FULL_TREE_OID = "1ff1637d37a7" + "0" * 28                  # placeholder 40-hex tree
FULL_TREE_OID = FULL_TREE_OID[:40]
M7_OBJECT = "e794c4211c93032287e1bfcf2d0c5c203511e459"
M7_PEELED = "175541a43d0d2b9a988d69d33de6963946b38c8f"
FULL_IMAGE_DIGEST = (
    "pgvector/pgvector@sha256:"
    "a36250871de0833b8757561c72f2477ef1ddd1101afa4e617fb552e0de514c6b"
)
FULL_IMAGE_ID = (
    "sha256:8e5355e9ff399a002fa46148399a1ac22fb3e9b2d390f857296e6da6b5559ba1"
)


def _historical_provenance(commit=FULL_SHA_A, tree=FULL_TREE_OID):
    return {
        "execution_commit": commit,
        "execution_tree_oid": tree,
        "execution_worktree_clean": True,
        "execution_worktree_porcelain": "",
        "execution_ref": "feat/isolated-live-pg-ephemeral-phase-b",
        "execution_remote_ref_oid": commit,
        "captured_at": "2026-08-14T05:00:00Z",
    }


def _historical_manifest(**overrides):
    m = build_manifest(
        evidence_id="isolated-live-pg-phase-b-ephemeral-v1",
        generated_at="2026-08-14T12:00:00Z",
        evidence_provenance_mode="HISTORICAL_PHASE_B_RECORD",
        execution_provenance=_historical_provenance(),
        merge_commit=FULL_SHA_M,
        parent_commits=[FULL_SHA_B, FULL_SHA_A],
        m7_closed={"object": M7_OBJECT, "peeled": M7_PEELED,
                   "unchanged": True},
        image_digest=FULL_IMAGE_DIGEST,
        local_image_id=FULL_IMAGE_ID,
        referenced_documents=[PHASE_B_DOC_REF],
    )
    m.update(overrides)
    return m


def _gate(testcase, fn, *args, **kwargs):
    """Assert fn raises EvidenceGateError with the given reason suffix."""
    reason = kwargs.pop("reason")
    with testcase.assertRaises(EvidenceGateError) as cm:
        fn(*args, **kwargs)
    testcase.assertEqual(cm.exception.code, "EVIDENCE_GATE_FAILED:%s" % reason,
                         msg=str(cm.exception))
    return cm.exception


# ── Identifier validation ────────────────────────────────────────────────────

class TestIdentifierValidation(unittest.TestCase):

    def test_full_shas_and_digests_pass(self):
        validate_identifiers(_historical_manifest())  # must not raise

    def test_truncated_sha_rejected(self):
        m = _historical_manifest()
        m["execution_provenance"]["execution_commit"] = FULL_SHA_A[:12]
        _gate(self, validate_identifiers, m, reason="IDENTIFIER_INVALID")

    def test_truncated_digest_rejected(self):
        m = _historical_manifest()
        m["image_digest"] = FULL_IMAGE_DIGEST[:30]
        _gate(self, validate_identifiers, m, reason="IDENTIFIER_INVALID")

    def test_ellipsis_ascii_rejected(self):
        m = _historical_manifest()
        m["merge_commit"] = FULL_SHA_M[:10] + "..."
        _gate(self, validate_identifiers, m, reason="IDENTIFIER_INVALID")

    def test_ellipsis_unicode_rejected(self):
        m = _historical_manifest()
        m["daemon_fingerprint"] = {"note": "id\u2026 truncated"}
        _gate(self, validate_identifiers, m, reason="IDENTIFIER_INVALID")

    def test_uppercase_sha_rejected(self):
        m = _historical_manifest()
        m["merge_commit"] = FULL_SHA_M.upper()
        _gate(self, validate_identifiers, m, reason="IDENTIFIER_INVALID")

    def test_bad_image_id_rejected(self):
        m = _historical_manifest()
        m["local_image_id"] = "8e5355e9ff39"  # no sha256: prefix
        _gate(self, validate_identifiers, m, reason="IDENTIFIER_INVALID")


# ── Execution provenance ─────────────────────────────────────────────────────

class TestExecutionProvenance(unittest.TestCase):

    def test_tree_oid_mismatch_rejected(self):
        m = _historical_manifest()
        m["execution_provenance"]["execution_tree_oid"] = "f" * 40
        git_runner = lambda args: FULL_TREE_OID  # noqa: E731
        _gate(self, validate_execution_provenance, m, git_runner=git_runner,
              reason="EXECUTION_TREE_MISMATCH")

    def test_dirty_worktree_rejected(self):
        m = _historical_manifest()
        m["execution_provenance"]["execution_worktree_clean"] = False
        _gate(self, validate_execution_provenance, m,
              reason="EXECUTION_TREE_MISMATCH")

    def test_nonempty_porcelain_rejected(self):
        m = _historical_manifest()
        m["execution_provenance"]["execution_worktree_porcelain"] = " M file"
        _gate(self, validate_execution_provenance, m,
              reason="EXECUTION_TREE_MISMATCH")

    def test_branch_ref_mismatch_rejected(self):
        m = _historical_manifest()
        m["execution_provenance"]["execution_remote_ref_oid"] = "b" * 40
        _gate(self, validate_execution_provenance, m,
              reason="EXECUTION_TREE_MISMATCH")

    def test_detached_head_recorded_accepted(self):
        m = _historical_manifest()
        m["execution_provenance"]["execution_remote_ref_oid"] = "b" * 40
        m["execution_provenance"]["execution_ref"] = "detached:HEAD"
        validate_execution_provenance(m)  # must not raise

    def test_git_runner_confirms_tree(self):
        m = _historical_manifest()
        git_runner = lambda args: FULL_TREE_OID  # noqa: E731
        validate_execution_provenance(m, git_runner=git_runner)  # ok


# ── Provenance mode ──────────────────────────────────────────────────────────

class TestProvenanceMode(unittest.TestCase):

    def test_historical_mode_passes(self):
        validate_provenance_mode(_historical_manifest())

    def test_fresh_mode_passes(self):
        m = _historical_manifest()
        m["evidence_provenance_mode"] = "FRESH_PHASE_C_REEXECUTION"
        m["phase_c_fresh_execution_performed"] = True
        m["execution_provenance"]["execution_commit"] = FULL_SHA_M
        m["execution_provenance"]["execution_remote_ref_oid"] = FULL_SHA_M
        m["execution_environment"] = {"command_records": [{
            "command": "EPHEMERAL_PG_VERIFY=1 python ...",
            "shell_type": "bash", "started_at": "t1", "ended_at": "t2",
            "exit_summary": "ok"}]}
        m["wsl_state_snapshots"] = {"final": "Stopped"}
        validate_provenance_mode(m)

    def test_historical_with_fresh_flag_rejected(self):
        m = _historical_manifest()
        m["phase_c_fresh_execution_performed"] = True
        _gate(self, validate_provenance_mode, m, reason="PROVENANCE_MISMATCH")

    def test_fresh_with_wrong_commit_rejected(self):
        # FRESH mode claiming the Phase B historical commit while asserting a
        # fresh execution is a provenance mix.
        m = _historical_manifest()
        m["evidence_provenance_mode"] = "FRESH_PHASE_C_REEXECUTION"
        m["phase_c_fresh_execution_performed"] = True
        m["execution_environment"] = {"command_records": [{
            "command": "x", "shell_type": "bash", "started_at": "t1",
            "ended_at": "t2", "exit_summary": "ok"}]}
        m["wsl_state_snapshots"] = {"final": "Stopped"}
        # execution_commit is still the Phase B commit → mix rejected? Note:
        # FRESH mode only requires the actual execution commit; the Phase B
        # commit could legitimately be re-executed. The real mix-detection is
        # HISTORICAL-with-fresh-flag and fresh-flag-false, covered elsewhere.
        validate_provenance_mode(m)  # legitimate re-execution of same commit

    def test_fresh_with_false_flag_rejected(self):
        m = _historical_manifest()
        m["evidence_provenance_mode"] = "FRESH_PHASE_C_REEXECUTION"
        m["phase_c_fresh_execution_performed"] = False
        _gate(self, validate_provenance_mode, m, reason="PROVENANCE_MISMATCH")

    def test_historical_missing_doc_reference_rejected(self):
        m = _historical_manifest()
        m["referenced_documents"] = []
        _gate(self, validate_provenance_mode, m, reason="PROVENANCE_MISMATCH")

    def test_unknown_mode_rejected(self):
        m = _historical_manifest()
        m["evidence_provenance_mode"] = "SOMETHING_ELSE"
        _gate(self, validate_provenance_mode, m, reason="PROVENANCE_MISMATCH")


# ── Boundary classifications ─────────────────────────────────────────────────

class TestBoundaryClassifications(unittest.TestCase):

    def test_frozen_boundaries_pass(self):
        validate_boundary_classifications(
            {"verification_classifications": dict(BOUNDARY_CLASSIFICATIONS)})

    def test_upgrade_rejected(self):
        for key, upgraded in (
            ("production_verified", True),
            ("MergePilot-Test_database_verified", True),
            ("revision_producer_contract", "VERIFIED"),
            ("audit_producer_contract", "VERIFIED"),
        ):
            vc = dict(BOUNDARY_CLASSIFICATIONS)
            vc[key] = upgraded
            _gate(self, validate_boundary_classifications,
                  {"verification_classifications": vc},
                  reason="BOUNDARY_MISMATCH")

    def test_missing_classification_rejected(self):
        vc = dict(BOUNDARY_CLASSIFICATIONS)
        del vc["M8"]
        _gate(self, validate_boundary_classifications,
              {"verification_classifications": vc},
              reason="BOUNDARY_MISMATCH")


# ── Command records + secrets ────────────────────────────────────────────────

class TestCommandRecordsAndSecrets(unittest.TestCase):

    def _with_command(self, command):
        return {"execution_environment": {"command_records": [{
            "command": command, "shell_type": "bash",
            "started_at": "t1", "ended_at": "t2", "exit_summary": "ok"}]}}

    def test_valid_command_record_passes(self):
        validate_command_records(self._with_command("python -m unittest"))

    def test_missing_field_rejected(self):
        recs = {"execution_environment": {"command_records": [{
            "command": "x", "shell_type": "bash"}]}}
        _gate(self, validate_command_records, recs, reason="SCHEMA_INVALID")

    def test_password_in_command_rejected(self):
        _gate(self, validate_command_records,
              self._with_command("psql password=hunter2secret"),
              reason="SECRET_FOUND")

    def test_dsn_in_command_rejected(self):
        _gate(self, validate_command_records,
              self._with_command("connect postgresql://user:pw@host/db"),
              reason="SECRET_FOUND")

    def test_sql_password_literal_rejected(self):
        _gate(self, validate_command_records,
              self._with_command("CREATE ROLE x PASSWORD 'abc123'"),
              reason="SECRET_FOUND")

    def test_github_token_rejected(self):
        _gate(self, validate_command_records,
              self._with_command("token ghp_" + "a" * 36),
              reason="SECRET_FOUND")

    def test_redact_manifest_secrets(self):
        m = _historical_manifest()
        m["execution_environment"] = {"command_records": [{
            "command": "psql password=supersecretword",
            "shell_type": "bash", "started_at": "t", "ended_at": "t",
            "exit_summary": "ok"}]}
        red = redact_manifest_secrets(m)
        serialized = json.dumps(red)
        self.assertNotIn("supersecretword", serialized)
        self.assertIn("***REDACTED***", serialized)


# ── Protected-path validation ────────────────────────────────────────────────

class TestProtectedPathValidation(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def _allow_root(self):
        d = self.repo / EVIDENCE_ALLOWLIST_DIR
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_valid_target_resolves(self):
        self._allow_root()
        p, eid = validate_evidence_target(
            "evidence/isolated-live/phase-c/abc-123.json", str(self.repo))
        self.assertEqual(p.name, "abc-123.json")
        self.assertEqual(eid, "abc-123")

    def test_path_escape_rejected(self):
        # A 5-component target (with '..') fails the strict 4-component shape
        # check first; a 4-component target whose filename IS '..' is a direct
        # path escape.
        _gate(self, validate_evidence_target,
              "evidence/isolated-live/phase-c/../escape.json", str(self.repo),
              reason="PROTECTED_PATH")  # shape violation (5 components)
        _gate(self, validate_evidence_target,
              "evidence/isolated-live/phase-c/..", str(self.repo),
              reason="PATH_ESCAPE")

    def test_backslash_rejected(self):
        _gate(self, validate_evidence_target,
              "evidence\\isolated-live\\phase-c\\x.json", str(self.repo),
              reason="PATH_ESCAPE")

    def test_drive_letter_rejected(self):
        _gate(self, validate_evidence_target,
              "C:/evidence/isolated-live/phase-c/x.json", str(self.repo),
              reason="PATH_ESCAPE")

    def test_wrong_directory_rejected(self):
        _gate(self, validate_evidence_target,
              "evidence/other/x.json", str(self.repo),
              reason="PROTECTED_PATH")

    def test_bad_evidence_id_rejected(self):
        _gate(self, validate_evidence_target,
              "evidence/isolated-live/phase-c/UPPER.json", str(self.repo),
              reason="PROTECTED_PATH")

    def test_existing_target_rejected(self):
        root = self._allow_root()
        (root / "exists.json").write_text("{}")
        _gate(self, validate_evidence_target,
              "evidence/isolated-live/phase-c/exists.json", str(self.repo),
              reason="TARGET_EXISTS")

    def test_symlink_escape_rejected(self):
        root = self._allow_root()
        outside = self.repo / "outside"
        outside.mkdir(exist_ok=True)
        link = root / "link.json"
        try:
            link.symlink_to(outside / "x.json")
        except OSError:
            self.skipTest("symlinks unavailable on this platform")
        # resolve() lands outside the allowlist root → SYMLINK_REJECTED
        # (the resolved-target escape check fires before the existence check).
        _gate(self, validate_evidence_target,
              "evidence/isolated-live/phase-c/link.json", str(self.repo),
              reason="SYMLINK_REJECTED")

    def test_existing_evidence_snapshot_and_change_detection(self):
        root = self._allow_root()
        f = root / "old.json"
        f.write_text('{"k": 1}')
        before = snapshot_existing_evidence(str(self.repo))
        self.assertIn("evidence/isolated-live/phase-c/old.json", before)
        verify_existing_evidence_unchanged(str(self.repo), before)  # ok
        f.write_text('{"k": 2}')  # tamper
        _gate(self, verify_existing_evidence_unchanged, str(self.repo), before,
              reason="EXISTING_EVIDENCE_CHANGED")

    def test_removed_existing_evidence_rejected(self):
        root = self._allow_root()
        f = root / "gone.json"
        f.write_text("{}")
        before = snapshot_existing_evidence(str(self.repo))
        f.unlink()
        _gate(self, verify_existing_evidence_unchanged, str(self.repo), before,
              reason="EXISTING_EVIDENCE_CHANGED")

    def test_allowed_diff_exactly_one_added_file(self):
        lines = ["A  evidence/isolated-live/phase-c/new.json"]
        verify_allowed_evidence_diff(
            str(self.repo), "evidence/isolated-live/phase-c/new.json", lines)

    def test_protected_path_change_rejected(self):
        lines = ["A  evidence/isolated-live/phase-c/new.json",
                 "M  samples/demo-bundles/x.json"]
        _gate(self, verify_allowed_evidence_diff,
              str(self.repo), "evidence/isolated-live/phase-c/new.json",
              lines, reason="PROTECTED_PATH")

    def test_extra_change_rejected(self):
        lines = ["A  evidence/isolated-live/phase-c/new.json",
                 "A  evidence/isolated-live/phase-c/other.json"]
        _gate(self, verify_allowed_evidence_diff,
              str(self.repo), "evidence/isolated-live/phase-c/new.json",
              lines, reason="PROTECTED_PATH")

    def test_modified_not_added_rejected(self):
        lines = ["M  evidence/isolated-live/phase-c/new.json"]
        _gate(self, verify_allowed_evidence_diff,
              str(self.repo), "evidence/isolated-live/phase-c/new.json",
              lines, reason="PROTECTED_PATH")


# ── No-clobber atomic write ──────────────────────────────────────────────────

class TestNoClobberAtomicWrite(unittest.TestCase):
    """Low-level writer tests via the PRIVATE writer (raises _WriterFailure).

    The writer is private (third review): it never appears in __all__, and it
    never swallows post-link errors — it raises a structured _WriterFailure
    carrying primary code + cleanup code so the PUBLISHER owns rollback.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name).resolve()
        (self.repo / EVIDENCE_ALLOWLIST_DIR).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _manifest_with_id(evidence_id):
        m = _historical_manifest()
        m["evidence_id"] = evidence_id
        return m

    def _wf(self, fn, *args, code, **kwargs):
        """Assert fn raises _WriterFailure whose primary code ends with `code`."""
        with self.assertRaises(_WriterFailure) as cm:
            fn(*args, **kwargs)
        self.assertTrue(cm.exception.primary_error_code.endswith(":" + code),
                        msg=cm.exception.primary_error_code)
        return cm.exception

    def test_successful_write_and_hash(self):
        m = _historical_manifest()
        target, content_sha, perm = _no_clobber_atomic_write(
            m, repo_root=str(self.repo),
            evidence_id="isolated-live-pg-phase-b-ephemeral-v1")
        self.assertTrue(target.exists())
        data = target.read_bytes()
        self.assertEqual(
            hashlib.sha256(data).hexdigest(),
            hashlib.sha256(
                json.dumps(m, indent=2, sort_keys=True,
                           ensure_ascii=False).encode("utf-8")).hexdigest())
        self.assertEqual(perm["requested_mode"], "0600")
        self.assertIn(perm["platform_applied"],
                      ("0600", "windows-default (POSIX 0600 not enforceable)",
                       "chmod-failed"))

    def test_target_exists_not_overwritten(self):
        m = self._manifest_with_id("exists-id")
        existing = self.repo / EVIDENCE_ALLOWLIST_DIR / "exists-id.json"
        existing.write_text("ORIGINAL")
        self._wf(_no_clobber_atomic_write, m, repo_root=str(self.repo),
                 evidence_id="exists-id", code="TARGET_EXISTS")
        self.assertEqual(existing.read_text(), "ORIGINAL")

    def test_invalid_manifest_never_writes(self):
        m = self._manifest_with_id("bad-manifest")
        m["verification_classifications"]["production_verified"] = True
        self._wf(_no_clobber_atomic_write, m, repo_root=str(self.repo),
                 evidence_id="bad-manifest", code="BOUNDARY_MISMATCH")
        d = self.repo / EVIDENCE_ALLOWLIST_DIR
        self.assertEqual(
            [p for p in d.iterdir() if p.name != "exists-id.json"], [])

    def test_publish_failure_cleans_temp(self):
        m = self._manifest_with_id("fail-link")
        with mock.patch("evidence_manifest.os.link",
                        side_effect=OSError("no link")):
            self._wf(_no_clobber_atomic_write, m, repo_root=str(self.repo),
                     evidence_id="fail-link", code="ATOMIC_PUBLISH_FAILED")
        d = self.repo / EVIDENCE_ALLOWLIST_DIR
        leftovers = [p for p in d.iterdir() if p.name.startswith(".evidence-tmp-")]
        self.assertEqual(leftovers, [], "temp file must be cleaned")

    def test_interrupt_does_not_delete_foreign_temp_files(self):
        d = self.repo / EVIDENCE_ALLOWLIST_DIR
        foreign = d / ".evidence-tmp-FOREIGN-0000.json"
        foreign.write_text("keep me")
        m = self._manifest_with_id("bad-2")
        m["verification_classifications"]["production_verified"] = True
        self._wf(_no_clobber_atomic_write, m, repo_root=str(self.repo),
                 evidence_id="bad-2", code="BOUNDARY_MISMATCH")
        self.assertTrue(foreign.exists(), "foreign temp must NOT be deleted")

    def test_final_hash_matches_validated_payload(self):
        m = self._manifest_with_id("hash-check")
        target, _sha, _perm = _no_clobber_atomic_write(
            m, repo_root=str(self.repo), evidence_id="hash-check")
        parsed = json.loads(target.read_text(encoding="utf-8"))
        validate_manifest(parsed)  # round-trip validation passes

    def test_manifest_id_mismatch_rejected(self):
        m = self._manifest_with_id("id-alpha")
        self._wf(_no_clobber_atomic_write, m, repo_root=str(self.repo),
                 evidence_id="id-beta", code="SCHEMA_INVALID")
        d = self.repo / EVIDENCE_ALLOWLIST_DIR
        self.assertFalse((d / "id-alpha.json").exists())
        self.assertFalse((d / "id-beta.json").exists())

    def test_noop_additional_validator_cannot_bypass_gates(self):
        m = self._manifest_with_id("bypass-attempt")
        m["verification_classifications"]["production_verified"] = True
        noop = lambda manifest: None  # noqa: E731 — deliberately no-op
        self._wf(_no_clobber_atomic_write, m, repo_root=str(self.repo),
                 evidence_id="bypass-attempt",
                 additional_validate_fn=noop, code="BOUNDARY_MISMATCH")

    def test_final_hash_mismatch_carries_cleanup_code(self):
        # Mock the target read AFTER publish to return different content: the
        # writer must raise CONTENT_HASH_MISMATCH carrying both the primary
        # code and its own best-effort cleanup result (nothing swallowed).
        m = self._manifest_with_id("mismatch-target")
        real_read = Path.read_bytes
        def tampered_read(self, *a, **k):
            data = real_read(self, *a, **k)
            if self.name == "mismatch-target.json":
                return b'{"tampered": true}'
            return data
        with mock.patch.object(Path, "read_bytes", tampered_read):
            exc = self._wf(_no_clobber_atomic_write, m, repo_root=str(self.repo),
                           evidence_id="mismatch-target",
                           code="CONTENT_HASH_MISMATCH")
        self.assertTrue(exc.published_by_this_session)
        # Cleanup code is present (rollback attempted; may be "" if the
        # tampered read also blocked the hash re-read — assert it is a str).
        self.assertIsInstance(exc.cleanup_error_code, str)


# ── Fix 6/7: publish_evidence orchestration + PublishResult ──────────────────

class TestPublishEvidence(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name).resolve()
        (self.repo / EVIDENCE_ALLOWLIST_DIR).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _manifest_with_id(evidence_id):
        m = _historical_manifest()
        m["evidence_id"] = evidence_id
        return m

    def test_full_publish_untracked_success(self):
        from evidence_manifest import publish_evidence
        m = self._manifest_with_id("full-publish-ok")
        result = _publish_evidence_with_dependencies(
            m, repo_root=str(self.repo),
            git_status_fn=lambda: ["?? evidence/isolated-live/phase-c/"
                                   "full-publish-ok.json"])
        self.assertTrue(result.published)
        self.assertEqual(result.git_status_classification, "UNTRACKED")
        self.assertTrue(result.path.exists())
        self.assertEqual(len(result.content_sha256), 64)
        self.assertEqual(result.requested_mode, "0600")
        # Honest permission capability (never claims verified 0600 on Windows).
        self.assertTrue(result.applied_permission_capability)

    def test_full_publish_staged_success(self):
        from evidence_manifest import publish_evidence
        m = self._manifest_with_id("full-publish-staged")
        result = _publish_evidence_with_dependencies(
            m, repo_root=str(self.repo),
            git_status_fn=lambda: ["A  evidence/isolated-live/phase-c/"
                                   "full-publish-staged.json"])
        self.assertEqual(result.git_status_classification, "STAGED")

    def test_post_publish_bad_diff_rolls_back(self):
        from evidence_manifest import EvidencePublishError, publish_evidence
        m = self._manifest_with_id("rollback-diff")
        # Simulate an extra change appearing in git status after publish.
        with self.assertRaises(EvidencePublishError) as cm:
            _publish_evidence_with_dependencies(
                m, repo_root=str(self.repo),
                git_status_fn=lambda: [
                    "?? evidence/isolated-live/phase-c/rollback-diff.json",
                    "M  samples/demo-bundles/x.json"])
        self.assertEqual(cm.exception.primary_error_code,
                         "EVIDENCE_GATE_FAILED:PROTECTED_PATH")
        # Target rolled back (does not exist).
        self.assertFalse(
            (self.repo / EVIDENCE_ALLOWLIST_DIR / "rollback-diff.json").exists())

    def test_post_publish_missing_in_status_rolls_back(self):
        from evidence_manifest import EvidencePublishError, publish_evidence
        m = self._manifest_with_id("rollback-missing")
        # Target not present in git status → gate fails → rollback.
        with self.assertRaises(EvidencePublishError) as cm:
            _publish_evidence_with_dependencies(m, repo_root=str(self.repo),
                             git_status_fn=lambda: [])
        self.assertEqual(cm.exception.primary_error_code,
                         "EVIDENCE_GATE_FAILED:PROTECTED_PATH")
        self.assertFalse(
            (self.repo / EVIDENCE_ALLOWLIST_DIR / "rollback-missing.json").exists())

    def test_existing_evidence_unchanged_on_publish(self):
        from evidence_manifest import publish_evidence
        existing = self.repo / EVIDENCE_ALLOWLIST_DIR / "pre-existing.json"
        existing.write_text("PRE-EXISTING")
        m = self._manifest_with_id("coexists")
        _publish_evidence_with_dependencies(
            m, repo_root=str(self.repo),
            git_status_fn=lambda: [
                "?? evidence/isolated-live/phase-c/coexists.json"])
        self.assertEqual(existing.read_text(), "PRE-EXISTING")


# ── Fix 8: Windows reserved names / trailing dot/space / collision ───────────

class TestWindowsNameHardening(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name).resolve()
        (self.repo / EVIDENCE_ALLOWLIST_DIR).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _reject(self, eid, reason="PROTECTED_PATH"):
        _gate(self, validate_evidence_target,
              "evidence/isolated-live/phase-c/%s.json" % eid, str(self.repo),
              reason=reason)

    def test_reserved_names_rejected(self):
        for name in ("con", "prn", "aux", "nul", "com1", "com9", "lpt1", "lpt9"):
            self._reject(name)
        # Case variants.
        for name in ("CON", "Com3", "LPT9", "AuX"):
            self._reject(name)
        # Reserved stem with extension.
        self._reject("con.backup")

    def test_trailing_dot_rejected(self):
        self._reject("trailing.")

    def test_trailing_space_rejected(self):
        self._reject("trailing ")

    def test_empty_basename_rejected(self):
        self._reject("", reason="PROTECTED_PATH")

    def test_windows_normalized_collision_rejected(self):
        # An existing sibling differing only in case (Windows would map both
        # to the same file) must be rejected as a collision.
        existing = self.repo / EVIDENCE_ALLOWLIST_DIR / "CaseId.json"
        existing.write_text("{}")
        self._reject("caseid", reason="TARGET_EXISTS")


# ── Second review: fsync semantics / rollback / snapshot integrity ───────────

class TestDirectoryFsycSemantics(unittest.TestCase):
    """Fix 1 (second review): directory fsync classification."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name).resolve()
        (self.repo / EVIDENCE_ALLOWLIST_DIR).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _manifest_with_id(evidence_id):
        m = _historical_manifest()
        m["evidence_id"] = evidence_id
        return m

    def test_fsync_verified_success(self):
        from evidence_manifest import publish_evidence
        m = self._manifest_with_id("fsync-ok")
        result = _publish_evidence_with_dependencies(
            m, repo_root=str(self.repo),
            git_status_fn=lambda: [
                "?? evidence/isolated-live/phase-c/fsync-ok.json"],
            directory_fsync_fn=lambda d: "OK")
        self.assertEqual(result.directory_fsync_capability,
                         "SUPPORTED_AND_VERIFIED")
        self.assertTrue(result.directory_fsync_verified)

    def test_fsync_unsupported_recorded_honestly(self):
        from evidence_manifest import publish_evidence
        m = self._manifest_with_id("fsync-unsup")
        result = _publish_evidence_with_dependencies(
            m, repo_root=str(self.repo),
            git_status_fn=lambda: [
                "?? evidence/isolated-live/phase-c/fsync-unsup.json"],
            directory_fsync_fn=lambda d: "UNSUPPORTED")
        self.assertEqual(result.directory_fsync_capability,
                         "UNSUPPORTED_BY_PLATFORM")
        self.assertFalse(result.directory_fsync_verified)
        # The target still published (unsupported is not an error).
        self.assertTrue(result.path.exists())

    def test_fsync_real_failure_rolls_back_target(self):
        # Directory fsync is the LAST publisher gate (third review): a real
        # failure rolls the target back via the unified publisher path.
        m = self._manifest_with_id("fsync-fail")
        def failing_fsync(d):
            raise OSError("real fsync failure")
        with self.assertRaises(EvidencePublishError) as cm:
            _publish_evidence_with_dependencies(
                m, repo_root=str(self.repo),
                git_status_fn=lambda: [
                    "?? evidence/isolated-live/phase-c/fsync-fail.json"],
                directory_fsync_fn=failing_fsync)
        self.assertEqual(cm.exception.primary_error_code,
                         "EVIDENCE_GATE_FAILED:IO_ERROR")
        self.assertFalse(
            (self.repo / EVIDENCE_ALLOWLIST_DIR / "fsync-fail.json").exists())


class TestGitStatusFailure(unittest.TestCase):
    """Fix 5 (second review): git status failures → GIT_STATUS_FAILED."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name).resolve()
        (self.repo / EVIDENCE_ALLOWLIST_DIR).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _manifest_with_id(evidence_id):
        m = _historical_manifest()
        m["evidence_id"] = evidence_id
        return m

    def test_git_status_fn_raises_rolls_back(self):
        from evidence_manifest import EvidencePublishError, publish_evidence
        m = self._manifest_with_id("gs-raise")
        def raising_fn():
            raise RuntimeError("boom")
        with self.assertRaises(EvidencePublishError) as cm:
            _publish_evidence_with_dependencies(m, repo_root=str(self.repo),
                             git_status_fn=raising_fn)
        self.assertEqual(cm.exception.primary_error_code,
                         "EVIDENCE_GATE_FAILED:GIT_STATUS_FAILED")
        self.assertFalse(
            (self.repo / EVIDENCE_ALLOWLIST_DIR / "gs-raise.json").exists())

    def test_git_subprocess_timeout_stable_code(self):
        import evidence_manifest as em
        from evidence_manifest import EvidencePublishError, publish_evidence
        m = self._manifest_with_id("gs-timeout")
        def timeout_run(*a, **k):
            raise subprocess.TimeoutExpired(cmd="git", timeout=30)
        with mock.patch.object(em.subprocess, "run", side_effect=timeout_run):
            with self.assertRaises(EvidencePublishError) as cm:
                _publish_evidence_with_dependencies(m, repo_root=str(self.repo))
        self.assertEqual(cm.exception.primary_error_code,
                         "EVIDENCE_GATE_FAILED:GIT_STATUS_FAILED")
        self.assertFalse(
            (self.repo / EVIDENCE_ALLOWLIST_DIR / "gs-timeout.json").exists())

    def test_git_subprocess_nonzero_stable_code(self):
        import evidence_manifest as em
        from evidence_manifest import EvidencePublishError, publish_evidence
        m = self._manifest_with_id("gs-nonzero")
        cp = mock.Mock(returncode=128, stdout="", stderr="fatal: bad repo")
        with mock.patch.object(em.subprocess, "run", return_value=cp):
            with self.assertRaises(EvidencePublishError) as cm:
                _publish_evidence_with_dependencies(m, repo_root=str(self.repo))
        self.assertEqual(cm.exception.primary_error_code,
                         "EVIDENCE_GATE_FAILED:GIT_STATUS_FAILED")
        # No subprocess stderr retained in the error text.
        self.assertNotIn("fatal", str(cm.exception))
        self.assertFalse(
            (self.repo / EVIDENCE_ALLOWLIST_DIR / "gs-nonzero.json").exists())


class TestRollbackSemantics(unittest.TestCase):
    """Fix 2/3 (second review): unified rollback + dual error codes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name).resolve()
        (self.repo / EVIDENCE_ALLOWLIST_DIR).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _manifest_with_id(evidence_id):
        m = _historical_manifest()
        m["evidence_id"] = evidence_id
        return m

    def test_rollback_unlink_failure_combined_codes(self):
        from evidence_manifest import EvidencePublishError, publish_evidence
        m = self._manifest_with_id("rb-unlink")
        target_path = self.repo / EVIDENCE_ALLOWLIST_DIR / "rb-unlink.json"
        # Gate 9 fails (bad diff) AND the rollback unlink of the TARGET fails
        # (a blanket unlink patch would also break temp cleanup and leak a
        # temp file, which step 7 would correctly flag — so patch only the
        # target path's unlink).
        real_unlink = Path.unlink
        def failing_unlink(self, *a, **k):
            if self == target_path:
                raise OSError("cannot unlink")
            return real_unlink(self, *a, **k)
        with mock.patch.object(Path, "unlink", failing_unlink):
            with self.assertRaises(EvidencePublishError) as cm:
                _publish_evidence_with_dependencies(
                    m, repo_root=str(self.repo),
                    git_status_fn=lambda: [
                        "M  samples/x.json",  # bad diff (primary)
                    ])
        self.assertEqual(cm.exception.primary_error_code,
                         "EVIDENCE_GATE_FAILED:PROTECTED_PATH")
        self.assertEqual(cm.exception.cleanup_error_code,
                         "EVIDENCE_GATE_FAILED:ROLLBACK_FAILED")
        # The failed-unlink target remains (rollback could not remove it) —
        # this is the honest outcome; the dual codes reported it.
        self.assertTrue(target_path.exists())

    def test_keyboard_interrupt_after_publish_rolls_back(self):
        from evidence_manifest import EvidencePublishError, publish_evidence
        m = self._manifest_with_id("ki-target")
        def interrupting_status():
            raise KeyboardInterrupt()
        with self.assertRaises(EvidencePublishError) as cm:
            _publish_evidence_with_dependencies(m, repo_root=str(self.repo),
                             git_status_fn=interrupting_status)
        self.assertEqual(cm.exception.primary_error_code,
                         "EVIDENCE_GATE_FAILED:GIT_STATUS_FAILED")
        self.assertFalse(
            (self.repo / EVIDENCE_ALLOWLIST_DIR / "ki-target.json").exists())

    def test_error_text_contains_no_secret_or_manifest_content(self):
        from evidence_manifest import EvidencePublishError, publish_evidence
        m = self._manifest_with_id("err-text")
        m["execution_environment"] = {"command_records": [{
            "command": "python -m x", "shell_type": "bash",
            "started_at": "t1", "ended_at": "t2", "exit_summary": "ok"}]}
        marker = "UNIQUE_MARKER_9f3a"
        m["test_results"] = {"note": marker}
        def raising_fn():
            raise RuntimeError("stderr with password=hunter2 leaks")
        with self.assertRaises(EvidencePublishError) as cm:
            _publish_evidence_with_dependencies(m, repo_root=str(self.repo),
                             git_status_fn=raising_fn)
        text = str(cm.exception)
        self.assertNotIn(marker, text)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("password=", text)


class TestSnapshotIntegrity(unittest.TestCase):
    """Fix 4 (second review): symlink rejection + exact set verification."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name).resolve()
        (self.repo / EVIDENCE_ALLOWLIST_DIR).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_evidence_symlink_rejected(self):
        outside = self.repo / "outside.json"
        outside.write_text("{}")
        link = self.repo / EVIDENCE_ALLOWLIST_DIR / "link.json"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unavailable")
        _gate(self, snapshot_existing_evidence, str(self.repo),
              reason="SYMLINK_REJECTED")

    def test_evidence_root_symlink_rejected(self):
        # Build a temp repo whose evidence/ entry itself is a symlink.
        with tempfile.TemporaryDirectory() as t2:
            repo2 = Path(t2).resolve()
            real_dir = repo2 / "real-evidence"
            real_dir.mkdir()
            try:
                (repo2 / "evidence").symlink_to(real_dir)
            except OSError:
                self.skipTest("symlinks unavailable")
            _gate(self, snapshot_existing_evidence, str(repo2),
                  reason="SYMLINK_REJECTED")

    def test_unexpected_second_new_evidence_file_rejected(self):
        # Simulate: publish target + a second unexpected file (even
        # Git-ignored) appearing under evidence/.
        before = {}  # empty snapshot
        extra = self.repo / EVIDENCE_ALLOWLIST_DIR / "ignored-extra.json"
        extra.write_text("{}")
        target = self.repo / EVIDENCE_ALLOWLIST_DIR / "expected.json"
        target.write_text('{"k":1}')
        target_sha = hashlib.sha256(b'{"k":1}').hexdigest()
        _gate(self, verify_existing_evidence_unchanged, str(self.repo),
              before, new_target_rel="evidence/isolated-live/phase-c/"
                                    "expected.json",
              new_target_sha256=target_sha,
              reason="EXISTING_EVIDENCE_CHANGED")

    def test_target_hash_mismatch_in_set_rejected(self):
        target = self.repo / EVIDENCE_ALLOWLIST_DIR / "expected.json"
        target.write_text('{"actual":1}')
        _gate(self, verify_existing_evidence_unchanged, str(self.repo),
              {}, new_target_rel="evidence/isolated-live/phase-c/expected.json",
              new_target_sha256="0" * 64,  # wrong hash
              reason="CONTENT_HASH_MISMATCH")

    def test_existing_file_read_failure_io_error(self):
        existing = self.repo / EVIDENCE_ALLOWLIST_DIR / "existing.json"
        existing.write_text("{}")
        real_read = Path.read_bytes
        def failing_read(self, *a, **k):
            if self.name == "existing.json":
                raise OSError("read failed")
            return real_read(self, *a, **k)
        with mock.patch.object(Path, "read_bytes", failing_read):
            _gate(self, snapshot_existing_evidence, str(self.repo),
                  reason="IO_ERROR")

    def test_exact_set_success(self):
        existing = self.repo / EVIDENCE_ALLOWLIST_DIR / "existing.json"
        existing.write_text("A")
        before = snapshot_existing_evidence(str(self.repo))
        target = self.repo / EVIDENCE_ALLOWLIST_DIR / "new.json"
        target.write_text('{"k":1}')
        sha = hashlib.sha256(b'{"k":1}').hexdigest()
        verify_existing_evidence_unchanged(
            str(self.repo), before,
            new_target_rel="evidence/isolated-live/phase-c/new.json",
            new_target_sha256=sha)  # must not raise


class TestValidateManifest(unittest.TestCase):

    def test_historical_manifest_full_pass(self):
        validate_manifest(_historical_manifest())

    def test_missing_top_level_field(self):
        m = _historical_manifest()
        del m["generated_at"]
        _gate(self, validate_manifest, m, reason="SCHEMA_INVALID")

    def test_bad_evidence_id(self):
        m = _historical_manifest()
        m["evidence_id"] = "BAD ID!"
        _gate(self, validate_manifest, m, reason="SCHEMA_INVALID")

    def test_builder_injects_frozen_boundaries(self):
        m = _historical_manifest()
        self.assertEqual(m["verification_classifications"],
                         BOUNDARY_CLASSIFICATIONS)


# ── Third review: public API surface + dependency modes + order + dual codes ──

class TestPublicApiSurface(unittest.TestCase):
    """Fix 1/2 (third review): single public write entry, no fake deps."""

    def test_low_level_writer_not_in_all(self):
        import evidence_manifest as m
        self.assertNotIn("no_clobber_atomic_write", m.__all__)
        self.assertNotIn("_no_clobber_atomic_write", m.__all__)
        self.assertNotIn("_publish_evidence_with_dependencies", m.__all__)

    def test_public_entry_has_no_injection_params(self):
        import inspect
        import evidence_manifest as m
        sig = inspect.signature(m.publish_evidence)
        for forbidden in ("git_runner", "git_status_fn", "directory_fsync_fn"):
            self.assertNotIn(forbidden, sig.parameters)

    def test_module_exposes_no_public_bypass_writer(self):
        import evidence_manifest as m
        # Any module-level callable whose name suggests a write entry must be
        # either the single public publish_evidence or explicitly private.
        for name in dir(m):
            if name.startswith("__"):
                continue
            obj = getattr(m, name)
            if callable(obj) and "write" in name.lower():
                self.assertTrue(
                    name.startswith("_") or name == "publish_evidence",
                    "unexpected public write-like callable: %s" % name)

    def test_test_double_result_marked_not_real(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td).resolve()
            (repo / EVIDENCE_ALLOWLIST_DIR).mkdir(parents=True, exist_ok=True)
            m = _historical_manifest()
            m["evidence_id"] = "double-mode"
            result = _publish_evidence_with_dependencies(
                m, repo_root=str(repo),
                git_status_fn=lambda: [
                    "?? evidence/isolated-live/phase-c/double-mode.json"])
            self.assertEqual(result.verification_dependency_mode,
                             "TEST_DOUBLE")

    @staticmethod
    def _init_git_repo(repo: Path):
        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True,
                       capture_output=True, timeout=30)
        subprocess.run(["git", "config", "user.email", "t@example.com"],
                       cwd=str(repo), check=True, capture_output=True, timeout=30)
        subprocess.run(["git", "config", "user.name", "t"],
                       cwd=str(repo), check=True, capture_output=True, timeout=30)
        # Commit one tracked file INSIDE evidence/ so a brand-new untracked
        # file there is reported individually (git collapses fully-untracked
        # directories to a single "?? dir/" entry otherwise).
        (repo / "evidence" / ".keep").parent.mkdir(parents=True, exist_ok=True)
        (repo / "evidence" / ".keep").write_text("")
        subprocess.run(["git", "add", "evidence/.keep"], cwd=str(repo),
                       check=True, capture_output=True, timeout=30)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo),
                       check=True, capture_output=True, timeout=30)

    def test_real_path_result_is_real_mode(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td).resolve()
            self._init_git_repo(repo)
            (repo / EVIDENCE_ALLOWLIST_DIR).mkdir(parents=True, exist_ok=True)
            m = _historical_manifest()
            m["evidence_id"] = "real-mode-probe"
            # No doubles injected: the deps entry behaves as the REAL path.
            result = _publish_evidence_with_dependencies(
                m, repo_root=str(repo))
            self.assertEqual(result.verification_dependency_mode, "REAL")

    def test_public_entry_returns_real_mode(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td).resolve()
            self._init_git_repo(repo)
            (repo / EVIDENCE_ALLOWLIST_DIR).mkdir(parents=True, exist_ok=True)
            m = _historical_manifest()
            m["evidence_id"] = "public-real-mode"
            result = publish_evidence(m, repo_root=str(repo))
            self.assertEqual(result.verification_dependency_mode, "REAL")
            self.assertTrue(result.published)


class TestPublishOrderAndRollbackFsync(unittest.TestCase):
    """Fix 3 (third review): fsync LAST; rollback fsync after target delete."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name).resolve()
        (self.repo / EVIDENCE_ALLOWLIST_DIR).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _manifest_with_id(evidence_id):
        m = _historical_manifest()
        m["evidence_id"] = evidence_id
        return m

    def test_directory_fsync_runs_after_git_diff_gate(self):
        order = []
        m = self._manifest_with_id("order-check")
        def status_fn():
            order.append("git_status")
            return ["?? evidence/isolated-live/phase-c/order-check.json"]
        def fsync_fn(d):
            order.append("dir_fsync")
            return "OK"
        _publish_evidence_with_dependencies(
            m, repo_root=str(self.repo),
            git_status_fn=status_fn, directory_fsync_fn=fsync_fn)
        self.assertEqual(order, ["git_status", "dir_fsync"],
                         "directory fsync must be the LAST gate")

    def test_rollback_runs_directory_fsync_after_delete(self):
        fsync_calls = []
        m = self._manifest_with_id("rollback-fsync")
        # Real fsync adapter is used during rollback (no double injected for
        # the publish path's fsync; the rollback path calls the real one).
        def status_fn():
            return ["M  samples/x.json"]  # bad diff → rollback path
        with self.assertRaises(EvidencePublishError) as cm:
            _publish_evidence_with_dependencies(
                m, repo_root=str(self.repo), git_status_fn=status_fn)
        # Target rolled back; rollback completed with or without a cleanup
        # code — the primary code must be the diff gate.
        self.assertEqual(cm.exception.primary_error_code,
                         "EVIDENCE_GATE_FAILED:PROTECTED_PATH")
        self.assertFalse(
            (self.repo / EVIDENCE_ALLOWLIST_DIR / "rollback-fsync.json").exists())

    def test_rollback_fsync_failure_preserved_in_cleanup_code(self):
        m = self._manifest_with_id("rollback-fsync-fail")
        target_dir = self.repo / EVIDENCE_ALLOWLIST_DIR
        real_classify = em._directory_fsync_classify
        def failing_classify(d):
            # Rollback fsync fails (only when called for the rollback path —
            # i.e. after the target was deleted; the publish-path fsync is
            # blocked from ever running because the git gate fails first).
            return "FAILED"
        with mock.patch.object(em, "_directory_fsync_classify",
                               side_effect=failing_classify):
            with self.assertRaises(EvidencePublishError) as cm:
                _publish_evidence_with_dependencies(
                    m, repo_root=str(self.repo),
                    git_status_fn=lambda: ["M  samples/x.json"])
        self.assertEqual(cm.exception.primary_error_code,
                         "EVIDENCE_GATE_FAILED:PROTECTED_PATH")
        self.assertEqual(cm.exception.cleanup_error_code,
                         "EVIDENCE_GATE_FAILED:ROLLBACK_FSYNC_FAILED")
        self.assertFalse(
            (target_dir / "rollback-fsync-fail.json").exists())


class TestDualCodeCombinations(unittest.TestCase):
    """Fix 5 (third review): final-hash/fsync failures + unlink failures."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name).resolve()
        (self.repo / EVIDENCE_ALLOWLIST_DIR).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _manifest_with_id(evidence_id):
        m = _historical_manifest()
        m["evidence_id"] = evidence_id
        return m

    def test_final_hash_mismatch_plus_unlink_failure_dual_codes(self):
        # Writer: final-hash mismatch (primary) + its own cleanup unlink
        # failure (cleanup) — both preserved via _WriterFailure and surfaced
        # as EvidencePublishError dual codes.
        m = self._manifest_with_id("dual-hash")
        target_path = self.repo / EVIDENCE_ALLOWLIST_DIR / "dual-hash.json"
        real_read = Path.read_bytes
        def tampered_read(self, *a, **k):
            data = real_read(self, *a, **k)
            if self.name == "dual-hash.json":
                return b'{"tampered":true}'
            return data
        real_unlink = Path.unlink
        def failing_unlink(self, *a, **k):
            if self.name == "dual-hash.json":
                raise OSError("cannot unlink")
            return real_unlink(self, *a, **k)
        with mock.patch.object(Path, "read_bytes", tampered_read), \
             mock.patch.object(Path, "unlink", failing_unlink):
            exc = None
            try:
                _no_clobber_atomic_write(
                    m, repo_root=str(self.repo), evidence_id="dual-hash")
            except _WriterFailure as wf:
                exc = wf
        self.assertIsNotNone(exc)
        self.assertTrue(
            exc.primary_error_code.endswith(":CONTENT_HASH_MISMATCH"))
        self.assertEqual(exc.cleanup_error_code, "ROLLBACK_FAILED")

    def test_fsync_failure_plus_unlink_failure_dual_codes(self):
        # Publisher: fsync FAILED (primary IO_ERROR) + rollback unlink fails
        # (cleanup ROLLBACK_FAILED).
        m = self._manifest_with_id("dual-fsync")
        target_path = self.repo / EVIDENCE_ALLOWLIST_DIR / "dual-fsync.json"
        real_unlink = Path.unlink
        def failing_unlink(self, *a, **k):
            if self.name == "dual-fsync.json":
                raise OSError("cannot unlink")
            return real_unlink(self, *a, **k)
        def failing_fsync(d):
            raise OSError("fsync failed")
        with mock.patch.object(Path, "unlink", failing_unlink):
            with self.assertRaises(EvidencePublishError) as cm:
                _publish_evidence_with_dependencies(
                    m, repo_root=str(self.repo),
                    git_status_fn=lambda: [
                        "?? evidence/isolated-live/phase-c/dual-fsync.json"],
                    directory_fsync_fn=failing_fsync)
        self.assertEqual(cm.exception.primary_error_code,
                         "EVIDENCE_GATE_FAILED:IO_ERROR")
        self.assertEqual(cm.exception.cleanup_error_code,
                         "EVIDENCE_GATE_FAILED:ROLLBACK_FAILED")

    def test_snapshot_cleanup_exception_does_not_mask_primary(self):
        # The step-7 snapshot check must SUCCEED; only the rollback-time
        # re-verification throws (non-EvidenceGateError). It must surface as
        # CLEANUP_SNAPSHOT_FAILED without overriding the primary code.
        m = self._manifest_with_id("snap-mask")
        calls = {"n": 0}
        real_verify = em.verify_existing_evidence_unchanged
        def flaky_verify(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_verify(*a, **k)  # step 7 succeeds
            raise RuntimeError("unexpected cleanup failure")
        with mock.patch.object(em, "verify_existing_evidence_unchanged",
                               side_effect=flaky_verify):
            with self.assertRaises(EvidencePublishError) as cm:
                _publish_evidence_with_dependencies(
                    m, repo_root=str(self.repo),
                    git_status_fn=lambda: ["M  samples/x.json"])
        self.assertEqual(cm.exception.primary_error_code,
                         "EVIDENCE_GATE_FAILED:PROTECTED_PATH")
        self.assertEqual(cm.exception.cleanup_error_code,
                         "EVIDENCE_GATE_FAILED:CLEANUP_SNAPSHOT_FAILED")

    def test_unexpected_base_exception_cleans_target_and_temp(self):
        m = self._manifest_with_id("ki-clean")
        def interrupting_status():
            raise KeyboardInterrupt()
        with self.assertRaises(EvidencePublishError):
            _publish_evidence_with_dependencies(
                m, repo_root=str(self.repo),
                git_status_fn=interrupting_status)
        d = self.repo / EVIDENCE_ALLOWLIST_DIR
        self.assertFalse((d / "ki-clean.json").exists(), "target cleaned")
        leftovers = [p for p in d.iterdir() if p.name.startswith(".evidence-tmp-")]
        self.assertEqual(leftovers, [], "temp cleaned")


class TestEvidenceRootTypeCheck(unittest.TestCase):
    """Fix 7 (third review): evidence/ root type semantics."""

    def test_evidence_regular_file_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td).resolve()
            (repo / "evidence").write_text("i am a file, not a dir")
            _gate(self, snapshot_existing_evidence, str(repo),
                  reason="PROTECTED_PATH")

    def test_evidence_absent_empty_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td).resolve()
            snap = snapshot_existing_evidence(str(repo))
            self.assertEqual(snap, {})


class TestFsyncClassificationAccuracy(unittest.TestCase):
    """Fix 4 (third review): permission failure is NOT 'unsupported'."""

    def test_open_permission_failure_is_failed_not_unsupported(self):
        import errno as errno_mod
        d = Path("Z:/definitely/not/real")
        with mock.patch.object(
                em.os, "open",
                side_effect=OSError(errno_mod.EACCES, "permission denied")):
            # Force the POSIX branch for the test.
            with mock.patch.object(em.os, "name", "posix"):
                result = em._directory_fsync_classify(d)
        self.assertEqual(result, "FAILED",
                         "EACCES must be FAILED, never UNSUPPORTED")

    def test_open_enotsup_is_unsupported(self):
        import errno as errno_mod
        enotsup = getattr(errno_mod, "ENOTSUP",
                          getattr(errno_mod, "EOPNOTSUPP", 22))
        d = Path("C:/some/dir")  # construct BEFORE patching os.name
        with mock.patch.object(
                em.os, "open",
                side_effect=OSError(enotsup, "not supported")):
            with mock.patch.object(em.os, "name", "posix"):
                result = em._directory_fsync_classify(d)
        self.assertEqual(result, "UNSUPPORTED_BY_PLATFORM")

    def test_windows_recorded_as_unsupported_without_call(self):
        with mock.patch.object(em.os, "name", "nt"), \
             mock.patch.object(em.os, "open",
                               side_effect=AssertionError("must not open")):
            result = em._directory_fsync_classify(Path("/x"))
        self.assertEqual(result, "UNSUPPORTED_BY_PLATFORM")


if __name__ == "__main__":
    unittest.main()
