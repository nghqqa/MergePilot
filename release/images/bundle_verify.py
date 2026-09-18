#!/usr/bin/env python3
"""Offline bundle verifier for MergePilot image deliveries (stdlib only).

Used by:
  - release/images/load-images.sh / load-images.ps1 (pre-load verification)
  - tests/release_delivery/test_image_delivery.py (hermetic contract tests)

Checks, in order:
  1. manifest parses and carries the required fields
  2. asset names are ASCII (GitHub Release asset contract)
  3. SHA256SUMS entries match the actual files and cover archive + manifest
  4. no secret-shaped strings in shipped text assets
  5. no host-machine absolute paths in shipped text assets

`verify_bundle(root, expect_images=None, skip_docker=True)` returns
(ok: bool, problems: list[str]).
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

REQUIRED_MANIFEST_FIELDS = ("schema_version", "bundle", "archive", "platform",
                            "source_commit", "release_version", "created_at", "images")
REQUIRED_IMAGE_FIELDS = ("image", "tag", "ref", "digest", "architecture")
ASSET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

SECRET_PATTERNS = [
    ("DeepSeek/OpenAI-style key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("GitHub PAT (classic)", re.compile(r"ghp_[A-Za-z0-9]{30,}")),
    ("GitHub PAT (fine-grained)", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("Aliyun AccessKey", re.compile(r"LTAI[A-Za-z0-9]{12,}")),
    ("private key block", re.compile(r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY")),
    ("AWS AccessKey", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"xox[bap]-[A-Za-z0-9-]{10,}")),
    ("Matrix access token", re.compile(r"syt_[A-Za-z0-9._-]{20,}")),
]
ALLOWED_SECRET_VALUES = {
    # canonical documentation example keys that appear inside upstream test fixtures
    "AKIAIOSFODNN7EXAMPLE", "AKIAI44QH8DHBEXAMPLE",
}

HOST_PATH_PATTERNS = [
    ("dev-machine repo path (Windows)", re.compile(r"D:\\goai", re.I)),
    ("dev-machine user dir", re.compile(r"C:\\Users\\", re.I)),
    ("dev-machine repo path (POSIX)", re.compile(r"/d/goai", re.I)),
    ("dev-machine user dir (POSIX)", re.compile(r"/c/users/ngh", re.I)),
    ("WSL mount path", re.compile(r"/mnt/[a-z]/goai", re.I)),
]


def load_manifest(path: Path):
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")  # tolerate a BOM from Windows tooling
    m = json.loads(text)
    problems = []
    for field in REQUIRED_MANIFEST_FIELDS:
        if field not in m:
            problems.append(f"manifest missing required field: {field}")
    for i, img in enumerate(m.get("images", [])):
        for field in REQUIRED_IMAGE_FIELDS:
            if field not in img:
                problems.append(f"manifest images[{i}] missing field: {field}")
        digest = img.get("digest", "")
        if not digest.startswith("sha256:") or len(digest) != 71:
            problems.append(f"manifest images[{i}]: digest must be 'sha256:' + 64 hex chars")
        if img.get("tag") == "latest" or str(img.get("tag", "")).lower() == "latest":
            problems.append(f"manifest images[{i}]: floating tag 'latest' is forbidden")
    for key in ("bundle", "archive"):
        v = str(m.get(key, ""))
        if v and not ASSET_NAME_RE.match(v):
            problems.append(f"manifest {key} name is not ASCII-safe: {v}")
    return m, problems


def verify_sha256sums(root: Path, sha_path: Path):
    ok, problems = True, []
    entries = []
    for line in sha_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        digest, sep, name = line.partition(" ")
        name = name.lstrip("*").strip()
        entries.append((digest, name))
        p = root / name
        if not p.is_file():
            problems.append(f"SHA256SUMS: missing file {name}"); ok = False; continue
        actual = hashlib.sha256(p.read_bytes()).hexdigest()
        if actual != digest:
            problems.append(f"SHA256SUMS: hash mismatch for {name}"); ok = False
    covered = {name for _, name in entries}
    for required in ("MergePilot-images-manifest.json",
                     "MergePilot-images-linux-amd64.tar.zst"):
        if required not in covered:
            problems.append(f"SHA256SUMS: does not cover {required}"); ok = False
    return ok, problems, covered


def scan_secrets(paths):
    hits = []
    for p in paths:
        try:
            text = Path(p).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for label, pat in SECRET_PATTERNS:
            for m in pat.finditer(text):
                value = m.group(0)
                if value in ALLOWED_SECRET_VALUES:
                    continue
                line = text[:m.start()].count("\n") + 1
                hits.append(f"{p}:{line}: {label} ({value[:16]}...)")
    return hits


def scan_host_paths(paths):
    hits = []
    for p in paths:
        try:
            text = Path(p).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for label, pat in HOST_PATH_PATTERNS:
            for m in pat.finditer(text):
                line = text[:m.start()].count("\n") + 1
                hits.append(f"{p}:{line}: {label}")
    return hits


def verify_bundle(root: Path, *, check_files: bool = True):
    """Verify an extracted bundle directory. Returns (ok, problems)."""
    root = Path(root)
    problems = []
    manifest_path = root / "MergePilot-images-manifest.json"
    if not manifest_path.is_file():
        return False, [f"manifest not found: {manifest_path}"]
    manifest, problems = load_manifest(manifest_path)
    if problems:
        return False, problems
    if check_files:
        sha = root / "SHA256SUMS"
        if not sha.is_file():
            problems.append("SHA256SUMS not found")
        else:
            _, file_problems, _ = verify_sha256sums(root, sha)
            problems.extend(file_problems)
    text_assets = [manifest_path, root / "README-runtime-images.md"]
    secret_hits = scan_secrets([str(x) for x in text_assets if Path(x).is_file()])
    path_hits = scan_host_paths([str(x) for x in text_assets if Path(x).is_file()])
    problems.extend("secrets: " + h for h in secret_hits)
    problems.extend("host-path: " + h for h in path_hits)
    return (len(problems) == 0), problems


if __name__ == "__main__":
    import sys
    ok, problems = verify_bundle(Path(sys.argv[1] if len(sys.argv) > 1 else "."))
    for p in problems:
        print("PROBLEM:", p)
    print("BUNDLE_OK" if ok else "BUNDLE_INVALID")
    sys.exit(0 if ok else 1)
