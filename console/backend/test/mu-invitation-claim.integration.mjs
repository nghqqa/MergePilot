// console/backend/test/mu-invitation-claim.integration.mjs — 邀请认领语义回归（角色覆盖事故修复）。
// 层级：真实服务代码（store 事务路径）+ 隔离 PG。覆盖 2026-10-10 生产事故语义：
// maintainer 被 contributor 邀请自动认领降级（audit MU_MEMBERSHIP_ROLE_RESTORED 可查）。
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const here = path.dirname(fileURLToPath(import.meta.url));
const require2 = createRequire(path.join(here, 'support/noop.js'));
const { Pool } = require2('pg');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail !== undefined ? ' — ' + JSON.stringify(detail).slice(0, 200) : ''}`); }
};

const CTR = `mu-invclaim-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 16680 + Math.floor(Math.random() * 60);
execFileSync('docker', ['run', '-d', '--name', CTR, '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PGPORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
const DSN = `postgres://postgres:x@127.0.0.1:${PGPORT}/mu`;
process.on('exit', () => { try { execFileSync('docker', ['rm', '-f', CTR], { stdio: 'pipe' }); } catch { /* */ } });

try {
  for (let i = 0; i < 60; i++) {
    try { const p = new Pool({ connectionString: DSN }); await p.query('SELECT 1'); await p.end(); break; }
    catch { await new Promise((r) => setTimeout(r, 800)); }
  }
  const { createMuStore } = await import('../lib/multiuser/store.mjs');
  const pool = new Pool({ connectionString: DSN });
  const store = await createMuStore({ pool, env: process.env });
  await store.initSchema();

  const tenant = await store.ensureTenant({ slug: 'invclaim', displayName: '邀请认领回归' });
  const tenantId = tenant.tenant_id;
  const mkUser = async (login) => (await store.ensureUser({ login })).user_id;
  const role = async (uid) => (await pool.query(
    `SELECT role, state FROM mu.membership WHERE tenant_id=$1 AND user_id=$2`,
    [tenantId, uid])).rows[0] ?? null;

  // ── S1 新用户正常入驻 ──
  const u1 = await mkUser('inv-new-user');
  const s1 = await store.claimInvitationOnboard({ inviteId: null, userId: u1, invitedRole: 'contributor' });
  ok('S0 前置：不可认领（inviteId=null）→ not_claimable', s1.ok === false && s1.reason === 'not_claimable');

  const mkInvite = async (role, subject, ttlMs = 3600_000) => {
    const inv = await store.createInvitation({ tenantId, role, createdBy: null,
      expectedSubject: subject, ttlMs });
    return inv.invite_id;
  };

  // ── S2 新用户 + contributor 邀请 → 正常入驻 ──
  const u2 = await mkUser('inv-fresh');
  const i2 = await mkInvite('contributor', 'github-oauth:2000002');
  const r2 = await store.claimInvitationOnboard({ inviteId: i2, userId: u2, invitedRole: 'contributor' });
  ok('S2 新用户入驻 onboarded+contributor', r2.ok && r2.outcome === 'onboarded' && r2.role === 'contributor');
  ok('S2b membership 落行', (await role(u2))?.role === 'contributor');

  // ── S3 事故语义：maintainer 遇 contributor 邀请 → 保留 maintainer ──
  const u3 = await mkUser('inv-maintainer');
  await store.ensureMembership({ tenantId, userId: u3, role: 'maintainer' }); // 既有更高角色
  const i3 = await mkInvite('contributor', 'github-oauth:2000003');
  const r3 = await store.claimInvitationOnboard({ inviteId: i3, userId: u3, invitedRole: 'contributor' });
  ok('S3 maintainer+contributor 邀请 → preserved_active 且角色不变',
    r3.outcome === 'preserved_active' && r3.role === 'maintainer', r3);
  ok('S3b membership 仍 maintainer（事故回归锁）', (await role(u3))?.role === 'maintainer');

  // ── S4 已有 contributor 遇 maintainer 邀请 → 不自动升级 ──
  const u4 = await mkUser('inv-contributor');
  await store.ensureMembership({ tenantId, userId: u4, role: 'contributor' });
  const i4 = await mkInvite('maintainer', 'github-oauth:2000004');
  const r4 = await store.claimInvitationOnboard({ inviteId: i4, userId: u4, invitedRole: 'maintainer' });
  ok('S4 contributor+maintainer 邀请 → 不按邀请升级（保留 contributor）',
    r4.outcome === 'preserved_active' && r4.role === 'contributor', r4);

  // ── S5 auditor 遇 contributor 邀请 → 保留 auditor（无等级排序）──
  const u5 = await mkUser('inv-auditor');
  await store.ensureMembership({ tenantId, userId: u5, role: 'auditor' });
  const i5 = await mkInvite('contributor', 'github-oauth:2000005');
  const r5 = await store.claimInvitationOnboard({ inviteId: i5, userId: u5, invitedRole: 'contributor' });
  ok('S5 auditor+contributor 邀请 → 保留 auditor', r5.role === 'auditor');

  // ── S6 revoked membership：不顺带激活 ──
  const u6 = await mkUser('inv-revoked');
  await store.ensureMembership({ tenantId, userId: u6, role: 'contributor' });
  await store.revokeMembership(tenantId, u6);
  const i6 = await mkInvite('contributor', 'github-oauth:2000006');
  const r6 = await store.claimInvitationOnboard({ inviteId: i6, userId: u6, invitedRole: 'contributor' });
  ok('S6 revoked+邀请 → preserved_revoked（未激活）',
    r6.outcome === 'preserved_revoked' && (await role(u6))?.state === 'revoked', r6);

  // ── S7 已认领邀请 → not_claimable（不二次消耗）──
  const r7 = await store.claimInvitationOnboard({ inviteId: i2, userId: u2, invitedRole: 'contributor' });
  ok('S7 已认领邀请不可再认领', r7.ok === false && r7.reason === 'not_claimable');

  // ── S8 过期邀请 → not_claimable ──
  const i8 = await mkInvite('contributor', 'github-oauth:2000008', -1000); // 已过期（负 TTL）
  const r8 = await store.claimInvitationOnboard({ inviteId: i8, userId: await mkUser('inv-exp'), invitedRole: 'contributor' });
  ok('S8 过期邀请不可认领', r8.ok === false);

  // ── S9 并发认领同一邀请：恰一成功 ──
  const u9a = await mkUser('inv-race-a');
  const u9b = await mkUser('inv-race-b');
  const i9 = await mkInvite('contributor', 'github-oauth:2000009');
  const [ra, rb] = await Promise.all([
    store.claimInvitationOnboard({ inviteId: i9, userId: u9a, invitedRole: 'contributor' }),
    store.claimInvitationOnboard({ inviteId: i9, userId: u9b, invitedRole: 'contributor' }),
  ]);
  ok('S9 并发认领：恰一成功（CAS）',
    (ra.ok === true) !== (rb.ok === true), { ra: ra.ok, rb: rb.ok });

  // ── S10 多邀请消歧保持既有逻辑（resolveClaimCandidates 层——此处锁 store 原语）──
  const i10a = await mkInvite('contributor', 'github-oauth:2000010');
  const i10b = await mkInvite('contributor', 'github-oauth:2000010');
  const u10 = await mkUser('inv-ambiguous');
  const cands = await store.findClaimableInvitationsAll({ subject: 'github-oauth:2000010' });
  ok('S10 同 subject 双邀请 → candidates=2（ambiguous 拒绝语义的输入）', cands.length === 2);

  // ── S11 管理员路径不受影响：ensureMembership 仍可明确调整（含降级）──
  await store.ensureMembership({ tenantId, userId: u4, role: 'reviewer' });
  ok('S11 管理员明确调整语义不变（contributor→reviewer）', (await role(u4))?.role === 'reviewer');

  console.log(`\n  invitation-claim regression: ${pass} pass, ${fail} fail`);
  process.exit(fail > 0 ? 1 : 0);
} catch (e) {
  console.error('DRILL-ERROR', e?.stack ?? e);
  process.exit(1);
}
