// ragtrial/api.mjs — /api/rag-trial/* HTTP 路由（LOCAL_RAG_TRIAL）。
//
// 与 A 链（/api/rag/org-search，lexical，reference-only）完全并行、互不影响；
// 与 C 链（cchain）互不接触。全部端点需会话（401 未登录）。
// PG 未接线（CONSOLE_PG_DSN 未配置）→ 如实 BACKEND_NOT_WIRED，不伪装。

import fs from 'node:fs';
import path from 'node:path';
import { createRagTrialStore, RagTrialError } from './store.mjs';
import { createJobQueue } from './queue.mjs';
import { resolveProvider, ensureProviderAttested } from './embed.mjs';
import {
  toAuxEvidence, canAutoPromote, fixerPatchInputs, verifierAccepts,
  promotionRequest, attachToRun,
} from './review.mjs';
import { verifyRunBindingAndAudit } from '../cchain/wiring.mjs';
import { runBindingAuthStatus, providerAttestConfig, fetchProviderAttestation } from '../cchain/index.mjs';

const BODY_LIMIT = 8 * 1024 * 1024; // 语料摄取走 body（默认 64KB 不够）
const inMemory = { error_states: {} }; // PG 不可达时的 error 态计数（PG 日志不可能写下）

function errKindCount(kind) {
  inMemory.error_states[kind] = (inMemory.error_states[kind] ?? 0) + 1;
}
let storePromise = null;
let queuePromise = null;
function getPool(env) {
  return (async () => {
    const pg = await import('pg').catch(() => null);
    if (!pg) throw new RagTrialError('pg module unavailable', 'pg_unavailable', 503);
    const pool = new pg.Pool({ connectionString: env.CONSOLE_PG_DSN, max: 4 });
    // PG 中断时 idle 连接会发 'error' 事件；不挂监听会成为未捕获异常把进程打死
    // （S12 实测坑）。查询路径自己处理连接错误，这里只记录保活。
    pool.on('error', (err) => {
      errKindCount('pool_connection_error');
      console.error(`[ragtrial] pg pool error (kept alive): ${String(err.message).slice(0, 120)}`);
    });
    return pool;
  })();
}
async function getStore(env) {
  if (!env.CONSOLE_PG_DSN) return null;
  if (!storePromise) {
    storePromise = (async () => {
      const pool = await getPool(env);
      const store = await createRagTrialStore({ pool, env });
      await store.initSchema();
      return store;
    })().catch((e) => { storePromise = null; throw e; });
  }
  return storePromise;
}
async function getQueue(env) {
  if (!env.CONSOLE_PG_DSN) return null;
  if (!queuePromise) {
    queuePromise = (async () => {
      const pool = await getPool(env);
      await createRagTrialStore({ pool, env }).then((s) => s.initSchema()); // 确保 schema（jobs 表）
      return createJobQueue({ pool });
    })().catch((e) => { queuePromise = null; throw e; });
  }
  return queuePromise;
}

async function buildS3(env) {
  const endpoint = env.RAGTRIAL_S3_ENDPOINT;
  if (!endpoint) return { configured: false };
  const { createArtifactStore } = await import('../fxv/artifacts.mjs');
  return createArtifactStore({
    endpoint,
    bucket: env.RAGTRIAL_S3_BUCKET || 'ragtrial',
    accessKey: env.RAGTRIAL_S3_ACCESS_KEY,
    secretKey: env.RAGTRIAL_S3_SECRET_KEY,
  });
}

function readCorpusDir(dir, { base }) {
  // 语料目录只允许 env 声明的根下（容器内 ro 挂载 /app/rag-corpus）
  const root = path.resolve(base);
  const abs = path.resolve(dir);
  if (abs !== root && !abs.startsWith(root + path.sep)) {
    throw new RagTrialError(`corpus dir 越界（仅允许 ${root} 下）`, 'corpus_dir_forbidden', 400);
  }
  const out = [];
  for (const name of fs.readdirSync(abs).sort()) {
    if (!name.endsWith('.md') && !name.endsWith('.txt')) continue;
    const p = path.join(abs, name);
    if (!fs.statSync(p).isFile()) continue;
    out.push({ path: name, text: fs.readFileSync(p, 'utf8') });
  }
  if (!out.length) throw new RagTrialError(`corpus dir 无 .md/.txt 文档: ${abs}`, 'corpus_empty', 400);
  return out;
}

