// remote-write.mjs — 用户授权远端写入服务（应用修复到 PR 源分支 + 合并 PR）。
//
// 职责（与建议链的职责分离）：
//  * 本模块执行**用户授权的实际远端写入**（创建提交/合并 PR）；
//  * 模型与四角色链只生成建议——本模块不调用 LLM/AgentTeams；
//  * 凭据始终来自部署级 env（Gitee PAT / GitHub App installation token），
//    不进入用户 session/OAuth token/前端/日志/临时 Git 配置；
//  * 每次远端写入绑定持久化操作记录（mu.fix_apply_operation），
//    幂等键=run_id+operation_type+patch_digest 防重复；
//  * 远端条件更新——分支推进时拒绝，不覆盖他人提交：
//      - 创建提交：pushCommit（隔离副本 apply→commit→git push 无 --force——
//        非快进被服务端拒绝，条件更新在远端强制生效；Gitee v5 实测无 git/refs
//        API，Git Data 链无法移动分支——2026-10-15 定案，双平台统一 push 语义）；
//      - 合并 PR：平台条件合并 API（Gitee PUT merge sha / GitHub merge sha 均为
//        服务端 head 匹配检查）；
//  * 结果不确定（超时/连接中断）先读回对账，不盲目重试。

import crypto from 'node:crypto';
import { pushCommit } from './patch-applicability.mjs';

/**
 * 平台统一：在指定分支创建远端提交（推送式，条件更新）。
 * ctx：{ env, provider, owner, repo, branch, expectedHead, diffText, message,
 *        installationId?(github), credentialToken?(覆盖默认取法) }
 */
export async function platformCreateCommit(ctx) {
  const { env, provider, owner, repo, branch, expectedHead, diffText, message } = ctx;
  // repoUrl 可显式传入（测试注本地裸仓；生产不传——按 provider 构造）
  const repoUrl = ctx.repoUrl ?? `https://${providerHost(provider, env)}/${owner}/${repo}.git`;
  const credentialToken = ctx.credentialToken
    ?? (provider === 'github' ? await githubInstallationToken(env, ctx.installationId) : null);
  return pushCommit({ repoUrl, headSha: expectedHead, diffText, branch, message, credentialToken,
    workRoot: env.MP_PUSH_WORKROOT ?? undefined });
}

