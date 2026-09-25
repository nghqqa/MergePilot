// closeout.mjs — CONTROLLED_REFERENCE_RAG_PILOT 收口流程。
// 到期或人工要求收口时执行：关 A 链→验证 disabled→复验两 PR→读回审计/MinIO→rollback→清理。
import { execSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const B = 'http://127.0.0.1:48200';
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const results = [];
function rec(id, name, pass, detail) {
  results.push({ id, name, pass });
  console.log(`${pass ? 'PASS' : 'FAIL'} [${id}] ${name}${detail ? ' — ' + detail : ''}`);
}
const pgq = (sql) => execSync(`docker exec mp-stage-pg psql -U mpstage -d mpstage -tAc "${sql}"`,
  { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();

async function login() {
  const raw = await fetch(`${B}/api/auth/login`, { method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ user: 'pilot', password: 'pilot-read-only-2026' }) });
  return (raw.headers.getSetCookie ? raw.headers.getSetCookie() : [raw.headers.get('set-cookie')])
    .flat().map((c) => c.split(';')[0]).join('; ');
}

async function main() {
  const PRE = pgq("SELECT (SELECT count(*) FROM skill_receipt_outbox), (SELECT count(*) FROM approval.tickets), (SELECT count(*) FROM skill_gate_audit)");
  console.log(`pre-closeout PG: ${PRE}`);

  // 1. flag-off（重建容器不带 A 链 env）
  const STAGE_PW = execSync(
    `docker inspect mp-stage-pg --format "{{range .Config.Env}}{{println .}}{{end}}"`,
    { encoding: 'utf8' }).split('\n').find((l) => l.startsWith('POSTGRES_PASSWORD=')).split('=')[1].trim();
  execSync('docker rm -f mp-stage-console', { stdio: 'pipe' });
  execSync(`docker run -d --name mp-stage-console --restart unless-stopped --network mp-stage-net ` +
    `-p 48200:4730 -v "D:/goai/mp-worktrees/console/evidence:/app/evidence:ro" ` +
    `-e "CONSOLE_PG_DSN=host=mp-stage-pg port=5432 user=mpstage password=${STAGE_PW} dbname=mpstage connect_timeout=5" ` +
    `-e "CONSOLE_PILOT_USER=pilot" -e "CONSOLE_PILOT_PASSWORD=pilot-read-only-2026" ` +
    `-e "CONSOLE_SESSION_SECRET=closeout-$(date +%s)" ` +
    `-e "CONSOLE_REPO_ALLOWLIST=wookat/speaktype,nghqqa/tizhou" ` +
    `mp-canonical-console:candidate`, { stdio: 'pipe' });

  await new Promise(r => setTimeout(r, 5000));

  // 2. verify a_chain_disabled
  try {
    const CK = await login();
    const r = await fetch(`${B}/api/rag/org-search?q=x`, { headers: { cookie: CK } });
    const b = await r.json();
    rec('C1', 'A 链关闭：a_chain_disabled', b.service_state === 'a_chain_disabled', b.service_state);
  } catch (e) { rec('C1', 'A 链关闭', false, e.message); }

  // 3. two-PR zero-change
  const POST = pgq("SELECT (SELECT count(*) FROM skill_receipt_outbox), (SELECT count(*) FROM approval.tickets), (SELECT count(*) FROM skill_gate_audit)");
  rec('C2', `两 PR 零变化（${PRE} → ${POST}）`, PRE === POST, `${PRE}→${POST}`);

  // 4. PG audit + MinIO read-back
  const auditCount = pgq("SELECT count(*) FROM skill_gate_audit");
  rec('C3', 'PG audit 可读', Number(auditCount) >= 2, `count=${auditCount}`);

  try {
    const minioObjects = execSync(
      `docker run --rm --network mp-stage-net --entrypoint sh mp-r13-worker-skill:candidate -c ` +
      `"mc alias set s http://mp-stage-minio:9000 $(docker inspect mp-stage-minio --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep '^MINIO_ROOT_USER=' | cut -d= -f2) $(docker inspect mp-stage-minio --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep '^MINIO_ROOT_PASSWORD=' | cut -d= -f2) >/dev/null 2>&1 && mc ls s/staging-ops/ 2>&1 | wc -l"`,
      { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], shell: true, timeout: 30000 }).trim();
    rec('C4', 'MinIO 证据可读', Number(minioObjects) >= 1, `objects=${minioObjects}`);
  } catch (e) { rec('C4', 'MinIO 证据可读', false, e.message.slice(0, 60)); }

  // 5. health after rollback
  try {
    const h = await fetch(`${B}/api/health`);
    rec('C5', 'rollback 后 health=200', h.status === 200);
  } catch (e) { rec('C5', 'health', false, e.message); }

  // 6. update ledger
  const ledger = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../gate/RPD-LEDGER.json'), 'utf8'));
  ledger.overall_status = 'PILOT_CLOSED';
  fs.writeFileSync(path.resolve(__dirname, '../gate/RPD-LEDGER.json'),
    JSON.stringify(ledger, null, 2) + '\n');

  const failed = results.filter((r) => !r.pass);
  console.log(`\ncloseout: ${results.length - failed.length}/${results.length} PASS`);
  process.exit(failed.length ? 1 : 0);
}

main().catch((e) => { console.error('CLOSEOUT ERROR:', e); process.exit(1); });
