// fix-apply.mjs — 用户授权远端写入服务层：应用修复补丁到 PR 源分支 + 合并 PR。
//
// 职责分离（本波红线）：
//  * 模型与四角色链只生成与验证建议（dry-run，零远端写）——本模块只在用户显式确认后
//    执行实际远端写入，凭据只从部署级 env / installation token 注入；
//  * 每次操作绑定 mu.fix_apply_operation 持久化记录；幂等键=run_id+op_type+patch_digest
//    （apply）或 run_id+merge_pr+expected_head（merge）——重复点击/并发/重启不产生重复远端写；
//  * 远端条件更新（expected head / force=false）——分支已推进时拒绝，不覆盖他人提交；
//  * 物化失败（补丁在当前 head 不可应用）→ 中止，零远端副作用；
//  * 结果不确定（提交已建但 ref 更新失败等）→ 如实标记 result_uncertain，先查验再行动；
//  * 不做：自动提交、自动合并、required check 操作、代批审批。
//
// 凭据纪律：Gitee PAT / GitHub installation token 只存在于本次调用的内存栈中；
// 不进前端、不进日志、不进数据库记录、不写临时 Git 配置（物化用 URL 参数瞬时传递）。

import { platformCreateCommit, platformMergePR } from './remote-write.mjs';
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

/**
 * 预览应用修复（只读——零远端写）。返回目标信息+补丁验证状态+机器人身份，供用户确认。
 */
