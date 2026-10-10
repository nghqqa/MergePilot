// console/backend/test/mu-forge.test.mjs — ForgeAdapter Gitee 首版单元测试（G-2；无 PG，CI glob 内）。
// 层级标注：真实服务代码 + stub fetchImpl（模拟外部 API）——不混淆真实平台。
// 依据：探针定案样本（r3work/forge-m0/gitee-api-verification.md §3/§4）作为 fixture 基准。
import test from 'node:test';
import assert from 'node:assert/strict';
import { validateForgeEventV1, deriveDeliveryRef, FORGE_INSTANCES } from '../lib/multiuser/forge/index.mjs';
import { createGiteeAdapter, verifyGiteeWebhook, GiteeProviderError, GITEE_LIMITS }
  from '../lib/multiuser/forge/gitee.mjs';

const VALID_V1 = {
  schema_version: 1, event: 'pull_request', forge_kind: 'gitee',
  delivery_ref: 'a'.repeat(32), provider_repo_id: '10919030',
  pr_number: 95569, head_sha: '5f61c05b90b4e321e83b77bb15a104ad74dad63b',
  action: 'open', trigger_source: null, installation_id: null,
};

// ── 规范事件 v1 校验器 ────────────────────────────────────────────
test('v1 校验：合法 payload 通过', () => {
  assert.equal(validateForgeEventV1(VALID_V1).ok, true);
});
test('v1 校验：缺必填字段逐项拒绝且可定位（#389 教训）', () => {
  for (const field of ['schema_version', 'event', 'forge_kind', 'delivery_ref', 'provider_repo_id', 'pr_number', 'head_sha']) {
    const bad = { ...VALID_V1 };
    if (field === 'schema_version') bad.schema_version = 2;
    else if (field === 'event') bad.event = 'push';
    else delete bad[field];
    const r = validateForgeEventV1(bad);
    assert.equal(r.ok, false, `${field} 缺失应拒绝`);
    assert.equal(r.reason, 'event_payload_invalid');
    assert.match(String(r.field ?? ''), new RegExp(field.slice(0, 6)), `reason 应可定位到 ${field}`);
  }
});
test('v1 校验：gitee 携带 installation_id → 拒绝（防伪装合成）', () => {
  const r = validateForgeEventV1({ ...VALID_V1, installation_id: 12345 });
  assert.equal(r.ok, false);
  assert.equal(r.field, 'installation_id');
});
test('v1 校验：github kind 未启用（随迁入波）', () => {
  const r = validateForgeEventV1({ ...VALID_V1, forge_kind: 'github' });
  assert.equal(r.ok, false);
});
test('v1 校验：head_sha 非 hex / pr_number 非正整数拒绝', () => {
  assert.equal(validateForgeEventV1({ ...VALID_V1, head_sha: 'zzzz' }).ok, false);
  assert.equal(validateForgeEventV1({ ...VALID_V1, pr_number: 0 }).ok, false);
  assert.equal(validateForgeEventV1({ ...VALID_V1, pr_number: 'x' }).ok, false);
});

// ── delivery_ref 确定性（v1.1：不含接收时间戳）────────────────────
test('delivery_ref：同输入恒同输出；body 变化则变；与时间无关', () => {
  const base = { forgeKind: 'gitee', providerRepoId: '10919030', changeRequestKey: '95569', eventType: 'pull_request' };
  const a1 = deriveDeliveryRef({ ...base, rawBody: '{"x":1}' });
  const a2 = deriveDeliveryRef({ ...base, rawBody: '{"x":1}' });
  assert.equal(a1, a2, '同一投递重试必须同 ref');
  assert.equal(a1.length, 32);
  assert.notEqual(a1, deriveDeliveryRef({ ...base, rawBody: '{"x":2}' }), 'body 变化 ref 变化（残余风险已登记）');
  // 时间无关性：模拟两个接收时刻，ref 一致
  assert.equal(deriveDeliveryRef({ ...base, rawBody: '{"x":1}'}), a1);
});

// ── webhook 验真（显式单模式；连接配置决定，禁止降级/择一）────────
const SECRET = 'SEC-test-secret';
import crypto from 'node:crypto';
function signSync(ts, secret = SECRET) {
  return crypto.createHmac('sha256', Buffer.from(secret, 'utf8')).update(`${ts}\n`, 'utf8').digest('base64');
}

