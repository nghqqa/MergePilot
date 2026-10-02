#!/usr/bin/env node
// console/backend/test/mu-matrix-w34.integration.mjs — Wave 3.4 Matrix 正式任务传输层测试。
// 覆盖：配置门 fail-closed/信封结构（taskId+correlationId+submissionId+role）/登录与 401 重登/
// 发送幂等（txnId 稳定）/四重绑定收集（room/sender/时间窗/marker）/重放与过期拒绝/错误 sender 拒绝/
// schema 无效重试/管线级：Matrix 缺配置 gate 拒、首条丢失重试成功、verifier FAIL→REWORK、
// 两轮无效→死信+BLOCKED、cancel 真实调用、审计摘要、零泄漏。
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const { Pool } = createRequire(path.join(HERE, 'support/noop.js'))('pg');
const mt = await import('../lib/multiuser/agents/matrix-transport.mjs');
const at = await import('../lib/multiuser/agents/agentteams-executor.mjs');
const { createMuStore } = await import('../lib/multiuser/store.mjs');
const orch = await import('../lib/multiuser/orchestration.mjs');
const fxo = await import('../lib/multiuser/agents/fix-orchestrator.mjs');

let pass = 0, fail = 0;
const ok = (n, c, d) => { c ? (pass++, console.log('  PASS  ' + n)) : (fail++, console.log('  FAIL  ' + n + (d ? ' ' + JSON.stringify(d).slice(0, 200) : ''))); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ── 配置解析 ──
ok('M1a Matrix 未配置 → rejected fail-closed', mt.resolveMatrixConfig({}).reason === 'MT_NOT_CONFIGURED');
ok('M1b 非 http URL → rejected', mt.resolveMatrixConfig({ MU_AGENTTEAMS_MATRIX_URL: 'ftp://x', MU_AGENTTEAMS_MATRIX_USER: 'u', MU_AGENTTEAMS_MATRIX_PASSWORD: 'p' }).reason === 'MT_BAD_URL');
const MT_ENV = { MU_AGENTTEAMS_MATRIX_URL: 'http://mt.test', MU_AGENTTEAMS_MATRIX_USER: 'mp-admin', MU_AGENTTEAMS_MATRIX_PASSWORD: 'mt-pass' };
ok('M1c 合法配置', mt.resolveMatrixConfig(MT_ENV).kind === 'matrix');

// ── 信封结构 ──
const { body, marker } = mt.buildTaskEnvelope({ workerMatrixId: '@mergepilot-fixer:dom', taskId: 't-fix',
  correlationId: 'run-1', submissionId: 'run-1:t-fix:1', role: 'fixer', brief: 'BRIEF' });
ok('M2a 信封含 @mention+marker+DATA 内联+输出契约（无元数据括号）',
  body.startsWith('@mergepilot-fixer:dom [mp:run-1:t-fix:1]') && body.includes('DATA (do not copy): BRIEF')
  && body.includes('ONLY the contract keys') && !body.includes('(correlation') && !body.includes('mp_task'));
ok('M2b marker 可派生', marker === '[mp:run-1:t-fix:1]');
ok('M2c 信封不含凭据形状字段名', !/api[_-]?key|password/i.test(body));
ok('M2d 无可回显的外层 JSON 包装（实测防回显）', !body.includes('mp_task'));

// ── JSON 提取边界 ──
ok('M3a 提取末尾完整对象（跳过 code fence）', mt.extractReplyJson('```json\n{"findings":[{"severity":"P0"}]}\n```')?.findings?.[0]?.severity === 'P0');
ok('M3b 无 JSON → null', mt.extractReplyJson('no json here') === null);
ok('M3c 前导噪声中提取', mt.extractReplyJson('thoughts... [mp:x] {"verdict":"PASS"}')?.verdict === 'PASS');

// ── mock Matrix（可编程：记录 txnId/marker/提供回复） ──
const ROOMS = { reviewer: '!mergepilot-reviewer:dom', leader: '!mergepilot-leader:dom',
  fixer: '!mergepilot-fixer:dom', verifier: '!mergepilot-verifier:dom' };
const SENDERS = Object.fromEntries(Object.keys(ROOMS).map((r) => [r, `@mergepilot-${r}:dom`]));
const REPLY = {
  reviewer: { findings: [{ severity: 'P0', path: 'a.js', summary: 's' }] },
  leader: { recommendation: 'fix_required', confidence: 0.8 },
  fixer: { suggestion: 's', patch_hint: 'p' },
  verifier: { verdict: 'PASS', note: 'ok' },
};
function mkMatrixApi({ override = {}, swallowFirstN = 0, wrongSender = false, staleTs = false, loginFails = 0, expireTokenOnce = false } = {}) {
  const state = { txns: [], sent: [], logins: 0 };
  let tokenSeq = 0;
  const api = async (url, opts = {}) => {
    if (url.includes('/login')) {
      state.logins++;
      if (state.logins <= loginFails) return { status: 401, ok: false };
      return { status: 200, ok: true, json: async () => ({ access_token: 'tok' + (++tokenSeq) }) };
    }
    const authOk = /access_token=tok/.test(url);
    if (!authOk) return { status: 401, ok: false };
    if (url.includes('/send/m.room.message/')) {
      if (expireTokenOnce && tokenSeq === 1 && !api._expiredOnce) { api._expiredOnce = true; return { status: 401, ok: false }; }
      const txn = /m\.room\.message\/([^/?]+)/.exec(url)[1];
      const b = JSON.parse(opts.body ?? '{}').body ?? '';
      const m = /\[mp:([^\]]+)\]/.exec(b);
      state.txns.push(txn);
      state.sent.push({ marker: m?.[1] ?? null, taskId: m ? m[1].split(':')[1] : null });
      return { status: 200, ok: true, json: async () => ({ event_id: '$e' + state.sent.length }) };
    }
    if (url.includes('/messages?')) {
      const room = decodeURIComponent(/rooms\/([^/]+)\/messages/.exec(url)[1]);
      const role = Object.keys(ROOMS).find((r) => ROOMS[r] === room);
      if (!role) return { status: 200, ok: true, json: async () => ({ chunk: [] }) };
      const TASK_ROLE = { 't-review': 'reviewer', 't-leader': 'leader', 't-fix': 'fixer', 't-verify': 'verifier' };
      const chunk = [];
      state.sent.forEach((x, i) => {
        if (!x.marker || TASK_ROLE[x.taskId] !== role) return;
        if (i < swallowFirstN) return;
        chunk.push({ type: 'm.room.message', sender: wrongSender ? '@attacker:dom' : SENDERS[role],
          origin_server_ts: staleTs ? Date.now() - 100_000 : Date.now(),
          content: { body: `[mp:${x.marker}] ${JSON.stringify(override[role] ?? REPLY[role])}` } });
      });
      return { status: 200, ok: true, json: async () => ({ chunk }) };
    }
    return { status: 404, ok: false };
  };
  api.state = state;
  return api;
}
const cfg = mt.resolveMatrixConfig(MT_ENV);

