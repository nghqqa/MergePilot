// status-map.test.mjs — 状态语义映射测试（提示词九"数据语义"验证矩阵的固化）
// 运行：node --test console/backend/test/status-map.test.mjs

import test from 'node:test';
import assert from 'node:assert/strict';
import {
  executionMap, verdictMap, gateMap, publishMap, EXECUTION, GATE,
  SEVERITY, SEVERITY_ORDER, FXV, CCHAIN, CCHAIN_OVERALL, RAG, TICKET, TICKET_ACTION,
  fxvMap, fxvArtifactMap, cchainMap, cchainOverallMap, ragMap, ticketMap, ticketActionMap,
  toneToColor, VERDICT_SEVERITY_TONE,
} from '../../frontend/src/status-map.js';
import { STATES as FXV_STATES_REF } from '../lib/fxv/orchestrator.mjs';

test('HIGH + 执行完成：执行态是中性事实，不显示成安全通过', () => {
  const exec = executionMap({ status: 'COMPLETED', source: 'project_meta' });
  assert.equal(exec.tone, 'neutral');
  assert.match(exec.note, /不代表无安全问题/);
  assert.doesNotMatch(exec.label, /通过|成功/);

  const processed = executionMap({ status: 'PROCESSED', source: 'delivery_ledger' });
  assert.equal(processed.tone, 'neutral');
  assert.match(processed.note, /不代表审查通过或检查通过/);
});

test('APPROVED + HIGH：批准 ≠ 已修复 ≠ 已合并', () => {
  const gate = gateMap('APPROVED');
  assert.equal(gate.tone, 'info');
  assert.match(gate.note, /不代表问题已修复/);
  assert.match(gate.note, /更不代表已合并/);

  const verdict = verdictMap({
    verdict: 'FINDING_CONFIRMED', severity: 'HIGH', cwe: 'CWE-22', source: 'project/result.md',
  });
  assert.equal(verdict.tone, 'bad');
  assert.match(verdict.label, /HIGH/);
  assert.match(verdict.note, /发现确认不等于已修复/);
});

test('GitHub 成功回写一个 failure check：回写成功与检查未通过是两个事实', () => {
  const p = publishMap({
    status: 'published', conclusion: 'failure', check_run_id: 123,
  });
  assert.equal(p.tone, 'warn', '不是 bad（回写本身成功了）');
  assert.match(p.label, /已回写/);
  assert.match(p.label, /检查未通过/);
  assert.match(p.note, /回写通道成功/);
  assert.match(p.note, /不是回写失败/);
});

test('check success：绿色正向成立，但不证明补丁已验证', () => {
  const p = publishMap({ status: 'published', conclusion: 'success', check_run_id: 5 });
  assert.equal(p.tone, 'ok');
  assert.match(p.note, /不证明补丁已验证/);
});

test('无发布记录：不能断言"未回写"', () => {
  const p = publishMap({ status: 'not_recorded' });
  assert.equal(p.tone, 'neutral');
  assert.match(p.label, /未找到发布记录/);
  assert.match(p.note, /不推导为"未回写"/);
  const p2 = publishMap({ status: 'processed_no_checkrun_record' });
  assert.match(p2.note, /不代表未回写/);
});

test('未记录结论 ≠ 无问题', () => {
  const v = verdictMap({});
  assert.equal(v.tone, 'neutral');
  assert.match(v.note, /不等于"无问题"/);
});

test('LOW/MEDIUM 确认发现用 warn，不用红色', () => {
  assert.equal(verdictMap({ verdict: 'FINDING_CONFIRMED', severity: 'LOW' }).tone, 'warn');
  assert.equal(verdictMap({ verdict: 'FINDING_CONFIRMED', severity: 'MEDIUM' }).tone, 'warn');
  assert.equal(verdictMap({ verdict: 'FINDING_CONFIRMED', severity: 'CRITICAL' }).tone, 'bad');
});

test('BLOCKED 是受控停止（warn），ERROR 是执行错误（bad）', () => {
  assert.equal(executionMap({ status: 'BLOCKED' }).tone, 'warn');
  assert.equal(executionMap({ status: 'ERROR' }).tone, 'bad');
});

test('NOT_CONFIRMED 是明确正向（ok），但 note 说明边界', () => {
  const v = verdictMap({ verdict: 'NOT_CONFIRMED', severity: 'LOW' });
  assert.equal(v.tone, 'ok');
  assert.match(v.note, /不等于绝对无风险/);
});

test('人工确认状态：REJECTED 是受控安全停止（warn）', () => {
  assert.equal(gateMap('REJECTED').tone, 'warn');
  assert.equal(gateMap('NOT_REQUIRED').tone, 'neutral');
  assert.equal(gateMap(null).tone, 'neutral');
  assert.match(gateMap('RECORDED', 'human-gate.md').note, /human-gate\.md/);
});

// ── P1 修复波新增：状态族独立键空间契约（FXV/C 链/RAG/票据/严重度/工件）──

