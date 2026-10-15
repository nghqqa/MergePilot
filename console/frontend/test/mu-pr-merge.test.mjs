// mu-pr-merge.test.mjs — UX-01 MU 仓库 PR 列表归并纯函数测试（node --test，无 DOM）。
// 判别点：同 PR 多 head 归并、当前 head 推定与「未确认」、旧 head run/票不冒充当前事实、
// REVIEWED/COMPLETED 不映射为通过、跨仓库同编号隔离、权威待办区分当前/历史 head、
// 窗口截断标记、缺字段不伪造 0/通过/无待办。
import test from 'node:test';
import assert from 'node:assert/strict';
import { mergeMuRepoPrs } from '../src/data/mu-pr-merge.js';
import { reviewStageDecisionMap, leaderDecisionMap } from '../src/status-map.js';

const REPO = 'acme/app';

test('同 PR 多 head 行归并为一行；当前 head=服务端事件序首行且恒标未确认', () => {
  const prRows = [
    { provider_pr_number: 12, head_sha: 'be8789d6cf2c', title: 'fix discount', state: 'open',
      updated_at: '2026-10-15T08:00:00Z', created_at: '2026-10-15T07:00:00Z', branch_protection_status: 'unknown' },
    { provider_pr_number: 12, head_sha: 'e48d5979ed3f', title: 'fix discount', state: 'open',
      updated_at: '2026-10-14T08:00:00Z', created_at: '2026-10-14T07:00:00Z', branch_protection_status: 'unknown' },
    { provider_pr_number: 7, head_sha: '1149d9a48124', title: null, state: 'open',
      updated_at: '2026-10-01T00:00:00Z', created_at: '2026-10-01T00:00:00Z' },
  ];
  const { prs, meta } = mergeMuRepoPrs({ repo: REPO, prRows, runRows: [], pendingTickets: [] });
  assert.equal(prs.length, 2, '两个 PR（12/7）而非 3 行');
  const p12 = prs.find((p) => p.prNumber === 12);
  assert.equal(p12.currentHead, 'be8789d6cf2c', '当前 head=updated_at 最新的行');
  assert.equal(p12.currentHeadConfirmed, false, 'event_order 口径——恒未确认');
  assert.equal(p12.heads.length, 2, '两个 head 都保留');
  assert.equal(p12.key, `${REPO}#12`);
  assert.equal(p12.title, 'fix discount');
  assert.equal(meta.prRowsTruncated, false);
});

test('当前 head 无 run、旧 head 有 run：latestRun 为空 + staleRunHint 如实，不冒充当前结论', () => {
  const runRows = [
    { run_id: 'run-old', provider_pr_number: 12, head_sha: 'e48d5979ed3f', status: 'REVIEWED',
      findings_count: 3, top_severity: 'P1', created_at: '2026-10-14T09:00:00Z' },
  ];
  const { prs } = mergeMuRepoPrs({
    repo: REPO,
    prRows: [{ provider_pr_number: 12, head_sha: 'be8789d6cf2c', updated_at: '2026-10-15T08:00:00Z' }],
    runRows, pendingTickets: [],
  });
  const p = prs[0];
  assert.equal(p.latestRun, null, '当前 head 无 run——latestRun 空');
  assert.ok(p.staleRunHint, '旧 head 运行以 staleRunHint 提示');
  assert.equal(p.runState, null);
  assert.equal(p.findingsCount, null, '旧 head 的发现数不当作当前 head 事实');
  assert.equal(p.severityLabel, null);
  assert.equal(p.review.basis, 'stale-run-only');
});

test('REVIEWED 不映射为通过：仅输出运行状态+发现计数，verdict 恒空', () => {
  const runRows = [
    { run_id: 'run-1', provider_pr_number: 12, head_sha: 'be8789d6cf2c', status: 'REVIEWED',
      findings_count: 2, top_severity: 'P1', created_at: '2026-10-15T08:00:00Z' },
    { run_id: 'run-c', provider_pr_number: 13, head_sha: 'aa11bb22cc33', status: 'COMPLETED',
      findings_count: 0, top_severity: null, created_at: '2026-10-15T08:00:00Z' },
  ];
  const { prs } = mergeMuRepoPrs({
    repo: REPO,
    prRows: [
      { provider_pr_number: 12, head_sha: 'be8789d6cf2c', updated_at: '2026-10-15T08:00:00Z' },
      { provider_pr_number: 13, head_sha: 'aa11bb22cc33', updated_at: '2026-10-15T08:00:00Z' },
    ],
    runRows, pendingTickets: [],
  });
  const p12 = prs.find((x) => x.prNumber === 12);
  const p13 = prs.find((x) => x.prNumber === 13);
  assert.equal(p12.latestRun.status, 'REVIEWED');
  assert.equal(p12.review.review.verdict, undefined, '不伪造 verdict');
  assert.equal(p12.runState.label, '已审查');
  assert.ok(!/通过|无问题/.test(p12.runState.label), '运行状态标签不得表述为通过/无问题');
  assert.equal(p12.findingsCount, 2);
  assert.equal(p12.severityLabel, '高', 'top_severity P1 → 词表标签');
  assert.equal(p13.findingsCount, 0);
  assert.equal(p13.severityLabel, null, '0 发现不产生级别标签（0 是真实零值，级别未取得）');
});

