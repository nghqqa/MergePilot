// console/backend/test/invocation-recorder.test.mjs — C1 recorder 纯函数/参数校验单测（无 PG，CI glob 内）。
// 覆盖：敏感数据守卫（超长/prompt 正文形状/DSN/私钥/Bearer/sk- key/AWS AKIA → 一律拒绝且
// 不回显值）；digest/枚举/幂等键参数校验先于任何 DB 访问；白名单投影（无幂等键/无正文字段）。
import test from 'node:test';
import assert from 'node:assert/strict';
import * as rec from '../lib/multiuser/invocation-recorder.mjs';

const {
  guardInvocationMetadata, sha256Hex, projectSkillEvent, projectRagEvent,
  recordSkillInvocationStart, recordSkillInvocationFinish, recordRagRetrieval,
  recordInvocationFailure, INVOCATION_STATUSES, INVOCATION_KINDS, AGENT_ROLES,
  TERMINAL_INVOCATION_STATUSES,
} = rec;

const BASE_ARGS = {
  tenantId: '00000000-0000-0000-0000-000000000001',
  repoId: '00000000-0000-0000-0000-000000000002',
  prId: '00000000-0000-0000-0000-000000000003',
  runId: '00000000-0000-0000-0000-000000000004',
  agentRole: 'reviewer', skillKey: 'skill_x', invocationKind: 'other',
  idempotencyKey: 'unit-key-1',
};

test('守卫：合法元数据放行', () => {
  assert.deepEqual(guardInvocationMetadata({ a: 'ok', b: 1, c: ['x', null] }), { ok: true });
  assert.deepEqual(guardInvocationMetadata({ nested: { deep: 'fine' } }), { ok: true });
});

test('守卫：超长字符串拒绝（prompt/代码正文形状在结构上无法通过）', () => {
  const r = guardInvocationMetadata({ inputDigest: 'x'.repeat(513) });
  assert.equal(r.ok, false);
  assert.equal(r.code, 'sensitive_param_rejected');
  assert.equal(r.field, 'inputDigest');
});

test('守卫：凭据/私钥/DSN/Bearer 形状拒绝，且错误信息零回显', () => {
  const cases = {
    dsn: ['postgres://user', ':secret@db.example:5432/fxv'].join(''),
    privateKey: ['--', '--', '--BEGIN', ' RSA ', 'PRIVATE', ' ', 'KEY', '--', '--', '--', String.fromCharCode(10), 'MIIabc'].join(''),
    openaiKey: 'sk-' + 'a'.repeat(20),
    ghPat: 'ghp_' + 'a'.repeat(30),
    awsAkia: 'AKIA' + 'B2C3D4E5F6G7H8' + 'IJ',
    bearer: 'Bearer ' + 'eyJhbGciOi.eyJzdWIi.sig9',
  };
  for (const [field, val] of Object.entries(cases)) {
    const r = guardInvocationMetadata({ [field]: val });
    assert.equal(r.ok, false, field);
    assert.equal(r.code, 'sensitive_param_rejected', field);
    assert.ok(!JSON.stringify(r).includes(val), '拒绝结果不得回显值: ' + field);
  }
});

test('sha256Hex 输出 64 位小写 hex', () => {
  assert.match(sha256Hex('x'), /^[0-9a-f]{64}$/);
});

test('枚举词汇封闭（与 v19 CHECK 域一致）', () => {
  for (const s of ['RUNNING', 'SUCCEEDED', 'FAILED', 'TIMEOUT', 'CANCELLED', 'INTERRUPTED']) {
    assert.ok(INVOCATION_STATUSES.includes(s));
  }
  for (const s of ['SUCCEEDED', 'FAILED', 'TIMEOUT', 'CANCELLED', 'INTERRUPTED']) {
    assert.ok(TERMINAL_INVOCATION_STATUSES.has(s));
  }
  assert.ok(TERMINAL_INVOCATION_STATUSES.has('INTERRUPTED'));
  assert.ok(INVOCATION_KINDS.includes('verifier_tool') && INVOCATION_KINDS.includes('agentteams_round')
    && INVOCATION_KINDS.includes('rag_query'));
  assert.ok(AGENT_ROLES.includes('leader') && AGENT_ROLES.includes('reviewer')
    && AGENT_ROLES.includes('fixer') && AGENT_ROLES.includes('verifier') && AGENT_ROLES.includes('system'));
});

