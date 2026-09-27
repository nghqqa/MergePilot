// console/backend/lib/multiuser/authz.mjs — MU 统一授权（Developer Edition，纯函数无 IO）。
//
// 合同（对齐设计报告 §2/§3 与 Phase 0 复核不变式）：
//  * 默认拒绝：无 membership / state 非 active / 未知角色 / 角色无此动作 / 需 Binding 而无
//    active Binding —— 一律 fail-closed（reason 机器码，供 HTTP 层 403 与审计）；
//  * tenant_id / repo_id 永远由服务端解析（session + tenant 收窄查询），不信任请求体；
//  * PlatformAdmin 不自动获得代码读取（GitHub 内容/RAG 语料）；
//  * Auditor 只读审计元数据（无 read_code_content / rag_query / 仓库/PR 读）。

export const MU_ROLES = ['contributor', 'reviewer', 'maintainer', 'platform_admin', 'auditor'];

export const MU_ACTIONS = [
  'read_repository',        // 仓库元数据/绑定状态
  'read_pull_request',      // PR 快照 + 审查记录
  'read_code_content',      // 代码内容（变更摘录等）
  'rag_query',              // RAG 检索（语料内容面）
  'request_review',         // 触发只读审查
  'decide_review',          // 人工审批（approve/reject）
  'request_repair',         // 发起受控修复（需 active Binding）
  'manage_repository_binding', // 绑定/解绑仓库
  'manage_membership',      // 成员与角色管理
  'read_audit',             // 审计元数据读取
  'manage_instance',        // 实例级配置（含 tenant 创建）
];

const CONTRIBUTOR = ['read_repository', 'read_pull_request', 'read_code_content', 'rag_query'];
const ROLE_ACTIONS = {
  contributor: [...CONTRIBUTOR],
  reviewer: [...CONTRIBUTOR, 'request_review'],
  maintainer: [...CONTRIBUTOR, 'request_review', 'decide_review', 'request_repair', 'manage_repository_binding'],
  auditor: ['read_audit'],
  platform_admin: ['read_repository', 'manage_membership', 'read_audit', 'manage_instance'],
};

// 需要 active Binding 才允许的动作（provider 侧连接存在性由调用方传入 binding 判定）
export const MU_BINDING_REQUIRED = ['request_repair'];

export function roleActions(role) {
  return ROLE_ACTIONS[role] ?? null;
}

export function authorize({ membership, action, binding = null }) {
  if (!membership) return { ok: false, reason: 'not_a_member' };
  if (membership.state !== 'active') return { ok: false, reason: 'membership_inactive' };
  const actions = roleActions(membership.role);
  if (!actions) return { ok: false, reason: 'unknown_role' };
  if (!actions.includes(action)) return { ok: false, reason: 'action_not_granted' };
  if (MU_BINDING_REQUIRED.includes(action) && !binding) return { ok: false, reason: 'binding_required' };
  return { ok: true, role: membership.role, action };
}
