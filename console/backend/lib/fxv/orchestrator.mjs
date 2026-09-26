// fxv/orchestrator.mjs — Review→Finding→Ticket→Fixer→Test→Verifier→Audit 生产编排状态机。
// 原则：
//   * 每次转迁持久化 + 全量审计（fxv.audit_events）
//   * 绑定不变量：repo/branch/head/patch_digest 与立案行一致，任何漂移=终态失败
//   * 幂等：重复请求同转迁返回 idempotent，不重复副作用
//   * fail-closed：handler 缺失/异常/超时 → MANUAL_WAIT 或 ERROR_FATAL，绝不伪成功
//   * dry-run 默认开启；GitHub 写入必须显式授权（gate 消费一次性 grant）
//   * 重启恢复：非终态 attempts 由 recover() 重新校验 TTL/head 并续跑或转 MANUAL_WAIT
import { assertTargetAllowed } from './config.mjs';
import { consumePipelineGrant, makeWriteGate } from './github-gate.mjs';

export const STATES = Object.freeze({
  FILED: 'FILED',                          // 票据立案（绑定字段已固化）
  AWAITING_APPROVAL: 'AWAITING_APPROVAL',  // 人工等待：审批
  APPROVED: 'APPROVED',                    // 审批通过
  REJECTED: 'REJECTED',                    // 人工终态：审批拒绝
  PATCH_GENERATING: 'PATCH_GENERATING',    // Fixer 生成补丁
  PATCH_READY: 'PATCH_READY',
  DRY_RUN_APPLY: 'DRY_RUN_APPLY',          // 隔离工作区应用
  DRY_RUN_VERIFIED: 'DRY_RUN_VERIFIED',    // 测试通过（隔离验证）
  DRY_RUN_COMPLETE: 'DRY_RUN_COMPLETE',    // dry-run 终态（默认路径）
  AWAITING_GITHUB_GRANT: 'AWAITING_GITHUB_GRANT', // 人工等待：写入授权
  GRANTED: 'GRANTED',
  COMMITTING: 'COMMITTING',                // 提交+推送（真实写）
  COMMITTED: 'COMMITTED',
  TEST_RUNNING: 'TEST_RUNNING',            // 推送后 CI/harness 复核
  VERIFIED: 'VERIFIED',                    // 成功终态
  EXPIRED: 'EXPIRED',                      // 审批/流程 TTL 到期终态
  STALE_HEAD: 'STALE_HEAD',                // head 漂移终态
  DIGEST_DRIFT: 'DIGEST_DRIFT',            // 补丁摘要漂移终态
  TEST_FAILED: 'TEST_FAILED',              // 测试失败终态
  ROLLED_BACK: 'ROLLED_BACK',              // 回滚完成终态
  TIMEOUT: 'TIMEOUT',                      // 步骤超时终态
  ERROR_FATAL: 'ERROR_FATAL',              // 不可恢复错误终态
  MANUAL_WAIT: 'MANUAL_WAIT',              // 人工等待（handler 缺失/恢复需人工）
});

export const TERMINAL = new Set([STATES.REJECTED, STATES.DRY_RUN_COMPLETE, STATES.VERIFIED,
  STATES.EXPIRED, STATES.STALE_HEAD, STATES.DIGEST_DRIFT, STATES.TEST_FAILED,
  STATES.ROLLED_BACK, STATES.TIMEOUT, STATES.ERROR_FATAL]);

