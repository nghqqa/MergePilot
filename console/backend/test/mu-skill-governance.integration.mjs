#!/usr/bin/env node
// console/backend/test/mu-skill-governance.integration.mjs — v17 技能版本治理面集成测试
// （B 波：/api/mu/skills 全矩阵——迁移/状态机/不可变/并发/幂等/RBAC/CSRF/跨租户/审计脱敏）。
// 运行：node console/backend/test/mu-skill-governance.integration.mjs
// （自起一次性 postgres:16-alpine + 进程内 createConsole 真实 HTTP；跑毕 docker rm -f。）
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const { Pool } = createRequire(path.join(HERE, 'support/noop.js'))('pg');
const { createConsole } = await import('../server.mjs');
// 迁移目标版本动态取自权威迁移表——新增 v19+ 迁移时本测试不再需要改版本钉。
const { MU_MIGRATIONS } = await import('../lib/multiuser/schema.mjs');
const LATEST_SCHEMA_VERSION = Math.max(...MU_MIGRATIONS.map((m) => m.version));

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail ? ' — ' + JSON.stringify(detail).slice(0, 240) : ''}`); }
};

const CTR = `mu-sg-it-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 17900 + Math.floor(Math.random() * 60);
execFileSync('docker', ['run', '-d', '--name', CTR,
  '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PGPORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
const dsn = `postgres://postgres:x@127.0.0.1:${PGPORT}/mu`;
{
  const deadline = Date.now() + 60_000;
  for (;;) {
    try { const p = new Pool({ connectionString: dsn }); await p.query('SELECT 1'); await p.end(); break; }
    catch { if (Date.now() > deadline) throw new Error('pg timeout'); await new Promise((r) => setTimeout(r, 800)); }
  }
}

process.env.MU_MODE = 'multiuser';
process.env.CONSOLE_PG_DSN = dsn;
process.env.CONSOLE_PILOT_USER = 'dev-pilot';
process.env.CONSOLE_PILOT_PASSWORD = 'legacy-test-password';
process.env.CONSOLE_SESSION_SECRET = 'mu-sg-session-secret';
process.env.MU_ALLOW_FIXTURE_LOGIN = '1';

const { server } = createConsole({ evidenceRoot: HERE, distDir: path.join(HERE, 'no-dist') });
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const BASE = `http://127.0.0.1:${server.address().port}`;
const pool = new Pool({ connectionString: dsn });

async function muLogin(subject, tenantSlug) {
  const res = await fetch(BASE + '/api/mu/auth/login', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ provider: 'fixture', subject, ...(tenantSlug ? { tenant_slug: tenantSlug } : {}) }),
  });
  const cookie = (res.headers.get('set-cookie') || '').split(';')[0];
  let json = null; try { json = await res.json(); } catch { /* */ }
  return { status: res.status, cookie, csrf: json?.csrf ?? null };
}
const call = async (sess, method, p, opts = {}) => {
  const r = await fetch(BASE + p, { method,
    headers: { ...(opts.body ? { 'content-type': 'application/json' } : {}),
      ...(sess?.cookie ? { cookie: sess.cookie } : {}),
      ...(opts.csrf && sess?.csrf ? { 'x-csrf-token': sess.csrf } : {}) },
    body: opts.body ? JSON.stringify(opts.body) : undefined });
  return { status: r.status, body: await r.json().catch(() => null) };
};

const SHA = (c) => c.repeat(64);

