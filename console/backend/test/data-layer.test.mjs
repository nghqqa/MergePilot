// data-layer.test.mjs — 数据源注入机制契约测试（页面级接入准备）
// 运行：node --test console/backend/test/data-layer.test.mjs

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { normalizeConfig } from '../../frontend/src/data/config.js';
import { createDataSource, createRaceGuard } from '../../frontend/src/data/sources.js';
import { prViewFromContract, attentionFromContract } from '../../frontend/src/data/pr-view.js';

const CONTRACT_HEALTH = {
  ok: true, data_mode: 'fixture',
  sources: { primary: 'contract', contract_v2: { available: true, data_mode: 'fixture' } },
  declared_repos: ['acme/widget', 'acme/other'],
};

test('配置归一：服务未声明 → 保守 snapshot；仅显式声明 contract+available 才启用', () => {
  assert.equal(normalizeConfig(null).mode, 'snapshot', '配置不可得 → snapshot（不猜测）');
  assert.equal(normalizeConfig({}).mode, 'snapshot');
  assert.equal(normalizeConfig({ sources: { primary: 'contract' } }).mode, 'snapshot', '声明缺 available 不启用');
  assert.equal(normalizeConfig({ sources: { primary: 'contract', contract_v2: { available: false } } }).mode, 'snapshot');
  assert.equal(normalizeConfig(CONTRACT_HEALTH).mode, 'contract');
  assert.deepEqual(normalizeConfig(CONTRACT_HEALTH).declaredRepos, ['acme/widget', 'acme/other']);
  // 模式不受 URL/sessionStorage 类输入影响：normalize 只消费服务响应体
  assert.equal(normalizeConfig({ mode: 'live', query: '?mode=live' }).mode, 'snapshot');
});

function fetchFromRoutes(routes) {
  return async (url, init) => {
    const u = new URL(url, 'http://x');
    const key = `${u.pathname}${u.search}`;
    for (const [pattern, handler] of routes) {
      if (typeof pattern === 'string' ? key === pattern || key.startsWith(pattern) : pattern.test(key)) {
        return handler(u, init ?? {});
      }
    }
    return { ok: false, status: 404, json: async () => ({ error: { code: 404, reason: 'not_found', message: 'no route' } }) };
  };
}

test('源分离：contract 源只调用契约端点，绝不读 snapshot /api/runs', async () => {
  const calls = [];
  const fetchImpl = fetchFromRoutes([
    ['/api/pulls?', (u) => {
      calls.push(u.pathname + u.search);
      const repo = u.searchParams.get('repo');
      return {
        ok: true, status: 200,
        json: async () => ({ data_mode: 'fixture', total: 0, items: [] }),
      };
    }],
    ['/api/runs', () => { calls.push('/api/runs'); return { ok: true, status: 200, json: async () => ({ items: [{ run_id: 'x', repo: 'r/x', pr_number: 1 }] }) }; }],
  ]);
  const src = createDataSource(normalizeConfig(CONTRACT_HEALTH), fetchImpl);
  await src.listPrs('acme/widget');
  assert.ok(calls.every((c) => c.startsWith('/api/pulls')), `contract 源仅可调用契约端点，实际：${calls}`);
  assert.ok(!calls.includes('/api/runs'), 'contract 源不得混用 snapshot /api/runs');
});

test('snapshot 源只调用 /api/runs，不调用契约端点', async () => {
  const calls = [];
  const fetchImpl = async (url) => {
    calls.push(new URL(url, 'http://x').pathname);
    return { ok: true, status: 200, json: async () => ({ items: [], total: 0 }) };
  };
  const src = createDataSource(normalizeConfig({ sources: { primary: 'snapshot' } }), fetchImpl);
  await src.listPrs('acme/widget');
  assert.ok(calls.every((c) => c === '/api/runs'), `snapshot 源仅可调用 /api/runs，实际：${calls}`);
});

test('契约源失败不回退：404/错误原样抛出（错误不冒充空数据）', async () => {
  const fetchImpl = fetchFromRoutes([
    ['/api/pulls?', () => ({
      ok: false, status: 404,
      json: async () => ({ error: { code: 404, reason: 'repo_not_found', message: 'no fixture repo' } }),
    })],
  ]);
  const src = createDataSource(normalizeConfig(CONTRACT_HEALTH), fetchImpl);
  await assert.rejects(() => src.listPrs('no/such'), (e) => e.status === 404 && e.reason === 'repo_not_found');
});

test('竞态守卫：cancel 后一切 token 失效；新代次使旧代次过期', () => {
  const g = createRaceGuard();
  const a = g.next();
  assert.ok(g.isCurrent(a));
  const b = g.next();
  assert.ok(!g.isCurrent(a), '旧代次过期');
  assert.ok(g.isCurrent(b));
  g.cancel();
  assert.ok(!g.isCurrent(b), '卸载/取消后全部失效');
});

test('契约视图映射：pending_tickets 为真实待办；stale HIGH 不生成当前待办', () => {
  const base = { repo: 'acme/widget', pr_number: 9, state: 'open', title: 't',
    current_head_sha: 'c'.repeat(40) };
  // 有效票据 → pending_tickets（后端权威）
  let a = attentionFromContract({ ...base, has_pending_tickets: 2, latest_result: { stale: false, verdict: 'NOT_CONFIRMED' } });
  assert.equal(a.flag, 'pending_tickets');
  // 当前 head 的 FINDING_CONFIRMED → decision
  a = attentionFromContract({ ...base, latest_result: { stale: false, verdict: 'FINDING_CONFIRMED' } });
  assert.equal(a.flag, 'decision');
  // 旧 head 的 FINDING_CONFIRMED → 仅"历史需关注"，不是当前待办
  a = attentionFromContract({ ...base, latest_result: { stale: true, verdict: 'FINDING_CONFIRMED' } });
  assert.equal(a.flag, 'stale_attention');
  assert.match(a.label, /历史记录中需关注/);
  // 当前 head 无完成结果
  a = attentionFromContract({ ...base, latest_result: { stale: true, verdict: 'NOT_CONFIRMED' } });
  assert.equal(a.flag, 'stale_only');
});

test('契约视图：stale 标记透传到 review；当前 head 透传；runCountExact=false', () => {
  const item = {
    repo: 'acme/widget', pr_number: 10, title: 'x', state: 'open',
    current_head_sha: 'd'.repeat(40),
    latest_run: { run_id: 'r2', status: 'RUNNING', class: 'execution', mode: 'on', started_at: '2026-09-23T11:00:00Z' },
    latest_result: { run_id: 'r1', head_sha: 'e'.repeat(40), stale: true, verdict: 'FINDING_CONFIRMED', severity: 'HIGH' },
    has_pending_tickets: 0,
  };
  const v = prViewFromContract(item);
  assert.equal(v.kind, 'contract');
  assert.equal(v.currentHead, 'd'.repeat(40));
  assert.equal(v.review.stale, true);
  assert.equal(v.runCountExact, false);
  assert.equal(v.activityAt, '2026-09-23T11:00:00Z');
});

test('pulls fixture 文件本身满足契约形状并带 fixture 标记', () => {
  const fx = JSON.parse(readFileSync(
    fileURLToPath(new URL('../../frontend/src/fixtures/pulls.fixture.json', import.meta.url)), 'utf8'));
  assert.equal(fx.data_mode, 'fixture');
  for (const it of fx.items) {
    const v = prViewFromContract(it);
    assert.equal(v.kind, 'contract');
    assert.ok(v.prNumber != null && v.repo);
  }
});
