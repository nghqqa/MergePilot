# controlled divergent replay (labeled experiment) on run-canary6-tz-2:
# same invocation id, truncated file list -> input digest differs ->
# sink must refuse CONFLICT and keep the original receipt.
import importlib.util, sys, json, os, tempfile, subprocess
sys.path.insert(0, 'D:/goai/mp-worktrees/integration')
os.chdir('D:/goai/mp-worktrees/integration')
spec = importlib.util.spec_from_file_location('canary_runner', 'pilot/rc/canary-runner.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
data = m.pr_data('nghqqa/tizhou', 2)
head = data['head_sha']
with tempfile.TemporaryDirectory() as td:
    ctx = m.write_context('run-canary6-tz-2', 'nghqqa/tizhou', 2, head, td)
    env_extra = {"MERGEPILOT_SKILL_GATE_SINK_DSN": m.DSN,
                 "MERGEPILOT_SKILL_RUN_CONTEXT_FILE": ctx,
                 "MERGEPILOT_SKILL_RUN_CONTEXT_ROOT": td,
                 "MERGEPILOT_SKILL_EXPECTED_RUN_ID": 'run-canary6-tz-2',
                 "MERGEPILOT_SKILL_EXPECTED_HEAD_SHA": head,
                 "MERGEPILOT_SKILL_EXPECTED_REPO": 'nghqqa/tizhou',
                 "SKILL_AUDIT_ENDPOINT": m.API + "/api/rag/skill-audit"}
    env = dict(os.environ); env.update(env_extra); env["PYTHONIOENCODING"] = "utf-8"
    req = {"contract_version": "1", "request_id": "req-run-canary6-tz-2-sast-scan",
           "trace_id": "trace-drift6", "input": {"mode": "inline", "files": data['files'][:5], "options": {}}}
    r = subprocess.run([sys.executable, "-X", "utf8", "-m", "skills.sast_scan.run"],
                       input=json.dumps(req), capture_output=True, text=True, timeout=180, env=env, cwd='.')
    out = json.loads(r.stdout)
    from skills.common import receipt_cli, receipt_sinks
    os.environ.update(env_extra)
    rec = receipt_cli.finalize({"skill": "sast-scan", "skill_version": out.get("skill_version") or "1.0.0",
                                "request_id": out.get("request_id"), "input": out.get("input") or {},
                                "status": out.get("status") or "ERROR", "output": out.get("output"),
                                "started_at": "t0", "completed_at": "t1", "duration_ms": 5})
    try:
        sink = receipt_sinks.open_durable_sink("production", dict(os.environ))
        sink.put(rec)
        print('DIVERGENT REPLAY: ACCEPTED (unexpected)')
    except receipt_sinks.ReceiptConflict:
        print('DIVERGENT REPLAY: CONFLICT (fail-closed, original kept)')
import psycopg2
conn = psycopg2.connect(m.DSN); cur = conn.cursor()
cur.execute("SELECT count(*) FROM skill_receipt_outbox WHERE payload->>'run_id'='run-canary6-tz-2'")
print('rows (must stay 2):', cur.fetchone()[0]); conn.close()
