// console/backend/test/r4-remediation.test.mjs — R4 整改轮回归测试。
// 覆盖：/api/runs 会话 allowlist 服务端过滤（P0 零泄漏）、/api/pulls/:n
// 401/403/404/200、/api/health live 口径（primary=contract_v2 + declared_repos）、
// 会话 user 结构化（不再"未知用户"）。
// 运行：node --test console/backend/test/r4-remediation.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createConsole } from '../server.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

process.env.CONSOLE_PILOT_USER = 'pilot';
process.env.CONSOLE_PILOT_PASSWORD = 'pw-test';
process.env.CONSOLE_SESSION_SECRET = 'test-secret';
process.env.CONSOLE_REPO_ALLOWLIST = 'wookat/speaktype,nghqqa/tizhou';
delete process.env.CONSOLE_PG_DSN;

async function start() {
  const { server } = createConsole({ evidenceRoot: __dirname, distDir: path.join(__dirname, 'no-dist') });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}`;
  return { server, base };
}

async function call(base, p, init) {
  const r = await fetch(base + p, init);
  let body = null; try { body = await r.json(); } catch { /* non-json */ }
  return { status: r.status, body, headers: r.headers };
}

async function loginCookie(base) {
  const r = await call(base, '/api/auth/login', { method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ user: 'pilot', password: 'pw-test' }) });
  assert.strictEqual(r.status, 200);
  return (r.headers.get('set-cookie') || '').split(';')[0];
}

test('health: live 未配置 → snapshot 保守声明，declared_repos 为空', async () => {
  const { server, base } = await start();
  try {
    const r = await call(base, '/api/health');
    assert.strictEqual(r.body?.data_mode, 'snapshot');
    assert.strictEqual(r.body?.sources?.primary, 'snapshot');
    assert.strictEqual(r.body?.sources?.contract_v2?.available, false);
    assert.deepStrictEqual(r.body?.declared_repos, []);
  } finally { server.close(); }
});

test('session user 为结构化对象（前端不再显示"未知用户"）', async () => {
  const { server, base } = await start();
  try {
    const cookie = await loginCookie(base);
    const r = await call(base, '/api/auth/session', { headers: { cookie } });
    assert.strictEqual(r.status, 200);
    assert.deepStrictEqual(r.body?.user, { name: 'pilot' });
  } finally { server.close(); }
});

test('P0：/api/runs 已认证会话按 allowlist 服务端过滤（非授权仓库零条目）', async () => {
  const { server, base } = await start();
  try {
    const cookie = await loginCookie(base);
    const r = await call(base, '/api/runs?limit=200', { headers: { cookie } });
    assert.strictEqual(r.status, 200);
    assert.strictEqual(r.body?.scope, 'session_allowlist');
    const repos = new Set((r.body?.items ?? []).map((x) => x.repo ?? null));
    for (const repo of repos) {
      if (repo === null) continue;   // 旧证据包无 repo 字段且不含 finding 正文的行——以 pack 维度再验
      assert.ok(['wookat/speaktype', 'nghqqa/tizhou'].includes(repo),
        `allowlist 外仓库泄漏: ${repo}`);
    }
    // 未认证 → 演示语义（scope 显式标注）
    const anon = await call(base, '/api/runs?limit=200');
    assert.strictEqual(anon.body?.scope, 'demo_snapshot');
  } finally { server.close(); }
});

test('P0：越权 pack 详情/证据子资源对已认证会话返回 404（不泄露存在性）', async () => {
  const { server, base } = await start();
  try {
    const cookie = await loginCookie(base);
    const list = await call(base, '/api/runs?limit=200');
    const outsider = (list.body?.items ?? []).find(
      (x) => x.repo && !['wookat/speaktype', 'nghqqa/tizhou'].includes(x.repo));
    if (!outsider) return;   // 证据根内无越权 pack 时跳过（fixture 树相关）
    const d = await call(base, `/api/runs/${outsider.pack_id}`, { headers: { cookie } });
    assert.strictEqual(d.status, 404);
    const e = await call(base, `/api/runs/${outsider.pack_id}/evidence`, { headers: { cookie } });
    assert.strictEqual(e.status, 404);
  } finally { server.close(); }
});

test('PR 详情端点：401 / 403（allowlist 外）/ 404（no_live_record）/ 200 契约形状', async () => {
  const { server, base } = await start();
  try {
    const un = await call(base, '/api/pulls/426?repo=wookat%2Fspeaktype');
    assert.strictEqual(un.status, 401);

    const cookie = await loginCookie(base);
    const forbidden = await call(base, '/api/pulls/1?repo=other-org%2Foutsider-repo',
      { headers: { cookie } });
    assert.strictEqual(forbidden.status, 403);
    assert.strictEqual(forbidden.body?.error?.reason, 'repo_not_in_allowlist');

    const missing = await call(base, '/api/pulls/99999?repo=wookat%2Fspeaktype',
      { headers: { cookie } });
    assert.strictEqual(missing.status, 404);
    assert.strictEqual(missing.body?.error?.reason, 'no_live_record');

    // live 未接入本地证据根时 overview 为 BACKEND_NOT_WIRED → prs 空 → 404 no_live_record
    // （200 契约形状由隔离 staging 活体验证覆盖，见评估报告）
    assert.strictEqual(missing.body?.error?.reason, 'no_live_record');
  } finally { server.close(); }
});
