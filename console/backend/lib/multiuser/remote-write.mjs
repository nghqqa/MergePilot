// remote-write.mjs — 用户授权远端写入服务（应用修复到 PR 源分支 + 合并 PR）。
//
// 职责（与建议链的职责分离）：
//  * 本模块执行**用户授权的实际远端写入**（创建提交/合并 PR）；
//  * 模型与四角色链只生成建议——本模块不调用 LLM/AgentTeams；
//  * 凭据始终来自部署级 env（Gitee PAT / GitHub App installation token），
//    不进入用户 session/OAuth token/前端/日志/临时 Git 配置；
//  * 每次远端写入绑定持久化操作记录（mu.fix_apply_operation），
//    幂等键=run_id+operation_type+patch_digest 防重复；
//  * 远端条件更新（expected head）——并发推进时拒绝陈旧操作；
//  * 结果不确定（超时/连接中断）先读回对账，不盲目重试。

import crypto from 'node:crypto';

// ── Gitee Git Data API（原子提交：blob→tree→commit→ref）────────────────

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
 * Gitee 原子提交：将补丁文件集合作为一个完整提交推送到指定分支。
 * 使用 Git Data API（blob→tree→commit→ref 四步原子链）。
 * expectedHead 条件更新——远端分支 head 已变化时返回 { ok:false, reason:'stale_head' }。
 */
export async function giteeCreateCommit({ env, owner, repo, branch, expectedHead, files, message }) {
  // 1) 获取当前分支 head commit SHA
  const ref = await giteeApi('GET', `/repos/${owner}/${repo}/git/refs/heads/${branch}`, { env });
  if (!ref.ok) return { ok: false, reason: 'branch_not_found', detail: ref.text };
  const currentSha = ref.json?.object?.sha;
  if (currentSha !== expectedHead) {
    return { ok: false, reason: 'stale_head', detail: `expected=${expectedHead?.slice(0, 12)} actual=${currentSha?.slice(0, 12)}` };
  }

  // 2) 为每个变更文件创建 blob（content=null 表示删除该文件——tree 条目 sha 置空）
  const tree = [];
  for (const f of (files ?? [])) {
    if (f.content === null) { tree.push({ path: f.path, mode: '100644', type: 'blob', sha: null }); continue; }
    const blob = await giteeApi('POST', `/repos/${owner}/${repo}/git/blobs`, {
      env, body: { content: Buffer.from(f.content).toString('base64'), encoding: 'base64' },
    });
    if (!blob.ok) return { ok: false, reason: 'blob_create_failed', detail: blob.text };
    tree.push({ path: f.path, mode: '100644', type: 'blob', sha: blob.json.sha });
  }

  // 3) 获取 base commit 的 tree，创建新 tree（base tree + 变更文件）
  const baseCommit = await giteeApi('GET', `/repos/${owner}/${repo}/git/commits/${currentSha}`, { env });
  if (!baseCommit.ok) return { ok: false, reason: 'base_commit_read_failed' };
  const newTree = await giteeApi('POST', `/repos/${owner}/${repo}/git/trees`, {
    env, body: { base_tree: baseCommit.json.tree.sha, tree },
  });
  if (!newTree.ok) return { ok: false, reason: 'tree_create_failed', detail: newTree.text };

  // 4) 创建新 commit（parent=currentSha）
  const identity = { name: 'MergePilot Bot', email: 'bot@mergepilot.local' };
  const commit = await giteeApi('POST', `/repos/${owner}/${repo}/git/commits`, {
    env, body: { message, tree: newTree.json.sha, parents: [currentSha],
      author: identity, committer: identity },
  });
  if (!commit.ok) return { ok: false, reason: 'commit_create_failed', detail: commit.text };

  // 5) 条件更新分支 ref（old_head=currentSha）
  const update = await giteeApi('PATCH', `/repos/${owner}/${repo}/git/refs/heads/${branch}`, {
    env, body: { sha: commit.json.sha, force: false },
  });
  if (!update.ok) return { ok: false, reason: 'ref_update_failed', detail: update.text };

  return { ok: true, commitSha: commit.json.sha, previousSha: currentSha };
}