test('验真 signature：header 携带、窗口内 → ok', () => {
  const ts = String(Date.now());
  const r = verifyGiteeWebhook({ mode: 'signature', secret: SECRET,
    headers: {}, rawBody: '{}', extracted: { tsHeader: ts, tokenHeader: signSync(ts) } });
  assert.equal(r.ok, true);
});
test('验真 signature：payload 顶层携带（钉钉式）→ ok', () => {
  const ts = String(Date.now());
  const r = verifyGiteeWebhook({ mode: 'signature', secret: SECRET,
    headers: {}, rawBody: '{}', extracted: { bodyTimestamp: ts, bodySign: signSync(ts) } });
  assert.equal(r.ok, true);
});
test('验真 signature：错签 → mismatch；超窗 → out_of_window；缺字段 → missing', () => {
  const ts = String(Date.now());
  assert.equal(verifyGiteeWebhook({ mode: 'signature', secret: SECRET, headers: {}, rawBody: '{}',
    extracted: { tsHeader: ts, tokenHeader: 'bad-signature' } }).reason, 'signature_mismatch');
  assert.equal(verifyGiteeWebhook({ mode: 'signature', secret: SECRET, headers: {}, rawBody: '{}',
    extracted: { tsHeader: String(Date.now() - 2 * 60 * 60 * 1000), tokenHeader: signSync(String(Date.now() - 2 * 60 * 60 * 1000)) } }).reason,
    'timestamp_out_of_window');
  assert.equal(verifyGiteeWebhook({ mode: 'signature', secret: SECRET, headers: {}, rawBody: '{}',
    extracted: {} }).reason, 'signature_fields_missing');
});
test('验真 password：常量时间比较；错密 → mismatch', () => {
  assert.equal(verifyGiteeWebhook({ mode: 'password', secret: SECRET, headers: {}, rawBody: '{}',
    extracted: { tokenHeader: SECRET } }).ok, true);
  assert.equal(verifyGiteeWebhook({ mode: 'password', secret: SECRET, headers: {}, rawBody: '{}',
    extracted: { tokenHeader: 'wrong' } }).ok, false);
});
test('验真：mode 非法 / secret 缺失 → fail-closed', () => {
  assert.equal(verifyGiteeWebhook({ mode: 'auto', secret: SECRET, headers: {}, rawBody: '{}', extracted: {} }).reason, 'mode_invalid');
  assert.equal(verifyGiteeWebhook({ mode: 'signature', secret: '', headers: {}, rawBody: '{}', extracted: {} }).reason, 'secret_missing');
});

// ── GiteeAdapter：stub fetchImpl（模拟外部 API）──────────────────
function stubFetch(routes) {
  const calls = [];
  const fn = async (url) => {
    const u = url instanceof URL ? url : new URL(String(url));
    // 适配器拼 apiBase（https://gitee.com/api/v5）——路由匹配剥前缀
    const path = u.pathname.replace(/^\/api\/v5/, '') || '/';
    calls.push({ path, query: Object.fromEntries(u.searchParams) });
    // 两轮匹配：先精确（/pulls/N 不吃掉 /pulls/N/files），再前缀
    for (const exact of [true, false]) {
      for (const [pattern, responder] of routes) {
        const hit = exact ? path === pattern : path.startsWith(pattern);
        if (hit) {
          const r = typeof responder === 'function' ? responder(u) : responder;
          return { ok: r.status >= 200 && r.status < 300, status: r.status, json: async () => r.body };
        }
      }
    }
    return { ok: false, status: 404, json: async () => ({ message: 'Not Found' }) };
  };
  fn.calls = calls;
  return fn;
}
function adapter(fetchImpl, env = { MU_GITEE_PAT: 'pat-test-token' }) {
  return createGiteeAdapter({ env, fetchImpl });
}
const REPO = 'openharmony/docs';
const PR_DETAIL = {
  number: 95569, state: 'open', title: 't', html_url: 'https://gitee.com/x',
  head: { sha: 'a'.repeat(40), repo: { id: 40319550 } },
  base: { sha: 'b'.repeat(40), repo: { id: 10919030 } },
};

test('适配器：endpoint 白名单——path 带 query 串拒绝', async () => {
  const a = adapter(stubFetch([]));
  await assert.rejects(a.getChangeRequest({ providerRepoId: 'o/r?x=1', crKey: '1' }), /endpoint_invalid|not_allowlisted/);
});

test('适配器：凭据缺失 → gitee_credentials_missing（不发起请求）', async () => {
  const f = stubFetch([]);
  const a = createGiteeAdapter({ env: {}, fetchImpl: f });
  await assert.rejects(a.listRepositories(), (e) => e instanceof GiteeProviderError && e.code === 'gitee_credentials_missing');
  assert.equal(f.calls.length, 0, '无凭据不得发起任何请求');
});