// 合法转迁表（from -> Set(to)）。终态不可再转（除 ROLLED_BACK 由人工触发）。
export const LEGAL = buildLegal();
function buildLegal() {
  const m = new Map();
  const add = (f, ...ts) => { if (!m.has(f)) m.set(f, new Set()); for (const t of ts) m.get(f).add(t); };
  add(STATES.FILED, STATES.AWAITING_APPROVAL);
  add(STATES.AWAITING_APPROVAL, STATES.APPROVED, STATES.REJECTED, STATES.EXPIRED, STATES.MANUAL_WAIT);
  add(STATES.APPROVED, STATES.PATCH_GENERATING, STATES.MANUAL_WAIT, STATES.EXPIRED);
  add(STATES.PATCH_GENERATING, STATES.PATCH_READY, STATES.ERROR_FATAL, STATES.TIMEOUT, STATES.MANUAL_WAIT);
  add(STATES.PATCH_READY, STATES.DRY_RUN_APPLY, STATES.STALE_HEAD, STATES.DIGEST_DRIFT, STATES.MANUAL_WAIT);
  add(STATES.DRY_RUN_APPLY, STATES.DRY_RUN_VERIFIED, STATES.STALE_HEAD, STATES.DIGEST_DRIFT,
      STATES.TEST_FAILED, STATES.ERROR_FATAL, STATES.TIMEOUT, STATES.MANUAL_WAIT);
  add(STATES.DRY_RUN_VERIFIED, STATES.DRY_RUN_COMPLETE, STATES.AWAITING_GITHUB_GRANT, STATES.MANUAL_WAIT);
  add(STATES.AWAITING_GITHUB_GRANT, STATES.GRANTED, STATES.EXPIRED, STATES.DRY_RUN_COMPLETE, STATES.MANUAL_WAIT);
  add(STATES.GRANTED, STATES.COMMITTING, STATES.STALE_HEAD, STATES.MANUAL_WAIT, STATES.EXPIRED);
  add(STATES.COMMITTING, STATES.COMMITTED, STATES.STALE_HEAD, STATES.DIGEST_DRIFT,
      STATES.ERROR_FATAL, STATES.TIMEOUT, STATES.MANUAL_WAIT);
  add(STATES.COMMITTED, STATES.TEST_RUNNING, STATES.ROLLED_BACK, STATES.MANUAL_WAIT);
  add(STATES.TEST_RUNNING, STATES.VERIFIED, STATES.TEST_FAILED, STATES.TIMEOUT,
      STATES.ERROR_FATAL, STATES.MANUAL_WAIT, STATES.ROLLED_BACK);
  add(STATES.MANUAL_WAIT, STATES.AWAITING_APPROVAL, STATES.APPROVED, STATES.PATCH_GENERATING,
      STATES.PATCH_READY, STATES.DRY_RUN_APPLY, STATES.COMMITTING, STATES.ROLLED_BACK, STATES.EXPIRED);
  return m;
}

export class TransitionConflict extends Error {
  constructor(msg, info) { super(msg); this.name = 'TransitionConflict'; this.info = info; }
}

// 单次转迁：合法表校验 + 绑定不变量 + 乐观并发 + 审计；幂等
export async function transition(store, { attemptId, from, to, actor = 'system', reason, meta }) {
  const cur = await store.getAttempt(attemptId);
  if (!cur) throw new TransitionConflict('attempt_not_found', { attemptId });
  if (cur.state === to) return { ok: true, idempotent: true, attempt: cur }; // 幂等重放
  const SAFETY_TERMINALS = new Set([STATES.EXPIRED, STATES.STALE_HEAD, STATES.DIGEST_DRIFT]);
  const allowed = LEGAL.get(cur.state) ?? new Set();
  const legal = allowed.has(to) || (SAFETY_TERMINALS.has(to) && !TERMINAL.has(cur.state));
  if (!legal) {
    throw new TransitionConflict('illegal_transition', { from: cur.state, to });
  }
  if (from !== cur.state) {
    throw new TransitionConflict('expected_state_mismatch', { expected: from, actual: cur.state });
  }
  // 绑定不变量：转迁携带的 head/digest 必须与立案一致
  if (meta?.head_sha && meta.head_sha !== cur.base_head_sha) {
    await toTerminal(store, cur, STATES.STALE_HEAD, actor, `head drift: filed=${cur.base_head_sha} now=${meta.head_sha}`, meta);
    throw new TransitionConflict('stale_head', { filed: cur.base_head_sha, now: meta.head_sha });
  }
  if (meta?.patch_digest && meta.patch_digest !== cur.patch_digest) {
    await toTerminal(store, cur, STATES.DIGEST_DRIFT, actor, `digest drift: filed=${cur.patch_digest} now=${meta.patch_digest}`, meta);
    throw new TransitionConflict('digest_drift', { filed: cur.patch_digest, now: meta.patch_digest });
  }
  const updated = await store.compareAndSetState(attemptId, from, to, { last_reason: reason ?? null });
  if (!updated) { // 并发竞态：另一执行者已转
    const now = await store.getAttempt(attemptId);
    if (now?.state === to) return { ok: true, idempotent: true, attempt: now };
    throw new TransitionConflict('concurrent_claim_lost', { attemptId, from, to, actual: now?.state });
  }
  await store.recordEvent({ attempt_id: attemptId, kind: 'TRANSITION', from_state: from, to_state: to, actor, reason, meta });
  return { ok: true, idempotent: false, attempt: updated };
}

