// console/backend/lib/multiuser/reviewer-rules.mjs — Wave 3 PR-B：deterministic Reviewer。
// 纯函数：unified diff + PR 元数据 → 结构化 findings（无 LLM、无网络、无副作用）。
//
// 八类规则（任务书 PR-B §5）：secret 形状 / SQL 拼接 / shell 拼接 / 路径穿越 /
// 不安全反序列化 / workflow 权限变化 / 大文件·二进制 / 依赖 manifest 风险。
// evidence 纪律：行内容脱敏（命中片段打码，最多 60 字符）；不保存完整源码/diff。
export const RULES_VERSION = 'det-v1';

const maskLine = (s) => {
  let t = String(s).trim();
  if (t.length > 60) t = `${t.slice(0, 57)}…`;
  return t
    .replace(/(ghp_|gho_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]{10,}/g, '$1********')
    .replace(/AKIA[0-9A-Z]{12,}/g, 'AKIA********')
    .replace(/sk-[A-Za-z0-9]{12,}/g, 'sk-********')
    .replace(/(-----BEGIN [A-Z ]*PRIVATE KEY-----)/g, '-----BEGIN *** KEY-----');
};

// 解析 unified diff：per-file added-line 编号（新文件行号）
export function parseDiff(diff) {
  const files = [];
  let cur = null;
  let newLine = 0;
  for (const raw of String(diff ?? '').split('\n')) {
    const m = raw.match(/^diff --git a\/(.+) b\/(.+)$/);
    if (m) { cur = { path: m[2], added: [], isBinary: false, addedBytes: 0 }; files.push(cur); continue; }
    if (!cur) continue;
    if (raw.startsWith('Binary files') || raw.startsWith('GIT binary patch')) { cur.isBinary = true; continue; }
    const hm = raw.match(/^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
    if (hm) { newLine = Number(hm[1]); continue; }
    if (raw.startsWith('+') && !raw.startsWith('+++')) {
      cur.added.push({ line: newLine, text: raw.slice(1) });
      cur.addedBytes += Buffer.byteLength(raw);
      newLine++;
    } else if (raw.startsWith('-') && !raw.startsWith('---')) { /* 删除行不改新行号 */ }
    else newLine++;
  }
  return files;
}

const RULES = [
  { id: 'R-SECRET', severity: 'P0', title: '疑似凭据/密钥形状',
    patterns: [/ghp_[A-Za-z0-9]{30,}/, /gho_[A-Za-z0-9]{30,}/, /github_pat_[A-Za-z0-9_]{20,}/,
      /AKIA[0-9A-Z]{16}/, /sk-[A-Za-z0-9]{32,}/, /-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----/,
      /password\s*[:=]\s*["'][^"'\s]{8,}["']/i, /api[_-]?key\s*[:=]\s*["'][^"'\s]{12,}["']/i],
    remediation: '凭据移入 secret manager/环境变量；已泄露的立即轮换' },
  { id: 'R-SQL-CONCAT', severity: 'P1', title: 'SQL 字符串拼接（注入风险）',
    patterns: [/["'`]\s*(SELECT|INSERT|UPDATE|DELETE)\b[^"'`]*["'`]\s*\+\s*\w/i,
      /f["'][^"']*\b(SELECT|INSERT|UPDATE|DELETE)\b[^"']*\{/i,
      /`[^`]*\$\{[^}]+\}[^`]*\b(SELECT|INSERT|UPDATE|DELETE)\b/i,
      /(SELECT|INSERT|UPDATE|DELETE)\b[^;'"]*%\s*\(/i],
    remediation: '改用参数化查询/预编译语句' },
  { id: 'R-SHELL-CONCAT', severity: 'P1', title: 'shell 命令拼接（注入风险）',
    patterns: [/(execSync|exec|spawnSync|os\.system|subprocess\.\w+)\s*\([^)]*["'`][^"'`]*["'`]\s*\+/,
      /(exec|spawn)\s*\(\s*[`'"][^`'"]*\$\{/],
    remediation: '改用数组参数形式的子进程调用，杜绝字符串拼接 shell' },
  { id: 'R-PATH-TRAVERSAL', severity: 'P1', title: '路径穿越（../ 序列）',
    patterns: [/path\.join\([^)]*['"]\.\.\/\.\.\//, /open\(\s*['"]\.\.\/\.\.\//, /\.\.\/\.\.\/\.\.\//],
    remediation: '校验并规范化路径；限制在授权目录内' },
  { id: 'R-DESERIALIZE', severity: 'P1', title: '不安全反序列化/动态执行',
    patterns: [/pickle\.loads\(/, /yaml\.load\((?!.*Loader\s*=\s*yaml\.SafeLoader)/, /\beval\(/, /new Function\(/, /\bFunction\(\s*["']return/],
    remediation: 'pickle→json；yaml 用 safe_load；禁用 eval/Function' },
  { id: 'R-WORKFLOW-PERM', severity: 'P1', title: 'workflow 权限扩大',
    pathFilter: /^\.github\/workflows\//, lineOnly: true,
    patterns: [/permissions\s*:\s*write-all/, /^\s*permissions\s*:\s*(write|all)\s*$/],
    remediation: '最小权限：只读权限或按 job 细分' },
  { id: 'R-DEP-RISK', severity: 'P2', title: '依赖 manifest 高风险变化',
    pathFilter: /(package\.json|requirements\.txt|package-lock\.json|pnpm-lock\.yaml)$/, lineOnly: true,
    patterns: [/curl[^|]*\|\s*(ba)?sh/, /wget[^|]*\|\s*(ba)?sh/, /"http:\/\/[^"]+"/, /postinstall"\s*:\s*"[^"]*(curl|wget|http)/],
    remediation: '脚本不走网络管道；依赖用 https 且校验来源' },
];

const CONF = { P0: 0.9, P1: 0.8, P2: 0.7, P3: 0.6 };

/** 规则审查：diff+meta → findings[]（每条含脱敏摘要；head_sha 由调用方补）。 */
export function reviewDiff(diff, meta = {}) {
  const findings = [];
  const files = parseDiff(diff);
  for (const f of files) {
    if (f.isBinary) {
      findings.push({ rule_id: 'R-LARGE-FILE', severity: 'P3', confidence: 0.9, path: f.path,
        line_start: null, line_end: null, title: '二进制文件（不可审查）',
        evidence_ref: `diff:${f.path}#binary`, remediation: '确认是否必须引入二进制；与 manifest 对账' });
      continue;
    }
    if (f.addedBytes > 512 * 1024) {
      findings.push({ rule_id: 'R-LARGE-FILE', severity: 'P2', confidence: 0.95, path: f.path,
        line_start: f.added[0]?.line ?? null, line_end: f.added[f.added.length - 1]?.line ?? null,
        title: `超大文件改动（新增约 ${Math.round(f.addedBytes / 1024)}KiB）`,
        evidence_ref: `diff:${f.path}#oversize`, remediation: '拆分改动或生成文件改由构建产出' });
    }
    if (f.added.length > 2000) {
      findings.push({ rule_id: 'R-LARGE-FILE', severity: 'P3', confidence: 0.8, path: f.path,
        line_start: f.added[0]?.line ?? null, line_end: f.added[f.added.length - 1]?.line ?? null,
        title: `单文件新增 ${f.added.length} 行`, evidence_ref: `diff:${f.path}#toomanylines`,
        remediation: '拆分 PR，便于审查' });
    }
    for (const rule of RULES) {
      if (rule.pathFilter && !rule.pathFilter.test(f.path)) continue;
      for (const { line, text } of f.added) {
        const hit = rule.patterns.find((p) => {
          try { return p.test(text); } catch { return false; }
        });
        if (hit) {
          findings.push({ rule_id: rule.id, severity: rule.severity, confidence: CONF[rule.severity],
            path: f.path, line_start: line, line_end: line,
            title: rule.title, evidence_ref: `diff:${f.path}#L${line}`,
            remediation: rule.remediation, summary_masked: maskLine(text) });
          break; // 每规则每文件取首个命中（避免噪声刷屏）
        }
      }
    }
  }
  // 全局上限：diff 超限
  if (meta.over_diff_limit) {
    findings.push({ rule_id: 'R-LARGE-FILE', severity: 'P2', confidence: 1.0, path: '*',
      line_start: null, line_end: null, title: 'diff 超过 1MiB 上限',
      evidence_ref: 'diff:#over-limit', remediation: '拆分 PR 后重新审查' });
  }
  return findings.slice(0, 500);
}
