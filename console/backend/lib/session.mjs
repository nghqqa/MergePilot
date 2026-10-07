// console/backend/lib/session.mjs — 服务端会话 + 仓库 allowlist（CANONICAL_CONSOLE_PROMOTION）。
// 迁移自 demo-platform 已验证实现（RC 轮 401/403/TTL/重启全实测），对齐 console 契约 v2：
//   * 会话权威 = 服务端 Cookie mp_session（HttpOnly、SameSite=Strict、HMAC 签名 opaque sid）；
//   * GET /api/auth/session：未登录 401 JSON {error:{reason}}，服务端不重定向；
//   * 副作用方法（POST login/logout）携带 X-CSRF-Token；login 为会话引导步骤豁免
//     （无会话可锚定），logout 强制校验；
//   * 凭据 = 环境变量配置的具名 pilot 操作员（不新建账号库，不伪造登录成功）；
//   * MERGEPILOT_REPO_ALLOWLIST 行级数据边界（未授权仓库 → 403 或无数据）。
// 零第三方依赖；内存会话（单进程 staging；重启即失效需重登 —— 诚实恢复语义）。
import crypto from 'node:crypto';

const TTL_MS = Number(process.env.CONSOLE_SESSION_TTL_MS || 8 * 3600 * 1000);
const SESSION_COOKIE = 'mp_session';
const CSRF_COOKIE = 'mp_csrf';

function secret() {
  return process.env.CONSOLE_SESSION_SECRET || '';
}

import { parseAccessModel, resolveRepos, resolveSubject } from './permissions.mjs';

// G-07 多凭证：每用户独立口令（来自受控 env/secrets，绝不入库入仓）。
// 默认拒绝：模型无此主体 或 凭据表无此用户 => 401。
function userCredentials() {
  const raw = process.env.CONSOLE_USER_CREDENTIALS_JSON;
  if (!raw) return null;
  try { return JSON.parse(raw); } catch { return null; }
}

export function consoleAuthConfigured() {
  return Boolean(secret() && process.env.CONSOLE_PILOT_USER && process.env.CONSOLE_PILOT_PASSWORD);
}

export function repoAllowlist() {
  // 2026-09-26：配置访问模型时按主体解析（最小权限：模型内无此主体=空）；
  // 未配置回落 legacy 单用户 allowlist。
  const model = parseAccessModel();
  if (model.mode === 'model') {
    return resolveRepos(model, process.env.CONSOLE_PILOT_USER || '');
  }
  return (process.env.CONSOLE_REPO_ALLOWLIST || '')
    .split(',').map((s) => s.trim()).filter(Boolean);
}

function timingSafeEqualString(a, b) {
  const x = Buffer.from(a); const y = Buffer.from(b);
  if (x.length !== y.length) { const n = Math.max(x.length, y.length); const t = Buffer.alloc(n); crypto.timingSafeEqual(t, t); return false; }
  return crypto.timingSafeEqual(x, y);
}

function sign(sid) {
  const mac = crypto.createHmac('sha256', secret()).update(sid).digest('base64url');
  return `${sid}.${mac}`;
}

function verify(token) {
  if (typeof token !== 'string' || !token.includes('.')) return null;
  const i = token.lastIndexOf('.');
  const sid = token.slice(0, i);
  const expect = crypto.createHmac('sha256', secret()).update(sid).digest('base64url');
  const a = Buffer.from(token.slice(i + 1));
  const b = Buffer.from(expect);
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) return null;
  return sid;
}

const store = new Map(); // sid -> { user, repos, csrf, expiresAt }

function sweep() {
  const now = Date.now();
  for (const [k, v] of store) if (v.expiresAt < now) store.delete(k);
}

// L-2（2026-09-27 加固波）：导出供全部安全敏感比较统一使用（logout/rotate 等）。
export function safeEqual(a, b) {
  const ba = Buffer.from(String(a));
  const bb = Buffer.from(String(b));
  if (ba.length !== bb.length) return false;
  return crypto.timingSafeEqual(ba, bb);
}

function sessionCookie(token, maxAgeMs) {
  return `${SESSION_COOKIE}=${token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=${Math.floor(maxAgeMs / 1000)}`;
}

function csrfCookie(token) {
  return `${CSRF_COOKIE}=${token}; Path=/; SameSite=Strict; Max-Age=${Math.floor(TTL_MS / 1000)}`;
}

