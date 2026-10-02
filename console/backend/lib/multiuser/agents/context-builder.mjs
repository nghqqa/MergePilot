// console/backend/lib/multiuser/agents/context-builder.mjs — ADR-002 PR B：Context Builder。
//
// 职责：为 external_api/local 模式构建受控代码上下文（唯一出站输入构造点）。
// 安全契约（ADR-002 §4）：
//  * 四元组绑定：tenant/repo/PR number/head SHA + diff digest；
//  * 默认只读变更文件；关联文件仅按 finding 命中符号追加（模型无权扩大范围——
//    buildContext 是一次性纯构造，无工具回调）；
//  * 排除：二进制/vendor/生成文件/lockfile/凭据路径（硬编码族 + policy denylist）；
//  * 双通道 redaction：正则族 + 熵检测；redaction 管道异常 fail-closed（不降级）；
//  * 上限：文件数/每文件行数/总字节/估算 token（policy context_budget）；
//  * PR 内容（diff/路径/注释/README/测试文本）一律置于 untrusted 数据区——
//    envelope 的指令区只含固定协议文本，注入文本无法改变系统策略；
//  * 审计只存 manifest+digest——不保存额外原始代码副本（DB/日志/重试队列）。
import crypto from 'node:crypto';

export const CONTEXT_DEFAULTS = Object.freeze({
  max_files: 5,
  max_lines_per_file: 200,
  max_total_bytes: 24 * 1024,
  max_tokens_est: 8192,
  context_lines: 30, // finding 行 ±30
});

// 凭据/生成物排除（硬编码族——denylist 之上再加一层）
const EXCLUDE_PARTS = [
  /(^|\/)(node_modules|vendor|dist|build|out|\.next|target)\//i,
  /(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Cargo\.lock|go\.sum|Gemfile\.lock|composer\.lock)$/i,
  /\.(env|pem|key|p12|pfx|crt|keystore)$/i,
  /(secret|credential|password|token|apikey|api_key)/i,
];
const isExcludedPath = (p) => EXCLUDE_PARTS.some((re) => re.test(p));
const BINARY_EXT_RE = /\.(png|jpe?g|gif|bmp|ico|webp|woff2?|ttf|eot|mp4|zip|tar|gz|jar|class|so|dylib|dll|exe|wasm|pdf)$/i;

// secret 正则族（发送前 redaction——与 reviewer-rules 同族+扩展）
const SECRET_RES = [
  /(ghp_[A-Za-z0-9]{20,})/g, /(gho_[A-Za-z0-9]{20,})/g, /(ghs_[A-Za-z0-9]{20,})/g,
  /(github_pat_[A-Za-z0-9_]{20,})/g,
  /(sk-[A-Za-z0-9]{16,})/g, /(xox[baprs]-[A-Za-z0-9-]{10,})/g,
  /(AKIA[0-9A-Z]{12,})/g,
  /(-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (RSA |EC |OPENSSH )?PRIVATE KEY-----)/g,
  /(Bearer\s+[A-Za-z0-9._-]{16,})/gi,
  /(password\s*[=:]\s*['"][^'"\s]{6,}['"])/gi,
];
const REDACTED = '[REDACTED]';

/** 熵检测：随机 token（≥20 chars， Shannon > 3.5 且无空格）。 */
function shannonEntropy(s) {
  const freq = {};
  for (const ch of s) freq[ch] = (freq[ch] ?? 0) + 1;
  let h = 0;
  for (const c of Object.values(freq)) {
    const p = c / s.length;
    h -= p * Math.log2(p);
  }
  return h;
}
export function highEntropyTokens(line) {
  const out = [];
  const re = /[A-Za-z0-9_\-+/=]{20,}/g;
  let m;
  while ((m = re.exec(line)) !== null) {
    const tok = m[0];
    if (/[A-Za-z0-9+/=]{20,}/.test(tok) && shannonEntropy(tok) > 3.5 && !/^(https?:\/\/|[0-9a-f]{40,})/i.test(tok)) {
      out.push(tok);
    }
  }
  return out;
}

/** 单行双通道 redaction：正则族 + 熵 token。返回 {line, redactions}；失败抛错（fail-closed）。 */
export function redactLine(line) {
  if (typeof line !== 'string') throw new Error('redact_line_type_invalid');
  let out = line;
  let n = 0;
  for (const re of SECRET_RES) {
    const before = out;
    out = out.replace(re, () => { n++; return REDACTED; });
    if (typeof out !== 'string') throw new Error('redact_regex_malformed');
  }
  for (const tok of highEntropyTokens(out)) {
    out = out.split(tok).join(REDACTED);
    n++;
  }
  return { line: out, redactions: n };
}

