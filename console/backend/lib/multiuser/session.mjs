// console/backend/lib/multiuser/session.mjs — MU 数据库持久化安全会话（Wave 2A）。
//
// 安全合同：
//  * cookie 名 mu_session（与 legacy mp_session 分离，互不覆盖）；
//    属性 HttpOnly + SameSite=Lax + Path=/，NODE_ENV=production 时强制 Secure；
//  * DB 只存 token/csrf 的 sha256 摘要（mu.session.token_hash/csrf_hash）——
//    明文只存在于 cookie 与内存比较瞬间；
//  * 会话过期/撤销/轮换（tenant 切换旋转 token）；logout 单会话撤销；
//    revokeAllSessionsForUser 全量撤销；
//  * CSRF：双提交（mp_csrf 非 HttpOnly cookie + X-CSRF-Token 头），服务端比对摘要
//    （timing-safe）；
//  * 成员撤权即时失效由 api 层逐请求 live membership 查询保证（会话存续不等于
//    权限存续）——本层不做权限缓存。
import crypto from 'node:crypto';

const MU_COOKIE = 'mu_session';
const MU_CSRF_COOKIE = 'mp_csrf';
const DEFAULT_TTL_MS = Number(process.env.MU_SESSION_TTL_MS || 8 * 3600_000);

const sha256Of = (v) => crypto.createHash('sha256').update(String(v)).digest('hex');

function cookieAttrs() {
  // 生产模式强制 Secure（cookie 不经明明文 HTTP 传输）
  const secure = process.env.NODE_ENV === 'production' ? '; Secure' : '';
  return `Path=/; HttpOnly; SameSite=Lax${secure}`;
}

export function muSessionCookie(token, maxAgeMs) {
  return `${MU_COOKIE}=${token}; ${cookieAttrs()}; Max-Age=${Math.floor(maxAgeMs / 1000)}`;
}
export function muCsrfCookie(csrf, maxAgeMs) {
  // 双提交 CSRF：可读 cookie + 请求头比对（非 HttpOnly 是刻意的）
  return `${MU_CSRF_COOKIE}=${csrf}; Path=/; SameSite=Lax; Max-Age=${Math.floor(maxAgeMs / 1000)}`;
}
export function muClearCookies() {
  return [`${MU_COOKIE}=; ${cookieAttrs()}; Max-Age=0`, `${MU_CSRF_COOKIE}=; Path=/; SameSite=Lax; Max-Age=0`];
}
export function muTokenFromCookieHeader(header) {
  if (typeof header !== 'string') return '';
  for (const part of header.split(';')) {
    const [k, ...v] = part.trim().split('=');
    if (k === MU_COOKIE) return v.join('=');
  }
  return '';
}

export async function issueMuSession(store, { userId, tenantId, login, role, provider }) {
  const csrf = crypto.randomBytes(24).toString('base64url');
  const ttlMs = DEFAULT_TTL_MS;
  const { token, expiresAt } = await store.createSession({
    userId, tenantId, login, role, provider, ttlMs, csrfHash: sha256Of(csrf),
  });
  const maxAge = Math.max(1000, new Date(expiresAt).getTime() - Date.now());
  return { token, csrf, expiresAt, setCookie: [muSessionCookie(token, maxAge), muCsrfCookie(csrf, maxAge)] };
}

export async function resolveMuSession(store, req) {
  const token = muTokenFromCookieHeader(req.headers.cookie);
  if (!token) return null;
  return store.findSessionByToken(token); // null=不存在/已撤销/已过期/用户停用
}

export function muCsrfOk(session, req) {
  const header = String(req.headers['x-csrf-token'] ?? '');
  if (!header || !session?.csrf_hash) return false;
  const a = Buffer.from(sha256Of(header));
  const b = Buffer.from(session.csrf_hash);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

export async function rotateMuSession(store, sessionId, { tenantId, role }) {
  const rotated = await store.rotateSession(sessionId, { tenantId, role });
  if (!rotated) return null;
  const maxAge = Math.max(1000, new Date(rotated.expiresAt).getTime() - Date.now());
  return { ...rotated, setCookie: [muSessionCookie(rotated.token, maxAge), muCsrfCookie(rotated.csrf, maxAge)] };
}
