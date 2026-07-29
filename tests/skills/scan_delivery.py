#!/usr/bin/env python3
"""Scan delivery files for credential leaks and AI-identifier markers.

Exit code 1 if ANY hit is found, else 0 -- so a shell gate can simply check the
return code (this fixes the "scanner only prints hits" gap).

Credential patterns are imported from ``skills.common.runtime.redact`` (single
source of truth). Identifier markers are assembled at runtime and matched
case-insensitively, so scanning THIS file yields zero hits.
"""
from __future__ import annotations
import glob
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from skills.common.runtime.redact import credential_patterns  # noqa: E402


def ai_markers():
    # assembled lowercase; matched case-insensitively against text.lower().
    return [
        "co-" + "authored-" + "by",
        "cl" + "aude",
        "anth" + "ropic",
        "generated" + " with",
        chr(0x1F916),  # robot emoji
    ]


def collect(paths):
    files = []
    for p in paths:
        p = p.replace(os.sep, "/")
        if os.path.isdir(p):
            files += [f.replace(os.sep, "/") for f in glob.glob(p + "/**/*", recursive=True) if os.path.isfile(f)]
        elif os.path.isfile(p):
            files.append(p)
    return sorted(set(files))


def scan(files):
    pats = credential_patterns()
    markers = ai_markers()
    hits = 0
    for f in files:
        with open(f, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        low = text.lower()
        for rx, label in pats:
            for m in rx.finditer(text):
                lineno = text.count("\n", 0, m.start()) + 1
                print("CRED_HIT %s:%d %s" % (f, lineno, label))
                hits += 1
        for marker in markers:
            if marker in low:
                print("AI_HIT %s" % f)
                hits += 1
    return hits


def main(argv):
    paths = argv[1:] if len(argv) > 1 else ["skills/common", "tests/skills", "evidence/m4/m4a", "THIRD_PARTY.md"]
    files = collect(paths)
    hits = scan(files)
    print("scan_targets: %d files" % len(files))
    print("total_hits=%d" % hits)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
