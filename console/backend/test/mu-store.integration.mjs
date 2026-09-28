#!/usr/bin/env node
// console/backend/test/mu-store.integration.mjs — MU Phase 1 schema/bootstrap 集成测试。
// 运行：node console/backend/test/mu-store.integration.mjs（自起一次性 postgres:16-alpine 容器，
// 随机回环端口，跑毕 docker rm -f——绝不触碰任何常驻栈；无真实凭据，库口令为 1 字符占位）。
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const supportDir = fileURLToPath(new URL('./support/', import.meta.url));
const { Pool } = createRequire(path.join(supportDir, 'noop.js'))('pg');
const { createMuStore } = await import('../lib/multiuser/store.mjs');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail ? ' — ' + JSON.stringify(detail).slice(0, 220) : ''}`); }
};

const CTR = `mu-store-it-${crypto.randomBytes(4).toString('hex')}`;
const PORT = 16000 + Math.floor(Math.random() * 90);
execFileSync('docker', ['run', '-d', '--name', CTR,
  '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
const dsn = `postgres://postgres:x@127.0.0.1:${PORT}/mu`;
const deadline = Date.now() + 60_000;
for (;;) {
  try { const p = new Pool({ connectionString: dsn }); await p.query('SELECT 1'); await p.end(); break; }
  catch { if (Date.now() > deadline) throw new Error('pg timeout'); await new Promise((r) => setTimeout(r, 800)); }
}