export function login(user, password) {
  // MU Phase 2：正式多用户模式下禁止 legacy 共享/环境账号登录（迁移模式例外须显式
  // MU_LEGACY_LOGIN=1）。默认 MU_MODE=legacy——本门只在显式 multiuser 时生效，
  // Phase 0a 及全部既有登录语义零改动。
  if (process.env.MU_MODE === 'multiuser' && process.env.MU_LEGACY_LOGIN !== '1') {
    return { ok: false, status: 403, code: 'legacy_login_disabled_in_multiuser',
      error: { reason: 'legacy_login_disabled_in_multiuser',
        detail: '正式多用户模式禁用共享操作员账号——使用 /api/mu/auth/login（OAuth/身份提供商）' } };
  }
  const model = parseAccessModel();
  const creds = userCredentials();
  if (model.mode === 'model' && creds && typeof creds === 'object') {
    const subject = resolveSubject(model, String(user ?? ''));
    const expect = creds[String(user ?? '')] ?? creds[String(user ?? '')]?.password ?? null;
    if (!subject || !expect || typeof password !== 'string' ||
        !timingSafeEqualString(String(password), String(expect))) {
      return { ok: false, status: 401, code: 'not_authenticated',
        error: { reason: 'not_authenticated' } };
    }
    const sid = crypto.randomUUID();
    const csrf = crypto.randomBytes(24).toString('base64url');
    store.set(sid, { user: subject.subject, repos: resolveRepos(model, subject.subject), csrf, expiresAt: Date.now() + TTL_MS });
    return { ok: true, setCookie: [sessionCookie(sign(sid), TTL_MS), csrfCookie(csrf)] };
  }
  if (!consoleAuthConfigured()) {
    return { ok: false, status: 503, code: 'auth_unavailable',
      error: { reason: 'auth_unavailable' } };
  }
  if (typeof user !== 'string' || typeof password !== 'string'
      || !safeEqual(user, process.env.CONSOLE_PILOT_USER)
      || !safeEqual(password, process.env.CONSOLE_PILOT_PASSWORD)) {
    return { ok: false, status: 401, code: 'not_authenticated',
      error: { reason: 'not_authenticated' } };
  }
  sweep();
  const sid = crypto.randomBytes(24).toString('hex');
  const csrf = crypto.randomBytes(24).toString('hex');
  store.set(sid, { user, repos: repoAllowlist(), csrf, expiresAt: Date.now() + TTL_MS });
  return { ok: true, sid,
    setCookie: [sessionCookie(sign(sid), TTL_MS), csrfCookie(csrf)] };
}

export function logout(token, csrfHeader) {
  const sid = verify(token);
  if (!sid) return { ok: true, setCookie: [sessionCookie('', 0)] };
  const s = store.get(sid);
  // 契约 §0.1：副作用方法必须携带 X-CSRF-Token —— 有会话而缺令牌即拒绝
  if (s && (!csrfHeader || !safeEqual(csrfHeader, s.csrf))) {
    return { ok: false, status: 403, code: 'csrf_required', error: { reason: 'csrf_required' } };
  }
  store.delete(sid);
  return { ok: true, setCookie: [sessionCookie('', 0), `${CSRF_COOKIE}=; Path=/; Max-Age=0`] };
}

export function getSession(token) {
  sweep();
  const sid = verify(token);
  if (!sid) return null;
  const s = store.get(sid);
  if (!s || s.expiresAt < Date.now()) return null;
  const { user, repos, csrf, expiresAt, mu } = s;
  return { user, repos, csrf, expiresAt, ...(mu ? { mu } : {}) };
}

// MU Phase 2：多用户会话签发（身份已由调用方经 ExternalIdentity 解析——本层不认
// 任何密码/token）。会话载荷附 mu={userId,tenantId,login,role,provider}；
// cookie/CSRF/TTL 与 legacy 会话同一纪律（HttpOnly+SameSite=Strict+HMAC 签名 sid）。
export function createMuSession({ login: muLogin, userId, tenantId, role, provider }) {
  sweep();
  const sid = crypto.randomUUID();
  const csrf = crypto.randomBytes(24).toString('base64url');
  store.set(sid, {
    user: muLogin, repos: [], csrf, expiresAt: Date.now() + TTL_MS,
    mu: { userId, tenantId, login: muLogin, role, provider },
  });
  return { ok: true, csrf, setCookie: [sessionCookie(sign(sid), TTL_MS), csrfCookie(csrf)] };
}

export function sessionBody(auth) {
  // 契约 §1：200 带 user；401 JSON 不重定向。
  // R4（FB-06）：user 为结构化对象（前端不再显示"未知用户"）。
  // 数据可信度加固（PR #320 三波）：显式认证 provider——
  //   mu 载荷存在 → 会话由该 provider 签发（当前唯一值 'github-oauth'，oauth.mjs）；
  //   无 mu 载荷   → legacy 会话，本服务唯一 legacy 登录路径是操作员账号密码
  //   （/api/auth/login user+password）。前端按该显式标记取身份来源，不再按模式推断。
  const loginType = auth.mu?.provider ?? 'operator_password';
  return { user: { name: auth.user, login_type: loginType },
           session_source: auth.mu ? 'mu_session' : 'legacy_session',
           expires_at: new Date(auth.expiresAt).toISOString(),
           repos: auth.repos };
}

export function anonymousBody(reason = 'not_authenticated', env = process.env) {
  // 登录页能力驱动渲染的唯一后端信号（无秘密；布尔 capability）：
  //   legacy_login = 旧操作员账号密码（POST /api/auth/login）当前是否可用
  //     —— multiuser 生产形态默认禁用（本文件 login() 门禁），MU_LEGACY_LOGIN=1 显式例外；
  //   multiuser   = MU 多用户形态（决定登录页是否呈现邀请制提示/隐藏演示入口）。
  // 注意：这与 /api/mu/auth/login（fixture 身份，MU_ALLOW_FIXTURE_LOGIN 门禁）是两个独立端点。
  const multiuser = env.MU_MODE === 'multiuser';
  const legacyLoginEnabled = !multiuser || env.MU_LEGACY_LOGIN === '1';
  return { error: { reason }, capabilities: { legacy_login: legacyLoginEnabled, multiuser } };
}

export function tokenFromCookieHeader(header) {
  if (typeof header !== 'string') return '';
  for (const part of header.split(';')) {
    const [k, ...v] = part.trim().split('=');
    if (k === SESSION_COOKIE) return v.join('=');
  }
  return '';
}

export function sessionTtlMs() { return TTL_MS; }
