// patch-applicability.mjs — 建议补丁真实可应用性检查（2026-10-13/14 波）。
//
// 做什么：在隔离临时工作副本中检出补丁的目标 head，运行 `git apply --check`
// 校验补丁能否干净应用。产出结构化证据（方式/结果/耗时/时间/脱敏诊断），
// 由调用方并入 mu.fix_patch.applicability。
//
// 不做什么：不推送、不改远端、不执行模型生成的任意命令、不把结构检查冒充为
// "可应用性验证通过"（通过=本次 git apply --check 实际执行且退出码 0）。
//
// 安全：诊断输出脱敏（token/PAT 形态）；临时目录用后即清；超时强杀；
//       clone 凭据仅来自部署级 env（MU_GITEE_PAT），只进子进程参数，不落日志。

import { execFile } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { promisify } from 'node:util';

const execFileP = promisify(execFile);
const REDACT = (s) => String(s ?? '')
  .replace(/(ghp_|gho_|github_pat_|sk-)[A-Za-z0-9_]{6,}/g, '$1***')
  .replace(/access_token=[^&\s]+/g, 'access_token=***')
  .replace(/oauth2:[^@/\s]+@/g, 'oauth2:***@');

const git = (args, cwd, timeoutMs) => execFileP('git', args, {
  cwd, timeout: timeoutMs, maxBuffer: 4 * 1024 * 1024,
}).then((r) => ({ code: 0, stdout: r.stdout ?? '', stderr: r.stderr ?? '' }))
  .catch((e) => ({
    code: e.code ?? -1,
    stdout: e.stdout ?? '',
    stderr: String(e.stderr ?? e.message ?? '').slice(0, 400),
    killed: Boolean(e.killed),
  }));

/**
 * 校验补丁能否应用到准确 head。
 * 参数：{ repoUrl(https 形式，无需内置凭据——私有仓库自动用 env.MU_GITEE_PAT),
 *        headSha, diffText, patchDigest(可选——证据绑定 digest，补丁更新后旧证据失效),
 *        timeoutMs=60s, workRoot=/tmp }
 * 纪律：检查内容=调用方传入的原样文本（不做任何静默归一化——检查结论只对传入内容负责；
 *       缺尾换行等可确定修复的格式问题由调用方产出新版本补丁并重新检查）。
 * 返回：
 *  { verified: true, ok: bool, method: 'git apply --check', head_sha, patch_digest?,
 *    duration_ms, checked_at(ISO), output(脱敏,≤400 字符),
 *    diagnosis?: 'missing_trailing_newline'（失败且文本缺尾换行——确定性修正的充分线索） }
 *  { verified: false, skipped: true, reason } —— 环境不可用（如无 git/克隆失败）；
 *    调用方如实显示"未验证：原因"，不误报通过。
 */
export async function verifyPatchApplies({ repoUrl, headSha, diffText, patchDigest = null,
  timeoutMs = 60_000, workRoot = os.tmpdir() } = {}) {
  const checkedAt = new Date().toISOString();
  const t0 = Date.now();
  if (!repoUrl || !headSha || !diffText) {
    return { verified: false, skipped: true, reason: 'missing_repo_or_head_or_diff', checked_at: checkedAt };
  }
  let workdir = null;
  try {
    workdir = fs.mkdtempSync(path.join(workRoot, 'mp-apply-'));
    const step = (ms) => Math.max(5_000, ms);

    const init = await git(['init'], workdir, step(10_000));
    if (init.code !== 0) return { verified: false, skipped: true, reason: 'git_init_failed', output: REDACT(init.stderr), checked_at: checkedAt };

    // 凭据注入纪律：URL 只进子进程参数（瞬时、不落盘）——绝不 git remote add
    // （带 PAT 的 URL 会持久化进临时工作树的 .git/config）。
    const urlWithCred = appendCredential(repoUrl);

    // 浅取目标 head（服务端支持按 sha fetch；不支持时退化为全量取回后 checkout）
    let fetch = await git(['fetch', '--depth', '5', urlWithCred, headSha], workdir, step(timeoutMs));
    if (fetch.code !== 0) {
      fetch = await git(['fetch', urlWithCred], workdir, step(timeoutMs));
    }
    if (fetch.code !== 0) return { verified: false, skipped: true, reason: 'fetch_head_failed',
      output: REDACT(fetch.stderr), checked_at: checkedAt };

    const co = await git(['checkout', '--detach', headSha], workdir, step(20_000));
    if (co.code !== 0) return { verified: false, skipped: true, reason: 'checkout_head_failed',
      output: REDACT(co.stderr), checked_at: checkedAt };
    // Windows 检出默认 CRLF 转换会让补丁上下文与工作树字节不一致——
    // 统一禁用（临时仓库，不影响任何用户配置）。
    await git(['config', 'core.autocrlf', 'false'], workdir, step(5_000));
    await git(['checkout', '--', '.'], workdir, step(10_000));

    const diffFile = path.join(workdir, 'suggested.patch');
    fs.writeFileSync(diffFile, String(diffText), { encoding: 'utf8', mode: 0o600 });

    const apply = await git(['apply', '--check', '--whitespace=nowarn', path.basename(diffFile)],
      workdir, step(timeoutMs));
    const dur = Date.now() - t0;
    const failedText = String(apply.stderr || apply.stdout);
    const evidence = {
      verified: true,
      ok: apply.code === 0,
      method: 'git apply --check',
      head_sha: headSha,
      duration_ms: dur,
      checked_at: checkedAt,
      output: REDACT(apply.code === 0 ? 'git apply --check 通过（可干净应用）'
        : `git apply --check 失败: ${failedText.trim().slice(0, 200)}`),
    };
    if (patchDigest) evidence.patch_digest = patchDigest;
    // 成因线索（仅诊断，不做修复——修复动作属调用方的版本化产物流程）：
    // 缺尾换行时 git apply 报 corrupt patch，且该情形可无歧义确定性修正。
    if (apply.code !== 0 && /corrupt patch/i.test(failedText)
      && !String(diffText).endsWith('\n')) {
      evidence.diagnosis = 'missing_trailing_newline';
    }
    return evidence;
  } catch (e) {
    return { verified: false, skipped: true, reason: 'environment_error',
      output: REDACT(String(e?.message ?? e)).slice(0, 200), checked_at: checkedAt };
  } finally {
    if (workdir) {
      try { fs.rmSync(workdir, { recursive: true, force: true }); } catch { /* 尽力清理 */ }
    }
  }
}

function appendCredential(repoUrl) {
  const pat = process.env.MU_GITEE_PAT ?? '';
  if (!pat || !repoUrl.includes('gitee.com')) return repoUrl;
  if (repoUrl.includes('@')) return repoUrl; // 已带凭据
  return repoUrl.replace('https://', `https://oauth2:${pat}@`);
}