const pool = new Pool({ connectionString: dsn });
try {
  const store = await createMuStore({ pool, env: { MU_BOOTSTRAP_ADMIN_LOGIN: 'mu-admin' } });

  // ── MS*：迁移与 bootstrap ──
  ok('MS1 迁移应用且可重复执行（幂等）', (await store.initSchema()) === true && (await store.initSchema()) === true);
  const vers = (await pool.query(`SELECT version FROM mu.schema_migrations ORDER BY version`)).rows.map((r) => r.version);
  ok('MS1b 版本表记录 1+2+3', vers.includes(1) && vers.includes(2) && vers.includes(3), vers);

  const b1 = await store.bootstrap();
  const b2 = await store.bootstrap();
  ok('MS2 bootstrap 幂等（两次同 tenant/user/membership）',
    b1.tenant.tenant_id === b2.tenant.tenant_id && b1.user.user_id === b2.user.user_id
      && b1.membership.membership_id === b2.membership.membership_id, { t: b1.tenant.slug, u: b1.user.login });
  ok('MS2b 迁移 tenant 标记 + pilot 操作员 → platform_admin',
    b1.tenant.is_migration_tenant === true && b1.tenant.slug === 'default'
      && b1.membership.role === 'platform_admin' && b1.user.login === 'mu-admin');
  ok('MS2c bootstrap 身份映射（fixture provider，无任何 token 列）',
    (await store.getUserByIdentity('fixture', 'fixture:mu-admin'))?.user_id === b1.user.user_id);

  // ── MS*：复合唯一与跨 tenant 隔离 ──
  const tA = await store.ensureTenant({ slug: `ten-a-${Date.now()}`, displayName: 'A' });
  const tB = await store.ensureTenant({ slug: `ten-b-${Date.now()}`, displayName: 'B' });
  const uA = await store.ensureUser({ login: `alice-${Date.now()}` });
  const uB = await store.ensureUser({ login: `bob-${Date.now()}` });
  await store.ensureMembership({ tenantId: tA.tenant_id, userId: uA.user_id, role: 'maintainer' });
  await store.ensureMembership({ tenantId: tB.tenant_id, userId: uB.user_id, role: 'contributor' });

  const SAME_REPO_ID = 'R_gh_1001';
  const rA = await store.ensureRepository({ tenantId: tA.tenant_id, provider: 'github', providerRepoId: SAME_REPO_ID, owner: 'acme', name: 'app' });
  const rB = await store.ensureRepository({ tenantId: tB.tenant_id, provider: 'github', providerRepoId: SAME_REPO_ID, owner: 'acme', name: 'app' });
  ok('MS3 同 provider_repo_id 可存在于两个 tenant（复合唯一含 tenant 维度）',
    rA.repo_id !== rB.repo_id && rA.tenant_id === tA.tenant_id && rB.tenant_id === tB.tenant_id);

  ok('MS4 resolveRepository 按 tenant 收窄：A 的会话解析不出 B 的 repo',
    (await store.resolveRepository(tA.tenant_id, rB.repo_id)) === null
      && (await store.resolveRepository(tB.tenant_id, rA.repo_id)) === null
      && (await store.resolveRepository(tA.tenant_id, rA.repo_id))?.repo_id === rA.repo_id);

  // 同 tenant 同 repo 幂等 upsert（复合唯一冲突走 UPDATE 不抛错）
  const rA2 = await store.ensureRepository({ tenantId: tA.tenant_id, provider: 'github', providerRepoId: SAME_REPO_ID, owner: 'acme', name: 'app' });
  ok('MS5 同 tenant 内同 repo 幂等（同 repo_id）', rA2.repo_id === rA.repo_id);

  // ── MS*：binding 与约束 ──
  const bindA = await store.ensureBinding({ tenantId: tA.tenant_id, repoId: rA.repo_id, kind: 'fixture', installationId: 'inst-synth-a1', grantedScopes: ['pull_requests:read'] });
  const bindA2 = await store.ensureBinding({ tenantId: tA.tenant_id, repoId: rA.repo_id, kind: 'fixture', installationId: 'inst-synth-a1', grantedScopes: ['pull_requests:read'] });
  ok('MS6 binding (tenant,repo,kind) 幂等', bindA.binding_id === bindA2.binding_id);
  await store.revokeBinding(tA.tenant_id, rA.repo_id);
  ok('MS6b 吊销后 getBindingForRepo 为 null（fail-closed）', (await store.getBindingForRepo(tA.tenant_id, rA.repo_id)) === null);
  await store.ensureBinding({ tenantId: tA.tenant_id, repoId: rA.repo_id, kind: 'fixture', installationId: 'inst-synth-a1', grantedScopes: ['pull_requests:read'] });
  ok('MS6c 重启用恢复 active', (await store.getBindingForRepo(tA.tenant_id, rA.repo_id)) !== null);

  let roleCheckRejected = false;
  try { await pool.query(`INSERT INTO mu.membership (tenant_id, user_id, role) VALUES ($1,$2,'superadmin')`, [tA.tenant_id, uB.user_id]); }
  catch { roleCheckRejected = true; }
  ok('MS7 角色词汇 CHECK 约束拒绝未知角色', roleCheckRejected);

  let loginDupRejected = false;
  try { await pool.query(`INSERT INTO mu.app_user (login) VALUES ($1)`, [uA.login]); }
  catch { loginDupRejected = true; }
  ok('MS8 login 全局唯一', loginDupRejected);

  // 凭据红线：核心表无任何 token/secret 形状列
  const cols = (await pool.query(
    `SELECT table_name, column_name FROM information_schema.columns WHERE table_schema='mu'`)).rows;
  // Wave 2A 起允许 *_hash 摘要列（sha256，非明文凭据）；明文形状列仍必须为零。
  // Wave 3 PR-A 追加精确白名单：mu.agent_attempt.token_count = LLM 用量计数
  // （INT 计数器，语义为"消耗了多少 token"，非凭据材料——正则形状命中但非明文凭据）。
  const SECRET_COL_ALLOWLIST = new Set(['token_count', 'max_output_tokens']); // W3.2：LLM 输出上限计数列（INT，非凭据）
  const secretCols = cols.filter((c) => /token|secret|password|key/i.test(c.column_name)
    && !/_hash$/.test(c.column_name) && !SECRET_COL_ALLOWLIST.has(c.column_name));
  ok('MS9 schema 零明文凭据列（允许 *_hash 摘要列 + 精确白名单）', secretCols.length === 0, secretCols);

  // ── MS*：membership 撤销语义 ──
  await store.revokeMembership(tA.tenant_id, uA.user_id);
  const mRevoked = await store.getMembership(tA.tenant_id, uA.user_id);
  ok('MS10 撤销后 membership.state=revoked（保留历史，authz 按 state fail-closed）', mRevoked?.state === 'revoked');
  await store.ensureMembership({ tenantId: tA.tenant_id, userId: uA.user_id, role: 'reviewer' });
  ok('MS10b 重新授予恢复 active 且角色更新', (await store.getMembership(tA.tenant_id, uA.user_id))?.state === 'active'
    && (await store.getMembership(tA.tenant_id, uA.user_id))?.role === 'reviewer');

  // ── MS11*（Beta Hardening W1）：数据库级跨租户一致性约束（直接 SQL 写入，非 HTTP 面）──
  const hwA = await store.ensureTenant({ slug: `hw-a-${Date.now()}`, displayName: 'HW A' });
  const hwB = await store.ensureTenant({ slug: `hw-b-${Date.now()}`, displayName: 'HW B' });
  const hwRepoA = await store.ensureRepository({ tenantId: hwA.tenant_id, provider: 'github',
    providerRepoId: `HW_${Date.now()}`, owner: 'hw', name: 'a' });
  const hwPrA = await store.upsertPullRequest({ tenantId: hwA.tenant_id, repoId: hwRepoA.repo_id,
    providerPrNumber: 7, headSha: 'ab'.repeat(20) });
  const expectReject = async (label, sql, params) => {
    try { await pool.query(sql, params); ok(label, false, 'INSERT 未被 DB 拒绝'); }
    catch (e) { ok(label, true, String(e.code || e.message).slice(0, 40)); }
  };
  await expectReject('MS11 binding 跨租户（B tenant + A repo）被复合 FK 拒绝',
    `INSERT INTO mu.binding (tenant_id, repo_id, kind) VALUES ($1,$2,'fixture')`, [hwB.tenant_id, hwRepoA.repo_id]);
  await expectReject('MS11b pull_request 跨租户（B tenant + A repo）被复合 FK 拒绝',
    `INSERT INTO mu.pull_request (tenant_id, repo_id, provider_pr_number, head_sha) VALUES ($1,$2,8,$3)`,
    [hwB.tenant_id, hwRepoA.repo_id, 'cd'.repeat(20)]);
  await expectReject('MS11c review_record 跨租户（B tenant + A repo + A pr）被复合 FK 拒绝',
    `INSERT INTO mu.review_record (tenant_id, repo_id, pr_id, kind, head_sha) VALUES ($1,$2,$3,'ai_review','x')`,
    [hwB.tenant_id, hwRepoA.repo_id, hwPrA.pr_id]);
  await expectReject('MS11d job 跨租户（B tenant + A repo + A pr）被复合 FK 拒绝',
    `INSERT INTO mu.job (tenant_id, repo_id, pr_id, kind, requested_by, requested_role) VALUES ($1,$2,$3,'review_run',$4,'reviewer')`,
    [hwB.tenant_id, hwRepoA.repo_id, hwPrA.pr_id, uA.user_id]);
  await expectReject('MS11e audit_event 无 tenant_id 被 NOT NULL 拒绝（tenant 域必须归属）',
    `INSERT INTO mu.audit_event (kind) VALUES ('hw_no_tenant')`, []);
  let auditThrows = false;
  try { await store.audit('HW_SHOULD_THROW', { tenantId: null, detail: {} }); }
  catch { auditThrows = true; }
  ok('MS11f store.audit 对无 tenantId 前置拒绝（应用层+DB 双保险）', auditThrows);
  await store.auditPlatform('SESSION_REVOKED', { detail: { probe: 1 } });
  ok('MS11g platform 域审计独立表可写（明确 scope 建模，非 nullable 歧义）',
    (await pool.query(`SELECT count(*)::int n FROM mu.platform_audit_event WHERE kind='SESSION_REVOKED' AND detail->>'probe'='1'`)).rows[0].n === 1);
  let kindRejected = false;
  try { await store.auditPlatform('BOGUS_KIND', { detail: {} }); } catch { kindRejected = true; }
  ok('MS11g2 platform 审计 kind 注册表 fail-closed（未知类型拒绝）', kindRejected);
  const cons = (await pool.query(
    `SELECT conname FROM pg_constraint WHERE conname IN ('mu_repository_tenant_repo_uk','mu_pull_request_tenant_pr_uk',
       'mu_binding_tenant_repo_fk','mu_pr_tenant_repo_fk','mu_review_tenant_repo_fk','mu_review_tenant_pr_fk',
       'mu_job_tenant_repo_fk','mu_job_tenant_pr_fk')`)).rows.map((r) => r.conname);
  ok('MS11h 八条复合约束全部在目录（2 UK + 6 FK）', cons.length === 8, cons);
  const hwJob = await store.enqueueJob({ tenantId: hwA.tenant_id, repoId: hwRepoA.repo_id,
    prId: hwPrA.pr_id, kind: 'review_run', requestedBy: uA.user_id, requestedRole: 'reviewer' });
  const hwJobNoPr = await store.enqueueJob({ tenantId: hwA.tenant_id, repoId: hwRepoA.repo_id,
    kind: 'review_run', requestedBy: uA.user_id, requestedRole: 'reviewer' });
  ok('MS11i 同租户组合照常可写（含 pr_id NULL 的 MATCH SIMPLE 放行）',
    Boolean(hwJob.job_id) && Boolean(hwJobNoPr.job_id));
  ok('MS11j 迁移可重复执行（initSchema 三跑零错——语句级幂等守卫）', (await store.initSchema()) === true);

  // ── MS11k..m（PR250 复核 P1 回归）：同 tenant 内 repo/pr 交叉组合由三列 FK 拒绝 ──
  const hwRepoB = await store.ensureRepository({ tenantId: hwA.tenant_id, provider: 'github',
    providerRepoId: `HW_B_${Date.now()}`, owner: 'hw', name: 'b' });
  await expectReject('MS11k review_record 同租户 repo/pr 交叉（repoA + repoB 的 PR）被三列 FK 拒绝',
    `INSERT INTO mu.review_record (tenant_id, repo_id, pr_id, kind, head_sha) VALUES ($1,$2,$3,'ai_review','x')`,
    [hwA.tenant_id, hwRepoA.repo_id, (await store.upsertPullRequest({ tenantId: hwA.tenant_id,
      repoId: hwRepoB.repo_id, providerPrNumber: 9, headSha: '99'.repeat(20) })).pr_id]);
  await expectReject('MS11l job 同租户 repo/pr 交叉被三列 FK 拒绝',
    `INSERT INTO mu.job (tenant_id, repo_id, pr_id, kind, requested_by, requested_role) VALUES ($1,$2,$3,'review_run',$4,'reviewer')`,
    [hwA.tenant_id, hwRepoA.repo_id, (await store.upsertPullRequest({ tenantId: hwA.tenant_id,
      repoId: hwRepoB.repo_id, providerPrNumber: 10, headSha: '88'.repeat(20) })).pr_id, uA.user_id]);
  const okJob = await store.enqueueJob({ tenantId: hwA.tenant_id, repoId: hwRepoB.repo_id,
    prId: (await store.upsertPullRequest({ tenantId: hwA.tenant_id, repoId: hwRepoB.repo_id,
      providerPrNumber: 11, headSha: '77'.repeat(20) })).pr_id,
    kind: 'review_run', requestedBy: uA.user_id, requestedRole: 'reviewer' });
  ok('MS11m repo/pr 一致的合法组合照常可写', Boolean(okJob.job_id));

  // ── MS12（PR250 验收要求）：已有数据升级 / 失败可诊断 / 自愈重放 ──
  // 12a 模拟"v2 数据 + v3 升级"：删版本行 + 摘一条 FK → initSchema 在已有合法数据上重放 v3 重加约束
  await pool.query(`DELETE FROM mu.schema_migrations WHERE version = 3`);
  await pool.query(`ALTER TABLE mu.job DROP CONSTRAINT mu_job_tenant_repo_pr_fk`);
  ok('MS12a 已有合法数据上重放 migration v3（重加 FK 校验存量行）成功', (await store.initSchema()) === true
    && (await pool.query(`SELECT 1 FROM pg_constraint WHERE conname='mu_job_tenant_repo_pr_fk'`)).rowCount === 1);
  // 12b 失败可诊断：audit 历史脏行（NULL tenant）使 SET NOT NULL 失败并报可定位错误；清理后自愈
  await pool.query(`ALTER TABLE mu.audit_event ALTER COLUMN tenant_id DROP NOT NULL`);
  await pool.query(`INSERT INTO mu.audit_event (kind) VALUES ('hw_legacy_null_tenant')`);
  await pool.query(`DELETE FROM mu.schema_migrations WHERE version = 3`);
  let diagErr = null;
  try { await store.initSchema(); } catch (e) { diagErr = e; }
  ok('MS12b 历史脏行升级失败可诊断（PG 23502 含列名提示）',
    diagErr !== null && /tenant_id|null/i.test(String(diagErr?.message ?? diagErr)), String(diagErr?.message ?? '').slice(0, 90));
  await pool.query(`DELETE FROM mu.audit_event WHERE kind='hw_legacy_null_tenant'`);
  ok('MS12c 清理脏行后重放自愈（部分失败幂等恢复）', (await store.initSchema()) === true
    && (await pool.query(`SELECT attnotnull FROM pg_attribute WHERE attrelid='mu.audit_event'::regclass AND attname='tenant_id'`)).rows[0].attnotnull === true);
} catch (e) {
  fail++;
  console.error('FATAL', e);
} finally {
  await pool.end().catch(() => {});
  try { execFileSync('docker', ['rm', '-f', CTR], { stdio: 'pipe' }); } catch { /* */ }
}
console.log(`\nmu-store.integration: ${pass} pass / ${fail} fail`);
process.exit(fail ? 1 : 0);