// ── RAG scope allowlist（CANONICAL_A：自 B 波选择性吸收并加固）────────────
// 合同：默认拒绝——RAGTRIAL_ALLOWED_SCOPES 未设置/为空 → 一律 403
// （缺配置不得扩大范围）；撤销 env 即时生效（每次请求现读 env）；
// 拒绝审计有界且脱敏：仅记录 repo@branch 与原因（不落查询文本、不回显 allowlist），
// 同源+滚动窗口封顶（默认 30 条/10min，对齐 cchain M-2 防膨胀惯例）。
const scopeDenyBySource = new Map(); // source -> { window_start, count }
function scopeDenyAuditAllowed(env, source) {
  const cap = Number(env.RAGTRIAL_SCOPE_AUDIT_DENY_CAP ?? 30);
  const win = Number(env.RAGTRIAL_SCOPE_AUDIT_WINDOW_MS ?? 600_000);
  if (!(cap > 0)) return true;
  const now = Date.now();
  if (scopeDenyBySource.size > 10_000) {
    for (const [k, st] of scopeDenyBySource) if (now - st.window_start >= win) scopeDenyBySource.delete(k);
  }
  const key = source ?? 'unknown';
  let st = scopeDenyBySource.get(key);
  if (!st || now - st.window_start >= win) st = { window_start: now, count: 0 };
  scopeDenyBySource.set(key, st);
  st.count += 1;
  return st.count <= cap;
}

export function checkScope(env, repo, branch) {
  const raw = String(env.RAGTRIAL_ALLOWED_SCOPES ?? '').trim();
  if (!raw) return { ok: false, reason: 'scope_not_configured' }; // 默认拒绝
  const allowed = raw.split(',').map((x) => x.trim()).filter(Boolean);
  return allowed.includes(`${repo}@${branch}`)
    ? { ok: true }
    : { ok: false, reason: 'scope_not_allowed', allowed_count: allowed.length }; // 脱敏：只报数量
}

async function scopeDeny(env, { store, actor, repo, branch, source, kind }) {
  const check = checkScope(env, repo, branch);
  if (check.ok) return { status: 200 }; // allow 短路——只在拒绝时产生 403/审计
  const bounded = scopeDenyAuditAllowed(env, source);
  if (store && bounded) {
    await store.audit('QUERY_SCOPE_DENIED', actor, {
      repo, branch,
      detail: { reason: check.reason, channel: kind, allowed_count: check.allowed_count ?? 0 },
    }).catch(() => {});
  }
  return { status: 403, body: { error: { reason: check.reason,
    detail: `repo@branch 不在 RAGTRIAL_ALLOWED_SCOPES（env 撤销即时生效${check.reason === 'scope_not_configured' ? '；未配置=默认拒绝' : ''}）` },
    ...(bounded ? {} : { audit_suppressed: true, note: '同源窗口内拒绝审计已封顶' }) } };
}

// ── 会话仓库授权（PHASE0A：与部署 scope 取交集的会话层门）────────────────
// 有效授权 = 部署级 RAGTRIAL_ALLOWED_SCOPES（repo@branch）∩ 已认证会话的
// 授权仓库面 auth.repos（repo 级，来自 CONSOLE_ACCESS_MODEL_JSON 主体解析或
// legacy CONSOLE_REPO_ALLOWLIST）。两层缺一不可；请求参数/body/job payload
// 只能缩小（被校验），不能扩大该交集。机器端点（machine/query）不经会话层，
// 保持 HMAC+部署 scope 的既有语义不变。
export function sessionRepoAllowed(auth, repo) {
  return Array.isArray(auth?.repos) && auth.repos.includes(String(repo ?? ''));
}

export function resetScopeDenyLimiter() { scopeDenyBySource.clear(); } // 仅供测试隔离（对齐 cchain resetDenyAuditLimiter 惯例）

async function sessionRepoDeny(env, { store, auth, actor, repo, branch, source }) {
  if (sessionRepoAllowed(auth, repo)) return { status: 200 };
  const bounded = scopeDenyAuditAllowed(env, source);
  if (store && bounded) {
    await store.audit('RAG_REPO_DENIED', actor, {
      repo, branch,
      detail: { reason: 'repo_not_in_allowlist', channel: 'session-repo',
        session_repo_count: Array.isArray(auth?.repos) ? auth.repos.length : 0 },
    }).catch(() => {});
  }
  return { status: 403, body: { error: { reason: 'repo_not_in_allowlist',
    detail: '会话授权仓库面不含该 repo（服务端强制，默认拒绝；与 /api/pulls 同语义）' },
    ...(bounded ? {} : { audit_suppressed: true, note: '同源窗口内拒绝审计已封顶' }) } };
}

// 组合门：部署 scope（先判，保持既有 reason 语义与测试断言不变）→ 会话 repo（后判）。
// 两路拒绝均有界脱敏审计：只记 repo@branch、reason 与计数，不落查询正文/密钥/allowlist 原值。
async function authorizeRepo(env, { store, auth, actor, repo, branch, source, kind }) {
  const sd = await scopeDeny(env, { store, actor, repo, branch, source, kind });
  if (sd.status === 403) return sd;
  return sessionRepoDeny(env, { store, auth, actor, repo, branch, source });
}

