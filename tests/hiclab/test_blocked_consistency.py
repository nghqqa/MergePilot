"""Unit tests: BLOCKED_UPSTREAM status is consistent with docs and claims.

Verifies that the codebase does NOT overclaim a closed worker-creation chain.
"""
import os
import pathlib
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HICLAB = pathlib.Path(HERE).parent.parent / "tools" / "hiclab"


class TestBlockedUpstreamDoc(unittest.TestCase):
    def _doc(self):
        return (HICLAB / "UPSTREAM_BLOCKED.md").read_text(encoding="utf-8")

    def test_states_blocked_upstream(self):
        text = self._doc()
        self.assertIn("BLOCKED_UPSTREAM", text)

    def test_states_option_b(self):
        text = self._doc()
        self.assertIn("option b", text.lower())

    def test_forbids_manager_auto_create(self):
        text = self._doc()
        self.assertIn("FORBIDDEN", text)
        self.assertIn("Manager", text)

    def test_states_proxy_not_implemented(self):
        text = self._doc()
        self.assertIn("No socket-proxy daemon is implemented", text)

    def test_clarifies_harden_policy_is_pure_strategy(self):
        text = self._doc()
        self.assertIn("PURE", text)
        self.assertIn("STRATEGY", text)
        self.assertIn("NOT a deployed proxy", text)

    def test_restricts_to_create_hardened_worker(self):
        text = self._doc()
        self.assertIn("create_hardened_worker.sh", text)
        self.assertIn("manual", text.lower())

    def test_narrowed_scope_claim(self):
        """The delivery must be named with the narrowed scope, not
        'complete worker storage hardening'."""
        text = self._doc().lower()
        self.assertIn("disk guard + guarded base-service startup", text)
        self.assertIn("cleanup tooling candidate", text)
        self.assertIn("not \"complete worker storage hardening\"", text)

    def test_programmatic_enforcement_documented(self):
        """The doc must state the hiclaw-manager block is programmatic.

        The exact emit string format changed in commit 3c96701 (the
        ``Programmatic enforcement'' section was rewritten to cover both
        hiclaw-controller and hiclaw-manager). The test now asserts the
        substantive content: the block is PROGRAMMATIC (via
        manager_start_allowed), names hiclaw-manager, and is tied to the
        BLOCKED_UPSTREAM status. This is stronger than the old single
        concatenated-string assertion, which had drifted from the doc.
        """
        text = self._doc()
        self.assertIn("manager_start_allowed", text)
        self.assertIn("PROGRAMMATICALLY", text)
        self.assertIn("hiclaw-manager", text)
        self.assertIn("BLOCKED_UPSTREAM", text)

    def test_d2b3_blocked(self):
        text = self._doc()
        self.assertIn("D2B-3", text)
        self.assertIn("non-runnable", text)


class TestNoOverclaimInCode(unittest.TestCase):
    """No source file should claim the worker-creation chain is closed."""

    def _source_files(self):
        files = []
        for p in HICLAB.rglob("*"):
            if p.is_file() and p.suffix in (".py", ".sh"):
                files.append(p)
        return files

    def test_no_closed_chain_claim(self):
        banned = [
            "creation chain closed",
            "real creation chain intercepted",
            "proxy deployed",
            "proxy is live",
        ]
        for p in self._source_files():
            text = p.read_text(encoding="utf-8").lower()
            for phrase in banned:
                self.assertNotIn(
                    phrase, text,
                    "%s overclaims: %r" % (p.name, phrase))

    def test_harden_policy_docstring_honest(self):
        text = (HICLAB / "harden_policy.py").read_text(encoding="utf-8")
        # must describe itself as a strategy/policy module, not a live interceptor
        self.assertTrue(
            "policy" in text.lower() and "not" in text.lower(),
            "harden_policy.py must be clearly described as non-deployed")


if __name__ == "__main__":
    unittest.main()
