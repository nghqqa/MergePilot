"""Hygiene gate for hiclab hardening files.

Scans tools/hiclab/* and tests/hiclab/* for:
  * BOM, CR, missing final LF, trailing whitespace
  * AI-identifier markers (assembled to avoid self-match)
  * literal credentials (reuses skills.common.runtime.redact patterns)
  * dangerous broad commands (docker prune family, recursive root removal)

This is a self-contained local gate -- no WSL/Docker/MinIO needed.
"""
import os
import pathlib
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from skills.common.runtime.redact import credential_patterns  # noqa: E402

HICLAB_DIR = pathlib.Path(ROOT) / "tools" / "hiclab"
TESTS_DIR = pathlib.Path(HERE)
TEXT_SUFFIXES = {".py", ".sh"}


def _ai_markers():
    # Assembled via concatenation so this source file does not self-match.
    return [
        "co-" + "authored-" + "by",
        "cl" + "aude",
        "anth" + "ropic",
        "generated" + " with",
        chr(0x1F916),
    ]


def _dangerous_patterns():
    return [
        re.compile(r"docker\s+system\s+prune", re.IGNORECASE),
        re.compile(r"docker\s+rm\s+-[a-z]*f[a-z]*\s+\$\(docker\s+ps",
                   re.IGNORECASE),
        # rm -rf /  or  rm -rf /*  (bare root; NOT /root/... which has a char
        # after the slash)
        re.compile(r"(^|[;&|`])\s*rm\s+-rf\s+/(\s|$|\*)"),
        re.compile(r"docker\s+volume\s+prune", re.IGNORECASE),
        re.compile(r"docker\s+image\s+prune", re.IGNORECASE),
    ]


def _all_files():
    files = []
    for d in (HICLAB_DIR, TESTS_DIR):
        for p in sorted(d.rglob("*")):
            if p.is_file() and p.suffix in TEXT_SUFFIXES:
                files.append(p)
    return sorted(set(files))


class TestFileHygiene(unittest.TestCase):
    def test_no_bom(self):
        for p in _all_files():
            self.assertFalse(
                p.read_bytes().startswith(b"\xef\xbb\xbf"), "BOM in %s" % p)

    def test_no_cr(self):
        for p in _all_files():
            self.assertEqual(
                p.read_bytes().count(b"\r"), 0, "CR in %s" % p)

    def test_final_lf(self):
        for p in _all_files():
            data = p.read_bytes()
            if data:
                self.assertTrue(data.endswith(b"\n"), "NO_FINAL_LF %s" % p)

    def test_no_trailing_ws(self):
        for p in _all_files():
            for i, line in enumerate(p.read_bytes().splitlines(), 1):
                self.assertEqual(
                    line, line.rstrip(b" \t"),
                    "TRAILING_WS %s:%d" % (p, i))

    def test_no_ai_markers(self):
        markers = _ai_markers()
        for p in _all_files():
            text = p.read_text(encoding="utf-8", errors="replace").lower()
            for m in markers:
                self.assertNotIn(m, text, "AI marker %r in %s" % (m, p))

    def test_no_literal_credentials(self):
        pats = credential_patterns()
        for p in _all_files():
            text = p.read_text(encoding="utf-8", errors="replace")
            for rx, label in pats:
                hits = rx.findall(text)
                self.assertEqual(
                    len(hits), 0,
                    "%s pattern %r found %r in %s" % (label, rx.pattern, hits[:3], p))

    def test_no_dangerous_commands(self):
        pats = _dangerous_patterns()
        for p in _all_files():
            text = p.read_text(encoding="utf-8", errors="replace")
            for rx in pats:
                m = rx.search(text)
                self.assertIsNone(
                    m, "dangerous cmd %r in %s at: %s" % (rx.pattern, p,
                                                          text[m.start():m.end()] if m else ""))


if __name__ == "__main__":
    unittest.main()
