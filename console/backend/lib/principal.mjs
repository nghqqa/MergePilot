// console/backend/lib/principal.mjs — 统一认证解析层。
//
// 核心契约：
//  * MU_MODE=on  → mu_session 是唯一凭证（绝不回退 mp_session）
//  * MU_MODE=off → mp_session 保留 legacy 行为（零变化）
//  * 业务处理器不得自行读 Cookie——一律经 resolvePrincipal
const MU_COOKIE = 'mu_session';

export const ROLE_PERMISSIONS = Object.freeze({
  contributor: ['read:pr', 'read:run', 'read:evidence', 'read:ticket'],
  reviewer: ['read:pr', 'read:run', 'read:evidence', 'read:ticket', 'review:pr', 'read:audit'],
  maintainer: ['read:pr', 'read:run', 'read:evidence', 'read:ticket', 'review:pr', 'read:audit',
    'manage:repository_binding', 'trigger:review', 'read:overview', 'read:pending', 'read:pulls',
    'read:cchain', 'read:rag', 'read:runs', 'read:approvals', 'read:tickets'],
  platform_admin: ['*'],
  auditor: ['read:pr', 'read:run', 'read:evidence', 'read:ticket', 'read:audit', 'read:overview',
    'read:pending', 'read:pulls', 'read:runs', 'read:approvals', 'read:tickets', 'read:cchain'],
});

export function permissionsForRole(role) {
  return ROLE_PERMISSIONS[String(role ?? '').toLowerCase()] ?? [];
}

export function hasPermission(principal, perm) {
  if (!principal?.authenticated) return false;
  const perms = principal.permissions ?? [];
  return perms.includes('*') || perms.includes(perm);
}

function muTokenFromCookie(header) {
  if (typeof header !== 'string') return '';
  for (const part of header.split(';')) {
    const [k, ...v] = part.trim().split('=');
    if (k === MU_COOKIE) return v.join('=');
  }
  return '';
}

import { createHash } from 'node:crypto';
const sha256Of = (v) => createHash('sha256').update(String(v)).digest('hex');

/**
 * 统一认证解析（仅 MU 模式——legacy 由调用方自行处理）
 * @param {object} req - Node HTTP request（读 headers.cookie）
 * @param {object} opts - { muStore } 必须
 */
export async function resolvePrincipal(req, opts = {}) {
  const notAuth = { authenticated: false, userId: null, username: null, orgId: null,
    tenantId: null, roles: [], permissions: [], authMode: 'multiuser', sessionId: null };
  const muToken = muTokenFromCookie(req.headers?.cookie);
  if (!muToken || !opts.muStore) return notAuth;
  const session = await opts.muStore.findSessionByToken(muToken).catch(() => null);
  if (!session) return notAuth;
  const userId = String(session.user_id ?? '');
  const membership = await opts.muStore.getMembership(
    String(session.tenant_id ?? ''), userId).catch(() => null);
  const role = (membership && membership.state === 'active') ? membership.role
    : (session.role ?? 'contributor');
  const user = await opts.muStore.getUser(userId).catch(() => null);
  return { authenticated: true, userId,
    username: user?.login ?? session.login ?? 'mu-user',
    orgId: String(session.tenant_id ?? ''), tenantId: String(session.tenant_id ?? ''),
    roles: [role],
    permissions: permissionsForRole(role),
    authMode: 'multiuser', sessionId: muToken, muSession: session };
}