async function toTerminal(store, cur, state, actor, reason, meta) {
  const updated = await store.compareAndSetState(cur.attempt_id, cur.state, state, { last_reason: reason });
  if (updated) await store.recordEvent({ attempt_id: cur.attempt_id, kind: 'TERMINAL', from_state: cur.state, to_state: state, actor, reason, meta });
  return state;
}

// ── 管线执行器：从当前状态推进直到人工等待/终态 ──
// handlers（全部可选，缺省=fail-closed 到 MANUAL_WAIT）：
//   generatePatch(ctx) -> { patch_text, patch_digest }
//   dryRunApply(ctx)   -> { applied: true, workspace }
//   runTests(ctx)      -> { passed: bool, output }
//   commitPush(ctx, gate) -> { committed_sha }   // gate 强制授权检查
//   rollback(ctx)      -> { rolled_back: true }
//   currentHead(repo, branch) -> sha | null      // 恢复期 stale 校验
export async function runPipeline(store, cfg, handlers, attemptId, actor = 'fxv-runner') {
  const ctx = () => store.getAttempt(attemptId);
  let cur = await ctx();
  if (!cur) throw new TransitionConflict('attempt_not_found', { attemptId });
  const allow = assertTargetAllowed(cfg, cur.repo, cur.branch);
  if (!allow.ok) return toTerminal(store, cur, STATES.ERROR_FATAL, actor, allow.reason, { ...allow, repo: undefined, allowlist: allow.allowlist?.length });

  const step = async (name, from, to, fn, opts = {}) => {
    cur = await ctx();
    if (cur.state !== from) return cur.state; // 已被并发推进/终态
    if (TERMINAL.has(cur.state)) return cur.state;
    await transition(store, { attemptId, from, to, actor, reason: `step:${name}` });
    try {
      const result = await withTimeout(fn(cur), opts.timeoutMs ?? cfg.timeouts.step_ms, name);
      cur = await ctx();
      return result;
    } catch (e) {
      const kind = e?.code === 'STEP_TIMEOUT' ? STATES.TIMEOUT : STATES.ERROR_FATAL;
      cur = await ctx();
      await transitionSafe(store, cur, kind, actor, `step:${name} failed: ${String(e?.message || e)}`, { step: name });
      return kind;
    }
  };

  for (let guard = 0; guard < 24; guard++) {
    cur = await ctx();
    switch (cur.state) {
      case STATES.FILED:
        await transition(store, { attemptId, from: STATES.FILED, to: STATES.AWAITING_APPROVAL, actor, reason: 'await human approval' });
        break;
      case STATES.AWAITING_APPROVAL:
        return cur.state; // 人工等待：审批是外部输入
      case STATES.APPROVED:
        if (!handlers.generatePatch) return manualWait(store, cur, actor, 'handler_not_configured:generatePatch');
        {
          const r = await step('generatePatch', STATES.APPROVED, STATES.PATCH_GENERATING, async () => {
            const out = await handlers.generatePatch(cur);
            if (!out.patch_text || !String(out.patch_text).trim()) throw Object.assign(
              new Error('patch is empty (no changes produced)'), { code: 'PATCH_EMPTY' });
            if (out.patch_digest !== cur.patch_digest) throw Object.assign(
              new Error(`digest drift: filed=${cur.patch_digest} produced=${out.patch_digest}`), { code: 'DIGEST_DRIFT' });
            const leak = secretShapedIn(out.patch_text ?? '');
            if (leak) throw Object.assign(
              new Error(`patch content contains secret-shaped string: ${leak.slice(0, 24)}…`), { code: 'PATCH_SECRET_SHAPED' });
            return out;
          });
          if (r === STATES.ERROR_FATAL || r === STATES.TIMEOUT) return r;
          if (typeof r === 'string') break;
          await transition(store, { attemptId, from: STATES.PATCH_GENERATING, to: STATES.PATCH_READY, actor, reason: 'patch ready' });
        }
        break;
      case STATES.PATCH_READY:
      case STATES.DRY_RUN_APPLY:
        if (!handlers.dryRunApply || !handlers.runTests) return manualWait(store, cur, actor, 'handler_not_configured:dryRunApply/runTests');
        {
          if (cur.state === STATES.PATCH_READY) {
            await transition(store, { attemptId, from: STATES.PATCH_READY, to: STATES.DRY_RUN_APPLY, actor, reason: 'dry-run begin' });
          }
          const applied = await handlers.dryRunApply(cur); // 异常直接抛→外层捕获由 step 语义覆盖
          if (!applied?.applied) throw new Error('dry_run_apply returned not-applied');
          const t = await handlers.runTests(cur);
          cur = await ctx();
          if (t?.passed) {
            await transition(store, { attemptId, from: STATES.DRY_RUN_APPLY, to: STATES.DRY_RUN_VERIFIED, actor, reason: 'isolated tests passed' });
          } else {
            await transitionSafe(store, cur, STATES.TEST_FAILED, actor, 'isolated tests failed', { output: String(t?.output ?? '').slice(0, 2000) });
            return STATES.TEST_FAILED;
          }
        }
        break;
      case STATES.DRY_RUN_VERIFIED:
        if (cfg.dryRun) {
          await transition(store, { attemptId, from: STATES.DRY_RUN_VERIFIED, to: STATES.DRY_RUN_COMPLETE, actor, reason: 'dry_run=on (default) — 真实写入需授权关闭 dry-run' });
          return STATES.DRY_RUN_COMPLETE;
        }
        await transition(store, { attemptId, from: STATES.DRY_RUN_VERIFIED, to: STATES.AWAITING_GITHUB_GRANT, actor, reason: 'dry_run=off — 需显式 GitHub 写入授权' });
        break;
      case STATES.AWAITING_GITHUB_GRANT: {
        const grant = await consumePipelineGrant(cfg, store, cur.repo); // gate：一次性消费
        if (!grant) return cur.state; // 人工等待：授权
        await transition(store, { attemptId, from: STATES.AWAITING_GITHUB_GRANT, to: STATES.GRANTED, actor: grant.operator, reason: `grant ${grant.grant_id} consumed` });
        break;
      }
      case STATES.GRANTED:
        if (!handlers.commitPush) return manualWait(store, cur, actor, 'handler_not_configured:commitPush');
        {
          await transition(store, { attemptId, from: STATES.GRANTED, to: STATES.COMMITTING, actor, reason: 'commit under grant' });
          const r = await handlers.commitPush(cur, makeWriteGate(cfg, store));
          cur = await ctx();
          if (r?.committed_sha) {
            await transition(store, { attemptId, from: STATES.COMMITTING, to: STATES.COMMITTED, actor, reason: `committed ${r.committed_sha.slice(0, 12)}`, meta: { committed_sha: r.committed_sha } });
          } else {
            await transitionSafe(store, cur, STATES.ERROR_FATAL, actor, 'commitPush returned no sha');
            return STATES.ERROR_FATAL;
          }
        }
        break;
      case STATES.COMMITTED:
        await transition(store, { attemptId, from: STATES.COMMITTED, to: STATES.TEST_RUNNING, actor, reason: 'post-push verification' });
        break;
      case STATES.TEST_RUNNING: {
        if (!handlers.runTests) return manualWait(store, cur, actor, 'handler_not_configured:runTests(post-push)');
        const t = await handlers.runTests(cur);
        cur = await ctx();
        if (t?.passed) {
          await transition(store, { attemptId, from: STATES.TEST_RUNNING, to: STATES.VERIFIED, actor, reason: 'post-push verified' });
          return STATES.VERIFIED;
        }
        if (handlers.rollback) {
          await handlers.rollback(cur);
          await transitionSafe(store, cur, STATES.ROLLED_BACK, actor, 'post-push tests failed — rolled back', { output: String(t?.output ?? '').slice(0, 2000) });
          return STATES.ROLLED_BACK;
        }
        await transitionSafe(store, cur, STATES.TEST_FAILED, actor, 'post-push tests failed (no rollback handler)');
        return STATES.TEST_FAILED;
      }
      default:
        return cur.state; // 终态/人工等待
    }
  }
  return (await ctx()).state;
}