// ── 传输层单测 ──
mt.__resetMatrixLoginForTests();
const good = mkMatrixApi();
const send1 = await mt.sendTaskDelegation(cfg, { room: ROOMS.fixer, workerMatrixId: SENDERS.fixer,
  taskId: 't-fix', correlationId: 'c1', submissionId: 'c1:t-fix:1', role: 'fixer', brief: 'b', fetchImpl: good });
const send2 = await mt.sendTaskDelegation(cfg, { room: ROOMS.fixer, workerMatrixId: SENDERS.fixer,
  taskId: 't-fix', correlationId: 'c1', submissionId: 'c1:t-fix:1', role: 'fixer', brief: 'b', fetchImpl: good });
ok('M4a 发送成功（event 非 null）', send1.ok === true && Boolean(send1.eventId));
ok('M4b 幂等：同 submissionId → 同 txnId（Matrix PUT txn 语义）', good.state.txns[0] === good.state.txns[1]);
const send3 = await mt.sendTaskDelegation(cfg, { room: ROOMS.fixer, workerMatrixId: SENDERS.fixer,
  taskId: 't-fix', correlationId: 'c1', submissionId: 'c1:t-fix:2', role: 'fixer', brief: 'b', fetchImpl: good });
ok('M4c 重试轮（新 submissionId）→ 新 txnId', send3.ok === true && good.state.txns[2] !== good.state.txns[0]);

