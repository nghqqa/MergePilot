// console/backend/lib/multiuser/artifacts.mjs — 审查产物交付层（2026-10-13 波）。
//
// 职责：
//  * 四角色链原始输出持久化（mu.review_output）——模型实际输出全文，不静默丢；
//  * 建议补丁：从 fixer 输出提取统一 diff → 结构/路径/目标版本检查 → mu.fix_patch；
//  * 报告构造：Markdown / 结构化 JSON（下载物与服务端保存同源）；
//  * PR 发布评论构造 + forge 评论 upsert（Gitee 实测；GitHub 走 App installation 合同实现）。
//
// 纪律：
//  * 模型输出按不可信文本处理：入库前不解释、出站前不额外美化；
//  * 路径白名单：补丁只允许触及变更文件集内的相对路径，越界=不可应用；
//  * 发布幂等：每 (run,head) 至多一条已发布评论（marker 定位，更新不新建）；
//  * 凭据/秘密永不进入评论、日志、下载物（finding 级脱敏沿用 summary_masked）。

import crypto from 'node:crypto';

/** 每 run 的发布评论 marker——远端定位自己人评论的唯一依据（不依赖用户名猜测）。 */
export function publishMarker(runId, headSha) {
  return `<!-- mergepilot-review:${runId}:${String(headSha).slice(0, 12)} -->`;
}

