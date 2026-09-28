// console/backend/lib/multiuser/agents/fxv-adapter.mjs — Wave 3 PR-D：FXV 子进程接入。
//
// 复用 lib/fxv/exec/ 真子进程契约（worker-fixer/worker-verifier）——独立进程边界：
//  * Fixer（默认且唯一模式=dry-run）：stdin {repo_url, base_head_sha, finding{file,
//    pattern, replacement}, workspace} → 产出 patch_text+patch_digest；不写 GitHub、
//    不建 commit、不动原仓库；
//  * Verifier：stdin {repo_url, base_head_sha, patch_text, patch_digest, test_cmd,
//    workspace} → 独立 fresh fetch@head + digest 复核 + apply + harness → verdict；
//    只收 artifact，绝不信任 Fixer 自述；
//  * 禁改区：finding 落在 .github/workflows/**、权限/保护/deployment 配置的文件
//    → fixer 拒绝（SKIPPED_FORBIDDEN_ZONE），绝不生成此类 patch；
//  * 全部结果绑定五元组由调用方（fix-orchestrator）落库；本层零 DB 依赖。
import { spawn } from 'node:child_process';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FXV = path.resolve(HERE, '../../fxv/exec');
const FIXER = path.join(FXV, 'worker-fixer.mjs');
const VERIFIER = path.join(FXV, 'worker-verifier.mjs');

export const FORBIDDEN_FIX_ZONES = [/^\.github\/workflows\//, /^\.github\/CODEOWNERS$/,
  /^infra\/policies\//, /^deployment\/secrets\//];

const WORKER_TIMEOUT_MS = 120_000;

function runWorker(script, payload, timeoutMs = WORKER_TIMEOUT_MS) {
  return new Promise((resolve, reject) => {
    const p = spawn(process.execPath, ['--disable-warning=ExperimentalWarning', script],
      { stdio: ['pipe', 'pipe', 'pipe'] });
    let out = '', err = '';
    const t = setTimeout(() => { p.kill('SIGKILL'); reject(Object.assign(new Error('worker_timeout'), { code: 'STEP_TIMEOUT' })); }, timeoutMs);
    p.stdout.on('data', (d) => { out += d; });
    p.stderr.on('data', (d) => { err += d; });
    p.on('close', (code) => {
      clearTimeout(t);
      let parsed = null;
      try { parsed = JSON.parse(out.trim().split('\n').pop()); } catch { /* keep null */ }
      if (parsed) return resolve({ exit_code: code, ...parsed });
      reject(new Error(`worker_unparsable(exit ${code}): ${err.slice(0, 200)}`));
    });
    p.stdin.write(JSON.stringify(payload));
    p.stdin.end();
  });
}

const wsRoot = () => path.join(os.tmpdir(), 'mu-fxv-' + crypto.randomBytes(6).toString('hex'));

/** 禁改区判定（workflows/权限/保护/deployment secrets——任务书 PR-D 红线）。 */
export function isForbiddenFixZone(filePath) {
  return FORBIDDEN_FIX_ZONES.some((re) => re.test(String(filePath ?? '')));
}

/**
 * Fixer dry-run：按 finding 规则生成 patch（独立进程；不写任何远端）。
 * 返回 {ok, patch_text, patch_digest, rule_digest} 或 {ok:false, reason}。
 */
export async function fixerDryRun({ repoUrl, baseHeadSha, finding }) {
  if (isForbiddenFixZone(finding.file)) {
    return { ok: false, reason: 'SKIPPED_FORBIDDEN_ZONE', zone: String(finding.file) };
  }
  const workspace = wsRoot();
  const r = await runWorker(FIXER, { repo_url: repoUrl, base_head_sha: baseHeadSha,
    finding: { file: finding.file, pattern: finding.pattern, replacement: finding.replacement },
    workspace });
  if (r.ok === false) return { ok: false, reason: r.reason ?? 'FIXER_FAILED' };
  return { ok: true, patch_text: r.patch_text, patch_digest: r.patch_digest, rule_digest: r.rule_digest };
}

/**
 * Verifier 独立验证：fresh fetch@head + digest 复核 + apply + harness。
 * 返回 {verdict:'PASS'|'FAIL'|'BLOCKED', evidence}（FXV VERIFIED→PASS / REJECTED→FAIL）。
 */
export async function verifierVerify({ repoUrl, baseHeadSha, patchText, patchDigest, testCmd }) {
  const workspace = wsRoot();
  try {
    const r = await runWorker(VERIFIER, { repo_url: repoUrl, base_head_sha: baseHeadSha,
      patch_text: patchText, patch_digest: patchDigest, test_cmd: testCmd, workspace });
    const verdict = r.verdict === 'VERIFIED' ? 'PASS' : r.verdict === 'REJECTED' ? 'FAIL' : 'BLOCKED';
    // evidence 只保留结论性字段（digest/applied/harness_exit）——不回传输出正文
    return { verdict, evidence: {
      digest_binding: Boolean(r.evidence?.digest_binding),
      applied: Boolean(r.evidence?.applied),
      harness_exit: r.evidence?.harness_exit ?? null } };
  } catch (e) {
    return { verdict: 'BLOCKED', evidence: { error: String(e?.message ?? e).slice(0, 80) } };
  }
}
