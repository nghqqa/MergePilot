"""Unit tests for harden_orchestrate.py (end-to-end argv + rollback + secret)."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "tools", "hiclab"))

import harden_orchestrate
import worker_argv

INSPECT = {
    "Name": "/hiclaw-worker-fixer",
    "Config": {
        "Image": "sha256:img1",
        "Env": ["PATH=/usr/bin", "HOME=/root", "INTERNAL_VAL=xyz123val"],
        "Entrypoint": ["/entry.sh"],
        "Cmd": ["run"],
        "User": "root",
        "WorkingDir": "/root",
        "Labels": {"app": "hiclaw"},
        "Healthcheck": {"Test": ["CMD-SHELL", "true"], "Interval": 10000000000},
    },
    "HostConfig": {
        "NetworkMode": "hiclab-net",
        "RestartPolicy": {"Name": "unless-stopped"},
        "Binds": ["/var/run/docker.sock:/var/run/docker.sock"],
        "CapAdd": ["SYS_PTRACE"],
    },
    "NetworkSettings": {"Networks": {"hiclab-net": {"Aliases": ["fixer"]}}},
}


class TestOrchestrate(unittest.TestCase):
    def _orchestrate(self, inspect=INSPECT, **kw):
        rb_written = []
        ef_written = []

        def rb_writer(path, content, mode):
            rb_written.append((path, content, mode))

        def ef_writer(path, content, mode):
            ef_written.append((path, content, mode))

        result = harden_orchestrate.orchestrate(
            inspect, "worker", "fixer", "run1",
            shm_dir="/dev/shm",
            rollback_writer=rb_writer, env_writer=ef_writer,
            rng_fn=lambda: "tok", **kw)
        return result, rb_written, ef_written

    def test_returns_argv_and_paths(self):
        result, rb, ef = self._orchestrate()
        self.assertIn("argv", result)
        self.assertTrue(result["rollback_path"].startswith("/dev/shm/"))
        self.assertTrue(result["envfile_path"].startswith("/dev/shm/"))

    def test_rollback_saved_0600_with_full_inspect(self):
        _result, rb, _ef = self._orchestrate()
        self.assertEqual(rb[0][2], 0o600)
        saved = json.loads(rb[0][1])
        self.assertEqual(saved["Config"]["Image"], "sha256:img1")

    def test_env_file_0600_contains_auth_env(self):
        _result, _rb, ef = self._orchestrate()
        self.assertEqual(ef[0][2], 0o600)
        content = ef[0][1].decode("utf-8")
        self.assertIn("INTERNAL_VAL=xyz123val", content)
        self.assertIn("PATH=/usr/bin", content)

    def test_argv_no_inline_secret(self):
        result, _rb, _ef = self._orchestrate()
        self.assertTrue(harden_orchestrate.verify_no_secret_in_argv(result))
        self.assertNotIn("xyz123val", result["argv"])
        self.assertNotIn("-e", result["argv"])

    def test_argv_uses_env_file(self):
        result, _rb, _ef = self._orchestrate()
        self.assertIn("--env-file", result["argv"])

    def test_full_contract_preserved(self):
        result, _rb, _ef = self._orchestrate()
        argv = result["argv"]
        self.assertIn("--entrypoint", argv)
        self.assertIn("--cap-add", argv)
        self.assertIn("--network", argv)
        self.assertIn("sha256:img1", argv)

    def test_accepts_inspect_list(self):
        """docker inspect returns a list; orchestrate must normalize."""
        result, _rb, _ef = self._orchestrate(inspect=[INSPECT])
        self.assertIn("sha256:img1", result["argv"])

    def test_missing_image_raises(self):
        with self.assertRaises(ValueError):
            harden_orchestrate.orchestrate(
                {"Name": "/x", "Config": {}}, "worker", "x", "r",
                rollback_writer=lambda p, c, m: None,
                env_writer=lambda p, c, m: None,
                rng_fn=lambda: "t")


if __name__ == "__main__":
    unittest.main()