// 全局索引操作（index/invalidate、index/rollback）跨界门：操作目标模型的
// 现存文档仓库集合必须 ⊆ 会话授权面，否则拒绝（脱敏：只报越界数量不回显清单）。
async function indexBoundaryGuard(env, { store, auth, actor, modelId, source }) {
  let info = null;
  try { info = await store.modelRepos(modelId); }
  catch { return { status: 200 }; } // 存储异常交由后续原生路径如实失败
  if (!info) return { status: 200 }; // 模型不存在 → 后续 invalidate/rollback 原生 409
  const outOfScope = info.repos.filter((r) => !sessionRepoAllowed(auth, r));
  if (!outOfScope.length) return { status: 200 };
  const bounded = scopeDenyAuditAllowed(env, source);
  if (store && bounded) {
    await store.audit('RAG_REPO_DENIED', actor, {
      repo: outOfScope[0], branch: null,
      detail: { reason: 'index_op_crosses_repo_boundary', channel: 'index-boundary',
        model_id: info.model_id, out_of_scope_count: outOfScope.length },
    }).catch(() => {});
  }
  return { status: 403, body: { error: { reason: 'index_op_crosses_repo_boundary',
    detail: `模型索引覆盖仓库超出会话授权面（${outOfScope.length} 个越界，脱敏不回显清单）——全局索引操作拒绝` },
    ...(bounded ? {} : { audit_suppressed: true }) } };
}

