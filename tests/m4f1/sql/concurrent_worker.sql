-- tests/m4f1/sql/concurrent_worker.sql — per-session concurrent rollback insert.
-- 传入 psql 变量 run_a / run_b / tag / wrole。两条 run 已由 runner 预建(ACTIVE)。
-- _writer_gate_rollback 按 run_id 稳定顺序 FOR KEY SHARE;并发(含反向 parent/revert)应无死锁。
\set ON_ERROR_STOP on
SET ROLE :wrole;
INSERT INTO rollback_runs(rollback_id, parent_run_id, revert_run_id, reverted_merge_sha, repo, pr_number, trigger_event_id)
SELECT 'm4f1_conc_' || :'tag', :'run_a', :'run_b',
       left(encode(digest(:'tag','sha256'),'hex'),40), 'o/r', 1, 'ev-' || :'tag';
RESET ROLE;
\echo 'worker ' :tag ' OK'
