// patch-test.mjs — 实际修复测试（验证第三分，2026-10-14 可用补丁闭环波）。
//
// 做什么：在隔离临时工作副本中检出准确 head → 修复前运行【预批准】测试命令（须失败，
// 证明测试确实覆盖该缺陷）→ 真实 git apply 应用补丁 → 修复后同一命令（须通过）。
// 产出结构化证据：命令/两次退出码/脱敏日志尾/耗时/checked_at/head/补丁 digest。
//
// 纪律：
//  * testCmd 只能来自部署级预批准配置（resolvePreApprovedTestCmd），绝不信模型输出——
//    本服务不校验命令内容的安全性，调用方必须保证其来自配置而非用户/模型；
//  * 子进程环境最小化（PATH/HOME 仅此两项）——不携带生产凭据、不继承宿主 env；
//  * 纯文件系统临时目录（无挂载、无 Docker socket、无网络服务）；
//  * "修复前即通过"= ok:false 且 reason=pre_test_passed——不能证明修复有效性，如实报告。
//
// 不做什么：不判定代码行为正确性之外的事；不推送；不把本服务当通用沙箱。

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

const run = (cmd, args, cwd, timeoutMs) => execFileP(cmd, args, {
  cwd, timeout: timeoutMs, maxBuffer: 4 * 1024 * 1024,
  // 环境最小化（生产=linux 容器）：只给 PATH/HOME——不携带生产凭据、不继承宿主 env。
  // win32 仅本机测试路径：node/git 解析需要宿主 PATH（生产 linux 不走此分支）。
  env: process.platform === 'win32'
    ? { PATH: process.env.PATH ?? '/usr/bin:/bin', HOME: cwd }
    : { PATH: '/usr/local/bin:/usr/bin:/bin', HOME: cwd },
}).then((r) => ({ code: 0, stdout: r.stdout ?? '', stderr: r.stderr ?? '' }))
  .catch((e) => ({
    code: typeof e.code === 'number' ? e.code : -1,
    stdout: e.stdout ?? '', stderr: String(e.stderr ?? e.message ?? ''),
    killed: Boolean(e.killed),
  }));

const git = (args, cwd, timeoutMs) => run('git', args, cwd, timeoutMs);
const tail400 = (r) => REDACT(String(r.stdout || r.stderr || '').trim().split('\n').slice(-8).join('\n').slice(0, 400));

/**
 * 实际修复测试。参数：{ repoUrl, headSha, diffText, testCmd(预批准),
 *   patchDigest, timeoutMs=120s, workRoot=/tmp }
 * 返回：
 *  { verified: true, ok: bool, test_cmd, head_sha, patch_digest?, checked_at,
 *    pre: {code,duration_ms,output}, post: {code,duration_ms,output}|null, apply_ok }
 *  { verified: false, skipped: true, reason } —— 环境不可用，调用方如实"未验证"。
 */
export async function verifyFixTest({ repoUrl, headSha, diffText, testCmd,
  patchDigest = null, timeoutMs = 120_000, workRoot = os.tmpdir() } = {}) {
  const checkedAt = new Date().toISOString();
  if (!repoUrl || !headSha || !diffText || !testCmd) {
    return { verified: false, skipped: true, reason: 'missing_args', checked_at: checkedAt };
  }
  let workdir = null;
  try {
    workdir = fs.mkdtempSync(path.join(workRoot, 'mp-fixtest-'));
    const step = (ms) => Math.max(5_000, ms);
    const testStep = () => Math.max(10_000, Math.floor(timeoutMs * 0.4));

    if ((await git(['init'], workdir, step(10_000))).code !== 0) {
      return { verified: false, skipped: true, reason: 'git_init_failed', checked_at: checkedAt };
    }
    // 凭据注入纪律：URL 只进子进程参数——绝不 git remote add（PAT 不写 .git/config）。
    const pat = process.env.MU_GITEE_PAT ?? '';
    const url = (pat && repoUrl.includes('gitee.com') && !repoUrl.includes('@'))
      ? repoUrl.replace('https://', `https://oauth2:${pat}@`) : repoUrl;
    let fetch = await git(['fetch', '--depth', '5', url, headSha], workdir, step(timeoutMs));
    if (fetch.code !== 0) fetch = await git(['fetch', url], workdir, step(timeoutMs));
    if (fetch.code !== 0) {
      return { verified: false, skipped: true, reason: 'fetch_head_failed',
        output: REDACT(fetch.stderr).slice(0, 200), checked_at: checkedAt };
    }
    if ((await git(['checkout', '--detach', headSha], workdir, step(20_000))).code !== 0) {
      return { verified: false, skipped: true, reason: 'checkout_head_failed', checked_at: checkedAt };
    }
    await git(['config', 'core.autocrlf', 'false'], workdir, step(5_000));
    await git(['checkout', '--', '.'], workdir, step(10_000));

    const shell = process.platform === 'win32' ? 'sh' : 'sh';
    const t0pre = Date.now();
    const pre = await run(shell, ['-c', testCmd], workdir, testStep());
    const preOut = tail400(pre);
    if (pre.code === 0) {
      // 修复前即通过：测试没有覆盖该缺陷——不能证明修复有效性，如实报告
      return { verified: true, ok: false, reason: 'pre_test_passed', test_cmd: testCmd,
        head_sha: headSha, ...(patchDigest ? { patch_digest: patchDigest } : {}),
        checked_at: checkedAt, pre: { code: 0, duration_ms: Date.now() - t0pre, output: preOut }, post: null };
    }
    const diffFile = path.join(workdir, 'suggested.patch');
    fs.writeFileSync(diffFile, String(diffText), { encoding: 'utf8', mode: 0o600 });
    const apply = await git(['apply', '--whitespace=nowarn', path.basename(diffFile)],
      workdir, step(timeoutMs));
    if (apply.code !== 0) {
      return { verified: true, ok: false, reason: 'apply_failed', test_cmd: testCmd,
        head_sha: headSha, ...(patchDigest ? { patch_digest: patchDigest } : {}),
        checked_at: checkedAt, pre: { code: pre.code, duration_ms: Date.now() - t0pre, output: preOut },
        post: null, apply_output: REDACT(apply.stderr || apply.stdout).slice(0, 200) };
    }
    const t0post = Date.now();
    const post = await run(shell, ['-c', testCmd], workdir, testStep());
    const postOut = tail400(post);
    return { verified: true, ok: post.code === 0, test_cmd: testCmd, head_sha: headSha,
      ...(patchDigest ? { patch_digest: patchDigest } : {}), checked_at: checkedAt,
      pre: { code: pre.code, duration_ms: Date.now() - t0pre, output: preOut },
      post: { code: post.code, duration_ms: Date.now() - t0post, output: postOut } };
  } catch (e) {
    return { verified: false, skipped: true, reason: 'environment_error',
      output: REDACT(String(e?.message ?? e)).slice(0, 200), checked_at: checkedAt };
  } finally {
    if (workdir) {
      try { fs.rmSync(workdir, { recursive: true, force: true }); } catch { /* 尽力清理 */ }
    }
  }
}
