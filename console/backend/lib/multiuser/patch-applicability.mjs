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
  .replace(/oauth2:[^@/\s]+@/g, 'oauth2:***@')
  .replace(/x-access-token:[^@/\s]+@/g, 'x-access-token:***@');

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

function appendCredential(repoUrl, token = null) {
  // Gitee：部署级 PAT（MU_GITEE_PAT）。其他平台（GitHub 私有仓）由调用方传
  // installation token（credentialToken）——凭据只进子进程参数，不落盘不落日志。
  if (token && !repoUrl.includes('@')) {
    return repoUrl.replace('https://', `https://x-access-token:${token}@`);
  }
  const pat = process.env.MU_GITEE_PAT ?? '';
  if (!pat || !repoUrl.includes('gitee.com')) return repoUrl;
  if (repoUrl.includes('@')) return repoUrl; // 已带凭据
  return repoUrl.replace('https://', `https://oauth2:${pat}@`);
}

/**
 * 补丁物化（用户确认应用修复的执行前置）：在隔离临时工作副本中检出准确 head，
 * 实际 `git apply` 补丁，读取每个变更文件的完整新内容——供 Git Data API 原子提交使用。
 *
 * 与 verifyPatchApplies 的关系：同一套隔离副本机械；这里是"执行面"（真 apply+物化），
 * 那里是"证据面"（--check）。物化失败=补丁在此 head 上不可应用→调用方中止，零远端写。
 *
 * 返回：
 *  { ok: true, files: [{path, content}], head_sha, digest }  —— content=null 表示补丁删除该文件
 *  { ok: false, reason, output? }                            —— 物化失败（未产生任何远端副作用）
 */
export async function materializePatchedFiles({ repoUrl, headSha, diffText, digest = null,
  credentialToken = null, timeoutMs = 60_000, workRoot = os.tmpdir() } = {}) {
  if (!repoUrl || !headSha || !diffText) {
    return { ok: false, reason: 'missing_repo_or_head_or_diff' };
  }
  let workdir = null;
  try {
    workdir = fs.mkdtempSync(path.join(workRoot, 'mp-applymat-'));
    const step = (ms) => Math.max(5_000, ms);

    const init = await git(['init'], workdir, step(10_000));
    if (init.code !== 0) return { ok: false, reason: 'git_init_failed', output: REDACT(init.stderr) };

    const urlWithCred = appendCredential(repoUrl, credentialToken);
    let fetch = await git(['fetch', '--depth', '5', urlWithCred, headSha], workdir, step(timeoutMs));
    if (fetch.code !== 0) fetch = await git(['fetch', urlWithCred], workdir, step(timeoutMs));
    if (fetch.code !== 0) return { ok: false, reason: 'fetch_head_failed', output: REDACT(fetch.stderr) };

    let co = await git(['checkout', '--detach', headSha], workdir, step(20_000));
    if (co.code !== 0) {
      // 浅取可能缺 tree 对象（本地 bare/部分服务端常见）——全量补齐后重试
      await git(['fetch', urlWithCred], workdir, step(timeoutMs));
      co = await git(['checkout', '--detach', headSha], workdir, step(20_000));
    }
    if (co.code !== 0) return { ok: false, reason: 'checkout_head_failed', output: REDACT(co.stderr) };
    await git(['config', 'core.autocrlf', 'false'], workdir, step(5_000));
    await git(['checkout', '--', '.'], workdir, step(10_000));

    const diffFile = path.join(workdir, 'suggested.patch');
    fs.writeFileSync(diffFile, String(diffText), { encoding: 'utf8', mode: 0o600 });
    const apply = await git(['apply', '--whitespace=nowarn', path.basename(diffFile)], workdir, step(timeoutMs));
    if (apply.code !== 0) {
      return { ok: false, reason: 'patch_apply_failed',
        output: REDACT(String(apply.stderr || apply.stdout)).slice(0, 300) };
    }

    const changed = await git(['diff', '--name-only', 'HEAD'], workdir, step(10_000));
    if (changed.code !== 0) return { ok: false, reason: 'diff_list_failed', output: REDACT(changed.stderr) };
    const files = [];
    for (const rel of String(changed.stdout).split('\n').map((s) => s.trim()).filter(Boolean)) {
      const abs = path.join(workdir, rel);
      if (!abs.startsWith(workdir)) return { ok: false, reason: 'path_escape_blocked' };
      if (!fs.existsSync(abs)) { files.push({ path: rel, content: null }); continue; } // 补丁删除的文件
      const st = fs.statSync(abs);
      if (!st.isFile()) return { ok: false, reason: 'unsupported_change_type', output: rel };
      files.push({ path: rel, content: fs.readFileSync(abs, 'utf8') });
    }
    if (!files.length) return { ok: false, reason: 'no_changes_after_apply' };
    return { ok: true, files, head_sha: headSha, ...(digest ? { digest } : {}) };
  } catch (e) {
    return { ok: false, reason: 'environment_error', output: REDACT(String(e?.message ?? e)).slice(0, 200) };
  } finally {
    if (workdir) {
      try { fs.rmSync(workdir, { recursive: true, force: true }); } catch { /* 尽力清理 */ }
    }
  }
}

