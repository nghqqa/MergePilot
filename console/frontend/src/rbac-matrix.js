// rbac-matrix.js — 角色权限矩阵静态数据（展示面/文档性质）。
// 唯一真源是后端 console/backend/lib/multiuser/authz.mjs（roleActions 纯函数）；
// 本文件由 test/rbac-matrix.test.mjs 契约测试与后端逐项锁定——改权限面时先改
// authz.mjs，再同步这里，测试红即为漂移提醒。label 仅影响展示，不参与判定；
// binding: true = 还需仓库处于 active Binding（对应后端 MU_BINDING_REQUIRED）。
export const RBAC_ROLES = ['contributor', 'reviewer', 'maintainer', 'platform_admin', 'auditor'];

export const RBAC_ACTIONS = [
  { id: 'read_repository', label: '查看仓库' },
  { id: 'read_pull_request', label: '查看 PR' },
  { id: 'read_code_content', label: '读取代码内容' },
  { id: 'rag_query', label: 'RAG 检索' },
  { id: 'request_review', label: '发起只读审查' },
  { id: 'decide_review', label: '审批决定（通过/驳回）' },
  { id: 'request_repair', label: '发起受控修复', binding: true },
  { id: 'apply_fix', label: '应用修复补丁', binding: true },
  { id: 'merge_pr', label: '合并 PR', binding: true },
  { id: 'manage_repository_binding', label: '管理仓库绑定' },
  { id: 'manage_membership', label: '成员与角色管理' },
  { id: 'read_audit', label: '读取审计' },
  { id: 'manage_instance', label: '实例管理（含租户创建）' },
];

export const RBAC_GRANTS = {
  contributor: ['read_repository', 'read_pull_request', 'read_code_content', 'rag_query'],
  reviewer: ['read_repository', 'read_pull_request', 'read_code_content', 'rag_query', 'request_review'],
  maintainer: ['read_repository', 'read_pull_request', 'read_code_content', 'rag_query',
    'request_review', 'decide_review', 'request_repair', 'apply_fix', 'merge_pr', 'manage_repository_binding'],
  platform_admin: ['read_repository', 'read_pull_request', 'rag_query', 'request_review',
    'request_repair', 'manage_repository_binding', 'manage_membership', 'read_audit', 'manage_instance'],
  auditor: ['read_audit'],
};

export const RBAC_ROLE_NOTES = {
  maintainer: '组织内审查与修复的执行角色；审批裁定（decide_review）仅此角色。',
  platform_admin: '平台运维角色：成员/实例/绑定管理。刻意不含 decide_review 与 read_code_content（平台与裁定分离）。',
  auditor: '只读审计元数据；无代码内容、RAG、仓库/PR 读取。',
  contributor: '组织基础读面：仓库/PR/代码摘录/RAG 检索。',
  reviewer: 'contributor 基础上可发起只读审查。',
};