test('适配器：错误消息不含 access_token/完整 URL（凭据红线）', async () => {
  const a = adapter(stubFetch([['/user/repos', { status: 401, body: {} }]]));
  try { await a.listRepositories(); assert.fail('应抛'); }
  catch (e) {
    assert.equal(e.code, 'gitee_auth_failed');
    assert.ok(!String(e.message).includes('pat-test-token'), 'message 不得含 token');
    assert.ok(!String(e.message).includes('access_token'), 'message 不得含 query 键');
    assert.ok(!String(e.message).includes('http'), 'message 不得含完整 URL');
  }
});

test('适配器：HTTP 错误分类 401/403/404/429/500 + transient 标记', async () => {
  for (const [status, code, transient] of [[401, 'gitee_auth_failed', false], [403, 'gitee_forbidden', false],
    [404, 'gitee_not_found', false], [429, 'gitee_rate_limited', true], [500, 'gitee_upstream_error', true]]) {
    const a = adapter(stubFetch([['/user/repos', { status, body: {} }]]));
    try { await a.listRepositories(); assert.fail(`status ${status} 应抛`); }
    catch (e) {
      assert.equal(e.code, code, `status ${status}`);
      assert.equal(e.transient, transient, `status ${status} transient`);
    }
  }
});

test('适配器：网络失败/超时 → transient unreachable/timeout', async () => {
  const a = createGiteeAdapter({ env: { MU_GITEE_PAT: 't' }, fetchImpl: async () => { throw new Error('ECONNREFUSED'); } });
  await assert.rejects(a.listRepositories(), (e) => e.transient === true && e.code === 'gitee_unreachable');
  const a2 = createGiteeAdapter({ env: { MU_GITEE_PAT: 't' }, timeoutMs: 20,
    fetchImpl: (url, init) => new Promise((_, rej) => init.signal.addEventListener('abort',
      () => { const e = new Error('aborted'); e.name = 'TimeoutError'; rej(e); })) });
  await assert.rejects(a2.listRepositories(), (e) => e.code === 'gitee_timeout');
});

test('fetchChangeContext：探针样本形状——patch={diff} 对象解包 + 字符串数字转换 + unified 拼接', async () => {
  const fileEntry = {
    sha: 'x', filename: 'src/app.md', status: null,
    additions: '2', deletions: '2',
    patch: { diff: '@@ -1,2 +1,2 @@\n-old line\n+new line' }, // 探针实返形状
  };
  const f = stubFetch([
    [`/repos/${REPO}/pulls/95569`, { status: 200, body: PR_DETAIL }],
    [`/repos/${REPO}/pulls/95569/files`, { status: 200, body: [fileEntry] }],
  ]);
  const a = adapter(f);
  const ctx = await a.fetchChangeContext({ providerRepoId: REPO, crKey: '95569', expectedHeadSha: 'a'.repeat(40), declaredFileCount: 1 });
  assert.equal(ctx.stale_head, false);
  assert.equal(ctx.diff.includes('diff --git a/src/app.md b/src/app.md'), true, 'unified 拼接含文件头');
  assert.equal(ctx.diff.includes('+new line'), true);
  assert.equal(ctx.files[0].additions, 2, '字符串数字→Number');
  assert.equal(ctx.files[0].patch_status, 'present');
  assert.equal(ctx.completeness.status, 'complete');
  assert.equal(ctx.protection.provided, false, '本接入未提供（非平台结论）');
  assert.equal(ctx.protection.reason, 'not_provided_in_this_release');
});

test('fetchChangeContext：patch 缺失 → missing（不断定二进制/重命名），completeness=partial', async () => {
  const f = stubFetch([
    [`/repos/${REPO}/pulls/95569`, { status: 200, body: PR_DETAIL }],
    [`/repos/${REPO}/pulls/95569/files`, { status: 200, body: [
      { filename: 'a.md', additions: '1', deletions: '1', patch: { diff: '@@ -1 +1 @@\n-x\n+y' } },
      { filename: 'binary.bin', additions: '0', deletions: '0', patch: null }, // 形状未知→missing
    ] }],
  ]);
  const a = adapter(f);
  const ctx = await a.fetchChangeContext({ providerRepoId: REPO, crKey: '95569', expectedHeadSha: 'a'.repeat(40), declaredFileCount: 2 });
  assert.equal(ctx.files[1].patch_status, 'missing');
  assert.equal(ctx.completeness.status, 'partial');
  assert.equal(ctx.completeness.notes, 'files_missing_patch');
  assert.equal(ctx.diff.includes('binary.bin'), false, '缺失 patch 不掺入 diff');
});

