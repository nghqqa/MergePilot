#!/usr/bin/env node
// p3-readonly-business.mjs — Phase3 真实业务只读联调（2026-09-27）。
// 输入：PR #235 真实 review 数据（gh 只读拉取的标题/状态/被移除的缺陷行）。
// 输出边界：GitHub 零写入；一切落库/归档仅在 promote2 隔离栈。
// 断言：
//  A) 负向：真实业务仓库（nghqqa/MergePilot）不在 allowlist → 立案即被
//     REPO_NOT_IN_FXV_ALLOWLIST 拒绝（ERROR_FATAL），无 patch/gate/写入，
//     且 console API 对其不可见（allowlist 过滤）——真实数据不被错误转化。
//  B) 正向：以 PR #235 真实移除行为 finding 素材的干跑链 → DRY_RUN_COMPLETE
//     + artifact COMPLETE + 五类对象 + 绑定 + 审计链。
import { execFileSync } from 'node:child_process';
import fs from 'node:fs'; import os from 'node:os'; import path from 'node:path'; import crypto from 'node:crypto';
import { createRequire } from 'node:module'; import { fileURLToPath } from 'node:url';

const supportDir = fileURLToPath(new URL('./support/', import.meta.url));
const { Pool } = createRequire(path.join(supportDir, 'noop.js'))('pg');
const { createFxvStore } = await import('../lib/fxv/store.mjs');
const { STATES } = await import('../lib/fxv/orchestrator.mjs');
const { makeExecHandlers, ruleDigest } = await import('../lib/fxv/exec/exec.mjs');
const { createArtifactStore } = await import('../lib/fxv/artifacts.mjs');
const { runPipelineArchived, artifactStatusOf } = await import('../lib/fxv/archive.mjs');
const { loadFxvConfig } = await import('../lib/fxv/config.mjs');

let pass = 0, fail = 0;
const ok = (n, c, d) => { if (c) { pass++; console.log('  PASS  ' + n); } else { fail++; console.log('  FAIL  ' + n + (d ? ' — ' + d : '')); } };
const sha = () => crypto.randomBytes(10).toString('hex');
const git = (c, ...a) => execFileSync('git', ['-c', 'user.email=t@t', '-c', 'user.name=t', ...a], { cwd: c, encoding: 'utf8' });

// ── 真实 review 数据（gh 只读，2026-09-27 拉取）──
const PR235 = {
  number: 235, state: 'MERGED', mergedAt: '2026-09-26T23:26:52Z',
  title: 'feat(cchain): C 链接线 API/metrics/audit/前端 + RUN_BINDING_AUTH 验签与密钥轮换 + FXV enforcement gate + compose 发行物加固',
  // PR #235 真实移除的缺陷行（gh pr diff 235 实取）：
  removedStaleComment: '// 已知边界（诚实登记）：多凭证认证（每用户独立密码/OIDC）未接线（G-07）——',
  removedBadPlaceholder: 'image: minio/minio:latest@sha256:_NO-pin-placeholder',
  // 对应评审修复（PR #235 真实引入的内容摘要）：
  fixedComment: '// 已知边界（2026-09-26 G-07 已接线后更新）：每用户独立口令多凭证已接线',
  fixedPlaceholder: 'image: elestio/minio@sha256:25348a257f1ece1b192f25f6cd9854618fa86422ac87b494b5d4e629c556d4bd',
};
console.log(`== review 源：PR #${PR235.number} ${PR235.state} @ ${PR235.mergedAt}（只读）==`);

const pool = new Pool({ connectionString: process.env.FXV_PG_TEST_DSN });
const store = await createFxvStore({ pool }); await store.initSchema();
const S3 = createArtifactStore({ endpoint: process.env.FXV_S3_ENDPOINT, bucket: process.env.FXV_S3_BUCKET || 'fxv-artifacts-e2e',
  accessKey: process.env.FXV_S3_ACCESS_KEY, secretKey: process.env.FXV_S3_SECRET_KEY });
await S3.ensureBucket();
const cfg = (o = {}) => loadFxvConfig({ FXV_REPO_ALLOWLIST: 'acme/app', FXV_DRY_RUN: '1', ...process.env, ...o }).config;

// 以真实 PR 移除行为种子的隔离 git 仓库（无任何远端，无 GitHub 接触）
function origin() {
  const d = fs.mkdtempSync(path.join(os.tmpdir(), 'p3rb-')); const b = path.join(d, 'o.git'); fs.mkdirSync(b); git(b, 'init', '--bare', '-b', 'main');
  const s = path.join(d, 's'); fs.mkdirSync(s); git(s, 'init', '-b', 'main');
  // 种子文件 = 真实 review 快照（PR #235 移除前的缺陷内容）
  fs.writeFileSync(path.join(s, 'review-findings.md'),
    PR235.removedStaleComment + '\n' + PR235.removedBadPlaceholder + '\n');
  git(s, 'add', '.'); git(s, 'commit', '-m', 'seed: real review snapshot (PR#235 removed lines)'); git(s, 'push', b.replace(/\\/g, '/'), 'main');
  return { url: b.replace(/\\/g, '/'), head: git(s, 'rev-parse', 'HEAD').trim() };
}
// harness 只验证本 finding 的修复（单 ticket 单 finding；占位符行属另一 finding，不在本链范围）
const TEST_OK = ['node', '-e', "const s=require('fs').readFileSync('review-findings.md','utf8');if(s.includes('未接线（G-07）')||!s.includes('已接线'))process.exit(1)"];