/**
 * 推送式远端提交（应用修复的执行面，2026-10-15 定案；两阶段版 2026-10-15 收口波）：
 *   prepareCommit —— 隔离副本检出准确 head → CAS 前检 → 真实 apply → 本地提交，
 *                    返回候选 commitSha 并保留工作目录（供推送阶段使用）；
 *   pushPrepared  —— 从该目录 git push（无 --force——非快进被服务端拒绝，即真条件更新），
 *                    结束后清理目录。
 * 两阶段拆分的目的：候选 SHA 在【远端写入前】持久化到 mu.fix_apply_operation——
 * push 阶段超时/断连/进程退出时，读回对账（reconcile）可凭候选 SHA 判定远端实际状态。
 *
 * 为什么不用 Git Data API 链：Gitee v5 实测无 git/refs 端点（list/单查全 404），
 * 无分支 ref 更新 API——API 链无法移动分支；git push 语义等价且更强
 * （服务端 CAS：并发推进时 push 直接被拒，无预检/落 ref 两步间竞态窗）。
 * GitHub installation token 同样可用于 HTTPS push（x-access-token）——双平台单实现。
 *
 * 凭据：与 appendCredential 同纪律——URL 只进子进程参数，不落盘、不落日志（stderr 经 REDACT）。
 */

const BOT_IDENTITY = ['-c', 'user.name=MergePilot Bot', '-c', 'user.email=bot@mergepilot.local'];

