// live-api.test.mjs — 契约 v2（API-AUTH-MERGE-V0 @ 7ccecb9）适配层契约测试
// 运行：node --test console/backend/test/live-api.test.mjs
//
// 覆盖：会话端点路径与分类矩阵（401/403/503/404）、repo 寻址编码、CSRF 方法集（含 PATCH）、
// pulls fixture 形状、权威 head 关联语义（stale 不冒充当前结论）。

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import {
  SESSION_PATH, sessionUrl, capabilitiesUrl, pullsUrl, pullUrl,
  classifySession, fetchSession, requiresCsrf, buildWriteHeaders,
  v3RunToRecord,
} from '../../frontend/src/api-live.js';
import { associateCurrentHead } from '../../frontend/src/pr-model.js';

const REPO = 'acme/widget';

test('契约路径：会话为 GET /api/auth/session；repo 寻址走查询参数且 URL 编码', () => {
  assert.equal(SESSION_PATH, '/api/auth/session');
  assert.equal(sessionUrl(), '/api/auth/session');
  assert.equal(capabilitiesUrl(REPO), '/api/me/capabilities?repo=acme%2Fwidget');
  assert.equal(pullsUrl(REPO, { state: 'open', limit: 20, offset: 0 }),
    '/api/pulls?repo=acme%2Fwidget&state=open&limit=20&offset=0');
  assert.equal(pullUrl(REPO, 9), '/api/pulls/9?repo=acme%2Fwidget');
});

test('会话分类矩阵：200/401 两种 reason/403/503/404 各归其位', () => {
  assert.equal(classifySession({ status: 200, body: { user: { user_id: 'u1' }, expires_at: 'x' } }).state, 'authed');
  assert.equal(classifySession({ status: 200, body: {} }).state, 'anonymous');
  assert.equal(classifySession({ status: 401, body: { error: { reason: 'not_authenticated' } } }).state, 'anonymous');
  assert.equal(classifySession({ status: 401, body: { error: { reason: 'session_expired' } } }).state, 'expired');
  assert.equal(classifySession({ status: 403, body: { error: { reason: 'not_a_member' } } }).state, 'forbidden');
  assert.equal(classifySession({ status: 503, body: { error: { reason: 'auth_unavailable' } } }).state, 'auth_unavailable');
  assert.equal(classifySession({ status: 404 }).state, 'not_implemented');
  assert.equal(classifySession({ status: 500 }).state, 'unavailable');
});

test('fetchSession：网络失败=服务不可达（区别于 503 auth_unavailable）；携带同源 Cookie', async () => {
  const netFail = await fetchSession(async () => { throw new Error('ECONNREFUSED'); });
  assert.equal(netFail.state, 'unavailable');
  let seen = null;
  const ok = await fetchSession(async (url, init) => {
    seen = { url, init };
    return { status: 200, json: async () => ({ user: { user_id: 'u1' }, expires_at: 'e' }) };
  });
  assert.equal(seen.url, '/api/auth/session');
  assert.equal(seen.init.credentials, 'same-origin');
  assert.equal(ok.state, 'authed');
  assert.equal(ok.user.user_id, 'u1');
});

test('CSRF：全部副作用方法（含 PATCH）必须携带 X-CSRF-Token；缺 token 本地拦截', () => {
  for (const m of ['POST', 'PUT', 'PATCH', 'DELETE']) assert.ok(requiresCsrf(m), m);
  assert.ok(!requiresCsrf('GET'));
  assert.deepEqual(buildWriteHeaders('GET'), {});
  assert.deepEqual(buildWriteHeaders('PATCH', 'tok'), { 'X-CSRF-Token': 'tok' });
  assert.throws(() => buildWriteHeaders('POST'), /CSRF token 缺失/);
});

test('pulls fixture：data_mode=fixture，形状符合契约 §2（current_head_sha / latest_result.stale）', () => {
  const fx = JSON.parse(readFileSync(
    fileURLToPath(new URL('../../frontend/src/fixtures/pulls.fixture.json', import.meta.url)), 'utf8'));
  assert.equal(fx.data_mode, 'fixture');
  for (const it of fx.items) {
    assert.ok(/^[0-9a-f]{40}$/.test(it.current_head_sha), '当前 head 为 40hex 权威值');
    assert.equal(typeof it.latest_result.stale, 'boolean');
    assert.ok(it.latest_run && it.latest_result);
  }
  assert.equal(fx.items[1].latest_result.stale, true, '第二个场景：旧 head 结论必须 stale');
});

test('权威 head 关联：结果按 head 匹配才归当前；进行中=head 无完成结果；其余全 stale', () => {
  const runs = [
    { run_id: 'old-ok', head_sha: 'B'.repeat(40), created_at: '2026-09-20T10:00:00Z', execution: { status: 'PROCESSED' }, review: { verdict: 'NOT_CONFIRMED' } },
    { run_id: 'cur-run', head_sha: 'a'.repeat(40), created_at: '2026-09-23T11:00:00Z', execution: { status: 'RUNNING' } },
  ];
  const assoc = associateCurrentHead(runs, 'a'.repeat(40));
  assert.equal(assoc.state, 'running_no_result', '当前 head 只有进行中的 run');
  assert.equal(assoc.completedRun, null);
  assert.equal(assoc.staleRuns.length, 1);
  assert.equal(assoc.staleRuns[0].run_id, 'old-ok', "旧 head 的 NOT_CONFIRMED 不得升为当前结论");

  const none = associateCurrentHead(runs, 'f'.repeat(40));
  assert.equal(none.state, 'no_run_on_current_head');
  assert.equal(none.staleRuns.length, 2);
});

test('无权威 head（null）→ 返回 null，调用方维持"最近记录"展示', () => {
  assert.equal(associateCurrentHead([{ run_id: 'r', head_sha: 'aa' }], null), null);
  assert.equal(associateCurrentHead([], ''), null);
});

test('console_v3 适配：mode 标签原样保留，不伪造审查结论', () => {
  const rec = v3RunToRecord({
    run_id: 'v3-1', repo: 'acme/widget', pr_number: 9, head_sha: 'ab'.repeat(20),
    mode: 'fixture', outcome: 'REVIEW_COMPLETED_OK', risk_tier: 'high',
    coverage_missing: ['patch_validation'], superseded: false, updated_at: '2026-09-23T12:00:00Z',
  });
  assert.equal(rec._v3.mode, 'fixture', 'shadow/fixture 标签永不丢失');
  assert.equal(rec.review.verdict, null, 'v3 只读模型无独立结论字段——不伪造');
  assert.equal(rec.execution.source, 'console_v3 (read-only)');
});
