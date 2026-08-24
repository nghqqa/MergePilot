#!/usr/bin/env python3
"""Unit tests for the M5-0B delivery digest tool (P1/P2 fix round).

Covers: credential fail-closed, generated-artifact exclusion, manifest
exact-equality / fail-closed / malformed, M4-F subset, self-reference,
file-change sensitivity, and CLI rc propagation.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DIGEST_TOOL = ROOT / "tests/m5_0/m5_0b_delivery_digest.py"

sys.path.insert(0, str(ROOT / "tests/m5_0"))
import m5_0b_delivery_digest as m5dd  # noqa: E402
sys.path.insert(0, str(ROOT / "tests/m4f1"))
import delivery_digest as m4fdd  # noqa: E402


def _copy_surface(tmp_root: pathlib.Path) -> None:
    """Copy the full M5-0B delivery surface from the real repo into tmp_root."""
    for rel in m5dd.m5_0b_delivery_files(ROOT):
        src = ROOT / rel
        dst = tmp_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst)


# ── P1-A: generated artifacts excluded ──

class TestGeneratedExclusion:
    @pytest.mark.parametrize("rel,name,content", [
        ("tests/m5_0/backup/old.py", "old.py", "# old\n"),
        ("tests/m5_0/temp/gen.py", "gen.py", "# generated\n"),
        ("tests/m5_0/logs/tool.py", "tool.py", "# log\n"),
        ("tests/m5_0/run.log", "run.log", "log line\n"),
        ("tests/m5_0/x.pyo", "x.pyo", "binary\n"),
        ("tests/m5_0/x.bak", "x.bak", "backup\n"),
        ("tests/m5_0/x.tmp", "x.tmp", "temp\n"),
        ("tests/m5_0/editor.py~", "editor.py~", "# backup\n"),
    ])
    def test_generated_file_excluded(self, rel, name, content):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = pathlib.Path(tmp)
            _copy_surface(tmp_root)
            target = tmp_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            surface = set(m5dd.m5_0b_delivery_files(tmp_root))
            assert rel not in surface, f"generated file should be excluded: {rel}"

    def test_changing_generated_does_not_change_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = pathlib.Path(tmp)
            _copy_surface(tmp_root)
            d1, _ = m5dd.compute_digest(tmp_root)
            logf = tmp_root / "tests/m5_0/logs/run.log"
            logf.parent.mkdir(parents=True, exist_ok=True)
            logf.write_text("changed\n")
            d2, _ = m5dd.compute_digest(tmp_root)
            assert d1 == d2


# ── P1-B: credential fail-closed ──

class TestCredentialFailClosed:
    @pytest.mark.parametrize("rel", [
        "tests/m5_0/credentials.yaml",
        "tests/m5_0/secrets.json",
        "tests/m5_0/sample.env",
        "tests/m5_0/.env",
        "tests/m5_0/id_rsa",
        "tests/m5_0/sample.pem",
        "tests/m5_0/sample.key",
    ])
    def test_credential_raises(self, rel):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = pathlib.Path(tmp)
            _copy_surface(tmp_root)
            target = tmp_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("SECRET\n")
            with pytest.raises(m5dd.DeliveryScopeError, match="credential"):
                m5dd.m5_0b_delivery_files(tmp_root)

    def test_legitimate_credentials_test_not_false_positive(self):
        """test_credentials.py is a legitimate source file — must be in surface."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = pathlib.Path(tmp)
            _copy_surface(tmp_root)
            (tmp_root / "tests/m5_0").mkdir(parents=True, exist_ok=True)
            (tmp_root / "tests/m5_0/test_credentials.py").write_text("# legit\n")
            surface = set(m5dd.m5_0b_delivery_files(tmp_root))
            assert "tests/m5_0/test_credentials.py" in surface

    def test_cli_credential_rc2_no_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = pathlib.Path(tmp)
            _copy_surface(tmp_root)
            (tmp_root / "tests/m5_0").mkdir(parents=True, exist_ok=True)
            (tmp_root / "tests/m5_0/secrets.json").write_text('{}\n')
            r = subprocess.run(
                [sys.executable, str(DIGEST_TOOL), str(tmp_root), "--list"],
                capture_output=True, text=True, check=False)
            assert r.returncode == 2
            assert "DELIVERY_SCOPE_ERROR" in r.stderr
            assert "credential" in r.stderr.lower()
            assert "Traceback" not in r.stderr