export async function ragTrialApi(req, res, ctx) {
  // q（查询参数）自 ctx 解构——PHASE0A 修复存量缺陷：GET /jobs 曾引用未解构的
  // q（ReferenceError→500，此前无 HTTP 层测试覆盖，SA8 首次命中）
  const { p, q, sendJson, readJsonBody, requireSession } = ctx;
  const env = process.env;
  const actorOf = (auth) => auth?.user?.name || auth?.user || 'rag-trial-operator';

  // ── 机器端点：RUN_BINDING_AUTH 验签（无会话；HMAC 替代会话，与 cchain 同 keystore）──
  // 密钥未分发/keystore 缺失 → 如实 RUN_BINDING_AUTH_BLOCKED（BLOCKED 语义，不降级）。
  if (p === '/api/rag-trial/machine/query' && req.method === 'POST') {
    const body = await readJsonBody(req) ?? {};
    const source = req.socket?.remoteAddress ?? null;
    const verify = await verifyRunBindingAndAudit(env, env.CONSOLE_PG_DSN, {
      run_id: body.run_id, nonce: body.nonce, timestamp: body.timestamp, signature: body.signature,
    }, { source });
    if (!verify.body.ok) {
      return sendJson(res, verify.status, { ...verify.body,
        note: 'RUN_BINDING_AUTH 拒绝——机器查询通道保持 BLOCKED，不回退到匿名访问' });
    }
    let mstore;
    try { mstore = await getStore(env); }
    catch (e) {
      return sendJson(res, 503, { service_state: 'error', error_kind: 'pg_unavailable' });
    }
    if (!mstore) return sendJson(res, 200, { service_state: 'backend_not_wired' });
    const q = String(body.q || '');
    const repo = String(body.repo || '');
    const branch = String(body.branch || '');
    if (!q || !repo || !branch) return sendJson(res, 400, { error: { reason: 'q/repo/branch required' } });
    // scope 门（机器身份与人工同权约束：越权 403，不因验签通过而放宽）
    const sd = await scopeDeny(env, { store: mstore, actor: `run-binding:${String(body.run_id).slice(0, 64)}`,
      repo, branch, source, kind: 'machine' });
    if (sd.status === 403) return sendJson(res, sd.status, sd.body);
    try {
      const r = await mstore.search({
        q, repo, branch, k: Number(body.k || 5),
        modelId: body.model_id ? String(body.model_id) : null,
        actor: `run-binding:${String(body.run_id).slice(0, 64)}`,
      });
      // 机器通道产出与人工通道同受 Review 边界约束：reference only
      const aux = (r.results ?? []).map((h) => toAuxEvidence(h, { runId: body.run_id }));
      return sendJson(res, 200, {
        ...r, results: undefined, auxiliary_evidence: aux,
        policy: { usage: 'reference_only', may_auto_promote: false,
          excluded_from: ['finding', 'ticket', 'gate', 'VERIFIED', 'fixer_patch_input', 'verifier_evidence'] },
      });
    } catch (e) {
      const kind = e instanceof RagTrialError ? e.kind : 'internal';
      errKindCount(kind);
      return sendJson(res, e?.status ?? 503, { service_state: 'error', error_kind: kind, error: String(e.message).slice(0, 160) });
    }
  }

  // 全部端点需会话
  const auth = await requireSession();
  if (!auth) return sendJson(res, 401, { error: { reason: 'unauthorized' } });
  const actor = actorOf(auth);

  // ── 生产依赖就绪监控（HARDENING 波）：实时探测+告警字段+可观测性日志 ──
  // 返回 keystore/attestation 逐项状态+告警阈值+上次探测时间——缺失=BLOCKED 不伪装。
  // 置于 PG 接线门之前：该端点只探测 env/keystore/外部 attestation，不依赖 PG；
  // 依赖异常期恰是最需要它的时刻（Prometheus/值班抓取不应因存储不可用而失明）。
  if (p === '/api/rag-trial/dependency-status' && req.method === 'GET') {
    const ks = runBindingAuthStatus(env);
    const attestCfg = providerAttestConfig(env);
    const attest = attestCfg.configured
      ? await fetchProviderAttestation(env).catch(() => ({ state: 'UNREACHABLE', blocked_condition: '探测异常（fail-closed，不降级为未配置）' }))
      : null;
    const now = new Date().toISOString();
    const deps = {
      timestamp: now,
      keystore: {
        state: ks.state,
        blocked_condition: ks.blocked_condition ?? null,
        key_count: ks.key_count ?? 0,
        not_distributed: ks.not_distributed ?? false,
      },
      attestation: attest
        ? { state: attest.state, blocked_condition: attest.blocked_condition ?? null, key_id: attest.key_id ?? null }
        : { state: 'NOT_CONFIGURED', blocked_condition: attestCfg.blocked_condition ?? '未设置' },
    };
    deps.overall = (deps.keystore.state === 'READY' && deps.attestation.state === 'ATTESTED') ? 'READY' : 'BLOCKED';
    // 告警字段（运维可挂 Prometheus/Grafana）：缺失即告警
    deps.alerts = [];
    if (deps.keystore.state !== 'READY') deps.alerts.push({ severity: 'CRITICAL', component: 'keystore', condition: deps.keystore.state });
    if (deps.attestation.state !== 'ATTESTED') deps.alerts.push({ severity: 'CRITICAL', component: 'attestation', condition: deps.attestation.state });
    // 可观测性日志（结构化——不落任何凭据/路径原值）
    console.log(JSON.stringify({ ts: now, level: 'info', component: 'ragtrial.dependency',
      keystore_state: deps.keystore.state, attestation_state: deps.attestation.state, overall: deps.overall }));
    return sendJson(res, 200, deps);
  }

  let store;
  try { store = await getStore(env); }
  catch (e) {
    const kind = e instanceof RagTrialError ? e.kind : 'pg_unavailable';
    errKindCount(kind);
    return sendJson(res, 503, {
      service_state: 'error', error_kind: kind,
      in_memory_error_states: inMemory.error_states,
      note: 'ragtrial 存储不可用——显式错误，不伪装',
    });
  }
  if (!store) {
    return sendJson(res, 200, {
      service_state: 'backend_not_wired',
      note: 'CONSOLE_PG_DSN 未配置 — ragtrial API 返回 BACKEND_NOT_WIRED（不伪装检索）',
    });
  }

  const json = async (limit = 64 * 1024) => readJsonBody(req, limit);

  try {
    // ── 状态（含生产就绪组件：语义 provider attestation / RUN_BINDING / 队列） ──
    if (p === '/api/rag-trial/status' && req.method === 'GET') {
      const base = await store.status();
      // 语义 provider：远端配置时做一次真实 /manifest attestation（fail-closed 观测）
      const provider = resolveProvider(env);
      let semantic;
      if (provider.kind === 'local') {
        semantic = { state: 'NOT_CONFIGURED', note: 'RAGTRIAL_EMBED_ENDPOINT 未配置——语义 provider 未接线（BLOCKED，不伪装）' };
      } else if (!provider.expected_manifest) {
        semantic = { state: 'BLOCKED', blocked_condition: 'RAGTRIAL_EMBED_EXPECTED_MANIFEST 未配置（attestation 强制缺失=BLOCKED）' };
      } else {
        try {
          const a = await ensureProviderAttested(provider);
          semantic = a.attested
            ? { state: 'LOCAL_MANIFEST_VERIFIED',
                provider_kind: 'remote-sidecar-local-manifest',
                model_id: provider.model_id,
                manifest_sha256: a.manifest_sha256, dims: a.dims,
                runtime: a.manifest?.runtime ?? null,
                verification: 'sha256+bytes+count（sidecar 启动 fail-closed + 查询期 pin 比对）',
                external_attestation: { state: 'NOT_CONFIGURED',
                  note: '本地 manifest 验证不等于外部 attestation——外部服务未接入，如实 NOT_CONFIGURED' } }
            : { state: 'BLOCKED', blocked_condition: a.note };
        } catch (e) {
          semantic = { state: e?.name === 'ModelBlockedError' ? 'BLOCKED' : 'UNREACHABLE',
            blocked_condition: String(e.message).slice(0, 120) };
        }
      }
      const binding = runBindingAuthStatus(env);
      let queueStats = null;
      try {
        queueStats = await (await getQueue(env)).stats();
        // PHASE0A：死信清单按会话授权面过滤（聚合计数保持全局观测语义）
        if (queueStats && Array.isArray(queueStats.dead_letters)) {
          queueStats.dead_letters = queueStats.dead_letters.filter((d) => sessionRepoAllowed(auth, d.repo));
        }
      }
      catch { queueStats = { state: 'error' }; }
      return sendJson(res, 200, {
        service_state: 'ready', ...base,
        production_readiness: {
          semantic_provider: semantic,
          run_binding_auth: binding.state === 'READY'
            ? { state: 'READY', key_count: binding.key_count }
            : { state: 'BLOCKED', blocked_condition: binding.blocked_condition },
          persistent_queue: queueStats,
          note: '任一组件 BLOCKED 时生产候选保持 TRIAL_READY，不宣称 PRODUCTION_READY',
        },
      });
    }

    // ── 持久任务队列 ──
    if (p === '/api/rag-trial/jobs' && req.method === 'POST') {
      const queue = await getQueue(env);
      const body = await json(BODY_LIMIT) ?? {};
      const jobs = body.jobs ?? (body.kind ? [body] : null);
      if (!Array.isArray(jobs) || !jobs.length
          || jobs.some((j) => !j?.kind || !j?.repo || !j?.branch || !j?.model_id)) {
        return sendJson(res, 400, { error: { reason: 'jobs[{kind,repo,branch,model_id,(doc_path,text)}] 必填' } });
      }
      const out = [];
      // PHASE0A：整批先过组合授权门——任一 job 越权即全批拒绝，零入队副作用
      const jobsSource = req.socket?.remoteAddress ?? null;
      for (const j of jobs) {
        const sdJ = await authorizeRepo(env, { store, auth, actor,
          repo: String(j.repo), branch: String(j.branch || ''), source: jobsSource, kind: 'session' });
        if (sdJ.status === 403) {
          return sendJson(res, sdJ.status, { ...sdJ.body, batch_rejected: true });
        }
      }
      for (const j of jobs) {
        // ingest_doc 允许 text 内联（worker 消费）；delete_doc 需 doc_path
        if (j.kind === 'delete_doc' && !j.doc_path) {
          return sendJson(res, 400, { error: { reason: 'delete_doc 需要 doc_path' } });
        }
        out.push(await queue.enqueue(j, { actor }));
      }
      return sendJson(res, 200, { ok: true, enqueued: out });
    }
    if (p === '/api/rag-trial/jobs' && req.method === 'GET') {
      const queue = await getQueue(env);
      // PHASE0A：任务列表按会话授权面服务端过滤（越权仓库的 job 零行返回）
      const rows = (await queue.list({ state: q.state ?? null, limit: Number(q.limit || 50) }))
        .filter((j) => sessionRepoAllowed(auth, j.repo));
      return sendJson(res, 200, { jobs: rows });
    }
    if (p.startsWith('/api/rag-trial/jobs/') && p.endsWith('/requeue') && req.method === 'POST') {
      const queue = await getQueue(env);
      const jobId = p.slice('/api/rag-trial/jobs/'.length, -'/requeue'.length);
      // PHASE0A/F-1：不存在与"存在但越权"统一 404（消除存在性差分——此前不存在
      // →409 not_dead、越权→404，状态码本身构成存在性预言机）。job 状态语义对
      // 已授权调用者保持：存在+授权+非 dead → 409 not_dead 原样。
      // 越权存在仍在服务端留有界审计（审计为可信侧，不回显差异给调用者）。
      const target = await queue.get(jobId);
      if (!target || !sessionRepoAllowed(auth, target.repo)) {
        if (target) {
          const bounded = scopeDenyAuditAllowed(env, req.socket?.remoteAddress ?? null);
          if (store && bounded) {
            await store.audit('RAG_REPO_DENIED', actor, {
              repo: target.repo, branch: target.branch,
              detail: { reason: 'repo_not_in_allowlist', channel: 'requeue' },
            }).catch(() => {});
          }
        }
        return sendJson(res, 404, { error: { reason: 'unknown_job' } });
      }
      const r = await queue.requeueDead(jobId, { actor });
      if (!r.ok) return sendJson(res, 409, { error: { reason: r.reason } });
      return sendJson(res, 200, r);
    }
    if (p === '/api/rag-trial/queue/metrics' && req.method === 'GET') {
      const queue = await getQueue(env);
      const qm = await queue.stats();
      // PHASE0A：死信明细按会话授权面过滤（状态聚合计数保持全局观测语义）
      if (Array.isArray(qm.dead_letters)) {
        qm.dead_letters = qm.dead_letters.filter((d) => sessionRepoAllowed(auth, d.repo));
      }
      return sendJson(res, 200, qm);
    }

    // ── 供应链披露（自 B 波选择性吸收）：只报类型/模型/摘要/维度/验证状态 ──
    if (p === '/api/rag-trial/providers' && req.method === 'GET') {
      const provider = resolveProvider(env);
      const deterministic = {
        provider_kind: 'deterministic-hash', model_id: 'local-hash-v1', dims: 256,
        role: '测试与回归基线（不宣称真实语义能力）',
      };
      let semantic;
      if (provider.kind === 'local') {
        semantic = { state: 'NOT_CONFIGURED',
          blocked_condition: 'RAGTRIAL_EMBED_ENDPOINT 未设置——语义 provider 未接线' };
      } else if (!provider.expected_manifest) {
        semantic = { state: 'BLOCKED', blocked_condition: 'RAGTRIAL_EMBED_EXPECTED_MANIFEST 未配置' };
      } else {
        try {
          const a = await ensureProviderAttested(provider);
          semantic = a.attested
            ? { state: 'LOCAL_MANIFEST_VERIFIED', provider_kind: 'remote-sidecar-local-manifest',
                model_id: provider.model_id, manifest_sha256: a.manifest_sha256, dims: a.dims,
                runtime: a.manifest?.runtime ?? null,
                external_attestation: { state: 'NOT_CONFIGURED' } }
            : { state: 'BLOCKED', blocked_condition: a.note };
        } catch (e) {
          semantic = { state: e?.name === 'ModelBlockedError' ? 'BLOCKED' : 'UNREACHABLE',
            blocked_condition: String(e.message).slice(0, 120) };
        }
      }
      return sendJson(res, 200, {
        primary_requested: provider.kind === 'local' ? 'deterministic' : 'semantic-sidecar',
        deterministic, semantic,
        disclosure_note: '不回显密钥、keystore 路径或 env 原值；本地 manifest 验证不标记为外部 attestation 成功',
      });
    }

    // ── 模型注册（语义模型：dims+manifest 链式绑定） ──
    if (p === '/api/rag-trial/models' && req.method === 'POST') {
      const body = await json(BODY_LIMIT) ?? {};
      const modelId = String(body.model_id || '');
      const dims = Number(body.dims || 0);
      const manifest = body.manifest;
      if (!modelId || ![256, 768, 1024].includes(dims) || !manifest || typeof manifest !== 'object') {
        return sendJson(res, 400, { error: { reason: 'model_id + dims(256|1024) + manifest{} 必填' } });
      }
      // spec digest 绑定：model_id+dims+manifest 内容+runtime——任一变化即 digest 冲突
      const spec = {
        model_id: modelId, dims, provider: 'remote-attested',
        manifest, distance: 'cosine', pooling: manifest.pooling ?? 'cls_l2',
        runtime: manifest.runtime ?? 'numpy-bert-v1',
      };
      const r = await store.registerModel(spec, 'remote', {
        actor, dims, manifest, force: body.force === true,
      });
      return sendJson(res, 200, r);
    }

    // ── 摄取（body docs 或 corpus_dir） ──
    if (p === '/api/rag-trial/ingest' && req.method === 'POST') {
      const body = await json(BODY_LIMIT) ?? {};
      const repo = String(body.repo || '');
      const branch = String(body.branch || '');
      if (!repo || !branch) return sendJson(res, 400, { error: { reason: 'repo/branch required' } });
      // PHASE0A 组合授权门（scope → 会话 repo）：越权 403，零摄取副作用
      const sdIng = await authorizeRepo(env, { store, auth, actor, repo, branch,
        source: req.socket?.remoteAddress ?? null, kind: 'session' });
      if (sdIng.status === 403) return sendJson(res, sdIng.status, sdIng.body);
      let docs = body.docs;
      if (!docs && body.corpus_dir) {
        docs = readCorpusDir(body.corpus_dir, { base: env.RAGTRIAL_CORPUS_DIR || '/app/rag-corpus' });
      }
      if (!Array.isArray(docs) || !docs.length || docs.some((d) => !d?.path || typeof d.text !== 'string')) {
        // 机器字段契约：reason 为稳定英文机器码；中文解释放 detail（语言策略见 docs/GLOSSARY.md）
        return sendJson(res, 400, { error: { reason: 'docs or corpus_dir required', detail: 'docs[{path,text}] 或 corpus_dir 必填' } });
      }
      const s3 = await buildS3(env);
      if (s3.configured) await s3.ensureBucket(); // 幂等（已存在→409 容忍）
      const r = await store.ingestDocuments({ docs, repo, branch, actor, objectStore: s3.configured ? s3 : null, modelId: body.model_id ? String(body.model_id) : null });
      return sendJson(res, 200, r);
    }

    // ── 删除文档 ──
    if (p === '/api/rag-trial/delete' && req.method === 'POST') {
      const body = await json() ?? {};
      const { repo, branch, doc_path } = body;
      if (!repo || !branch || !doc_path) {
        return sendJson(res, 400, { error: { reason: 'repo/branch/doc_path required' } });
      }
      // PHASE0A 组合授权门（scope → 会话 repo）：越权 403，零删除副作用
      const sdDel = await authorizeRepo(env, { store, auth, actor, repo: String(repo), branch: String(branch),
        source: req.socket?.remoteAddress ?? null, kind: 'session' });
      if (sdDel.status === 403) return sendJson(res, sdDel.status, sdDel.body);
      return sendJson(res, 200, await store.deleteDocument({ repo, branch, doc_path }, { actor }));
    }

    // ── 查询（六状态） ──
    if (p === '/api/rag-trial/query' && req.method === 'POST') {
      const body = await json() ?? {};
      const q = String(body.q || '');
      const repo = String(body.repo || '');
      const branch = String(body.branch || '');
      if (!q || !repo || !branch) {
        return sendJson(res, 400, { error: { reason: 'q/repo/branch required' } });
      }
      // PHASE0A 组合授权门（scope → 会话 repo；默认拒绝；env 撤销即时生效；越权 403+有界脱敏审计）
      const sd = await authorizeRepo(env, { store, auth, actor, repo, branch,
        source: req.socket?.remoteAddress ?? null, kind: 'session' });
      if (sd.status === 403) return sendJson(res, sd.status, sd.body);
      const r = await store.search({
        q, repo, branch,
        k: Number(body.k || 5),
        modelId: body.model_id ? String(body.model_id) : null,
        actor,
      });
      return sendJson(res, 200, r);
    }

    // ── 指标 ──
    if (p === '/api/rag-trial/metrics' && req.method === 'GET') {
      const m = await store.metrics();
      return sendJson(res, 200, {
        ...m,
        in_memory_error_states: inMemory.error_states,
        note: 'error 态（PG 不可达）无法写 PG 日志，由进程内存计数补齐',
      });
    }

    // ── QA 评测（Recall@K） ──
    if (p === '/api/rag-trial/eval' && req.method === 'POST') {
      const body = await json(BODY_LIMIT) ?? {};
      const { repo, branch } = body;
      const qa = body.qa;
      if (!repo || !branch || !Array.isArray(qa) || !qa.length
          || qa.some((i) => !i?.q || !i?.expect_doc)) {
        return sendJson(res, 400, { error: { reason: 'repo/branch/qa[{q,expect_doc}] required' } });
      }
      // PHASE0A 组合授权门（scope → 会话 repo）：越权 403，零评测泄露
      // （evalQa detail 含逐条命中 doc_path——拒绝路径必须先于任何检索）
      const sdEval = await authorizeRepo(env, { store, auth, actor, repo: String(repo), branch: String(branch),
        source: req.socket?.remoteAddress ?? null, kind: 'session' });
      if (sdEval.status === 403) return sendJson(res, sdEval.status, sdEval.body);
      return sendJson(res, 200, await store.evalQa({
        qa, repo, branch, k: Number(body.k || 5),
        qaSet: String(body.qa_set || 'inline'), actor,
      }));
    }

    // ── 索引失效 / 回滚 ──
    if (p === '/api/rag-trial/index/invalidate' && req.method === 'POST') {
      const body = await json() ?? {};
      // PHASE0A 跨界门：模型现存文档仓库 ⊆ 会话授权面，否则全局索引操作越权拒绝
      const igInv = await indexBoundaryGuard(env, { store, auth, actor,
        modelId: body.model_id ? String(body.model_id) : null,
        source: req.socket?.remoteAddress ?? null });
      if (igInv.status === 403) return sendJson(res, igInv.status, igInv.body);
      return sendJson(res, 200, await store.invalidateIndex({
        modelId: body.model_id ? String(body.model_id) : null, actor,
      }));
    }
    if (p === '/api/rag-trial/index/rollback' && req.method === 'POST') {
      const body = await json() ?? {};
      const toVersion = Number(body.to_index_version);
      if (!Number.isInteger(toVersion) || toVersion < 1) {
        return sendJson(res, 400, { error: { reason: 'to_index_version (int>=1) required' } });
      }
      // PHASE0A 跨界门（同 invalidate）：模型现存文档仓库 ⊆ 会话授权面
      const igRb = await indexBoundaryGuard(env, { store, auth, actor,
        modelId: body.model_id ? String(body.model_id) : null,
        source: req.socket?.remoteAddress ?? null });
      if (igRb.status === 403) return sendJson(res, igRb.status, igRb.body);
      return sendJson(res, 200, await store.rollbackIndex({
        toVersion, modelId: body.model_id ? String(body.model_id) : null, actor,
      }));
    }

    // ── Review 联动（辅助证据 + 策略演示） ──
    if (p === '/api/rag-trial/review-aux' && req.method === 'POST') {
      const body = await json() ?? {};
      const { q, repo, branch, run_id } = body;
      if (!q || !repo || !branch) {
        return sendJson(res, 400, { error: { reason: 'q/repo/branch required' } });
      }
      // PHASE0A 组合授权门（scope → 会话 repo）：修复 review-aux 旁路（复核 CT-01）——
      // 越权 403 先于任何检索，响应不含 snippet/citation/doc_path
      const sdAux = await authorizeRepo(env, { store, auth, actor, repo: String(repo), branch: String(branch),
        source: req.socket?.remoteAddress ?? null, kind: 'session' });
      if (sdAux.status === 403) return sendJson(res, sdAux.status, sdAux.body);
      const r = await store.search({ q, repo, branch, k: Number(body.k || 5), actor });
      const aux = (r.results ?? []).map((h) => toAuxEvidence(h, { runId: run_id ?? null }));
      const runView = attachToRun(
        { run_id: run_id ?? 'ad-hoc', findings: [], tickets: [], gates: [] }, aux);
      return sendJson(res, 200, {
        service_state: r.service_state,
        run_view: runView,
        policy: canAutoPromote(aux),
        note: 'RAG 命中仅作 Review 辅助引用；findings/tickets/gates 未被触碰',
      });
    }
    if (p === '/api/rag-trial/policy-check' && req.method === 'POST') {
      const body = await json() ?? {};
      const evidence = body.evidence ?? [];
      return sendJson(res, 200, {
        auto_promote: canAutoPromote(evidence),
        as_finding: promotionRequest({ target: 'finding', evidence }),
        as_ticket: promotionRequest({ target: 'ticket', evidence }),
        as_gate: promotionRequest({ target: 'gate', evidence }),
        as_VERIFIED: promotionRequest({ target: 'VERIFIED', evidence }),
        fixer: fixerPatchInputs(evidence),
        verifier: Array.isArray(evidence)
          ? evidence.map((e) => verifierAccepts(e))
          : [verifierAccepts(evidence)],
      });
    }

    return sendJson(res, 404, { error: { reason: `unknown rag-trial path: ${p}` } });
  } catch (e) {
    if (e instanceof RagTrialError) {
      if (e.kind === 'pg_unavailable' || e.kind === 'pgvector_unavailable') errKindCount(e.kind);
      return sendJson(res, e.status, {
        service_state: e.kind === 'provider_unavailable' ? 'provider_unavailable' : 'error',
        error_kind: e.kind,
        error: e.message,
        in_memory_error_states: inMemory.error_states,
        note: 'ragtrial 显式失败——不伪装为空结果',
      });
    }
    // 停库期间 DNS/连接类错误（如 EAI_AGAIN/ENOTFOUND）同样归类 pg_unavailable
    const pgCodes = ['ECONNREFUSED', 'ETIMEDOUT', 'EAI_AGAIN', 'ENOTFOUND', '28P01', '3D000'];
    if (pgCodes.includes(e?.code)) {
      errKindCount('pg_unavailable');
      return sendJson(res, 503, {
        service_state: 'error', error_kind: 'pg_unavailable',
        error: String(e.message).slice(0, 120),
        in_memory_error_states: inMemory.error_states,
        note: 'PG 不可达——显式错误，不伪装为空结果',
      });
    }
    const msg = String(e?.message || e).slice(0, 200);
    errKindCount('internal');
    return sendJson(res, 500, {
      service_state: 'error', error_kind: 'internal', error: msg,
      in_memory_error_states: inMemory.error_states,
    });
  }
}

