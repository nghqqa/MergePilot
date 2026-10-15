// mt-projection.test.mjs — Matrix 传输角色投影回归锁（2026-10-15 真实闭环验收暴露）。
// 核心：fixer 投影必须透传 patch——丢弃会导致 orchestrator 永远拿不到 diff，
// 全部 agentteams 修复轮 hint-only → verifier BLOCKED（PR#12 实证）。
import assert from 'node:assert';
import test from 'node:test';

const mt = await import('../lib/multiuser/agents/matrix-transport.mjs');
const at = await import('../lib/multiuser/agents/agentteams-executor.mjs');

test('fixer 投影透传 patch（回归：PR#12 闭环验收暴露的系统性丢弃）', () => {
  const json = { suggestion: 'add null guard', patch_hint: 'guard cart.items',
    patch: '--- a/discount.js\n+++ b/discount.js\n@@ -1,3 +1,4 @@\n x' };
  const projected = mt.projectToRoleSchema('fixer', json);
  assert.equal(projected.patch, json.patch, 'patch 字段不得被投影丢弃');
  assert.equal(at.validateAgentTeamsOutput('fixer', projected).ok, true);
});

test('fixer 投影：hint-only 仍合法（schema 不收紧），但不含 patch', () => {
  const projected = mt.projectToRoleSchema('fixer', { suggestion: 's', patch_hint: 'h' });
  assert.equal(projected.patch, undefined);
  assert.equal(at.validateAgentTeamsOutput('fixer', projected).ok, true);
});

test('fixer 投影：非法 patch（非统一 diff）投影后仍被 schema 拒绝（不放宽边界）', () => {
  const projected = mt.projectToRoleSchema('fixer', { suggestion: 's', patch: 'not a diff' });
  assert.equal(at.validateAgentTeamsOutput('fixer', projected).ok, false);
});

test('verifier/leader 投影不受影响', () => {
  assert.deepEqual(mt.projectToRoleSchema('verifier', { verdict: 'PASS', note: 'n' }),
    { verdict: 'PASS', note: 'n' });
  assert.deepEqual(mt.projectToRoleSchema('leader', { recommendation: 'fix_required', confidence: 0.8 }),
    { recommendation: 'fix_required', confidence: 0.8 });
});