# ── P2-a: manifest exact equality ──

class TestManifestExactEquality:
    def test_manifest_equals_surface(self):
        surface = set(m5dd.m5_0b_delivery_files(ROOT))
        manifest = set(m5dd._load_required_manifest(ROOT))
        assert manifest == surface, "manifest must exactly equal surface"

    def test_manifest_sorted_unique(self):
        paths = m5dd._load_required_manifest(ROOT)
        assert paths == sorted(paths)
        assert len(paths) == len(set(paths))

    def test_manifest_non_empty(self):
        paths = m5dd._load_required_manifest(ROOT)
        assert len(paths) > 0

    def test_verify_required_clean(self):
        missing, unexpected = m5dd.verify_required(ROOT)
        assert missing == []
        assert unexpected == []


# ── P2-b: manifest fail-closed ──

class TestManifestFailClosed:
    def _write_manifest(self, tmp_root, lines):
        mf = tmp_root / m5dd._MANIFEST_PATH
        mf.parent.mkdir(parents=True, exist_ok=True)
        mf.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))

    def test_missing_manifest_rc2(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = pathlib.Path(tmp)
            _copy_surface(tmp_root)
            (tmp_root / m5dd._MANIFEST_PATH).unlink(missing_ok=True)
            with pytest.raises(m5dd.DeliveryScopeError, match="not found"):
                m5dd._load_required_manifest(tmp_root)

    def test_empty_manifest_rc2(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = pathlib.Path(tmp)
            _copy_surface(tmp_root)
            self._write_manifest(tmp_root, [])
            with pytest.raises(m5dd.DeliveryScopeError, match="empty"):
                m5dd._load_required_manifest(tmp_root)

    def test_duplicate_manifest_rc2(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = pathlib.Path(tmp)
            _copy_surface(tmp_root)
            surface = m5dd.m5_0b_delivery_files(ROOT)
            dup = list(surface) + [surface[0]]
            self._write_manifest(tmp_root, dup)
            with pytest.raises(m5dd.DeliveryScopeError, match="duplicate"):
                m5dd._load_required_manifest(tmp_root)

    def test_unsorted_manifest_rc2(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = pathlib.Path(tmp)
            _copy_surface(tmp_root)
            surface = m5dd.m5_0b_delivery_files(ROOT)
            shuffled = list(reversed(surface))
            self._write_manifest(tmp_root, shuffled)
            with pytest.raises(m5dd.DeliveryScopeError, match="not sorted"):
                m5dd._load_required_manifest(tmp_root)

    def test_absolute_path_rc2(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = pathlib.Path(tmp)
            _copy_surface(tmp_root)
            surface = m5dd.m5_0b_delivery_files(ROOT)
            bad = sorted(list(surface) + ["/etc/passwd"])
            self._write_manifest(tmp_root, bad)
            with pytest.raises(m5dd.DeliveryScopeError, match="absolute"):
                m5dd._load_required_manifest(tmp_root)

    def test_backslash_path_rc2(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = pathlib.Path(tmp)
            _copy_surface(tmp_root)
            surface = m5dd.m5_0b_delivery_files(ROOT)
            bad = sorted(list(surface) + ["tools\\bad.py"])
            self._write_manifest(tmp_root, bad)
            with pytest.raises(m5dd.DeliveryScopeError, match="backslash"):
                m5dd._load_required_manifest(tmp_root)

    def test_dotdot_path_rc2(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = pathlib.Path(tmp)
            _copy_surface(tmp_root)
            surface = m5dd.m5_0b_delivery_files(ROOT)
            bad = sorted(list(surface) + ["../escape.py"])
            self._write_manifest(tmp_root, bad)
            with pytest.raises(m5dd.DeliveryScopeError, match=r"\.\."):
                m5dd._load_required_manifest(tmp_root)

    def test_delete_required_path_rc1(self):
        """Removing a path from the manifest → it appears as UNEXPECTED
        (in surface, not in manifest). --verify-required must rc=1."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = pathlib.Path(tmp)
            _copy_surface(tmp_root)
            surface = m5dd.m5_0b_delivery_files(ROOT)
            removed = surface[:1]
            remaining = sorted([p for p in surface if p != removed[0]])
            self._write_manifest(tmp_root, remaining)
            missing, unexpected = m5dd.verify_required(tmp_root)
            assert missing == []
            assert len(unexpected) == 1
            assert unexpected[0] == removed[0]

    def test_add_unexpected_path_rc1(self):
        """Adding a non-existent path to the manifest → it appears as MISSING
        (in manifest, not in surface). --verify-required must rc=1."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = pathlib.Path(tmp)
            _copy_surface(tmp_root)
            surface = m5dd.m5_0b_delivery_files(ROOT)
            added = sorted(list(surface) + ["zzz/unexpected.py"])
            self._write_manifest(tmp_root, added)
            missing, unexpected = m5dd.verify_required(tmp_root)
            assert len(missing) == 1
            assert missing[0] == "zzz/unexpected.py"
            assert unexpected == []


# ── Determinism + M4-F subset ──

class TestDeterminism:
    def test_two_runs_identical(self):
        d1, c1 = m5dd.compute_digest(ROOT)
        d2, c2 = m5dd.compute_digest(ROOT)
        assert d1 == d2 and c1 == c2

    def test_m4f_strict_subset(self):
        m4f = set(m4fdd.delivery_files(ROOT))
        m5 = set(m5dd.m5_0b_delivery_files(ROOT))
        assert m4f < m5  # strict subset (proper)

    def test_m4f_digest_stable_across_m5_compute(self):
        d1, _ = m4fdd.compute_digest(ROOT)
        _, _ = m5dd.compute_digest(ROOT)
        d2, _ = m4fdd.compute_digest(ROOT)
        assert d1 == d2


# ── Self-reference immunity ──

class TestSelfReference:
    def test_evidence_m5_0b_output_does_not_change_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = pathlib.Path(tmp)
            _copy_surface(tmp_root)
            d1, _ = m5dd.compute_digest(tmp_root)
            ev = tmp_root / "evidence/m5/0b/delivery.json"
            ev.parent.mkdir(parents=True, exist_ok=True)
            ev.write_text(json.dumps({"digest": d1}) + "\n")
            d2, _ = m5dd.compute_digest(tmp_root)
            assert d1 == d2

    def test_log_temp_output_does_not_change_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = pathlib.Path(tmp)
            _copy_surface(tmp_root)
            d1, _ = m5dd.compute_digest(tmp_root)
            (tmp_root / "tests/m5_0/tmp").mkdir(parents=True, exist_ok=True)
            (tmp_root / "tests/m5_0/tmp/output.txt").write_text("data\n")
            (tmp_root / "tests/m5_0/logs").mkdir(parents=True, exist_ok=True)
            (tmp_root / "tests/m5_0/logs/run.log").write_text("log\n")
            d2, _ = m5dd.compute_digest(tmp_root)
            assert d1 == d2


# ── File sensitivity ──

class TestFileSensitivity:
    def test_protected_file_change_changes_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = pathlib.Path(tmp)
            _copy_surface(tmp_root)
            d1, c1 = m5dd.compute_digest(tmp_root)
            (tmp_root / "tools/workflow-controller/controller.py").write_text("CHANGED\n")
            d2, c2 = m5dd.compute_digest(tmp_root)
            assert d1 != d2 and c1 == c2

    def test_excluded_file_change_does_not_change_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = pathlib.Path(tmp)
            _copy_surface(tmp_root)
            (tmp_root / "evidence").mkdir(exist_ok=True)
            evf = tmp_root / "evidence/x.txt"
            evf.write_text("V1\n")
            d1, _ = m5dd.compute_digest(tmp_root)
            evf.write_text("V2_DIFFERENT\n")
            d2, _ = m5dd.compute_digest(tmp_root)
            assert d1 == d2


# ── P2-1 fix: read-failure fail-closed ──

class TestReadFailureFailClosed:
    """compute_digest must convert OSError (FileNotFoundError,
    PermissionError, etc.) to DeliveryScopeError with rc=2 semantics."""

    def test_m4f_explicit_file_missing_raises_scope_error(self):
        """Delete a file inherited from M4-F delivery_files (controller.py)
        AFTER surface enumeration, then compute_digest must raise
        DeliveryScopeError (not raw FileNotFoundError)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = pathlib.Path(tmp)
            _copy_surface(tmp_root)
            target = tmp_root / "tools/workflow-controller/controller.py"
            assert target.is_file()
            target.unlink()
            with pytest.raises(m5dd.DeliveryScopeError, match="controller.py"):
                m5dd.compute_digest(tmp_root)

    def test_m4f_explicit_file_missing_error_has_type(self):
        """Error message must include the exception type name."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = pathlib.Path(tmp)
            _copy_surface(tmp_root)
            (tmp_root / "tools/workflow-controller/controller.py").unlink()
            with pytest.raises(m5dd.DeliveryScopeError) as exc_info:
                m5dd.compute_digest(tmp_root)
            assert "FileNotFoundError" in str(exc_info.value)

    def test_permission_error_raises_scope_error(self):
        """Any OSError (PermissionError etc.) during read → DeliveryScopeError.
        Uses mock so the test is portable (Windows chmod 000 doesn't deny reads)."""
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = pathlib.Path(tmp)
            _copy_surface(tmp_root)
            original = pathlib.Path.read_bytes
            count = [0]
            def fail_first(self):
                count[0] += 1
                if count[0] == 1:
                    raise PermissionError("simulated denial")
                return original(self)
            with patch.object(pathlib.Path, "read_bytes", fail_first):
                with pytest.raises(m5dd.DeliveryScopeError, match="PermissionError"):
                    m5dd.compute_digest(tmp_root)

    def test_cli_missing_file_rc2_no_traceback(self):
        """CLI subprocess: missing file → rc=2, DELIVERY_SCOPE_ERROR in stderr,
        no Traceback, no digest in stdout."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = pathlib.Path(tmp)
            _copy_surface(tmp_root)
            (tmp_root / "tools/workflow-controller/controller.py").unlink()
            r = subprocess.run(
                [sys.executable, str(DIGEST_TOOL), str(tmp_root)],
                capture_output=True, text=True, check=False)
            assert r.returncode == 2
            assert "DELIVERY_SCOPE_ERROR" in r.stderr
            assert "controller.py" in r.stderr
            assert "Traceback" not in r.stderr
            assert len(r.stdout.strip()) == 0  # no digest output

    def test_no_partial_digest_returned(self):
        """On read failure, compute_digest must NOT return a value."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = pathlib.Path(tmp)
            _copy_surface(tmp_root)
            (tmp_root / "tools/workflow-controller/controller.py").unlink()
            try:
                result = m5dd.compute_digest(tmp_root)
                assert False, "should have raised, got %r" % (result,)
            except m5dd.DeliveryScopeError:
                pass  # expected

    def test_positive_control_all_files_readable(self):
        """All files present → compute_digest succeeds, deterministic."""
        d1, c1 = m5dd.compute_digest(ROOT)
        d2, c2 = m5dd.compute_digest(ROOT)
        assert d1 == d2
        assert c1 == c2
        assert c1 == 95  # 92 + 3 console productization files


# ── CLI ──

class TestCLI:
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(DIGEST_TOOL), str(ROOT), *args],
            capture_output=True, text=True, check=False)

    def test_default_output(self):
        r = self._run()
        assert r.returncode == 0
        assert len(r.stdout.strip().split("\n")) == 2

    def test_check_match(self):
        digest, _ = m5dd.compute_digest(ROOT)
        r = self._run("--check", digest)
        assert r.returncode == 0 and "OK" in r.stdout

    def test_check_mismatch_rc1(self):
        r = self._run("--check", "0" * 64)
        assert r.returncode == 1 and "MISMATCH" in r.stderr

    def test_verify_required_rc0(self):
        r = self._run("--verify-required")
        assert r.returncode == 0

    def test_list_sorted(self):
        r = self._run("--list")
        assert r.returncode == 0
        lines = r.stdout.strip().split("\n")
        assert lines == sorted(lines)

    def test_json_schema(self):
        r = self._run("--json")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert data["files"] == len(data["paths"])
        assert data["paths"] == sorted(data["paths"])

    def test_no_traceback_on_scope_error(self):
        """If the repo surface is clean (no credential), no scope error."""
        r = self._run("--list")
        assert "Traceback" not in r.stderr


# ── Required-path coverage ──

REQUIRED_PATHS = [
    "tools/workflow-controller/controller.py",
    "tools/handoff_watcher.py",
    "tools/handoff_watcher_v2.py",
    "tools/test-env/mp_guard.sh",
    "tools/test-env/wsl_test.ps1",
    "tests/m5_0/m5_0b_delivery_digest.py",
    "tests/m5_0/test_m5_0b_delivery_digest.py",
    "tests/m5_0/m5_0b_delivery_required.txt",
    "tests/m5_0/fixtures/mini_matrix_hs.py",
    "tests/m5_0/fixtures/policy-m5-live-e2e.yaml",
    "tests/m5_0/fixtures/inject_skill_completion.py",
    "tests/m5_0/fixtures/run_m5_concurrency.py",
    "tests/m5_0/fixtures/run_neg_guard.sh",
    "config/souls/reviewer/SOUL.md",
    "tests/test_env_isolation.ps1",
    "tests/m4f1/check_hygiene.py",
    "tests/m4f1/run_all.sh",
    "docs/M5-0-HiClaw-Live设计冻结.md",
]

class TestRequiredCoverage:
    @pytest.mark.parametrize("rel", REQUIRED_PATHS)
    def test_in_surface(self, rel):
        surface = set(m5dd.m5_0b_delivery_files(ROOT))
        assert rel in surface, f"required path missing: {rel}"

    def test_design_doc_in_surface_python(self):
        """CJK design doc must be in surface — verify via Python, not grep."""
        surface = m5dd.m5_0b_delivery_files(ROOT)
        docs = [p for p in surface if "HiClaw" in p and p.startswith("docs/")]
        assert len(docs) == 1
        assert docs[0].endswith(".md")

    def test_all_candidate_files_in_surface(self):
        """Every modified + untracked candidate file that is a formal delivery
        file must be in the surface (NUL-safe Python read, UTF-8).
        Skipped if git is not available (e.g. bare container)."""
        import shutil
        if not shutil.which("git"):
            pytest.skip("git not available")
        import subprocess
        r = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain", "-z"],
            capture_output=True, check=False)
        if r.returncode != 0:
            pytest.skip("git status failed (rc=%d)" % r.returncode)
        surface = set(m5dd.m5_0b_delivery_files(ROOT))
        missing = []
        for entry in r.stdout.decode("utf-8", errors="replace").split("\0"):
            if not entry:
                continue
            path = entry[3:]  # strip XY + space
            if not path:
                continue
            suffix = pathlib.Path(path).suffix.lower()
            if suffix not in m5dd._M5_FORMAL_SUFFIXES and pathlib.Path(path).name not in (
                    "Dockerfile", "SOUL.md", "__init__.py"):
                continue
            if path not in surface:
                missing.append(path)
        assert missing == [], f"candidate files not in surface: {missing}"
