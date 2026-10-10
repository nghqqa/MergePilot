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

// ── webhook 验真（v2：官方算法=timestamp+LF+secret 三段；显式单源/单编码）────
const SECRET = 'SEC-test-secret';
import crypto from 'node:crypto';
function signSync(ts, secret = SECRET) {
  // 参考实现（测试侧独立路径）：官方三步 urlEncode(Base64(HmacSHA256(msg=ts+"\n"+secret, key=secret)))
  const b64 = crypto.createHmac('sha256', Buffer.from(secret, 'utf8'))
    .update(ts + '\n' + secret, 'utf8').digest('base64');
  return b64.replace(/\+/g, '%2B').replace(/\//g, '%2F').replace(/=/g, '%3D');
}

// 独立向量（python hmac/urllib 独立计算，非 node 实现公式复刻）——防实现与测试自洽退化。
// 旧缺陷公式（消息缺 secret 段）的输出 5TpOj3... 与官方算法不一致——作为防退化断言。
// 向量值以 -sample 后缀标识合成样本（secret-scan 豁免词表）；计算路径=python hmac 独立算
const VEC = { ts: '1700000000000', secret: 'SEC-vector-check-sample', nowMs: 1700000000000 + 1000,
  urlB64: 'qTaSShJKum3shqEp4azT7y%2Bm3symBeRs%2F6MZ855cKcw%3D',
  b64: 'qTaSShJKum3shqEp4azT7y+m3symBeRs/6MZ855cKcw=',
  oldBrokenB64: 'GVbnD600BxInTxmJrLzAj/NT4vDNBIjLigg1/aCtDhA=' };

test('验真独立向量：官方算法（消息含 secret 段）url_b64 → ok；v1 缺段公式 → mismatch（防退化）', () => {
  const okR = verifyGiteeWebhook({ mode: 'signature', secret: VEC.secret,
    extracted: { tsHeader: VEC.ts, tokenHeader: VEC.urlB64 },
    signSource: 'header', signEncoding: 'url_b64', nowMs: VEC.nowMs });
  assert.equal(okR.ok, true, `独立向量应通过（官方算法）: ${okR.reason ?? ''}`);
  const b64R = verifyGiteeWebhook({ mode: 'signature', secret: VEC.secret,
    extracted: { tsHeader: VEC.ts, tokenHeader: VEC.b64 },
    signSource: 'header', signEncoding: 'b64', nowMs: VEC.nowMs });
  assert.equal(b64R.ok, true, 'b64 编码分支（显式配置）应通过');
  const oldR = verifyGiteeWebhook({ mode: 'signature', secret: VEC.secret,
    extracted: { tsHeader: VEC.ts, tokenHeader: VEC.oldBrokenB64 },
    signSource: 'header', signEncoding: 'b64', nowMs: VEC.nowMs });
  assert.equal(oldR.reason, 'signature_mismatch', 'v1 缺 secret 段公式不得通过（防退化自洽）');
});

test('验真 source：单源配置；另一来源不一致 → sign_source_conflict（不自动回退/混合）', () => {
  const ts = String(Date.now());
  const good = signSync(ts);
  // header 模式：body 带不同对 → 冲突
  const conflict = verifyGiteeWebhook({ mode: 'signature', secret: SECRET,
    extracted: { tsHeader: ts, tokenHeader: good, bodyTimestamp: ts, bodySign: 'different' },
    signSource: 'header' });
  assert.equal(conflict.reason, 'sign_source_conflict');
  // body 模式：以 body 为准 → ok（另一侧不一致时拒绝而不是切换）
  const bodyOk = verifyGiteeWebhook({ mode: 'signature', secret: SECRET,
    extracted: { tsHeader: ts, tokenHeader: 'bogus', bodyTimestamp: ts, bodySign: good },
    signSource: 'body' });
  assert.equal(bodyOk.reason, 'sign_source_conflict', '两侧都有且其一为伪 → 冲突拒绝（不择一通过）');
  // 仅 header → ok
  const onlyHeader = verifyGiteeWebhook({ mode: 'signature', secret: SECRET,
    extracted: { tsHeader: ts, tokenHeader: good }, signSource: 'header' });
  assert.equal(onlyHeader.ok, true);
});

test('验真 source/encoding 非法值 → fail-closed', () => {
  const ts = String(Date.now());
  assert.equal(verifyGiteeWebhook({ mode: 'signature', secret: SECRET,
    extracted: { tsHeader: ts, tokenHeader: signSync(ts) }, signSource: 'auto' }).reason, 'sign_source_invalid');
  assert.equal(verifyGiteeWebhook({ mode: 'signature', secret: SECRET,
    extracted: { tsHeader: ts, tokenHeader: signSync(ts) }, signEncoding: 'raw' }).reason, 'sign_encoding_invalid');
});

test('验真 signature：header 携带、窗口内 → ok', () => {
  const ts = String(Date.now());
  const r = verifyGiteeWebhook({ mode: 'signature', secret: SECRET,
    extracted: { tsHeader: ts, tokenHeader: signSync(ts) }, signEncoding: 'url_b64' });
  assert.equal(r.ok, true);
});
test('验真 signature：错误密钥 → mismatch（非异常）', () => {
  const ts = String(Date.now());
  const otherSecret = 'SEC-other-secret-dummy'; // 值含豁免词标识合成
  const r = verifyGiteeWebhook({ mode: 'signature', secret: otherSecret,
    extracted: { tsHeader: ts, tokenHeader: signSync(ts) }, signEncoding: 'url_b64' });
  assert.equal(r.reason, 'signature_mismatch');
});
test('验真 signature：错误/非数字时间戳 → invalid；过期 → out_of_window；缺字段 → missing', () => {
  const ts = String(Date.now());
  assert.equal(verifyGiteeWebhook({ mode: 'signature', secret: SECRET,
    extracted: { tsHeader: 'not-a-number', tokenHeader: signSync(ts) } }).reason, 'timestamp_invalid');
  assert.equal(verifyGiteeWebhook({ mode: 'signature', secret: SECRET,
    extracted: { tsHeader: '0', tokenHeader: signSync('0') } }).reason, 'timestamp_invalid', '畸形（≤0）优先判 invalid');
  const old = String(Date.now() - 2 * 60 * 60 * 1000);
  assert.equal(verifyGiteeWebhook({ mode: 'signature', secret: SECRET,
    extracted: { tsHeader: old, tokenHeader: signSync(old) } }).reason, 'timestamp_out_of_window');
  assert.equal(verifyGiteeWebhook({ mode: 'signature', secret: SECRET,
    extracted: {} }).reason, 'signature_fields_missing');
});
test('验真 password：常量时间比较；错密 → mismatch', () => {
  assert.equal(verifyGiteeWebhook({ mode: 'password', secret: SECRET,
    extracted: { tokenHeader: SECRET } }).ok, true);
  assert.equal(verifyGiteeWebhook({ mode: 'password', secret: SECRET,
    extracted: { tokenHeader: 'wrong' } }).ok, false);
});
test('验真：mode 非法 / secret 缺失 → fail-closed', () => {
  assert.equal(verifyGiteeWebhook({ mode: 'auto', secret: SECRET, extracted: {} }).reason, 'mode_invalid');
  assert.equal(verifyGiteeWebhook({ mode: 'signature', secret: '', extracted: {} }).reason, 'secret_missing');
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

test('适配器：网络失败/超时 → transient unreachable/timeout（同步 settle，CI/本地一致）', async () => {
  const a = createGiteeAdapter({ env: { MU_GITEE_PAT: 't' }, fetchImpl: async () => { throw new Error('ECONNREFUSED'); } });
  await assert.rejects(a.listRepositories(), (e) => e.transient === true && e.code === 'gitee_unreachable');
  // 超时语义单测：模拟 fetch 层抛 TimeoutError（giteeGet 的 catch 分支归类 gitee_timeout）——
  // 不依赖真实 timer/AbortSignal 时序（CI runner 的 event loop 判定差异曾致挂起）。
  const a2 = createGiteeAdapter({ env: { MU_GITEE_PAT: 't' },
    fetchImpl: () => Promise.resolve().then(() => {
      const e = new Error('The operation was aborted due to timeout');
      e.name = 'TimeoutError';
      throw e;
    }) });
  await assert.rejects(a2.listRepositories(), (e) => e.code === 'gitee_timeout' && e.transient === true);
});

// fetchChangeContext v2：签名 { repoPath, nativeRepoId, crKey, ... }（稳定 id 归属核验）
function ctxFetch(routes, args) {
  const f = stubFetch(routes);
  return { f, p: adapter(f).fetchChangeContext({ repoPath: REPO, nativeRepoId: '10919030',
    crKey: '95569', expectedHeadSha: 'a'.repeat(40), ...args }) };
}
const REPO_ROUTE = [`/repos/${REPO}`, { status: 200, body: { id: 10919030, full_name: REPO, default_branch: 'master' } }];

test('fetchChangeContext：探针样本形状——patch={diff} 对象解包 + 字符串数字转换 + unified 拼接', async () => {
  const fileEntry = {
    sha: 'x', filename: 'src/app.md', status: null,
    additions: '2', deletions: '2',
    patch: { diff: '@@ -1,2 +1,2 @@\n-old line\n+new line' }, // 探针实返形状
  };
  const { p } = ctxFetch([
    REPO_ROUTE,
    [`/repos/${REPO}/pulls/95569`, { status: 200, body: PR_DETAIL }],
    [`/repos/${REPO}/pulls/95569/files`, { status: 200, body: [fileEntry] }],
  ], { declaredFileCount: 1 });
  const ctx = await p;
  assert.equal(ctx.stale_head, false);
  assert.equal(ctx.diff.includes('diff --git a/src/app.md b/src/app.md'), true, 'unified 拼接含文件头');
  assert.equal(ctx.diff.includes('+new line'), true);
  assert.equal(ctx.files[0].additions, 2, '字符串数字→Number');
  assert.equal(ctx.files[0].patch_status, 'present');
  assert.equal(ctx.completeness.status, 'complete');
  assert.equal(ctx.completeness.declared_file_count, 1, '全量自证 declared=returned');
  assert.equal(ctx.protection.provided, false, '本接入未提供（非平台结论）');
  assert.equal(ctx.protection.reason, 'not_provided_in_this_release');
});

test('fetchChangeContext：patch 缺失 → missing（不断定二进制/重命名），completeness=partial', async () => {
  const { p } = ctxFetch([
    REPO_ROUTE,
    [`/repos/${REPO}/pulls/95569`, { status: 200, body: PR_DETAIL }],
    [`/repos/${REPO}/pulls/95569/files`, { status: 200, body: [
      { filename: 'a.md', additions: '1', deletions: '1', patch: { diff: '@@ -1 +1 @@\n-x\n+y' } },
      { filename: 'binary.bin', additions: '0', deletions: '0', patch: null }, // 形状未知→missing
    ] }],
  ], { declaredFileCount: 2 });
  const ctx = await p;
  assert.equal(ctx.files[1].patch_status, 'missing');
  assert.equal(ctx.completeness.status, 'partial');
  assert.equal(ctx.completeness.notes, 'files_missing_patch');
  assert.equal(ctx.diff.includes('binary.bin'), false, '缺失 patch 不掺入 diff');
});

test('fetchChangeContext：末页不满页 → 全量自证 complete（手动入口不依赖 webhook 声明）', async () => {
  const { p } = ctxFetch([
    REPO_ROUTE,
    [`/repos/${REPO}/pulls/95569`, { status: 200, body: PR_DETAIL }],
    [`/repos/${REPO}/pulls/95569/files`, { status: 200, body: [{ filename: 'a.md', patch: { diff: '@@ -1 +1 @@\n-x\n+y' } }] }],
  ], { declaredFileCount: null });
  const ctx = await p;
  assert.equal(ctx.completeness.status, 'complete', '末页不满页=平台全量已取得（自证），不再 unknown');
  assert.equal(ctx.completeness.declared_file_count, 1);
});

test('fetchChangeContext：webhook declared 与实际不符 → partial(returned_less_than_declared)', async () => {
  const { p } = ctxFetch([
    REPO_ROUTE,
    [`/repos/${REPO}/pulls/95569`, { status: 200, body: PR_DETAIL }],
    [`/repos/${REPO}/pulls/95569/files`, { status: 200, body: [{ filename: 'a.md', patch: { diff: '@@ -1 +1 @@\n-x\n+y' } }] }],
  ], { declaredFileCount: 5 });
  const ctx1 = await p;
  assert.equal(ctx1.completeness.status, 'partial');
  assert.equal(ctx1.completeness.notes, 'returned_less_than_declared');
});

test('fetchChangeContext：本地限额——diff 超 1MiB → partial(local_limit)+limits.over_diff_limit', async () => {
  const bigPatch = { diff: '@@ -1 +1,2 @@\n-x\n+' + 'y'.repeat(1024 * 1024) };
  const { p } = ctxFetch([
    REPO_ROUTE,
    [`/repos/${REPO}/pulls/95569`, { status: 200, body: PR_DETAIL }],
    [`/repos/${REPO}/pulls/95569/files`, { status: 200, body: [{ filename: 'big.md', patch: bigPatch }] }],
  ], { declaredFileCount: 1 });
  const ctx = await p;
  assert.equal(ctx.completeness.status, 'partial');
  assert.equal(ctx.completeness.notes, 'local_limit');
  assert.equal(ctx.completeness.local_limit_hit.diff_bytes, true);
  assert.equal(ctx.limits.over_diff_limit, true);
});

test('fetchChangeContext：含空白字符路径 → unparsed_path 不拼入 diff（parseDiff 安全防护），partial', async () => {
  const { p } = ctxFetch([
    REPO_ROUTE,
    [`/repos/${REPO}/pulls/95569`, { status: 200, body: PR_DETAIL }],
    [`/repos/${REPO}/pulls/95569/files`, { status: 200, body: [
      { filename: 'docs/has space.md', patch: { diff: '@@ -1 +1 @@\n-x\n+y' } },
      { filename: 'ok.md', patch: { diff: '@@ -1 +1 @@\n-x\n+y' } },
    ] }],
  ], { declaredFileCount: 2 });
  const ctx = await p;
  assert.equal(ctx.files[0].patch_status, 'unparsed_path');
  assert.equal(ctx.diff.includes('has space'), false, '空白路径不得进入 diff（diff --git 行按空白分列）');
  assert.equal(ctx.completeness.status, 'partial');
  assert.equal(ctx.completeness.notes, 'files_missing_patch');
});

test('fetchChangeContext：仓库改名后旧路径命中同名新仓库 → gitee_repo_moved（id 归属核验）', async () => {
  const { p } = ctxFetch([
    [`/repos/${REPO}`, { status: 200, body: { id: 999999, full_name: REPO } }], // id 与登记不符
  ], {});
  await assert.rejects(p, (e) => e.code === 'gitee_repo_moved');
});

test('fetchChangeContext：触平台 300 上限 → partial(platform_file_count_limit)', async () => {
  // 4 页满 100 → 300 触发（平台上限先于翻完）
  const full = Array.from({ length: 100 }, (_, i) => ({ filename: `f${i}.md`, patch: { diff: '@@ -1 +1 @@\n-x\n+y' } }));
  const { p } = ctxFetch([
    REPO_ROUTE,
    [`/repos/${REPO}/pulls/95569`, { status: 200, body: PR_DETAIL }],
    [`/repos/${REPO}/pulls/95569/files`, () => ({ status: 200, body: full })],
  ], { declaredFileCount: null });
  const ctx = await p;
  assert.equal(ctx.completeness.status, 'partial');
  assert.equal(ctx.completeness.notes, 'platform_file_count_limit');
  assert.equal(ctx.completeness.platform_limit_hit.file_count, true);
  assert.equal(ctx.files.length, 300);
});

test('fetchChangeContext：head 漂移（首读不符）→ gitee_head_moved 明确失败', async () => {
  const { p } = ctxFetch([
    REPO_ROUTE,
    [`/repos/${REPO}/pulls/95569`, { status: 200, body: PR_DETAIL }],
  ], { expectedHeadSha: 'c'.repeat(40) });
  await assert.rejects(p, (e) => e.code === 'gitee_head_moved');
});

test('fetchChangeContext：分页期间 head 推进（双读不一致）→ gitee_head_moved', async () => {
  let reads = 0;
  const advanced = { ...PR_DETAIL, head: { sha: 'ff'.repeat(20), repo: { id: 999 } } };
  const f = stubFetch([
    REPO_ROUTE,
    [`/repos/${REPO}/pulls/95569`, (u) => {
      reads += 1;
      return { status: 200, body: reads >= 2 ? advanced : PR_DETAIL }; // 第二读已推进
    }],
    [`/repos/${REPO}/pulls/95569/files`, { status: 200, body: [{ filename: 'a.md', patch: { diff: '@@ -1 +1 @@\n-x\n+y' } }] }],
  ]);
  await assert.rejects(adapter(f).fetchChangeContext({ repoPath: REPO, nativeRepoId: '10919030',
    crKey: '95569', expectedHeadSha: 'a'.repeat(40) }), (e) => e.code === 'gitee_head_moved');
});

test('fetchChangeContext：分页拉全（两页）+ fork 判定', async () => {
  const page1 = Array.from({ length: 100 }, (_, i) => ({ filename: `f${i}.md`, additions: '1', deletions: '0', patch: { diff: '@@ -1 +1 @@\n-x\n+y' } }));
  const page2 = Array.from({ length: 3 }, (_, i) => ({ filename: `g${i}.md`, additions: '1', deletions: '0', patch: { diff: '@@ -1 +1 @@\n-x\n+y' } }));
  const f = stubFetch([
    REPO_ROUTE,
    [`/repos/${REPO}/pulls/95569`, { status: 200, body: { ...PR_DETAIL, head: { sha: 'a'.repeat(40), repo: { id: 999 } } } }],
    [`/repos/${REPO}/pulls/95569/files`, (u) => {
      const page = Number(u.searchParams.get('page') ?? 1);
      return { status: 200, body: page === 1 ? page1 : page2 };
    }],
  ]);
  const ctx = await adapter(f).fetchChangeContext({ repoPath: REPO, nativeRepoId: '10919030',
    crKey: '95569', expectedHeadSha: 'a'.repeat(40), declaredFileCount: 103 });
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
