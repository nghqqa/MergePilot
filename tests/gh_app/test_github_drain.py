"""github_drain tests (M8-GH-1 §4/§5) — fully mocked DB."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = HERE.parent.parent
for p in (str(HERE), str(ROOT / "tools" / "workflow-controller")):
    if p not in sys.path:
        sys.path.insert(0, p)

from fakes import FakeConnection                            # noqa: E402
import github_drain as gd                                   # noqa: E402
from github_drain import GithubDrainError                   # noqa: E402

REPO = "nghqqa/MergePilot"
ROOM = "!gh-room:server"
HEAD = "c" * 40
DELIVERY = "abc12345-1111-2222-3333-444455556666"
INSTALL = 42

CONFIG = {"rooms": {REPO: ROOM}, "allowlist": {REPO}}


def payload(action="opened", repo=REPO, installation=INSTALL, pr=101,
            head=HEAD, branch="feature/x"):
    return {
        "schema_version": "1", "event_name": "pull_request",
        "action": action, "installation_id": installation, "repo": repo,
        "pr_number": pr, "branch": branch, "observed_head_sha": head,
        "observed_base_sha": "b" * 40,
        "body_sha256": "d" * 64,
    }


def expected_run_id(p):
    return gd.derive_github_run_id(p["installation_id"], p["repo"],
                                   p["pr_number"], p["observed_head_sha"])


class SpySubmit:
    def __init__(self, outcome="created"):
        self.calls = []
        self.outcome = outcome

    def __call__(self, conn, submission, source):
        self.calls.append((submission, source))
        from task_submit import SubmitResult
        return SubmitResult(outcome=self.outcome,
                            run_id=submission.run_id, stage="review",
                            dispatch_key="%s:review:1" % submission.run_id)


def claim_conn(p=None, *, confirm_rowcount=1, stage_rowcount=1):
    conn = FakeConnection()
    conn.enqueue("WITH candidate", rowcount=1,
                 fetchone=(DELIVERY, "claim-1",
                           json.dumps(p or payload()), 1))
    conn.enqueue("INSERT INTO stage_events", rowcount=stage_rowcount)
    conn.enqueue("UPDATE public.github_deliveries", rowcount=confirm_rowcount)
    return conn


class TestRunIdDerivation(unittest.TestCase):

    def test_deterministic_and_shape(self):
        run_id = gd.derive_github_run_id(INSTALL, REPO, 101, HEAD)
        self.assertTrue(gd.GH_RUN_ID_RE.fullmatch(run_id))
        self.assertEqual(run_id, gd.derive_github_run_id(INSTALL, REPO, 101,
                                                         HEAD))
        gd.validate_derived_run_id(run_id, INSTALL, REPO, 101, HEAD)

    def test_mismatch_rejected(self):
        with self.assertRaises(GithubDrainError):
            gd.validate_derived_run_id("gh-" + "0" * 24, INSTALL, REPO, 101,
                                       HEAD)


class TestRoomMapAndAlignment(unittest.TestCase):

    def _write(self, text):
        fd, name = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, name)
        return name

    def test_valid_map_and_alignment(self):
        room_map = self._write('repos:\n  "%s":\n    room_id: "%s"\n'
                               % (REPO, ROOM))
        policy = (ROOT / "tools" / "policy-gateway" / "policy.yaml")
        config = gd.load_github_ingress_config(room_map, policy)
        self.assertEqual(config["rooms"], {REPO: ROOM})

    def test_duplicate_repo_rejected(self):
        path = self._write('repos:\n  "%s":\n    room_id: "%s"\n'
                           '  "%s":\n    room_id: "%s"\n'
                           % (REPO, ROOM, REPO, ROOM))
        with self.assertRaises(GithubDrainError):
            gd.parse_room_map(path)

    def test_missing_room_id_rejected(self):
        path = self._write('repos:\n  "%s":\n' % REPO)
        with self.assertRaises(GithubDrainError):
            gd.parse_room_map(path)

    def test_malformed_shape_rejected(self):
        for bad in ('other:\n', 'repos:\nnot-indented\n',
                    'repos:\n  "%s":\n    wrong: "x"\n' % REPO):
            path = self._write(bad)
            with self.assertRaises(GithubDrainError):
                gd.parse_room_map(path)

    def test_alignment_mismatch_rejected(self):
        room_map = self._write('repos:\n  "%s":\n    room_id: "%s"\n'
                               % (REPO, ROOM))
        policy = (ROOT / "tools" / "policy-gateway" / "policy.yaml")
        # 本仓库 policy allowlist 恰为单 repo —— 完全一致时必须成功
        config = gd.load_github_ingress_config(room_map, policy)
        self.assertEqual(config["rooms"], {REPO: ROOM})
        # 多出的 repo(allowlist 之外)→ 1:1 失配 → 拒绝
        room_map2 = self._write(
            'repos:\n  "%s":\n    room_id: "%s"\n  "other/repo":\n'
            '    room_id: "!x:s"\n' % (REPO, ROOM))
        with self.assertRaises(GithubDrainError) as ctx:
            gd.load_github_ingress_config(room_map2, policy)
        self.assertIn("1:1", str(ctx.exception))
        # 缺失 allowlist repo → 失配 → 拒绝
        room_map3 = self._write('repos:\n  "other/repo":\n    room_id: "!x:s"\n')
        with self.assertRaises(GithubDrainError):
            gd.load_github_ingress_config(room_map3, policy)


class TestDrainHappyPath(unittest.TestCase):

    def test_claim_stage_submit_confirm(self):
        conn = claim_conn()
        spy = SpySubmit()
        events = []
        handled = gd.drain_github_deliveries(lambda: conn, config=CONFIG,
                                             submit=spy,
                                             observer=events.append)
        self.assertEqual(handled, 1)
        self.assertEqual(len(spy.calls), 1)
        submission, source = spy.calls[0]
        self.assertEqual(submission.run_id, expected_run_id(payload()))
        self.assertEqual(submission.room_id, ROOM)
        self.assertEqual(submission.repo, REPO)
        self.assertEqual(source.channel, "github")
        self.assertEqual(source.event_id, "gh:%s" % DELIVERY)
        self.assertEqual(source.sender_identity, "github-app[42]")
        stage_sql, stage_params = [e for e in conn.executed
                                   if "INSERT INTO stage_events" in e[0]][0]
        self.assertIn("ON CONFLICT (event_id) DO NOTHING", stage_sql)
        self.assertIn("'TASK_SUBMITTED'", stage_sql)      # event_type 字面量
        # 参数顺序: (event_id, room_id, sender, raw_body, body_sha256)
        self.assertEqual(stage_params[0], "gh:%s" % DELIVERY)
        self.assertEqual(stage_params[1], ROOM)
        self.assertEqual(stage_params[2], "github-app[42]")
        self.assertIn('"event_name":"pull_request"', stage_params[3])
        self.assertEqual(stage_params[4], "d" * 16)   # body_sha256[:16]
        # marks(event_id 精确匹配)与成功确认(claim CAS)
        self.assertTrue(any("SET status='PROCESSED', processed_at=now() "
                            "WHERE event_id=" in sql
                            and params[0] == "gh:%s" % DELIVERY
                            for sql, params in conn.executed))
        confirm = conn.params_of("SET status = 'PROCESSED', processed_at")[0]
        self.assertEqual(confirm[1], DELIVERY)
        self.assertEqual(confirm[2], "claim-1")
        self.assertEqual(events[-1]["event"], "github.delivery.processed")

    def test_no_claimable_idle(self):
        conn = FakeConnection()
        conn.enqueue("WITH candidate", rowcount=1, fetchone=None)
        spy = SpySubmit()
        self.assertEqual(gd.drain_github_deliveries(
            lambda: conn, config=CONFIG, submit=spy), 0)
        self.assertEqual(spy.calls, [])

    def test_duplicate_submit_still_processes(self):
        conn = claim_conn()
        spy = SpySubmit(outcome="duplicate")
        handled = gd.drain_github_deliveries(lambda: conn, config=CONFIG,
                                             submit=spy)
        self.assertEqual(handled, 1)
        confirm = conn.params_of("SET status = 'PROCESSED', processed_at")[0]
        self.assertEqual(confirm[0], expected_run_id(payload()))


class TestDrainFailClosed(unittest.TestCase):

    def test_stage_event_exists_zero_submit_zero_marks(self):
        conn = FakeConnection()
        conn.enqueue("WITH candidate", rowcount=1,
                     fetchone=(DELIVERY, "claim-1",
                               json.dumps(payload()), 1))
        conn.enqueue("INSERT INTO stage_events", rowcount=0)   # 已存在
        conn.enqueue("SELECT status FROM stage_events", rowcount=1,
                     fetchone=("PROCESSED",))
        conn.enqueue("UPDATE public.github_deliveries", rowcount=1)
        spy = SpySubmit()
        gd.drain_github_deliveries(lambda: conn, config=CONFIG, submit=spy)
        self.assertEqual(spy.calls, [])      # 零 submit_task
        # 零 mark/update(冲突分支后没有任何 stage_events UPDATE)
        self.assertEqual(
            [sql for sql in conn.sqls() if "UPDATE stage_events" in sql], [])
        failure = conn.params_of("SET status = CASE WHEN attempt_count")[0]
        self.assertIn("STAGE_EVENT_ID_COLLISION:PROCESSED", failure[2])

    def test_room_mapping_missing_permanent_error(self):
        conn = claim_conn(payload(repo="unmapped/repo"))
        spy = SpySubmit()
        gd.drain_github_deliveries(lambda: conn, config=CONFIG, submit=spy)
        self.assertEqual(spy.calls, [])
        failure = conn.params_of("SET status = CASE WHEN attempt_count")[0]
        self.assertIn("ROOM_MAPPING_MISSING", failure[2])
        self.assertIn("PERMANENT", failure[2])

    def test_stale_confirm_rolls_back_work(self):
        conn = claim_conn(confirm_rowcount=0)   # lease 被接管
        spy = SpySubmit()
        events = []
        gd.drain_github_deliveries(lambda: conn, config=CONFIG, submit=spy,
                                   observer=events.append)
        self.assertEqual(len(spy.calls), 1)      # submit 曾执行
        self.assertGreaterEqual(conn.rollbacks, 1)   # 工作事务整体回滚
        self.assertEqual(events[-1]["event"], "github.delivery.stale_confirm")
        # 成功确认未被记录为 processed
        self.assertFalse(any(e["event"] == "github.delivery.processed"
                             for e in events))

    def test_claim_sql_is_legal_cte_with_skip_locked(self):
        self.assertIn("WITH candidate AS (", gd._CLAIM_SQL)
        self.assertIn("FOR UPDATE SKIP LOCKED", gd._CLAIM_SQL)
        self.assertIn("make_interval(secs => %s)", gd._CLAIM_SQL)
        self.assertIn("gen_random_uuid()::text", gd._CLAIM_SQL)
        self.assertIn("RETURNING d.delivery_id, d.claim_id, "
                      "d.canonical_payload, d.attempt_count",
                      gd._CLAIM_SQL)
        self.assertIn("claim_id = %s AND status = 'RUNNING'",
                      gd._SUCCESS_CONFIRM_SQL)
        self.assertIn("claim_id = %s AND status = 'RUNNING'",
                      gd._FAILURE_CONFIRM_SQL)

    def test_transient_failure_backoff_and_terminal(self):
        conn = claim_conn()
        conn.plan.clear()
        conn.enqueue("WITH candidate", rowcount=1,
                     fetchone=(DELIVERY, "claim-1",
                               json.dumps(payload()), 3))
        conn.enqueue("UPDATE public.github_deliveries", rowcount=1)
        spy = SpySubmit()
        # submit 抛瞬时异常 → 退避重试(attempt=3 < 5)
        def boom(conn, submission, source):
            raise RuntimeError("db hiccup")
        gd.drain_github_deliveries(lambda: conn, config=CONFIG,
                                   submit=boom)
        failure = conn.params_of("SET status = CASE WHEN attempt_count")[0]
        self.assertEqual(failure[0], 5)          # max_attempts 参数
        self.assertEqual(failure[1], 120)        # 30*2^(3-1)=120
        self.assertIn("db hiccup", failure[2])
        self.assertNotIn("PERMANENT", failure[2])


if __name__ == "__main__":
    unittest.main()
