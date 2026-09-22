// pr-model.test.mjs — PR 聚合口径验证（批次 A 的验收矩阵固化）
// 运行：node --test console/backend/test/pr-model.test.mjs

import test from 'node:test';
import assert from 'node:assert/strict';
import {
  prKeyOf, groupRunsByPr, groupRunsByHead, attentionOf, reposFromPrs, paginate,
} from '../../frontend/src/pr-model.js';

const run = (over = {}) => ({
  pack_id: 'PACK', run_id: 'run', repo: 'acme/widget', pr_number: 3,
  pr_title: 'Add widget', pr_url: 'https://github.com/acme/widget/pull/3',
  head_sha: 'a'.repeat(40), created_at: '2026-09-19T10:00:00Z',
  execution: { status: 'PROCESSED' }, review: { verdict: 'FINDING_CONFIRMED', severity: 'HIGH' },
  ...over,
});

test('同仓库同 PR 多 run 聚合为一个 PR，runs 按时间倒序，latest 是最近一次', () => {
  const prs = groupRunsByPr([
    run({ pack_id: 'OLD', created_at: '2026-09-16T10:00:00Z' }),
    run({ pack_id: 'NEW', created_at: '2026-09-19T10:00:00Z' }),
    run({ pack_id: 'MID', created_at: '2026-09-18T10:00:00Z' }),
  ]);
  assert.equal(prs.length, 1);
  assert.equal(prs[0].runs.length, 3);
  assert.equal(prs[0].latest.pack_id, 'NEW');
  assert.equal(prs[0].activityAt, '2026-09-19T10:00:00Z');
  assert.deepEqual(prs[0].attention, { flag: 'decision', label: '有待处理发现（最近记录）' });
});

test('不同仓库相同 PR 编号不合并', () => {
  const prs = groupRunsByPr([
    run({ repo: 'acme/widget' }),
    run({ repo: 'acme/other', pr_url: 'https://github.com/acme/other/pull/3' }),
  ]);
  assert.equal(prs.length, 2);
  assert.ok(prs.every((p) => p.runs.length === 1));
});

test('无 repo 或 pr_number 的 run 不进入 PR 聚合', () => {
  assert.equal(prKeyOf(run({ pr_number: null })), null);
  assert.equal(prKeyOf(run({ repo: null })), null);
  assert.equal(groupRunsByPr([run({ pr_number: null }), run()]).length, 1);
});

test('旧 head 结果不冒充当前结论：聚合体无 current 字段，head 分组各自保留最近记录', () => {
  const prs = groupRunsByPr([
    run({ pack_id: 'H1', head_sha: 'a'.repeat(40), created_at: '2026-09-16T10:00:00Z' }),
    run({ pack_id: 'H2', head_sha: 'b'.repeat(40), created_at: '2026-09-19T10:00:00Z' }),
  ]);
  const pr = prs[0];
  assert.ok(!('currentHead' in pr), 'snapshot 无当前 head 权威，不得产出 currentHead');
  assert.ok(!('currentVerdict' in pr));
  assert.equal(pr.heads.length, 2);
  assert.equal(pr.heads[0].head, 'b'.repeat(40), '按最近活动排序 head 组');
  assert.equal(pr.heads[0].latest.pack_id, 'H2');
  assert.equal(pr.heads[1].latest.pack_id, 'H1');
  // head 未记录的 run 单独成组（head=null）
  const withNull = groupRunsByHead([run({ head_sha: null }), run()]);
  assert.equal(withNull.length, 2);
  assert.ok(withNull.some((h) => h.head === null));
});

test('PR 数与 run 数分别统计，不互相冒充', () => {
  const prs = groupRunsByPr([
    run({ pr_number: 1 }), run({ pr_number: 1, pack_id: 'P2' }),
    run({ pr_number: 2, pack_id: 'P3' }),
  ]);
  const repos = reposFromPrs(prs);
  assert.equal(repos.length, 1);
  assert.equal(repos[0].prCount, 2, '按 PR 统计 = 2');
  assert.equal(repos[0].runCount, 3, '按 run 统计 = 3');
});

test('attentionOf 只看所给 run：已拒绝修复与未发现问题分别成立，跨 run 不推导', () => {
  assert.equal(attentionOf(run({ review: { verdict: 'FINDING_CONFIRMED' } })).flag, 'decision');
  assert.equal(attentionOf(run({ review: { verdict: null, human_gate: 'REJECTED' } })).flag, 'blocked');
  assert.equal(attentionOf(run({ review: { verdict: 'NOT_CONFIRMED' } })).flag, 'clear');
  assert.equal(attentionOf(run({ review: {} })).flag, 'unknown');
  assert.equal(attentionOf(null).flag, 'unknown');
});

test('latestCompleted 只认执行终态（PROCESSED/COMPLETED），全未完成为 null', () => {
  const prs = groupRunsByPr([
    run({ pack_id: 'DONE', execution: { status: 'PROCESSED' }, created_at: '2026-09-16T10:00:00Z' }),
    run({ pack_id: 'RUN', execution: { status: 'RUNNING' }, created_at: '2026-09-19T10:00:00Z' }),
  ]);
  assert.equal(prs[0].latest.pack_id, 'RUN', 'latest 是最近一次（哪怕是进行中）');
  assert.equal(prs[0].latestCompleted.pack_id, 'DONE', 'latestCompleted 是最近的完成态');
  const none = groupRunsByPr([run({ execution: { status: 'BLOCKED' } })]);
  assert.equal(none[0].latestCompleted, null, 'BLOCKED 是受控停止，不算完成');
});

test('标题取同 PR 任一记录中最近的非空标题（仅部分历史包有记录）', () => {
  const prs = groupRunsByPr([
    run({ pack_id: 'HAS_TITLE', pr_title: 'Real PR title', created_at: '2026-09-16T10:00:00Z' }),
    run({ pack_id: 'NO_TITLE', pr_title: null, created_at: '2026-09-19T10:00:00Z' }),
  ]);
  assert.equal(prs[0].title, 'Real PR title');
});

test('分页：页码钳制、末页切片、总数正确', () => {
  const items = Array.from({ length: 23 }, (_, i) => i);
  const p1 = paginate(items, 1, 10);
  assert.deepEqual([p1.total, p1.pages, p1.items.length], [23, 3, 10]);
  const p3 = paginate(items, 3, 10);
  assert.equal(p3.items.length, 3);
  const over = paginate(items, 99, 10);
  assert.equal(over.page, 3, '超界页码钳制到最后一页');
  assert.deepEqual(paginate([], 1, 10), { page: 1, perPage: 10, total: 0, pages: 1, items: [] });
});
