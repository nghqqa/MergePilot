// fix-apply.mjs — 用户授权远端写入服务层：应用修复补丁到 PR 源分支 + 合并 PR。
//
// 职责分离（本波红线）：
//  * 模型与四角色链只生成与验证建议（dry-run，零远端写）——本模块只在用户显式确认后
//    执行实际远端写入，凭据只从部署级 env / installation token 注入；
//  * 每次操作绑定 mu.fix_apply_operation 持久化记录；
//  * 幂等与并发：数据库原子认领（单语句 INSERT..ON CONFLICT DO NOTHING / 受保护 UPDATE）
//    ——只有执行权持有者可做远端写与最终状态更新；并发败方零写入、零覆写；
//  * 预览—确认绑定：confirm 必须携带预览所示 expected_remote_head / expected_patch_digest，
//    缺字段直接拒绝；预览后 active patch 换版或 head 漂移 → 拒绝并要求重新预览；
//  * 两阶段写：prepareCommit（本地候选提交，SHA 先持久化）→ pushPrepared（真 push）——
//    候选 SHA 在远端写入前落库；超时/断连/进程退出 → result_uncertain，读回对账定案，
//    禁止盲目重试（不靠超时清锁、不自动重复写）；
//  * 合并资格门：preview 与 confirm 用同一 evaluateMergeEligibility（confirm 重读远端事实）：
//    PR 开放、head 一致、mergeable 已知且可合并、当前 head 有效审查完成且无未处理 P0/P1、
//    当前 head 真实 CI 通过；pending/failed/unknown/not_configured/unsupported 一律阻塞并给原因；
//  * 不做：自动提交、自动合并、required check 操作、代批审批、PAT 标绿/伪造检查结果。
//
// 凭据纪律：Gitee PAT / GitHub installation token 只存在于本次调用的内存栈中；
// 不进前端、不进日志、不进数据库记录、不写临时 Git 配置（物化用 URL 参数瞬时传递）。

import { prepareCommit, pushPrepared, checkCommitOnBranch } from './patch-applicability.mjs';
import { platformMergePR } from './remote-write.mjs';
import { fetchRemotePr } from './publish.mjs';

// pool 参数：传入查询函数（muPoolQ）或 Pool 实例均可——normQ 统一。
const normQ = (pool) => (typeof pool === 'function' ? pool : (text, params) => pool.query(text, params));

const RUN_SELECT = `
  SELECT r.run_id, r.status, r.head_sha, r.repo_id, r.pr_id, r.tenant_id,
         pr.provider_pr_number, rep.provider, rep.owner, rep.name AS repo_name,
         (SELECT rb.installation_id FROM mu.repository_binding rb
           WHERE rb.repo_id = r.repo_id AND rb.tenant_id = r.tenant_id
             AND rb.binding_state = 'active' LIMIT 1) AS installation_id
    FROM mu.review_run r
    JOIN mu.pull_request pr ON pr.pr_id = r.pr_id
    JOIN mu.repository rep ON rep.repo_id = r.repo_id
   WHERE r.run_id=$1 AND r.tenant_id=$2`;

const ACTIVE_PATCH_SELECT = `
  SELECT patch_id, patch_version, patch_digest, head_sha, diff_text,
         applicability, test_verification
    FROM mu.fix_patch WHERE run_id=$1 AND active
   ORDER BY patch_version DESC LIMIT 1`;

function robotIdentity(provider) {
  return provider === 'github' ? 'MergePilot GitHub App' : 'MergePilot Gitee Bot';
}