export async function prepareCommit({ repoUrl, headSha, diffText, message,
  credentialToken = null, timeoutMs = 90_000, workRoot = os.tmpdir() } = {}) {
  if (!repoUrl || !headSha || !diffText || !message) {
    return { ok: false, reason: 'missing_args' };
  }
  let workdir = null;
  try {
    workdir = fs.mkdtempSync(path.join(workRoot, 'mp-prepare-'));
    const step = (ms) => Math.max(5_000, ms);

    const init = await git(['init'], workdir, step(10_000));
    if (init.code !== 0) return { ok: false, reason: 'git_init_failed', output: REDACT(init.stderr) };

    const urlWithCred = appendCredential(repoUrl, credentialToken);
    let fetch = await git(['fetch', '--depth', '5', urlWithCred, headSha], workdir, step(timeoutMs));
    if (fetch.code !== 0) fetch = await git(['fetch', urlWithCred], workdir, step(timeoutMs));
    if (fetch.code !== 0) return { ok: false, reason: 'fetch_head_failed', output: REDACT(fetch.stderr) };

    let co = await git(['checkout', '--detach', headSha], workdir, step(20_000));
    if (co.code !== 0) {
      // 浅取可能缺 tree 对象（本地 bare/部分服务端常见）——全量补齐后重试
      await git(['fetch', urlWithCred], workdir, step(timeoutMs));
      co = await git(['checkout', '--detach', headSha], workdir, step(20_000));
    }
    if (co.code !== 0) return { ok: false, reason: 'checkout_head_failed', output: REDACT(co.stderr) };
    await git(['config', 'core.autocrlf', 'false'], workdir, step(5_000));
    await git(['checkout', '--', '.'], workdir, step(10_000));

    // 前置 CAS 读取：远端分支 tip 校验留待推送阶段（prepare 不绑分支，绑定关系在调用方）
    const diffFile = path.join(workdir, 'suggested.patch');
    fs.writeFileSync(diffFile, String(diffText), { encoding: 'utf8', mode: 0o600 });
    const apply = await git(['apply', '--whitespace=nowarn', path.basename(diffFile)], workdir, step(timeoutMs));
    if (apply.code !== 0) {
      return await discard(workdir, { ok: false, reason: 'patch_apply_failed',
        output: REDACT(String(apply.stderr || apply.stdout)).slice(0, 300) });
    }

    await git(['add', '-A'], workdir, step(20_000));
    const ci = await git([...BOT_IDENTITY, 'commit', '-m', message], workdir, step(30_000));
    if (ci.code !== 0) {
      return await discard(workdir, { ok: false, reason: 'commit_failed', output: REDACT(ci.stderr) });
    }
    const sha = (await git(['rev-parse', 'HEAD'], workdir, step(5_000))).stdout.trim();
    return { ok: true, commitSha: sha, previousSha: headSha, dir: workdir };
  } catch (e) {
    if (workdir) await discard(workdir, null);
    return { ok: false, reason: 'environment_error', output: REDACT(String(e?.message ?? e)).slice(0, 200) };
  }
}

/**
 * 推送已准备的候选提交。前置 CAS：远端分支 tip 必须仍等于 expectedHead；
 * push 无 --force：即便通过前检，并发推进仍会被服务端拒绝（条件更新的最终保证在远端）。
 * 成败均清理工作目录（result_uncertain 不保留目录——恢复走 reconcile，不依赖本地残留）。
 */
export async function pushPrepared({ dir, repoUrl, branch, expectedHead,
  credentialToken = null, timeoutMs = 90_000 } = {}) {
  if (!dir || !repoUrl || !branch || !expectedHead) {
    if (dir) await discard(dir, null);
    return { ok: false, reason: 'missing_args' };
  }
  try {
    const urlWithCred = appendCredential(repoUrl, credentialToken);
    const ls = await git(['ls-remote', urlWithCred, `refs/heads/${branch}`], dir, 30_000);
    if (ls.code !== 0) return { ok: false, reason: 'ls_remote_failed', output: REDACT(ls.stderr) };
    const remoteTip = String(ls.stdout).trim().split(/	/)[0];
    if (!remoteTip) return { ok: false, reason: 'branch_not_found', detail: `refs/heads/${branch}` };
    if (remoteTip.toLowerCase() !== String(expectedHead).toLowerCase()) {
      return { ok: false, reason: 'stale_head',
        detail: `expected=${String(expectedHead).slice(0, 12)} actual=${String(remoteTip).slice(0, 12)}` };
    }

    // 无 --force：非快进（并发推进）被服务端拒绝——条件更新在远端强制生效
    const push = await git(['push', urlWithCred, `HEAD:refs/heads/${branch}`], dir, timeoutMs);
    if (push.code !== 0) {
      const t = String(push.stderr || push.stdout);
      if (/non-fast-forward|rejected|fetch first|stale info|\[rejected\]/i.test(t)) {
        return { ok: false, reason: 'stale_head', detail: `push rejected (non-fast-forward): ${REDACT(t).slice(0, 200)}` };
      }
      return { ok: false, reason: 'push_failed', output: REDACT(t).slice(0, 300) };
    }
    return { ok: true };
  } finally {
    await discard(dir, null);
  }
}

/** 丢弃已准备的候选目录（候选 SHA 已持久化时，恢复走 reconcile 不依赖目录）。 */
export async function discardPrepared(dir) {
  await discard(dir, null);
}