async function manualWait(store, cur, actor, reason) {
  const updated = await store.compareAndSetState(cur.attempt_id, cur.state, STATES.MANUAL_WAIT, { last_reason: reason });
  if (updated) await store.recordEvent({ attempt_id: cur.attempt_id, kind: 'MANUAL_WAIT', from_state: cur.state, to_state: STATES.MANUAL_WAIT, actor, reason });
  return STATES.MANUAL_WAIT;
}
async function transitionSafe(store, cur, to, actor, reason, meta) {
  try {
    return await transition(store, { attemptId: cur.attempt_id, from: cur.state, to, actor, reason, meta });
  } catch (e) {
    if (e instanceof TransitionConflict && e.info?.actual === to) return { ok: true, idempotent: true };
    throw e;
  }
}

// ── 重启恢复：非终态重校验（TTL/stale head）→ 可续跑的留给 runner，其余 MANUAL_WAIT ──
export async function recover(store, cfg, { currentHead } = {}, actor = 'recovery') {
  const rows = await store.listNonTerminal([...TERMINAL]);
  const report = [];
  for (const a of rows) {
    if (a.expires_at && new Date(a.expires_at).getTime() < Date.now()) {
      await transitionSafe(store, a, STATES.EXPIRED, actor, 'recovery: TTL expired');
      report.push({ attempt_id: a.attempt_id, action: 'EXPIRED' });
      continue;
    }
    if (currentHead) {
      const head = await currentHead(a);
      if (head && head !== a.base_head_sha) {
        await transitionSafe(store, a, STATES.STALE_HEAD, actor, `recovery: head moved filed=${a.base_head_sha} now=${head}`);
        report.push({ attempt_id: a.attempt_id, action: 'STALE_HEAD' });
        continue;
      }
    }
    report.push({ attempt_id: a.attempt_id, action: 'RESUMABLE', state: a.state });
  }
  return report;
}


// 补丁内容凭据形状闸（与 scripts/secret-scan.sh 同族模式；文档示例/占位符豁免）
const SECRET_SHAPED_RE = /(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,40}|github_pat_[A-Za-z0-9_]{60,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|(password|secret|token|api_key)["']?\s*[:=]\s*["'][^"']{8,}["']/gi;
const SECRET_EXEMPT_RE = /EXAMPLE|placeholder|changeme|dummy|sample|fake|your[-_]|xxx+|abcdef|ABCDEFGH|0{8,}|REDACTED|SUPERSECRET|process\.env/i;
export function secretShapedIn(text) {
  const m = String(text).match(SECRET_SHAPED_RE);
  if (!m) return null;
  const hit = m.find((x) => !SECRET_EXEMPT_RE.test(x));
  return hit ?? null;
}

function withTimeout(p, ms, name) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(Object.assign(new Error(`step timeout: ${name}`), { code: 'STEP_TIMEOUT' })), ms);
    p.then((v) => { clearTimeout(t); resolve(v); }, (e) => { clearTimeout(t); reject(e); });
  });
}
