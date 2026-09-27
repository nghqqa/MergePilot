// console/backend/lib/multiuser/provider.mjs — MU 身份/安装/执行 provider 层（Developer 切片）。
//
// 身份边界合同（对齐设计报告 §6 + Phase 0 复核）：
//  * 两条独立流程：用户身份（OAuth）与仓库安装（GitHub App installation）——互不混用；
//  * 本切片只实现 FixtureProvider（合成身份/合成 installation/合成执行），
//    真实 GitHub OAuth/App 接入为 PR 未完成项——github 状态永远如实 not_configured；
//  * fixture 执行器只产生审计与合成记录，绝不触达真实 GitHub（无网络调用）；
//  * 自动 approve 与绕过 branch protection 的合并不存在实现路径（结构性禁止）。
import crypto from 'node:crypto';

export function githubOAuthStatus(_env = process.env) {
  // 真实 GitHub OAuth 未接入——fail-closed 如实声明（不伪装可配置）
  return { configured: false, reason: 'github_oauth_not_wired',
    note: 'Developer Edition 切片仅 fixture 身份提供商；GitHub OAuth 流程位保留' };
}

export function githubAppStatus(_env = process.env) {
  return { configured: false, reason: 'github_app_not_wired',
    note: '仓库安装当前仅合成 installation（fixture）；真实 GitHub App 接入为未完成项' };
}

// ── fixture：合成 installation（绑定/测试用） ──
export function fixtureInstallationId(seed) {
  return `fixture-install-${crypto.createHash('sha256').update(String(seed)).digest('hex').slice(0, 12)}`;
}

// ── fixture：变更摘录（read_code_content 动作的数据面——合成文本，零真实代码） ──
export function fixtureChangedExcerpt(pr) {
  return [
    `# 合成变更摘录（fixture）——非真实代码`,
    `repo: ${pr.repo_owner}/${pr.repo_name}  PR #${pr.provider_pr_number}  head: ${pr.head_sha?.slice(0, 12)}`,
    '```diff',
    `- legacy_call(args...)`,
    `+ hardened_call(args, { verify: true })  # fixture line ${pr.head_sha?.slice(0, 6)}`,
    '```',
  ].join('\n');
}

// ── fixture：RAG 检索（rag_query 动作的数据面——确定性词法命中，零真实语料） ──
export function fixtureRagSearch(repo, queryText) {
  const terms = String(queryText || '').toLowerCase().split(/\s+/).filter(Boolean);
  const corpus = [
    { doc: 'docs/security-policy.md', text: '访问控制 默认拒绝 fail-closed 最小权限 审计' },
    { doc: 'docs/rollback-runbook.md', text: '回滚 演练 锚点 digest 双闸 branch protection' },
    { doc: 'docs/api-contract.md', text: '契约 reason 机器码 403 404 不泄露存在性' },
  ];
  const hits = corpus
    .map((c) => ({ ...c, score: terms.filter((t) => c.text.toLowerCase().includes(t)).length }))
    .filter((h) => h.score > 0)
    .sort((a, b) => b.score - a.score);
  return {
    provider: 'fixture-lexical', repo: `${repo.owner}/${repo.name}`,
    note: '合成语料检索（fixture）——仅用于授权边界验证，非真实 RAG',
    results: hits.map((h) => ({ doc_path: h.doc, score: h.score, excerpt: h.text.slice(0, 60) })),
  };
}

// ── fixture：只读审查执行（review_run job——确定性结论，零 GitHub 访问） ──
export function fixtureReviewRun(pr) {
  const digest = crypto.createHash('sha256')
    .update([pr.tenant_id, pr.repo_id, pr.provider_pr_number, pr.head_sha].join('|')).digest('hex');
  return {
    provider: 'fixture-reviewer',
    summary: 'fixture 只读审查：合成结论（真实 AI 审查接入为未完成项）',
    findings_count: digest[0] % 3,
    reference_sha256: digest,
    usage: 'reference_only',
  };
}

// ── fixture：受控修复执行（repair_push job——合成"写入"，零真实 GitHub 写） ──
export function fixtureRepairPush(pr, job) {
  return {
    provider: 'fixture-executor',
    wrote: true,
    ref: `refs/heads/fix/fixture-${String(job.job_id).slice(0, 8)}`,
    note: '合成执行记录（fixture）——绝不触达真实 GitHub；branch protection 不可绕过（无实现路径）',
  };
}
