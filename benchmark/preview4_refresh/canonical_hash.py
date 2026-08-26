#!/usr/bin/env python3
"""Canonical hashing for cross-platform benchmark archival.

canonical_digest(path):
- UTF-8 text extensions (.json/.jsonl/.txt/.md/.py): SHA256 over the
  LF-normalized bytes (CRLF -> LF). A CRLF working tree and an LF checkout
  of the same logical content therefore produce the SAME digest.
- Everything else (binaries): SHA256 over raw bytes.

This is the v2 hash scheme ("canonical-lf-v2"). Run-time records produced
before normalization (raw-run embedded digests) are preserved verbatim and
recorded separately in identity.json under run_time_identity.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

TEXT_EXTS = {".json", ".jsonl", ".txt", ".md", ".py"}


def canonical_bytes(path) -> tuple:
    raw = Path(path).read_bytes()
    if Path(path).suffix.lower() in TEXT_EXTS:
        return raw, raw.replace(b"\r\n", b"\n")
    return raw, raw


def canonical_digest(path) -> str:
    _, norm = canonical_bytes(path)
    return hashlib.sha256(norm).hexdigest()


def canonical_dir_digest(path) -> str:
    h = hashlib.sha256()
    for p in sorted(x for x in Path(path).rglob("*") if x.is_file()):
        h.update(p.relative_to(path).as_posix().encode("utf-8"))
        h.update(bytes.fromhex(canonical_digest(p)))
    return h.hexdigest()
