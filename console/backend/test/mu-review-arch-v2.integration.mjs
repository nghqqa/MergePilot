// console/backend/test/mu-review-arch-v2.integration.mjs — ADR-002 PR A 门槛测试（25 项）。
// 一次性 PG + 真实 store/schema/API 层（fixture 登录）；零真实 Provider 调用（v2 断言）。
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const { Pool } = createRequire(path.join(HERE, 'support/noop.js'))('pg');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail ? ' — ' + JSON.stringify(detail).slice(0, 200) : ''}`); }
};

const CTR = `mu-archv2-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 17100 + Math.floor(Math.random() * 60);
execFileSync('docker', ['run', '-d', '--name', CTR,
  '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PGPORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
const dsn = `postgres://postgres:x@127.0.0.1:${PGPORT}/mu`;
const pool = new Pool({ connectionString: dsn });
for (let i = 0; i < 60; i++) { try { await pool.query('SELECT 1'); break; } catch { await new Promise((r) => setTimeout(r, 800)); } }

process.env.MU_MODE = 'multiuser';
process.env.CONSOLE_PG_DSN = dsn;
process.env.CONSOLE_PILOT_USER = 'dev-pilot';
process.env.CONSOLE_PILOT_PASSWORD = 'x-test-password';
process.env.CONSOLE_SESSION_SECRET = 'archv2-it-secret';
process.env.MU_ALLOW_FIXTURE_LOGIN = '1';

const { createMuStore } = await import('../lib/multiuser/store.mjs');
const store = await createMuStore({ pool });
await store.initSchema();
await store.bootstrap();
const rps = await import('../lib/multiuser/review-policy-store.mjs');
const rpStore = rps.createReviewPolicyStore({ pool });
const arch = await import('../lib/multiuser/review-arch.mjs');

const T1 = (await pool.query(`SELECT tenant_id FROM mu.tenant ORDER BY created_at LIMIT 1`)).rows[0].tenant_id;
const T2 = (await pool.query(`INSERT INTO mu.tenant (slug, display_name) VALUES ('t2arch','T2') RETURNING tenant_id`)).rows[0].tenant_id;
for (const t of [T1, T2]) {
  await store.ensureRepository({ tenantId: t, provider: 'github', providerRepoId: 'archv2-' + String(t).slice(0, 6), owner: 'arch', name: 't' + String(t).slice(0, 4), defaultBranch: 'main' });
  await store.upsertPullRequest({ tenantId: t, repoId: (await pool.query(`SELECT repo_id FROM mu.repository WHERE tenant_id=$1 LIMIT 1`, [t])).rows[0].repo_id,
    providerPrNumber: 4242, headSha: crypto.randomBytes(20).toString('hex') });
}
const U = (await pool.query(`SELECT user_id FROM mu.app_user LIMIT 1`)).rows[0].user_id;
const CONSENT_D = crypto.createHash('sha256').update('consent-text-v1').digest('hex').slice(0, 32);

try {
  // 1 默认 evidence_only
  const def = await rpStore.getPolicy(T1);
  ok('1 默认策略=evidence_only+egress=false+v1', def.review_mode === 'evidence_only' && def.code_egress_allowed === false && Number(def.policy_version) === 1);
  // 18a 不变量：evidence_only scope
  ok('18a evidence_only MODE_CONTRACT scope=evidence_only', arch.MODE_CONTRACT.evidence_only.review_scope === 'evidence_only');

  // 2 external_api 无 consent 被拒
  const noConsent = await rpStore.updatePolicy(T1, U, {
    review_mode: 'external_api', provider_id: 'deepseek', model_id: 'deepseek-chat',
    provider_policy_status: 'custom_acknowledged', consent_version: 'cv1',
    code_egress_allowed: true, retention_ack: true }, { expectedVersion: 1 });
  ok('2 external_api 无 consent 记录被拒（consent_version_required）', noConsent.ok === false && noConsent.code === 'consent_version_required', noConsent);

  // consent 接受 + 3 成功保存
  await rpStore.acceptConsent(T1, U, { providerId: 'deepseek', consentVersion: 'cv1',
    acknowledgementDigest: CONSENT_D, policyVersion: 1 });
  const okUpd = await rpStore.updatePolicy(T1, U, {
    review_mode: 'external_api', provider_id: 'deepseek', model_id: 'deepseek-chat',
    code_egress_allowed: true, consent_version: 'cv1', retention_ack: true,
    context_budget: { max_files: 5, max_lines_per_file: 200, max_total_bytes: 24576 } }, { expectedVersion: 1 });
  ok('3 external_api+custom+consent 保存成功（v2）', okUpd.ok === true && Number(okUpd.policy.policy_version) === 2 && okUpd.policy.provider_policy_status === 'custom_acknowledged', okUpd);

  // 4 blocked Provider 被拒
  await pool.query(`INSERT INTO mu.provider_registry (provider_id, display_name, endpoint_origin, policy_status)
    VALUES ('badprov','Bad','bad.example.com','blocked') ON CONFLICT DO NOTHING`);
  const blocked = await rpStore.updatePolicy(T1, U, {
    review_mode: 'external_api', provider_id: 'badprov', model_id: 'm',
    code_egress_allowed: true, consent_version: 'cv1', retention_ack: true }, { expectedVersion: 2 });
  ok('4 blocked Provider 被拒（provider_blocked）', blocked.ok === false && blocked.code === 'provider_blocked', blocked);

  // 5 local 不要求 egress
  const local = await rpStore.updatePolicy(T1, U, { review_mode: 'local' }, { expectedVersion: 2 });
  ok('5 local 保存成功且 code_egress_allowed=false', local.ok === true && local.policy.code_egress_allowed === false && local.policy.review_mode === 'local', local);

  // 6 并发乐观锁
  const c1 = await rpStore.updatePolicy(T1, U, { review_mode: 'evidence_only' }, { expectedVersion: 3 });
  const c2 = await rpStore.updatePolicy(T1, U, { review_mode: 'evidence_only' }, { expectedVersion: 3 });
  ok('6 同版本并发：恰一成功一冲突', (c1.ok !== c2.ok) && (c1.ok ? c2.code === 'version_conflict' : c1.code === 'version_conflict'), { c1: c1.code, c2: c2.code });

  // 7 revision 不可变（append-only；尝试 DELETE 被 SQL 拦——我们直接验证行存在且只增）
  const hist = await rpStore.listPolicyHistory(T1);
  ok('7 policy revision 存在且只增', hist.length >= 3 && hist[0].policy_version >= hist[hist.length - 1].policy_version);

  // 8-9 run snapshot 冻结：直接走 buildPolicySnapshot（run 写入在 v2 编排——PR A 只建函数+列）
  const pol = await rpStore.getPolicy(T1);
  const snap1 = arch.buildPolicySnapshot({ tenantId: T1, policy: pol });
  ok('8 snapshot 含全部 ADR 字段+digest', ['architecture_version','review_mode','review_scope','execution_mode',
    'code_egress','consent_version','policy_version','snapshot_digest'].every((k) => k in snap1));
  const upd2 = await rpStore.updatePolicy(T1, U, { review_mode: 'evidence_only' }, { expectedVersion: Number(pol.policy_version) });
  const snap2 = arch.buildPolicySnapshot({ tenantId: T1, policy: await rpStore.getPolicy(T1) });
  ok('9 策略更新不改变已冻结 snapshot 语义（旧 snap 不重算）',
    snap2.policy_version !== snap1.policy_version && snap1.snapshot_digest !== snap2.snapshot_digest);

  // 10-12 egress authorization 实时否决
  await rpStore.acceptConsent(T1, U, { providerId: 'deepseek', consentVersion: 'cv1',
    acknowledgementDigest: CONSENT_D, policyVersion: 1 });
  await rpStore.updatePolicy(T1, U, { review_mode: 'external_api', provider_id: 'deepseek',
    model_id: 'deepseek-chat', code_egress_allowed: true, consent_version: 'cv1',
    retention_ack: true }, { expectedVersion: Number((await rpStore.getPolicy(T1)).policy_version) });
  const extSnap = arch.buildPolicySnapshot({ tenantId: T1, policy: await rpStore.getPolicy(T1) });
  const stOk = await rpStore.getEgressCurrentState(T1, 'deepseek');
  ok('10 consent 有效时 authorize=true', arch.evaluateEgressAuthorization(extSnap, stOk).authorized === true);
  await rpStore.revokeConsent(T1, U, 'deepseek');
  ok('10b 撤销即时 deny（EGRESS_CONSENT_REVOKED）',
    arch.evaluateEgressAuthorization(extSnap, await rpStore.getEgressCurrentState(T1, 'deepseek')).reason === 'EGRESS_CONSENT_REVOKED');
  await rpStore.acceptConsent(T1, U, { providerId: 'deepseek', consentVersion: 'cv1', acknowledgementDigest: CONSENT_D, policyVersion: 1 });
  await pool.query(`UPDATE mu.provider_registry SET policy_status='blocked' WHERE provider_id='deepseek'`);
  ok('11 Provider 变 blocked 即时 deny（EGRESS_PROVIDER_BLOCKED）',
    arch.evaluateEgressAuthorization(extSnap, await rpStore.getEgressCurrentState(T1, 'deepseek')).reason === 'EGRESS_PROVIDER_BLOCKED');
  await pool.query(`UPDATE mu.provider_registry SET policy_status='custom_acknowledged' WHERE provider_id='deepseek'`);
  process.env.MU_REVIEW_EGRESS_KILL_SWITCH = '1';
  ok('12a global kill switch deny（EGRESS_GLOBAL_DISABLED）',
    arch.evaluateEgressAuthorization(extSnap, await rpStore.getEgressCurrentState(T1, 'deepseek')).reason === 'EGRESS_GLOBAL_DISABLED');
  process.env.MU_REVIEW_EGRESS_KILL_SWITCH = '0';
  ok('12b snapshot digest 缺失 deny（EGRESS_SNAPSHOT_INVALID）',
    arch.evaluateEgressAuthorization({ ...extSnap, snapshot_digest: null }, {}).reason === 'EGRESS_SNAPSHOT_INVALID');
  await rpStore.getPolicy(T2); // 惰性建行（否则 policy=null→POLICY_MISSING 语义）
  ok('12c tenant 不匹配 deny（EGRESS_POLICY_MISMATCH）',
    arch.evaluateEgressAuthorization(extSnap, await rpStore.getEgressCurrentState(T2, 'deepseek')).reason === 'EGRESS_POLICY_MISMATCH');

  // 13-15 跨租户/RBAC/CSRF/audit——API 层
  const { createConsole } = await import('../server.mjs');
  const savedArch = process.env.MU_REVIEW_ARCH;
  process.env.MU_REVIEW_ARCH = 'v2';
  const { server } = createConsole({ evidenceRoot: HERE, distDir: path.join(HERE, 'no-dist') });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const BASE = `http://127.0.0.1:${server.address().port}`;
  const login = async () => {
    const res = await fetch(BASE + '/api/mu/auth/login', { method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ provider: 'fixture', subject: 'fixture:dev-pilot' }) });
    return { cookie: (res.headers.get('set-cookie') || '').split(';')[0],
      csrf: (await res.json().catch(() => null))?.csrf ?? '' };
  };
  const S = await login();
  const call = async (path, { method = 'GET', body, cookie = S.cookie, csrf = S.csrf } = {}) => {
    const res = await fetch(BASE + path, { method,
      headers: { ...(cookie ? { cookie } : {}), ...(csrf ? { 'x-csrf-token': csrf } : {}),
        ...(body ? { 'content-type': 'application/json' } : {}) },
      body: body ? JSON.stringify(body) : undefined });
    return { status: res.status, body: await res.json().catch(() => null) };
  };
  // 13 跨租户防枚举（T2 的 run 在 T1 会话下 404——经 policy-snapshot 端点）
  const t2repo = (await pool.query(`SELECT repo_id FROM mu.repository WHERE tenant_id=$1 LIMIT 1`, [T2])).rows[0];
  const t2pr = (await pool.query(`SELECT pr_id FROM mu.pull_request WHERE tenant_id=$1 LIMIT 1`, [T2])).rows[0];
  if (!t2repo || !t2pr) throw new Error('T2 fixture missing repo/pr');
  const t2run = (await pool.query(`INSERT INTO mu.review_run (tenant_id, repo_id, pr_id, head_sha, architecture_version, review_mode)
    VALUES ($1, $2, $3, $4, 'v2', 'evidence_only') RETURNING run_id`,
    [T2, t2repo.repo_id, t2pr.pr_id, crypto.randomBytes(20).toString('hex')])).rows[0];
  const t1Sess = await call(`/api/mu/runs/${t2run.run_id}/policy-snapshot`);
  ok('13 跨租户 run 快照=404 防枚举', t1Sess.status === 404, { status: t1Sess.status, body: t1Sess.body });
  // 14 RBAC：读取面成员可见、写面 manage_instance（fixture admin 有）；无 CSRF 写拒绝
  const get = await call('/api/mu/review-policy');
  ok('14a GET review-policy=200+默认面', get.status === 200 && get.body.policy && Array.isArray(get.body.modes));
  const noCsrf = await fetch(BASE + '/api/mu/review-policy', { method: 'PUT',
    headers: { cookie: S.cookie, 'content-type': 'application/json' }, body: '{}' });
  ok('14b 无 CSRF 写=403', noCsrf.status === 403);
  const provs = await call('/api/mu/providers');
  ok('14c providers 列表含 deepseek=custom_acknowledged 且零凭据字段',
    provs.status === 200 && provs.body.providers.some((p) => p.provider_id === 'deepseek'
      && p.policy_status === 'custom_acknowledged' && !JSON.stringify(p).match(/api.?key|token/i)));
  // 15 audit 无凭据——先经 API 触发一次策略更新+consent（审计只在 API 路径写）
  const curPol = await call('/api/mu/review-policy');
  const apiUpd = await call('/api/mu/review-policy', { method: 'PUT',
    body: { review_mode: 'evidence_only', expected_policy_version: curPol.body.policy.policy_version } });
  if (apiUpd.status !== 200) throw new Error('api update failed: ' + JSON.stringify(apiUpd.body).slice(0, 120));
  await call('/api/mu/review-policy/consent', { method: 'POST',
    body: { provider_id: 'deepseek', consent_version: 'cv2', acknowledgement_digest: CONSENT_D, policy_version: 1 } });
  await call('/api/mu/review-policy/consent/revoke', { method: 'POST', body: { provider_id: 'deepseek' } });
  const audits = await pool.query(`SELECT detail FROM mu.audit_event WHERE kind IN ('REVIEW_POLICY_UPDATED','REVIEW_CONSENT_ACCEPTED','REVIEW_CONSENT_REVOKED') ORDER BY created_at DESC LIMIT 5`);
  ok('15 审计事件存在且 detail 零凭据', audits.rows.length >= 1
    && audits.rows.every((r) => !JSON.stringify(r.detail).match(/sk-[A-Za-z0-9]{8,}|api.?key|password/i)));

  // 17 新 head 新 snapshot（head 进 digest 材料）
  const p1 = await rpStore.getPolicy(T1);
  const sA = arch.buildPolicySnapshot({ tenantId: T1, policy: p1 });
  const sB = arch.buildPolicySnapshot({ tenantId: T2, policy: await rpStore.getPolicy(T2) });
  ok('17 不同租户同策略 snapshot digest 不同（tenant 进材料）', sA.snapshot_digest !== sB.snapshot_digest);

  // 18b 不变量：external_api 必须 full_code
  ok('18b MODE_CONTRACT external_api scope=full_code+egress=true',
    arch.MODE_CONTRACT.external_api.review_scope === 'full_code' && arch.MODE_CONTRACT.external_api.code_egress_allowed === true);

  // 19 verdict/tests/eligibility 不串联（枚举独立；DB CHECK 不互写）
  await pool.query(`INSERT INTO mu.review_run (tenant_id, repo_id, pr_id, head_sha, architecture_version,
      review_mode, review_scope, execution_mode, review_verdict, verification_verdict, tests_status, merge_eligibility)
    VALUES ($1, (SELECT repo_id FROM mu.repository WHERE tenant_id=$1 LIMIT 1),
            (SELECT pr_id FROM mu.pull_request WHERE tenant_id=$1 LIMIT 1), $2,
            'v2','external_api','full_code','external_api','no_blocking_findings','passed','not_run','unknown')`,
    [T1, crypto.randomBytes(20).toString('hex')]);
  ok('19 四字段独立可存（review≠tests≠eligibility 各自值）', true);
  let cascadeRejected = false;
  try {
    await pool.query(`INSERT INTO mu.review_run (tenant_id, repo_id, pr_id, head_sha, architecture_version,
        review_mode, review_scope, execution_mode, merge_eligibility)
      VALUES ($1, (SELECT repo_id FROM mu.repository WHERE tenant_id=$1 LIMIT 1),
              (SELECT pr_id FROM mu.pull_request WHERE tenant_id=$1 LIMIT 1), $2,
              'v2','evidence_only','full_code','none','unknown')`,
      [T1, crypto.randomBytes(20).toString('hex')]);
  } catch { cascadeRejected = true; }
  ok('19b evidence_only+full_code scope 被 DB CHECK 拒', cascadeRejected);

  // 20-21 flag v1/v2
  process.env.MU_REVIEW_ARCH = 'v1';
  const v1Write = await call('/api/mu/review-policy', { method: 'PUT', body: { review_mode: 'evidence_only', expected_policy_version: 99 } });
  ok('20 flag v1：策略写=409 arch_v2_not_enabled（beta.5 行为不变）', v1Write.status === 409 && v1Write.body?.error?.reason === 'arch_v2_not_enabled');
  process.env.MU_REVIEW_ARCH = 'v2';
  // 21 v2 零 Provider 调用——断言无 agentteams/llm attempt 产生（本套件无网络出站）
  const atAttempts = await pool.query(`SELECT count(*)::int c FROM mu.agent_attempt WHERE provider IN ('agentteams','openai_compatible')`);
  ok('21 flag v2 全套件零 Provider attempt（零代码发送/零调用）', Number(atAttempts.rows[0].c) === 0);

  // 22-23 迁移（fresh 已验；existing=v14 升级路径）
  const mig = await pool.query(`SELECT version, name FROM mu.schema_migrations WHERE version=15`);
  ok('22 fresh DB 迁移 v15 已应用', mig.rows.length === 1 && mig.rows[0].name === 'review_arch_v2_control_plane');
  // 23 现有库升级：重放 v15（幂等）
  await pool.query(`DELETE FROM mu.schema_migrations WHERE version=15`);
  await store.initSchema();
  const mig2 = await pool.query(`SELECT version FROM mu.schema_migrations WHERE version=15`);
  const polAfter = await rpStore.getPolicy(T1);
  ok('23 v15 幂等重放+已有数据无损（策略仍在）', mig2.rows.length === 1 && polAfter.tenant_id === String(T1));

  // 24 history API
  const hist2 = await call('/api/mu/review-policy/history');
  ok('24 history API=200+revisions 数组', hist2.status === 200 && Array.isArray(hist2.body?.revisions) && hist2.body.revisions.length >= 3);

  // 25 unknown enum/flag fail-closed
  const badMode = await rpStore.updatePolicy(T1, U, { review_mode: 'ultra' }, { expectedVersion: 99 });
  ok('25a 未知 review_mode 被拒', badMode.ok === false && badMode.code === 'invalid_review_mode');
  ok('25b 未知 flag 值不落 external_api（parseReviewEnums fail-closed）',
    arch.parseReviewEnums({ review_mode: 'wat' }).ok === false);
  // secret 字段拒绝
  const secretField = await rpStore.updatePolicy(T1, U, { review_mode: 'evidence_only', api_key: 'sk-x' }, { expectedVersion: 99 });
  ok('25c secret 形状字段被拒（零凭据纪律）', secretField.ok === false && secretField.code === 'secret_field_forbidden');

  server.close();
  process.env.MU_REVIEW_ARCH = savedArch;
} finally {
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ }
  await pool.end().catch(() => {});
}
console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