function providerHost(provider, env) {
  return provider === 'github' ? 'github.com'
    : String(env.MU_GITEE_INSTANCE_WEB_BASE ?? 'https://gitee.com').replace(/^https?:\/\//, '').replace(/\/+$/, '');
}

function cloneUrl(provider, owner, repo, env = process.env) {
  return `https://${providerHost(provider, env)}/${owner}/${repo}.git`;
}

/** 结果不明语义：网络层失败（超时/断连/响应丢失/进程异常）——远端状态不可判定。 */
const UNCERTAIN_CLASS = /timeout|abort|econn|network|socket|fetch failed|etimedout|ehostunreach|enetunreach/i;

// ── 原子认领（数据库单语句；败方零写入）──────────────────────────────

/**
 * 原子认领执行权。返回：
 *   { claimed: true,  operationId }                      —— 本调用持有执行权
 *   { claimed: false, state, operationId?, commitSha? }  —— state: idempotent|in_progress
 * 纯 SQL 认领：INSERT..ON CONFLICT DO NOTHING（新建即 executing）；
 * 已存在行按状态分流，重试认领用受保护 UPDATE（仅 failed/previewed 可重新认领；
 * result_uncertain 一律先读回对账——对账定案为 failed（确认未写入）后才可重试，
 * 禁止从 uncertain 直接重认领造成重复远端写）。
 */
async function claimOperation(q, { insertSql, insertParams, idemKey }) {
  const ins = await q(
    `${insertSql} ON CONFLICT (idempotency_key) DO NOTHING RETURNING operation_id`,
    insertParams);
  if (ins.rows.length) return { claimed: true, operationId: ins.rows[0].operation_id };
  const ex = (await q(
    `SELECT operation_id, status, candidate_commit_sha, remote_result_sha
       FROM mu.fix_apply_operation WHERE idempotency_key=$1`, [idemKey])).rows[0] ?? null;
  if (!ex) return { claimed: false, state: 'in_progress' }; // 理论不可达（INSERT 已兜底）
  if (['committed', 'merged', 'completed'].includes(ex.status)) {
    return { claimed: false, state: 'idempotent', operationId: ex.operation_id,
      commitSha: ex.candidate_commit_sha ?? ex.remote_result_sha };
  }
  if (['executing', 'confirmed'].includes(ex.status)) {
    return { claimed: false, state: 'in_progress', operationId: ex.operation_id };
  }
  // result_uncertain 一律拒绝并指向对账：证据不足禁止重新准备/推送；
  // 只有对账定案为 failed（确认未写入）后才会落到下面的 failed 分支允许重试。
  if (ex.status === 'result_uncertain') {
    return { claimed: false, state: 'requires_reconcile', operationId: ex.operation_id };
  }
  // failed / previewed → 尝试原子重认领（败方拿不到）。
  const re = await q(
    `UPDATE mu.fix_apply_operation SET status='executing', error_code=NULL, error_detail=NULL,
       updated_at=now()
      WHERE idempotency_key=$1 AND status IN ('failed','previewed')
      RETURNING operation_id`, [idemKey]);
  if (re.rows.length) return { claimed: true, operationId: re.rows[0].operation_id };
  return { claimed: false, state: 'in_progress', operationId: ex.operation_id };
}

/**
 * 受保护状态更新：仅当行仍处于 from 状态时写入（执行权持有者语义）。
 * 返回 true=本调用是执行权持有者且更新成功；false=已被他人推进（零覆写）。
 */
async function guardedUpdate(q, operationId, fromStatuses, setSql, params = []) {
  const r = await q(
    `UPDATE mu.fix_apply_operation SET ${setSql}
      WHERE operation_id=$${params.length + 1} AND status = ANY($${params.length + 2}::text[])
      RETURNING operation_id`,
    [...params, operationId, fromStatuses]);
  return r.rows.length > 0;
}

// ── 预览：应用修复 ───────────────────────────────────────────────────

export async function previewApplyFix(pool, { env = process.env, runId, tenantId }) {
  const q = normQ(pool);
  const run = (await q(RUN_SELECT, [runId, tenantId])).rows[0] ?? null;
  if (!run) return { ok: false, reason: 'run_not_found' };
  const provider = String(run.provider ?? '');
  if (!['gitee', 'github'].includes(provider)) return { ok: false, reason: 'provider_unsupported' };

  const patch = (await q(ACTIVE_PATCH_SELECT, [runId])).rows[0] ?? null;
  if (!patch) return { ok: false, reason: 'no_active_patch' };

  let remote = null, remote_error = null;
  try {
    remote = await fetchRemotePr(env, { provider, owner: run.owner, repo: run.repo_name,
      prNumber: run.provider_pr_number, installationId: run.installation_id });
  } catch (e) { remote_error = String(e?.message ?? e).slice(0, 80); }

  const headMatch = Boolean(remote?.headSha)
    && remote.headSha.toLowerCase() === String(patch.head_sha).toLowerCase();

  const ga = patch.applicability ?? {};
  const tv = patch.test_verification ?? null;

  const ops = (await q(
    `SELECT operation_id, status, candidate_commit_sha, remote_result_sha, error_code,
            error_detail, operation_data, created_at, updated_at
       FROM mu.fix_apply_operation
      WHERE run_id=$1 AND operation_type='apply_fix'
      ORDER BY updated_at DESC LIMIT 5`, [runId])).rows;

  const patchInfo = {
    id: patch.patch_id, version: patch.patch_version, digest: patch.patch_digest,
    applicability_state: ga.git_apply?.ok === true ? 'pass'
      : ga.git_apply?.ok === false ? 'fail' : 'not_executed',
    fix_test: tv?.verified
      ? { executed: true, ok: tv.ok === true }
      : { executed: false },
  };

  const alreadyApplied = ops.some((o) => ['committed', 'merged', 'completed'].includes(o.status));
  const inFlight = ops.some((o) => ['executing', 'confirmed'].includes(o.status)
    && !o.operation_data?.reconciled_at);
  const can_apply = Boolean(remote) && headMatch && !alreadyApplied && !inFlight;

  return {
    ok: true, action: 'apply_fix',
    target: {
      provider, repo: `${run.owner}/${run.repo_name}`, pr_number: run.provider_pr_number,
      source_branch: remote?.sourceBranch ?? null, target_branch: remote?.targetBranch ?? null,
      pr_state: remote?.state ?? null,
      review_head: patch.head_sha, remote_head: remote?.headSha ?? null, head_match: headMatch,
      // remote_head_error 为前端读取的规范键（2026-10-17 定案：键名错位使错误信息恒丢失，
      // head_match=false+无 head 被误标「已变化」）；remote_error 保留向后兼容。
      ...(remote_error ? { remote_error, remote_head_error: remote_error } : {}),
    },
    patch: patchInfo,
    robot_identity: robotIdentity(provider),
    previous_operations: ops.map((o) => ({
      operation_id: o.operation_id, status: o.status,
      commit: o.candidate_commit_sha ?? o.remote_result_sha ?? null,
      error_code: o.error_code ?? null,
      reconcilable: ['executing', 'result_uncertain'].includes(o.status),
      stale_lock: o.status === 'executing'
        && (Date.now() - new Date(o.updated_at).getTime()) > 10 * 60 * 1000,
      updated_at: o.updated_at,
    })),
    can_apply,
    blocked_reason: can_apply ? null
      : alreadyApplied ? 'already_applied'   // 本地操作记录优先于远端 head 状态（应用成功后 head 必然已推进）
      : inFlight ? 'operation_in_progress'
      : !remote ? 'remote_unreachable'
      : !headMatch ? 'head_moved'
      : null,
    confirm_binds: {
      expected_remote_head: remote?.headSha ?? null,
      expected_patch_digest: patch.patch_digest,
      expected_patch_version: patch.patch_version,
    },
    operation_note: '确认后将在 PR 源分支新增一个提交（补丁的全部变更）。不合并 PR；合并需要再次单独确认。',
    rollback_note: '服务回滚不会撤销已发生的远端提交；回退需在平台上手动 revert。',
  };
}

// ── 确认：应用修复（两阶段 + 原子认领 + 读回对账可恢复）──────────────

export async function confirmApplyFix(pool, { env = process.env, runId, tenantId, userId,
  confirm = false, expectedRemoteHead = null, expectedPatchDigest = null, deps = {} }) {
  const q = normQ(pool);
  if (confirm !== true) return { ok: false, reason: 'confirm_required' };
  if (!userId) return { ok: false, reason: 'operator_required' };
  // 预览—确认绑定：两个期望字段缺一不可（用户确认的是"这个补丁字节打到这个 head"）
  if (!expectedRemoteHead || !expectedPatchDigest) {
    return { ok: false, reason: 'invalid_confirm_fields',
      detail: '缺少必要确认字段（expected_remote_head / expected_patch_digest）——请从预览发起确认' };
  }
  const run = (await q(RUN_SELECT, [runId, tenantId])).rows[0] ?? null;
  if (!run) return { ok: false, reason: 'run_not_found' };
  const provider = String(run.provider ?? '');
  if (!['gitee', 'github'].includes(provider)) return { ok: false, reason: 'provider_unsupported' };

  const patch = (await q(ACTIVE_PATCH_SELECT, [runId])).rows[0] ?? null;
  if (!patch) return { ok: false, reason: 'no_active_patch' };
  // digest 绑定：确认的补丁字节=当前 active 版本；预览后换版 → 拒绝（禁止静默应用新版本）
  if (String(expectedPatchDigest).toLowerCase() !== String(patch.patch_digest).toLowerCase()) {
    return { ok: false, reason: 'patch_changed',
      detail: `确认的补丁 digest ${String(expectedPatchDigest).slice(0, 12)} 与当前 active 版本 ${String(patch.patch_digest).slice(0, 12)} 不一致——补丁已更新，请重新预览` };
  }
  // 确认时复核存储内容 digest（下载/应用同源——存储被篡改则在此拒绝）
  const crypto = await import('node:crypto');
  const storedDigest = crypto.createHash('sha256').update(patch.diff_text, 'utf8').digest('hex');
  if (storedDigest !== String(patch.patch_digest).toLowerCase()) {
    return { ok: false, reason: 'digest_mismatch',
      detail: '存储补丁内容与登记 digest 不一致——已拒绝执行（请重新生成补丁）' };
  }

  const idemKey = `${runId}:apply_fix:${patch.patch_digest}`;

  // 幂等先于一切远端检查：已成功提交的同键操作直接返回既有结果
  // （真实平台实证 2026-10-15：push 落地后 PR head 已推进，重复确认若先查远端会被
  //   head 检查拒绝，永远到不了幂等返回——重复请求必须返回记录在案的结果）
  const preExisting = (await q(
    `SELECT operation_id, status, candidate_commit_sha FROM mu.fix_apply_operation
      WHERE idempotency_key=$1`, [idemKey])).rows[0] ?? null;
  if (preExisting && ['committed', 'merged', 'completed'].includes(preExisting.status)) {
    return { ok: true, idempotent: true, operation_id: preExisting.operation_id,
      status: preExisting.status, commit_sha: preExisting.candidate_commit_sha };
  }

  // 远端 head 复核（用户确认的是"把这个补丁打到这个 head"）
  let remote = null;
  try {
    remote = await fetchRemotePr(env, { provider, owner: run.owner, repo: run.repo_name,
      prNumber: run.provider_pr_number, installationId: run.installation_id });
  } catch (e) {
    return { ok: false, reason: 'remote_unreachable', detail: String(e?.message ?? e).slice(0, 80) };
  }
  if (!remote.headSha || remote.headSha.toLowerCase() !== String(patch.head_sha).toLowerCase()) {
    return { ok: false, reason: 'stale_head',
      detail: `补丁基于 ${String(patch.head_sha).slice(0, 12)}，远端当前 ${String(remote.headSha).slice(0, 12)}` };
  }
  // 双确认：请求携带的期望 head 与服务端所见一致（防预览→确认窗口漂移）
  if (expectedRemoteHead.toLowerCase() !== remote.headSha.toLowerCase()) {
    return { ok: false, reason: 'head_moved_since_preview',
      detail: `预览时 ${String(expectedRemoteHead).slice(0, 12)}，当前 ${String(remote.headSha).slice(0, 12)}——请刷新预览后重试` };
  }

  // 原子认领（新建即 executing；failed/previewed 可重认领；result_uncertain 先对账）
  const claim = await claimOperation(q, {
    idemKey,
    insertSql: `INSERT INTO mu.fix_apply_operation
         (operation_type, status, tenant_id, repo_id, pr_id, run_id, patch_id, patch_digest,
          provider, provider_pr_number, source_branch, target_branch, expected_head,
          robot_identity, idempotency_key, operator_user_id)
       SELECT 'apply_fix','executing',$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14`,
    insertParams: [tenantId, run.repo_id, run.pr_id, runId, patch.patch_id, patch.patch_digest,
      provider, run.provider_pr_number, remote.sourceBranch, remote.targetBranch,
      patch.head_sha, robotIdentity(provider), idemKey, userId],
  });
  if (!claim.claimed) {
    if (claim.state === 'idempotent') {
      return { ok: true, idempotent: true, operation_id: claim.operationId,
        status: 'committed', commit_sha: claim.commitSha };
    }
    if (claim.state === 'requires_reconcile') {
      return { ok: false, reason: 'result_uncertain_requires_reconcile',
        detail: '上次推送结果不明——请先执行「读回对账」定案（证据不足保持 uncertain，不会自动重试）',
        operation_id: claim.operationId ?? null };
    }
    return { ok: false, reason: 'operation_in_progress', operation_id: claim.operationId ?? null };
  }
  const opId = claim.operationId;
  const LOST = { ok: false, reason: 'lost_execution_right',
    detail: '执行权已被接管（对账/其他请求）——本次调用停止；远端状态以读回对账为准' };
  const failTo = async (status, errorCode, detail = null) => {
    const kept = await guardedUpdate(q, opId, ['executing'],
      `status='${status}', error_code=$1, error_detail=$2, updated_at=now()`, [errorCode, detail]);
    return kept; // false=执行权已易主（零覆写）——调用方必须停止
  };

  // 阶段一：准备候选提交（本地）。失败=零远端副作用，且此时未持久化候选——直接 failed。
  // deps.prepareCommit/deps.repoUrlFor 可注入（故障/并发测试指向隔离本地仓；生产用默认）。
  const commitMsg = `fix: apply MergePilot review patch (run ${String(runId).slice(0, 8)}, patch v${patch.patch_version})\n\nPatch digest: ${patch.patch_digest}\nReview run: ${runId}`;
  const doPrepare = deps.prepareCommit ?? prepareCommit;
  const repoUrl = (deps.repoUrlFor ?? ((prov, own, rep) => cloneUrl(prov, own, rep, env)))(provider, run.owner, run.repo_name);
  let prepared = null;
  try {
    prepared = await doPrepare({
      repoUrl,
      headSha: remote.headSha, diffText: patch.diff_text, message: commitMsg,
      credentialToken: provider === 'github'
        ? await import('./remote-write.mjs').then((m) => m.githubInstallationToken(env, run.installation_id)).catch(() => null)
        : null,
    });
  } catch (e) {
    const kept = await failTo('failed', 'prepare_exception', String(e?.message ?? e).slice(0, 160));
    if (!kept) return { ...LOST, operation_id: opId };
    return { ok: false, reason: 'prepare_exception', detail: String(e?.message ?? e).slice(0, 120), operation_id: opId };
  }
  if (!prepared.ok) {
    const kept = await failTo('failed', prepared.reason, prepared.detail ?? prepared.output ?? null);
    if (!kept) return { ...LOST, operation_id: opId };
    return { ok: false, reason: prepared.reason, detail: prepared.detail ?? null,
      output: prepared.output ?? null, operation_id: opId };
  }

  // 写前持久化：候选 SHA + 操作意图 + 绑定信息（进程在此刻退出也能凭记录对账恢复）。
  // 持久化失败=失去执行权——必须停止，不得进入推送阶段。
  const persisted = await guardedUpdate(q, opId, ['executing'],
    `candidate_commit_sha=$1, operation_data=$2::jsonb, updated_at=now()`,
    [prepared.commitSha, JSON.stringify({
      intent: 'push', branch: remote.sourceBranch, expected_head: remote.headSha,
      previous_head: remote.headSha, patch_digest: patch.patch_digest,
      patch_version: patch.patch_version, provider, prepared_at: new Date().toISOString(),
    })]);
  if (!persisted) {
    await import('./patch-applicability.mjs').then((m) => m.discardPrepared(prepared.dir));
    return LOST;
  }

  // 阶段二：推送（真远端写）。可注入 deps.pushPrepared 供并发/故障测试用同步点控制。
  const doPush = deps.pushPrepared ?? pushPrepared;
  const candidateSha = prepared.commitSha;
  let pushResult = null;
  let pushThrew = null;
  try {
    pushResult = await doPush({
      dir: prepared.dir, repoUrl,
      branch: remote.sourceBranch, expectedHead: remote.headSha,
      credentialToken: provider === 'github'
        ? await import('./remote-write.mjs').then((m) => m.githubInstallationToken(env, run.installation_id)).catch(() => null)
        : null,
    });
  } catch (e) {
    pushThrew = String(e?.message ?? e).slice(0, 200);
  }
  prepared = null; // pushPrepared 内部已清理目录（含异常路径 finally）——恢复不依赖本地残留

  if (pushResult && pushResult.ok) {
    const kept = await guardedUpdate(q, opId, ['executing'],
      `status='committed', remote_result_sha=$1, error_code=NULL, error_detail=NULL,
         operation_data = operation_data || $2::jsonb, updated_at=now()`,
      [candidateSha, JSON.stringify({ committed_at: new Date().toISOString() })]);
    if (!kept) return { ...LOST, operation_id: opId };

    // 重审入队（可恢复，best-effort）：成功 push 绝不因入队失败回退或重推。
    const enq = await enqueueReReview(q, { env, run, headSha: candidateSha, userId, deps });
    return {
      ok: true, operation_id: opId, status: 'committed',
      commit_sha: candidateSha, previous_sha: remote.headSha,
      patch_version: patch.patch_version, patch_digest: patch.patch_digest,
      provider, pr_number: run.provider_pr_number, source_branch: remote.sourceBranch,
      review_reenqueue: enq,
      next_step: enq.ok
        ? `已按新 head ${String(candidateSha).slice(0, 12)} 入队重新审查${enq.deduped ? '（既有 run 命中，未重复建）' : ''}；审查与 CI 通过后可预览合并。`
        : '远端分支已推进；重审入队失败——请用「发起审查」手动触发新 head 审查（勿重复应用）。',
    };
  }

  // 推送被确定性拒绝（stale_head 非快进等）→ failed（可安全重试）
  if (pushResult && !pushResult.ok
      && ['stale_head', 'branch_not_found', 'ls_remote_failed'].includes(pushResult.reason)) {
    if (pushResult.reason === 'stale_head') {
      const kept = await failTo('failed', 'stale_head', pushResult.detail ?? null);
      if (!kept) return { ...LOST, operation_id: opId };
      return { ok: false, reason: 'stale_head', detail: pushResult.detail ?? null, operation_id: opId };
    }
    const kept2 = await failTo('failed', pushResult.reason, pushResult.detail ?? pushResult.output ?? null);
    if (!kept2) return { ...LOST, operation_id: opId };
    return { ok: false, reason: pushResult.reason, detail: pushResult.detail ?? null,
      output: pushResult.output ?? null, operation_id: opId };
  }

  // 其余一律 result_uncertain：超时/断连/响应丢失/进程异常——远端状态不可判定，
  // 先读回对账（reconcile）再行动；禁止判失败后盲目重推（可能产生重复提交）。
  const errDetail = pushThrew ?? pushResult?.detail ?? pushResult?.output ?? null;
  const errCode = pushThrew ? 'push_exception' : (pushResult?.reason ?? 'push_failed');
  const keptU = await failTo('result_uncertain', errCode, errDetail);
  if (!keptU) return { ...LOST, operation_id: opId };
  return {
    ok: false, reason: 'result_uncertain', uncertain: true,
    detail: `推送结果不明（${errCode}）：候选提交 ${String(candidateSha).slice(0, 12)} 已登记——请执行「读回对账」确认远端实际状态，勿直接重试`,
    operation_id: opId, candidate_commit_sha: candidateSha,
  };
}

// ── 读回对账与恢复（result_uncertain / executing 卡死的唯一出口）──────

/**
 * 对账单个操作。按操作类型读远端事实，回写确定态；证据不足保持 uncertain 并给原因。
 * 幂等：对已终态（committed/merged/completed/failed）操作为 no-op（返回现状）。
 */
export async function reconcileOperation(pool, { env = process.env, operationId, tenantId, deps = {} }) {
  const q = normQ(pool);
  const repoUrlFor = deps.repoUrlFor ?? ((provider, owner, repo) => cloneUrl(provider, owner, repo, env));
  const op = (await q(
    `SELECT * FROM mu.fix_apply_operation WHERE operation_id=$1 AND tenant_id=$2`,
    [operationId, tenantId])).rows[0] ?? null;
  if (!op) return { ok: false, reason: 'operation_not_found' };
  if (['committed', 'merged', 'completed', 'failed', 'previewed'].includes(op.status)) {
    // 恢复入口：应用已 committed 但重审关联未完成 → 幂等补入队（不重复应用补丁）
    if (op.status === 'committed' && op.operation_type === 'apply_fix'
        && op.operation_data?.review_reenqueue?.ok !== true && deps.enqueueEventSync) {
      const runRow = (await q(`SELECT * FROM mu.review_run WHERE run_id=$1`, [op.run_id])).rows[0] ?? null;
      if (runRow) {
        const enq = await enqueueReReview(q, { env, run: runRow,
          headSha: op.candidate_commit_sha, userId: op.operator_user_id, deps });
        await q(`UPDATE mu.fix_apply_operation
                   SET operation_data = operation_data || $2::jsonb, updated_at=now()
                  WHERE operation_id=$1 AND status='committed'`,
          [operationId, JSON.stringify({ review_reenqueue: enq })]);
        return { ok: true, reconciled: false, status: 'committed', review_reenqueue: enq,
          note: enq.ok ? '重审关联已补齐' : `重审入队仍失败（${enq.reason}）——可再次调用本端点重试` };
      }
    }
    return { ok: true, reconciled: false, status: op.status,
      note: '该操作已是确定态（或无远端写入意图），无需对账' };
  }
  // executing（含写前进程退出）与 result_uncertain 才需要对账。
  // 竞态防护：executing 且 10 分钟内活跃 → 可能仍是活执行者（push 在途），
  // 此刻对账可能把操作释放后原执行者继续写入——拒绝并提示等待；
  // result_uncertain 表示执行者的写入尝试已结束（响应不明），随时可安全对账。
  // （即便发生窗口内的极端竞态，推送阶段的服务端 CAS 也会拒绝第二次写入——不会重复提交。）
  if (op.status === 'executing'
      && (Date.now() - new Date(op.updated_at).getTime()) < 10 * 60 * 1000) {
    return { ok: true, reconciled: false, status: op.status,
      note: '该操作仍在执行窗口内（近 10 分钟有活动）——请稍后再对账；若确认进程已死可等窗口过后重试' };
  }
  const provider = op.provider;
  const ownerRepo = (await q(
    `SELECT rep.owner, rep.name AS repo_name,
            (SELECT rb.installation_id FROM mu.repository_binding rb
              WHERE rb.repo_id = op.repo_id AND rb.tenant_id = op.tenant_id
                AND rb.binding_state='active' LIMIT 1) AS installation_id
       FROM (SELECT repo_id, tenant_id FROM mu.fix_apply_operation WHERE operation_id=$1) op
       JOIN mu.repository rep ON rep.repo_id = op.repo_id`, [operationId])).rows[0] ?? null;
  if (!ownerRepo) return { ok: false, reason: 'repository_not_found' };

  const evidence = { reconciled_at: new Date().toISOString() };
  const data = op.operation_data ?? {};
  const branch = op.source_branch ?? data.branch ?? null;
  const expectedHead = op.expected_head ?? data.expected_head ?? null;
  const candidateSha = op.candidate_commit_sha;

  if (op.operation_type === 'apply_fix') {
    // 候选提交与分支 tip 的关系判定
    if (!branch || !expectedHead) {
      return { ok: false, reason: 'reconcile_missing_bindings', detail: '操作记录缺少分支/head 绑定' };
    }
    if (!candidateSha) {
      // 写前退出（未持久化候选）——查 tip 是否仍为 expected：是 → 未写入任何内容
      const rel = await checkCommitOnBranch({ repoUrl: repoUrlFor(provider, ownerRepo.owner, ownerRepo.repo_name),
        branch, candidateSha: expectedHead,
        credentialToken: await credFor(provider, env, ownerRepo.installation_id) });
      evidence.remote_tip = rel.tip ?? null;
      if (rel.relation === 'tip') {
        await guardedUpdate(q, operationId, ['executing', 'result_uncertain', 'confirmed'],
          `status='failed', error_code='no_write_happened', operation_data = operation_data || $1::jsonb, updated_at=now()`,
          [JSON.stringify({ evidence })]);
        return { ok: true, reconciled: true, status: 'failed',
          note: '进程在远端写入前退出——分支未推进，未产生任何远端提交；可安全重试' };
      }
      return { ok: true, reconciled: false, status: op.status,
        note: `无法判定远端状态（${rel.relation}${rel.reason ? ':' + rel.reason : ''}）——保持 uncertain，稍后重试对账` };
    }
    const rel = await checkCommitOnBranch({ repoUrl: repoUrlFor(provider, ownerRepo.owner, ownerRepo.repo_name),
      branch, candidateSha,
      credentialToken: await credFor(provider, env, ownerRepo.installation_id) });
    evidence.remote_tip = rel.tip ?? null;
    if (rel.relation === 'tip' || rel.relation === 'ancestor') {
      const kept = await guardedUpdate(q, operationId, ['executing', 'result_uncertain', 'confirmed'],
        `status='committed', remote_result_sha=$1, error_code=NULL, error_detail=NULL,
           operation_data = operation_data || $2::jsonb, updated_at=now()`,
        [candidateSha, JSON.stringify({ evidence, reconciled: true })]);
      if (!kept) return { ok: false, reason: 'lost_execution_right',
        detail: '对账定案期间执行权被接管——请刷新后重试对账' };
      // 对账确认应用成功 → 补齐新 head 重审关联（失败持久化，可经本端点幂等重试）
      let enq = { ok: false, reason: 'enqueue_unavailable' };
      const runRow = (await q(`SELECT * FROM mu.review_run WHERE run_id=$1`, [op.run_id])).rows[0] ?? null;
      if (runRow) {
        enq = await enqueueReReview(q, { env, run: runRow, headSha: candidateSha,
          userId: op.operator_user_id, deps });
        await q(`UPDATE mu.fix_apply_operation
                   SET operation_data = operation_data || $2::jsonb, updated_at=now()
                  WHERE operation_id=$1 AND status='committed'`,
          [operationId, JSON.stringify({ review_reenqueue: enq })]);
      }
      return { ok: true, reconciled: true, status: 'committed', commit_sha: candidateSha,
        review_reenqueue: enq,
        note: '读回确认：候选提交已在远端分支上（提交成功，仅响应丢失）'
          + (enq.ok ? '；重审关联已补齐' : `；重审入队失败（${enq.reason}）——可再次调用本端点幂等重试`) };
    }
    if (rel.relation === 'absent') {
      const untouched = rel.tip && expectedHead && rel.tip.toLowerCase() === String(expectedHead).toLowerCase();
      await guardedUpdate(q, operationId, ['executing', 'result_uncertain', 'confirmed'],
        `status='failed', error_code=$2, operation_data = operation_data || $1::jsonb, updated_at=now()`,
        [JSON.stringify({ evidence }), untouched ? 'push_not_landed' : 'branch_advanced_elsewhere']);
      return { ok: true, reconciled: true, status: 'failed',
        note: untouched
          ? '读回确认：分支仍停在预期 head——推送未落地，未产生远端提交；可安全重试'
          : '读回确认：分支被他人推进且候选不在其祖先链上——本次推送未落地；基于新 head 重新走审查流程' };
    }
    return { ok: true, reconciled: false, status: op.status,
      note: `无法判定远端状态（${rel.reason ?? rel.relation}）——保持 uncertain，稍后重试对账` };
  }

  if (op.operation_type === 'merge_pr') {
    let remote = null;
    try {
      remote = await fetchRemotePr(env, { provider, owner: ownerRepo.owner, repo: ownerRepo.repo_name,
        prNumber: op.provider_pr_number, installationId: ownerRepo.installation_id });
    } catch (e) {
      return { ok: true, reconciled: false, status: op.status,
        note: `远端不可达（${String(e?.message ?? e).slice(0, 80)}）——保持 uncertain，稍后重试对账` };
    }
    evidence.pr_state = remote.state; evidence.pr_head = remote.headSha;
    if (remote.state === 'merged') {
      const mergeSha = remote.mergeCommitSha ?? op.remote_result_sha ?? candidateSha;
      await guardedUpdate(q, operationId, ['executing', 'result_uncertain', 'confirmed'],
        `status='merged', remote_result_sha=$1, error_code=NULL, error_detail=NULL,
           operation_data = operation_data || $2::jsonb, updated_at=now()`,
        [mergeSha, JSON.stringify({ evidence, reconciled: true })]);
      return { ok: true, reconciled: true, status: 'merged', merge_sha: mergeSha,
        note: '读回确认：PR 已合并（成功，仅响应丢失）' };
    }
    if (remote.state === 'open') {
      await guardedUpdate(q, operationId, ['executing', 'result_uncertain', 'confirmed'],
        `status='failed', error_code='merge_not_landed', operation_data = operation_data || $1::jsonb, updated_at=now()`,
        [JSON.stringify({ evidence })]);
      return { ok: true, reconciled: true, status: 'failed',
        note: '读回确认：PR 仍开放——合并未发生；资格重核后可重试' };
    }
    // closed（未合并即关闭）
    await guardedUpdate(q, operationId, ['executing', 'result_uncertain', 'confirmed'],
      `status='failed', error_code='pr_closed_unmerged', operation_data = operation_data || $1::jsonb, updated_at=now()`,
      [JSON.stringify({ evidence })]);
    return { ok: true, reconciled: true, status: 'failed', note: '读回确认：PR 已关闭且未合并' };
  }

  return { ok: false, reason: 'unknown_operation_type' };
}

async function credFor(provider, env, installationId) {
  if (provider !== 'github') return null;
  try {
    const m = await import('./remote-write.mjs');
    return await m.githubInstallationToken(env, installationId);
  } catch { return null; }
}

// ── 重审入队（应用成功后关联新 head；webhook 幂等去重）────────────────

/**
 * 按新 head 入队重审。幂等：该 (tenant,repo,pr,head) 已有 run → 不重复（deduped=true）；
 * 已有同 head 的待处理 event_sync job → 不重复入队。
 * 失败可恢复：抛错/失败返回 {ok:false}，不污染 apply 的成功语义（push 已落地绝不回退）。
 */
export async function enqueueReReview(qRaw, { env = process.env, run, headSha, userId, deps = {} }) {
  const q = normQ(qRaw);
  try {
    // 1) 该 head 已有 run（webhook 或上次入队已建）→ 去重
    const exRun = (await q(
      `SELECT run_id, status FROM mu.review_run
        WHERE tenant_id=$1 AND repo_id=$2 AND pr_id=$3 AND head_sha=$4`,
      [run.tenant_id, run.repo_id, run.pr_id, headSha])).rows[0] ?? null;
    if (exRun) return { ok: true, deduped: true, run_id: exRun.run_id };
    // 2) 已有同 head 的 queued/running job → 去重
    const exJob = (await q(
      `SELECT job_id FROM mu.job
        WHERE tenant_id=$1 AND repo_id=$2 AND kind='event_sync'
          AND state IN ('queued','running','locked')
          AND payload->>'head_sha'=$3 LIMIT 1`,
      [run.tenant_id, run.repo_id, headSha])).rows[0] ?? null;
    if (exJob) return { ok: true, deduped: true, job_id: exJob.job_id };
    // 3) 入队（经由 API 层注入的 store.enqueueJob——与 webhook 同一消费单元）
    if (!deps.enqueueEventSync) return { ok: false, reason: 'enqueue_unavailable' };
    const job = await deps.enqueueEventSync({ headSha, run });
    return { ok: true, deduped: false, job_id: job?.job_id ?? null };
  } catch (e) {
    return { ok: false, reason: 'enqueue_failed', detail: String(e?.message ?? e).slice(0, 120) };
  }
}

// ── 统一合并资格门（preview 与 confirm 共用；confirm 重读远端事实）────

/**
 * 判定当前远端事实下的合并资格。全部条件显式表达，阻塞给机器码+人话原因。
 *   conditions.pr_open        PR 开放
 *   conditions.head_known     head 可读
 *   conditions.head_match     head 与期望一致（confirm 绑定预览所示 head）
 *   conditions.mergeable      mergeable 已知且为 true（null/undefined=未知→阻塞）
 *   conditions.review         当前 head 审查证据：pass|missing|pending|blocked_findings
 *   conditions.ci             当前 head 真实 CI：pass|pending|failed|not_configured|unsupported|unknown
 * 平台保护：不由本服务判定/绕过——合并时由平台强制执行（如实标注 delegated_to_platform）。
 */
export async function evaluateMergeEligibility(q, { env = process.env, run, provider, owner,
  repoName, prNumber, installationId, remote, expectedHead = null }) {
  const conditions = {};
  const blocked = [];

  conditions.pr_open = remote.state === 'open';
  if (!conditions.pr_open) blocked.push({ code: 'pr_not_open',
    detail: `PR 状态为 ${remote.state}` });

  conditions.head_known = Boolean(remote.headSha);
  conditions.head_match = Boolean(expectedHead)
    ? Boolean(remote.headSha) && remote.headSha.toLowerCase() === String(expectedHead).toLowerCase()
    : true;
  if (expectedHead && !conditions.head_match) {
    blocked.push({ code: 'head_moved_since_preview',
      detail: `预览时 ${String(expectedHead).slice(0, 12)}，当前 ${String(remote.headSha ?? '').slice(0, 12)}——请刷新预览` });
  }

  // mergeable：未知（null/undefined）不得放行
  conditions.mergeable_known = remote.mergeable === true || remote.mergeable === false;
  conditions.mergeable = remote.mergeable === true;
  if (!conditions.mergeable_known) {
    blocked.push({ code: 'mergeable_unknown', detail: '平台未给出可合并性判定（可能仍在计算）——不予放行' });
  } else if (!conditions.mergeable) {
    blocked.push({ code: 'merge_conflict', detail: 'PR 存在冲突或平台判定不可合并' });
  }

  // 审查证据：当前 head 必须有审查 run，且无未处理 P0/P1（旧 head 审查不可替代）
  const headRun = (await q(
    `SELECT run_id, status FROM mu.review_run
      WHERE tenant_id=$1 AND repo_id=$2 AND pr_id=$3 AND head_sha=$4
      ORDER BY created_at DESC LIMIT 1`,
    [run.tenant_id, run.repo_id, run.pr_id, remote.headSha])).rows[0] ?? null;
  if (!headRun) {
    conditions.review = 'missing';
    blocked.push({ code: 'review_missing_for_head',
      detail: `当前 head ${String(remote.headSha ?? '').slice(0, 12)} 无审查 run——旧 head 的审查与测试结果不能替代新 head 证据；请先完成重审` });
  } else if (headRun.status === 'BLOCKED') {
    conditions.review = 'blocked_run';
    blocked.push({ code: 'review_run_blocked',
      detail: `当前 head 的审查 run ${String(headRun.run_id).slice(0, 8)} 已被阻塞（${headRun.status}）——按既有流程处理后重新审查` });
  } else if (headRun.status === 'FAILED') {
    conditions.review = 'failed_run';
    blocked.push({ code: 'review_run_failed',
      detail: `当前 head 的审查 run ${String(headRun.run_id).slice(0, 8)} 已失败——请重新发起审查` });
  } else if (!['REVIEWED', 'COMPLETED', 'VERIFIED'].includes(headRun.status)) {
    // REVIEWED=审查完成（干净 PR 的合法终态，可与 COMPLETED/VERIFIED 同列——修复 #458 资格门
    // 把干净 PR 永久判 pending 的缺陷）；其余状态（审查中/等审批/修复轮/验证轮）=进行中。
    conditions.review = 'pending';
    blocked.push({ code: 'review_pending',
      detail: `当前 head 的审查 run ${String(headRun.run_id).slice(0, 8)} 尚未完成（${headRun.status}）` });
  } else {
    const p01 = (await q(
      `SELECT count(*)::int c FROM mu.agent_finding
        WHERE run_id=$1 AND severity IN ('P0','P1')`, [headRun.run_id])).rows[0].c;
    if (p01 > 0) {
      conditions.review = 'blocked_findings';
      blocked.push({ code: 'blocking_findings_open',
        detail: `当前 head 审查存在 ${p01} 条 P0/P1 发现——须按既有流程处理（修复或裁定）后方可合并` });
    } else {
      conditions.review = 'pass';
    }
  }

  // 真实 CI：读当前 head 的平台提交状态；读不到/不支持一律阻塞（不冒充、不代判）
  conditions.ci = await readHeadCi(env, { provider, owner, repoName, headSha: remote.headSha,
    installationId });
  if (conditions.ci.state !== 'pass') {
    blocked.push({ code: `ci_${conditions.ci.state}`,
      detail: conditions.ci.reason ?? `当前 head CI 状态：${conditions.ci.state}` });
  }

  conditions.platform_protection = 'delegated_to_platform'; // 平台在合并时强制执行，本服务不绕过不预判

  return {
    eligible: blocked.length === 0,
    conditions,
    blocked,
  };
}

/**
 * head 域 CI 读取。双平台：
 *   GitHub — 双面判定：combined status（status API）+ check runs（GitHub Actions 只产
 *            check runs 不产 status）。两个面都读；任一面失败态=失败，任一面进行中=pending；
 *            两面全空=not_configured；权限不足=unsupported 并报告所需权限（不用 PAT 绕过）；
 *            重跑取最新态（API 语义即最新）；旧 head 结果与本判定无关（按精确 head 读取）。
 *            必需检查清单（branch protection required checks）需 admin 权限才能读——本服务
 *            不预判必需清单，保护规则的强制由平台在合并时执行（delegated_to_platform）。
 *   Gitee  — GET /repos/{o}/{r}/commits/{sha}/status（实测 gitee.com 双形态 404 →
 *            unsupported=能力限制，如实阻塞）。
 * 状态映射：全 success=pass；任一 pending=阻塞；任一 failure/error=阻塞；
 *           无任何状态=not_configured（阻塞）；API 不支持/无权限=unsupported（阻塞）；
 *           网络失败=unknown（阻塞）。
 */
export async function readHeadCi(env, { provider, owner, repoName, headSha, installationId,
  fetchImpl = fetch }) {
  if (!headSha) return { state: 'unknown', reason: 'head 不可读' };
  try {
    if (provider === 'github') {
      // 面 1：combined status（commit statuses）
      const rs = await ghGet(env, installationId,
        `/repos/${owner}/${repoName}/commits/${headSha}/status`, fetchImpl);
      if (rs.status === 403) {
        // 403=无权限（报告所需权限，不绕过）
        return { state: 'unsupported',
          reason: 'GitHub App 缺 Commit statuses (read) 权限——请在 App 设置为该安装授予后重试' };
      }
      if (!rs.ok) return { state: 'unknown', reason: `GitHub status API HTTP ${rs.status}` };
      // 面 2：check runs（Actions/第三方 checks）——完整分页读取。
      // 任何一页读取失败（含 5xx）→ 判定面不完整 → unknown 阻塞，绝不退化为仅 status
      // 判定（status 绿 + check-runs 里藏着失败 = 错误放行，2026-10-15 收口指令点名）。
      const checks = [];
      for (let page = 1; page <= 10; page++) {
        const rc = await ghGet(env, installationId,
          `/repos/${owner}/${repoName}/commits/${headSha}/check-runs?per_page=100&page=${page}`,
          fetchImpl);
        if (rc.status === 403) {
          return { state: 'unsupported',
            reason: 'GitHub App 缺 Checks (read) 权限——请在 App 设置为该安装授予后重试' };
        }
        if (!rc.ok) {
          return { state: 'unknown',
            reason: `GitHub check-runs HTTP ${rc.status}（第 ${page} 页）——判定面不完整，不予放行` };
        }
        const batch = Array.isArray(rc.json?.check_runs) ? rc.json.check_runs : [];
        checks.push(...batch);
        const total = Number(rc.json?.total_count ?? 0);
        if (checks.length >= total || batch.length === 0) break;
      }
      return judgeGithubBothSurfaces(rs.json, checks);
    }
    if (provider === 'gitee') {
      const r = await giteeGet(env, `/repos/${owner}/${repoName}/commits/${headSha}/status`, fetchImpl);
      if (!r.ok) return { state: 'unsupported', reason: `Gitee status API HTTP ${r.status}（当前连接器能力边界）` };
      const statuses = Array.isArray(r.json?.statuses) ? r.json.statuses : [];
      if (!statuses.length) return { state: 'not_configured',
        reason: '该 head 无任何构建状态——仓库未配置 CI 或 CI 未运行' };
      const state = String(r.json?.state ?? '');
      if (state === 'success') return { state: 'pass' };
      if (state === 'pending') return { state: 'pending', reason: 'CI 运行中' };
      return { state: 'failed', reason: `CI 状态 ${state}` };
    }
    return { state: 'unsupported', reason: `平台 ${provider} 不支持 CI 状态读取` };
  } catch (e) {
    return { state: 'unknown', reason: String(e?.message ?? e).slice(0, 80) };
  }
}

// 双面合并判定（checks=调用方完整分页后的数组）：任一失败=failed；任一进行中=pending；
// 任一未知（completed 且无结论——重跑占位/平台异常态）=unknown 阻塞；两面全空=not_configured；
// 否则 pass。conclusion 映射：success/neutral/skipped=通过（skipped=不适用）；其余=失败。
// 重跑语义：check-runs 端点默认 filter=latest——同 name 重跑只返回最新一次，直接采用。
function judgeGithubBothSurfaces(statusJson, checks) {
  const runList = Array.isArray(checks) ? checks : [];
  const statuses = Array.isArray(statusJson?.statuses) ? statusJson.statuses : [];
  if (!runList.length && !statuses.length) {
    return { state: 'not_configured',
      reason: '该 head 无任何 check run 或提交状态——仓库未配置 CI 或 CI 未运行' };
  }
  const PENDING = new Set(['queued', 'in_progress', 'waiting', 'pending']);
  const BAD = new Set(['failure', 'error', 'timed_out', 'action_required', 'cancelled', 'startup_failure']);
  const OK = new Set(['success', 'neutral', 'skipped']);
  let hasBad = false, badLabel = '';
  let hasPending = false, pendingLabel = '';
  let hasUnknown = false, unknownLabel = '';
  for (const c of runList) {
    const st = String(c?.status ?? '');
    const nm = String(c?.name ?? 'check');
    if (PENDING.has(st)) { hasPending = true; pendingLabel = pendingLabel || `${nm} ${st}`; continue; }
    const cc = c?.conclusion == null ? '' : String(c.conclusion);
    if (st === 'completed' && !cc) {
      // completed 且无结论=未知终态——不得当 pass（收口指令点名场景）
      hasUnknown = true; unknownLabel = unknownLabel || `${nm} completed/conclusion=null`;
      continue;
    }
    if (OK.has(cc)) continue;
    if (BAD.has(cc)) { hasBad = true; badLabel = badLabel || `${nm} ${cc}`; continue; }
    // 其余非空结论（未来新增枚举）→ 未知阻塞，不放行
    hasUnknown = true; unknownLabel = unknownLabel || `${nm} conclusion=${cc || '(空)'}`;
  }
  for (const st2 of statuses) {
    const s2 = String(st2?.state ?? '');
    const ctx2 = String(st2?.context ?? 'status');
    if (s2 === 'pending') { hasPending = true; pendingLabel = pendingLabel || `${ctx2} pending`; continue; }
    if (s2 === 'success') continue;
    hasBad = true; badLabel = badLabel || `${ctx2} ${s2}`;
  }
  if (hasBad) return { state: 'failed', reason: `CI 未通过：${badLabel}` };
  if (hasPending) return { state: 'pending', reason: `CI 运行中：${pendingLabel}` };
  if (hasUnknown) return { state: 'unknown', reason: `存在未知结论的检查（${unknownLabel}）——按阻塞处理` };
  return { state: 'pass' };
}

async function ghGet(env, installationId, endpoint, fetchImpl = fetch) {
  const m = await import('./remote-write.mjs');
  const tok = await m.githubInstallationToken(env, installationId);
  const res = await fetchImpl(`https://api.github.com${endpoint}`, {
    headers: { authorization: `Bearer ${tok}`, accept: 'application/vnd.github+json' },
  });
  const json = await res.json().catch(() => null);
  return { ok: res.ok, status: res.status, json };
}

async function giteeGet(env, endpoint, fetchImpl = fetch) {
  const pat = String(env.MU_GITEE_PAT ?? '');
  const res = await fetchImpl(`https://gitee.com/api/v5${endpoint}${endpoint.includes('?') ? '&' : '?'}access_token=${encodeURIComponent(pat)}`);
  const json = await res.json().catch(() => null);
  return { ok: res.ok, status: res.status, json };
}

// ── 预览：合并 PR（资格门可见化）─────────────────────────────────────

export async function previewMergePr(pool, { env = process.env, runId, tenantId }) {
  const q = normQ(pool);
  const run = (await q(RUN_SELECT, [runId, tenantId])).rows[0] ?? null;
  if (!run) return { ok: false, reason: 'run_not_found' };
  const provider = String(run.provider ?? '');
  if (!['gitee', 'github'].includes(provider)) return { ok: false, reason: 'provider_unsupported' };

  let remote = null, remote_error = null;
  try {
    remote = await fetchRemotePr(env, { provider, owner: run.owner, repo: run.repo_name,
      prNumber: run.provider_pr_number, installationId: run.installation_id });
  } catch (e) { remote_error = String(e?.message ?? e).slice(0, 80); }
  if (!remote) return { ok: false, reason: 'remote_unreachable', ...(remote_error ? { detail: remote_error } : {}) };

  const ops = (await q(
    `SELECT operation_id, candidate_commit_sha, remote_result_sha FROM mu.fix_apply_operation
      WHERE run_id=$1 AND operation_type='apply_fix' AND status='committed'
      ORDER BY updated_at DESC LIMIT 1`, [runId])).rows;

  const gate = await evaluateMergeEligibility(q, { env, run, provider, owner: run.owner,
    repoName: run.repo_name, prNumber: run.provider_pr_number,
    installationId: run.installation_id, remote });

  return {
    ok: true, action: 'merge_pr',
    target: {
      provider, repo: `${run.owner}/${run.repo_name}`, pr_number: run.provider_pr_number,
      source_branch: remote.sourceBranch, target_branch: remote.targetBranch,
      head: remote.headSha,
    },
    merge_conditions: {
      pr_open: gate.conditions.pr_open,
      mergeable_known: gate.conditions.mergeable_known,
      mergeable: gate.conditions.mergeable,
      head_known: gate.conditions.head_known,
      review: gate.conditions.review,
      ci: gate.conditions.ci,
      platform_protection: gate.conditions.platform_protection,
    },
    blocked: gate.blocked,
    applied_commit: ops[0]?.candidate_commit_sha ?? null,
    robot_identity: robotIdentity(provider),
    can_merge: gate.eligible,
    blocked_reason: gate.eligible ? null : (gate.blocked[0]?.code ?? 'not_eligible'),
    confirm_binds: { expected_head: remote.headSha },
    operation_note: '确认后将以机器人身份合并此 PR 到目标分支。合并不可由本服务自动撤销。',
  };
}

// ── 确认：合并 PR（资格门重核 + 原子认领 + 结果不明对账）──────────────

export async function confirmMergePr(pool, { env = process.env, runId, tenantId, userId,
  confirm = false, expectedHead = null, mergeMethod = 'merge', deps = {} }) {
  const q = normQ(pool);
  if (confirm !== true) return { ok: false, reason: 'confirm_required' };
  if (!userId) return { ok: false, reason: 'operator_required' };
  if (!expectedHead) {
    return { ok: false, reason: 'invalid_confirm_fields',
      detail: '缺少必要确认字段（expected_head）——请从预览发起确认' };
  }
  const run = (await q(RUN_SELECT, [runId, tenantId])).rows[0] ?? null;
  if (!run) return { ok: false, reason: 'run_not_found' };
  const provider = String(run.provider ?? '');
  if (!['gitee', 'github'].includes(provider)) return { ok: false, reason: 'provider_unsupported' };

  let remote = null;
  try {
    remote = await fetchRemotePr(env, { provider, owner: run.owner, repo: run.repo_name,
      prNumber: run.provider_pr_number, installationId: run.installation_id });
  } catch (e) {
    return { ok: false, reason: 'remote_unreachable', detail: String(e?.message ?? e).slice(0, 80) };
  }

  // 幂等先于资格门：PR 已合并后重复确认返回既有结果（不得被 pr_open 提前拒绝）
  const idemKey = `${runId}:merge_pr:${String(remote.headSha ?? '').toLowerCase()}`;
  const existing = (await q(
    `SELECT operation_id, status, remote_result_sha FROM mu.fix_apply_operation
      WHERE idempotency_key=$1`, [idemKey])).rows[0] ?? null;
  if (existing && ['merged', 'completed'].includes(existing.status)) {
    return { ok: true, idempotent: true, operation_id: existing.operation_id,
      status: existing.status, merge_sha: existing.remote_result_sha };
  }

  // 预览绑定：请求 head 与远端现状一致
  if (!remote.headSha || expectedHead.toLowerCase() !== remote.headSha.toLowerCase()) {
    return { ok: false, reason: 'head_moved_since_preview',
      detail: `预览时 ${String(expectedHead).slice(0, 12)}，当前 ${String(remote.headSha ?? '').slice(0, 12)}——请刷新预览后重试` };
  }

  // 统一资格门（confirm 重读的远端事实即上方 fetch 的 remote）
  const gate = await evaluateMergeEligibility(q, { env, run, provider, owner: run.owner,
    repoName: run.repo_name, prNumber: run.provider_pr_number,
    installationId: run.installation_id, remote, expectedHead });
  if (!gate.eligible) {
    return { ok: false, reason: 'merge_not_eligible', blocked: gate.blocked,
      detail: gate.blocked.map((b) => `${b.code}: ${b.detail}`).join('；') };
  }

  // 原子认领（重复 in-flight 拒绝；failed/result_uncertain 可重认领）
  const claim = await claimOperation(q, {
    idemKey,
    insertSql: `INSERT INTO mu.fix_apply_operation
         (operation_type, status, tenant_id, repo_id, pr_id, run_id,
          provider, provider_pr_number, source_branch, target_branch, expected_head,
          robot_identity, idempotency_key, operator_user_id)
       SELECT 'merge_pr','executing',$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12`,
    insertParams: [tenantId, run.repo_id, run.pr_id, runId, provider, run.provider_pr_number,
      remote.sourceBranch, remote.targetBranch, remote.headSha,
      robotIdentity(provider), idemKey, userId],
  });
  if (!claim.claimed) {
    if (claim.state === 'idempotent') {
      return { ok: true, idempotent: true, operation_id: claim.operationId,
        status: 'merged', merge_sha: claim.commitSha };
    }
    if (claim.state === 'requires_reconcile') {
      return { ok: false, reason: 'result_uncertain_requires_reconcile',
        detail: '上次合并结果不明——请先执行「读回对账」按 PR 实际状态定案',
        operation_id: claim.operationId ?? null };
    }
    return { ok: false, reason: 'operation_in_progress', operation_id: claim.operationId ?? null };
  }
  const opId = claim.operationId;

  // 合并（可注入 deps.merge 供测试控制响应丢失）
  const doMerge = deps.merge ?? platformMergePR;
  let result = null;
  let mergeThrew = null;
  try {
    result = await doMerge({
      env, provider, owner: run.owner, repo: run.repo_name,
      prNumber: run.provider_pr_number, expectedHead: remote.headSha, mergeMethod,
      commitTitle: `Merge pull request #${run.provider_pr_number} (via MergePilot)`,
      ...(provider === 'github' ? { installationId: run.installation_id } : {}),
    });
  } catch (e) {
    mergeThrew = String(e?.message ?? e).slice(0, 200);
  }

  if (result && result.ok) {
    const kept = await guardedUpdate(q, opId, ['executing'],
      `status='merged', remote_result_sha=$1, error_code=NULL, error_detail=NULL,
         operation_data = operation_data || $2::jsonb, updated_at=now()`,
      [result.mergeSha, JSON.stringify({ merged_at: new Date().toISOString() })]);
    if (!kept) return { ok: false, reason: 'operation_in_progress', operation_id: opId };
    return { ok: true, operation_id: opId, status: 'merged',
      merge_sha: result.mergeSha, provider, pr_number: run.provider_pr_number };
  }

  // 确定性拒绝（平台保护门/条件不符）→ failed（如实保留平台拒绝原因）
  if (result && !result.ok && !UNCERTAIN_CLASS.test(String(result.detail ?? ''))) {
    await guardedUpdate(q, opId, ['executing'],
      `status='failed', error_code=$1, error_detail=$2, updated_at=now()`,
      [result.reason ?? 'merge_failed', result.detail ?? null]);
    return { ok: false, reason: result.reason ?? 'merge_failed',
      detail: result.detail ?? null, operation_id: opId };
  }

  // 结果不明（超时/断连/响应丢失）→ result_uncertain + 读回对账入口
  const errCode = mergeThrew ? 'merge_exception' : (result?.reason ?? 'merge_failed');
  await guardedUpdate(q, opId, ['executing'],
    `status='result_uncertain', error_code=$1, error_detail=$2, updated_at=now()`,
    [errCode, mergeThrew ?? result?.detail ?? null]);
  return {
    ok: false, reason: 'result_uncertain', uncertain: true,
    detail: `合并结果不明（${errCode}）——请执行「读回对账」按 PR 实际状态定案，勿盲目重试`,
    operation_id: opId,
  };
}