test('参数校验先于任何 DB 访问（pool=null 亦可判定拒绝）', async () => {
  const r1 = await recordSkillInvocationStart(null, { ...BASE_ARGS, invocationKind: 'nope' });
  assert.equal(r1.ok, false); assert.equal(r1.code, 'invocation_kind_invalid');
  const r2 = await recordSkillInvocationStart(null, { ...BASE_ARGS, agentRole: 'intruder' });
  assert.equal(r2.code, 'agent_role_invalid');
  const r3 = await recordSkillInvocationStart(null, { ...BASE_ARGS, inputDigest: 'zzz' });
  assert.equal(r3.code, 'digest_invalid');
  const r4 = await recordSkillInvocationStart(null, { ...BASE_ARGS, idempotencyKey: '' });
  assert.equal(r4.code, 'idempotency_key_invalid');
  const r5 = await recordSkillInvocationStart(null, { ...BASE_ARGS, idempotencyKey: 'k'.repeat(201) });
  assert.equal(r5.code, 'idempotency_key_invalid');
  const r6 = await recordSkillInvocationStart(null, { ...BASE_ARGS, inputDigest: 'prompt: '.repeat(100) });
  assert.equal(r6.code, 'sensitive_param_rejected');
  const r7 = await recordSkillInvocationStart(null, { ...BASE_ARGS, skillKey: '' });
  assert.equal(r7.code, 'skill_key_invalid');
});

test('finish/failure/RAG 的前置校验（无 DB）', async () => {
  const f1 = await recordSkillInvocationFinish(null, { eventId: 'e', status: 'RUNNING' });
  assert.equal(f1.code, 'invalid_finish_status');
  const f2 = await recordSkillInvocationFinish(null, { eventId: 'e', status: 'MAGIC' });
  assert.equal(f2.code, 'invalid_finish_status');
  const f3 = await recordSkillInvocationFinish(null, { eventId: 'e', status: 'SUCCEEDED', outputDigest: 'nope' });
  assert.equal(f3.code, 'digest_invalid');
  const f4 = await recordSkillInvocationFinish(null, { status: 'SUCCEEDED' });
  assert.equal(f4.code, 'event_not_found');
  const g1 = await recordInvocationFailure(null, { eventId: 'e', status: 'SUCCEEDED', errorCode: 'x' });
  assert.equal(g1.code, 'invalid_failure_status');
  const g2 = await recordInvocationFailure(null, { eventId: 'e', status: 'TIMEOUT', errorCode: 'AT_TIMEOUT' });
  assert.equal(g2.ok, false); // 校验通过、无 DB 寻址失败——承诺：永不抛异常
  const q1 = await recordRagRetrieval(null, { tenantId: 't', repoId: 'r', queryDigest: 'bad',
    resultCount: 0, sourceDigestList: [], idempotencyKey: 'k' });
  assert.equal(q1.code, 'digest_invalid');
  const q2 = await recordRagRetrieval(null, { tenantId: 't', repoId: 'r', queryDigest: sha256Hex('q'),
    resultCount: 1, sourceDigestList: ['BAD'], idempotencyKey: 'k' });
  assert.equal(q2.code, 'digest_invalid');
  const q3 = await recordRagRetrieval(null, { tenantId: 't', repoId: 'r', queryDigest: sha256Hex('q'),
    resultCount: 0, sourceDigestList: [], idempotencyKey: 'k', status: 'PENDING' });
  assert.equal(q3.code, 'invalid_status');
});

test('白名单投影：skill 事件恰 12 键、RAG 事件恰 11 键，均无幂等键/无正文域', () => {
  const skill = projectSkillEvent({ event_id: 'e1', tenant_id: 't', idempotency_key: 'secret-key',
    agent_role: 'verifier', skill_key: 'k', skill_version: '1.0.0', invocation_kind: 'verifier_tool',
    status: 'SUCCEEDED', started_at: 't0', completed_at: 't1', latency_ms: '5',
    input_digest: 'a'.repeat(32), output_digest: 'b'.repeat(32), error_code: null });
  assert.equal(Object.keys(skill).length, 12);
  assert.deepEqual(Object.keys(skill).sort(), ['agent_role', 'completed_at', 'error_code', 'event_id',
    'input_digest', 'invocation_kind', 'latency_ms', 'output_digest', 'skill_key', 'skill_version',
    'started_at', 'status'].sort());
  assert.ok(!('idempotency_key' in skill));
  assert.equal(skill.latency_ms, 5);
  assert.deepEqual(projectSkillEvent(null), null);
  const rag = projectRagEvent({ event_id: 'e2', agent_role: 'system', skill_key: 'rag.retrieve',
    status: 'SUCCEEDED', started_at: 't0', completed_at: 't1', latency_ms: 0, error_code: null,
    query_digest: 'c'.repeat(64), result_count: '3', source_digest_list: ['d'.repeat(64)],
    idempotency_key: 'secret', tenant_id: 't' });
  assert.equal(Object.keys(rag).length, 11);
  assert.ok(!('idempotency_key' in rag) && !('tenant_id' in rag));
  assert.equal(rag.result_count, 3);
  assert.deepEqual(projectRagEvent(null), null);
});

test('recorder 零导出泄漏检查（模块面不含 prompt/query 域词汇）', () => {
  const src = Object.keys(rec).join(',');
  assert.ok(!/prompt|response_body|query_text|code_body|secret/i.test(src));
});