test('权威 PENDING 票区分当前/历史 head；跨仓库同编号的票不混入', () => {
  const HEAD = 'be8789d6cf2c';
  const prRows = [{ provider_pr_number: 12, head_sha: HEAD, updated_at: '2026-10-15T08:00:00Z' }];
  const pendingTickets = [
    { pr_number: 12, repo_owner: 'acme', repo_name: 'app', head_sha: HEAD, severity: 'P1' },
    { pr_number: 12, repo_owner: 'acme', repo_name: 'app', head_sha: 'e48d5979ed3f', severity: 'P0' },
    { pr_number: 12, repo_owner: 'other', repo_name: 'app', head_sha: HEAD, severity: 'P0' }, // 跨仓库同编号
    { pr_number: 99, repo_owner: 'acme', repo_name: 'app', head_sha: HEAD, severity: 'P1' }, // 其他 PR
  ];
  const { prs } = mergeMuRepoPrs({ repo: REPO, prRows, runRows: [], pendingTickets });
  const p12 = prs.find((x) => x.prNumber === 12);
  assert.equal(p12.pendingTickets.current, 1, '仅统计本仓库且绑定当前 head 的票');
  assert.equal(p12.pendingTickets.historical, 1, '旧 head 票单独计数');
  assert.equal(p12.attention.flag, 'pending_tickets');
  assert.ok(p12.attention.label.includes('当前 head'), '标签表达当前 head 绑定');
  assert.ok(p12.attention.label.includes('旧 head'), '标签表达历史票绑定');
  const p99 = prs.find((x) => x.prNumber === 99);
  assert.ok(p99, '只有票、无 head 行的 PR 仍列出（不因单一来源截断丢行）');
  assert.equal(p99.currentHead, null);
  assert.ok(p99.attention.label.includes('head 未取得'), '当前 head 不可得时不得谎称「当前 head」或「旧 head」');
  assert.equal(p99.attention.flag, 'pending_tickets');
});

test('无 run 无票无 head 行 → attention 保持无待办但缺数据字段为 null（不伪造 0）', () => {
  const { prs } = mergeMuRepoPrs({ repo: REPO, prRows: [], runRows: [], pendingTickets: [] });
  assert.equal(prs.length, 0);
  // 有 head 行但什么都缺：
  const { prs: p2 } = mergeMuRepoPrs({
    repo: REPO,
    prRows: [{ provider_pr_number: 5, head_sha: 'ff00ff00ff00', updated_at: null }],
    runRows: [], pendingTickets: [],
  });
  const p = p2[0];
  assert.equal(p.latestRun, null);
  assert.equal(p.findingsCount, null);
  assert.equal(p.attention.label, '—', '无权威票数据时不显示「无待办」之外的内容——此处确为空集，label 为 —');
  assert.equal(p.currentHeadConfirmed, false);
});

test('窗口截断：任一端点达到 LIMIT 帽即输出 truncated 标记', () => {
  const prRows = Array.from({ length: 50 }, (_, i) => ({
    provider_pr_number: i + 1, head_sha: `h${i}`, updated_at: '2026-10-15T00:00:00Z',
  }));
  const { meta } = mergeMuRepoPrs({ repo: REPO, prRows, runRows: [], pendingTickets: [] });
  assert.equal(meta.prRowsTruncated, true, '50 行=服务端 LIMIT，可能少计');
  const { meta: meta2 } = mergeMuRepoPrs({
    repo: REPO,
    prRows: [{ provider_pr_number: 1, head_sha: 'h', updated_at: null }],
    runRows: Array.from({ length: 50 }, (_, i) => ({
      run_id: `r${i}`, provider_pr_number: 1, head_sha: 'h', status: 'REVIEWED',
      findings_count: 0, created_at: '2026-10-15T00:00:00Z',
    })),
    pendingTickets: [],
  });
  assert.equal(meta2.runsTruncated, true);
  assert.equal(meta2.prRowsTruncated, false);
});

test('同 run 多次出现在同 head：最新 created_at 在前，findingsCount 取最新 run', () => {
  const runRows = [
    { run_id: 'run-new', provider_pr_number: 12, head_sha: 'h1', status: 'REVIEWED',
      findings_count: 1, top_severity: 'P2', created_at: '2026-10-15T09:00:00Z' },
    { run_id: 'run-old', provider_pr_number: 12, head_sha: 'h1', status: 'REVIEWING',
      findings_count: 5, top_severity: 'P0', created_at: '2026-10-15T08:00:00Z' },
  ];
  const { prs } = mergeMuRepoPrs({
    repo: REPO,
    prRows: [{ provider_pr_number: 12, head_sha: 'h1', updated_at: '2026-10-15T09:00:00Z' }],
    runRows, pendingTickets: [],
  });
  assert.equal(prs[0].latestRun.runId, 'run-new', '当前 head 最新 run 权威');
  assert.equal(prs[0].findingsCount, 1);
});

test('词表：review_done 阶段 clean/findings_present 有映射；与 Leader 终裁值域互不混用', () => {
  const clean = reviewStageDecisionMap('clean');
  assert.equal(clean.tone, 'ok');
  assert.ok(clean.label.includes('无发现记录'));
  assert.ok(clean.note.includes('review_done'), 'note 标明阶段来源');
  const fp = reviewStageDecisionMap('findings_present');
  assert.equal(fp.tone, 'warn');
  // 终裁词表不认识 clean——保留原始枚举（fail-closed 不吞值）
  assert.equal(leaderDecisionMap('clean').label, 'clean');
  // 终裁正常路径不受影响
  assert.equal(leaderDecisionMap('needs_human').label, '需人工裁定');
});
