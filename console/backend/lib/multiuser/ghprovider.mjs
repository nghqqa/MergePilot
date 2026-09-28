// console/backend/lib/multiuser/ghprovider.mjs — Wave 3 PR-B：GitHub PR 上下文只读拉取。
//
// 安全契约（任务书 PR-B）：
//  * installation token 仅存在于本模块调用栈内存——不入库、不入日志、不入审计、
//    不进入 finding 正文、不作为返回值（返回体只有 PR 元数据/diff/checks/protection）；
//  * 上限：diff ≤ 1MiB、文件数 ≤ 300、单文件新增 ≤ 512KiB（超限按 R-LARGE 由
//    规则层报告，不截断成“看似完整”）；
//  * TOCTOU：expectedHeadSha 给定时，PR 当前 head 不匹配 → { stale_head: true }，
//    调用方必须放弃本次 attempt（旧 head 结果不得写入新 head 的 run）；
//  * provider 可注入（真实实现 fetch / 测试 mock）——与 ghapp.mjs 同一纪律。
import { ghAppConfig } from './ghapp.mjs';
import { createAppJwt } from './ghapp.mjs';

export const GH_LIMITS = Object.freeze({
  maxDiffBytes: 1024 * 1024,
  maxFiles: 300,
  maxFileBytes: 512 * 1024,
});

async function installationToken(cfg, installationId, fetchImpl) {
  const jwt = createAppJwt(cfg.appId, process.env.MU_GITHUB_APP_PRIVATE_KEY);
  const res = await fetchImpl(`https://api.github.com/app/installations/${installationId}/access_tokens`, {
    method: 'POST', headers: { authorization: `Bearer ${jwt}`, accept: 'application/vnd.github+json' },
  });
  if (!res.ok) throw new Error(`ghp_token_http_${res.status}`);
  const body = await res.json().catch(() => ({}));
  if (!body.token) throw new Error('ghp_token_failed');
  return body.token; // 内存瞬时：仅本函数调用栈
}

const realProvider = {
  async fetchPrContext(cfg, { installationId, owner, repo, prNumber, expectedHeadSha = null }) {
    const f = cfg.fetchImpl ?? fetch;
    const token = await installationToken(cfg, installationId, f); // 不外传
    const gh = (path, headers = {}) => f(`https://api.github.com${path}`, {
      headers: { authorization: `Bearer ${token}`, accept: 'application/vnd.github+json', ...headers },
    });
    const prRes = await gh(`/repos/${owner}/${repo}/pulls/${prNumber}`);
    if (!prRes.ok) throw new Error(`ghp_pr_http_${prRes.status}`);
    const pr = await prRes.json();
    const headSha = String(pr.head?.sha ?? '');
    if (expectedHeadSha && headSha !== expectedHeadSha) {
      return { stale_head: true, fetched_head_sha: headSha, expected_head_sha: expectedHeadSha };
    }
    // unified diff（v3.diff 单请求；上限校验后返回原文——由规则层判定超大）
    const diffRes = await gh(`/repos/${owner}/${repo}/pulls/${prNumber}`,
      { accept: 'application/vnd.github.v3.diff' });
    if (!diffRes.ok) throw new Error(`ghp_diff_http_${diffRes.status}`);
    const diff = await diffRes.text();
    const files = (pr.changed_files ?? 0);
    // checks（head commit）
    let checks = [];
    try {
      const cr = await gh(`/repos/${owner}/${repo}/commits/${headSha}/check-runs?per_page=100`,
        { accept: 'application/vnd.github+json' });
      if (cr.ok) checks = (await cr.json()).check_runs?.map((c) => ({
        name: c.name, conclusion: c.conclusion, status: c.status })) ?? [];
    } catch { /* checks 缺失不阻断审查（fail-closed 由规则层标注） */ }
    // branch protection（base 分支；404=未配置保护）
    let protection = { configured: false };
    try {
      const bp = await gh(`/repos/${owner}/${repo}/branches/${pr.base?.ref}/protection`);
      if (bp.ok) {
        const j = await bp.json();
        protection = { configured: true,
          required_checks: (j.required_status_checks?.contexts ?? j.required_status_checks?.checks ?? []).length,
          required_reviews: j.required_pull_request_reviews?.required_approving_review_count ?? 0,
          enforce_admins: Boolean(j.enforce_admins?.enabled) };
      }
    } catch { /* 未配置/无权限 → configured:false（如实） */ }
    return {
      stale_head: false,
      pr: { number: Number(pr.number), state: String(pr.state), title: String(pr.title ?? ''),
        head: { sha: headSha, ref: String(pr.head?.ref ?? '') },
        base: { sha: String(pr.base?.sha ?? ''), ref: String(pr.base?.ref ?? '') },
        changed_files: Number(files) },
      diff, checks, protection,
      limits: { diff_bytes: Buffer.byteLength(diff), over_diff_limit: Buffer.byteLength(diff) > GH_LIMITS.maxDiffBytes,
        over_file_limit: files > GH_LIMITS.maxFiles },
      fetched_head_sha: headSha,
    };
  },
};

let providerImpl = realProvider;
export function __setGhProviderForTests(impl) { providerImpl = impl; }
export function __resetGhProvider() { providerImpl = realProvider; }
export function fetchPrContext(cfg, args) { return providerImpl.fetchPrContext(cfg, args); }
export function ghProviderRealConfigured() { return ghAppConfig().configured; }
