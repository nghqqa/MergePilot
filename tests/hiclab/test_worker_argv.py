"""Unit tests for worker_argv.py (full contract + secret-safe env-file)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "tools", "hiclab"))

import worker_argv

FULL_INSPECT = {
    "Name": "/hiclaw-worker-fixer",
    "Config": {
        "Image": "sha256:abc123",
        "Env": ["PATH=/usr/bin", "HOME=/root", "INTERNAL_VAL=xyz123val"],
        "Entrypoint": ["/docker-entrypoint.sh"],
        "Cmd": ["supervisord", "-c", "/etc/supervisor/supervisord.conf"],
        "User": "root",
        "WorkingDir": "/root",
        "Hostname": "fixer",
        "Tty": False,
        "Labels": {"maintainer": "hiclaw"},
        "Healthcheck": {
            "Test": ["CMD-SHELL", "curl -f http://localhost/"],
            "Interval": 30000000000,
            "Timeout": 5000000000,
            "Retries": 3,
        },
        "ExposedPorts": {"9000/tcp": {}},
        "StopSignal": "SIGTERM",
        "StopTimeout": 10,
    },
    "HostConfig": {
        "NetworkMode": "hiclab-net",
        "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
        "AutoRemove": False,
        "Binds": ["/var/run/docker.sock:/var/run/docker.sock"],
        "CapAdd": ["SYS_PTRACE"],
        "CapDrop": [],
        "SecurityOpt": ["label=disable"],
        "ShmSize": 67108864,
        "Privileged": False,
    },
    "NetworkSettings": {
        "Networks": {"hiclab-net": {"Aliases": ["fixer"]}},
    },
    "Mounts": [
        {"Type": "bind", "Source": "/opt/data", "Destination": "/data",
         "RW": True, "Propagation": "rprivate"},
    ],
}


class TestPrepareEnvFile(unittest.TestCase):
    def test_writes_0600_to_shm(self):
        written = []

        def writer(path, content, mode):
            written.append((path, content, mode))

        path = worker_argv.prepare_env_file(
            ["FOO=bar", "BAZ=qux"], shm_dir="/dev/shm",
            rng_fn=lambda: "tok1", writer=writer)
        self.assertTrue(path.startswith("/dev/shm/mp-env-tok1"))
        self.assertEqual(written[0][2], 0o600)
        self.assertIn(b"FOO=bar", written[0][1])
        self.assertIn(b"BAZ=qux", written[0][1])

    def test_content_contains_all_pairs(self):
        written = []

        def writer(path, content, mode):
            written.append(content)

        worker_argv.prepare_env_file(
            ["A=1", "B=2"], shm_dir="/tmp", rng_fn=lambda: "t",
            writer=writer)
        self.assertEqual(written[0], b"A=1\nB=2\n")


class TestSaveRollbackArtifact(unittest.TestCase):
    def test_saves_full_inspect_0600(self):
        written = []

        def writer(path, content, mode):
            written.append((path, content, mode))

        path = worker_argv.save_rollback_artifact(
            "hiclaw-worker-fixer", {"Config": {"Image": "x"}},
            shm_dir="/dev/shm", rng_fn=lambda: "rb1", writer=writer)
        self.assertTrue(path.startswith("/dev/shm/mp-rollback-hiclaw-worker-fixer-rb1"))
        self.assertEqual(written[0][2], 0o600)
        self.assertIn(b'"Image": "x"', written[0][1])


class TestMakeHardening(unittest.TestCase):
    def test_worker_tmpfs(self):
        h = worker_argv.make_hardening("worker", "fixer", "run1")
        paths = [s.split(":")[0] for s in h["tmpfs_mounts"]]
        self.assertIn("/root/hiclaw-fs/agents/fixer/.codex/tmp", paths)
        self.assertIn("/tmp", paths)
        self.assertEqual(h["env_additions"], [])

    def test_manager_tmpfs_and_env(self):
        h = worker_argv.make_hardening("manager", "manager", "mgr")
        paths = [s.split(":")[0] for s in h["tmpfs_mounts"]]
        self.assertIn(worker_argv.MANAGER_NPM_CACHE_PATH, paths)
        self.assertIn(worker_argv.MANAGER_NODE_COMPILE_PATH, paths)
        self.assertIn("NPM_CONFIG_CACHE=" + worker_argv.MANAGER_NPM_CACHE_PATH,
                      h["env_additions"])

    def test_no_home_redirect(self):
        for kind in ("worker", "manager"):
            h = worker_argv.make_hardening(kind, "fixer", "run1")
            for s in h["tmpfs_mounts"]:
                self.assertNotIn("HOME", s)
            for e in h["env_additions"]:
                self.assertFalse(e.startswith("HOME="))


class TestFullContract(unittest.TestCase):
    def _build(self, force_restart_no=True, storage_opt_gib=None):
        h = worker_argv.make_hardening(
            "worker", "fixer", "run1", storage_opt_gib=storage_opt_gib)
        return worker_argv.build_run_argv_from_inspect(
            "hiclaw-worker-fixer", FULL_INSPECT, "/dev/shm/envf", h,
            force_restart_no=force_restart_no)

    def _opts(self, argv, flag):
        return [argv[i + 1] for i, a in enumerate(argv) if a == flag]

    def test_image_and_cmd_last(self):
        argv = self._build()
        self.assertEqual(argv[0], "run")
        self.assertIn("sha256:abc123", argv)
        img_idx = argv.index("sha256:abc123")
        # Cmd args follow the image
        self.assertEqual(argv[img_idx + 1], "supervisord")
        self.assertEqual(argv[img_idx + 2], "-c")

    def test_entrypoint_preserved(self):
        argv = self._build()
        self.assertIn("--entrypoint", argv)
        idx = argv.index("--entrypoint")
        self.assertEqual(argv[idx + 1], "/docker-entrypoint.sh")

    def test_user_workdir_hostname(self):
        argv = self._build()
        self.assertIn("--user", argv)
        self.assertIn("--workdir", argv)
        self.assertIn("--hostname", argv)

    def test_mounts_and_binds_preserved(self):
        argv = self._build()
        mounts = self._opts(argv, "--mount")
        self.assertTrue(any("target=/data" in m for m in mounts), mounts)
        self.assertTrue(any("source=/opt/data" in m for m in mounts), mounts)
        # Binds via -v
        v_opts = self._opts(argv, "-v")
        self.assertIn("/var/run/docker.sock:/var/run/docker.sock", v_opts)

    def test_network_and_aliases(self):
        argv = self._build()
        self.assertIn("--network", argv)
        self.assertIn("--network-alias", argv)
        na = self._opts(argv, "--network-alias")
        self.assertIn("fixer", na)

    def test_caps_security_preserved(self):
        argv = self._build()
        self.assertIn("--cap-add", argv)
        idx = argv.index("--cap-add")
        self.assertEqual(argv[idx + 1], "SYS_PTRACE")
        so = self._opts(argv, "--security-opt")
        self.assertIn("label=disable", so)

    def test_healthcheck_preserved(self):
        argv = self._build()
        self.assertIn("--health-cmd", argv)
        self.assertIn("--health-interval", argv)
        hi = self._opts(argv, "--health-interval")
        self.assertEqual(hi[0], "30s")
        self.assertIn("--health-retries", argv)

    def test_exposed_ports_preserved(self):
        argv = self._build()
        self.assertIn("--expose", argv)
        idx = argv.index("--expose")
        self.assertEqual(argv[idx + 1], "9000/tcp")

    def test_stop_signal_timeout(self):
        argv = self._build()
        self.assertIn("--stop-signal", argv)
        self.assertIn("--stop-timeout", argv)

    def test_labels_merged(self):
        argv = self._build()
        labels = self._opts(argv, "--label")
        self.assertIn("maintainer=hiclaw", labels)  # original
        self.assertIn("com.mergepilot.hardened=1", labels)  # added
        self.assertIn("com.mergepilot.run_id=run1", labels)

    def test_restart_overridden_to_no(self):
        argv = self._build(force_restart_no=True)
        self.assertIn("--restart=no", argv)

    def test_restart_preserved_on_rollback(self):
        argv = self._build(force_restart_no=False)
        self.assertIn("--restart", argv)
        idx = argv.index("--restart")
        self.assertEqual(argv[idx + 1], "unless-stopped")


class TestSecretSafety(unittest.TestCase):
    def test_argv_uses_env_file_not_inline_e(self):
        h = worker_argv.make_hardening("worker", "fixer", "run1")
        argv = worker_argv.build_run_argv_from_inspect(
            "hiclaw-worker-fixer", FULL_INSPECT, "/dev/shm/envf", h)
        self.assertNotIn("-e", argv)
        self.assertIn("--env-file", argv)
        idx = argv.index("--env-file")
        self.assertEqual(argv[idx + 1], "/dev/shm/envf")

    def test_no_secret_value_in_argv(self):
        """The authoritative env value must never appear in the argv."""
        h = worker_argv.make_hardening("worker", "fixer", "run1")
        argv = worker_argv.build_run_argv_from_inspect(
            "hiclaw-worker-fixer", FULL_INSPECT, "/dev/shm/envf", h)
        # INTERNAL_VAL=xyz123val is in Config.Env; must NOT be in argv
        self.assertNotIn("xyz123val", argv)
        self.assertNotIn("INTERNAL_VAL=xyz123val", argv)
        # confirm via helper
        self.assertFalse(
            worker_argv.argv_has_inline_secret(argv, ["xyz123val"]))

    def test_tmpfs_and_storage_opt(self):
        h = worker_argv.make_hardening(
            "worker", "fixer", "run1", storage_opt_gib=10)
        argv = worker_argv.build_run_argv_from_inspect(
            "hiclaw-worker-fixer", FULL_INSPECT, "/dev/shm/envf", h)
        tmpfs = [argv[i + 1] for i, a in enumerate(argv) if a == "--tmpfs"]
        self.assertTrue(any(".codex/tmp" in s for s in tmpfs), tmpfs)
        self.assertTrue(any(s.startswith("/tmp:") for s in tmpfs), tmpfs)
        so = [argv[i + 1] for i, a in enumerate(argv) if a == "--storage-opt"]
        self.assertEqual(so, ["size=10g"])

    def test_storage_opt_omitted_when_none(self):
        h = worker_argv.make_hardening("worker", "fixer", "run1")
        argv = worker_argv.build_run_argv_from_inspect(
            "hiclaw-worker-fixer", FULL_INSPECT, "/dev/shm/envf", h)
        self.assertNotIn("--storage-opt", argv)


if __name__ == "__main__":
    unittest.main()
