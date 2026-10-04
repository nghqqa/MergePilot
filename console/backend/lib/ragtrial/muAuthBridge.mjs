// Dogfooding P1 修复：RAG Trial 兼容 MU 持久化会话
// 在 server.mjs 传给 ragTrialApi 的 requireSession 中增加 MU 会话回退。
// 安全合同：
//  * legacy mp_session 优先（非 multiuser 模式零回归）；
//  * MU mu_session 逐请求 DB 解析：session 有效 + user active + membership active
//    + 角色 rag_query 动作 + tenant 仓库列表（服务端取，不信任客户端）；
//  * 不写入 legacy session Map（read-only 兼容层）；
//  * Auditor/PlatformAdmin 默认无 rag_query → RAG 401（角色矩阵保持）；
//  * machine/A 链端点不经此路径（HMAC/scope 语义不变）。
import { muTokenFromCookieHeader } from '../multiuser/session.mjs';
import { roleActions } from '../multiuser/authz.mjs';
import crypto from 'node:crypto';

let muPool = null;
// pg 解析：生产镜像 /app/node_modules 常规 import 即得；测试裸仓（依赖仅装在
// console/backend/test/support）走 createRequire fallback——解析失败如实返回 null
//（调用方 401，不伪装可用）。
async function loadPg() {
  try { return await import('pg'); } catch { /* fallback below */ }
  try {
    const { createRequire } = await import('node:module');
    return createRequire(new URL('../../test/support/noop.js', import.meta.url))('pg');
  } catch { return null; }
}
async function getMuPool(env) {
  if (!muPool) {
    const pg = await loadPg();
    if (!pg) return null;
    muPool = new pg.Pool({ connectionString: env.CONSOLE_PG_DSN, max: 2 });
    muPool.on('error', () => {});
  }
  return muPool;
}

const sha256Of = (v) => crypto.createHash('sha256').update(String(v)).digest('hex');

/**
 * 解析 MU 会话并构造 RAG 兼容 auth 对象（{ user, repos }）。
 * 逐请求检查 session/user/membership/role/repos（全部服务端 DB 查询）。
 * 返回 null = 无有效 MU 会话（调用方回退 401）。
 */
export async function resolveMuAuthForRag(req, env = process.env) {
  if (env.MU_MODE !== 'multiuser' || !env.CONSOLE_PG_DSN) return null;
  const token = muTokenFromCookieHeader(req.headers.cookie);
  if (!token) return null;
  const pool = await getMuPool(env);
  if (!pool) return null;
  try {
    const sess = await pool.query(
      `SELECT s.user_id, s.tenant_id, s.login, s.role_snapshot, s.csrf_hash, u.state AS user_state
         FROM mu.session s JOIN mu.app_user u ON u.user_id = s.user_id
        WHERE s.token_hash = $1 AND s.revoked_at IS NULL
          AND s.expires_at > now() AND u.state = 'active'`,
      [sha256Of(token)]);
    if (!sess.rows.length) return null;
    const s = sess.rows[0];

    // membership 逐请求 live 检查（session 角色快照不可信）
    const memb = await pool.query(
      `SELECT role, state FROM mu.membership WHERE tenant_id = $1 AND user_id = $2`,
      [s.tenant_id, s.user_id]);
    const m = memb.rows[0];
    if (!m || m.state !== 'active') return null;

    // 角色是否有 rag_query 动作（Auditor 无 → null → 401）
    const actions = roleActions(m.role) ?? [];
    if (!actions.includes('rag_query')) return null;

    // tenant 仓库列表（服务端，不信任客户端）
    const repos = await pool.query(
      `SELECT owner, name FROM mu.repository WHERE tenant_id = $1 AND state = 'active'`,
      [s.tenant_id]);
    const repoNames = repos.rows.map((r) => `${r.owner}/${r.name}`);

    // 构造 RAG 兼容 auth（只读，不写 legacy Map）
    // rc.10 SEC-3：csrf_hash 随会话解析带回（仅供服务端双提交比对，绝不下发客户端）
    return { user: s.login, repos: repoNames,
      _mu: { userId: s.user_id, tenantId: s.tenant_id, role: m.role, csrfHash: s.csrf_hash } };
  } catch { return null; }
}
