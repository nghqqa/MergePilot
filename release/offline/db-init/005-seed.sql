-- offline delivery: seed the demo run so the console startup probe
-- (RUN_NOT_FOUND fail-closed) has a row matching MERGEPILOT_RUN_ID.
INSERT INTO task_runs (run_id, room_id, repo, pr_number, branch, status, current_stage, verdict)
VALUES ('offline-demo', 'smoke-room', 'offline-demo/local', 1, 'offline-demo', 'PASS', 'L4_REPORT', 'PASS')
ON CONFLICT (run_id) DO NOTHING;