export async function previewApplyFix(pool, { env = process.env, runId, tenantId }) {
  const q = normQ(pool);
  const run = (await q(RUN_SELECT, [runId, tenantId])).rows[0] ?? null;
  if (!run) return { ok: false, reason: 'run_not_found' };
  const provider = String(run.provider ?? '');
  if (!['gitee', 'github'].includes(provider)) return { ok: false, reason: 'provider_unsupported' };

  const patch = (await q(ACTIVE_PATCH_SELECT, [runId])).rows[0] ?? null;
  if (!patch) return { ok: false, reason: 'no_active_patch' };

  // 远端 PR 当前状态（凭据与发布链同源）
  let remote = null, remote_error = null;
  try {
    remote = await fetchRemotePr(env, { provider, owner: run.owner, repo: run.repo_name,
      prNumber: run.provider_pr_number, installationId: run.installation_id });
  } catch (e) { remote_error = String(e?.message ?? e).slice(0, 80); }

  const headMatch = Boolean(remote?.headSha)
    && remote.headSha.toLowerCase() === String(patch.head_sha).toLowerCase();

  const ga = patch.applicability ?? {};
  const tv = patch.test_verification ?? null;

  // 本 run 已发生的 apply 操作（含成功提交）——如实呈现
  const ops = (await q(
    `SELECT operation_id, status, candidate_commit_sha, remote_result_sha, error_code,
            created_at, updated_at
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
  const can_apply = Boolean(remote) && headMatch && !alreadyApplied;

  return {
    ok: true, action: 'apply_fix',
    target: {
      provider, repo: `${run.owner}/${run.repo_name}`, pr_number: run.provider_pr_number,
      source_branch: remote?.sourceBranch ?? null, target_branch: remote?.targetBranch ?? null,
      pr_state: remote?.state ?? null,
      review_head: patch.head_sha, remote_head: remote?.headSha ?? null, head_match: headMatch,
      ...(remote_error ? { remote_error } : {}),
    },
    patch: patchInfo,
    robot_identity: robotIdentity(provider),
    previous_operations: ops.map((o) => ({
      status: o.status, commit: o.candidate_commit_sha ?? o.remote_result_sha ?? null,
      error_code: o.error_code ?? null, updated_at: o.updated_at,
    })),
    can_apply,
    blocked_reason: can_apply ? null
      : !remote ? 'remote_unreachable'
      : !headMatch ? 'head_moved'
      : alreadyApplied ? 'already_applied'
      : null,
    operation_note: '确认后将在 PR 源分支新增一个提交（补丁的全部变更）。不合并 PR；合并需要再次单独确认。',
    rollback_note: '服务回滚不会撤销已发生的远端提交；回退需在平台上手动 revert。',
  };
}

/**
 * 确认应用修复（执行远端写入）。服务端重核 head/digest/幂等，再物化补丁并原子提交。
 */
export async function confirmApplyFix(pool, { env = process.env, runId, tenantId, userId,
  confirm = false, expectedRemoteHead = null }) {
  const q = normQ(pool);
  if (confirm !== true) return { ok: false, reason: 'confirm_required' };
  if (!userId) return { ok: false, reason: 'operator_required' };
  const run = (await q(RUN_SELECT, [runId, tenantId])).rows[0] ?? null;
  if (!run) return { ok: false, reason: 'run_not_found' };
  const provider = String(run.provider ?? '');
  if (!['gitee', 'github'].includes(provider)) return { ok: false, reason: 'provider_unsupported' };

  const patch = (await q(ACTIVE_PATCH_SELECT, [runId])).rows[0] ?? null;
  if (!patch) return { ok: false, reason: 'no_active_patch' };

  // 幂等：同 run+同 digest 已成功提交过 → 直接返回既有结果（不重复远端写）
  const idemKey = `${runId}:apply_fix:${patch.patch_digest}`;
  const existing = (await q(
    `SELECT operation_id, status, candidate_commit_sha FROM mu.fix_apply_operation
      WHERE idempotency_key=$1`, [idemKey])).rows[0] ?? null;
  if (existing && ['committed', 'merged', 'completed'].includes(existing.status)) {
    return { ok: true, idempotent: true, operation_id: existing.operation_id,
      status: existing.status, commit_sha: existing.candidate_commit_sha };
  }
  // 进行中并发 → 拒绝；failed/result_uncertain → 允许人工查验后重试（记录复用）
  if (existing && ['executing', 'confirmed'].includes(existing.status)) {
    return { ok: false, reason: 'operation_in_progress', operation_id: existing.operation_id };
  }

  // 操作记录先行（confirmed——用户已确认，执行待始；UNIQUE 幂等键兜底并发）。
  // 先落记录再执行：被拒的尝试（陈旧 head/漂移）也留下可审计痕迹。
  const opId = existing?.operation_id
    ?? (await q(
      `INSERT INTO mu.fix_apply_operation
         (operation_type, status, tenant_id, repo_id, pr_id, run_id, patch_id, patch_digest,
          provider, provider_pr_number, source_branch, target_branch, expected_head,
          robot_identity, idempotency_key, operator_user_id)
       VALUES ('apply_fix','confirmed',$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
       ON CONFLICT (idempotency_key) DO UPDATE SET status='confirmed', updated_at=now()
       RETURNING operation_id`,
      [tenantId, run.repo_id, run.pr_id, runId, patch.patch_id, patch.patch_digest,
       provider, run.provider_pr_number, null, null,
       patch.head_sha, robotIdentity(provider), idemKey, userId])).rows[0].operation_id;
  const failOp = async (errorCode, detail = null) => {
    await q(`UPDATE mu.fix_apply_operation SET status='failed', error_code=$2, error_detail=$3,
               updated_at=now() WHERE operation_id=$1`, [opId, errorCode, detail]);
  };

  // 远端 head 复核：必须仍在补丁目标 head（用户确认的是"把这个补丁打到这个 head"）
  let remote = null;
  try {
    remote = await fetchRemotePr(env, { provider, owner: run.owner, repo: run.repo_name,
      prNumber: run.provider_pr_number, installationId: run.installation_id });
  } catch (e) {
    await failOp('remote_unreachable', String(e?.message ?? e).slice(0, 120));
    return { ok: false, reason: 'remote_unreachable', detail: String(e?.message ?? e).slice(0, 80) };
  }
  if (!remote.headSha || remote.headSha.toLowerCase() !== String(patch.head_sha).toLowerCase()) {
    await failOp('stale_head',
      `expected=${String(patch.head_sha).slice(0, 12)} actual=${String(remote.headSha ?? '').slice(0, 12)}`);
    return { ok: false, reason: 'stale_head',
      detail: `补丁基于 ${String(patch.head_sha).slice(0, 12)}，远端当前 ${String(remote.headSha).slice(0, 12)}` };
  }
  // 双确认：请求携带的 expectedRemoteHead 与服务端所见一致（防预览→确认窗口漂移）
  if (expectedRemoteHead && expectedRemoteHead.toLowerCase() !== remote.headSha.toLowerCase()) {
    await failOp('head_moved_since_preview',
      `preview=${String(expectedRemoteHead).slice(0, 12)} actual=${String(remote.headSha).slice(0, 12)}`);
    return { ok: false, reason: 'head_moved_since_preview',
      detail: `预览时 ${String(expectedRemoteHead).slice(0, 12)}，当前 ${String(remote.headSha).slice(0, 12)}——请刷新预览后重试` };
  }

  await q(`UPDATE mu.fix_apply_operation
             SET status='executing', source_branch=$2, target_branch=$3, updated_at=now()
            WHERE operation_id=$1`, [opId, remote.sourceBranch, remote.targetBranch]);

  // 推送式远端提交（pushCommit）：隔离副本检出准确 head → 真 apply → 本地提交 →
  // git push（无 --force）。物化失败/非快进均零远端副作用；条件更新在服务端强制生效。
  const commitMsg = `fix: apply MergePilot review patch (run ${String(runId).slice(0, 8)}, patch v${patch.patch_version})\n\nPatch digest: ${patch.patch_digest}\nReview run: ${runId}`;
  const result = await platformCreateCommit({
    env, provider, owner: run.owner, repo: run.repo_name, branch: remote.sourceBranch,
    expectedHead: remote.headSha, diffText: patch.diff_text, message: commitMsg,
    ...(provider === 'github' ? { installationId: run.installation_id } : {}),
  });

  if (!result.ok) {
    // push 失败的语义由 pushCommit 归类：stale_head=并发推进零写入；其余=失败（可重试）
    await q(`UPDATE mu.fix_apply_operation SET status=$2, error_code=$3, error_detail=$4,
               updated_at=now() WHERE operation_id=$1`,
      [opId, 'failed', result.reason, result.detail ?? result.output ?? null]);
    return { ok: false, reason: result.reason, uncertain: false,
      detail: result.detail ?? null, output: result.output ?? null, operation_id: opId };
  }

  await q(`UPDATE mu.fix_apply_operation
             SET status='committed', candidate_commit_sha=$2, remote_result_sha=$2,
                 error_code=NULL, error_detail=NULL, updated_at=now()
            WHERE operation_id=$1`, [opId, result.commitSha]);
  return {
    ok: true, operation_id: opId, status: 'committed',
    commit_sha: result.commitSha, previous_sha: result.previousSha,
    patch_version: patch.patch_version, patch_digest: patch.patch_digest,
    provider, pr_number: run.provider_pr_number, source_branch: remote.sourceBranch,
    next_step: '远端分支已推进；请在平台触发重新审查/CI，之后可再次预览并确认合并。',
  };
}

/**
 * 预览合并 PR（只读）。呈现合并条件（开放性/可合并性/head），供用户确认。
 */
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
    `SELECT candidate_commit_sha FROM mu.fix_apply_operation
      WHERE run_id=$1 AND operation_type='apply_fix' AND status='committed'
      ORDER BY updated_at DESC LIMIT 1`, [runId])).rows;

  const conditions = {
    pr_open: remote.state === 'open',
    mergeable: remote.mergeable !== false,
    head_known: Boolean(remote.headSha),
    ci_status: 'not_configured', // 首版不读 CI 状态——如实标注，不冒充"CI 通过"
    branch_protection: 'not_bypassed', // 平台自身保护规则照常生效，本服务不绕过
  };
  const can_merge = conditions.pr_open && conditions.mergeable && conditions.head_known;

  return {
    ok: true, action: 'merge_pr',
    target: {
      provider, repo: `${run.owner}/${run.repo_name}`, pr_number: run.provider_pr_number,
      source_branch: remote.sourceBranch, target_branch: remote.targetBranch,
      head: remote.headSha,
    },
    merge_conditions: conditions,
    applied_commit: ops[0]?.candidate_commit_sha ?? null,
    robot_identity: robotIdentity(provider),
    can_merge,
    blocked_reason: can_merge ? null
      : !conditions.pr_open ? 'pr_not_open'
      : !conditions.mergeable ? 'pr_not_mergeable'
      : 'head_unknown',
    operation_note: '确认后将以机器人身份合并此 PR 到目标分支。合并不可由本服务自动撤销。',
  };
}

/**
 * 确认合并 PR。服务端重核 PR 状态与 head，条件合并（sha 匹配才允许）。
 */
export async function confirmMergePr(pool, { env = process.env, runId, tenantId, userId,
  confirm = false, expectedHead = null, mergeMethod = 'merge' }) {
  const q = normQ(pool);
  if (confirm !== true) return { ok: false, reason: 'confirm_required' };
  if (!userId) return { ok: false, reason: 'operator_required' };
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
  if (remote.state !== 'open') return { ok: false, reason: 'pr_not_open' };
  if (!remote.headSha) return { ok: false, reason: 'head_unknown' };
  if (expectedHead && expectedHead.toLowerCase() !== remote.headSha.toLowerCase()) {
    return { ok: false, reason: 'head_moved_since_preview',
      detail: `预览时 ${String(expectedHead).slice(0, 12)}，当前 ${String(remote.headSha).slice(0, 12)}——请刷新预览后重试` };
  }

  const idemKey = `${runId}:merge_pr:${remote.headSha.toLowerCase()}`;
  const existing = (await q(
    `SELECT operation_id, status, remote_result_sha FROM mu.fix_apply_operation
      WHERE idempotency_key=$1`, [idemKey])).rows[0] ?? null;
  if (existing && ['merged', 'completed'].includes(existing.status)) {
    return { ok: true, idempotent: true, operation_id: existing.operation_id,
      status: existing.status, merge_sha: existing.remote_result_sha };
  }
  if (existing && ['executing', 'confirmed'].includes(existing.status)) {
    return { ok: false, reason: 'operation_in_progress', operation_id: existing.operation_id };
  }

  const opId = existing?.operation_id
    ?? (await q(
      `INSERT INTO mu.fix_apply_operation
         (operation_type, status, tenant_id, repo_id, pr_id, run_id,
          provider, provider_pr_number, source_branch, target_branch, expected_head,
          robot_identity, idempotency_key, operator_user_id)
       VALUES ('merge_pr','executing',$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
       ON CONFLICT (idempotency_key) DO UPDATE SET status='executing', updated_at=now()
       RETURNING operation_id`,
      [tenantId, run.repo_id, run.pr_id, runId, provider, run.provider_pr_number,
       remote.sourceBranch, remote.targetBranch, remote.headSha,
       robotIdentity(provider), idemKey, userId])).rows[0].operation_id;

  const result = await platformMergePR({
    env, provider, owner: run.owner, repo: run.repo_name,
    prNumber: run.provider_pr_number, expectedHead: remote.headSha, mergeMethod,
    commitTitle: `Merge pull request #${run.provider_pr_number} (via MergePilot)`,
    ...(provider === 'github' ? { installationId: run.installation_id } : {}),
  });

  if (!result.ok) {
    // 合并 API 响应均为确定性结果（平台拒绝/条件不符）——记 failed，不虚标 result_uncertain
    // （真实平台实测 2026-10-15：保护门拒绝如实呈现"未通过设置的审查/测试"）
    await q(`UPDATE mu.fix_apply_operation SET status='failed', error_code=$2, error_detail=$3,
               updated_at=now() WHERE operation_id=$1`,
      [opId, result.reason, result.detail ?? null]);
    return { ok: false, reason: result.reason, detail: result.detail ?? null, operation_id: opId };
  }

  await q(`UPDATE mu.fix_apply_operation
             SET status='merged', remote_result_sha=$2,
                 error_code=NULL, error_detail=NULL, updated_at=now() WHERE operation_id=$1`,
    [opId, result.mergeSha]);
  return { ok: true, operation_id: opId, status: 'merged',
    merge_sha: result.mergeSha, provider, pr_number: run.provider_pr_number };
}