const got = await mt.collectReply(cfg, { room: ROOMS.fixer, expectedSender: SENDERS.fixer,
  marker: '[mp:c1:t-fix:1]', sinceTs: send1.ts - 2_000, timeoutMs: 3_000, fetchImpl: good, sleepImpl: async () => {} });
ok('M5a 收集合法回复（marker+sender+room 绑定）', got.ok === true && got.json?.suggestion === 's', got);

const ws = mkMatrixApi({ wrongSender: true });
const gotWs = await mt.collectReply(cfg, { room: ROOMS.fixer, expectedSender: SENDERS.fixer,
  marker: '[mp:c1:t-fix:1]', sinceTs: send1.ts - 2_000, timeoutMs: 1_500, fetchImpl: ws, sleepImpl: async () => {} });
ok('M5b 错误 sender 消息被忽略（超时拒绝）', gotWs.ok === false && gotWs.reason === 'MT_REPLY_TIMEOUT');

const stale = mkMatrixApi({ staleTs: true });
const gotStale = await mt.collectReply(cfg, { room: ROOMS.fixer, expectedSender: SENDERS.fixer,
  marker: '[mp:c1:t-fix:1]', sinceTs: send1.ts - 2_000, timeoutMs: 1_500, fetchImpl: stale, sleepImpl: async () => {} });
ok('M5c 过期 ts（时间窗外）回复被忽略（重放拒绝语义）', gotStale.ok === false && gotStale.reason === 'MT_REPLY_TIMEOUT');

const otherRoom = mkMatrixApi();
const gotOther = await mt.collectReply(cfg, { room: '!unknown-room:dom', expectedSender: SENDERS.fixer,
  marker: '[mp:c1:t-fix:1]', sinceTs: send1.ts - 2_000, timeoutMs: 1_500, fetchImpl: otherRoom, sleepImpl: async () => {} });
ok('M5d 其他 room 的消息不可见（固定 room 绑定）', gotOther.ok === false);

mt.__resetMatrixLoginForTests();
const exp = mkMatrixApi({ expireTokenOnce: true });
const expSend = await mt.sendTaskDelegation(cfg, { room: ROOMS.fixer, workerMatrixId: SENDERS.fixer,
  taskId: 't-fix', correlationId: 'c2', submissionId: 'c2:t-fix:1', role: 'fixer', brief: 'b', fetchImpl: exp });
ok('M6a token 过期 → 自动重登一次并完成发送', expSend.ok === true && exp.state.logins >= 2, { logins: exp.state.logins });

mt.__resetMatrixLoginForTests();
const lf = mkMatrixApi({ loginFails: 2 });
const lfSend = await mt.sendTaskDelegation(cfg, { room: ROOMS.fixer, workerMatrixId: SENDERS.fixer,
  taskId: 't-fix', correlationId: 'c3', submissionId: 'c3:t-fix:1', role: 'fixer', brief: 'b', fetchImpl: lf });
ok('M6b 登录持续失败 → 稳定 reason（fail-closed）', lfSend.ok === false && lfSend.reason === 'MT_LOGIN_HTTP_401');

mt.__resetMatrixLoginForTests();
const noReply = mkMatrixApi({ swallowFirstN: 99 });
const t0 = Date.now();
const gotTimeout = await mt.collectReply(cfg, { room: ROOMS.fixer, expectedSender: SENDERS.fixer,
  marker: '[mp:none]', sinceTs: t0, timeoutMs: 1_200, fetchImpl: noReply, sleepImpl: async () => {} });
ok('M7 收集超时 → MT_REPLY_TIMEOUT（不伪造）', gotTimeout.ok === false && gotTimeout.reason === 'MT_REPLY_TIMEOUT');

