// mu-console-api-overview.test.mjs — /api/overview 投影契约测试（发布收口修复）。
// 纯 mock pool，无 PG：锁定顶层附加字段与行级 head_basis/head_count 投影，
// 以及多 head 归并、截断声明与向后兼容（行缺可选列不 500）。
// 运行：cd console/backend && node --test test/mu-console-api-overview.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { createMuConsoleApi } from '../lib/mu-console-api.mjs';

// mock pool：按 SQL 特征路由返回 { rows }
function mockPool(routes) {
  return {
    async query(text) {
      const t = text.replace(/\s+/g, ' ');
      for (const [match, rows] of routes) {
        if (t.includes(match)) return { rows: typeof rows === 'function' ? rows(t) : rows };
      }
      return { rows: [] };
    },
  };
}

const TENANT = 't-1';
const baseRow = (over = {}) => ({
  pr_id: 'pr-1', provider_pr_number: 8, title: 't', state: 'open',
  head_sha: 'a1b2c3d4e5f6', updated_at: '2026-10-05T03:00:00Z',
  owner: 'nghqqa', repo_name: 'demo',
  stage: 'BLOCKED', stage_source: 'mu_review_run',
  run_id: 'run-8', latest: '2026-10-05T03:00:00Z',
  ...over,
});

function routesFor(prRows, { extraHeads = 0 } = {}) {
  return [
    ['FROM mu.repository r', [{ repo: 'nghqqa/demo', prs: 1, runs: 1, pending: 0 }]],
    ['ORDER BY pr.updated_at DESC', prRows],
    ['FROM mu.review_run WHERE tenant_id=$1 GROUP BY 1', []], // trend
    ['GROUP BY status', []], // stage
    ['FROM mu.agent_finding', []], // findings
    ["status IN ('BLOCKED','REWORK_REQUIRED')", []], // blocked
    ["status IN ('FIX_QUEUED'", [{ c: 0 }]], // pending
  ];
}

test('overview：顶层附加字段存在（prs_truncated/prs_projection_limit/head_authority）', async () => {
  const api = createMuConsoleApi({ pool: mockPool(routesFor([baseRow()])) });
  const out = await api.overview(TENANT);
  assert.equal(out.prs_truncated, false);
  assert.equal(out.prs_projection_limit, 50);
  assert.ok(String(out.head_authority).includes('event_order'));
  assert.equal(out.source, 'MU_CANONICAL_LIVE');
});

test('overview：每个 prs 行保留 head_basis/head_count（多 head 行各自携带 head_count）', async () => {
  const prRows = [
    baseRow({ pr_id: 'pr-a', provider_pr_number: 8, head_sha: 'cur8', updated_at: '2026-10-05T03:00:00Z' }),
    baseRow({ pr_id: 'pr-a', provider_pr_number: 8, head_sha: 'old8', updated_at: '2026-10-04T03:00:00Z' }),
    baseRow({ pr_id: 'pr-a', provider_pr_number: 8, head_sha: 'old8b', updated_at: '2026-10-03T03:00:00Z' }),
    baseRow({ pr_id: 'pr-b', provider_pr_number: 9, head_sha: 'cur9', updated_at: '2026-10-02T00:00:00Z' }),
  ];
  const api = createMuConsoleApi({ pool: mockPool(routesFor(prRows)) });
  const out = await api.overview(TENANT);
  // 后端契约 = 每 head 一行（PR 去重归并由前端 groupRowsByPr 承担）
  assert.equal(out.prs.length, 4, '4 行（每 head 一行）');
  const rowsA = out.prs.filter((p) => p.pr === 8);
  assert.equal(rowsA.length, 3, '#8 的 3 个 head 行');
  for (const row of rowsA) {
    assert.equal(row.head_basis, 'event_order');
    assert.equal(row.head_count, 3, '#8 每行 head_count=该 PR 在投影内的 head 行数');
  }
  const rowsB = out.prs.filter((p) => p.pr === 9);
  assert.equal(rowsB[0].head_basis, 'event_order');
  assert.equal(rowsB[0].head_count, 1, '#9 单行 head_count=1');
  // 与 /api/mu/prs 行级契约同名同型：字符串 basis + 正整数 count
  for (const row of out.prs) {
    assert.equal(typeof row.head_basis, 'string');
    assert.equal(typeof row.head_count, 'number');
    assert.ok(Number.isInteger(row.head_count) && row.head_count >= 1);
  }
});

test('overview：向后兼容——行缺可选列/字段为空时不 500、字段仍投影', async () => {
  // 模拟旧数据：缺 run_id/latest/head_sha 为 null、stage_source 为空
  const legacyRow = baseRow({ run_id: null, latest: null, head_sha: null, stage_source: '' });
  const api = createMuConsoleApi({ pool: mockPool(routesFor([legacyRow])) });
  const out = await api.overview(TENANT);
  assert.equal(out.prs.length, 1);
  const row = out.prs[0];
  assert.equal(row.head_basis, 'event_order', '旧数据行仍获得 head_basis 投影');
  assert.equal(row.head_count, 1);
  assert.equal(row.head_sha, null, '缺列如实为 null，不虚构');
  assert.equal(row.stage_source, '', '空 stage_source 原样透出');
  assert.equal(out.total_prs, 1);
});

test('overview：prs_truncated=true 时顶层与行级字段仍可读（触顶切片）', async () => {
  // 构造 51 行触发触顶探测
  const many = Array.from({ length: 51 }, (_, i) =>
    baseRow({ pr_id: 'pr-' + i, provider_pr_number: 9000 + i, head_sha: 'h' + i, updated_at: new Date(Date.parse('2026-10-05T00:00:00Z') - i * 60000).toISOString() }));
  const routes = routesFor(many);
  const api = createMuConsoleApi({ pool: { async query(text) { const t = text.replace(/\s+/g, ' '); for (const [m, rows] of routes) { if (t.includes(m)) return { rows }; } return { rows: [] }; } } });
  const out = await api.overview(TENANT);
  assert.equal(out.prs_truncated, true, '51 行 > 50 触顶');
  assert.equal(out.prs.length, 50, '切片至投影上限');
  assert.equal(out.total_prs, 50);
  assert.ok(out.prs.every((p) => p.head_basis === 'event_order' && Number.isInteger(p.head_count)), '切片后行级字段仍完整');
});

test('overview：pr 行极简形状（缺 owner/name → repo 名缺失兜底）不 500', async () => {
  const api = createMuConsoleApi({ pool: mockPool(routesFor([{ pr_id: 'pr-x', provider_pr_number: 1 }])) });
  const out = await api.overview(TENANT);
  assert.equal(out.prs.length, 1);
  assert.ok(out.prs[0].head_basis === 'event_order');
});