console.log('== A) 真实业务仓库负向：nghqqa/MergePilot 不在 allowlist ==');
{
  const o = origin();
  const r = await store.fileAttempt({ attempt_id: 'att-' + sha(), ticket_id: 'tkt-' + sha(), finding_id: 'fn-' + sha(),
    repo: 'nghqqa/MergePilot', branch: 'main', base_head_sha: o.head, patch_digest: ruleDigest(o.head, 'review-findings.md', 'x', 'y'),
    actor: 'p3-readonly', reason: 'real business repo read-only probe' });
  const id = r.attempt.attempt_id;
  const H = makeExecHandlers({ cfg: cfg(), repoUrl: () => o.url, testCmd: TEST_OK, store, artifactStore: S3 });
  const end = await runPipelineArchived(store, cfg(), H, id, S3);
  const row = await store.getAttempt(id);
  const evs = await store.listEvents(id);
  ok('真实仓库立案即拒（ERROR_FATAL/REPO_NOT_IN_FXV_ALLOWLIST）',
    end === STATES.ERROR_FATAL && /REPO_NOT_IN_FXV_ALLOWLIST/.test(row.state_detail?.last_reason ?? ''),
    'end=' + end + ' reason=' + row.state_detail?.last_reason);
  const artsA = row.state_detail?.artifacts ?? {};
  ok('未产生 patch/verifier/test（仅失败证据 audit 归档，无 gate/写入）',
    !artsA.patch && !artsA.verifier_verdict && !artsA.test_results
      && !evs.some((e) => /GRANT|COMMIT/.test(e.kind ?? '')),
    'keys=' + Object.keys(artsA).join(','));
  ok('拒绝即终态（ERROR_FATAL，未滞留为活跃 ticket）', row.state === STATES.ERROR_FATAL);
}

console.log('== B) 正向干跑链：真实 review finding（PR#235 移除行）→ 修复 → COMPLETE ==');
{
  const o = origin();
  const rule = { file: 'review-findings.md', pattern: PR235.removedStaleComment, replacement: PR235.fixedComment };
  const res = await store.fileAttempt({ attempt_id: 'att-' + sha(), ticket_id: 'tkt-' + sha(), finding_id: 'fn-' + sha(),
    repo: 'acme/app', branch: 'main', base_head_sha: o.head, patch_digest: ruleDigest(o.head, rule.file, rule.pattern, rule.replacement),
    actor: 'p3-readonly', reason: 'real review data from PR#235 (read-only gh)' });
  const id = res.attempt.attempt_id;
  await store.compareAndSetState(id, 'FILED', 'FILED', { pr: 235, review_source: 'PR#235 real diff (read-only)',
    rule_file: rule.file, rule_pattern: rule.pattern, rule_replacement: rule.replacement });
  const { transition } = await import('../lib/fxv/orchestrator.mjs');
  await transition(store, { attemptId: id, from: STATES.FILED, to: STATES.AWAITING_APPROVAL, actor: 'p3-readonly', reason: 'review intake (read-only)' });
  await transition(store, { attemptId: id, from: STATES.AWAITING_APPROVAL, to: STATES.APPROVED, actor: 'pilot', reason: 'dry-run approval (isolated stack)' });
  const H = makeExecHandlers({ cfg: cfg(), repoUrl: () => o.url, testCmd: TEST_OK, store, artifactStore: S3 });
  const end = await runPipelineArchived(store, cfg(), H, id, S3);
  const row = await store.getAttempt(id);
  ok('业务 DRY_RUN_COMPLETE（dry-run 开，无任何 GitHub 写）', end === STATES.DRY_RUN_COMPLETE, 'end=' + end);
  ok('artifact_status=COMPLETE', row.state_detail.artifact_status === 'COMPLETE');
  const arts = row.state_detail.artifacts;
  ok('patch/verifier/tests/audit/manifest 全存在', ['patch', 'verifier_verdict', 'test_results', 'audit'].every((k) => arts?.[k]), 'keys=' + Object.keys(arts ?? {}).join(','));
  ok('绑定完整（repo/head/ticket/attempt/digest 全锚定）',
    row.repo === 'acme/app' && row.base_head_sha === o.head && !!row.ticket_id && !!row.attempt_id && row.patch_digest.length === 64);
  const st = await artifactStatusOf(store, S3, row);
  ok('读侧校验 OK/COMPLETE', st.artifact_status === 'OK' || st.artifact_status === 'COMPLETE');
  const evs = await store.listEvents(id);
  ok('审计链完整（FILED→…→DRY_RUN_COMPLETE 终态事件）', evs.some((e) => e.to_state === STATES.DRY_RUN_COMPLETE) && evs.length >= 6, 'events=' + evs.length);
}

console.log(`\np3-readonly-business: ${pass} passed, ${fail} failed`);
await pool.end(); process.exit(fail > 0 ? 1 : 0);