/** 统一 diff 结构解析：提取触及文件与增删行数；解析失败返回 {ok:false}（不静默吞）。 */
export function parseUnifiedDiff(diffText) {
  const text = String(diffText ?? '');
  if (!text.trim()) return { ok: false, reason: 'empty_diff' };
  const lines = text.split('\n');
  const files = [];
  let cur = null;
  let sawHunk = false;
  for (const line of lines) {
    if (line.startsWith('--- ')) {
      cur = { oldPath: line.slice(4).trim(), additions: 0, deletions: 0 };
      continue;
    }
    if (line.startsWith('+++ ')) {
      if (!cur) return { ok: false, reason: 'plus_before_minus' };
      cur.path = line.slice(4).trim().replace(/^b\//, '');
      if (cur.oldPath.startsWith('a/')) cur.oldPath = cur.oldPath.slice(2);
      files.push(cur);
      continue;
    }
    if (line.startsWith('@@')) { sawHunk = true; continue; }
    if (cur && line.startsWith('+')) cur.additions += 1;
    if (cur && line.startsWith('-')) cur.deletions += 1;
  }
  if (!files.length || !sawHunk) return { ok: false, reason: 'not_unified_diff' };
  for (const f of files) {
    if (!f.path || f.path.includes('..') || f.path.startsWith('/')) {
      return { ok: false, reason: 'unsafe_path', file: f.path ?? null };
    }
  }
  return { ok: true, files: files.map((f) => ({ path: f.path, additions: f.additions, deletions: f.deletions })) };
}

/**
 * 可应用性检查（结构级）：diff 格式合法 + 触及文件 ⊆ 该 head 的变更文件集 + 无越界路径。
 * 说明：这是结构级检查，不执行真实 git apply——applicability.checks 如实标注检查方式。
 */
export function checkPatchApplicability(diffText, changedFiles) {
  const parsed = parseUnifiedDiff(diffText);
  if (!parsed.ok) return { ok: false, parsed: null, issues: [parsed.reason], checks: ['format'] };
  const changedSet = new Set((changedFiles ?? []).map((f) => String(f.path ?? f)));
  const issues = [];
  for (const f of parsed.files) {
    if (changedSet.size && !changedSet.has(f.path)) {
      issues.push(`path_not_in_change_set:${f.path}`);
    }
  }
  return { ok: issues.length === 0, parsed, issues,
    checks: ['format', 'path_scope', 'relative_paths'] };
}

/** diff 文件 sha256（hex）——检查证据与展示按 digest 绑定：补丁更新→旧证据失效。 */
export function sha256Hex(s) {
  return crypto.createHash('sha256').update(String(s ?? ''), 'utf8').digest('hex');
}

/**
 * 唯一的确定性格式修正：缺尾换行（git apply 对无尾换行的补丁报 corrupt patch——
 * 2026-10-14 生产 f56e3137 实证成因）。仅在"文本像完整 diff 且确实缺尾换行"时返回修正；
 * 其余一切格式问题（坏 hunk/上下文不匹配/编码等）返回 null——交回模型再生成，不猜测。
 */
export function deterministicNormalize(diffText) {
  const t = String(diffText ?? '');
  if (!t.trim() || t.endsWith('\n')) return null;
  if (!/^--- |^diff --git /m.test(t) || !t.includes('@@')) return null;
  return { diff: t + '\n', kind: 'add_trailing_newline' };
}

/**
 * 真实可应用性权威判定（单一事实源——页面/报告/评论共用同一口径）：
 * 结构检查通过 ≠ 可应用；git_apply 证据按 digest 绑定；证据缺失/环境不可用如实分列。
 * 返回 { level: pass|fail|not_executed|env_unavailable|stale, structural, text }。
 */
export function applicabilityVerdict(applicability, currentDigest = null) {
  const a = applicability ?? null;
  const structural = { ok: a?.ok ?? null, issues: a?.issues ?? [], checks: a?.checks ?? [] };
  const ga = a?.git_apply ?? null;
  let level = null;
  let text = '';
  if (ga && currentDigest && ga.patch_digest && ga.patch_digest !== currentDigest) {
    level = 'stale';
    text = '检查证据已失效：补丁内容已更新（当前 digest 与检查时不一致），旧检查结果不再代表当前补丁';
  } else if (!ga) {
    level = 'not_executed';
    text = '未执行 git apply --check（结构检查通过 ≠ 可应用——以本行如实口径为准）';
  } else if (ga.verified === false) {
    level = 'env_unavailable';
    text = `未验证：环境不可用（${ga.reason ?? '未知原因'}）——不误报通过`;
  } else if (ga.ok) {
    level = 'pass';
    const at = ga.checked_at ? `，${String(ga.checked_at).slice(0, 19).replace('T', ' ')} UTC` : '';
    text = `通过（git apply --check 对准确 head${at}）`;
  } else {
    level = 'fail';
    text = `失败——补丁不可应用（${ga.output ?? 'git apply --check 未通过'}）`;
  }
  return { level, text, structural, git_apply: ga };
}

/** 四角色链输出持久化（幂等：UNIQUE(run,role,attempt) 冲突即更新内容）。 */
export async function persistChainOutputs(pool, { runId, tenantId, results, attempt = 1 }) {
  const roles = ['reviewer', 'leader', 'fixer', 'verifier'];
  for (const role of roles) {
    const content = results?.[role];
    if (content == null) continue;
    await pool.query(
      `INSERT INTO mu.review_output (run_id, tenant_id, agent_role, attempt, content)
       VALUES ($1,$2,$3,$4,$5)
       ON CONFLICT (run_id, agent_role, attempt)
       DO UPDATE SET content = EXCLUDED.content`,
      [runId, tenantId, role, attempt, JSON.stringify(content)]);
  }
}

/**
 * 建议补丁持久化（版本化）：每次调用=新版本行（原始 fixer 输出永远保留，修正不覆盖历史）。
 *  * patch_version = 同 run 内 MAX+1；新行 active=true，旧行一并失活；
 *  * patch_digest = sha256(diff_text)——检查证据/展示/下载按此绑定；
 *  * correction_of / correction_kind 记录修正来源（deterministic|regenerated|null=原始）。
 * 结构检查通过或带 issues 均如实保存（checks 字段承载检查方式；真实可应用性另存 git_apply）。
 */
export async function persistFixPatch(pool, { runId, tenantId, repoId, headSha,
  findingId = null, diffText, rationale = null, changedFiles = [],
  correctionOf = null, correctionKind = null }) {
  const applicability = checkPatchApplicability(diffText, changedFiles);
  if (!applicability.parsed) {
    return { ok: false, reason: applicability.issues?.[0] ?? 'invalid_diff' };
  }
  const digest = sha256Hex(diffText);
  const next = (await pool.query(
    `SELECT COALESCE(MAX(patch_version),0)+1 AS v FROM mu.fix_patch WHERE run_id=$1`, [runId]
  )).rows[0]?.v ?? 1;
  const r = await pool.query(
    `INSERT INTO mu.fix_patch (run_id, tenant_id, repo_id, head_sha, finding_id,
       files, diff_text, rationale, applicability, patch_version, patch_digest,
       active, correction_of, correction_kind)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,true,$12,$13) RETURNING patch_id, applicability`,
    [runId, tenantId, repoId, headSha, findingId,
     JSON.stringify(applicability.parsed.files), diffText, rationale,
     JSON.stringify({ ok: applicability.ok, issues: applicability.issues, checks: applicability.checks }),
     next, digest, correctionOf, correctionKind]);
  await pool.query(
    `UPDATE mu.fix_patch SET active = false WHERE run_id=$1 AND patch_id<>$2 AND active`,
    [runId, r.rows[0].patch_id]);
  return { ok: true, patchId: r.rows[0].patch_id, applicability: r.rows[0].applicability,
    patchVersion: next, patchDigest: digest };
}

/**
 * finding 展示行（解释要素分列、只用真实数据）：
 *  标题=title · 位置 · 规则 id+规则说明（静态）/模型实际摘要（模型）· 修复建议=remediation。
 *  静态发现的"原因"=规则标题（规则说明口径）；模型发现的"原因"=模型实际输出摘要；
 *  数据缺失的要素如实标（未提供），不编造。
 */
function findingLine(f) {
  const sev = String(f.severity ?? '?').toUpperCase();
  const loc = f.path ? `${f.path}${f.line_start ? ':' + f.line_start : ''}` : (f.path ?? '(未知位置)');
  const out = [`- **${sev}** \`${loc}\` — ${String(f.title ?? '').trim() || String(f.summary_masked ?? '').slice(0, 160) || '(无标题)'}（规则 ${f.rule_id ?? '?'}）`];
  if (f.summary_masked && f.title) out.push(`  说明：${String(f.summary_masked).slice(0, 160)}`);
  if (f.remediation) out.push(`  修复建议：${String(f.remediation).slice(0, 200)}`);
  return out.join('\n');
}

/**
 * 补丁验证三分的用户可读行（报告/评论/页面共用同一口径——applicabilityVerdict 单一事实源）：
 *  1) 格式与路径（结构级）；2) 真实可应用性（git apply --check，digest 绑定）；
 *  3) 实际修复测试（test_verification；未执行如实标注）。
 */
export function patchVerificationLines(patch) {
  if (!patch) return [];
  const v = applicabilityVerdict(patch.applicability, patch.patch_digest ?? null);
  const lines = [];
  lines.push(`- 格式与路径检查（结构级）：${v.structural.ok ? '通过' : `未通过（${(v.structural.issues ?? []).join('；') || '未知原因'}）`}`);
  lines.push(`- 真实可应用性（对准确 head 的 git apply --check）：${v.text}`);
  const tv = patch.test_verification ?? null;
  if (tv && tv.verified && tv.ok) {
    lines.push(`- 实际修复测试：通过（修复前失败→修复后通过；命令 \`${tv.test_cmd}\`，隔离环境，无生产凭据）`);
  } else if (tv && tv.verified) {
    lines.push(`- 实际修复测试：未通过（${tv.ok === false && tv.pre?.code === 0 ? '修复前测试即通过，不能证明修复有效性' : `pre exit=${tv.pre?.code ?? '?'}/post exit=${tv.post?.code ?? '?'}`}）`);
  } else if (tv) {
    lines.push(`- 实际修复测试：未验证（环境不可用：${tv.reason ?? '未知'}）`);
  } else {
    lines.push('- 实际修复测试：未执行（模型评估/可应用性通过 ≠ 行为正确）');
  }
  return lines;
}

/**
 * Markdown 报告（下载物与服务端保存同源）。包含：
 * 审查版本/目标、风险摘要、问题清单、建议补丁说明、验证范围（含未验证项）、边界声明。
 */
export function buildMarkdownReport({ run, findings, attempts, outputs, patch, verification,
  repoLabel, prNumber, headSha }) {
  const lines = [];
  const gen = new Date().toISOString();
  lines.push(`# MergePilot 审查报告`);
  lines.push('');
  lines.push(`- 仓库/PR：${repoLabel}${prNumber != null ? `#${prNumber}` : ''}`);
  lines.push(`- 被审查 head：\`${headSha}\``);
  lines.push(`- run：\`${run.run_id}\`（状态 ${run.status}，审查引擎 ${run.llm_mode ?? 'n/a'}）`);
  lines.push(`- 生成时间：${gen}（UTC）`);
  lines.push('');
  lines.push('## 风险摘要');
  lines.push('');
  const p0 = findings.filter((f) => f.severity === 'P0').length;
  const p1 = findings.filter((f) => f.severity === 'P1').length;
  lines.push(`P0=${p0}，P1=${p1}，共 ${findings.length} 项发现。`);
  lines.push('');
  lines.push('## 问题清单');
  lines.push('');
  for (const f of findings) lines.push(findingLine(f));
  if (!findings.length) lines.push('（无发现）');
  lines.push('');
  lines.push('## 四角色链输出');
  lines.push('');
  for (const role of ['reviewer', 'leader', 'fixer', 'verifier']) {
    const o = outputs?.[role];
    lines.push(`### ${role}`);
    lines.push('');
    lines.push('```json');
    lines.push(o ? JSON.stringify(o, null, 1).slice(0, 4000) : '（无输出）');
    lines.push('```');
    lines.push('');
  }
  lines.push('## 建议补丁');
  lines.push('');
  if (patch) {
    lines.push(`- 状态：**建议补丁，尚未应用**（head \`${patch.head_sha}\`，版本 v${patch.patch_version ?? 1}${patch.patch_digest ? `，digest \`${String(patch.patch_digest).slice(0, 12)}\`` : ''}${patch.correction_kind && patch.correction_kind !== 'original' ? `，${patch.correction_kind === 'deterministic' ? '确定性格式修正' : '模型再生成'}产物` : ''}）`);
    lines.push(`- 触及文件：${(patch.files ?? []).map((f) => `\`${f.path}\``).join(', ') || '（无）'}`);
    lines.push('');
    lines.push('### 验证三分');
    lines.push('');
    for (const l of patchVerificationLines(patch)) lines.push(l);
    lines.push('');
    if (patch.rationale) { lines.push(`- 理由：${patch.rationale}`); }
    lines.push('');
    lines.push('完整统一 diff 见随附 `.patch` 文件（与保存产物同源——下载即检查所验证的同一版本）。');
  } else {
    lines.push('（本轮未产出可展示的补丁——fixer 仅给出文字建议或未输出结构化 diff。）');
  }
  lines.push('');
  lines.push('## 验证范围');
  lines.push('');
  for (const v of (verification ?? [])) lines.push(`- ${v}`);
  lines.push('');
  lines.push('## 边界声明');
  lines.push('');
  lines.push('- 本报告由 AI 审查生成：**仅建议，未修改任何代码**；不构成 required review。');
  lines.push('- "验证通过"为模型独立评估，**不代表实际测试执行**；未验证项已如实列出。');
  lines.push('- 审查基于被审查 head 的变更内容与规则引擎/模型能力，存在误报/漏报可能。');
  return lines.join('\n');
}

/**
 * PR 发布评论正文（单条可更新汇总评论）。
 * 边界：默认不含原始源码/完整补丁/敏感原值；不含不可访问链接。
 */
export function buildPublishComment({ run, findings, patch, verification,
  repoLabel, prNumber, headSha, detailUrl, reviewedAt }) {
  const gen = new Date().toISOString();
  const lines = [];
  lines.push(publishMarker(run.run_id, headSha));
  lines.push('## MergePilot AI 审查（建议，未修改代码）');
  lines.push('');
  lines.push(`被审查版本：\`${String(headSha).slice(0, 12)}\`${reviewedAt ? `（${reviewedAt} UTC 完成）` : ''} · 引擎：${run.llm_mode ?? 'n/a'} 首轮 + AgentTeams 四角色链`);
  lines.push(`最近更新：${gen}（UTC）——同一评论原位更新，此行时间随每次发布变化`);
  lines.push('');
  lines.push('### 风险摘要');
  lines.push('');
  const p0 = findings.filter((f) => f.severity === 'P0').length;
  const p1 = findings.filter((f) => f.severity === 'P1').length;
  lines.push(`P0=${p0}，P1=${p1}，共 ${findings.length} 项。`);
  lines.push('');
  lines.push('### 发现（可操作项）');
  lines.push('');
  for (const f of findings.slice(0, 10)) lines.push(findingLine(f));
  if (!findings.length) lines.push('（自动审查未发现问题——不构成人工审查替代）');
  lines.push('');
  lines.push('### 建议修改与验证范围');
  lines.push('');
  if (patch) {
    lines.push(`建议补丁已生成（**尚未应用**）：触及 ${(patch.files ?? []).map((f) => `\`${f.path}\``).join(', ')}。`);
    lines.push('');
    for (const l of patchVerificationLines(patch)) lines.push(l);
    lines.push('');
    lines.push('完整 diff 请经 Console 下载（与检查所验证的同一版本，digest 绑定）。');
  } else {
    lines.push('本轮以文字建议为主，未产出结构化补丁。');
  }
  for (const v of (verification ?? []).slice(0, 6)) lines.push(`- ${v}`);
  lines.push('');
  if (detailUrl) { lines.push(`详情：${detailUrl}`); lines.push(''); }
  lines.push('> 仅生成建议，未修改代码、不构成 required review；存在误报/漏报可能，请人工复核。');
  return lines.join('\n');
}

/** 评论正文摘要（入库存档用——脱敏后的首行摘要，不含全文）。 */
export function publishSummary(body) {
  const line = String(body ?? '').split('\n').find((l) => l.trim() && !l.startsWith('<!--')) ?? '';
  return line.slice(0, 160);
}

/** 随机 hex（远端评论 id 兜底标识等）。 */
export function randomHex(n) {
  return crypto.randomBytes(Math.ceil(n / 2)).digest('hex').slice(0, n);
}
