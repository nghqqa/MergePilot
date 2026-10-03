#!/usr/bin/env node
// console/backend/test/mu-skill-governance.integration.mjs — v17 技能版本治理面集成测试
// （B 波：/api/mu/skills 全矩阵——迁移/状态机/不可变/CAS 并发/幂等/RBAC/CSRF/跨租户/审计脱敏
//   + PR-9 整改回归：发布响应含服务端真值 current_version/activated、首版自动激活 CAS 并发
//     恰一双赢家、恒成功死端点 /versions/:v/publish 已删除（404）、长输入/空列表/错误态。）
// 运行：node console/backend/test/mu-skill-governance.integration.mjs
// （env: FXV_PG_TEST_DSN——设了则直连该一次性 PG（可重复执行，标识均按 run 唯一）；
//   未设则自起一次性 postgres:16-alpine + 进程内 createConsole 真实 HTTP；跑毕 docker rm -f。）
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

// 标识按 run 唯一：FXV_PG_TEST_DSN 直连持久库时重复执行不撞历史数据
const RUN = crypto.randomBytes(3).toString('hex');
const dsnFromEnv = process.env.FXV_PG_TEST_DSN || null;
let container = null;
let dsn;
if (dsnFromEnv) {
  dsn = dsnFromEnv;
  console.log(`  NOTE  使用外部一次性 PG（FXV_PG_TEST_DSN），run=${RUN}`);
} else {
  container = `mu-sg-it-${crypto.randomBytes(4).toString('hex')}`;
  const PGPORT = 17900 + Math.floor(Math.random() * 60);
  execFileSync('docker', ['run', '-d', '--name', container,
    '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
    '-p', `127.0.0.1:${PGPORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
  dsn = `postgres://postgres:x@127.0.0.1:${PGPORT}/mu`;
}
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
process.env.CONSOLE_SESSION_SECRET = `mu-sg-session-secret-${RUN}`;
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
const K = (base) => `${base}.${RUN}`; // run 唯一技能 key（总长受 skill_key 2-64 限制约束）

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

  // ── SG0：迁移到最新版（动态取自 MU_MIGRATIONS——v18 起免跟版；外部 DSN 复跑时已迁移，幂等）──
  const mv = (await pool.query(`SELECT max(version) AS v FROM mu.schema_migrations`)).rows[0].v;
  ok(`SG0a 迁移到 v${LATEST_SCHEMA_VERSION}（外部 DSN 复跑幂等）`, Number(mv) === LATEST_SCHEMA_VERSION, { v: mv });

  // 本 run 专属租户与用户（外部 DSN 复跑不与历史 run 串数据）
  const T1 = (await pool.query(`INSERT INTO mu.tenant (slug, display_name) VALUES ($1,$2) RETURNING tenant_id`,
    [`sg-t1-${RUN}`, `SG T1 ${RUN}`])).rows[0].tenant_id;
  const mkUser = async (tenantId, login, role) => {
    const u = (await pool.query(`INSERT INTO mu.app_user (login, display_name) VALUES ($1,$1) RETURNING user_id`, [login])).rows[0];
    await pool.query(`INSERT INTO mu.membership (tenant_id, user_id, role) VALUES ($1,$2,$3)`, [tenantId, u.user_id, role]);
    await pool.query(`INSERT INTO mu.external_identity (provider, subject, user_id) VALUES ('fixture',$1,$2)`, [`fixture:${login}`, u.user_id]);
  };
  await mkUser(T1, `sg-admin-${RUN}`, 'platform_admin');
  await mkUser(T1, `sg-maint-${RUN}`, 'maintainer');
  await mkUser(T1, `sg-contrib-${RUN}`, 'contributor');
  await mkUser(T1, `sg-reviewer-${RUN}`, 'reviewer');
  await mkUser(T1, `sg-auditor-${RUN}`, 'auditor');
  // 第二租户（跨租户隔离对）
  const T2 = (await pool.query(`INSERT INTO mu.tenant (slug, display_name) VALUES ($1,$2) RETURNING tenant_id`,
    [`sg-t2-${RUN}`, `SG T2 ${RUN}`])).rows[0].tenant_id;
  await mkUser(T2, `sg-admin2-${RUN}`, 'platform_admin');

  const admin = await muLogin(`fixture:sg-admin-${RUN}`, `sg-t1-${RUN}`);
  const maint = await muLogin(`fixture:sg-maint-${RUN}`, `sg-t1-${RUN}`);
  const contrib = await muLogin(`fixture:sg-contrib-${RUN}`, `sg-t1-${RUN}`);
  const reviewer = await muLogin(`fixture:sg-reviewer-${RUN}`, `sg-t1-${RUN}`);
  const auditor = await muLogin(`fixture:sg-auditor-${RUN}`, `sg-t1-${RUN}`);
  const admin2 = await muLogin(`fixture:sg-admin2-${RUN}`, `sg-t2-${RUN}`);

  const SK1 = K('rag.retrieve');

  // ── SG1：注册（RBAC/CSRF/校验/幂等冲突/长输入）──
  ok('SG1a 未认证 GET → 401', (await call(null, 'GET', '/api/mu/skills')).status === 401);
  ok('SG1b 缺 CSRF 注册 → 403 csrf_required',
    (await call(admin, 'POST', '/api/mu/skills', { body: { skill_key: SK1, display_name: '检索' } })).status === 403);
  for (const [label, sess] of [['maintainer', maint], ['contributor', contrib], ['reviewer', reviewer]]) {
    const r = await call(sess, 'POST', '/api/mu/skills', { csrf: true, body: { skill_key: K('x.no'), display_name: 'x' } });
    ok(`SG1c ${label} 注册 → 403（manage_instance 缺）`, r.status === 403, r.status);
  }
  ok('SG1d 非法 skill_key → 400', (await call(admin, 'POST', '/api/mu/skills',
    { csrf: true, body: { skill_key: 'Bad Key', display_name: 'x' } })).status === 400);
  const reg = await call(admin, 'POST', '/api/mu/skills',
    { csrf: true, body: { skill_key: SK1, display_name: '审查检索技能', description: 'd' } });
  ok('SG1e platform_admin 注册 → 200', reg.status === 200 && reg.body?.skill?.skill_key === SK1, reg.status);
  ok('SG1f 重复 skill_key → 409 skill_key_exists', (await call(admin, 'POST', '/api/mu/skills',
    { csrf: true, body: { skill_key: SK1, display_name: 'x' } })).status === 409);
  ok('SG1g 超长 skill_key（65 字符）→ 400（2-64 限长）', (await call(admin, 'POST', '/api/mu/skills',
    { csrf: true, body: { skill_key: 'a'.repeat(65), display_name: 'x' } })).status === 400);
  ok('SG1h 超长 display_name（200 汉字）→ 200 且不崩溃', (await call(admin, 'POST', '/api/mu/skills',
    { csrf: true, body: { skill_key: K('long.name'), display_name: '长'.repeat(200) } })).status === 200);

  // ── SG2：发布版本（校验/首版自动激活 CAS/不可变/幂等/响应真值形状）──
  ok('SG2a 未知技能发布 → 404', (await call(admin, 'POST', `/api/mu/skills/${K('nope')}/versions`,
    { csrf: true, body: { version: '1.0.0', changelog: 'c', manifest_sha256: SHA('a') } })).status === 404);
  ok('SG2b 非语义化版本 → 400', (await call(admin, 'POST', `/api/mu/skills/${SK1}/versions`,
    { csrf: true, body: { version: '1.0', changelog: 'c', manifest_sha256: SHA('a') } })).status === 400);
  ok('SG2c 非法指纹 → 400', (await call(admin, 'POST', `/api/mu/skills/${SK1}/versions`,
    { csrf: true, body: { version: '1.0.0', changelog: 'c', manifest_sha256: 'zz' } })).status === 400);
  const v1 = await call(admin, 'POST', `/api/mu/skills/${SK1}/versions`,
    { csrf: true, body: { version: '1.0.0', changelog: '初始版', manifest_sha256: SHA('a'), artifact_ref: `skills/rag/1.0.0/` } });
  ok('SG2d 发布 1.0.0 → 200 首版自动激活（activated=true）', v1.status === 200 && v1.body?.idempotent === false
    && v1.body?.activated === true, v1.body);
  ok('SG2d2 发布响应含服务端真值 current_version=1.0.0', v1.body?.current_version === '1.0.0'
    && v1.body?.version?.version === '1.0.0', v1.body?.current_version);
  const det1 = await call(admin, 'GET', `/api/mu/skills/${SK1}`);
  ok('SG2e 详情显示 active=1.0.0（version_count=1）',
    det1.status === 200 && det1.body?.skill?.current_version === '1.0.0' && Number(det1.body?.skill?.version_count) === 1,
    det1.body?.skill?.current_version);
  const v2 = await call(admin, 'POST', `/api/mu/skills/${SK1}/versions`,
    { csrf: true, body: { version: '1.1.0', changelog: '排序改进', manifest_sha256: SHA('b') } });
  ok('SG2f 发布 1.1.0 → 200 且 activated=false、current_version 真值=1.0.0', v2.status === 200
    && v2.body?.activated === false && v2.body?.current_version === '1.0.0'
    && (await call(admin, 'GET', `/api/mu/skills/${SK1}`)).body?.skill?.current_version === '1.0.0',
    v2.body?.current_version);
  const dupSame = await call(admin, 'POST', `/api/mu/skills/${SK1}/versions`,
    { csrf: true, body: { version: '1.1.0', changelog: '重复发布', manifest_sha256: SHA('b') } });
  ok('SG2g 重复发布同版本同指纹 → 200 幂等且含真值 current_version', dupSame.status === 200
    && dupSame.body?.idempotent === true && dupSame.body?.current_version === '1.0.0'
    && dupSame.body?.activated === false, dupSame.body);
  const dupDiff = await call(admin, 'POST', `/api/mu/skills/${SK1}/versions`,
    { csrf: true, body: { version: '1.1.0', changelog: '换指纹', manifest_sha256: SHA('c') } });
  ok('SG2h 同版本不同指纹 → 409 version_immutable_conflict', dupDiff.status === 409
    && dupDiff.body?.error?.reason === 'version_immutable_conflict', dupDiff.body?.error);
  const rowSha = (await pool.query(
    `SELECT manifest_sha256, changelog FROM mu.skill_version WHERE skill_id=(SELECT skill_id FROM mu.skill WHERE skill_key=$1) AND version='1.1.0'`, [SK1])).rows[0];
  ok('SG2i 冲突后版本行原样（指纹/说明未被覆盖）', rowSha.manifest_sha256 === SHA('b') && rowSha.changelog === '排序改进');

  // ── SG2j/k：首版自动激活 CAS 并发 + 同版本并发发布（UNIQUE 收口）──
  const CASK = K('cas.first');
  await call(admin, 'POST', '/api/mu/skills', { csrf: true, body: { skill_key: CASK, display_name: 'CAS 并发' } });
  const [p1, p2] = await Promise.all([
    call(admin, 'POST', `/api/mu/skills/${CASK}/versions`,
      { csrf: true, body: { version: '1.0.0', changelog: 'a', manifest_sha256: SHA('a') } }),
    call(admin, 'POST', `/api/mu/skills/${CASK}/versions`,
      { csrf: true, body: { version: '1.0.1', changelog: 'b', manifest_sha256: SHA('b') } }),
  ]);
  const winners = [p1, p2].filter((x) => x.status === 200 && x.body?.activated === true);
  ok('SG2j 并发发布两个首版 → 恰一双赢家（无双重激活）', p1.status === 200 && p2.status === 200
    && winners.length === 1, { p1: p1.body?.activated, p2: p2.body?.activated });
  const casCur = (await pool.query(`SELECT current_version FROM mu.skill WHERE skill_key=$1`, [CASK])).rows[0]?.current_version;
  const winVer = winners[0]?.body?.current_version;
  ok('SG2j2 赢家响应 current_version=服务端真值=winner 版本', casCur === winVer
    && (casCur === '1.0.0' || casCur === '1.0.1'), { casCur, winVer });
  const casRows = (await pool.query(
    `SELECT count(*)::int c FROM mu.skill_version WHERE skill_id=(SELECT skill_id FROM mu.skill WHERE skill_key=$1)`, [CASK])).rows[0].c;
  ok('SG2j3 并发首版两个版本行都入库（败者不丢、但未激活）', casRows === 2, casRows);

  const CASK2 = K('cas.same');
  await call(admin, 'POST', '/api/mu/skills', { csrf: true, body: { skill_key: CASK2, display_name: '同版本并发' } });
  const [q1, q2] = await Promise.all([
    call(admin, 'POST', `/api/mu/skills/${CASK2}/versions`,
      { csrf: true, body: { version: '1.0.0', changelog: '同', manifest_sha256: SHA('e') } }),
    call(admin, 'POST', `/api/mu/skills/${CASK2}/versions`,
      { csrf: true, body: { version: '1.0.0', changelog: '同', manifest_sha256: SHA('e') } }),
  ]);
  const idem = [q1, q2].filter((x) => x.status === 200 && x.body?.idempotent === true).length;
  ok('SG2k 并发发布同版本同指纹 → 双 200 恰一幂等（UNIQUE 23505 收口）', q1.status === 200 && q2.status === 200
    && idem === 1, { s1: q1.status, s2: q2.status, b1: q1.body, b2: q2.body });
  const sameRows = (await pool.query(
    `SELECT count(*)::int c FROM mu.skill_version WHERE skill_id=(SELECT skill_id FROM mu.skill WHERE skill_key=$1)`, [CASK2])).rows[0].c;
  ok('SG2k2 同版本仅一行入库', sameRows === 1, sameRows);

  // ── SG3：死端点已删除 + 历史含发布者 + 空列表/长版本号 ──
  const dead = await call(admin, 'POST', `/api/mu/skills/${SK1}/versions/1.1.0/publish`, { csrf: true, body: {} });
  ok('SG3a 恒成功死端点 /versions/:v/publish 已删除 → 404', dead.status === 404
    && String(dead.body?.error?.reason ?? '').startsWith('unknown mu path'), dead.body);
  ok('SG3b 死端点 GET 同样不存在 → 404',
    (await call(admin, 'GET', `/api/mu/skills/${SK1}/versions/1.1.0/publish`)).status === 404);
  const hist = await call(admin, 'GET', `/api/mu/skills/${SK1}/versions`);
  ok('SG3c 历史按时间倒序且含发布者', hist.status === 200
    && hist.body?.versions?.[0]?.version === '1.1.0' && hist.body?.versions?.[0]?.published_by === `sg-admin-${RUN}`,
    hist.body?.versions?.[0]);
  ok('SG3d 未知技能详情/历史 → 404', (await call(admin, 'GET', `/api/mu/skills/${K('nope')}`)).status === 404
    && (await call(admin, 'GET', `/api/mu/skills/${K('nope')}/versions`)).status === 404);
  const emptyKey = K('empty.list');
  await call(admin, 'POST', '/api/mu/skills', { csrf: true, body: { skill_key: emptyKey, display_name: '空列表' } });
  const emptyHist = await call(admin, 'GET', `/api/mu/skills/${emptyKey}/versions`);
  ok('SG3e 从未发布技能历史 → 200 versions:[]（前端空态依赖）', emptyHist.status === 200
    && Array.isArray(emptyHist.body?.versions) && emptyHist.body.versions.length === 0, emptyHist.body);
  const longVer = `${'9'.repeat(40)}.0.1`;
  const lv = await call(admin, 'POST', `/api/mu/skills/${emptyKey}/versions`,
    { csrf: true, body: { version: longVer, changelog: '长版本号', manifest_sha256: SHA('d') } });
  const lvHist = await call(admin, 'GET', `/api/mu/skills/${emptyKey}/versions`);
  ok('SG3f 长版本号（40 位主版本）→ 发布 200 且历史可回读', lv.status === 200
    && lvHist.body?.versions?.[0]?.version === longVer, lv.body?.error);

  // ── SG4：激活/回滚状态机 + 并发语义（同目标幂等 / 异目标 last-write-wins）──
  ok('SG4a 未知版本激活 → 404', (await call(admin, 'POST', `/api/mu/skills/${SK1}/activate`,
    { csrf: true, body: { version: '2.0.0' } })).status === 404);
  ok('SG4a2 缺 version 激活 → 404 version_not_found', (await call(admin, 'POST', `/api/mu/skills/${SK1}/activate`,
    { csrf: true, body: {} })).status === 404);
  ok('SG4a3 缺 CSRF 激活 → 403', (await call(admin, 'POST', `/api/mu/skills/${SK1}/activate`,
    { body: { version: '1.1.0' } })).status === 403);
  const act2 = await call(admin, 'POST', `/api/mu/skills/${SK1}/activate`, { csrf: true, body: { version: '1.1.0' } });
  ok('SG4b 激活 1.1.0 → rollback=false', act2.status === 200 && act2.body?.rollback === false && act2.body?.idempotent === false);
  const back1 = await call(admin, 'POST', `/api/mu/skills/${SK1}/activate`, { csrf: true, body: { version: '1.0.0' } });
  ok('SG4c 回滚 1.0.0 → rollback=true 且 current 切换', back1.status === 200 && back1.body?.rollback === true
    && back1.body?.current_version === '1.0.0');
  const again = await call(admin, 'POST', `/api/mu/skills/${SK1}/activate`, { csrf: true, body: { version: '1.0.0' } });
  ok('SG4d 重复回滚同目标 → 幂等 200（响应含真值 current_version）', again.status === 200 && again.body?.idempotent === true
    && again.body?.current_version === '1.0.0');
  // 并发同目标：恰一个赢家（从 1.0.0 并发打 1.1.0 两次）
  const actBefore = (await pool.query(
    `SELECT count(*)::int c FROM mu.audit_event WHERE kind='MU_SKILL_ACTIVATED'`)).rows[0].c;
  const [w1, w2] = await Promise.all([
    call(admin, 'POST', `/api/mu/skills/${SK1}/activate`, { csrf: true, body: { version: '1.1.0' } }),
    call(admin, 'POST', `/api/mu/skills/${SK1}/activate`, { csrf: true, body: { version: '1.1.0' } }),
  ]);
  const winners2 = [w1, w2].filter((x) => x.status === 200 && x.body?.idempotent === false).length;
  ok('SG4e 并发激活同目标恰一赢家（另一幂等）', winners2 === 1 && w1.status === 200 && w2.status === 200,
    { w1: w1.body?.idempotent, w2: w2.body?.idempotent });
  const rollAudits = (await pool.query(
    `SELECT count(*)::int c FROM mu.audit_event WHERE kind='MU_SKILL_ACTIVATED'`)).rows[0].c;
  ok('SG4f 并发同目标对仅新增一笔激活审计（无重复）', rollAudits === actBefore + 1, { actBefore, rollAudits });
  // 异目标并发：先备好第三版本，使两个并发目标都 ≠ 当前指针 → 两笔都生效（last-write-wins），各审计一笔
  ok('SG4g0 预发布 1.2.0（不激活）→ 200', (await call(admin, 'POST', `/api/mu/skills/${SK1}/versions`,
    { csrf: true, body: { version: '1.2.0', changelog: '并发靶', manifest_sha256: SHA('f') } })).status === 200);
  const actBeforeX = (await pool.query(
    `SELECT count(*)::int c FROM mu.audit_event WHERE kind IN ('MU_SKILL_ACTIVATED','MU_SKILL_ROLLED_BACK')`)).rows[0].c;
  const [x1, x2] = await Promise.all([
    call(admin, 'POST', `/api/mu/skills/${SK1}/activate`, { csrf: true, body: { version: '1.2.0' } }),
    call(admin, 'POST', `/api/mu/skills/${SK1}/activate`, { csrf: true, body: { version: '1.0.0' } }),
  ]);
  const changed = [x1, x2].filter((x) => x.status === 200 && x.body?.idempotent === false).length;
  ok('SG4g 异目标并发 → 两笔均变更（last-write-wins，各审计）', changed === 2, { x1: x1.body, x2: x2.body });
  const actAfterX = (await pool.query(
    `SELECT count(*)::int c FROM mu.audit_event WHERE kind IN ('MU_SKILL_ACTIVATED','MU_SKILL_ROLLED_BACK')`)).rows[0].c;
  ok('SG4g2 异目标并发新增两笔审计', actAfterX === actBeforeX + 2, { actBeforeX, actAfterX });

  // ── SG5：停用/启用（幂等）──
  const dis = await call(admin, 'POST', `/api/mu/skills/${SK1}/disable`, { csrf: true, body: {} });
  ok('SG5a 停用 → 200 state=disabled', dis.status === 200 && dis.body?.state === 'disabled');
  const dis2 = await call(admin, 'POST', `/api/mu/skills/${SK1}/disable`, { csrf: true, body: {} });
  ok('SG5b 重复停用 → 幂等 200', dis2.status === 200 && dis2.body?.idempotent === true);
  ok('SG5c 启用 → 200 state=active', (await call(admin, 'POST', `/api/mu/skills/${SK1}/enable`,
    { csrf: true, body: {} })).body?.state === 'active');

  // ── SG6：读矩阵（contributor/reviewer/maintainer 可读；auditor 403）──
  for (const [label, sess] of [['maintainer', maint], ['contributor', contrib], ['reviewer', reviewer]]) {
    ok(`SG6a ${label} 读列表/历史 → 200`, (await call(sess, 'GET', '/api/mu/skills')).status === 200
      && (await call(sess, 'GET', `/api/mu/skills/${SK1}/versions`)).status === 200);
  }
  ok('SG6b auditor 读 → 403（仅 read_audit）', (await call(auditor, 'GET', '/api/mu/skills')).status === 403);
  ok('SG6c contributor 写面全 403（publish/activate/disable）',
    (await call(contrib, 'POST', `/api/mu/skills/${SK1}/versions`, { csrf: true, body: { version: '1.2.0', changelog: 'c', manifest_sha256: SHA('d') } })).status === 403
    && (await call(contrib, 'POST', `/api/mu/skills/${SK1}/activate`, { csrf: true, body: { version: '1.0.0' } })).status === 403
    && (await call(contrib, 'POST', `/api/mu/skills/${SK1}/disable`, { csrf: true, body: {} })).status === 403);

  // ── SG7：跨租户（T2 平台管理员对 T1 技能=404，不泄露存在性）──
  const reg2 = await call(admin2, 'POST', '/api/mu/skills',
    { csrf: true, body: { skill_key: K('t2.only'), display_name: 'T2 技能' } });
  ok('SG7a T2 管理员注册自己租户技能 → 200', reg2.status === 200);
  ok('SG7b T2 读 T1 技能详情/历史/激活 → 404',
    (await call(admin2, 'GET', `/api/mu/skills/${SK1}`)).status === 404
    && (await call(admin2, 'GET', `/api/mu/skills/${SK1}/versions`)).status === 404
    && (await call(admin2, 'POST', `/api/mu/skills/${SK1}/activate`, { csrf: true, body: { version: '1.0.0' } })).status === 404);
  const l2 = await call(admin2, 'GET', '/api/mu/skills');
  ok('SG7c T2 列表零 T1 技能（tenant 收窄）', (l2.body?.skills ?? []).every((s) => s.skill_key !== SK1)
    && l2.body?.skills?.some((s) => s.skill_key === K('t2.only')));
  const l1 = await call(admin, 'GET', '/api/mu/skills');
  ok('SG7d T1 列表零 T2 技能', (l1.body?.skills ?? []).every((s) => s.skill_key !== K('t2.only')));

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
  const pubAudits = (await pool.query(
    `SELECT count(*)::int c FROM mu.audit_event WHERE kind='MU_SKILL_VERSION_PUBLISHED' AND detail->>'skill_key'=$1`, [CASK])).rows[0].c;
  ok('SG8e 并发首版恰好两笔发布审计（每 insert 一笔，无激活重复审计）', pubAudits === 2, pubAudits);

  // ── SG9：重启语义（进程内服务仅一层——以 schema 重放+数据持久代证）──
  const kept = (await pool.query(`
    SELECT (SELECT count(*)::int FROM mu.skill) + (SELECT count(*)::int FROM mu.skill_version) AS kept`)).rows[0].kept;
  ok('SG9 技能/版本行持久（skill+version ≥ 3 行）', Number(kept) >= 3, kept);
} finally {
  try { server.close(); } catch { /* */ }
  if (container) {
    try { execFileSync('docker', ['rm', '-f', '-v', container], { stdio: 'pipe' }); } catch { /* */ }
  }
  await pool.end().catch(() => {});
}
console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
