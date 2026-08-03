#!/usr/bin/env python3
"""Deterministic SHA-256 delivery digest over the M4-F delivery surface.

Covers the frozen M4-F source: the audit schema, the host runtime image
definition, the controller / gateway / worker / demo modules, the policy
gateway, and every formal Python / shell / SQL / fixture file under tests/m4f1.

Generated evidence, logs, caches, .pyc and temp files are excluded so the
digest is stable and never self-referential (it never covers an artefact that
itself records the digest).

The digest is a length-prefixed canonical envelope over the sorted repo-relative
POSIX paths and the raw file bytes, so it is independent of the platform path
separator or filesystem walk order.
"""
from __future__ import annotations

import argparse
import hashlib
import pathlib
import sys


# Directory trees walked recursively (formal files only).
_TREE_ROOTS = (
    "tools/m4f-runtime",
    "tests/m4f1",
)
# Single files outside the walked trees.
_EXPLICIT_FILES = (
    "tools/audit-db/m4f1_state.sql",
    "tools/m4f_demo.py",
    "tools/m4f_skill_worker.py",
    "tools/start-controller-container.sh",
    "tools/workflow-controller/controller.py",
    "tools/workflow-controller/gateway_client.py",
    "tools/workflow-controller/m4f_controller.py",
    "tools/workflow-controller/m4f_ingress.py",
    "tools/workflow-controller/Dockerfile",
    "tools/policy-gateway/gateway.py",
)
_FORMAL_SUFFIXES = (".py", ".sh", ".sql", ".yaml", ".yml")
_KEEP_NAMES = {"Dockerfile"}
_SKIP_DIRS = {"__pycache__", ".pytest_cache"}
# Generated evidence that physically lives inside tests/m4f1 (excluded so the
# digest only covers authored source, not gate output).
_SKIP_NAMES = {"evidence.json"}

_DIGEST_TAG = b"mergepilot.m4f.delivery.v1\n"


def _keep(path: pathlib.Path, rel_parts: tuple[str, ...]) -> bool:
    if path.name in _SKIP_NAMES:
        return False
    if path.suffix == ".pyc":
        return False
    if any(part in _SKIP_DIRS for part in rel_parts):
        return False
    return path.suffix.lower() in _FORMAL_SUFFIXES or path.name in _KEEP_NAMES


def delivery_files(root: pathlib.Path) -> list[str]:
    root = root.resolve()
    found: set[str] = set()
    for rel in _EXPLICIT_FILES:
        found.add(rel.replace("\\", "/"))
    for sub in _TREE_ROOTS:
        base = root / sub
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if not _keep(path, rel.parts):
                continue
            found.add(rel.as_posix())
    return sorted(found)


def compute_digest(root: pathlib.Path) -> tuple[str, int]:
    root = root.resolve()
    files = delivery_files(root)
    h = hashlib.sha256()
    h.update(_DIGEST_TAG)
    h.update(("%d\n" % len(files)).encode("ascii"))
    for rel in files:
        raw = (root / rel).read_bytes()
        rel_b = rel.encode("utf-8")
        h.update(("%d:" % len(rel_b)).encode("ascii"))
        h.update(rel_b)
        h.update((":%d:" % len(raw)).encode("ascii"))
        h.update(raw)
    return h.hexdigest(), len(files)


def main() -> int:
    parser = argparse.ArgumentParser(description="M4-F delivery digest")
    parser.add_argument("repo_root", help="repository root")
    parser.add_argument(
        "--check",
        dest="expected",
        default=None,
        help="expected digest; exit 1 on mismatch (writes OK note on match)",
    )
    args = parser.parse_args()
    digest, count = compute_digest(pathlib.Path(args.repo_root))
    if args.expected is not None:
        if digest != args.expected:
            sys.stderr.write(
                "delivery_digest MISMATCH expected=%s got=%s\n" % (args.expected, digest)
            )
            return 1
        sys.stdout.write("delivery_digest OK files=%d\n" % count)
        return 0
    sys.stdout.write(digest + "\n")
    sys.stdout.write(("%d\n" % count))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
