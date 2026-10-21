// rbac-matrix.test.mjs — 前端权限矩阵静态数据 ↔ 后端 authz.mjs 契约锁定。
// src/rbac-matrix.js 是展示面（RoleMatrixPanel 数据源）；唯一真源是后端
// console/backend/lib/multiuser/authz.mjs 的 roleActions 纯函数。本测试逐项
// deepEqual 锁定——改权限面先改 authz.mjs，这里红即为前端漂移提醒。
// 运行：cd console/frontend && node --test test/rbac-matrix.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const authzUrl = pathToFileURL(path.resolve(__dirname, '../../backend/lib/multiuser/authz.mjs')).href;
const { MU_ROLES, MU_ACTIONS, MU_BINDING_REQUIRED, roleActions } = await import(authzUrl);
const { RBAC_ROLES, RBAC_ACTIONS, RBAC_GRANTS } = await import('../src/rbac-matrix.js');

test('rbac 矩阵契约：角色词汇与后端 MU_ROLES 逐项一致（含顺序）', () => {
  assert.deepEqual(RBAC_ROLES, MU_ROLES);
});

test('rbac 矩阵契约：动作清单与后端 MU_ACTIONS 逐项一致（含顺序）', () => {
  assert.deepEqual(RBAC_ACTIONS.map((a) => a.id), MU_ACTIONS);
});

test('rbac 矩阵契约：五角色授权面与后端 roleActions() 逐项 deepEqual', () => {
  for (const role of MU_ROLES) {
    assert.deepEqual(RBAC_GRANTS[role], roleActions(role), `role=${role}`);
  }
});

test('rbac 矩阵契约：需绑定动作标记与后端 MU_BINDING_REQUIRED 一致', () => {
  const marked = RBAC_ACTIONS.filter((a) => a.binding).map((a) => a.id);
  assert.deepEqual([...marked].sort(), [...MU_BINDING_REQUIRED].sort());
});