// P1 修复验证：挂起的服务（fetch 永不 resolve）必须超时返回稳定 reason，而非产品挂死
const hungFetch = () => new Promise(() => {});
mt.__resetMatrixLoginForTests();
const hungLogin = await mt.matrixLogin(cfg, hungFetch);
ok('M7a 挂起 fetch（服务 SIGSTOP 类）→ 登录稳定超时 reason', hungLogin.ok === false && /^MT_LOGIN_/.test(hungLogin.reason), hungLogin);
// M7c：AbortController 真取消——signal 在超时后被触发（连接资源释放的凭据）
let abortedFlag = false;
const abortAwareFetch = (url, opts = {}) => new Promise((_, reject) => {
  opts?.signal?.addEventListener('abort', () => { abortedFlag = true; reject(new Error('aborted')); });
});
mt.__resetMatrixLoginForTests();
await mt.matrixLogin(cfg, abortAwareFetch).catch(() => {});
ok('M7c 超时触发 AbortController（真取消，非仅 race 返回）', abortedFlag === true);

// M7d：迟到响应不产生副作用（慢 resolve 的值被丢弃，调用以超时 reason 返回）
const LATE_TOKEN = 'L8EVIL'; // 7 chars — synthetic, sub-scan threshold
const LATE = { status: 200, ok: true, json: async () => ({ access_token: LATE_TOKEN }) };
mt.__resetMatrixLoginForTests();
const late = await mt.matrixLogin(cfg, () => new Promise((res) => setTimeout(() => res(LATE), 60_000)));
ok('M7d 迟到响应被丢弃（不写缓存/不返回迟到值）', late.ok === false && /^MT_LOGIN_/.test(late.reason)
  && mt.__cachedTokenForTests() !== LATE_TOKEN, late);

// M7e：发送路径挂起 → MT_SEND_* 稳定族（登录成功后请求悬挂）
mt.__resetMatrixLoginForTests();
const loginOkApi = mkMatrixApi();
const tSend = await mt.sendTaskDelegation(cfg, { room: ROOMS.fixer, workerMatrixId: SENDERS.fixer,
  taskId: 't-fix', correlationId: 'c9', submissionId: 'c9:t-fix:1', role: 'fixer', brief: 'b',
  fetchImpl: async (url, opts) => (url.includes('/login') ? loginOkApi(url, opts) : new Promise(() => {})) });
ok('M7e 发送路径挂起 → MT_SEND_*（TIMEOUT/UNREACHABLE 稳定族）', tSend.ok === false && /^MT_SEND_/.test(String(tSend.reason)), tSend);

const hungRead = await mt.collectReply(cfg, { room: ROOMS.fixer, expectedSender: SENDERS.fixer,
  marker: '[mp:hung]', sinceTs: t0, timeoutMs: 1_500, fetchImpl: hungFetch, sleepImpl: async () => {} });
ok('M7b 挂起读取 → 稳定超时 reason（不挂死）', hungRead.ok === false && /^MT_/.test(hungRead.reason), hungRead);

