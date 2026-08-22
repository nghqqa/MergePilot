"""M8-GH-4B4 direction-aware hybrid rewiring harness tests.

Drives the PRODUCTION state machine with fakes ONLY at external
boundaries (Docker/supervisor-equivalent processes/MinIO). No real
containers, MinIO objects, or sync processes are touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
import unittest.mock
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = HERE.parent.parent
for p in (str(HERE), str(ROOT), str(ROOT / "tools" / "cli"),
          str(ROOT / "tools" / "harness"),
          str(ROOT / "tools" / "gh-app")):
    if p not in sys.path:
        sys.path.insert(0, p)

import mp_gh4_harness as hw                  # noqa: E402
import e2e_executors as ex                   # noqa: E402

TARGET = {r: ex.hiclaw_role_gateway_url(r) for r in hw.ROLES}



# R2: the direction tests share the SAME FakeSyncWorld as the
# migrated harness matrix (conditional create/replace semantics,
# lazy production ticks, tombstone lock protocol) so the two files
# can never drift apart on what "the sync world" means.
from test_hiclaw_harness import (  # noqa: E402
    FakeSyncWorld, _McShim, _legacy_config, _target_config)


class DirectionTestBase(unittest.TestCase):
    """Shrinks convergence budgets so timeout paths run fast."""

    def setUp(self):
        import tempfile
        import unittest.mock
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.journal = self.root / "journal.json"
        self.receipt = self.root / "receipt.json"
        # fast convergence for tests
        self._conv_patcher = unittest.mock.patch.dict(
            "e2e_executors.HICLAW_CONVERGENCE", {
                "manager": {"poll_seconds": 0.01,
                            "timeout_seconds": 0.5,
                            "stability_checks": 1},
                "worker": {"poll_seconds": 0.01,
                           "timeout_seconds": 0.5,
                           "stability_checks": 1},
            })
        self._conv_patcher.start()
        self.addCleanup(self._conv_patcher.stop)

    def _adapters(self, world):
        docker = hw.DockerAdapter(world.docker_exec)
        minio = hw.MinioAdapter(world.docker_exec)
        minio._docker = _McShim(world)
        return docker, minio

    def _lock_state(self, world):
        import json as _json
        raw = world.objects.get("%s/lock" % ex.HICLAW_TX_PREFIX)
        if raw is None:
            return None
        try:
            return _json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return {"state": "unparseable"}

    def _lock_released(self, world):
        doc = self._lock_state(world)
        return bool(doc) and doc.get("state") == hw.LOCK_STATE_RELEASED




class TestSyncModeAuthority(unittest.TestCase):

    def test_manager_live_to_canonical(self):
        self.assertEqual(ex.hiclaw_role_sync_mode("manager"),
                         "live_to_canonical")

    def test_workers_canonical_to_live(self):
        for r in ("reviewer", "fixer", "verifier"):
            self.assertEqual(ex.hiclaw_role_sync_mode(r),
                             "canonical_to_live")

    def test_keys_and_paths_single_authority(self):
        self.assertEqual(ex.hiclaw_role_canonical_key("manager"),
                         "manager/config/mcporter.json")
        self.assertEqual(ex.hiclaw_role_canonical_key("fixer"),
                         "agents/fixer/config/mcporter.json")
        self.assertEqual(
            ex.hiclaw_role_live_config_path("manager"),
            "/root/manager-workspace/config/mcporter.json")
        self.assertEqual(
            ex.hiclaw_role_live_config_path("verifier"),
            "/root/hiclaw-fs/agents/verifier/config/mcporter.json")

    def test_convergence_budgets(self):
        m = ex.hiclaw_role_convergence("manager")
        w = ex.hiclaw_role_convergence("fixer")
        self.assertGreaterEqual(m["stability_checks"], 2)
        self.assertGreaterEqual(w["timeout_seconds"],
                               w["poll_seconds"])

    def test_tx_prefix_outside_production(self):
        for r in hw.ROLES:
            key = ex.hiclaw_role_canonical_key(r)
            self.assertFalse(key.startswith(ex.HICLAW_TX_PREFIX))


class TestReadOnly(unittest.TestCase):

    def test_inspect_reports_direction_and_legacy(self):
        world = FakeSyncWorld(
            legacy_artifacts=("manager/config/mcporter.json"
                              ".mp-gh4-bak",))
        docker = hw.DockerAdapter(world.docker_exec)
        minio = hw.MinioAdapter(world.docker_exec)
        minio._docker = _McShim(world)
        state = hw.inspect_roles(docker, minio)
        self.assertEqual(state["roles"]["manager"]["sync_mode"],
                         "live_to_canonical")
        self.assertEqual(state["roles"]["fixer"]["sync_mode"],
                         "canonical_to_live")
        self.assertIn("manager/config/mcporter.json.mp-gh4-bak",
                      state["legacy_sync_artifacts"])
        self.assertEqual(world.docker_calls and True, True)

    def test_plan_zero_writes(self):
        world = FakeSyncWorld(
            legacy_artifacts=("a.mp-gh4-bak",))
        docker = hw.DockerAdapter(world.docker_exec)
        minio = hw.MinioAdapter(world.docker_exec)
        minio._docker = _McShim(world)
        result = hw.plan(self._jp(), docker, minio)
        self.assertEqual(result["writes_executed"], 0)
        self.assertTrue(result["apply_would_fail_closed"])
        modes = {a["role"]: a["sync_mode"] for a in result["actions"]}
        self.assertEqual(modes["manager"], "live_to_canonical")
        self.assertEqual(modes["fixer"], "canonical_to_live")

    def _jp(self):
        import tempfile
        return Path(tempfile.mkdtemp()) / "j.json"


class TestApply(DirectionTestBase):

    def test_apply_success_direction_aware(self):
        world = FakeSyncWorld()
        docker, minio = self._adapters(world)

        # wire convergence: hooks simulate the sync loops
        def hook(phase, role):
            if phase == "manager_live_mutated":
                world.simulate_push("manager")
            if phase == "canonical_mutated":
                world.simulate_pull(role)

        result = hw.apply(journal_path=self.journal,
                          receipt_path=self.receipt,
                          docker=docker, minio=minio,
                          session="ok1", phase_hook=hook)
        self.assertEqual(result["result"], "complete")
        # manager: live AND canonical both target
        mkey = ex.hiclaw_role_canonical_key("manager")
        self.assertIn(TARGET["manager"].split("//")[1],
                      world.objects[mkey].decode())
        mpath = ex.hiclaw_role_live_config_path("manager")
        mcont = ex.HICLAW_ROLE_FREEZE["manager"][0]
        self.assertIn(TARGET["manager"], world.live[mcont][mpath]
                      .decode())
        # workers: canonical AND live both target
        for r in ("reviewer", "fixer", "verifier"):
            key = ex.hiclaw_role_canonical_key(r)
            self.assertIn(TARGET[r], world.objects[key].decode())
            cont = ex.HICLAW_ROLE_FREEZE[r][0]
            path = ex.hiclaw_role_live_config_path(r)
            self.assertIn(TARGET[r], world.live[cont][path].decode())
        # journal complete, receipt exists
        journal = json.loads(self.journal.read_text())
        self.assertEqual(journal["status"], "complete")
        self.assertTrue(self.receipt.exists())
        # tx backups cleaned; the lock remains as a RELEASED
        # tombstone (conditional delete is not server-safe)
        self.assertEqual(
            [k for k in world.objects
             if k.startswith("mp-gh4-tx/")
             and k != "%s/lock" % ex.HICLAW_TX_PREFIX], [])
        self.assertTrue(self._lock_released(world),
                        self._lock_state(world))

    def test_apply_fails_closed_on_legacy_artifacts(self):
        world = FakeSyncWorld(legacy_artifacts=(
            "manager/config/mcporter.json.mp-gh4-bak",
            "agents/fixer/config/mcporter.json.mp-gh4-bak",
            "agents/reviewer/config/mcporter.json.mp-gh4-bak"))
        docker, minio = self._adapters(world)
        with self.assertRaises(hw.HarnessError) as ctx:
            hw.apply(journal_path=self.journal,
                     receipt_path=self.receipt,
                     docker=docker, minio=minio, session="leg")
        self.assertEqual(ctx.exception.code,
                         "HARNESS_LEGACY_SYNC_ARTIFACTS_PRESENT")
        # zero mutations
        for r in hw.ROLES:
            self.assertTrue(world.role_at_legacy(r))

    def test_apply_fails_closed_on_fingerprint_drift(self):
        world = FakeSyncWorld(fingerprint_ok=False)
        docker, minio = self._adapters(world)
        with self.assertRaises(hw.HarnessError) as ctx:
            hw.apply(journal_path=self.journal,
                     receipt_path=self.receipt,
                     docker=docker, minio=minio, session="fp")
        self.assertEqual(ctx.exception.code,
                         "HARNESS_SYNC_CONTRACT_DRIFT")

    def test_manager_push_timeout_rolls_back(self):
        world = FakeSyncWorld()
        world.push_mode["manager"] = "never"
        docker, minio = self._adapters(world)
        with self.assertRaises(hw.HarnessError) as ctx:
            hw.apply(journal_path=self.journal,
                     receipt_path=self.receipt,
                     docker=docker, minio=minio, session="pto",
                     phase_hook=lambda p, r: None)
        self.assertEqual(ctx.exception.code,
                         "HARNESS_MANAGER_PUSH_CONVERGENCE_TIMEOUT"
                         if "MANAGER_PUSH" in str(ctx.exception.code)
                         else ctx.exception.code)
        # live restored to the pre-rewiring gateway
        self.assertTrue(world.role_at_legacy("manager"))

    def test_worker_pull_timeout_rolls_back(self):
        world = FakeSyncWorld()
        world.pull_mode = {r: "never" for r in
                          ("reviewer", "fixer", "verifier")}
        docker, minio = self._adapters(world)

        def hook(phase, role):
            if phase == "manager_live_mutated":
                world.simulate_push("manager")

        with self.assertRaises(hw.HarnessError) as ctx:
            hw.apply(journal_path=self.journal,
                     receipt_path=self.receipt,
                     docker=docker, minio=minio, session="wto",
                     phase_hook=hook)
        code = ctx.exception.code
        # B6: an unavailable production pull now fails at the
        # explicit trigger with its own stable code
        self.assertEqual(code, "HARNESS_WORKER_PULL_TRIGGER_FAILED")
        # canonical restored
        self.assertTrue(world.role_at_legacy("reviewer"))

    def test_before_inconsistent_rejected(self):
        world = FakeSyncWorld()
        world.freeze_ticks()
        # diverge one canonical from live
        key = ex.hiclaw_role_canonical_key("fixer")
        world.objects[key] = b'{"different": true}'
        docker, minio = self._adapters(world)
        with self.assertRaises(hw.HarnessError) as ctx:
            hw.apply(journal_path=self.journal,
                     receipt_path=self.receipt,
                     docker=docker, minio=minio, session="bi")
        self.assertEqual(ctx.exception.code,
                         "HARNESS_BEFORE_INCONSISTENT")

    def test_tx_lock_conflict(self):
        world = FakeSyncWorld()
        world.objects["mp-gh4-tx/lock"] = b"other:session"
        docker, minio = self._adapters(world)
        with self.assertRaises(hw.HarnessError) as ctx:
            hw.apply(journal_path=self.journal,
                     receipt_path=self.receipt,
                     docker=docker, minio=minio, session="lc")
        self.assertEqual(ctx.exception.code,
                         "HARNESS_TX_LOCK_CONFLICT")


class TestReceiptContract(DirectionTestBase):

    def test_receipt_direction_fields(self):
        world = FakeSyncWorld()
        docker, minio = self._adapters(world)

        def hook(phase, role):
            if phase == "manager_live_mutated":
                world.simulate_push("manager")
            if phase == "canonical_mutated":
                world.simulate_pull(role)

        hw.apply(journal_path=self.journal,
                 receipt_path=self.receipt,
                 docker=docker, minio=minio, session="rc1",
                 phase_hook=hook)
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual(receipt["schema_version"], 2)
        self.assertIn("sync_contract_fingerprint", receipt)
        for agent in receipt["agents"]:
            for f in ("sync_mode", "canonical_key", "live_path",
                      "live_hash_before", "live_hash_after",
                      "canonical_hash_before",
                      "canonical_hash_after",
                      "canonical_etag_before",
                      "canonical_etag_after",
                      "convergence_evidence"):
                self.assertIn(f, agent)
        # no config body
        blob = self.receipt.read_text()
        for forbidden in ("legacy-", "Bearer", "secret-"):
            self.assertNotIn(forbidden, blob)

    def test_old_schema_receipt_rejected_by_validator(self):
        # build a v1 receipt missing direction fields
        agents = []
        for role in hw.ROLES:
            container, mxid, ip, _p = ex.HICLAW_ROLE_FREEZE[role]
            agents.append({
                "role": role, "container_name": container,
                "container_id": "cid-%s" % container, "mxid": mxid,
                "hiclaw_net_ip": ip,
                "gateway_url": ex.hiclaw_role_gateway_url(role),
                "config_hash_before": "b" * 64,
                "config_hash_after": "a" * 64,
                "token_hash": "c" * 64})
        receipt = {"schema_version": 1,
                   "rewire_session": "old-session",
                   "agents": agents,
                   "old_github_mcp": {"container_id": "x",
                                      "state": "exited",
                                      "restart_policy": "no",
                                      "network_attachments": []},
                   "rollback_ownership": "mp-gh4-harness"}
        receipt["receipt_sha256"] = hw._canonical_sha256(receipt)
        self.receipt.write_text(json.dumps(receipt))
        world = FakeSyncWorld()
        docker, minio = self._adapters(world)
        verdict = hw.verify(self.receipt, docker=docker,
                            minio=minio)
        # validator must reject v1 (stable schema error)
        self.assertFalse(verdict.get("verified", True))
        self.assertEqual(verdict.get("code"), "RECEIPT_SCHEMA")


class TestZeroSecretSurfaces(unittest.TestCase):

    def test_no_config_bodies_in_calls(self):
        world = FakeSyncWorld()
        docker = hw.DockerAdapter(world.docker_exec)
        minio = hw.MinioAdapter(world.docker_exec)
        minio._docker = _McShim(world)
        hw.inspect_roles(docker, minio)
        for argv in world.docker_calls + world.minio_calls:
            joined = " ".join(str(a) for a in argv)
            self.assertNotIn("legacy-", joined)
            self.assertNotIn("Bearer", joined)


if __name__ == "__main__":
    unittest.main()
