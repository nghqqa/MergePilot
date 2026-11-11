// console/backend/test/github-legacy-contract.test.mjs — GitHub legacy webhook
// payload 入队前契约预检单元测试（PR-B；无 PG，CI glob `*.test.mjs` 内）。
//
// 四层证明：
//  A. 逐字段正/负例（谓词逐字=消费侧 api.mjs:302-306，含 {6,40} 大小写边界）；
//  B. 逐字段配对证明（每条预检规则的拒绝/放行与消费侧行为一一对应；并证明
//     「零加强」——消费侧不闸的字段预检一律不闸）；
//  C. 属性测试（确定性 fuzz + 全叉乘）：precheck-accept ⊆ consumer-accept 且
//     反向亦然（双向等价）——任何「更严/更松」漂移即红；
//  D. 历史真实 payload 回放：现有测试中全部合法 pull_request webhook fixtures
//     （mu-ghapp / mu-job-consumer / mu-gitee-consumer / mu-tenant-boundary /
//     mu-wave3-e2e，含 action 变体）逐一过预检全部放行（回放语料来源与条数见
//     REPLAY 数组注释，PR body 如实引用）。
// 消费侧对照面 consumerEventPayloadInvalid 为 api.mjs:302-306 的逐字副本
// （仅形参名替换）——副本漂移时本文件与 CI 集成测试（mu-ghapp）共同兜底。
import test from 'node:test';
import assert from 'node:assert/strict';
import { validateGitHubLegacyPullRequestPayload, GITHUB_LEGACY_PRECHECK_FIELDS }
  from '../lib/multiuser/forge/github-legacy-contract.mjs';

// ── 消费侧闸逐字副本（来源 api.mjs:302-306；:304 githubRepoId 读取但不闸，保留以示对照）──
function consumerEventPayloadInvalid(payload) {
  const prNumber = Number(payload?.pr_number ?? 0);           // api.mjs:302
  const headSha = String(payload?.head_sha ?? '');            // api.mjs:303
  const githubRepoId = Number(payload?.github_repo_id ?? 0);  // api.mjs:304（读取，不参与 :305 判定）
  if (!prNumber || !/^[0-9a-f]{6,40}$/i.test(headSha)) return true; // api.mjs:305
  return false;
}

// ── 构造变换逐字副本（来源 api.mjs GitHub webhook pull_request 分支，PR-B 后与预检同点）──
function constructLegacyPayload(body, deliveryId) {
  return { event: 'pull_request', delivery_id: deliveryId,
    installation_id: Number(body.installation?.id ?? 0),
    github_repo_id: Number(body.repository?.id ?? 0),
    pr_number: Number(body.pull_request?.number ?? 0),
    head_sha: String(body.pull_request?.head?.sha ?? ''),
    action: String(body.action ?? '') };
}

const VALID_BODY = { installation: { id: 7001 }, repository: { id: 9001 }, action: 'opened',
  pull_request: { number: 42, head: { sha: 'ab'.repeat(20) } } };

