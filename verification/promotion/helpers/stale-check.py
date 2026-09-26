import sys
sys.path.insert(0, 'D:/goai/mp-worktrees/integration')
import os
os.chdir('D:/goai/mp-worktrees/integration')
os.environ['MERGEPILOT_SKILL_GATE_ENFORCE'] = 'on'
import json, psycopg2
conn = psycopg2.connect('host=127.0.0.1 port=45435 user=mpstage password=747d7593dfa2ee0ea7ce1eb4b96367af dbname=mpstage')
cur = conn.cursor()
cur.execute("SELECT payload FROM skill_receipt_outbox WHERE payload->>'run_id'='run-stage-st-426'")
rows = [json.loads(r[0]) if not isinstance(r[0], dict) else r[0] for r in cur.fetchall()]
conn.close()
from skills.common.gate_enforce import decide
g = decide(rows, 'run-stage-st-426', '0' * 40)
print(g.action, g.error_code)