// ── A 链集成联调（feat/rag-integration，2026-09-27）──────────────────
// 供 /api/rag/org-search 的"内部 ragtrial 接线"调用（MERGEPILOT_RAG_TRIAL_A_CHAIN=ragtrial）：
// 同进程直查隔离栈索引，不经外部网络；语义与 /api/rag-trial/query 完全一致
// （六状态 + query_log + audit）；结果一律 reference_only 辅助引用。
export async function ragTrialInternalQuery(env = process.env, { q, repo, branch, k = 5, actor = 'a-chain-org-search' } = {}) {
  let store;
  try { store = await getStore(env); }
  catch (e) {
    const kind = e instanceof RagTrialError ? e.kind : 'pg_unavailable';
    errKindCount(kind);
    return { status: 503, body: { service_state: 'error', error_kind: kind } };
  }
  if (!store) return { status: 200, body: { service_state: 'backend_not_wired', results: [] } };
  // scope 门（内部通道与 HTTP 通道同权；默认拒绝，撤销即时生效）
  const check = checkScope(env, repo, branch);
  if (!check.ok) {
    await store.audit('QUERY_SCOPE_DENIED', actor, {
      repo, branch, detail: { reason: check.reason, channel: 'a-chain-internal', allowed_count: check.allowed_count ?? 0 },
    }).catch(() => {});
    return { status: 403, body: { error: { reason: check.reason }, results: [] } };
  }
  try {
    const r = await store.search({ q, repo, branch, k, actor });
    return { status: 200, body: r };
  } catch (e) {
    if (e instanceof RagTrialError) {
      if (e.kind === 'pg_unavailable' || e.kind === 'pgvector_unavailable') errKindCount(e.kind);
      return { status: 503, body: { service_state: 'error', error_kind: e.kind, error: String(e.message).slice(0, 120) } };
    }
    return { status: 503, body: { service_state: 'error', error_kind: 'internal', error: String(e?.message || e).slice(0, 120) } };
  }
}