try {
  // ── 预热：首个 mu 请求触发 ensureMuReady（进程内 lazy 迁移），轮询 schema 就绪 ──
  for (let i = 0; i < 60; i++) {
    await fetch(BASE + '/api/mu/session').catch(() => {});
    try {
      const v = (await pool.query(`SELECT max(version) AS v FROM mu.schema_migrations`)).rows[0]?.v;
      if (Number(v) === LATEST_SCHEMA_VERSION) break;
    } catch { /* schema_migrations 未建——继续等 */ }
    await new Promise((r) => setTimeout(r, 700));
  }

  // ── SG0：fresh DB 迁移到最新版（动态取自 MU_MIGRATIONS）──
  const mv = (await pool.query(`SELECT max(version) AS v FROM mu.schema_migrations`)).rows[0].v;
  ok(`SG0a fresh DB 迁移到 v${LATEST_SCHEMA_VERSION}`, Number(mv) === LATEST_SCHEMA_VERSION, { v: mv });

  const T1 = (await pool.query(`SELECT tenant_id FROM mu.tenant LIMIT 1`)).rows[0].tenant_id;
  const mkUser = async (tenantId, login, role) => {
    const u = (await pool.query(`INSERT INTO mu.app_user (login, display_name) VALUES ($1,$1) RETURNING user_id`, [login])).rows[0];
    await pool.query(`INSERT INTO mu.membership (tenant_id, user_id, role) VALUES ($1,$2,$3)`, [tenantId, u.user_id, role]);
    await pool.query(`INSERT INTO mu.external_identity (provider, subject, user_id) VALUES ('fixture',$1,$2)`, [`fixture:${login}`, u.user_id]);
  };
  await mkUser(T1, 'sg-admin', 'platform_admin');
  await mkUser(T1, 'sg-maint', 'maintainer');
  await mkUser(T1, 'sg-contrib', 'contributor');
  await mkUser(T1, 'sg-reviewer', 'reviewer');
  await mkUser(T1, 'sg-auditor', 'auditor');
  // 第二租户（跨租户隔离对）
  const T2 = (await pool.query(`INSERT INTO mu.tenant (slug, display_name) VALUES ('sg-t2','SG T2') RETURNING tenant_id`)).rows[0].tenant_id;
  await mkUser(T2, 'sg-admin2', 'platform_admin');

  const admin = await muLogin('fixture:sg-admin');
  const maint = await muLogin('fixture:sg-maint');
  const contrib = await muLogin('fixture:sg-contrib');
  const reviewer = await muLogin('fixture:sg-reviewer');
  const auditor = await muLogin('fixture:sg-auditor');
  const admin2 = await muLogin('fixture:sg-admin2');

  // ── SG1：注册（RBAC/CSRF/校验/幂等冲突）──
  ok('SG1a 未认证 GET → 401', (await call(null, 'GET', '/api/mu/skills')).status === 401);
  ok('SG1b 缺 CSRF 注册 → 403 csrf_required',
    (await call(admin, 'POST', '/api/mu/skills', { body: { skill_key: 'rag.retrieve', display_name: '检索' } })).status === 403);
  for (const [label, sess] of [['maintainer', maint], ['contributor', contrib], ['reviewer', reviewer]]) {
    const r = await call(sess, 'POST', '/api/mu/skills', { csrf: true, body: { skill_key: 'x.no', display_name: 'x' } });
    ok(`SG1c ${label} 注册 → 403（manage_instance 缺）`, r.status === 403, r.status);
  }
  ok('SG1d 非法 skill_key → 400', (await call(admin, 'POST', '/api/mu/skills',
    { csrf: true, body: { skill_key: 'Bad Key', display_name: 'x' } })).status === 400);
  const reg = await call(admin, 'POST', '/api/mu/skills',
    { csrf: true, body: { skill_key: 'rag.retrieve', display_name: '审查检索技能', description: 'd' } });
  ok('SG1e platform_admin 注册 → 200', reg.status === 200 && reg.body?.skill?.skill_key === 'rag.retrieve', reg.status);
  ok('SG1f 重复 skill_key → 409 skill_key_exists', (await call(admin, 'POST', '/api/mu/skills',
    { csrf: true, body: { skill_key: 'rag.retrieve', display_name: 'x' } })).status === 409);

  // ── SG2：发布版本（校验/首版自动激活/不可变/幂等）──
  ok('SG2a 未知技能发布 → 404', (await call(admin, 'POST', '/api/mu/skills/nope/versions',
    { csrf: true, body: { version: '1.0.0', changelog: 'c', manifest_sha256: SHA('a') } })).status === 404);
  ok('SG2b 非语义化版本 → 400', (await call(admin, 'POST', '/api/mu/skills/rag.retrieve/versions',
    { csrf: true, body: { version: '1.0', changelog: 'c', manifest_sha256: SHA('a') } })).status === 400);
  ok('SG2c 非法指纹 → 400', (await call(admin, 'POST', '/api/mu/skills/rag.retrieve/versions',
    { csrf: true, body: { version: '1.0.0', changelog: 'c', manifest_sha256: 'zz' } })).status === 400);
  const v1 = await call(admin, 'POST', '/api/mu/skills/rag.retrieve/versions',
    { csrf: true, body: { version: '1.0.0', changelog: '初始版', manifest_sha256: SHA('a'), artifact_ref: 'skills/rag/1.0.0/' } });
  ok('SG2d 发布 1.0.0 → 200 且首版自动激活', v1.status === 200 && v1.body?.idempotent === false);
  const det1 = await call(admin, 'GET', '/api/mu/skills/rag.retrieve');
  ok('SG2e 详情显示 active=1.0.0（version_count=1）',
    det1.status === 200 && det1.body?.skill?.current_version === '1.0.0' && Number(det1.body?.skill?.version_count) === 1,
    det1.body?.skill?.current_version);
  const v2 = await call(admin, 'POST', '/api/mu/skills/rag.retrieve/versions',
    { csrf: true, body: { version: '1.1.0', changelog: '排序改进', manifest_sha256: SHA('b') } });
  ok('SG2f 发布 1.1.0 → 200 且 active 仍=1.0.0', v2.status === 200
    && (await call(admin, 'GET', '/api/mu/skills/rag.retrieve')).body?.skill?.current_version === '1.0.0');
  const dupSame = await call(admin, 'POST', '/api/mu/skills/rag.retrieve/versions',
    { csrf: true, body: { version: '1.1.0', changelog: '重复发布', manifest_sha256: SHA('b') } });
  ok('SG2g 重复发布同版本同指纹 → 200 幂等', dupSame.status === 200 && dupSame.body?.idempotent === true);
  const dupDiff = await call(admin, 'POST', '/api/mu/skills/rag.retrieve/versions',
    { csrf: true, body: { version: '1.1.0', changelog: '换指纹', manifest_sha256: SHA('c') } });
  ok('SG2h 同版本不同指纹 → 409 version_immutable_conflict', dupDiff.status === 409
    && dupDiff.body?.error?.reason === 'version_immutable_conflict', dupDiff.body?.error);
  const rowSha = (await pool.query(
    `SELECT manifest_sha256, changelog FROM mu.skill_version WHERE skill_id=(SELECT skill_id FROM mu.skill WHERE skill_key='rag.retrieve') AND version='1.1.0'`)).rows[0];
  ok('SG2i 冲突后版本行原样（指纹/说明未被覆盖）', rowSha.manifest_sha256 === SHA('b') && rowSha.changelog === '排序改进');

  // ── SG3：publish 幂等端点 + 历史含发布者 ──
  const pub = await call(admin, 'POST', '/api/mu/skills/rag.retrieve/versions/1.1.0/publish', { csrf: true, body: {} });
  ok('SG3a publish 幂等端点 → 200 already-published', pub.status === 200 && pub.body?.published === true);
  ok('SG3b publish 未知版本 → 404', (await call(admin, 'POST', '/api/mu/skills/rag.retrieve/versions/9.9.9/publish',
    { csrf: true, body: {} })).status === 404);
  const hist = await call(admin, 'GET', '/api/mu/skills/rag.retrieve/versions');
  ok('SG3c 历史按时间倒序且含发布者', hist.status === 200
    && hist.body?.versions?.[0]?.version === '1.1.0' && hist.body?.versions?.[0]?.published_by === 'sg-admin',
    hist.body?.versions?.[0]);
  ok('SG3d 未知技能详情/历史 → 404', (await call(admin, 'GET', '/api/mu/skills/nope')).status === 404
    && (await call(admin, 'GET', '/api/mu/skills/nope/versions')).status === 404);

  // ── SG4：激活/回滚状态机 + 并发单赢家 ──
  ok('SG4a 未知版本激活 → 404', (await call(admin, 'POST', '/api/mu/skills/rag.retrieve/activate',
    { csrf: true, body: { version: '2.0.0' } })).status === 404);
  const act2 = await call(admin, 'POST', '/api/mu/skills/rag.retrieve/activate', { csrf: true, body: { version: '1.1.0' } });
  ok('SG4b 激活 1.1.0 → rollback=false', act2.status === 200 && act2.body?.rollback === false && act2.body?.idempotent === false);
  const back1 = await call(admin, 'POST', '/api/mu/skills/rag.retrieve/activate', { csrf: true, body: { version: '1.0.0' } });
  ok('SG4c 回滚 1.0.0 → rollback=true 且 current 切换', back1.status === 200 && back1.body?.rollback === true
    && back1.body?.current_version === '1.0.0');
  const again = await call(admin, 'POST', '/api/mu/skills/rag.retrieve/activate', { csrf: true, body: { version: '1.0.0' } });
  ok('SG4d 重复回滚同目标 → 幂等 200', again.status === 200 && again.body?.idempotent === true);
  // 并发同目标：恰一个赢家（从 1.0.0 并发打 1.1.0 两次）
  const actBefore = (await pool.query(
    `SELECT count(*)::int c FROM mu.audit_event WHERE kind='MU_SKILL_ACTIVATED'`)).rows[0].c;
  const [w1, w2] = await Promise.all([
    call(admin, 'POST', '/api/mu/skills/rag.retrieve/activate', { csrf: true, body: { version: '1.1.0' } }),
    call(admin, 'POST', '/api/mu/skills/rag.retrieve/activate', { csrf: true, body: { version: '1.1.0' } }),
  ]);
  const winners = [w1, w2].filter((x) => x.status === 200 && x.body?.idempotent === false).length;
  ok('SG4e 并发激活恰一赢家（另一幂等）', winners === 1 && w1.status === 200 && w2.status === 200,
    { w1: w1.body?.idempotent, w2: w2.body?.idempotent });
  const rollAudits = (await pool.query(
    `SELECT count(*)::int c FROM mu.audit_event WHERE kind='MU_SKILL_ACTIVATED'`)).rows[0].c;
  ok('SG4f 并发对仅新增一笔激活审计（无重复）', rollAudits === actBefore + 1, { actBefore, rollAudits });

  // ── SG5：停用/启用（幂等）──
  const dis = await call(admin, 'POST', '/api/mu/skills/rag.retrieve/disable', { csrf: true, body: {} });
  ok('SG5a 停用 → 200 state=disabled', dis.status === 200 && dis.body?.state === 'disabled');
  const dis2 = await call(admin, 'POST', '/api/mu/skills/rag.retrieve/disable', { csrf: true, body: {} });
  ok('SG5b 重复停用 → 幂等 200', dis2.status === 200 && dis2.body?.idempotent === true);
  ok('SG5c 启用 → 200 state=active', (await call(admin, 'POST', '/api/mu/skills/rag.retrieve/enable',
    { csrf: true, body: {} })).body?.state === 'active');

  // ── SG6：读矩阵（contributor/reviewer/maintainer 可读；auditor 403）──
  for (const [label, sess] of [['maintainer', maint], ['contributor', contrib], ['reviewer', reviewer]]) {
    ok(`SG6a ${label} 读列表/历史 → 200`, (await call(sess, 'GET', '/api/mu/skills')).status === 200
      && (await call(sess, 'GET', '/api/mu/skills/rag.retrieve/versions')).status === 200);
  }
  ok('SG6b auditor 读 → 403（仅 read_audit）', (await call(auditor, 'GET', '/api/mu/skills')).status === 403);
  ok('SG6c contributor 写面全 403（publish/activate/disable）',
    (await call(contrib, 'POST', '/api/mu/skills/rag.retrieve/versions', { csrf: true, body: { version: '1.2.0', changelog: 'c', manifest_sha256: SHA('d') } })).status === 403
    && (await call(contrib, 'POST', '/api/mu/skills/rag.retrieve/activate', { csrf: true, body: { version: '1.0.0' } })).status === 403
    && (await call(contrib, 'POST', '/api/mu/skills/rag.retrieve/disable', { csrf: true, body: {} })).status === 403);

  // ── SG7：跨租户（T2 平台管理员对 T1 技能=404，不泄露存在性）──
  const reg2 = await call(admin2, 'POST', '/api/mu/skills',
    { csrf: true, body: { skill_key: 't2.only', display_name: 'T2 技能' } });
  ok('SG7a T2 管理员注册自己租户技能 → 200', reg2.status === 200);
  ok('SG7b T2 读 T1 技能详情/历史/激活 → 404',
    (await call(admin2, 'GET', '/api/mu/skills/rag.retrieve')).status === 404
    && (await call(admin2, 'GET', '/api/mu/skills/rag.retrieve/versions')).status === 404
    && (await call(admin2, 'POST', '/api/mu/skills/rag.retrieve/activate', { csrf: true, body: { version: '1.0.0' } })).status === 404);
  const l2 = await call(admin2, 'GET', '/api/mu/skills');
  ok('SG7c T2 列表零 T1 技能（tenant 收窄）', (l2.body?.skills ?? []).every((s) => s.skill_key !== 'rag.retrieve')
    && l2.body?.skills?.some((s) => s.skill_key === 't2.only'));
  const l1 = await call(admin, 'GET', '/api/mu/skills');
  ok('SG7d T1 列表零 T2 技能', (l1.body?.skills ?? []).every((s) => s.skill_key !== 't2.only'));

  // ── SG8：审计脱敏（detail 键白名单 + 无敏感形状；changelog 不得入审计）──
  const ev = (await pool.query(`SELECT kind, detail FROM mu.audit_event WHERE kind LIKE 'MU_SKILL%'`)).rows;
  ok('SG8a 技能审计事件≥5 类齐', new Set(ev.map((e) => e.kind)).size >= 5,
    [...new Set(ev.map((e) => e.kind))]);
  const ALLOWED = new Set(['skill_key', 'version', 'rollback', 'state']);
  const badKeys = [];
  for (const e of ev) for (const k of Object.keys(e.detail ?? {})) if (!ALLOWED.has(k)) badKeys.push(k);
  ok('SG8b 审计 detail 键全在白名单（无 changelog/prompt/工件）', badKeys.length === 0, [...new Set(badKeys)]);
  const raw = JSON.stringify(ev);
  ok('SG8c 审计零敏感形状（指纹/secret/DSN 全无）', !/(sk|ghp|gho)_[A-Za-z0-9]{10,}/.test(raw)
    && !/postgres:\/\/[^\s"']+:[^\s"']+@/.test(raw) && !/[0-9a-f]{64}/.test(raw));
  ok('SG8d changelog 内容不入审计（排序改进/初始版零出现）',
    !raw.includes('排序改进') && !raw.includes('初始版'));

  // ── SG9：重启语义（进程内服务仅一层——以 schema 重放+数据持久代证）──
  const kept = (await pool.query(`
    SELECT (SELECT count(*)::int FROM mu.skill) + (SELECT count(*)::int FROM mu.skill_version) AS kept`)).rows[0].kept;
  ok('SG9 技能/版本行持久（skill+version ≥ 3 行）', Number(kept) >= 3, kept);
} finally {
  try { server.close(); } catch { /* */ }
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ }
  await pool.end().catch(() => {});
}
console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