function providerHost(provider, env) {
  return provider === 'github' ? 'github.com'
    : String(env.MU_GITEE_INSTANCE_WEB_BASE ?? 'https://gitee.com').replace(/^https?:\/\//, '').replace(/\/+$/, '');
}

// ── GitHub（App installation token）────────────────────────────────────

export async function githubInstallationToken(env, installationId) {
  const jwt = createAppJwt(env);
  const res = await fetch(`https://api.github.com/app/installations/${installationId}/access_tokens`, {
    method: 'POST', headers: { authorization: `Bearer ${jwt}`, accept: 'application/vnd.github+json' },
  });
  if (!res.ok) throw new Error(`ghp_token_http_${res.status}`);
  const body = await res.json();
  if (!body.token) throw new Error('ghp_token_missing');
  return body.token;
}

function createAppJwt(env) {
  const appId = env.MU_GITHUB_APP_ID;
  let key = env.MU_GITHUB_APP_PRIVATE_KEY ?? '';
  if (key.includes('\\n')) key = key.replace(/\\n/g, '\n');
  const b64 = (o) => Buffer.from(JSON.stringify(o)).toString('base64url');
  const now = Math.floor(Date.now() / 1000);
  const head = b64({ alg: 'RS256', typ: 'JWT' }) + '.' + b64({ iat: now - 60, exp: now + 540, iss: String(appId) });
  return head + '.' + crypto.createSign('RSA-SHA256').update(head).sign(key, 'base64url');
}

async function ghApi(method, endpoint, { env, installationId, body = null, token = null }) {
  const tok = token || await githubInstallationToken(env, installationId);
  const res = await fetch(`https://api.github.com${endpoint}`, {
    method,
    headers: { 'content-type': 'application/json', authorization: `Bearer ${tok}`, accept: 'application/vnd.github+json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text().catch(() => '');
  let json = null;
  try { json = JSON.parse(text); } catch { /* */ }
  // 安全纪律：token 只在本函数作用域用完即弃——结果对象绝不携带（审阅 P2 项落地）
  return { status: res.status, ok: res.ok, json, text: text.slice(0, 500) };
}

/**
 * GitHub 条件合并（服务端 sha 匹配检查——PR head 推进时拒绝）。
 */
export async function githubMergePR({ env, installationId, owner, repo, prNumber, expectedHead, mergeMethod = 'merge', commitTitle }) {
  const pr = await ghApi('GET', `/repos/${owner}/${repo}/pulls/${prNumber}`, { env, installationId });
  if (!pr.ok) return { ok: false, reason: 'pr_read_failed' };
  if (pr.json.state !== 'open') return { ok: false, reason: 'pr_not_open' };
  if (pr.json.head?.sha !== expectedHead) {
    return { ok: false, reason: 'stale_head', detail: `expected=${expectedHead?.slice(0, 12)} actual=${pr.json.head?.sha?.slice(0, 12)}` };
  }
  const merge = await ghApi('PUT', `/repos/${owner}/${repo}/pulls/${prNumber}/merge`,
    { env, installationId, body: { merge_method: mergeMethod, sha: expectedHead, commit_title: commitTitle ?? `Merge PR #${prNumber}` } });
  if (!merge.ok) return { ok: false, reason: 'merge_failed', detail: merge.text };
  return { ok: true, mergeSha: merge.json?.sha ?? null };
}

// ── Gitee（PAT；API v5）────────────────────────────────────────────────

async function giteeApi(method, endpoint, { env, token = null, body = null }) {
  const pat = token || env.MU_GITEE_PAT || '';
  // Gitee v5 鉴权与现有只读链同源：access_token 走查询参数（publish.mjs 实证形态）。
  const sep = endpoint.includes('?') ? '&' : '?';
  const res = await fetch(`https://gitee.com/api/v5${endpoint}${sep}access_token=${encodeURIComponent(pat)}`, {
    method,
    headers: { 'content-type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text().catch(() => '');
  let json = null;
  try { json = JSON.parse(text); } catch { /* 空或 HTML */ }
  return { status: res.status, ok: res.ok, json, text: text.slice(0, 500) };
}

/**
 * Gitee 条件合并（PUT merge 的 sha 参数=服务端 PR head 匹配检查——推进时拒绝）。
 */
export async function giteeMergePR({ env, owner, repo, prNumber, expectedHead, mergeMethod = 'merge', commitTitle }) {
  const pr = await giteeApi('GET', `/repos/${owner}/${repo}/pulls/${prNumber}`, { env });
  if (!pr.ok) return { ok: false, reason: 'pr_read_failed' };
  if (pr.json.state !== 'open') return { ok: false, reason: 'pr_not_open' };
  if (pr.json.head?.sha !== expectedHead) {
    return { ok: false, reason: 'stale_head', detail: `expected=${expectedHead?.slice(0, 12)} actual=${pr.json.head?.sha?.slice(0, 12)}` };
  }
  const merge = await giteeApi('PUT', `/repos/${owner}/${repo}/pulls/${prNumber}/merge`, {
    env, body: { merge_method: mergeMethod, sha: expectedHead, commit_title: commitTitle ?? `Merge PR #${prNumber}` },
  });
  if (!merge.ok) return { ok: false, reason: 'merge_failed', detail: merge.text };
  return { ok: true, mergeSha: merge.json?.sha ?? null };
}

/**
 * 平台分发器——按 provider 调用对应的写入方法。
 */
export async function platformMergePR(ctx) {
  return ctx.provider === 'github'
    ? githubMergePR(ctx)
    : giteeMergePR(ctx);
}
