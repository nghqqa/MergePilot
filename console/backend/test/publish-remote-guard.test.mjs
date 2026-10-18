// publish-remote-guard.test.mjs — 发布链远端守卫隔离验证（2026-10-18 发布契约收口）。
// 背景：试用栈实测「远端 head 无法读取（No key provided to sign）」时发布预览可见、
// 前端确认按钮仍启用——需实证后端拒写正确且**零远端写调用**（守卫先于任何写）。
// 本测试直接驱动 publishRun（注入查询函数 + stub 全局 fetch），断言：
//   1) 远端凭据缺失 → stage=remote_unreachable，查询日志零 INSERT（零远端写、零落库）；
//   2) 远端 head 已变化 → stage=stale_head 409 语义，零 INSERT，fetch 序列只含 token+GET PR；
//   3) head 一致 + 评论 POST 201 → ok:true 正常发布（重试路径保留，不放宽任何条件）。
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

function makeQ() {
  const calls = [];
  const q = async (text) => {
    calls.push(String(text));
    if (String(text).includes('FROM mu.review_run')) return { rows: [{ ...RUN }] };
    return { rows: [] };
  };
  return { q, calls };
}

const ARGV = { env: {} };

test('远端凭据缺失：拒写 remote_unreachable，零 INSERT（零远端写、零落库）', async () => {
  const { q, calls } = makeQ();
  const r = await publishRun(q, { env: {}, runId: RUN.run_id, tenantId: 't', userId: 'u' });
  assert.equal(r.ok, false);
  assert.equal(r.stage, 'remote_unreachable');
  assert.match(r.reason, /key|sign|jwt/i);
  const inserts = calls.filter((c) => /INSERT INTO/i.test(c));
  assert.deepEqual(inserts, [], `不得有任何落库写：${inserts.join(' | ')}`);
});

test('远端 head 已变化：拒写 stale_head，fetch 序列只含 token+GET PR（零写）', async (t) => {
  const pem = generateKeyPairSync('rsa', { modulusLength: 2048 }).privateKey.export({ type: 'pkcs1', format: 'pem' });
  const env = { MU_GITHUB_APP_ID: '123', MU_GITHUB_APP_PRIVATE_KEY: pem };
  const seen = [];
  const origFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = origFetch; });
  globalThis.fetch = async (input, init) => {
    const u = String(input);
    if (u.includes('/app/installations/')) {
      return { ok: true, status: 201, json: async () => ({ token: 'fake-st-token' }) };
    }
    if (u.includes('/pulls/')) {
      return { ok: true, status: 200, json: async () => ({ state: 'open', head: { sha: 'b'.repeat(40), ref: 'feature' }, base: { sha: '' } }) };
    }
    throw new Error(`unexpected fetch: ${u}`);
  };
  const { q, calls } = makeQ();
  const r = await publishRun(q, { env, runId: RUN.run_id, tenantId: 't', userId: 'u' });
  assert.equal(r.ok, false);
  assert.equal(r.stage, 'stale_head');
  assert.ok(seen.every((u) => !/issues\/comments/.test(u)), '不得调用评论写端点');
  const inserts = calls.filter((c) => /INSERT INTO/i.test(c));
  assert.deepEqual(inserts, [], `不得有任何落库写：${inserts.join(' | ')}`);
});

test('head 一致：正常发布路径保留（ok:true，落库发布记录）', async (t) => {
  const pem = generateKeyPairSync('rsa', { modulusLength: 2048 }).privateKey.export({ type: 'pkcs1', format: 'pem' });
  const env = { MU_GITHUB_APP_ID: '123', MU_GITHUB_APP_PRIVATE_KEY: pem };
  const origFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = origFetch; });
  globalThis.fetch = async (input, init) => {
    const u = String(input);
    const method = String(init?.method ?? 'GET').toUpperCase();
    if (u.includes('/app/installations/')) {
      return { ok: true, status: 201, json: async () => ({ token: 'fake-ok-token' }) };
    }
    if (u.includes('/pulls/')) {
      return { ok: true, status: 200, json: async () => ({ state: 'open', head: { sha: RUN.head_sha, ref: 'feature' }, base: { sha: '' } }) };
    }
    if (u.includes('/issues/')) {
      return { ok: true, status: method === 'POST' ? 201 : 200, json: async () => (method === 'POST' ? { id: 42, html_url: 'https://demo/comment/42' } : []) };
    }
    throw new Error(`unexpected fetch: ${method} ${u}`);
  };
  const { q, calls } = makeQ();
  const r = await publishRun(q, { env, runId: RUN.run_id, tenantId: 't', userId: 'u', detailBaseUrl: null });
  assert.equal(r.ok, true);
  assert.equal(r.remoteCommentId, '42');
  assert.ok(calls.some((c) => /INSERT INTO\s+mu\.pr_publish/i.test(c)), '正常路径应落发布记录');
});