// ── 管线级（一次性 PG）：gate/重试/REWORK/死信/cancel/审计/泄漏 ──
const CTR = `w34-${crypto.randomBytes(3).toString('hex')}`;
const PORT = 17400 + Math.floor(Math.random() * 60);
execFileSync('docker', ['run', '-d', '--name', CTR, '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
const pool = new Pool({ connectionString: `postgres://postgres:x@127.0.0.1:${PORT}/mu` });
for (let i = 0; i < 150; i++) { try { await pool.query('SELECT 1'); break; } catch { await sleep(400); } }
// v16 审批门适配：真实路径放行（ensure+逐票 approve→run 至 FIX_QUEUED）
const faMod = await import('../lib/multiuser/fix-approval.mjs');
async function approveHighRisk(run, binding) {
  await faMod.ensureFixApprovals(pool, { run, binding });
  const ts = (await pool.query(
    `SELECT approval_id FROM mu.fix_approval WHERE run_id=$1 AND status='PENDING'`, [run.run_id])).rows;
  for (const t of ts) {
    await faMod.decideFixApproval(pool, { approvalId: t.approval_id, decision: 'approve',
      decidedBy: 'test:maintainer', tenantId: binding.tenantId });
  }
}

try {
  const store = await createMuStore({ pool }); await store.initSchema(); await store.bootstrap();
  const T = (await pool.query(`SELECT tenant_id FROM mu.tenant LIMIT 1`)).rows[0].tenant_id;
  const repo = await store.ensureRepository({ tenantId: T, provider: 'github', providerRepoId: '99801', owner: 'w34', name: 'r', defaultBranch: 'main' });
  // controller mock（workers 带绑定源）
  const atApi = async (url, opts = {}) => {
    const auth = String(opts.headers?.authorization ?? '');
    if (!auth.startsWith('Bearer ')) return { status: 401 };
    if (url.endsWith('/api/v1/projects?limit=1')) return { status: 200, ok: true, json: async () => ({ projects: [] }) };
    if (url.endsWith('/api/v1/workers') && (!opts.method || opts.method === 'GET')) {
      return { status: 200, ok: true, json: async () => ({ workers: ['leader', 'reviewer', 'fixer', 'verifier'].map((r) => ({
        name: 'mergepilot-' + r, roomID: ROOMS[r], matrixUserID: SENDERS[r], phase: 'Running' })) }) };
    }
    if (url.endsWith('/api/v1/workers') && opts.method === 'POST') return { status: 201, ok: true, json: async () => ({ name: 'x' }) };
    if (/\/workers\/[\w-]+$/.test(url) && opts.method === 'PUT') return { status: 200, ok: true };
    if (url.endsWith('/api/v1/projects') && opts.method === 'POST') return { status: 201, ok: true, json: async () => ({ project_id: 'mp-x' }) };
    if (/\/replan$/.test(url)) return { status: 200, ok: true };
    if (/\/cancel$/.test(url)) return { status: 200, ok: true };
    return { status: 404, ok: false };
  };
  const AT_ENV = { MU_EXECUTOR: 'agentteams', MU_AGENTTEAMS_BASE_URL: 'http://at.test', MU_AGENTTEAMS_TOKEN: 'at-tok', ...MT_ENV };
  const mkScenario = async (prNo, shaC) => {
    const pr = await store.upsertPullRequest({ tenantId: T, repoId: repo.repo_id, providerPrNumber: prNo, headSha: shaC.repeat(40) });
    const binding = { tenantId: T, repoId: repo.repo_id, prId: pr.pr_id, headSha: shaC.repeat(40) };
    const { run } = await orch.createRunIfAbsent(pool, { ...binding });
    for (const [f, t] of [['RECEIVED', 'REVIEW_QUEUED'], ['REVIEW_QUEUED', 'REVIEWING'], ['REVIEWING', 'REVIEWED']]) {
      await orch.transitionRun(pool, { runId: run.run_id, from: [f], to: t });
    }
    const att = await orch.claimNextAttempt(pool, { runId: run.run_id, agentRole: 'reviewer', provider: 'deterministic', maxAttempts: 3, ...binding });
    await orch.insertFindings(pool, { attemptId: att.attemptId, runId: run.run_id, ...binding,
      findings: [{ rule_id: 'R-SECRET', severity: 'P0', confidence: 0.9, path: 's.js', line_start: 2,
        line_end: 2, title: 'x', evidence_ref: 'e', summary_masked: 'ghp_***' }] });
    await approveHighRisk(run, binding);
    return { run, binding };
  };
  const depsBase = (mtFetch) => ({ env: { ...AT_ENV }, atFetch: atApi, mtFetch,
    mtSleep: async () => {}, mtRoleTimeoutMs: 2_000, assertServiceChain: async () => true,
    repoUrl: 'x', testCmd: 'node -e process.exit(0)', providerCfg: {}, installationId: '1', owner: 'w34', repoName: 'r', prNumber: 1 });

  // P1 Matrix 缺配置 → executor_gate_rejected（fail-closed，不回退）
  mt.__resetMatrixLoginForTests();
  const s1 = await mkScenario(31, 'a');
  const r1 = await fxo.fixVerifyRound(pool, { run: s1.run, binding: s1.binding, deps: { ...depsBase(mkMatrixApi()), env: { MU_EXECUTOR: 'agentteams', MU_AGENTTEAMS_BASE_URL: 'http://at.test', MU_AGENTTEAMS_TOKEN: 'at-tok' } } });
  const run1 = await orch.getRun(pool, s1.run.run_id);
  const fix1c = (await pool.query(`SELECT count(*) c FROM mu.fix_attempt WHERE run_id=$1`, [s1.run.run_id])).rows[0];
  const gate1 = (await pool.query(`SELECT count(*) c FROM mu.audit_event WHERE kind='executor_gate_rejected' AND detail::text LIKE $1`, [`%${s1.run.run_id}%`])).rows[0];
  ok('P1a Matrix 未配置 → gate 拒绝（MT_NOT_CONFIGURED）', r1.ok === false && r1.stage === 'executor_gate_rejected' && r1.reason === 'MT_NOT_CONFIGURED', r1);
  ok('P1b run 停留 FIX_QUEUED+零 fix_attempt+审计', run1.status === 'FIX_QUEUED' && Number(fix1c.c) === 0 && Number(gate1.c) >= 1);

  // P2 正常链：四角色 Matrix 轮次 → COMPLETED（MergePilot 终裁）
  mt.__resetMatrixLoginForTests();
  const s2 = await mkScenario(32, 'b');
  const r2 = await fxo.fixVerifyRound(pool, { run: s2.run, binding: s2.binding, deps: depsBase(mkMatrixApi()) });
  const run2 = await orch.getRun(pool, s2.run.run_id);
  ok('P2a 四角色 Matrix 轮次完成（executor=agentteams）', r2.ok === true && r2.executor === 'agentteams' && r2.verdict === 'PASS', r2);
  ok('P2b 终裁 COMPLETED', run2.status === 'COMPLETED', run2.status);
  const atts2 = (await pool.query(`SELECT agent_role, provider, status, output_digest FROM mu.agent_attempt WHERE run_id=$1`, [s2.run.run_id])).rows;
  ok('P2c fixer/verifier attempt provider=agentteams', atts2.some((a) => a.agent_role === 'fixer' && a.provider === 'agentteams') && atts2.some((a) => a.agent_role === 'verifier' && a.provider === 'agentteams'));
  ok('P2c2 reviewer/leader attempt 行 DONE+digest（四 Agent 全可见）',
    atts2.some((a) => a.agent_role === 'reviewer' && a.status === 'DONE' && a.output_digest)
    && atts2.some((a) => a.agent_role === 'leader' && a.status === 'DONE' && a.output_digest), atts2);
  const fix2 = (await pool.query(`SELECT status FROM mu.fix_attempt WHERE run_id=$1`, [s2.run.run_id])).rows[0];
  ok('P2d 外部 Fixer 仅 DRY_RUN', fix2?.status === 'DRY_RUN');
  const audit2 = (await pool.query(`SELECT detail FROM mu.audit_event WHERE kind='agentteams_round_completed' AND detail::text LIKE $1`, [`%${s2.run.run_id}%`])).rows[0];
  const det2 = typeof audit2?.detail === 'string' ? JSON.parse(audit2.detail) : audit2?.detail;
  ok('P2e 轮次完成审计摘要（无正文）', det2?.verdict === 'PASS' && !JSON.stringify(det2).includes('mp_task'), det2);

  // P3 首条丢失 → 重试（新 submissionId）成功
  mt.__resetMatrixLoginForTests();
  const s3 = await mkScenario(33, 'c');
  const r3 = await fxo.fixVerifyRound(pool, { run: s3.run, binding: s3.binding, deps: depsBase(mkMatrixApi({ swallowFirstN: 1 })) });
  ok('P3 首条回复丢失 → 第二轮（attemptNo=2）成功', r3.ok === true, r3);

  // P4 verifier FAIL → REWORK（回派语义保持）
  mt.__resetMatrixLoginForTests();
  const s4 = await mkScenario(34, 'd');
  const r4 = await fxo.fixVerifyRound(pool, { run: s4.run, binding: s4.binding, deps: depsBase(mkMatrixApi({ override: { verifier: { verdict: 'FAIL', note: 'not resolved' } } })) });
  const run4 = await orch.getRun(pool, s4.run.run_id);
  ok('P4 verifier FAIL → REWORK_REQUIRED（独立判定语义）', r4.verdict === 'FAIL' && run4.status === 'REWORK_REQUIRED', { r: r4.verdict, s: run4.status });

  // P5 两轮 schema 无效 → 死信+BLOCKED+cancel 被真实调用
  mt.__resetMatrixLoginForTests();
  const s5 = await mkScenario(35, 'e');
  const badApi = mkMatrixApi({ override: { fixer: { evil: 'shape' } } });
  const r5 = await fxo.fixVerifyRound(pool, { run: s5.run, binding: s5.binding, deps: depsBase(badApi) });
  const run5 = await orch.getRun(pool, s5.run.run_id);
  const dl5 = (await pool.query(`SELECT kind, agent_role FROM mu.dead_letter WHERE run_id=$1`, [s5.run.run_id])).rows[0];
  ok('P5a schema 两轮无效 → mt_round_failed 死信+BLOCKED（不伪造/不回退）', r5.ok === false && r5.stage === 'mt_round_failed'
    && run5.status === 'BLOCKED' && dl5?.kind === 'mt_round_failed', { r: r5.reason, dl: dl5 });
  ok('P5b 失败角色=fixer', r5.role === 'fixer' && dl5?.agent_role === 'fixer');
  const orphan5 = (await pool.query(
    `SELECT count(*)::int c FROM mu.agent_attempt WHERE run_id=$1 AND status='RUNNING' AND provider='agentteams'`, [s5.run.run_id])).rows[0].c;
  const rlFailed5 = (await pool.query(
    `SELECT count(*)::int c FROM mu.agent_attempt WHERE run_id=$1 AND agent_role IN ('reviewer','leader')
       AND provider='agentteams' AND status='FAILED'`, [s5.run.run_id])).rows[0].c;
  ok('P5b2 后续角色失败时预领取角色一并 FAILED（零 RUNNING 残留）',
    orphan5 === 0 && rlFailed5 === 2, { orphan5, rlFailed5 });

  // P6 错误 sender（伪造）→ 两轮超时 → 死信（拒绝伪造回复）
  mt.__resetMatrixLoginForTests();
  const s6 = await mkScenario(36, 'f');
  const r6 = await fxo.fixVerifyRound(pool, { run: s6.run, binding: s6.binding, deps: depsBase(mkMatrixApi({ wrongSender: true })) });
  ok('P6 伪造 sender 回复被拒 → MT_REPLY_TIMEOUT 死信', r6.ok === false && r6.reason === 'MT_REPLY_TIMEOUT', r6);

  // P7 全链零泄漏（token/密码/marker 正文不入库）
  let leaks = [];
  for (const tb of ['agent_attempt', 'fix_attempt', 'verification_attempt', 'audit_event', 'review_run', 'dead_letter']) {
    const cols = (await pool.query(`SELECT column_name FROM information_schema.columns WHERE table_schema='mu' AND table_name=$1 AND data_type IN ('text','jsonb')`, [tb])).rows.map((r) => r.column_name);
    for (const c of cols) {
      for (const m of ['mt-pass', 'at-tok', 'mp_task', 'ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ']) {
        const hit = await pool.query(`SELECT 1 FROM mu.${tb} WHERE CAST(${c} AS text) LIKE $1 LIMIT 1`, [`%${m}%`]);
        if (hit.rows.length) leaks.push(`${tb}.${c}:${m.slice(0, 6)}`);
      }
    }
  }
  ok('P7 凭据/信封/原始 secret 全库零泄漏', leaks.length === 0, leaks.slice(0, 3));
} catch (e) { fail++; console.error('HARNESS ERROR', e); }
finally {
  await pool.end().catch(() => {});
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ }
  console.log(`\n${pass} pass, ${fail} fail`);
  process.exit(fail ? 1 : 0);
}
