// console/backend/lib/multiuser/oauth.mjs — GitHub OAuth authorization-code 客户端（Wave 2A）。
//
// 安全合同：
//  * 配置显式三项：MU_GITHUB_OAUTH_CLIENT_ID / _CLIENT_SECRET / _CALLBACK_URL——
//    任一缺失 → configured:false，全部端点 fail-closed（不伪装可用）；
//  * callback URL 只来自配置，绝不信任 Host 头或请求参数；
//  * scope 最小化：read:user（仅读数字 id 与 login，不请求 email repo 等）；
//  * access_token 只在内存中瞬时使用（换取身份后即弃），绝不入库/入日志/入审计；
//  * 身份键 = GitHub 数字 user id（subject='github-oauth:<id>'），不按 login/email
//    合并——login 改名后身份不变；
//  * 测试注入：__setOAuthClientForTests 替换 HTTP 层（本地 mock，不访问真实 GitHub）。
import crypto from 'node:crypto';

export function oauthConfig(env = process.env) {
  const clientId = env.MU_GITHUB_OAUTH_CLIENT_ID || '';
  const clientSecret = env.MU_GITHUB_OAUTH_CLIENT_SECRET || '';
  const callbackUrl = env.MU_GITHUB_OAUTH_CALLBACK_URL || '';
  const configured = Boolean(clientId && clientSecret && callbackUrl);
  return { configured, clientId, clientSecret, callbackUrl,
    scope: 'read:user',
    authorizeUrl: 'https://github.com/login/oauth/authorize',
    tokenUrl: 'https://github.com/login/oauth/access_token',
    identityUrl: 'https://api.github.com/user',
    reason: configured ? null : 'oauth_not_configured',
  };
}

// 真实 GitHub 客户端（fetch 实现；测试以 mock 替换——见 __setOAuthClientForTests）
const realClient = {
  async exchangeCode(cfg, code) {
    const res = await fetch(cfg.tokenUrl, {
      method: 'POST',
      headers: { accept: 'application/json', 'content-type': 'application/json' },
      body: JSON.stringify({
        client_id: cfg.clientId, client_secret: cfg.clientSecret,
        code, redirect_uri: cfg.callbackUrl,
      }),
    });
    if (!res.ok) throw new Error(`oauth_token_http_${res.status}`);
    const body = await res.json().catch(() => ({}));
    if (!body.access_token) throw new Error('oauth_token_exchange_failed');
    return body.access_token; // 仅内存瞬时持有
  },
  async fetchIdentity(cfg, accessToken) {
    const res = await fetch(cfg.identityUrl, {
      headers: { authorization: `Bearer ${accessToken}`, accept: 'application/vnd.github+json' },
    });
    if (!res.ok) throw new Error(`oauth_identity_http_${res.status}`);
    const u = await res.json().catch(() => ({}));
    const id = Number(u.id);
    if (!Number.isInteger(id) || id <= 0 || !u.login) throw new Error('oauth_identity_invalid');
    return { id: String(id), login: String(u.login) }; // 身份键=数字 id；login 仅句柄
  },
};

let clientImpl = realClient;
export function __setOAuthClientForTests(impl) { clientImpl = impl; }
export function __resetOAuthClient() { clientImpl = realClient; }

export function newState() {
  return crypto.randomBytes(32).toString('base64url'); // 高熵（256bit）
}

export function buildAuthorizeUrl(cfg, state) {
  const u = new URL(cfg.authorizeUrl);
  u.searchParams.set('client_id', cfg.clientId);
  u.searchParams.set('redirect_uri', cfg.callbackUrl);
  u.searchParams.set('scope', cfg.scope);
  u.searchParams.set('state', state);
  return u.toString();
}

// code→身份（token 即弃）：返回 {subject, login}
export async function exchangeForIdentity(cfg, code) {
  const accessToken = await clientImpl.exchangeCode(cfg, code);
  try {
    const identity = await clientImpl.fetchIdentity(cfg, accessToken);
    return { subject: `github-oauth:${identity.id}`, login: identity.login };
  } finally {
    // 显式丢弃引用（无日志、无返回）；函数返回后 token 不可达
  }
}