async function discard(dir, carry) {
  try { fs.rmSync(dir, { recursive: true, force: true }); } catch { /* 尽力清理 */ }
  return carry;
}

/** 单调用组合器（prepare+push）；需要"写前持久化候选 SHA"的调用方应直接用两阶段。 */
export async function pushCommit({ repoUrl, headSha, diffText, branch, message,
  credentialToken = null, timeoutMs = 90_000, workRoot = os.tmpdir() } = {}) {
  const pre = await prepareCommit({ repoUrl, headSha, diffText, message,
    credentialToken, timeoutMs, workRoot });
  if (!pre.ok) return pre;
  const r = await pushPrepared({ dir: pre.dir, repoUrl, branch, expectedHead: headSha,
    credentialToken, timeoutMs });
  return r.ok ? { ok: true, commitSha: pre.commitSha, previousSha: pre.previousSha } : r;
}

/**
 * 读回对账原语：远端分支 tip 与候选提交的祖先关系判定（恢复任务用）。
 * 返回 { tip, isCandidate: 'tip'|'ancestor'|'absent'|'unknown' }：
 *   tip       = 分支 tip 就是候选提交（push 成功且其后无新提交）
 *   ancestor  = 候选是 tip 的祖先（push 成功，其后他人又推进——快进语义下只有这种"已落地"形态）
 *   absent    = 候选不在 tip 祖先链上（push 未落地）
 *   unknown   = 网络/环境原因无法判定（保持 uncertain，不得臆断）
 */
export async function checkCommitOnBranch({ repoUrl, branch, candidateSha, credentialToken = null,
  timeoutMs = 90_000, workRoot = os.tmpdir() } = {}) {
  let workdir = null;
  try {
    workdir = fs.mkdtempSync(path.join(workRoot, 'mp-recon-'));
    // 对账临时目录必须先初始化 Git 仓库——fetch/merge-base 均要求 repo 语境
    const init = await git(['init', '-q'], workdir, 10_000);
    if (init.code !== 0) return { relation: 'unknown', reason: 'git_init_failed' };
    const urlWithCred = appendCredential(repoUrl, credentialToken);
    const ls = await git(['ls-remote', urlWithCred, `refs/heads/${branch}`], workdir, 30_000);
    if (ls.code !== 0) return { relation: 'unknown', reason: 'ls_remote_failed' };
    const tip = String(ls.stdout).trim().split(/	/)[0];
    if (!tip) return { relation: 'unknown', reason: 'branch_not_found' };
    if (tip.toLowerCase() === String(candidateSha).toLowerCase()) return { relation: 'tip', tip };

    // 全量取目标分支历史（权威祖先判定）——浅取判 is-ancestor 会把"历史不足"误判为
    // "候选不存在"（浅边界之外的假阴性）；对账是一次性人工恢复路径，全量取单个分支可接受。
    const f1 = await git(['fetch', urlWithCred, `refs/heads/${branch}:refs/heads/recon-branch`],
      workdir, timeoutMs);
    if (f1.code !== 0) return { relation: 'unknown', reason: 'fetch_failed', tip };
    const have = await git(['cat-file', '-t', candidateSha], workdir, 10_000);
    if (have.code !== 0) return { relation: 'absent', tip }; // 分支全历史已本地、对象仍缺=确不在该分支
    const anc = await git(['merge-base', '--is-ancestor', candidateSha, 'refs/heads/recon-branch'],
      workdir, 15_000);
    if (anc.code === 0) return { relation: 'ancestor', tip };
    if (anc.code === 1) return { relation: 'absent', tip };
    return { relation: 'unknown', reason: 'merge_base_failed', tip };
  } catch (e) {
    return { relation: 'unknown', reason: 'environment_error', detail: String(e?.message ?? e).slice(0, 120) };
  } finally {
    if (workdir) { try { fs.rmSync(workdir, { recursive: true, force: true }); } catch { /* */ } }
  }
}