test('SEVERITY 含 CRITICAL（最严重等级不得被吞为"—"），排序 CRITICAL 最高', () => {
  assert.equal(SEVERITY.CRITICAL.tone, 'bad');
  assert.equal(SEVERITY.CRITICAL.label, '危急');
  assert.equal(SEVERITY.HIGH.tone, 'bad');
  assert.deepEqual(SEVERITY_ORDER, ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW']);
  // verdict 侧严重度色与 SEVERITY 单源一致
  for (const k of SEVERITY_ORDER) assert.equal(VERDICT_SEVERITY_TONE[k], SEVERITY[k].tone);
});

test('FXV 23 态全覆盖：与 orchestrator STATES 逐一对齐（快照锁定）', () => {
  const states = Object.values(FXV_STATES_REF);
  assert.equal(states.length, 23, 'FXV 状态机固定 23 态');
  for (const s of states) {
    const m = fxvMap(s);
    assert.notEqual(m.label, s, `${s} 必须有中文标签，不得裸枚举`);
    assert.ok(m.note.includes(s), `${s} 的 note 保留原始枚举`);
  }
  // 关键语义红线
  assert.equal(fxvMap('ERROR_FATAL').tone, 'bad', '致命错误必须 error 级着色');
  assert.equal(fxvMap('VERIFIED').tone, 'ok', '成功终态必须正向着色');
  assert.equal(fxvMap('TEST_FAILED').tone, 'bad');
  assert.equal(fxvMap('TIMEOUT').tone, 'bad');
  assert.equal(fxvMap('DRY_RUN_VERIFIED').tone, 'ok');
  assert.match(fxvMap('DRY_RUN_VERIFIED').note, /不等于生产验证/);
  assert.equal(fxvMap('MANUAL_WAIT').tone, 'warn');
  // FXV APPROVED 与票据/门禁 APPROVED 分属不同键空间，互不串色
  assert.equal(fxvMap('APPROVED').label, '审批通过');
  assert.equal(ticketMap('APPROVED').label, '已批准');
  assert.equal(gateMap('APPROVED').label, '已批准修复');
});

test('FXV 工件状态：COMPLETE 最强（ok），OK 逐条归档（ok），FAILED bad', () => {
  assert.equal(fxvArtifactMap('COMPLETE').tone, 'ok');
  assert.equal(fxvArtifactMap('OK').tone, 'ok');
  assert.equal(fxvArtifactMap('FAILED').tone, 'bad');
  assert.equal(fxvArtifactMap('NONE').label, '无工件');
  assert.equal(fxvArtifactMap(undefined).label, '无工件');
});

test('C 链状态全覆盖：与 lib/cchain 状态词表对齐；BLOCKED overall 为 bad', () => {
  for (const s of ['READY', 'ATTESTED', 'NOT_CONFIGURED', 'MISSING', 'CORRUPT', 'UNREACHABLE', 'INVALID']) {
    const m = cchainMap(s);
    assert.notEqual(m.label, s, `${s} 必须有中文标签`);
  }
  assert.equal(cchainOverallMap('READY').tone, 'ok');
  assert.equal(cchainOverallMap('BLOCKED').tone, 'bad');
  assert.equal(cchainOverallMap('BLOCKED').label, '已阻断');
});

test('RAG 状态全覆盖：六状态 + API 层附加态；hit=ok、degraded=bad', async () => {
  const { QUERY_STATES } = await import('../lib/ragtrial/store.mjs');
  for (const s of QUERY_STATES) {
    const m = ragMap(s);
    assert.notEqual(m.label, s, `${s} 必须有中文标签`);
  }
  assert.equal(ragMap('hit').tone, 'ok');
  assert.equal(ragMap('empty').tone, 'neutral');
  assert.equal(ragMap('provider_unavailable').tone, 'bad');
  assert.equal(ragMap('error').tone, 'bad');
  assert.equal(ragMap('degraded').tone, 'bad');
  assert.equal(ragMap('ready').tone, 'ok', "'ready' 必有着色（历史缺陷：回落红色）");
  assert.equal(ragMap('backend_not_wired').tone, 'neutral');
  assert.equal(ragMap('a_chain_disabled').tone, 'neutral');
});

test('票据状态/动作键空间：中文标签 + 未知动作兜底', () => {
  assert.equal(ticketMap('PENDING').label, '待审批');
  assert.equal(ticketMap('APPROVED').label, '已批准');
  assert.equal(ticketMap('REJECTED').label, '已拒绝');
  assert.equal(ticketMap('EXPIRED').label, '已过期');
  assert.equal(ticketActionMap('APPROVE_REMEDIATION').label, '批准修复授权');
  const unknownAction = ticketActionMap('SOMETHING_NEW');
  assert.equal(unknownAction.label, 'SOMETHING_NEW', '未知动作保留原始机器值');
  assert.match(unknownAction.note, /未知状态/);
});

test('未知枚举兜底统一：label=原始值 + note 标注"未知状态"', () => {
  for (const m of [fxvMap('NOT_A_STATE'), cchainMap('WEIRD'), ragMap('odd_state'), ticketMap('NOPE'),
    executionMap({ status: 'STRANGE' }), verdictMap({ verdict: 'mystery' }), gateMap('WHAT')]) {
    assert.match(m.note, /未知状态（原始枚举：/, `note 必须标注未知状态：${m.note}`);
  }
  assert.equal(fxvMap('NOT_A_STATE').label, 'NOT_A_STATE');
  assert.equal(cchainMap(undefined).label, 'UNKNOWN');
});

test('toneToColor：五档语义色 → antd 预设色完整', () => {
  assert.deepEqual(
    ['ok', 'info', 'warn', 'bad', 'neutral'].map((t) => toneToColor(t)),
    ['success', 'processing', 'warning', 'error', 'default']);
  assert.equal(toneToColor('nonsense'), 'default');
});
