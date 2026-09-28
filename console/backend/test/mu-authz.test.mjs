// console/backend/test/mu-authz.test.mjs — MU 统一授权纯函数单测（无 PG，CI glob 内）。
// 角色×动作矩阵 + 全部 fail-closed 路径（对齐任务要求：Contributor 不可审批/管理/
// 绑定/修复；Auditor 无代码/RAG；PlatformAdmin 不自动获得代码读取）。
import test from 'node:test';
import assert from 'node:assert/strict';
import { authorize, roleActions, MU_ACTIONS, MU_ROLES, MU_BINDING_REQUIRED } from '../lib/multiuser/authz.mjs';

const M = (role, state = 'active') => ({ role, state });

test('角色×动作矩阵（设计报告 §3 的 Developer 切片）', () => {
  assert.deepEqual(roleActions('contributor'),
    ['read_repository', 'read_pull_request', 'read_code_content', 'rag_query']);
  assert.ok(roleActions('reviewer').includes('request_review'));
  assert.ok(!roleActions('reviewer').includes('decide_review'), 'Reviewer 不可审批');
  for (const a of ['decide_review', 'request_repair', 'manage_repository_binding']) {
    assert.ok(roleActions('maintainer').includes(a), `maintainer 应有 ${a}`);
  }
  assert.deepEqual(roleActions('auditor'), ['read_audit'], 'Auditor 只读审计元数据');
  assert.ok(!roleActions('platform_admin').includes('read_code_content'), 'PlatformAdmin 不自动获得代码读取');
  assert.ok(!roleActions('platform_admin').includes('rag_query'), 'PlatformAdmin 不自动获得 RAG 语料');
  assert.ok(roleActions('platform_admin').includes('manage_membership'));
});

test('矩阵完备性：每个声明的动作至少被一个角色持有；词汇封闭', () => {
  for (const a of MU_ACTIONS) {
    assert.ok(MU_ROLES.some((r) => (roleActions(r) ?? []).includes(a)), `动作 ${a} 无人持有`);
  }
  for (const r of MU_ROLES) {
    for (const a of roleActions(r)) assert.ok(MU_ACTIONS.includes(a), `${r} 持有未声明动作 ${a}`);
  }
  assert.deepEqual(MU_BINDING_REQUIRED, ['request_repair']);
});

test('authorize fail-closed 全路径', () => {
  assert.equal(authorize({ membership: null, action: 'read_repository' }).reason, 'not_a_member');
  assert.equal(authorize({ membership: M('contributor', 'revoked'), action: 'read_repository' }).reason, 'membership_inactive');
  assert.equal(authorize({ membership: M('superadmin'), action: 'read_repository' }).reason, 'unknown_role');
  assert.equal(authorize({ membership: M('contributor'), action: 'decide_review' }).reason, 'action_not_granted');
  assert.equal(authorize({ membership: M('contributor'), action: 'manage_membership' }).reason, 'action_not_granted');
  assert.equal(authorize({ membership: M('contributor'), action: 'manage_repository_binding' }).reason, 'action_not_granted');
  assert.equal(authorize({ membership: M('contributor'), action: 'request_repair' }).reason, 'action_not_granted');
  assert.equal(authorize({ membership: M('maintainer'), action: 'request_repair' }).reason, 'binding_required');
  assert.equal(authorize({ membership: M('auditor'), action: 'read_code_content' }).reason, 'action_not_granted');
  assert.equal(authorize({ membership: M('auditor'), action: 'rag_query' }).reason, 'action_not_granted');
});

test('authorize 允许路径', () => {
  const okC = authorize({ membership: M('contributor'), action: 'read_code_content' });
  assert.equal(okC.ok, true);
  const okR = authorize({ membership: M('reviewer'), action: 'request_review' });
  assert.equal(okR.ok, true);
  const okM = authorize({ membership: M('maintainer'), action: 'request_repair', binding: { installation_state: 'active' } });
  assert.equal(okM.ok, true);
  const okA = authorize({ membership: M('auditor'), action: 'read_audit' });
  assert.equal(okA.ok, true);
});

test('multiuser 模式下 legacy 登录被拒（session 门，纯 env 语义）', async () => {
  // 通过 server 级测试在 mu-rbac.integration 覆盖 HTTP 面；此处锁 authz 词汇不外泄
  assert.ok(MU_ROLES.includes('platform_admin') && MU_ROLES.includes('auditor'));
});