/**
 * Gitee 合并 PR（指定 head 条件更新）。
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

// ── GitHub（App installation token）────────────────────────────────────

async function ghInstallationToken(env, installationId) {
  const jwt = createAppJwt(env);
  const res = await fetch(`https://api.github.com/app/installations/${installationId}/access_tokens`, {
    method: 'POST', headers: { authorization: `Bearer ${jwt}`, accept: 'application/vnd.github+json' },
  });
  if (!res.ok) throw new Error(`ghp_token_http_${res.status}`);
  const body = await res.json();
  if (!body.token) throw new Error('ghp_token_missing');
  return body.token;
}
export { ghInstallationToken as githubInstallationToken };

function createAppJwt(env) {
  const appId = env.MU_GITHUB_APP_ID;
  let key = env.MU_GITHUB_APP_PRIVATE_KEY ?? '';
  if (key.includes('\\n')) key = key.replace(/\\n/g, '\n');
  const b64 = o => Buffer.from(JSON.stringify(o)).toString('base64url');
  const now = Math.floor(Date.now() / 1000);
  const head = b64({ alg: 'RS256', typ: 'JWT' }) + '.' + b64({ iat: now - 60, exp: now + 540, iss: String(appId) });
  return head + '.' + crypto.createSign('RSA-SHA256').update(head).sign(key, 'base64url');
}

async function ghApi(method, endpoint, { env, installationId, body = null, token = null }) {
  const tok = token || await ghInstallationToken(env, installationId);
  const res = await fetch(`https://api.github.com${endpoint}`, {
    method,
    headers: { 'content-type': 'application/json', authorization: `Bearer ${tok}`, accept: 'application/vnd.github+json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text().catch(() => '');
  let json = null;
  try { json = JSON.parse(text); } catch { /* */ }
  return { status: res.status, ok: res.ok, json, text: text.slice(0, 500), token: tok };
}

/**
 * GitHub 原子提交：Git Data API（blob→tree→commit→ref）。
 */
export async function githubCreateCommit({ env, installationId, owner, repo, branch, expectedHead, files, message }) {
  let r = await ghApi('GET', `/repos/${owner}/${repo}/git/refs/heads/${branch}`, { env, installationId });
  if (!r.ok) return { ok: false, reason: 'branch_not_found' };
  const currentSha = r.json?.object?.sha;
  if (currentSha !== expectedHead) {
    return { ok: false, reason: 'stale_head', detail: `expected=${expectedHead?.slice(0, 12)} actual=${currentSha?.slice(0, 12)}` };
  }

  const tree = [];
  let tok = null;
  for (const f of (files ?? [])) {
    if (f.content === null) { tree.push({ path: f.path, mode: '100644', type: 'blob', sha: null }); continue; }
    const blob = await ghApi('POST', `/repos/${owner}/${repo}/git/blobs`,
      { env, installationId, body: { content: Buffer.from(f.content).toString('base64'), encoding: 'base64' } });
    if (!blob.ok) return { ok: false, reason: 'blob_create_failed' };
    tok = blob.token; tree.push({ path: f.path, mode: '100644', type: 'blob', sha: blob.json.sha });
  }

  const baseCommit = await ghApi('GET', `/repos/${owner}/${repo}/git/commits/${currentSha}`, { env, installationId });
  if (!baseCommit.ok) return { ok: false, reason: 'base_commit_read_failed' };
  const newTree = await ghApi('POST', `/repos/${owner}/${repo}/git/trees`,
    { env, installationId, body: { base_tree: baseCommit.json.tree.sha, tree } });
  if (!newTree.ok) return { ok: false, reason: 'tree_create_failed' };

  const identity = { name: 'MergePilot Bot', email: 'bot@mergepilot.local' };
  const commit = await ghApi('POST', `/repos/${owner}/${repo}/git/commits`,
    { env, installationId, body: { message, tree: newTree.json.sha, parents: [currentSha], author: identity, committer: identity } });
  if (!commit.ok) return { ok: false, reason: 'commit_create_failed' };

  const update = await ghApi('PATCH', `/repos/${owner}/${repo}/git/refs/heads/${branch}`,
    { env, installationId, body: { sha: commit.json.sha, force: false } });
  if (!update.ok) return { ok: false, reason: 'ref_update_failed' };

  return { ok: true, commitSha: commit.json.sha, previousSha: currentSha };
}

/**
 * GitHub 合并 PR（指定 head 条件更新）。
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

/**
 * 平台分发器——按 provider 调用对应的写入方法。
 */
export async function platformCreateCommit(ctx) {
  return ctx.provider === 'github'
    ? githubCreateCommit(ctx)
    : giteeCreateCommit(ctx);
}

export async function platformMergePR(ctx) {
  return ctx.provider === 'github'
    ? githubMergePR(ctx)
    : giteeMergePR(ctx);
}
