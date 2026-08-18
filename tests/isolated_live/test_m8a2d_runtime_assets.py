#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M8-A2-d — versioned runtime asset contracts and helper security tests.

Proves the real-E2E worker contract is now fully reproducible from the
repository alone: SOUL contracts, the Manager M4F_RUN state-machine
contract, hardened GitHub MCP helpers, and the deploy tool — with no
fixture hardcoding, no secrets, and no out-of-repo dependencies
(D:\\goai\\workers etc.). Direct behavior tests against production
helper/deploy logic; source-string assertions are supplementary only.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = _HERE.parent.parent
AT = ROOT / "tools" / "agentteams"
for _p in (str(AT), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import gh_read  # noqa: E402
import gh_fix_branch  # noqa: E402
import deploy_worker_contracts as dep  # noqa: E402

SOULS = {name: (ROOT / "config" / "souls" / name / "SOUL.md").read_text(
    encoding="utf-8") for name in ("reviewer", "fixer", "verifier")}
MGR = (ROOT / "config" / "souls" / "manager-state-machine.md").read_text(
    encoding="utf-8")


def _load(alias):
    """Load a FRESH instance of the real fix helper under a unique module
    name so per-test _mc patches never leak into the shared import."""
    spec = importlib.util.spec_from_file_location(
        alias, AT / "gh_fix_branch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestSoulAssetContracts(unittest.TestCase):

    def test_souls_have_versioned_completion_protocol(self):
        self.assertIn("TASK_COMPLETED: <run_id>-review", SOULS["reviewer"])
        self.assertIn("TASK_COMPLETED: <run_id>-fix", SOULS["fixer"])
        self.assertIn("TASK_COMPLETED: <run_id>-verify", SOULS["verifier"])
        self.assertIn("VERDICT=PASS", SOULS["verifier"])
        # role correctness: only verifier mentions VERDICT, only fixer writes
        self.assertNotIn("VERDICT=", SOULS["reviewer"])
        self.assertNotIn("gh_fix_branch", SOULS["reviewer"])
        self.assertNotIn("gh_fix_branch", SOULS["verifier"])
        self.assertIn("gh_fix_branch", SOULS["fixer"])

    def test_souls_forbid_example_pollution_and_enforce_fresh_message(self):
        for name, text in SOULS.items():
            self.assertIn("示例", text, name)
            self.assertIn("独立消息", text, name)
            self.assertIn("代码块", text, name)

    def test_no_fixture_hardcoding_anywhere(self):
        all_assets = "\n".join(list(SOULS.values()) + [MGR]) + \
            (AT / "gh_read.py").read_text(encoding="utf-8") + \
            (AT / "gh_fix_branch.py").read_text(encoding="utf-8") + \
            (AT / "deploy_worker_contracts.py").read_text(encoding="utf-8")
        for forbidden in ("#625", "m8a2m-d2", "e2e-a2d", "MergePilot-e2e-fixture",
                          "42176c6d", "e2e/calc.py"):
            self.assertNotIn(forbidden, all_assets, forbidden)

    def test_manager_contract_semantics(self):
        self.assertIn("VERDICT=PASS", MGR)
        self.assertIn("M4F_RUN", MGR)
        # only-after-verify-PASS, idempotent, fail-closed, role boundary
        self.assertIn("仅在 verifier 完成且 VERDICT=PASS 后", MGR)
        self.assertIn("至多一条", MGR)
        self.assertIn("FAIL/BLOCKED", MGR)
        self.assertIn("不得代发", MGR)
        self.assertIn("不是自主任务分解", MGR)
        # deployment config is explicit, not a silent default
        self.assertIn("requireMention", MGR)
        self.assertIn("显式", MGR)


class TestHelperValidation(unittest.TestCase):
    """Arg-validation deny-by-default behavior of the production helpers."""

    def _run_read(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            with patch.object(sys, "argv", ["gh_read.py", *argv]):
                try:
                    rc = gh_read.main(["gh_read.py", *argv])
                except SystemExit as e:
                    rc = e.code
        return rc

    def test_read_rejects_bad_repo_and_pr(self):
        for bad in (("pr", "bad owner!", "repo", "1"),
                    ("pr", "owner", "repo", "0"),
                    ("pr", "owner", "repo", "-3"),
                    ("pr", "owner", "repo", "abc")):
            self.assertNotEqual(self._run_read(*bad), 0, bad)

    def test_read_rejects_protected_branch_and_bad_path(self):
        self.assertNotEqual(
            self._run_read("file", "o", "r", "x", "main"), 0)
        self.assertNotEqual(
            self._run_read("file", "o", "r", "../etc/passwd", "br"), 0)
        self.assertNotEqual(
            self._run_read("file", "o", "r", "/abs/path", "br"), 0)

    def test_fix_rejects_protected_and_malformed(self):
        for bad in (["x", "owner", "repo", "1", "main", "p", "f", "msg"],
                    ["x", "owner", "repo", "abc", "br", "p", "f", "msg"],
                    ["x", "bad owner", "repo", "1", "br", "p", "f", "msg"],
                    ["x", "owner", "repo", "1", "br", "../x", "f", "msg"]):
            with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                try:
                    gh_fix_branch.main(bad)
                    rc = 0
                except SystemExit as e:
                    rc = e.code
            self.assertNotEqual(rc, 0, bad)

    def test_fix_head_branch_mismatch_rejected_before_write(self):
        calls = []

        def fake_mc(*tool_args):
            calls.append(tool_args)
            if tool_args[0] == "github.get_pull_request":
                return '{"number": 7, "head": {"ref": "other-branch"}}'
            return ""

        mod = _load("gh_fix_branch_probe")
        with patch.object(mod, "_mc", side_effect=fake_mc):
            with redirect_stderr(io.StringIO()):
                try:
                    mod.main(["x", "owner", "repo", "7", "br-fix",
                              "path/file.py", __file__, "fix: msg"])
                    rc = 0
                except SystemExit:
                    rc = 2
        self.assertNotEqual(rc, 0)
        # only the PR read happened — NO write call
        self.assertTrue(all("create_or_update" not in " ".join(c) for c in calls))

    def test_fix_write_without_readback_confirm_fails(self):
        state = {"sha_seen": False}

        def fake_mc(*tool_args):
            joined = " ".join(tool_args)
            if "get_pull_request" in joined and "pull_number" in joined:
                return '{"head": {"ref": "br-fix", "repo": {"full_name": "owner/repo"}}}'
            if "get_file_contents" in joined:
                if not state["sha_seen"]:
                    state["sha_seen"] = True
                    return "content...\nSHA: " + "a" * 40
                return "DIFFERENT content after write"
            return "ok"

        mod = _load("gh_fix_branch_probe2")
        with patch.object(mod, "_mc", side_effect=fake_mc):
            with redirect_stderr(io.StringIO()):
                try:
                    mod.main(["x", "owner", "repo", "7", "br-fix",
                              "p", __file__, "fix: msg"])
                    rc = 0
                except SystemExit:
                    rc = 2
        self.assertNotEqual(rc, 0)  # read-back mismatch is fatal

    def test_fix_happy_path_confirms(self):
        state = {"reads": 0}

        def fake_mc(*tool_args):
            joined = " ".join(tool_args)
            if "get_pull_request" in joined and "pull_number" in joined:
                return '{"head": {"ref": "br-fix", "repo": {"full_name": "owner/repo"}}}'
            if "get_file_contents" in joined:
                state["reads"] += 1
                if state["reads"] == 1:
                    return "old\nSHA: " + "a" * 40
                return "FIXED CONTENT"  # matches content file below
            return "commit ok"

        content = Path(__file__).parent / "_a2d_fix_content.tmp"
        content.write_text("FIXED CONTENT", encoding="utf-8")
        try:
            mod = _load("gh_fix_branch_probe3")
            with patch.object(mod, "_mc", side_effect=fake_mc):
                out = io.StringIO()
                with redirect_stdout(out), redirect_stderr(io.StringIO()):
                    rc = mod.main(["x", "owner", "repo", "7", "br-fix",
                                   "p", str(content), "fix: msg"])
            self.assertEqual(rc, 0)
            self.assertIn("confirmed", out.getvalue())
        finally:
            content.unlink()

    def test_helper_sources_have_no_secrets_or_local_paths(self):
        for f in ("gh_read.py", "gh_fix_branch.py",
                  "deploy_worker_contracts.py"):
            text = (AT / f).read_text(encoding="utf-8")
            # credential VALUE shapes (the redaction regex pattern itself
            # is expected and allowed)
            import re as _re
            self.assertFalse(_re.search(r"ghp_[A-Za-z0-9]{20,}", text), f)
            self.assertFalse(_re.search(r"github_pat_[A-Za-z0-9_]{20,}", text), f)
            for pat in ("PGPASSWORD", "password=", "postgresql://"):
                self.assertNotIn(pat, text, (f, pat))


class TestDeployTool(unittest.TestCase):

    def test_manifest_covers_all_assets_with_hashes(self):
        manifest = dep.build_manifest()
        srcs = {m["repo_source"] for m in manifest}
        self.assertIn("config/souls/reviewer/SOUL.md", srcs)
        self.assertIn("config/souls/manager-state-machine.md", srcs)
        self.assertIn("tools/agentteams/gh_read.py", srcs)
        self.assertIn("tools/agentteams/gh_fix_branch.py", srcs)
        # helpers deploy to all three workers / manager gets its contract
        readers = [m for m in manifest if m["repo_source"].endswith("gh_read.py")]
        self.assertEqual(len(readers), 3)
        for m in manifest:
            self.assertRegex(m["sha256"], r"^[a-f0-9]{64}$")

    def test_no_out_of_repo_worker_dependency(self):
        text = (ROOT / "tools" / "deploy-souls-and-helpers.sh").read_text(
            encoding="utf-8") if (ROOT / "tools" / "deploy-souls-and-helpers.sh").exists() else ""
        for f in ("deploy_worker_contracts.py",):
            text += (AT / f).read_text(encoding="utf-8")
        self.assertNotIn("D:\\goai\\workers", text)
        self.assertNotIn("/mnt/d/goai/workers", text)

    def test_dry_run_has_no_side_effects(self):
        out = io.StringIO()
        with patch.object(dep.subprocess, "run") as sr:
            with redirect_stdout(out):
                rc = dep.main.__wrapped__() if hasattr(dep.main, "__wrapped__") \
                    else _run_deploy_dry()
        self.assertEqual(rc, 0)
        self.assertIn("dry-run", out.getvalue())
        self.assertIn("requireMention", out.getvalue())
        # dry-run never shells out
        self.assertFalse(sr.called)

    def test_apply_requires_minio_secret_env(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("HICLAW_MINIO_USER", "HICLAW_MINIO_PASS")}
        with patch.object(dep, "preflight", return_value=None), \
             patch.object(dep.subprocess, "run") as sr, \
             patch.dict(os.environ, env, clear=True):
            buf = io.StringIO()
            with redirect_stderr(buf):
                rc = _main_apply()
        self.assertEqual(rc, 3)
        self.assertFalse(sr.called)


def _run_deploy_dry():
    with patch.object(sys, "argv", ["deploy_worker_contracts.py"]):
        return dep.main()


def _main_apply():
    with patch.object(sys, "argv", ["deploy_worker_contracts.py", "--apply"]):
        return dep.main()


if __name__ == "__main__":
    unittest.main()


class TestManagedMergeContract(unittest.TestCase):
    """§2 pure-function contract: fail-closed on every incident shape."""

    BASE = "line one\n# Manager Agent Workspace\n" + "x" * 80

    def test_rejects_incident_shapes(self):
        cases = ["", "   \n  \t ", "x" * 10, self.BASE + dep._MARK_BEGIN,
                 self.BASE + dep._MARK_END,
                 self.BASE + dep._MARK_BEGIN + dep._MARK_BEGIN + dep._MARK_END,
                 dep._MARK_END + self.BASE + dep._MARK_BEGIN,
                 self.BASE.replace("x", "\x00", 1)]
        for bad in cases:
            with self.assertRaises(ValueError, msg=bad[:30]):
                dep.merge_managed_agents(bad, "contract")
        with self.assertRaises(ValueError):
            dep.merge_managed_agents(self.BASE, "")
        with self.assertRaises(ValueError):
            dep.merge_managed_agents(self.BASE, dep._MARK_BEGIN)

    def test_append_then_replace_idempotent_and_external_exact(self):
        m1 = dep.merge_managed_agents(self.BASE, "c1")
        self.assertEqual(m1.count(dep._MARK_BEGIN), 1)
        self.assertIn(self.BASE.rstrip("\n"), m1)
        m2 = dep.merge_managed_agents(m1, "c2")
        self.assertIn("c2", m2)
        self.assertNotIn("c1", m2)
        self.assertEqual(dep.merge_managed_agents(m2, "c2"), m2)
        # marker-external newline-normalized equality
        import re as _re
        def ext(t):
            i = t.index(dep._MARK_BEGIN)
            j = t.index(dep._MARK_END) + len(dep._MARK_END)
            return _re.sub(r"\n+", "\n", t[:i] + t[j:]).strip("\n")
        self.assertEqual(ext(m2), _re.sub(r"\n+", "\n", self.BASE).strip("\n"))


class TestDeployTransaction(unittest.TestCase):
    """§6 incident regression: mocked _sh sequences verify the actual
    backup/replace/verify/rollback command flow (not error strings)."""

    BASE = "line one\n# Manager Agent Workspace\n" + "x" * 80

    def _fake_sh_factory(self, container_state, fail_at=None):
        calls = []

        def fake_sh(args, **kw):
            calls.append(list(args))
            key = " ".join(args[:4])
            if fail_at and fail_at in " ".join(args):
                class R: returncode = 1; stdout = ""; stderr = "injected"
                return R()
            joined = " ".join(args)
            if args[:2] == ["docker", "exec"] and args[3] == "sha256sum":
                path = args[4]
                content = container_state.get(path)
                if content is None:
                    class R: returncode = 1; stdout = ""; stderr = ""
                    return R()
                import hashlib as _h
                class R:
                    returncode = 0
                    stdout = _h.sha256(content.encode()).hexdigest() + "  " + path
                return R
            if args[:3] == ["docker", "exec", container_state["name"]] \
                    and args[3] == "cat" and args[4] in container_state:
                class R:
                    returncode = 0
                    stdout = container_state[args[4]]
                return R
            if args[:2] == ["docker", "exec"] and args[3] == "cp":
                src, dst = args[4], args[5]
                if src in container_state and dst in container_state:
                    container_state[dst] = container_state[src]
                elif src in container_state:
                    container_state[dst] = container_state[src]
                class R: returncode = 0; stdout = ""; stderr = ""
                return R
            if args[:2] == ["docker", "exec"] and args[3] == "mv":
                container_state[args[5]] = container_state.pop(args[4], None)
                class R: returncode = 0; stdout = ""; stderr = ""
                return R
            class R: returncode = 0; stdout = ""; stderr = ""
            return R
        return fake_sh, calls, container_state

    def _managed_item(self):
        return {"repo_source": "config/souls/manager-state-machine.md",
                "sha256": dep._sha256_local(
                    dep.REPO_ROOT / "config/souls/manager-state-machine.md"),
                "minio_dest": "hiclaw-storage/manager/AGENTS.md",
                "container": "test-ctr",
                "container_dest": "/root/AGENTS.md", "kind": "managed"}

    def _minio_stub(self, state):
        class M:
            def __init__(self): self.objs = {}
            def exists(self, obj): return obj in self.objs
            def write_verified(self, obj, path):
                state[obj] = path.read_text(encoding="utf-8")
                self.objs[obj] = True
                return True
            def read_to_host(self, obj, path):
                if obj not in state:
                    return False
                path.write_text(state[obj], encoding="utf-8")
                return True
            def delete(self, obj):
                state.pop(obj, None); self.objs.pop(obj, None)
                return True
        return M

    def test_managed_apply_rollback_on_replace_failure(self):
        state = {"name": "test-ctr",
                 "/root/AGENTS.md": TestDeployTransaction.BASE * 2}
        fake, calls, _ = self._fake_sh_factory(state, fail_at="mv ")
        M = self._minio_stub(state)
        item = self._managed_item()
        with patch.object(dep, "_sh", side_effect=fake):
            try:
                dep.deploy_asset(item, M())
                self.fail("expected DeployError")
            except dep.DeployError:
                pass
        # target untouched (mv failed → rollback of in-flight asset)
        self.assertIn("# Manager Agent Workspace", state["/root/AGENTS.md"])
        self.assertNotIn(dep._MARK_BEGIN, state["/root/AGENTS.md"])

    def test_empty_agents_refused_preflight_style(self):
        state = {"name": "test-ctr", "/root/AGENTS.md": ""}
        fake, _, _ = self._fake_sh_factory(state)
        M = self._minio_stub(state)
        with patch.object(dep, "_sh", side_effect=fake):
            with self.assertRaises(dep.DeployError) as ctx:
                dep.deploy_asset(self._managed_item(), M())
        self.assertIn("merge refused", str(ctx.exception))
        self.assertEqual(state["/root/AGENTS.md"], "")

    def test_minio_failure_triggers_full_round_rollback(self):
        # first asset applied to container, MinIO persist fails on it
        state = {"name": "test-ctr",
                 "/root/hiclaw-fs/agents/reviewer/SOUL.md": "old soul"}
        fake, calls, _ = self._fake_sh_factory(state)
        item = {"repo_source": "config/souls/reviewer/SOUL.md",
                "sha256": dep._sha256_local(
                    dep.REPO_ROOT / "config/souls/reviewer/SOUL.md"),
                "minio_dest": "hiclaw-storage/agents/reviewer/SOUL.md",
                "container": "test-ctr",
                "container_dest": "/root/hiclaw-fs/agents/reviewer/SOUL.md",
                "kind": "file"}

        class MinioFail:
            def exists(self, obj): return False
            def write_verified(self, obj, path): return False
            def read_to_host(self, obj, path): return False
            def delete(self, obj): return True
        with patch.object(dep, "_sh", side_effect=fake):
            try:
                dep.deploy_asset(item, MinioFail())
                self.fail("expected failure")
            except dep.DeployError:
                pass
            state["/tmp/bak_test"] = "old soul"
            recs = [{"item": item, "container_backup": "/tmp/bak_test",
                     "prev_container_sha": dep._sha256_bytes(b"old soul"),
                     "minio_backup_host": None, "minio_existed": False,
                     "container_done": "applied", "minio_done": False}]
            ok = dep.rollback_all(recs, MinioFail())
        self.assertTrue(ok)
        # rollback verified restore via the fake fs state
        self.assertEqual(state["/root/hiclaw-fs/agents/reviewer/SOUL.md"],
                         "old soul")


    def test_rollback_failure_detected(self):
        state = {"name": "test-ctr", "/root/AGENTS.md": "AAA" + "B"*80}
        fake, _, _ = self._fake_sh_factory(state, fail_at="cp /root/AGENTS.md")
        item = self._managed_item()
        recs = [{"item": item, "container_backup": "/tmp/bak",
                 "prev_container_sha": "f" * 64, "minio_backup_host": None,
                 "minio_existed": False, "container_done": "applied",
                 "minio_done": False}]
        with patch.object(dep, "_sh", side_effect=fake):
            ok = dep.rollback_all(recs, self._minio_stub(state)())
        self.assertFalse(ok)


class _DrillMinio(object):
    """Drill MinIO stub: no external persistence (container-only drill)."""

    def __init__(self):
        self.store = {}

    def exists(self, obj):
        return obj in self.store

    def write_verified(self, obj, path):
        self.store[obj] = path.read_text(encoding="utf-8")
        return True

    def read_to_host(self, obj, path):
        if obj not in self.store:
            return False
        path.write_text(self.store[obj], encoding="utf-8")
        return True

    def delete(self, obj):
        self.store.pop(obj, None)
        return True


@unittest.skipUnless(os.environ.get("M8A2D_DRILL") == "1",
                     "disposable-container drill requires M8A2D_DRILL=1 + docker")
class TestDisposableContainerDrill(unittest.TestCase):
    """One-shot drill against a REAL throwaway container (never the real
    hiclab agents): apply -> verify; apply again -> idempotent; empty-AGENTS
    injection -> refused with the emptied file left untouched; cleanup."""

    def _item(self, name, agents):
        return {"repo_source": "config/souls/manager-state-machine.md",
                "sha256": dep._sha256_local(
                    dep.REPO_ROOT / "config/souls/manager-state-machine.md"),
                "minio_dest": "hiclaw-storage/manager/AGENTS.md",
                "container": name, "container_dest": agents,
                "kind": "managed"}

    def test_drill(self):
        import subprocess
        name = "m8a2d-drill-%d" % os.getpid()
        r = subprocess.run(["docker", "run", "-d", "--name", name,
                            "python:3.12-slim", "sleep", "600"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            self.skipTest("docker unavailable")
        agents = "/root/AGENTS.md"
        try:
            base = "# Drill manager workspace\n" + "baseline " * 20
            w = subprocess.run(["docker", "exec", "-i", name, "sh", "-c",
                                "cat > %s" % agents],
                               input=base, text=True, capture_output=True)
            self.assertEqual(w.returncode, 0)
            mio = _DrillMinio()
            rec1 = dep.deploy_asset(self._item(name, agents), mio)
            self.assertEqual(rec1["container_done"], "applied")
            after = subprocess.run(["docker", "exec", name, "cat", agents],
                                   capture_output=True, text=True).stdout
            self.assertIn(dep._MARK_BEGIN, after)
            self.assertIn("baseline", after)
            sha1 = dep._container_sha(name, agents)
            rec2 = dep.deploy_asset(self._item(name, agents), mio)
            # managed assets are content-idempotent: second apply replaces
            # the same block, sha must be byte-identical
            self.assertEqual(dep._container_sha(name, agents), sha1)
            subprocess.run(["docker", "exec", name, "sh", "-c",
                            "printf '' > %s" % agents], capture_output=True)
            with self.assertRaises(dep.DeployError):
                dep.deploy_asset(self._item(name, agents), mio)
            empty = subprocess.run(["docker", "exec", name, "cat", agents],
                                   capture_output=True, text=True).stdout
            self.assertEqual(empty, "")
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
