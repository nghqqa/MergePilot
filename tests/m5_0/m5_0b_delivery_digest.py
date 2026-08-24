#!/usr/bin/env python3
"""Deterministic SHA-256 delivery digest over the M5-0B delivery surface.

Covers the **union** of:
  1. the full M4-F delivery surface (reused verbatim from
     tests/m4f1/delivery_digest.delivery_files — its definition is NOT modified),
  2. the M5-0B handoff + Docker-isolation additions.

Two exclusion categories:
  A. **Generated artifacts** (safely excluded): pycache, evidence, log, tmp,
     temp, backup dirs; .pyc/.pyo/.log/.tmp/.temp/.bak/.backup/.swp/.swo/~ files.
     These never enter the surface; changing them never changes the digest.
  B. **Credential-like files** (fail-closed DeliveryScopeError, CLI rc=2):
     .env, *.env, credentials.*, secrets.*, id_rsa, id_ed25519, *.pem, *.key,
     .netrc, .npmrc, .pypirc. Never silently included or skipped.

Manifest is fail-closed: missing/empty/unsorted/duplicate/illegal-format → rc=2.
Manifest must exactly equal the delivery surface (MISSING + UNEXPECTED → rc=1).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys

# Reuse M4-F delivery_files WITHOUT modifying its definition.
_M4F1_DIR = pathlib.Path(__file__).resolve().parents[1] / "m4f1"
if str(_M4F1_DIR) not in sys.path:
    sys.path.insert(0, str(_M4F1_DIR))
from delivery_digest import delivery_files as _m4f_delivery_files  # noqa: E402

_DIGEST_TAG = b"mergepilot.m5.0b.delivery.v1\n"
_SCOPE = (
    "M5-0B handoff + isolated test-daemon delivery surface, "
    "including M4-F regression base"
)

# ── M5-0B additional tree roots (walked recursively) ──
_M5_TREE_ROOTS = (
    "tools/test-env",
    "tests/m5_0",
)
# ── M5-0B additional explicit files (not inside the M4-F walk or M5 trees) ──
_M5_EXPLICIT_FILES = (
    "tools/start-m5-0-candidate.sh",
    "tools/handoff_watcher.py",
    "tools/handoff_watcher_v2.py",
    "config/m5-0-allowlist.yaml",
    "config/souls/reviewer/SOUL.md",
    "config/souls/fixer/SOUL.md",
    "config/souls/verifier/SOUL.md",
    "tests/test_env_isolation.sh",
    "tests/test_env_isolation.ps1",
    "docs/M5-0-HiClaw-Live设计冻结.md",
    "tests/m5_0/m5_0b_delivery_required.txt",
    # productization round (M8-GH-4): the E2E operations console is a
    # delivered formal surface — CLI projection/timeline derivation,
    # lifecycle error journaling, and its contract tests
    "tools/cli/mergepilot.py",
    "tools/cli/e2e_lifecycle.py",
    "tests/gh_app/test_e2e_console_status.py",
)
# M5-0B extends M4-F formal suffixes with .ps1 (PowerShell wrappers).
_M5_FORMAL_SUFFIXES = (".py", ".sh", ".sql", ".yaml", ".yml", ".ps1")

# ── Category A: generated-artifact exclusion (safely skip, no error) ──
_GEN_SKIP_DIRS = frozenset({
    "__pycache__", ".pytest_cache", ".git",
    "evidence", "log", "logs", "tmp", "temp", "backup", "backups",
})
_GEN_SKIP_SUFFIXES = frozenset({
    ".pyc", ".pyo", ".log", ".tmp", ".temp",
    ".bak", ".backup", ".swp", ".swo",
})
_GEN_SKIP_NAMES = frozenset({".DS_Store"})

# ── Category B: credential-like files (fail-closed DeliveryScopeError) ──
#  Match against the *base name* of the file.  Designed to NOT false-positive
#  on legitimate source like ``test_credentials.py`` — every pattern below
#  either IS the exact base name or is a suffix that never appears on a
#  legitimate Python / shell / SQL / YAML / PowerShell file.
_CRED_EXACT_NAMES = frozenset({
    ".env", "credentials.yaml", "credentials.yml", "credentials.json",
    "secrets.yaml", "secrets.yml", "secrets.json",
    "id_rsa", "id_ed25519",
    ".netrc", ".npmrc", ".pypirc",
})
_CRED_SUFFIXES = (".env", ".pem", ".key")


class DeliveryScopeError(Exception):
    """Raised when a credential-like file is discovered in the scanned tree,
    or when the manifest is missing / malformed."""


def _is_generated(path: pathlib.Path, rel_parts: tuple[str, ...]) -> bool:
    """Category A — safely excludable generated artifact."""
    if path.name in _GEN_SKIP_NAMES:
        return True
    if path.name.endswith("~"):  # editor backup (e.g. file.py~)
        return True
    if any(part in _GEN_SKIP_DIRS for part in rel_parts):
        return True
    if path.suffix.lower() in _GEN_SKIP_SUFFIXES:
        return True
    return False


def _is_credential(path: pathlib.Path) -> bool:
    """Category B — credential-like file that must trigger fail-closed.
    Matches on the base name only, so ``test_credentials.py`` is NOT matched
    (its base name is ``test_credentials.py``, not ``credentials.yaml``)."""
    name = path.name
    if name in _CRED_EXACT_NAMES:
        return True
    return any(name.endswith(suf) for suf in _CRED_SUFFIXES)


def _m5_keep(path: pathlib.Path, rel_parts: tuple[str, ...]) -> bool:
    """Return True if *path* should be in the M5-0B surface.
    Raises DeliveryScopeError if *path* is credential-like."""
    if _is_credential(path):
        raise DeliveryScopeError(
            "credential-like file in scanned tree: %s" % "/".join(rel_parts))
    if _is_generated(path, rel_parts):
        return False
    return path.suffix.lower() in _M5_FORMAL_SUFFIXES


def m5_0b_delivery_files(root: pathlib.Path) -> list[str]:
    """Return the sorted union of M4-F delivery files + M5-0B additions.
    Raises DeliveryScopeError if a credential-like file is found."""
    root = root.resolve()
    found: set[str] = set(_m4f_delivery_files(root))
    for rel in _M5_EXPLICIT_FILES:
        clean = rel.replace("\\", "/")
        if not (root / clean).is_file():
            raise DeliveryScopeError(
                "explicit M5 file does not exist: %s" % clean)
        found.add(clean)
    for sub in _M5_TREE_ROOTS:
        base = root / sub
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if _m5_keep(path, rel.parts):
                found.add(rel.as_posix())
    return sorted(found)


def compute_digest(root: pathlib.Path) -> tuple[str, int]:
    """Return (hexdigest, file_count) over the M5-0B delivery surface."""
    root = root.resolve()
    files = m5_0b_delivery_files(root)
    h = hashlib.sha256()
    h.update(_DIGEST_TAG)
    h.update(("%d\n" % len(files)).encode("ascii"))
    for rel in files:
        try:
            raw = (root / rel).read_bytes()
        except OSError as exc:
            raise DeliveryScopeError(
                "cannot read delivery file %s: %s" % (rel, type(exc).__name__)
            ) from exc
        rel_b = rel.encode("utf-8")
        h.update(("%d:" % len(rel_b)).encode("ascii"))
        h.update(rel_b)
        h.update((":%d:" % len(raw)).encode("ascii"))
        h.update(raw)
    return h.hexdigest(), len(files)


_MANIFEST_PATH = "tests/m5_0/m5_0b_delivery_required.txt"


def _load_required_manifest(root: pathlib.Path) -> list[str]:
    """Load + validate the manifest. Fail-closed (DeliveryScopeError) on:
    missing, empty, unsorted, duplicate, or illegal-format paths."""
    root = root.resolve()
    manifest_file = root / _MANIFEST_PATH
    if not manifest_file.is_file():
        raise DeliveryScopeError("manifest not found: %s" % _MANIFEST_PATH)
    raw_lines = manifest_file.read_text(encoding="utf-8").splitlines()
    paths = []
    for i, line in enumerate(raw_lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "\\" in stripped:
            raise DeliveryScopeError(
                "manifest line %d: backslash forbidden: %s" % (i, stripped))
        if stripped.startswith("/"):
            raise DeliveryScopeError(
                "manifest line %d: absolute path forbidden: %s" % (i, stripped))
        if ".." in stripped.split("/"):
            raise DeliveryScopeError(
                "manifest line %d: '..' forbidden: %s" % (i, stripped))
        paths.append(stripped)
    if not paths:
        raise DeliveryScopeError("manifest is empty")
    if len(paths) != len(set(paths)):
        raise DeliveryScopeError("manifest contains duplicates")
    if paths != sorted(paths):
        raise DeliveryScopeError("manifest is not sorted")
    return paths


def verify_required(root: pathlib.Path) -> tuple[list[str], list[str]]:
    """Return (missing, unexpected) — manifest paths not in surface, and
    surface paths not in manifest. Both empty = exact match."""
    root = root.resolve()
    surface = set(m5_0b_delivery_files(root))
    manifest = set(_load_required_manifest(root))
    missing = sorted(manifest - surface)
    unexpected = sorted(surface - manifest)
    return missing, unexpected


def main() -> int:
    parser = argparse.ArgumentParser(description="M5-0B delivery digest")
    parser.add_argument("repo_root", help="repository root")
    parser.add_argument("--check", dest="expected", default=None,
                        help="expected digest; exit 1 on mismatch")
    parser.add_argument("--list", action="store_true",
                        help="print sorted path list")
    parser.add_argument("--json", action="store_true",
                        help="JSON output: scope, digest, files, paths")
    parser.add_argument("--verify-required", action="store_true",
                        help="fail-closed if manifest != surface (rc=1) or "
                             "manifest malformed (rc=2)")
    args = parser.parse_args()
    root = pathlib.Path(args.repo_root).resolve()

    # All paths that can raise DeliveryScopeError → rc=2, clean stderr, no traceback.
    try:
        if args.verify_required:
            missing, unexpected = verify_required(root)
            if missing or unexpected:
                if missing:
                    sys.stderr.write("MISSING from surface (%d):\n" % len(missing))
                    for m in missing:
                        sys.stderr.write("  %s\n" % m)
                if unexpected:
                    sys.stderr.write("UNEXPECTED in surface (%d):\n" % len(unexpected))
                    for u in unexpected:
                        sys.stderr.write("  %s\n" % u)
                return 1
            sys.stdout.write("required paths OK (manifest == surface, %d files)\n"
                             % len(_load_required_manifest(root)))
            return 0

        digest, count = compute_digest(root)
    except DeliveryScopeError as e:
        sys.stderr.write("DELIVERY_SCOPE_ERROR: %s\n" % e)
        return 2

    if args.list:
        for f in m5_0b_delivery_files(root):
            sys.stdout.write(f + "\n")
        return 0

    if args.json:
        files = m5_0b_delivery_files(root)
        out = {
            "schema": "mergepilot.m5.0b.delivery.v1",
            "scope": _SCOPE,
            "digest": digest,
            "files": count,
            "paths": files,
        }
        sys.stdout.write(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
        return 0

    if args.expected is not None:
        if digest != args.expected:
            sys.stderr.write(
                "delivery_digest MISMATCH expected=%s got=%s\n" % (args.expected, digest))
            return 1
        sys.stdout.write("delivery_digest OK files=%d\n" % count)
        return 0

    sys.stdout.write(digest + "\n")
    sys.stdout.write(("%d\n" % count))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
