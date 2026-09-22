// live-v3.integration.test.mjs — console_v3 只读 HTTP 真实联调（环境门控）
//
// 运行方式（隔离环境）：
//   MERGEPILOT_V3_URL=http://127.0.0.1:4191 node --test console/backend/test/live-v3.integration.test.mjs
// 未设置 MERGEPILOT_V3_URL 时自动跳过（不伪装联调已发生）。
//
// 数据边界：console_v3 的数据自带 shadow/fixture 标签——本测试断言该标签存在且被适配层保留，
// 绝不把结果标为真实运行或历史快照。

import test from 'node:test';
import assert from 'node:assert/strict';

const BASE = process.env.MERGEPILOT_V3_URL;

test('console_v3 只读 HTTP 联调：列表→聚合→详情→404→只读边界', { skip: !BASE ? '未设置 MERGEPILOT_V3_URL' : false }, async (t) => {
  const { v3RunToRecord } = await import('../../frontend/src/api-live.js');
  const { groupRunsByPr } = await import('../../frontend/src/pr-model.js');

  // 1) 健康检查
  const health = await fetch(`${BASE}/healthz`);
  assert.equal(health.status, 200);

  // 2) run 列表（真实 HTTP）
  const listRes = await fetch(`${BASE}/api/runs`);
  assert.equal(listRes.status, 200, '/api/runs 应 200');
  const list = await listRes.json();
  assert.ok(Array.isArray(list.runs), '/api/runs 应返回 runs 数组');
  assert.match(String(list.data_mode), /shadow|fixture/, '服务自标 shadow/fixture——数据不是真实运行');
  t.diagnostic(`list data_mode=${list.data_mode} runs=${list.runs.length}`);

  // 3) 适配 → 聚合：repo/PR/head/run 关联在真实 HTTP 数据上成立，mode 标签保留
  const records = list.runs.map(v3RunToRecord);
  const prs = groupRunsByPr(records);
  t.diagnostic(`aggregated ${prs.length} PR(s) from ${records.length} run(s)`);
  assert.ok(prs.length >= 0);
  for (const pr of prs) {
    for (const r of pr.runs) {
      assert.ok(['shadow', 'fixture', 'on'].includes(r._v3.mode), `mode=${r._v3.mode} 为合法标签`);
    }
    assert.ok(pr.repo && pr.prNumber != null && pr.latest?.head_sha != null,
      `PR 聚合携带 repo/PR/head 关联：${pr.repo}#${pr.prNumber}`);
  }

  // 4) 单条 run 详情
  if (list.runs.length) {
    const first = list.runs[0];
    const detRes = await fetch(`${BASE}/api/runs/${encodeURIComponent(first.run_id)}`);
    assert.equal(detRes.status, 200);
    const det = await detRes.json();
    assert.ok(det.mode === 'shadow' || det.mode === 'fixture' || det.mode === 'on', '详情 mode 标签保留');
    t.diagnostic(`detail run_id=${first.run_id} mode=${det.mode} repo=${first.repo}#${first.pr_number}`);
  }

  // 5) 404 语义
  const missing = await fetch(`${BASE}/api/runs/NO-SUCH-RUN`);
  assert.equal(missing.status, 404);

  // 6) 只读边界：写方法被拒（405）
  const write = await fetch(`${BASE}/api/runs`, { method: 'POST' });
  assert.equal(write.status, 405, 'console_v3 GET-only：POST 必须 405');
});
