#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ingest_demo.py — 把 gh-pr1-demo + rollback-demo 的结构化证据写入 PolarDB-PG 兼容审计库。
生成 SQL,经 `docker exec audit-pg psql` 执行(无需 PG 驱动)。宿主侧运行。
用法: python3 ingest_demo.py
"""
import subprocess, sys

CTR = "audit-pg"
USER = "mergepilot"; DB = "mergepilot_audit"

def esc(s):
    return str(s).replace("'", "''")

def vals(items):
    return ", ".join("('"+esc(x)+"')" if isinstance(x, str) else "("+str(x)+")" for x in items)

SQL = []

# agents
agents = [
    ("manager", "coordinator/编排", "openclaw"),
    ("reviewer", "审查", "openclaw"),
    ("fixer", "修复", "openclaw"),
    ("verifier", "复核", "openclaw"),
    ("system", "系统/回滚执行", "builtin"),
]
SQL.append("INSERT INTO agents(name,role,runtime) VALUES " +
           ", ".join(f"('{esc(n)}','{esc(r)}','{esc(rt)}')" for n, r, rt in agents) +
           " ON CONFLICT (name) DO NOTHING;")

# tasks  (task_id, repo, pr_number, pr_url, branch, type, status)
tasks = [
    ("gh-pr1-review", "nghqqa/mergepilot-test", 1, "https://github.com/nghqqa/mergepilot-test/pull/1", "feature/vulnerable-pr", "review", "done"),
    ("gh-pr1-fix",    "nghqqa/mergepilot-test", 3, "https://github.com/nghqqa/mergepilot-test/pull/3", "fix/security-demo", "fix", "done"),
    ("gh-pr1-verify", "nghqqa/mergepilot-test", 3, "https://github.com/nghqqa/mergepilot-test/pull/3", "fix/security-demo", "verify", "done"),
    ("gh-pr1-merge",  "nghqqa/mergepilot-test", 3, "https://github.com/nghqqa/mergepilot-test/pull/3", "fix/security-demo", "merge", "done"),
    ("rollback-demo", "nghqqa/mergepilot-test", None, None, "release-candidate", "rollback", "done"),
]
SQL.append("INSERT INTO tasks(task_id,repo,pr_number,pr_url,branch,type,status) VALUES " +
           ", ".join(f"('{esc(t[0])}','{esc(t[1])}',{t[2] if t[2] is not None else 'NULL'},"
                     f"{('NULL' if not t[3] else repr(t[3]))},'{esc(t[4])}','{esc(t[5])}','{esc(t[6])}')" for t in tasks)
           + " ON CONFLICT (task_id) DO NOTHING;")

# findings (gh-pr1-review): F1-F5 + 概要
findings = [
    ("gh-pr1-review", "F1", "security", "critical", "L2", "user_service.py", 3,  "硬编码 OpenAI 生产 API 密钥 sk-live-***", "sast-scan"),
    ("gh-pr1-review", "F2", "security", "critical", "L2", "user_service.py", 6,  "SQL 注入:execute 字符串拼接 name", "sast-scan"),
    ("gh-pr1-review", "F3", "quality",  "medium",   "L1", "user_service.py", 7,  "连接泄漏:未用 context manager 关闭连接", "manual+sast"),
    ("gh-pr1-review", "F4", "quality",  "medium",   "L1", "user_service.py", 5,  "缺输入校验:name 未做类型/空值/长度检查", "manual"),
    ("gh-pr1-review", "F5", "quality",  "medium",   "L1", "user_service.py", 6,  "缺错误处理:未捕获 sqlite3.Error", "manual"),
    ("gh-pr1-review", "F6", "convention","low",     "L0", "user_service.py", 1,  "缺模块 docstring/类型注解", "manual"),
]
SQL.append("INSERT INTO findings(task_id,finding_id,category,severity,risk_level,file,line,description,source) VALUES " +
           ", ".join(f"('{esc(f[0])}','{esc(f[1])}','{esc(f[2])}','{esc(f[3])}','{esc(f[4])}','{esc(f[5])}',{f[6]},'{esc(f[7])}','{esc(f[8])}')" for f in findings)
           + " ON CONFLICT DO NOTHING;")

# decisions
decisions = [
    ("gh-pr1-verify", "PASS",     "merge",          "verifier", "5/5 findings resolved, 0 新问题;原密钥需人工吊销(needs-approval)", "https://github.com/nghqqa/mergepilot-test/pull/3", "0dd5831"),
    ("rollback-demo", "FAIL",     "rollback",       "verifier", "坏修复引入回归:硬编码密钥 sk-live-abcdef0123456789 + SQLi;sast 判 FAIL", None, "43eccc3"),
    ("rollback-demo", "PASS",     "reverify-after-rollback", "system", "revert 还原干净版,sast 复扫 0 findings", None, "a63bfe1"),
]
SQL.append("INSERT INTO decisions(task_id,verdict,action,decided_by,reason,pr_url,commit_sha) VALUES " +
           ", ".join(f"('{esc(d[0])}','{esc(d[1])}','{esc(d[2])}','{esc(d[3])}','{esc(d[4])}',"
                     f"{('NULL' if not d[5] else repr(d[5]))},'{esc(d[6])}')" for d in decisions)
           + " ON CONFLICT DO NOTHING;")

# audit_events (task_id, agent, action, target, detail, sha, via)
events = [
    ("gh-pr1-review", "reviewer", "review",   "nghqqa/mergepilot-test PR#1 user_service.py", "gh-mcp-read.sh 读真实 PR + sast-scan,产出 6 findings", None, "github-mcp+sast-scan"),
    ("gh-pr1-fix",    "fixer",    "fix",      "branch fix/security-demo + file user_service.py", "gh-mcp-fix.sh 建分支+写修复(5 项)", "9775daf", "github-mcp"),
    ("gh-pr1-fix",    "fixer",    "open_pr",  "PR #3 fix/security-demo -> feature/vulnerable-pr", "create_pull_request", None, "github-mcp"),
    ("gh-pr1-verify", "verifier", "verify",   "fix/security-demo user_service.py", "gh-mcp-read.sh 读修复分支逐项比对 -> PASS(5/5 resolved)", None, "github-mcp"),
    ("gh-pr1-merge",  "system",   "merge",    "PR #3", "merge_pull_request squash 合并", "0dd5831", "github-mcp"),
    ("rollback-demo", "fixer",    "apply_fix","branch release-candidate user_service.py", "应用坏修复(加回硬编码密钥+SQLi)", "43eccc3", "github-mcp"),
    ("rollback-demo", "verifier", "verify",   "release-candidate user_service.py", "sast-scan 检出 sk-live-abcdef0123456789 -> FAIL", None, "github-mcp+sast-scan"),
    ("rollback-demo", "system",   "rollback", "branch release-candidate", "revert commit 还原干净版(脚本触发回滚)", "a63bfe1", "github-mcp"),
    ("rollback-demo", "verifier", "reverify", "release-candidate user_service.py", "复扫 0 findings,回滚确认", None, "github-mcp+sast-scan"),
]
SQL.append("INSERT INTO audit_events(task_id,agent,action,target,detail,sha,via) VALUES " +
           ", ".join(f"('{esc(e[0])}','{esc(e[1])}','{esc(e[2])}','{esc(e[3])}','{esc(e[4])}',"
                     f"{('NULL' if not e[5] else repr(e[5]))},'{esc(e[6])}')" for e in events)
           + " ON CONFLICT DO NOTHING;")

sql_text = "\n".join(SQL) + "\n"
import os
out_sql = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ingest.sql")
with open(out_sql, "w", encoding="utf-8") as f:
    f.write(sql_text)
print(f"SQL 已写入 {out_sql}。用以下命令执行:")
print(f"  wsl -- docker exec -i {CTR} psql -U {USER} -d {DB} -v ON_ERROR_STOP=1 < /mnt/d/goai/tools/audit-db/ingest.sql")
print(f"({len(tasks)} tasks, {len(findings)} findings, {len(decisions)} decisions, {len(events)} audit_events)")

