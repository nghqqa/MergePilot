// console/backend/lib/multiuser/giteeoauth.mjs — Gitee OAuth authorization-code 客户端。
//
// 与 oauth.mjs（GitHub）同构的安全合同，差异仅在 provider 端点与身份前缀：
//  * 配置显式三项：MU_GITEE_OAUTH_CLIENT_ID / _CLIENT_SECRET / _CALLBACK_URL——
//    任一缺失 → configured:false，全部端点 fail-closed（不伪装可用）；
//  * callback URL 只来自配置，绝不信任 Host 头或请求参数；
//  * scope 最小化：user_info（Gitee 基础身份 scope，仅读数字 id 与 login）；
//  * access_token 只在内存中瞬时使用（换取身份后即弃），绝不入库/入日志/入审计；
//  * 身份键 = Gitee 数字 user id（subject='gitee-oauth:<id>'），不按 login/email
//    合并——与 GitHub 身份（github-oauth:<id>）前缀隔离，两平台数字 id 空间独立，
//    绝不混用（同名 login / 相同邮箱不构成同一身份）；
//  * 测试注入：__setGiteeOAuthClientForTests 替换 HTTP 层（本地 mock，不访问真实 Gitee）；
//    真实 Gitee OAuth 应用凭据缺失时如实 configured:false（见 PR 缺失输入清单）。
//
// Gitee 端点契约（官方 v5 文档）：
//  * 授权：GET https://gitee.com/oauth/authorize?client_id&redirect_uri&response_type=code&scope&state
//  * 换 token：POST https://gitee.com/oauth/token（grant_type=authorization_code + code/
//    client_id/client_secret/redirect_uri）→ JSON { access_token, ... }；
//  * 身份：GET https://gitee.com/api/v5/user?access_token=… → JSON { id:number, login }。
import crypto from 'node:crypto';

export function giteeOAuthConfig(env = process.env) {
  const clientId = env.MU_GITEE_OAUTH_CLIENT_ID || '';
  const clientSecret = env.MU_GITEE_OAUTH_CLIENT_SECRET || '';
  const callbackUrl = env.MU_GITEE_OAUTH_CALLBACK_URL || '';
  const configured = Boolean(clientId && clientSecret && callbackUrl);
  return { configured, clientId, clientSecret, callbackUrl,
    scope: 'user_info',
    authorizeUrl: 'https://gitee.com/oauth/authorize',
    tokenUrl: 'https://gitee.com/oauth/token',
    identityUrl: 'https://gitee.com/api/v5/user',
    reason: configured ? null : 'oauth_not_configured',
  };
}

// 真实 Gitee 客户端（fetch 实现；测试以 mock 替换——见 __setGiteeOAuthClientForTests）。
// token 请求按 Gitee 文档以查询串携带参数（grant_type 必填；client_secret 同传）。
const realClient = {
  async exchangeCode(cfg, code) {
    const u = new URL(cfg.tokenUrl);
    u.searchParams.set('grant_type', 'authorization_code');
    u.searchParams.set('code', code);
    u.searchParams.set('client_id', cfg.clientId);
    u.searchParams.set('client_secret', cfg.clientSecret);
    u.searchParams.set('redirect_uri', cfg.callbackUrl);
    const res = await fetch(u, { method: 'POST', headers: { accept: 'application/json' } });
    if (!res.ok) throw new Error(`oauth_token_http_${res.status}`);
    const body = await res.json().catch(() => ({}));
    if (!body.access_token) throw new Error('oauth_token_exchange_failed');
    return body.access_token; // 仅内存瞬时持有
  },
  async fetchIdentity(cfg, accessToken) {
    const u = new URL(cfg.identityUrl);
    u.searchParams.set('access_token', accessToken);
    const res = await fetch(u, { headers: { accept: 'application/json' } });
    if (!res.ok) throw new Error(`oauth_identity_http_${res.status}`);
    const g = await res.json().catch(() => ({}));
    const id = Number(g.id);
    if (!Number.isInteger(id) || id <= 0 || !g.login) throw new Error('oauth_identity_invalid');
    return { id: String(id), login: String(g.login) }; // 身份键=数字 id；login 仅句柄
  },
};

let clientImpl = realClient;
export function __setGiteeOAuthClientForTests(impl) { clientImpl = impl; }
export function __resetGiteeOAuthClient() { clientImpl = realClient; }

export function newGiteeState() {
  return crypto.randomBytes(32).toString('base64url'); // 高熵（256bit）
}

export function buildGiteeAuthorizeUrl(cfg, state) {
  const u = new URL(cfg.authorizeUrl);
  u.searchParams.set('client_id', cfg.clientId);
  u.searchParams.set('redirect_uri', cfg.callbackUrl);
  u.searchParams.set('response_type', 'code');
  u.searchParams.set('scope', cfg.scope);
  u.searchParams.set('state', state);
  return u.toString();
}

// code→身份（token 即弃）：返回 {subject, login}
export async function exchangeForGiteeIdentity(cfg, code) {
  const accessToken = await clientImpl.exchangeCode(cfg, code);
  try {
    const identity = await clientImpl.fetchIdentity(cfg, accessToken);
    return { subject: `gitee-oauth:${identity.id}`, login: identity.login };
  } finally {
    // 显式丢弃引用（无日志、无返回）；函数返回后 token 不可达
  }
}
