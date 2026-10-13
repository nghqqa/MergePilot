// console/backend/lib/multiuser/publish.mjs — 审查结果发布到原 PR（用户授权动作）。
//
// 纪律（2026-10-13 波）：
//  * 发布=独立用户授权动作：run 完成不自动发布；每次发布须服务端校验权限/租户/仓库归属；
//  * 发布前重新核对远端 PR head——与 run 记录 head 不一致 → 拒绝（陈旧结果不发布）；
//  * 幂等：同 (run,head) 的发布更新自己人的既有评论（marker 定位），不刷屏、不覆盖人工评论；
//  * 远端超时/结果不明 → 状态 unconfirmed（先查验再行动，不盲目重发）；
//  * 记录：发布者/run/head/远端评论 id/时间/摘要/状态；
//  * 凭据只在调用栈内存；评论内容经 buildPublishComment 构造（脱敏边界在构造器）。

import { buildPublishComment, publishMarker, publishSummary } from './artifacts.mjs';
import { createAppJwt } from './ghapp.mjs';

const MARKER = (runId, headSha) => publishMarker(runId, headSha);

// ── Gitee 评论原语（PR 复用 issue 评论空间；token 走部署级 PAT——与现有只读链同源）──

function giteeBase(env) {
  const inst = String(env.MU_GITEE_INSTANCE_WEB_BASE ?? 'https://gitee.com');
  return inst.replace(/\/+$/, '');
}

async function giteeCommentApi(env, { method, path, body }) {
  const pat = String(env.MU_GITEE_PAT ?? '');
  if (!pat) throw new Error('gitee_credentials_missing');
  const url = `${giteeBase(env)}/api/v5${path}${path.includes('?') ? '&' : '?'}access_token=${encodeURIComponent(pat)}`;
  const res = await fetch(url, {
    method, headers: { 'content-type': 'application/json' },
    body: body ? JSON.stringify({ ...body, access_token: pat }) : undefined,
  });
  let json = null;
  try { json = await res.json(); } catch { /* 空体容错 */ }
  return { status: res.status, ok: res.ok, json };
}

async function giteeListComments(env, { owner, repo, prNumber }) {
  const r = await giteeCommentApi(env, { method: 'GET',
    path: `/repos/${owner}/${repo}/pulls/${encodeURIComponent(prNumber)}/comments?per_page=100&page=1` });
  if (!r.ok) throw Object.assign(new Error(`gitee_comments_list_http_${r.status}`), { status: r.status });
  return Array.isArray(r.json) ? r.json : [];
}

async function giteeCreateComment(env, { owner, repo, prNumber, body }) {
  const r = await giteeCommentApi(env, { method: 'POST',
    path: `/repos/${owner}/${repo}/pulls/${encodeURIComponent(prNumber)}/comments`, body: { body } });
  if (!r.ok) throw Object.assign(new Error(`gitee_comment_create_http_${r.status}`), { status: r.status });
  return { remoteCommentId: String(r.json?.id ?? ''), remoteUrl: r.json?.html_url ?? null };
}

async function giteeUpdateComment(env, { owner, repo, commentId, body }) {
  // Gitee 更新 PR 评论实测（2026-10-13 三变体探针）：PATCH /repos/{o}/{r}/pulls/comments/{id}
  // =200；PUT 同路径=405；PATCH issues/comments/{id}=404（PR/issue 编号两套空间）。
  const r = await giteeCommentApi(env, { method: 'PATCH',
    path: `/repos/${owner}/${repo}/pulls/comments/${encodeURIComponent(commentId)}`, body: { body } });
  if (!r.ok) throw Object.assign(new Error(`gitee_comment_update_http_${r.status}`), { status: r.status });
  return { remoteCommentId: String(r.json?.id ?? commentId), remoteUrl: r.json?.html_url ?? null };
}

// ── GitHub 评论原语（App installation token；仅内存瞬时）──

async function ghInstallationToken(env, installationId) {
  const jwt = createAppJwt(String(env.MU_GITHUB_APP_ID ?? ''), String(env.MU_GITHUB_APP_PRIVATE_KEY ?? '').replace(/\\n/g, '\n'));
  const res = await fetch(`https://api.github.com/app/installations/${installationId}/access_tokens`, {
    method: 'POST', headers: { authorization: `Bearer ${jwt}`, accept: 'application/vnd.github+json' },
  });
  if (!res.ok) throw new Error(`ghp_token_http_${res.status}`);
  const body = await res.json().catch(() => ({}));
  if (!body.token) throw new Error('ghp_token_failed');
  return body.token;
}

