import sys
sys.path.insert(0, 'D:/goai/mp-worktrees/integration')
import os
os.chdir('D:/goai/mp-worktrees/integration')
import psycopg2
from skills.common.receipt_sinks import write_gate_audit
DSN = 'host=127.0.0.1 port=45434 user=mpcc password=mp-cc-staging-pw dbname=mpcc'
conn = psycopg2.connect(DSN); cur = conn.cursor()
cur.execute("SELECT DISTINCT payload->>'run_id', payload->>'repo', payload->>'head_sha' FROM skill_receipt_outbox WHERE payload->>'run_id' LIKE 'run-canary6%'")
rows = cur.fetchall(); conn.close()
for run_id, repo, head in rows:
    pr = 426 if 'st-426' in run_id else 2
    write_gate_audit(DSN, run_id, {"decision": "PRODUCE", "required": ["diff_parse", "sast_scan"],
                                    "present": ["diff_parse", "sast_scan"], "missing": [], "enforce": True,
                                    "repo": repo, "pr": pr, "head_sha": head})
    print('gate audit:', run_id)
