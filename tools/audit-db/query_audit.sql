-- 审计轨迹查询证据
\echo '===== 审计事件流(按 task, 时序)====='
SELECT task_id, agent, action, substring(target,1,42) AS target,
       COALESCE(sha,'-') AS sha, via
FROM audit_events ORDER BY task_id, id;

\echo ''
\echo '===== 决策(verdict / action / commit)====='
SELECT task_id, verdict, action, decided_by, COALESCE(commit_sha,'-') AS commit
FROM decisions ORDER BY id;

\echo ''
\echo '===== gh-pr1-review findings(按严重度)====='
SELECT finding_id, severity, risk_level, file, line, source
FROM findings WHERE task_id='gh-pr1-review' ORDER BY finding_id;

\echo ''
\echo '===== 统计:每个 task 的事件数 ====='
SELECT task_id, count(*) AS events FROM audit_events GROUP BY task_id ORDER BY task_id;
