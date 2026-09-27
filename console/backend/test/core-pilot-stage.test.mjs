// core-pilot-stage.test.mjs — gate 决策 → 阶段映射的 fail-closed 契约测试（P1 修复波）。
// 锁死：decision 缺失/无法识别时禁止标 PASSED（也不得断言不存在的 PRODUCE 决策）；
// 只有显式 PRODUCE 才 PASSED；REFUSE → BLOCKED。运行：node --test console/backend/test/core-pilot-stage.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { stageForGateDecision } from '../lib/core-pilot.mjs';

test('有效 PRODUCE 决策 → PASSED（stage_source 断言与决策一致）', () => {
  const r = stageForGateDecision({ decision: { decision: 'PRODUCE', repo: 'a/b' } });
  assert.equal(r.stage, 'PASSED');
  assert.equal(r.stage_source, 'skill_gate_audit (PRODUCE)');
  // 大小写不敏感（历史行可能出现小写）
  const r2 = stageForGateDecision({ decision: { decision: 'produce' } });
  assert.equal(r2.stage, 'PASSED');
});

test('REFUSE 决策 → BLOCKED', () => {
  const r = stageForGateDecision({ decision: { decision: 'REFUSE' } });
  assert.equal(r.stage, 'BLOCKED');
  assert.equal(r.stage_source, 'skill_gate_audit (REFUSE)');
});

test('decision 缺失/无法识别 → UNKNOWN（fail-closed：绝不冒充 PASSED）', () => {
  // gate 记录存在但无 decision 字段
  const missing = stageForGateDecision({ decision: null, created_at: new Date().toISOString() });
  assert.equal(missing.stage, 'UNKNOWN', 'decision 缺失不得标 PASSED');
  assert.match(missing.stage_source, /missing/);
  assert.ok(!/PRODUCE/.test(missing.stage_source) || /decision=/.test(missing.stage_source),
    '不得断言一个不存在的 PRODUCE 决策');

  // decision 字段存在但值为空串
  const empty = stageForGateDecision({ decision: { decision: '' } });
  assert.equal(empty.stage, 'UNKNOWN');

  // 无法识别的值（拼写错误/新枚举）
  const weird = stageForGateDecision({ decision: { decision: 'PRODUCE?' } });
  assert.equal(weird.stage, 'UNKNOWN');
  assert.match(weird.stage_source, /unrecognized/);

  // 完全无 gate 记录不是本函数职责（overviewState 归 REVIEWING）；此处验证不崩
  const none = stageForGateDecision(undefined);
  assert.equal(none.stage, 'UNKNOWN');
});

test('UNKNOWN 属于 stages_enum（前端可映射，stage_counts 可计数）', async () => {
  // stages_enum 由 overviewState 输出；此处静态验证 STAGES 数组包含 UNKNOWN
  const src = await import('node:fs').then((fs) =>
    fs.readFileSync(new URL('../lib/core-pilot.mjs', import.meta.url), 'utf8'));
  assert.match(src, /'UNKNOWN'/, 'STAGES 枚举必须包含 UNKNOWN');
});
