"""task_submit contract tests (M8-GH-1 §2) — fully mocked DB."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = HERE.parent.parent
for p in (str(HERE), str(ROOT / "tools" / "workflow-controller")):
    if p not in sys.path:
        sys.path.insert(0, p)

from fakes import FakeConnection                            # noqa: E402
from task_submit import (EventSource, SubmitTaskConflict,   # noqa: E402
                         TaskSubmission, dispatch_key_for, submit_task)

ROOM = "!room:server"
REPO = "nghqqa/MergePilot"
BODY = ("请审查 %s PR#101 (分支 feature/x)。用 gh-mcp-read.sh + sast-scan,"
        "findings 写 shared/tasks/gh-test-review/findings.md。"
        "完成写 TASK_COMPLETED: gh-test-review。" % REPO)


def submission(**overrides):
    values = dict(run_id="run-eph-1", room_id=ROOM, repo=REPO,
                  pr_number=101, branch="feature/x",
                  approval_required=False, dispatch_body=BODY)
    values.update(overrides)
    return TaskSubmission(**values)


MATRIX = EventSource(channel="matrix", event_id="$ev1",
                     sender_identity="@admin:server")
GITHUB = EventSource(channel="github", event_id="gh:d-1",
                     sender_identity="github-app[42]")


class TestCreatedPath(unittest.TestCase):

    def test_three_inserts_with_existing_idempotency_keys(self):
        conn = FakeConnection()
        result = submit_task(conn, submission(), MATRIX)
        self.assertEqual(result.outcome, "created")
        self.assertEqual(result.dispatch_key, "run-eph-1:review:1")
        sqls = conn.sqls()
        self.assertEqual(sum(1 for s in sqls if "INSERT INTO task_runs" in s),
                         1)
        self.assertEqual(
            sum(1 for s in sqls if "INSERT INTO stage_runs" in s), 1)
        self.assertEqual(
            sum(1 for s in sqls if "INSERT INTO dispatch_outbox" in s), 1)
        # 事务归调用方:helper 零 commit/rollback
        self.assertEqual((conn.commits, conn.rollbacks), (0, 0))
        dispatch_params = conn.params_of("INSERT INTO dispatch_outbox")[0]
        # 参数顺序: (idempotency_key, run_id, room_id, body)
        self.assertEqual(dispatch_params[0], "run-eph-1:review:1")
        self.assertEqual(dispatch_params[1], "run-eph-1")
        self.assertEqual(dispatch_params[2], ROOM)
        self.assertEqual(dispatch_params[3], BODY)

    def test_dispatch_key_format_frozen(self):
        self.assertEqual(dispatch_key_for("xyz"), "xyz:review:1")


class TestDuplicateLifecycleCompatible(unittest.TestCase):
    """duplicate 只比较不可变字段;生命周期状态(DISPATCHED/FAILED/…)不参与。"""

    def _duplicate_conn(self, identity=None, stage=("reviewer", 1),
                        dispatch=("reviewer", "review", 1, BODY)):
        conn = FakeConnection()
        conn.enqueue("INSERT INTO task_runs", rowcount=0)
        conn.enqueue("SELECT room_id, repo, pr_number, branch, "
                     "approval_required",
                     rowcount=1,
                     fetchone=identity or (ROOM, REPO, 101, "feature/x",
                                           False))
        conn.enqueue("SELECT agent, attempt FROM stage_runs", rowcount=1,
                     fetchone=stage)
        conn.enqueue("SELECT target_agent, target_stage, attempt, body",
                     rowcount=1, fetchone=dispatch)
        return conn

    def test_same_payload_duplicate_zero_writes(self):
        conn = self._duplicate_conn()
        result = submit_task(conn, submission(), MATRIX)
        self.assertEqual(result.outcome, "duplicate")
        # 冲突后只有 SELECT;唯一的 INSERT 是最初 rowcount=0 的 task_runs
        writes = [sql for sql in conn.sqls()
                  if sql.startswith("INSERT") or sql.startswith("UPDATE")]
        self.assertEqual(writes, ["INSERT INTO task_runs(run_id, room_id, "
                                  "repo, pr_number, branch, status, "
                                  "current_stage, approval_required) "
                                  "VALUES(%s, %s, %s, %s, %s, 'RUNNING', "
                                  "'review', %s) ON CONFLICT(run_id) DO "
                                  "NOTHING"])

    def test_duplicate_selects_exclude_lifecycle_columns(self):
        """SELECT 列清单不含 status —— DISPATCHED/RETRY/FAILED 皆不比较。"""
        conn = self._duplicate_conn()
        submit_task(conn, submission(), MATRIX)
        stage_sql = [sql for sql in conn.sqls()
                     if "FROM stage_runs" in sql][0]
        dispatch_sql = [sql for sql in conn.sqls()
                        if "FROM dispatch_outbox" in sql][0]
        self.assertNotIn("status", stage_sql)
        self.assertNotIn("status", dispatch_sql)
        self.assertNotIn("retry_count", dispatch_sql)
        self.assertNotIn("dispatched_at", dispatch_sql)

    def test_duplicate_with_null_optional_fields(self):
        conn = self._duplicate_conn(identity=(ROOM, None, None, None, False))
        result = submit_task(conn, submission(repo=None, pr_number=None,
                                              branch=None), MATRIX)
        self.assertEqual(result.outcome, "duplicate")


class TestRunIdConflict(unittest.TestCase):

    def _conflict(self, **overrides):
        identity_map = dict(room_id=ROOM, repo=REPO, pr_number=101,
                            branch="feature/x", approval_required=False)
        for key, value in overrides.items():
            identity_map[key] = value
        identity = (identity_map["room_id"], identity_map["repo"],
                    identity_map["pr_number"], identity_map["branch"],
                    identity_map["approval_required"])
        conn = FakeConnection()
        conn.enqueue("INSERT INTO task_runs", rowcount=0)
        conn.enqueue("SELECT room_id, repo, pr_number, branch, "
                     "approval_required", rowcount=1, fetchone=identity)
        return conn

    def test_different_repo_conflict_zero_stage_outbox_writes(self):
        conn = self._conflict(repo="other/repo")
        with self.assertRaises(SubmitTaskConflict) as ctx:
            submit_task(conn, submission(), MATRIX)
        self.assertIn("RUN_ID_CONFLICT", str(ctx.exception))
        self.assertIn("repo", ctx.exception.args[0] if ctx.exception.args
                      else "")
        # 冲突后零 stage/dispatch 写
        self.assertEqual(conn.params_of("INSERT INTO stage_runs"), [])
        self.assertEqual(conn.params_of("INSERT INTO dispatch_outbox"), [])

    def test_different_pr_room_branch_conflict(self):
        for overrides in ({"pr_number": 999}, {"room_id": "!other:s"},
                          {"branch": "other-branch"},
                          {"approval_required": True}):
            conn = self._conflict(**overrides)
            with self.assertRaises(SubmitTaskConflict):
                submit_task(conn, submission(), MATRIX)
            self.assertEqual(conn.params_of("INSERT INTO stage_runs"), [])

    def test_stage_structure_mismatch_conflict(self):
        conn = FakeConnection()
        conn.enqueue("INSERT INTO task_runs", rowcount=0)
        conn.enqueue("SELECT room_id", rowcount=1,
                     fetchone=(ROOM, REPO, 101, "feature/x", False))
        conn.enqueue("SELECT agent, attempt", rowcount=1,
                     fetchone=("someone-else", 2))
        with self.assertRaises(SubmitTaskConflict):
            submit_task(conn, submission(), MATRIX)

    def test_dispatch_body_mismatch_conflict(self):
        conn = FakeConnection()
        conn.enqueue("INSERT INTO task_runs", rowcount=0)
        conn.enqueue("SELECT room_id", rowcount=1,
                     fetchone=(ROOM, REPO, 101, "feature/x", False))
        conn.enqueue("SELECT agent", rowcount=1, fetchone=("reviewer", 1))
        conn.enqueue("SELECT target_agent", rowcount=1,
                     fetchone=("reviewer", "review", 1, "a different body"))
        with self.assertRaises(SubmitTaskConflict):
            submit_task(conn, submission(), MATRIX)


class TestChannelNamespace(unittest.TestCase):

    def test_matrix_cannot_use_gh_namespace(self):
        conn = FakeConnection()
        with self.assertRaises(SubmitTaskConflict) as ctx:
            submit_task(conn, submission(run_id="gh-" + "a" * 24), MATRIX)
        self.assertIn("RUN_ID_NAMESPACE_RESERVED",
                      str(ctx.exception))
        self.assertEqual(conn.executed, [])   # 零 SQL

    def test_github_requires_derived_shape(self):
        conn = FakeConnection()
        with self.assertRaises(SubmitTaskConflict):
            submit_task(conn, submission(run_id="gh-not-hex!"), GITHUB)
        self.assertEqual(conn.executed, [])
        conn2 = FakeConnection()
        with self.assertRaises(SubmitTaskConflict):
            submit_task(conn2, submission(run_id="run-plain"), GITHUB)
        self.assertEqual(conn2.executed, [])

    def test_github_valid_shape_proceeds(self):
        conn = FakeConnection()
        result = submit_task(conn,
                             submission(run_id="gh-" + "0" * 24), GITHUB)
        self.assertEqual(result.outcome, "created")


if __name__ == "__main__":
    unittest.main()