/** 不可信文本进入数据区的包裹（注入防线的结构层：内容永远不进入指令区）。 */
export function wrapUntrusted(text) {
  // 中和潜在 fence 逃逸：统一归一化内层 fence 标记
  const safe = String(text).replace(/```+/g, '``\u200b`');
  return '<<<UNTRUSTED_DATA\n' + safe + '\nUNTRUSTED_DATA>>>';
}

/** Provider request envelope（指令区固定协议；数据区=包裹后的文件内容）。 */
export function buildEnvelope({ files, instruction }) {
  const dataBlocks = files.map((f) =>
    `# FILE: ${f.path} (lines ${f.line_start}-${f.line_end})\n${wrapUntrusted(f.content)}`).join('\n\n');
  return {
    // 指令区（固定——PR 内容不可能到达这里）
    instruction: String(instruction ?? 'Review the code context for security issues. Output JSON findings only.'),
    // 数据区
    data: dataBlocks,
    serialize() {
      return `${this.instruction}\n\n${this.data}`;
    },
  };
}

const digest = (s) => crypto.createHash('sha256').update(s).digest('hex');

/**
 * 主构造函数。输入：
 *  ctx = { tenantId, repoId, prNumber, headSha, diffText, files: Map<path, {content, lines?}>,
 *          findings: [{path, line_start}], policy: {context_budget, file_allowlist, file_denylist} }
 * 输出：{ ok, context: {files[], envelope, manifest}, or { ok:false, code }
 * 纯函数——不做任何 IO；文件内容由调用方取（真实取数在 PR C 的 provider 适配层）。
 */
export function buildContext(ctx) {
  if (!ctx?.tenantId || !ctx?.repoId || !ctx?.prNumber || !ctx?.headSha || typeof ctx.diffText !== 'string') {
    return { ok: false, code: 'CTX_BINDING_INVALID' };
  }
  const budget = { ...CONTEXT_DEFAULTS, ...(ctx.policy?.context_budget ?? {}) };
  const allow = new Set(ctx.policy?.file_allowlist ?? []);
  const deny = ctx.policy?.file_denylist ?? [];

  // 变更文件集合（从 diff 解析）+ finding 命中文件
  const changed = parseChangedPaths(ctx.diffText);
  const findingPaths = [...new Set((ctx.findings ?? []).map((f) => String(f.path)).filter(Boolean))];
  let candidates = [...new Set([...changed, ...findingPaths])];

  // 过滤：denylist > allowlist > 硬编码排除
  candidates = candidates.filter((p) => {
    if (deny.some((d) => p.includes(d))) return false;
    if (allow.size > 0 && !allow.has(p)) return false;
    if (isExcludedPath(p) || BINARY_EXT_RE.test(p)) return false;
    return true;
  });
  candidates = candidates.slice(0, budget.max_files);

  const files = [];
  let totalBytes = 0, redactions = 0;
  for (const p of candidates) {
    const f = ctx.files?.[p];
    if (!f || typeof f.content !== 'string') continue; // 文件不可得=跳过（manifest 如实记录）
    // 行截取：finding 命中行 ±context_lines；无 finding 的变更文件取头部
    const lines = f.content.split('\n');
    const hit = (ctx.findings ?? []).find((x) => String(x.path) === p && x.line_start);
    let start = 0, end = Math.min(lines.length, budget.max_lines_per_file);
    if (hit?.line_start) {
      start = Math.max(0, Number(hit.line_start) - 1 - budget.context_lines);
      end = Math.min(lines.length, Number(hit.line_start) - 1 + budget.context_lines + 1, start + budget.max_lines_per_file);
    }
    let content = '';
    for (let i = start; i < end; i++) {
      let red;
      try { red = redactLine(lines[i]); }
      catch (e) { return { ok: false, code: 'CTX_REDACTION_FAILED', path: p }; } // fail-closed
      content += red.line + '\n';
      redactions += red.redactions;
    }
    const bytes = Buffer.byteLength(content, 'utf8');
    if (totalBytes + bytes > budget.max_total_bytes) break; // 超总预算=停（不截半文件）
    totalBytes += bytes;
    files.push({ path: p, line_start: start + 1, line_end: end, content, bytes });
  }

  const envelope = buildEnvelope({ files, instruction: ctx.instruction });
  const manifest = {
    files: files.map((f) => f.path),
    lines_total: files.reduce((n, f) => n + (f.line_end - f.line_start + 1), 0),
    bytes_total: totalBytes,
    tokens_est: Math.ceil(totalBytes / 3), // 粗估（1 token≈3B）——精确计数在 adapter
    redactions_applied: redactions,
    excluded: candidates.length - files.length,
    budget,
  };
  const diff_digest = digest(ctx.diffText).slice(0, 32);
  const context_digest = digest(JSON.stringify(manifest) + files.map((f) => digest(f.content)).join('')).slice(0, 32);
  // input_digest 必须绑定四元组：仅 envelope 内容不足以区分 head（B6 实证）——
  // 新 head 同内容 diff 时重试不得复用旧 digest（ADR §7 幂等键语义）
  const binding_str = JSON.stringify({ t: String(ctx.tenantId), r: String(ctx.repoId),
    p: Number(ctx.prNumber), h: String(ctx.headSha), d: diff_digest });
  const input_digest = digest(binding_str + envelope.serialize()).slice(0, 32);
  return {
    ok: true,
    context: {
      files, envelope, manifest,
      binding: { tenant_id: String(ctx.tenantId), repo_id: String(ctx.repoId),
        pr_number: Number(ctx.prNumber), head_sha: String(ctx.headSha), diff_digest },
      context_digest, input_digest,
      // 幂等键：同 binding+context → 同 digest（重试复用；adapter 层强制）
      idempotency_key: `${ctx.repoId}:${ctx.prNumber}:${String(ctx.headSha).slice(0, 12)}:${context_digest}`,
    },
  };
}

/** diff → 变更路径列表（新增/修改；删除文件不进上下文）。 */
export function parseChangedPaths(diffText) {
  const out = [];
  let cur = null;
  for (const line of String(diffText ?? '').split('\n')) {
    const m = line.match(/^diff --git a\/(.+) b\/(.+)$/);
    if (m) { cur = m[2]; continue; }
    if (cur && line.startsWith('new file mode')) { out.push(cur); cur = null; continue; }
    if (cur && line.startsWith('@@')) { out.push(cur); cur = null; }
  }
  return [...new Set(out)];
}
