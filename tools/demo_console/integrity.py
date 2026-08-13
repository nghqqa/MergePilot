#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared bundle integrity functions.

Single authoritative source for canonical JSON serialization and
bundle_sha256 computation. Used by schema.py, bundle_builder.py,
and live_poller.py to avoid circular imports.
"""
from __future__ import annotations

import hashlib
import json
import re

# Fields excluded from bundle_sha256 computation
VOLATILE_FIELDS = frozenset({"bundle_sha256", "generated_at"})

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def canonical_json_without_volatile(bundle: dict) -> str:
    """Canonical JSON for SHA-256, excluding volatile/self-referential fields."""
    clean = {k: v for k, v in bundle.items() if k not in VOLATILE_FIELDS}
    return json.dumps(clean, sort_keys=True, ensure_ascii=False)


def compute_bundle_sha256(bundle: dict) -> str:
    """Compute bundle SHA-256 over canonical JSON (excluding volatile fields)."""
    return hashlib.sha256(canonical_json_without_volatile(bundle).encode("utf-8")).hexdigest()


def is_valid_sha256(value) -> bool:
    """Check if value is a valid 64-char lowercase hex SHA-256."""
    if not isinstance(value, str):
        return False
    return bool(_SHA256_PATTERN.match(value))


def verify_bundle_integrity(bundle: dict) -> list[str]:
    """Verify bundle_sha256 field exists, is well-formed, and matches recomputed value.

    Returns list of error strings (empty = valid).
    """
    errors = []
    stored = bundle.get("bundle_sha256")
    if stored is None:
        errors.append("bundle_sha256 is missing")
        return errors
    if not is_valid_sha256(stored):
        errors.append(f"bundle_sha256 is not valid 64-char hex: {str(stored)[:20]}")
        return errors
    recomputed = compute_bundle_sha256(bundle)
    if stored != recomputed:
        errors.append(f"bundle_sha256 mismatch: stored={stored[:16]}... recomputed={recomputed[:16]}...")
    return errors