async function ghCommentApi(env, { method, owner, repo, installationId, prNumber, path = null, body = null }) {
  const token = await ghInstallationToken(env, installationId);
  const p = path ?? `/repos/${owner}/${repo}/issues/${encodeURIComponent(prNumber)}/comments`;
  const res = await fetch(`https://api.github.com${p}`, {
    method, headers: { authorization: `Bearer ${token}`, accept: 'application/vnd.github+json',
      'content-type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  let json = null;
  try { json = await res.json(); } catch { /* 空体容错 */ }
  return { status: res.status, ok: res.ok, json };
}

// ── 远端 head 复核（陈旧结果拒绝发布）──

async function fetchRemoteHead(env, { provider, owner, repo, prNumber, installationId }) {
  if (provider === 'gitee') {
    const r = await giteeCommentApi(env, { method: 'GET',
      path: `/repos/${owner}/${repo}/pulls/${encodeURIComponent(prNumber)}` });
    if (!r.ok) throw Object.assign(new Error(`gitee_pr_http_${r.status}`), { status: r.status });
    return String(r.json?.head?.sha ?? '');
  }
  if (provider === 'github') {
    const r = await ghCommentApi(env, { method: 'GET', owner, repo, installationId,
      path: `/repos/${owner}/${repo}/pulls/${encodeURIComponent(prNumber)}` });
    if (!r.ok) throw Object.assign(new Error(`ghp_pr_http_${r.status}`), { status: r.status });
    return String(r.json?.head?.sha ?? '');
  }
  throw new Error('provider_unsupported');
}

/**
 * 实际实现（带 tenant 参数——由 API 层以会话租户调用）。
 */
export async function publishRun(pool, { env = process.env, runId, tenantId, userId,
  detailBaseUrl = null }) {
  // pool 参数既可以是 Pool 实例也可以是注入的查询函数（muPoolQ）——统一成查询函数。
  const q = (typeof pool === 'function') ? pool : (text, params) => pool.query(text, params);
  const run = (await q(
    `SELECT r.run_id, r.status, r.head_sha, r.llm_mode, r.pr_id, r.repo_id,
            pr.provider_pr_number, rep.provider, rep.owner, rep.name AS repo_name,
            (SELECT rb.installation_id FROM mu.repository_binding rb
              WHERE rb.repo_id = r.repo_id AND rb.tenant_id = r.tenant_id
                AND rb.binding_state = 'active' LIMIT 1) AS installation_id
       FROM mu.review_run r
       JOIN mu.pull_request pr ON pr.pr_id = r.pr_id
       JOIN mu.repository rep ON rep.repo_id = r.repo_id
      WHERE r.run_id=$1 AND r.tenant_id=$2`, [runId, tenantId])).rows[0] ?? null;
  if (!run) return { ok: false, stage: 'not_found', reason: 'run_not_found' };
  if (!['COMPLETED', 'VERIFIED'].includes(run.status)) {
    return { ok: false, stage: 'not_verifiable', reason: 'run_not_verifiable', detail: { status: run.status } };
  }
  const provider = String(run.provider ?? '');
  if (!['gitee', 'github'].includes(provider)) {
    return { ok: false, stage: 'provider_unsupported', reason: 'provider_unsupported' };
  }

  // 2) 远端 head 复核（陈旧结果拒绝）
  let remoteHead = '';
  try {
    remoteHead = await fetchRemoteHead(env, { provider, owner: run.owner, repo: run.repo_name,
      prNumber: run.provider_pr_number, installationId: run.installation_id });
  } catch (e) {
    return { ok: false, stage: 'remote_unreachable', reason: String(e.message ?? e).slice(0, 80) };
  }
  if (!remoteHead || remoteHead.toLowerCase() !== String(run.head_sha).toLowerCase()) {
    return { ok: false, stage: 'stale_head', reason: 'head_moved',
      detail: { run_head: String(run.head_sha).slice(0, 12), remote_head: String(remoteHead).slice(0, 12) } };
  }

  // 3) 产物装配（run 的 findings/outputs/patch）
  const findings = (await q(
    `SELECT rule_id, severity, path, line_start, line_end, title, summary_masked, remediation
       FROM mu.agent_finding WHERE run_id=$1 ORDER BY severity, path`, [runId])).rows;
  const outRows = (await q(
    `SELECT agent_role, content FROM mu.review_output WHERE run_id=$1`, [runId])).rows;
  const outputs = {};
  for (const r of outRows) outputs[r.agent_role] = r.content;
  const patchRow = (await q(
    `SELECT head_sha, files, applicability, rationale FROM mu.fix_patch
      WHERE run_id=$1 ORDER BY created_at DESC LIMIT 1`, [runId])).rows[0] ?? null;
  const verification = [
    '静态规则检查（首轮 deterministic）：已执行',
    '模型独立评估（verifier）：已执行——为模型判断，非实际测试',
    '实际测试执行：未执行（DRY_RUN 建议链不含沙箱测试）',
  ];

  const body = buildPublishComment({ run, findings, patch: patchRow ? {
    files: patchRow.files, applicability: patchRow.applicability, head_sha: patchRow.head_sha,
  } : null, verification, repoLabel: `${run.owner}/${run.repo_name}`,
    prNumber: run.provider_pr_number, headSha: run.head_sha,
    detailUrl: detailBaseUrl ? `${detailBaseUrl.replace(/\/+$/, '')}/runs/${runId}` : null,
    reviewedAt: '2026-10-13' });

  const marker = MARKER(run.run_id, run.head_sha);
  const owner = run.owner; const repo = run.repo_name; const prNumber = run.provider_pr_number;

  // 4) 幂等发布：定位自己人的既有评论（marker）→ 更新；无 → 创建
  try {
    let existing = null;
    if (provider === 'gitee') {
      const list = await giteeListComments(env, { owner, repo, prNumber });
      existing = list.find((c) => String(c.body ?? '').includes(marker)) ?? null;
      if (existing) {
        const u = await giteeUpdateComment(env, { owner, repo,
          commentId: String(existing.id), body });
        return await finishPublished(pool, { runId, tenantId, userId, provider,
          prNumber: run.provider_pr_number, headSha: run.head_sha, body,
          remoteCommentId: u.remoteCommentId, remoteUrl: u.remoteUrl });
      }
      const c = await giteeCreateComment(env, { owner, repo, prNumber, body });
      return await finishPublished(pool, { runId, tenantId, userId, provider,
        prNumber: run.provider_pr_number, headSha: run.head_sha, body,
        remoteCommentId: c.remoteCommentId, remoteUrl: c.remoteUrl });
    }
    // github
    const list = await ghCommentApi(env, { method: 'GET', owner, repo,
      installationId: run.installation_id, prNumber });
    const comments = Array.isArray(list.json) ? list.json : [];
    existing = comments.find((c) => String(c.body ?? '').includes(marker)) ?? null;
    if (existing) {
      const u = await ghCommentApi(env, { method: 'PATCH', owner, repo,
        installationId: run.installation_id,
        path: `/repos/${owner}/${repo}/issues/comments/${encodeURIComponent(String(existing.id))}`,
        body: { body } });
      if (!u.ok) throw new Error(`ghp_comment_update_http_${u.status}`);
      return await finishPublished(pool, { runId, tenantId, userId, provider,
        prNumber: run.provider_pr_number, headSha: run.head_sha, body,
        remoteCommentId: String(u.json?.id ?? existing.id), remoteUrl: u.json?.html_url ?? null });
    }
    const c = await ghCommentApi(env, { method: 'POST', owner, repo,
      installationId: run.installation_id, prNumber, body: { body } });
    if (!c.ok) throw new Error(`ghp_comment_create_http_${c.status}`);
    return await finishPublished(pool, { runId, tenantId, userId, provider,
      prNumber: run.provider_pr_number, headSha: run.head_sha, body,
      remoteCommentId: String(c.json?.id ?? ''), remoteUrl: c.json?.html_url ?? null });
  } catch (e) {
    const msg = String(e.message ?? e).slice(0, 100);
    // 远端超时/结果不明 → unconfirmed（先查验再行动，不盲目重发）
    const unconfirmed = /timeout|abort|ECONN|network|socket/i.test(msg);
    const status = unconfirmed ? 'unconfirmed' : 'failed';
    await q(
      `INSERT INTO mu.pr_publish (run_id, tenant_id, repo_id, pr_id, provider,
         provider_pr_number, head_sha, comment_body, status, published_by, summary)
       VALUES ($1,$2,$3,$4,$5,$6,$7,'(见记录)', $8, $9, $10)`,
      [runId, tenantId, run.repo_id, run.pr_id, provider, run.provider_pr_number,
       run.head_sha, status, userId, publishSummary(body)]);
    return { ok: false, stage: 'publish_failed', reason: msg, status };
  }
}

async function finishPublished(pool, { runId, tenantId, userId, provider, prNumber, headSha,
  body, remoteCommentId, remoteUrl }) {
  // 与 publishRun 相同的归一化：pool 参数可为 Pool 实例或注入的查询函数。
  const q = (typeof pool === 'function') ? pool : (text, params) => pool.query(text, params);
  // 幂等记录：同 run 已有记录则更新（不重复建行）
  const ex = (await q(
    `SELECT publish_id FROM mu.pr_publish WHERE run_id=$1 AND status='published' LIMIT 1`,
    [runId])).rows[0];
  if (ex) {
    await q(
      `UPDATE mu.pr_publish SET remote_comment_id=$2, remote_url=$3,
         comment_body=$4, updated_at=now() WHERE publish_id=$1`,
      [ex.publish_id, remoteCommentId, remoteUrl, body]);
    return { ok: true, status: 'published', publishId: ex.publish_id,
      remoteCommentId, remoteUrl, updated: true };
  }
  const r = await q(
    `INSERT INTO mu.pr_publish (run_id, tenant_id, repo_id, pr_id, provider,
       provider_pr_number, head_sha, comment_body, remote_comment_id, remote_url,
       status, published_by, summary)
     SELECT run_id, tenant_id, repo_id, pr_id, $2, $3, $4, $5, $6, $7, 'published', $8, $9
       FROM mu.review_run WHERE run_id=$1 RETURNING publish_id`,
    [runId, provider, prNumber, headSha, body, remoteCommentId, remoteUrl, userId,
     publishSummary(body)]);
  return { ok: true, status: 'published', publishId: r.rows[0].publish_id,
    remoteCommentId, remoteUrl, updated: false };
}

export { MARKER as publishMarkerFor };
