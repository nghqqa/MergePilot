// publish-remote-guard.test.mjs — 发布链远端守卫隔离验证（2026-10-18 发布契约收口；同日审阅补强）。
// 背景：试用栈实测「远端 head 无法读取（No key provided to sign）」时发布预览可见、
// 前端确认按钮仍启用——需实证后端拒写正确且**零远端写调用**。
//
// 审阅补强（2026-10-18 独立审阅）：
//   * fetch stub 按【HTTP 方法+精确路径】匹配；任何未注册调用直接抛错（宽匹配会掩盖问题）；
//   * 显式统计评论写调用（POST/PATCH …/issues/:n/comments）——「零 INSERT」只证明零落库，
//     零远端写必须用写端点调用计数证明；三个场景都断言计数；
//   * 正常路径断言恰好一次 POST、零 PATCH（原位更新路径的 PATCH 计数留待带既有评论的用例）。
// 运行：cd console/backend && node --test test/publish-remote-guard.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { generateKeyPairSync } from 'node:crypto';
import { publishRun } from '../lib/multiuser/publish.mjs';

const RUN = {
  run_id: '1f0f0f0f-0000-4000-8000-000000000001',
  status: 'COMPLETED',
  head_sha: 'a'.repeat(40),
  llm_mode: 'test',
  pr_id: 'p'.repeat(36),
  repo_id: 'r'.repeat(36),
  provider_pr_number: 777,
  provider: 'github',
  owner: 'demo-owner',
  repo_name: 'demo-repo',
  installation_id: 12345,
};

const TOKEN_URL = `https://api.github.com/app/installations/${RUN.installation_id}/access_tokens`;
const PR_URL = `https://api.github.com/repos/${RUN.owner}/${RUN.repo_name}/pulls/${RUN.provider_pr_number}`;
const COMMENTS_URL = `https://api.github.com/repos/${RUN.owner}/${RUN.repo_name}/issues/${RUN.provider_pr_number}/comments`;

// 严格 stub：仅响应注册表中的 `${method} ${url}`；未注册调用抛错并记录（不宽匹配）。
function strictFetch(routes) {
  const commentWrites = { POST: 0, PATCH: 0 }; // 评论写端点调用计数（按方法）
  const seen = [];
  const fn = async (input, init) => {
    const method = String(init?.method ?? 'GET').toUpperCase();
    const url = String(input);
    seen.push(`${method} ${url}`);
    if (url === COMMENTS_URL && (method === 'POST' || method === 'PATCH')) commentWrites[method] += 1;
    const handler = routes[`${method} ${url}`];
    if (!handler) throw new Error(`unexpected fetch: ${method} ${url}`);
    return handler();
  };
  fn.commentWrites = commentWrites;
  fn.seen = seen;
  return fn;
}

function makeQ() {
  const calls = [];
  const q = async (text) => {
    calls.push(String(text));
    if (String(text).includes('FROM mu.review_run')) return { rows: [{ ...RUN }] };
    return { rows: [] };
  };
  return { q, calls };
}

const writeCallsZero = (fw) => {
  assert.equal(fw.commentWrites.POST + fw.commentWrites.PATCH, 0,
    `守卫拒绝后不得有任何评论写调用：${fw.seen.filter((s) => s.includes('/issues/')).join(' | ')}`);
};
const dbWritesZero = (calls) => {
  const inserts = calls.filter((c) => /INSERT INTO/i.test(c));
  assert.deepEqual(inserts, [], `不得有任何落库写：${inserts.join(' | ')}`);
};

test('远端凭据缺失：拒写 remote_unreachable，评论写调用计数=0，零落库', async () => {
  const fw = strictFetch({}); // 空注册表：任何 fetch 都算意外调用（凭据缺失应在 fetch 前失败）
  globalThis.fetch = fw;
  const { q, calls } = makeQ();
  const r = await publishRun(q, { env: {}, runId: RUN.run_id, tenantId: 't', userId: 'u' });
  assert.equal(r.ok, false);
  assert.equal(r.stage, 'remote_unreachable');
  assert.match(r.reason, /key|sign|jwt/i);
  writeCallsZero(fw);
  dbWritesZero(calls);
});

test('远端 head 已变化：拒写 stale_head，评论写调用计数=0（仅 token+GET PR），零落库', async (t) => {
  const pem = generateKeyPairSync('rsa', { modulusLength: 2048 }).privateKey.export({ type: 'pkcs1', format: 'pem' });
  const env = { MU_GITHUB_APP_ID: '123', MU_GITHUB_APP_PRIVATE_KEY: pem };
  const fw = strictFetch({
    [`POST ${TOKEN_URL}`]: () => ({ ok: true, status: 201, json: async () => ({ token: 'fake-st-token' }) }),
    [`GET ${PR_URL}`]: () => ({ ok: true, status: 200, json: async () => ({ state: 'open', head: { sha: 'b'.repeat(40), ref: 'feature' }, base: { sha: '' } }) }),
  });
  globalThis.fetch = fw;
  t.after(() => { globalThis.fetch = undefined; });
  const { q, calls } = makeQ();
  const r = await publishRun(q, { env, runId: RUN.run_id, tenantId: 't', userId: 'u' });
  assert.equal(r.ok, false);
  assert.equal(r.stage, 'stale_head');
  assert.ok(fw.seen.every((s) => !s.includes(COMMENTS_URL)), '不得触碰评论端点（读也不必）');
  writeCallsZero(fw);
  dbWritesZero(calls);
});

test('head 一致：正常发布路径保留——恰好一次评论 POST、零 PATCH、零意外调用', async (t) => {
  const pem = generateKeyPairSync('rsa', { modulusLength: 2048 }).privateKey.export({ type: 'pkcs1', format: 'pem' });
  const env = { MU_GITHUB_APP_ID: '123', MU_GITHUB_APP_PRIVATE_KEY: pem };
  const fw = strictFetch({
    [`POST ${TOKEN_URL}`]: () => ({ ok: true, status: 201, json: async () => ({ token: 'fake-ok-token' }) }),
    [`GET ${PR_URL}`]: () => ({ ok: true, status: 200, json: async () => ({ state: 'open', head: { sha: RUN.head_sha, ref: 'feature' }, base: { sha: '' } }) }),
    [`GET ${COMMENTS_URL}`]: () => ({ ok: true, status: 200, json: async () => [] }),
    [`POST ${COMMENTS_URL}`]: () => ({ ok: true, status: 201, json: async () => ({ id: 42, html_url: 'https://demo/comment/42' }) }),
  });
  globalThis.fetch = fw;
  t.after(() => { globalThis.fetch = undefined; });
  const { q, calls } = makeQ();
  const r = await publishRun(q, { env, runId: RUN.run_id, tenantId: 't', userId: 'u', detailBaseUrl: null });
  assert.equal(r.ok, true);
  assert.equal(r.remoteCommentId, '42');
  assert.equal(fw.commentWrites.POST, 1, '恰好一次评论创建');
  assert.equal(fw.commentWrites.PATCH, 0, '无 PATCH（新建路径）');
  assert.ok(calls.some((c) => /INSERT INTO\s+mu\.pr_publish/i.test(c)), '正常路径应落发布记录');
});
