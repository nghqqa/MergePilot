// console/backend/lib/multiuser/ghapp.mjs — GitHub App 只读 adapter（Wave 2B）。
//
// 权限合同（最小化，只读）：
//  * Metadata:read / Contents:read / Pull requests:read / Checks:read /
//    Commit statuses:read——不申请 Issues/Members/Administration/Actions，
//    Contents-write / PR-write / Administration-write 明确禁止（代码无任何写调用）；
//  * webhook 事件订阅：installation / installation_repositories / pull_request /
//    check_run / status（其余不订阅——未消费的事件不加）；
//  * private key / webhook secret 仅 env 显式配置，绝不入库/日志/审计；
//  * 测试注入：__setGithubAppForTests 替换 HTTP 层（合成密钥，零真实 GitHub）。
import crypto from 'node:crypto';

export const GHAPP_PERMISSIONS = [
  'metadata:read', 'contents:read', 'pull_requests:read',
  'checks:read', 'statuses:read',
];
export const GHAPP_EVENTS = [
  'installation', 'installation_repositories', 'pull_request', 'check_run', 'status',
];

export function ghAppConfig(env = process.env) {
  const appId = Number(env.MU_GITHUB_APP_ID || 0);
  const privateKey = env.MU_GITHUB_APP_PRIVATE_KEY || '';
  const webhookSecret = env.MU_GITHUB_WEBHOOK_SECRET || '';
  const installCallbackUrl = env.MU_GITHUB_APP_INSTALL_CALLBACK_URL || '';
  const configured = Boolean(appId && privateKey && webhookSecret && installCallbackUrl);
  return { configured, appId, webhookSecret, installCallbackUrl,
    reason: configured ? null : 'github_app_not_configured',
    permissions: GHAPP_PERMISSIONS, events: GHAPP_EVENTS,
    installUrl: 'https://github.com/apps/margepilot-dev/installations/new' };
}

// RS256 app JWT（真实路径；私钥仅驻内存）
function base64url(buf) { return Buffer.from(buf).toString('base64url'); }
export function createAppJwt(appId, privateKeyPem, nowMs = Date.now()) {
  const header = base64url(JSON.stringify({ alg: 'RS256', typ: 'JWT' }));
  const payload = base64url(JSON.stringify({
    iat: Math.floor(nowMs / 1000) - 60, exp: Math.floor(nowMs / 1000) + 600,
    iss: String(appId),
  }));
  const sig = crypto.createSign('RSA-SHA256').update(`${header}.${payload}`)
    .sign(privateKeyPem);
  return `${header}.${payload}.${base64url(sig)}`;
}

// 真实 GitHub 客户端（只读；fetch 实现——测试以 mock 替换）
const realClient = {
  async exchangeInstallationToken(cfg, installationId) {
    const jwt = createAppJwt(cfg.appId, process.env.MU_GITHUB_APP_PRIVATE_KEY);
    const res = await fetch(`https://api.github.com/app/installations/${installationId}/access_tokens`, {
      method: 'POST', headers: { authorization: `Bearer ${jwt}`, accept: 'application/vnd.github+json' },
    });
    if (!res.ok) throw new Error(`ghapp_token_http_${res.status}`);
    const body = await res.json().catch(() => ({}));
    if (!body.token) throw new Error('ghapp_token_failed');
    return body.token; // 内存瞬时——仅 listRepositories 内部使用
  },
  async listRepositories(_cfg, installationId) {
    const token = await this.exchangeInstallationToken(ghAppConfig(), installationId);
    const res = await fetch(`https://api.github.com/installation/repositories?per_page=100`, {
      headers: { authorization: `Bearer ${token}`, accept: 'application/vnd.github+json' },
    });
    if (!res.ok) throw new Error(`ghapp_repos_http_${res.status}`);
    const body = await res.json().catch(() => ({}));
    return (body.repositories ?? []).map((r) => ({
      id: Number(r.id), name: String(r.name),
      owner_login: String(r.owner?.login ?? ''), owner_id: Number(r.owner?.id ?? 0),
      default_branch: r.default_branch ?? null, private: Boolean(r.private),
    }));
  },
};

let clientImpl = realClient;
export function __setGithubAppForTests(impl) { clientImpl = impl; }
export function __resetGithubApp() { clientImpl = realClient; }
export function listInstallationRepositories(cfg, installationId) {
  return clientImpl.listRepositories(cfg, installationId);
}
