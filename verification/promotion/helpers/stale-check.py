import sys
sys.path.insert(0, 'D:/goai/mp-worktrees/integration')
import os
os.chdir('D:/goai/mp-worktrees/integration')
os.environ['MERGEPILOT_SKILL_GATE_ENFORCE'] = 'on'
import json, psycopg2
conn = psycopg2.connect('host=127.0.0.1 port=45434 user=mpcc password=mp-cc-staging-pw dbname=mpcc')
cur = conn.cursor()
cur.execute("SELECT payload FROM skill_receipt_outbox WHERE payload->>'run_id'='run-canary5-st-426'")
rows = [json.loads(r[0]) if not isinstance(r[0], dict) else r[0] for r in cur.fetchall()]
conn.close()
from skills.common.gate_enforce import decide
g = decide(rows, 'run-canary5-st-426', '0' * 40)
print(g.action, g.error_code)