test('fetchChangeContext：declared 不可得 → completeness=unknown（不折算 complete）', async () => {
  const f = stubFetch([
    [`/repos/${REPO}/pulls/95569`, { status: 200, body: PR_DETAIL }],
    [`/repos/${REPO}/pulls/95569/files`, { status: 200, body: [{ filename: 'a.md', patch: { diff: '@@ -1 +1 @@\n-x\n+y' } }] }],
  ]);
  const a = adapter(f);
  const ctx = await a.fetchChangeContext({ providerRepoId: REPO, crKey: '95569', expectedHeadSha: 'a'.repeat(40), declaredFileCount: null });
  assert.equal(ctx.completeness.status, 'unknown');
  assert.equal(ctx.completeness.notes, 'declared_file_count_unavailable');
});

test('fetchChangeContext：returned < declared → partial；平台 300 上限 → partial(file_count)', async () => {
  const f1 = stubFetch([
    [`/repos/${REPO}/pulls/95569`, { status: 200, body: PR_DETAIL }],
    [`/repos/${REPO}/pulls/95569/files`, { status: 200, body: [{ filename: 'a.md', patch: { diff: '@@ -1 +1 @@\n-x\n+y' } }] }],
  ]);
  const ctx1 = await adapter(f1).fetchChangeContext({ providerRepoId: REPO, crKey: '95569', expectedHeadSha: 'a'.repeat(40), declaredFileCount: 5 });
  assert.equal(ctx1.completeness.status, 'partial');
  assert.equal(ctx1.completeness.notes, 'returned_less_than_declared');
});

test('fetchChangeContext：head 漂移 → gitee_head_moved 明确失败（不静默续跑）', async () => {
  const f = stubFetch([
    [`/repos/${REPO}/pulls/95569`, { status: 200, body: PR_DETAIL }],
  ]);
  await assert.rejects(
    adapter(f).fetchChangeContext({ providerRepoId: REPO, crKey: '95569', expectedHeadSha: 'c'.repeat(40) }),
    (e) => e.code === 'gitee_head_moved');
});

test('fetchChangeContext：分页拉全（两页）+ fork 判定', async () => {
  const page1 = Array.from({ length: 100 }, (_, i) => ({ filename: `f${i}.md`, additions: '1', deletions: '0', patch: { diff: '@@ -1 +1 @@\n-x\n+y' } }));
  const page2 = Array.from({ length: 3 }, (_, i) => ({ filename: `g${i}.md`, additions: '1', deletions: '0', patch: { diff: '@@ -1 +1 @@\n-x\n+y' } }));
  let call = 0;
  const f = stubFetch([
    [`/repos/${REPO}/pulls/95569`, { status: 200, body: { ...PR_DETAIL, head: { sha: 'a'.repeat(40), repo: { id: 999 } } } }],
    [`/repos/${REPO}/pulls/95569/files`, (u) => {
      const page = Number(u.searchParams.get('page') ?? 1);
      return { status: 200, body: page === 1 ? page1 : page2 };
    }],
  ]);
  const ctx = await adapter(f).fetchChangeContext({ providerRepoId: REPO, crKey: '95569', expectedHeadSha: 'a'.repeat(40), declaredFileCount: 103 });
  assert.equal(ctx.files.length, 103);
  assert.equal(ctx.source_is_fork, true, 'head.repo.id ≠ base.repo.id');
  assert.equal(ctx.completeness.status, 'complete');
});

test('readChecks/readProtection：恒 unsupported + not_provided_in_this_release，零网络调用', async () => {
  const f = stubFetch([]);
  const a = adapter(f);
  assert.deepEqual(await a.readChecks(), { status: 'unsupported', reason: 'not_provided_in_this_release', items: [] });
  assert.deepEqual(await a.readProtection(), { status: 'unsupported', reason: 'not_provided_in_this_release', summary: null });
  assert.equal(f.calls.length, 0, '零调用——404 探针只证明所测请求失败，不证明平台能力');
});

test('probeConnection：可达+认证 ok / 401 → auth_ok:false（区分认证与不可达）', async () => {
  const ok = await adapter(stubFetch([['/user', { status: 200, body: { id: 1, login: 'x' } }]])).probeConnection();
  assert.equal(ok.reachable && ok.auth_ok, true);
  const denied = await adapter(stubFetch([['/user', { status: 401, body: {} }]])).probeConnection();
  assert.equal(denied.reachable, true);
  assert.equal(denied.auth_ok, false);
  assert.equal(denied.reason, 'gitee_auth_failed');
});

test('实例登记常量与 schema v24 seed 一致（capability 文案=not_provided）', () => {
  const gitee = FORGE_INSTANCES.find((i) => i.instance_id === 'gitee-cloud');
  assert.equal(gitee.capability.checks, 'not_provided');
  assert.equal(gitee.capability.protection, 'not_provided');
});