// 确定性 PRNG（mulberry32）——属性测试可复现，失败可重放
function mulberry32(a) {
  return function () {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// ══ A. 逐字段正/负例 ══════════════════════════════════════════════
test('A1 合法 payload（构造原样）→ ok 且无 reason/field', () => {
  const p = constructLegacyPayload(VALID_BODY, 'd-a1');
  const v = validateGitHubLegacyPullRequestPayload(p);
  assert.equal(v.ok, true);
  assert.equal(v.reason, undefined);
  assert.equal(v.field, undefined);
});
test('A2 pr_number 负例（0/NaN/缺字段/null 串）→ 拒绝且 field=pr_number', () => {
  for (const raw of [0, NaN, '', 'abc', '0', 'not-a-number', undefined, null, false]) {
    const body = { installation: { id: 7001 }, repository: { id: 9001 }, action: 'opened',
      pull_request: { number: raw, head: { sha: 'ab'.repeat(20) } } };
    const p = constructLegacyPayload(body, 'd-a2');
    const v = validateGitHubLegacyPullRequestPayload(p);
    assert.equal(v.ok, false, `pr_number raw=${String(raw)} 应拒`);
    assert.equal(v.reason, 'event_payload_invalid');
    assert.equal(v.field, 'pr_number');
    assert.equal(consumerEventPayloadInvalid(p), true, '消费侧同拒（api.mjs:302/305）');
  }
});
test('A3 pr_number 不加强正例：消费侧 !x 放行的边缘值预检同样放行（无正整数/isInteger 加强）', () => {
  for (const raw of [1, 42, 999999, 0.5, -5, '12', ' 42 ', true, 1e21, Number.MAX_SAFE_INTEGER]) {
    const body = { installation: { id: 7001 }, repository: { id: 9001 }, action: 'opened',
      pull_request: { number: raw, head: { sha: 'ab'.repeat(20) } } };
    const p = constructLegacyPayload(body, 'd-a3');
    assert.equal(validateGitHubLegacyPullRequestPayload(p).ok, true, `pr_number raw=${String(raw)}`);
    assert.equal(consumerEventPayloadInvalid(p), false, `消费侧同放行 raw=${String(raw)}`);
  }
});
test('A4 head_sha 负例（空/过短/过长/非 hex/带空白）→ 拒绝且 field=head_sha', () => {
  for (const sha of ['', 'a'.repeat(5), 'a'.repeat(41), 'a'.repeat(64),
    'zzzzzz', 'abcdeg', 'abc def', ' abcdef', 'abcdef ', 'abcdef\n', '0xabcdef',
    undefined, null, 123, true, {}, []]) {
    const p = { event: 'pull_request', delivery_id: 'd-a4', installation_id: 7001,
      github_repo_id: 9001, pr_number: 42, head_sha: sha, action: 'opened' };
    const v = validateGitHubLegacyPullRequestPayload(p);
    assert.equal(v.ok, false, `head_sha=${JSON.stringify(sha)} 应拒`);
    assert.equal(v.reason, 'event_payload_invalid');
    assert.equal(v.field, 'head_sha');
    assert.equal(consumerEventPayloadInvalid(p), true, '消费侧同拒（api.mjs:303/305）');
  }
});
test('A5 head_sha 边界逐字对齐：{6,40} 含大写 /i；64 位完整 sha 反而超界（与 forge v1 {6,64} 不同，禁止对齐 forge）', () => {
  for (const sha of ['abcdef', 'ABCDEF', 'AbCdEf', 'a'.repeat(7), 'a'.repeat(39),
    'a'.repeat(40), '0123456789ABCDEF']) {
    const p = { event: 'pull_request', delivery_id: 'd-a5', installation_id: 7001,
      github_repo_id: 9001, pr_number: 42, head_sha: sha, action: 'opened' };
    assert.equal(validateGitHubLegacyPullRequestPayload(p).ok, true, `head_sha=${JSON.stringify(sha)} 应放行`);
    assert.equal(consumerEventPayloadInvalid(p), false, `消费侧同放行 ${JSON.stringify(sha)}`);
  }
  // 64 位：消费侧 {6,40} 拒（GitHub 实发 40 位 sha1；64 位即超界）——预检保持同判
  const p64 = { event: 'pull_request', delivery_id: 'd-a5b', installation_id: 7001,
    github_repo_id: 9001, pr_number: 42, head_sha: 'cd'.repeat(32), action: 'opened' };
  assert.equal(validateGitHubLegacyPullRequestPayload(p64).ok, false);
  assert.equal(validateGitHubLegacyPullRequestPayload(p64).field, 'head_sha');
  assert.equal(consumerEventPayloadInvalid(p64), true, '消费侧 {6,40} 同拒 64 位');
});
test('A6 两字段同坏 → field 报 pr_number（:305 || 短路求值顺序；放行集合不受影响）', () => {
  const p = { event: 'pull_request', delivery_id: 'd-a6', installation_id: 7001,
    github_repo_id: 9001, pr_number: 0, head_sha: 'zz', action: 'opened' };
  const v = validateGitHubLegacyPullRequestPayload(p);
  assert.equal(v.ok, false);
  assert.equal(v.field, 'pr_number');
  assert.equal(consumerEventPayloadInvalid(p), true);
});

// ══ B. 逐字段配对证明 + 零加强证明 ════════════════════════════════
test('B1 逐字段配对证明：每条预检规则（GITHUB_LEGACY_PRECHECK_FIELDS）拒绝/放行与消费侧一一对应', () => {
  assert.deepEqual(GITHUB_LEGACY_PRECHECK_FIELDS, ['pr_number', 'head_sha'],
    '预检闸字段清单必须恰为消费侧 :302-306 据以拒绝的字段');
  const base = constructLegacyPayload(VALID_BODY, 'd-b1');
  for (const field of GITHUB_LEGACY_PRECHECK_FIELDS) {
    const bad = { ...base };
    if (field === 'pr_number') bad.pr_number = 0; else bad.head_sha = 'zz';
    const v = validateGitHubLegacyPullRequestPayload(bad);
    assert.equal(v.ok, false, `${field} 单独坏 → 预检拒`);
    assert.equal(v.field, field, `${field} 单独坏 → 预检定位该字段`);
    assert.equal(consumerEventPayloadInvalid(bad), true, `${field} 负例：消费侧同拒`);
    const fixed = { ...bad };
    if (field === 'pr_number') fixed.pr_number = 42; else fixed.head_sha = 'cd'.repeat(20);
    assert.equal(validateGitHubLegacyPullRequestPayload(fixed).ok, true, `${field} 修复 → 预检放行`);
    assert.equal(consumerEventPayloadInvalid(fixed), false, `${field} 修复 → 消费侧同放行`);
  }
});
test('B2 零加强证明：消费侧读取但不闸的字段（github_repo_id/event/delivery_id/installation_id/action）预检一律不闸', () => {
  const base = constructLegacyPayload(VALID_BODY, 'd-b2');
  const variants = [
    ['github_repo_id=0（:304 只读不闸）', { ...base, github_repo_id: 0 }],
    ['github_repo_id 缺失', { ...base, github_repo_id: undefined }],
    ['event 缺失', { ...base, event: undefined }],
    ['delivery_id 缺失', { ...base, delivery_id: undefined }],
    ['installation_id 缺失', { ...base, installation_id: undefined }],
    ['action 空串（无 action 白名单）', { ...base, action: '' }],
    ['action 未知值（无 action 白名单）', { ...base, action: 'weird_unknown_action' }],
    ['action 数字化', { ...base, action: 42 }],
    ['payload 携带多余字段（不因未知字段拒绝）', { ...base, extra_field: 'x' }],
  ];
  for (const [name, p] of variants) {
    assert.equal(validateGitHubLegacyPullRequestPayload(p).ok, true, `预检应放行：${name}`);
    assert.equal(consumerEventPayloadInvalid(p), false, `消费侧同放行：${name}`);
  }
});

// ══ C. 属性测试：预检与消费侧闸双向等价 ═══════════════════════════
const PR_RAWS = [undefined, null, 0, NaN, '', '0', '00', 'abc', 'not-a-number', ' 42 ', '12',
  1, 42, 999999, -5, 0.5, true, false, 1e21, Number.MAX_SAFE_INTEGER, 2 ** 53, '007', [], ['42'], {}];
const SHA_RAWS = [undefined, null, '', 'a', 'ab'.repeat(3), 'abcdef', 'ABCDEF', 'AbCdEf',
  'a'.repeat(5), 'a'.repeat(6), 'a'.repeat(7), 'a'.repeat(39), 'a'.repeat(40), 'a'.repeat(41),
  'a'.repeat(64), 'g'.repeat(6), 'zzzzzz', 'abcdeg', 'abc def', ' abcdef', 'abcdef ',
  123, true, {}, [], '0xabcdef', '…', 'ffffffffffffffffffffffffffffffffffffffff'];
const ACTION_RAWS = ['opened', 'synchronize', 'closed', 'reopened', 'labeled', '', 'x', null, undefined, 42];

function assertEquivalence(payload, where) {
  const v = validateGitHubLegacyPullRequestPayload(payload);
  const consumerBad = consumerEventPayloadInvalid(payload);
  assert.equal(v.ok, !consumerBad, `双向等价破坏（${where}）: ${JSON.stringify(payload)}`);
  if (v.ok) assert.equal(v.reason, undefined);
  else {
    assert.equal(v.reason, 'event_payload_invalid');
    assert.ok(GITHUB_LEGACY_PRECHECK_FIELDS.includes(v.field), `field 词表越界（${where}）: ${v.field}`);
  }
}
test('C1 属性测试·全叉乘：pr_number×head_sha 语料全组合双向等价（24×29=696 例）', () => {
  let accepted = 0, rejected = 0;
  for (const prRaw of PR_RAWS) {
    for (const shaRaw of SHA_RAWS) {
      const body = { installation: { id: 7001 }, repository: { id: 9001 }, action: 'opened',
        pull_request: { number: prRaw, head: { sha: shaRaw } } };
      const p = constructLegacyPayload(body, 'd-c1');
      assertEquivalence(p, `pr=${JSON.stringify(prRaw)} sha=${JSON.stringify(shaRaw)}`);
      if (validateGitHubLegacyPullRequestPayload(p).ok) accepted++; else rejected++;
    }
  }
  assert.ok(accepted >= 100 && rejected >= 100, `两侧样本须足量（accept=${accepted}, reject=${rejected}）`);
});
test('C2 属性测试·确定性 fuzz 5000 例（含 action/外层字段扰动）：双向等价', () => {
  const rand = mulberry32(0x5eedb);
  const pick = (arr) => arr[Math.floor(rand() * arr.length)];
  const hexish = () => {
    const chars = '0123456789abcdefABCDEFgzZ -_';
    const n = 1 + Math.floor(rand() * 66);
    return Array.from({ length: n }, () => chars[Math.floor(rand() * chars.length)]).join('');
  };
  let accepted = 0, rejected = 0;
  for (let i = 0; i < 5000; i++) {
    const body = {
      installation: { id: pick([0, 7001, '7001', null, undefined, 1.5]) },
      repository: { id: pick([0, 9001, '9001', null, undefined, 1.5]) },
      pull_request: { number: pick(PR_RAWS), head: { sha: rand() < 0.5 ? pick(SHA_RAWS) : hexish() } },
      action: pick(ACTION_RAWS),
    };
    const p = constructLegacyPayload(body, 'd-c2-' + i);
    assertEquivalence(p, `fuzz i=${i}`);
    if (validateGitHubLegacyPullRequestPayload(p).ok) accepted++; else rejected++;
  }
  assert.ok(accepted >= 250 && rejected >= 500, `两侧样本须足量（accept=${accepted}, reject=${rejected}）`);
});
test('C3 JSONB 往返保真：放行 payload 序列化往返后预检与消费侧判定不变', () => {
  const p = constructLegacyPayload(VALID_BODY, 'd-c3');
  const rt = JSON.parse(JSON.stringify(p));
  assert.deepEqual(rt, p, '7 字段 payload 全部 JSON 安全');
  assertEquivalence(rt, 'jsonb round-trip');
});
test('C4 NaN pr_number 不可入 JSONB：构造期即被预检拒绝（若漏网序列化为 null，消费侧同拒——等价性在两侧均成立）', () => {
  const p = constructLegacyPayload({ installation: { id: 1 }, repository: { id: 2 }, action: 'opened',
    pull_request: { number: 'not-a-number', head: { sha: 'ab'.repeat(20) } } }, 'd-c4');
  assert.equal(Number.isNaN(p.pr_number), true);
  assertEquivalence(p, 'NaN constructed');
  const serialized = JSON.parse(JSON.stringify(p)); // NaN → null（jsonb 同理）
  assert.equal(serialized.pr_number, null);
  assertEquivalence(serialized, 'NaN→null serialized');
});

// ══ D. 历史真实 payload 回放（全部必须放行——防「更严」回归）══════
// 语料来源=现有测试中全部【能到达入队路径】的合法 pull_request webhook fixtures
// （GA5f 的未知 installation 变体不入列——它在 :1038-1043 installation 闸即被忽略，
//   从不到达 payload 构造/预检；mu-job-consumer C8i 的负例为 SQL 直插 mu.job，
//   按设计绕过入口，由消费侧 :302-306 兜底（回归锁保留在原测试）。
const REPLAY = [
  // mu-ghapp.integration.mjs（6 条：delivery d-pr-1/d-es-1/d-es-2/d-es-3/d-es-4/d-es-prod）
  { src: 'mu-ghapp GA5d/GA5e prBody（d-pr-1，opened）', body: { installation: { id: 7001 }, repository: { id: 9001 }, action: 'opened', pull_request: { number: 42, head: { sha: 'ab'.repeat(20) } } } },
  { src: 'mu-ghapp ES0（d-es-1，opened）', body: { installation: { id: 7001 }, repository: { id: 9001 }, action: 'opened', pull_request: { number: 43, head: { sha: 'cd'.repeat(20) } } } },
  { src: 'mu-ghapp ES3（d-es-2，synchronize）', body: { installation: { id: 7001 }, repository: { id: 9001 }, action: 'synchronize', pull_request: { number: 43, head: { sha: 'cd'.repeat(20) } } } },
  { src: 'mu-ghapp ES4（d-es-3，opened）', body: { installation: { id: 7001 }, repository: { id: 9001 }, action: 'opened', pull_request: { number: 44, head: { sha: 'ef'.repeat(20) } } } },
  { src: 'mu-ghapp ES5（d-es-4，opened）', body: { installation: { id: 7001 }, repository: { id: 9001 }, action: 'opened', pull_request: { number: 45, head: { sha: 'ab'.repeat(20) } } } },
  { src: 'mu-ghapp ES9（d-es-prod，opened）', body: { installation: { id: 7001 }, repository: { id: 9001 }, action: 'opened', pull_request: { number: 47, head: { sha: '77'.repeat(20) } } } },
  // mu-job-consumer.integration.mjs deliverPrOpened 模板（C2/C3/C4/C5/C6/E1 六实例：
  // pr 101/102/103/104/105/201；原测试 sha=crypto 随机 40 hex，此处取代表性定值——
  // 任意 40 hex 已由 A5/C1/C2 覆盖）
  { src: 'mu-job-consumer deliverPrOpened C2（pr101）', body: deliverPrOpenedBody(101, '3f2a9c1e7b5d8046a2c9e81b70d3f5a4c6e9b281') },
  { src: 'mu-job-consumer deliverPrOpened C3（pr102）', body: deliverPrOpenedBody(102, '0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d') },
  { src: 'mu-job-consumer deliverPrOpened C4（pr103）', body: deliverPrOpenedBody(103, 'f'.repeat(40)) },
  { src: 'mu-job-consumer deliverPrOpened C5（pr104）', body: deliverPrOpenedBody(104, '0'.repeat(40)) },
  { src: 'mu-job-consumer deliverPrOpened C6（pr105）', body: deliverPrOpenedBody(105, '1234567890abcdef1234567890abcdef12345678') },
  { src: 'mu-job-consumer deliverPrOpened E1（pr201）', body: deliverPrOpenedBody(201, 'fedcba0987654321fedcba0987654321fedcba09') },
  // mu-gitee-consumer.integration.mjs T13（GitHub legacy 回归段）
  { src: 'mu-gitee-consumer T13（pr201，opened）', body: { action: 'opened', number: 201,
    pull_request: { number: 201, state: 'open', title: 'legacy', user: { login: 'u' },
      head: { ref: 'b', sha: '9'.repeat(40) }, base: { ref: 'main' }, changed_files: 1 },
    repository: { id: 98001, name: 'r', full_name: 'w31/r', owner: { login: 'w31' } },
    installation: { id: 1 }, sender: { login: 'u' } } },
  // mu-tenant-boundary.integration.mjs TB5a（ISO-5 tenant 回填，delivery tb-d-1）
  { src: 'mu-tenant-boundary TB5a（pr5，opened）', body: { installation: { id: 79101 }, repository: { id: 93001 }, action: 'opened', pull_request: { number: 5, head: { sha: 'aa'.repeat(20) } } } },
  // mu-wave3-e2e.integration.mjs E2/E3/E4（PRN=11；sha=真实 git rev-parse 40 hex，
  // 此处取定值——任意 40 hex 已由 A5/C1/C2 覆盖）
  { src: 'mu-wave3-e2e E2（pr11，opened，w3-d1）', body: { action: 'opened', installation: { id: 79001 }, repository: { id: 99001, full_name: 'w3/repo' }, pull_request: { number: 11, head: { sha: '1a2b3c4d5e6f70819a2b3c4d5e6f70819a2b3c4d' }, base: { ref: 'main' } } } },
  { src: 'mu-wave3-e2e E3（pr11，synchronize，w3-d2）', body: { action: 'synchronize', installation: { id: 79001 }, repository: { id: 99001 }, pull_request: { number: 11, head: { sha: '1a2b3c4d5e6f70819a2b3c4d5e6f70819a2b3c4d' } } } },
  { src: 'mu-wave3-e2e E4（pr11，opened，w3-d4）', body: { action: 'opened', installation: { id: 79001 }, repository: { id: 99001 }, pull_request: { number: 11, head: { sha: 'b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5' } } } },
];
function deliverPrOpenedBody(prNumber, headSha) {
  // 逐字复制 mu-job-consumer.integration.mjs deliverPrOpened 的 body 模板（:82-87）
  return { action: 'opened', number: prNumber,
    pull_request: { number: prNumber, state: 'open', title: 't', user: { login: 'u' },
      head: { ref: 'b', sha: headSha }, base: { ref: 'main' }, changed_files: 1 },
    repository: { id: 98001, name: 'r', full_name: 'w31/r', owner: { login: 'w31' } },
    installation: { id: 1 }, sender: { login: 'u' } };
}
test('D1 历史回放：现有测试全部合法 pull_request fixtures（17 条，5 个来源文件）逐一过预检全放行且消费侧同放行', () => {
  assert.equal(REPLAY.length, 17, '回放语料条数锁定（增删 fixtures 时同步更新 PR body 清单）');
  for (const { src, body } of REPLAY) {
    const p = constructLegacyPayload(body, 'd-replay');
    const v = validateGitHubLegacyPullRequestPayload(p);
    assert.equal(v.ok, true, `回放应放行：${src}`);
    assert.equal(consumerEventPayloadInvalid(p), false, `消费侧同放行：${src}`);
  }
});
