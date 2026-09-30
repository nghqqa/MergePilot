// console/backend/server.mjs — MergePilot 管理控制台（V0）
//
// 零第三方依赖的只读 HTTP 服务：
// - /api/* 从锁定的真实历史证据包（snapshot 模式）读取运行数据；
// - 其余 GET 服务于 frontend/dist（SPA）；
// - 默认只监听 127.0.0.1，不提供任何写操作接口。
//
// 启动：node console/backend/server.mjs
// 环境变量：CONSOLE_PORT（默认 4730）、CONSOLE_HOST（默认 127.0.0.1）、
//           CONSOLE_EVIDENCE_ROOT（默认 <repo>/evidence）

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { listRunPacks, safeResolve, listPackFiles, verifyPack, parseSha256Sums, looksTextual } from './lib/pack.mjs';
import { buildRunRecord, buildRunDetail } from './lib/runs.mjs';
import { login, logout, getSession, sessionBody, anonymousBody, tokenFromCookieHeader,
  repoAllowlist, sessionTtlMs, safeEqual } from './lib/session.mjs';
import { corePilotState, overviewState } from './lib/core-pilot.mjs';
import { fxvAttempts } from './lib/fxv/api.mjs';
import { fxvMetrics } from './lib/fxv/metrics.mjs';
import { parseAccessModel, authorize, denialAudit } from './lib/permissions.mjs';
import { cchainStatusObserved, verifyRunBindingAndAudit, rotateKeystore,
         cchainMetricsSnapshot, rememberStatusForMetrics } from './lib/cchain/wiring.mjs';
import { ragTrialApi, ragTrialInternalQuery } from './lib/ragtrial/api.mjs';
import { muApi, getMuStore } from './lib/multiuser/api.mjs';
import { createMuConsoleApi } from './lib/mu-console-api.mjs';

// 进程启动时刻（health.started_at 的唯一来源）。必须在模块加载时求值——
// 放进 apiHealth() 会变成"响应时刻"，容器 Up 时长与该字段即相互矛盾（2026-09-27 实测教训）。
const PROCESS_STARTED_AT = new Date().toISOString();

function readJsonBody(req, limit = 64 * 1024) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (c) => {
      size += c.length;
      if (size > limit) { reject(Object.assign(new Error('body too large'), { status: 400 })); return; }
      chunks.push(c);
    });
    req.on('end', () => {
      if (!chunks.length) return resolve(null);
      try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))); }
      catch { reject(Object.assign(new Error('invalid JSON body'), { status: 400 })); }
    });
    req.on('error', reject);
  });
}

function applyCookies(res, cookies) {
  const prev = res.getHeader('Set-Cookie');
  const all = []
    .concat(prev ? (Array.isArray(prev) ? prev : [prev]) : [])
    .concat(cookies || []);
  if (all.length) res.setHeader('Set-Cookie', all);
}

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_EVIDENCE_ROOT =
  process.env.CONSOLE_EVIDENCE_ROOT || path.resolve(__dirname, '..', '..', 'evidence');
const DEFAULT_DIST_DIR = path.resolve(__dirname, '..', 'frontend', 'dist');
const PORT = Number(process.env.CONSOLE_PORT || 4730);
const HOST = process.env.CONSOLE_HOST || '127.0.0.1';
const TEXT_VIEW_LIMIT = 512 * 1024;

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
  '.map': 'application/json',
  '.txt': 'text/plain; charset=utf-8',
};

function send(res, status, body, headers = {}) {
  const buf = typeof body === 'string' ? Buffer.from(body, 'utf8') : body;
  res.writeHead(status, { 'Content-Length': buf.length, ...headers });
  res.end(buf);
}

function sendJson(res, status, obj) {
  send(res, status, JSON.stringify(obj, null, 2), { 'Content-Type': 'application/json; charset=utf-8' });
}

// ── MU Console 数据适配层 ──
let _muApiInstance = null;
async function getMuConsoleApi() {
  if (process.env.MU_MODE !== 'multiuser') return null;
  if (!_muApiInstance) {
    // 创建独立 pool（mu store 的 pool 在闭包中不可外部访问）
    const pg = (await import('pg')).default;
    const pool = new pg.Pool({ connectionString: process.env.CONSOLE_PG_DSN, max: 2 });
    pool.on('error', () => {});
    _muApiInstance = createMuConsoleApi({ pool });
  }
  return _muApiInstance;
}

async function authGate(req) {
  if (process.env.MU_MODE === 'multiuser') {
    const store = await getMuStore(process.env).catch(() => null);
    if (store) {
      const { resolvePrincipal } = await import('./lib/principal.mjs');
      const principal = await resolvePrincipal(req, { muStore: store }).catch(() => null);
      if (principal?.authenticated) return { principal };
    }
    // mu store 不可用或 mu_session 无效 → 401（不回退 mp_session，安全边界）
    return { denied: 401 };
  }
  // legacy：原有 mp_session 行为（返回原始 session 对象——下游端点依赖其形状）
  const auth = getSession(tokenFromCookieHeader(req.headers.cookie));
  if (!auth) return { denied: 401 };
  // 返回原始 auth 对象（含 .user/.repos 等旧属性）+ MU 兼容标记
  return { principal: { ...auth, authenticated: true, authMode: 'legacy' }, legacyAuth: auth };
}

function sendError(res, status, message) {
  sendJson(res, status, { error: { code: status, message } });
}

export function createConsole({ evidenceRoot = DEFAULT_EVIDENCE_ROOT, distDir = DEFAULT_DIST_DIR } = {}) {
  const packDirOf = (packId) => {
    if (!/^[\w.-]+$/.test(packId)) {
      const err = new Error('bad pack id');
      err.status = 400;
      throw err;
    }
    const dir = path.join(evidenceRoot, packId);
    if (!fs.existsSync(dir) || !fs.statSync(dir).isDirectory()) {
      const err = new Error(`unknown run pack: ${packId}`);
      err.status = 404;
      throw err;
    }
    return dir;
  };

  const apiHealth = () => {
    const packs = listRunPacks(evidenceRoot);
    const withSums = packs.filter((p) => fs.existsSync(path.join(p.dir, 'SHA256SUMS'))).length;
    const liveConfigured = Boolean(process.env.CONSOLE_PG_DSN);
    // R4（OVERVIEW_REMEDIATION 二）：DSN 已配置 → 数据模式如实声明 live，
    // primary=contract_v2（前端据此前往 /api/pulls 等服务端 allowlist 过滤的实时端点）；
    // 未配置 → 维持 snapshot 声明（诚实降级，不假成功）。
    return {
      ok: true,
      service: 'mergepilot-console',
      version: '0.1.0',
      data_mode: liveConfigured ? 'live' : 'snapshot',
      data_mode_note: liveConfigured
        ? 'PG 实时（live）·隔离 staging 库：overview/pending/仓库/PR 详情/审计按会话 allowlist 实时读取；run 证据详情页仍为锁定快照只读'
        : '真实历史运行证据包（锁定只读），非实时数据；live 模式未接入',
      live: liveConfigured
        ? { configured: true, note: '核心控制面 API（overview/pulls/pending/tickets/evidence/audit）实时读 PG（会话 allowlist 过滤）' }
        : { configured: false, note: 'CONSOLE_PG_DSN 未配置 — 核心 API 返回 BACKEND_NOT_WIRED' },
      // 可信数据源配置：前端据此选择数据源（页面不做环境判断，sessionStorage/URL 无权改变）。
      // R4：live 已配置时 primary=contract_v2 并声明 allowlist 仓库清单（仓库页数据），
      // 页面不再回退到未过滤 snapshot 作为默认数据。
      sources: {
        primary: liveConfigured ? 'contract_v2' : 'snapshot',
        snapshot: { available: true, note: '锁定证据包（真实历史运行，只读；run 证据详情页使用）' },
        contract_v2: liveConfigured
          ? { available: true, reason: 'delivered_readonly_core', note: '核心控制面 API + 会话 + PR 详情（会话 allowlist 过滤）' }
          : { available: false, reason: 'pg_not_configured', note: 'CONSOLE_PG_DSN 未配置' },
      },
      declared_repos: liveConfigured ? repoAllowlist() : [],
      evidence_root: evidenceRoot,
      runs: packs.length,
      packs_with_sums: withSums,
      started_at: PROCESS_STARTED_AT,
    };
  };

  const apiRuns = (query) => {
    const packs = listRunPacks(evidenceRoot);
    let items = packs.map((p) => buildRunRecord(p.pack_id, p.dir));
    const { repo, pr, execution, verdict, publish, q } = query;
    if (repo) items = items.filter((r) => (r.repo ?? '').toLowerCase().includes(repo.toLowerCase()));
    if (pr) items = items.filter((r) => String(r.pr_number ?? '') === String(pr));
    if (execution) items = items.filter((r) => (r.execution.status ?? '').toUpperCase() === execution.toUpperCase());
    if (verdict) items = items.filter((r) => (r.review.verdict ?? '').toUpperCase() === verdict.toUpperCase());
    if (publish) items = items.filter((r) => (r.publish.status ?? '') === publish);
    if (q) {
      const needle = q.toLowerCase();
      items = items.filter((r) =>
        [r.pack_id, r.run_id, r.repo, r.head_sha, String(r.pr_number ?? '')]
          .some((v) => (v ?? '').toLowerCase().includes(needle)));
    }
    items.sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? ''));
    const total = items.length;
    const limit = Math.min(Math.max(Number(query.limit) || 50, 1), 200);
    const offset = Math.max(Number(query.offset) || 0, 0);
    return {
      data_mode: 'snapshot',
      generated_at: new Date().toISOString(),
      total,
      limit,
      offset,
      items: items.slice(offset, offset + limit),
    };
  };

  const apiEvidenceContent = async (packId, relPath, res, { download = false } = {}) => {
    const packDir = packDirOf(packId);
    const abs = safeResolve(packDir, relPath);
    let stat;
    try {
      stat = fs.statSync(abs);
    } catch {
      return sendError(res, 404, `file not found in pack: ${relPath}`);
    }
    if (!stat.isFile()) return sendError(res, 400, 'not a file');

    const sums = parseSha256Sums(packDir);
    const sumsStatus = !sums ? 'no_sums' : sums.has(relPath) ? 'listed' : 'unlisted';

    if (download) {
      const buf = await fs.promises.readFile(abs);
      return send(res, 200, buf, {
        'Content-Type': 'application/octet-stream',
        'Content-Disposition': `attachment; filename="${path.basename(abs).replace(/["\r\n]/g, '')}"`,
        'X-Pack-Id': packId,
        'X-Sums-Status': sumsStatus,
      });
    }

    const handle = await fs.promises.open(abs, 'r');
    try {
      const sample = Buffer.alloc(Math.min(TEXT_VIEW_LIMIT, stat.size));
      await handle.read(sample, 0, sample.length, 0);
      const truncated = stat.size > TEXT_VIEW_LIMIT;
      if (!looksTextual(sample)) {
        return sendJson(res, 200, {
          path: relPath,
          bytes: stat.size,
          encoding: 'binary',
          truncated: false,
          sums_status: sumsStatus,
          note: '二进制文件，请使用下载',
        });
      }
      return sendJson(res, 200, {
        path: relPath,
        bytes: stat.size,
        encoding: 'utf-8',
        truncated,
        text: sample.toString('utf8'),
        sums_status: sumsStatus,
      });
    } finally {
      await handle.close();
    }
  };

  const serveStatic = (p, res) => {
    if (!fs.existsSync(distDir)) {
      return send(res, 503, `<!doctype html><meta charset="utf-8"><title>MergePilot Console</title>
<body style="font-family:system-ui;padding:2rem">
<h1>前端未构建</h1>
<p>请先构建控制台前端：<code>cd console/frontend && npm install && npm run build</code></p>
<p>API 仍然可用：<a href="/api/health">/api/health</a></p></body>`, { 'Content-Type': 'text/html; charset=utf-8' });
    }
    let abs = p === '/' ? path.join(distDir, 'index.html') : path.resolve(distDir, '.' + p);
    const rootAbs = path.resolve(distDir);
    if (abs !== path.join(rootAbs, 'index.html') && !abs.startsWith(rootAbs + path.sep)) {
      return sendError(res, 400, 'bad path');
    }
    try {
      if (!fs.statSync(abs).isFile()) throw new Error('not file');
    } catch {
      abs = path.join(distDir, 'index.html'); // SPA fallback
    }
    const ext = path.extname(abs).toLowerCase();
    const buf = fs.readFileSync(abs);
    // HTML 不缓存（bundle 名变了必须拿到新 index.html）；hash 命名的静态资产可长缓存
    const isHtml = ext === '.html' || p === '/';
    return send(res, 200, buf, { 'Content-Type': MIME[ext] ?? 'application/octet-stream',
      'Cache-Control': isHtml ? 'no-cache, no-store, must-revalidate' : 'public, max-age=31536000, immutable' });
  };

  const route = async (req, res) => {
    const url = new URL(req.url, `http://${req.headers.host ?? 'localhost'}`);
    const p = url.pathname;
    const q = Object.fromEntries(url.searchParams.entries());

    if (!p.startsWith('/api/')) return serveStatic(p, res);

    // ── 契约 v2 会话三件套 + 核心控制面五 API（CANONICAL_CONSOLE_PROMOTION 迁移）──
    if (p === '/api/auth/session') {
      const legacyAuth = getSession(tokenFromCookieHeader(req.headers.cookie));
      if (legacyAuth) return sendJson(res, 200, sessionBody(legacyAuth));
      // MU 桥接：MU_MODE=multiuser 时检查 mu_session
      if (process.env.MU_MODE === 'multiuser') {
        try {
          const store = await getMuStore(process.env);
          if (store) {
            const { resolvePrincipal } = await import('./lib/principal.mjs');
            const principal = await resolvePrincipal(req, { muStore: store });
            if (principal.authenticated) {
              return sendJson(res, 200, {
                user: { name: principal.username, github_login: principal.username,
                  display_name: principal.username },
                repos: repoAllowlist(),
                expires_at: principal.muSession?.expires_at ?? null,
                session_source: 'mu_session', role: principal.roles?.[0] ?? null });
            }
          }
        } catch { /* mu store 不可用走 401 */ }
      }
      return sendJson(res, 401, anonymousBody());
    }
    if (p === '/api/auth/login' && req.method === 'POST') {
      const body = await readJsonBody(req);
      const r = login(body?.user, body?.password);
      if (!r.ok) return sendJson(res, r.status, r.code === 'auth_unavailable'
        ? r.error : anonymousBody(r.error?.reason));
      applyCookies(res, r.setCookie);
      return sendJson(res, 200, { user: { name: String(body.user ?? '') }, repos: repoAllowlist(),
        expires_at: new Date(Date.now() + sessionTtlMs()).toISOString() });
    }
    if (p === '/api/auth/logout' && req.method === 'POST') {
      const token = tokenFromCookieHeader(req.headers.cookie);
      const auth = getSession(token);
      const r = logout(token, auth ? req.headers['x-csrf-token'] : undefined);
      if (!r.ok) return sendJson(res, r.status, { error: { reason: r.code } });
      applyCookies(res, r.setCookie);
      // MU 会话桥接登出：同时清除 mu_session（MU_MODE=multiuser 时两套会话同步退出）
      const muCookies = ['mu_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0',
        'mp_csrf=; Path=/; SameSite=Lax; Max-Age=0',
        'mu_oauth_corr=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0'];
      const prevCookies = res.getHeader('Set-Cookie');
      const allCookies = (Array.isArray(prevCookies) ? prevCookies : prevCookies ? [prevCookies] : []).concat(muCookies);
      res.setHeader('Set-Cookie', allCookies);
      return sendJson(res, 200, { ok: true });
    }
    // ── A 链组织知识检索（受控代理；feature flag 显式启用，仅隔离 staging）──
    if (p === '/api/rag/org-search' && req.method === 'GET') {
      const gate = await authGate(req);
      if (gate.denied) return sendJson(res, gate.denied, anonymousBody());
      const auth = gate.principal.legacyAuth ?? gate.principal;
      if (process.env.MERGEPILOT_ORG_RAG_A_CHAIN !== '1') {
        return sendJson(res, 200, { service_state: 'a_chain_disabled',
          note: 'A 链未启用（feature flag 关闭）——不伪装检索', source: 'ORG_RAG' });
      }
      const query = String(q.q || '');
      const k = Math.min(Number(q.k || 5), 20);
      // RAG 本地试验内部接线（feat/rag-integration 联调）：flag=ragtrial 时同进程直查
      // 隔离栈索引（六状态语义 + query_log/audit 与 /api/rag-trial/query 完全一致）。
      // 结果一律 reference_only 辅助引用——不构成 finding/gate/ticket/VERIFIED/fixer 输入。
      // 命中态词汇契约：A 链端点与 ragtrial 内部统一发射 'hit'（不再改写为 'ok'，
      // 上游 org-rag 的 'ok' 在边界归一为 'hit'；契约文档 distribution/docs/API-CONTRACTS.md 同步）。
      if (process.env.MERGEPILOT_RAG_TRIAL_A_CHAIN === 'ragtrial') {
        const r = await ragTrialInternalQuery(process.env, {
          q: query, k, actor: auth.user,
          repo: process.env.RAGTRIAL_A_CHAIN_REPO || 'nghqqa/mergepilot',
          branch: process.env.RAGTRIAL_A_CHAIN_BRANCH || 'feat/local-rag-trial',
        });
        const b = r.body;
        if (r.status !== 200 || b.service_state === 'error') {
          res.setHeader('x-rag-service-state', 'degraded');
          return sendJson(res, 503, { service_state: 'degraded', source: 'RAG_TRIAL',
            degraded_reason: b.error_kind || 'internal', results: [],
            note: 'ragtrial 检索不可用——显式降级，不伪装为空成功' });
        }
        if (b.service_state !== 'hit') {
          return sendJson(res, 200, { service_state: b.service_state, source: 'RAG_TRIAL',
            results: [], model: b.model_id ? { model_id: b.model_id, model_digest: b.model_digest, index_version: b.index_version } : undefined,
            note: b.note || `ragtrial 状态=${b.service_state}（如实返回，不伪装命中）` });
        }
        return sendJson(res, 200, {
          service_state: b.service_state, source: 'RAG_TRIAL', knowledge_type: 'rag_trial_reference',
          model: { model_id: b.model_id, model_digest: b.model_digest, index_version: b.index_version },
          latency_ms: b.latency_ms,
          results: (b.results || []).map((h) => ({ score: h.score, snippet: h.snippet, citation: h.citation, reference_only: true })),
          usage_note: 'RAG trial 辅助引用（reference only）——不构成 finding/gate/ticket/VERIFIED/fixer 输入；Verifier 只接受独立测试证据',
        });
      }
      const base = process.env.ORG_RAG_LIVE_URL || 'http://host.docker.internal:48210';
      try {
        const r = await fetch(`${base}/api/rag/search?q=${encodeURIComponent(query)}&k=${k}`);
        const body = await r.json().catch(() => ({}));
        if (r.status !== 200 || body.service_state === 'degraded') {
          res.setHeader('x-rag-service-state', 'degraded');
          return sendJson(res, 503, { service_state: 'degraded', source: 'ORG_RAG',
            degraded_reason: body.degraded_reason || 'upstream_non_200',
            http_status: r.status, results: [],
            note: '组织知识检索暂不可用——显式降级，不伪装为空成功' });
        }
        return sendJson(res, 200, {
          ...body,
          // 词汇契约归一（边界转换）：上游 org-rag 的 'ok' 在 A 链端点统一为 'hit'
          service_state: body.service_state === 'ok' ? 'hit' : body.service_state,
          source: 'ORG_RAG',
          knowledge_type: 'org_knowledge',
          usage_note: '组织规范仅作参考（reference only）——不构成 finding/gate/ticket/VERIFIED 输入',
        });
      } catch (e) {
        res.setHeader('x-rag-service-state', 'degraded');
        return sendJson(res, 503, { service_state: 'degraded', source: 'ORG_RAG',
          degraded_reason: 'service_unreachable', results: [],
          error: String(e.cause?.code || e.message).slice(0, 80),
          note: 'rag-live 不可达——显式降级' });
      }
    }

    // ── C 链（cchain）状态/验签/轮换 —— B 轨接线（feat/core-b-parallel）──
    // 状态与 metrics：需会话（读观测面）；验签：机器对机器 HMAC（无会话，替代凭证）；
    // 轮换：会话 + CSRF + admin 角色（授权操作）。全部真实状态，BLOCKED 即 BLOCKED。
    if (p === '/api/cchain/status' && req.method === 'GET') {
      const gate = await authGate(req);
      if (gate.denied) return sendJson(res, gate.denied, anonymousBody());
      const auth = gate.principal.legacyAuth ?? gate.principal;
      const status = await cchainStatusObserved(process.env, process.env.CONSOLE_PG_DSN);
      rememberStatusForMetrics(status);
      return sendJson(res, 200, { ...status,
        enforce: { flag: process.env.MERGEPILOT_CCHAIN_ENFORCE === '1',
          note: 'enforce=on 时 FXV run 启动被 C 链 READY 门禁拦截（默认 off）' } });
    }
    if (p === '/api/cchain/metrics' && req.method === 'GET') {
      const gate = await authGate(req);
      if (gate.denied) return sendJson(res, gate.denied, anonymousBody());
      const auth = gate.principal.legacyAuth ?? gate.principal;
      return sendJson(res, 200, cchainMetricsSnapshot(process.env));
    }
    if (p === '/api/cchain/run-bindings/verify' && req.method === 'POST') {
      // 机器端点：RUN_BINDING_AUTH 入站验签（HMAC full-sha256 + nonce 防重放 + 时间窗）。
      // 审计失败不吞：audit_written=false 如实返回（actor=run-binding:<run_id>）。
      // M-2：拒绝侧审计按来源+窗口封顶（防未认证刷审计）；source=连接来源地址。
      const body = await readJsonBody(req);
      const r = await verifyRunBindingAndAudit(process.env, process.env.CONSOLE_PG_DSN, body ?? {},
        { source: req.socket?.remoteAddress ?? 'unknown' });
      return sendJson(res, r.status, r.body);
    }
    if (p === '/api/cchain/keystore/rotate' && req.method === 'POST') {
      const gate = await authGate(req);
      if (gate.denied) return sendJson(res, gate.denied, anonymousBody());
      const auth = gate.principal.legacyAuth ?? gate.principal;
      // 契约 §0.1：副作用方法必须携带 X-CSRF-Token
      // L-2：与 logout 一致的 timing-safe 比较（长度不等直接拒绝）
      if (!auth.csrf || !safeEqual(String(req.headers['x-csrf-token'] ?? ''), auth.csrf)) {
        return sendJson(res, 403, { error: { reason: 'csrf_required' } });
      }
      const model = parseAccessModel();
      const decision = authorize(model, auth.user, { action: 'admin' });
      if (!decision.ok) {
        return sendJson(res, 403, { error: { reason: decision.reason, detail: decision.detail },
          denial: denialAudit(auth.user, decision, { action: 'admin' }) });
      }
      const body = await readJsonBody(req);
      const r = await rotateKeystore(process.env, process.env.CONSOLE_PG_DSN,
        { operator: auth.user, grace_ms: Number(body?.grace_ms || 0) });
      return sendJson(res, r.status, r.body);
    }

    if (p === '/api/overview' && req.method === 'GET') {
      const gate = await authGate(req);
      if (gate.denied) return sendJson(res, gate.denied, anonymousBody());
      if (gate.principal.authMode === 'multiuser') {
        const api = await getMuConsoleApi();
        if (api) return sendJson(res, 200, await api.overview(gate.principal.tenantId));
      }
      const auth = gate.principal.legacyAuth ?? gate.principal;
      const ov = await overviewState(auth.repos);
      return sendJson(res, 200, ov);
    }
    if (p === '/api/fxv/metrics' && req.method === 'GET') {
      const gate = await authGate(req);
      if (gate.denied) return sendJson(res, gate.denied, anonymousBody());
      const auth = gate.principal.legacyAuth ?? gate.principal;
      return sendJson(res, 200, await fxvMetrics(process.env.CONSOLE_PG_DSN));
    }
    if (p === '/api/fxv/attempts' && req.method === 'GET') {
      const gate = await authGate(req);
      if (gate.denied) return sendJson(res, gate.denied, anonymousBody());
      const auth = gate.principal.legacyAuth ?? gate.principal;
      const r = await fxvAttempts(process.env.CONSOLE_PG_DSN, { limit: 50 });
      const allow = new Set(auth.repos);
      return sendJson(res, 200, { ...r, attempts: r.attempts.filter((a) => allow.has(a.repo)) });
    }
    if (['/api/pulls', '/api/pending', '/api/tickets', '/api/evidence', '/api/audit'].includes(p) && req.method === 'GET') {
      const gate = await authGate(req);
      if (gate.denied) return sendJson(res, gate.denied, anonymousBody());
      if (gate.principal.authMode === 'multiuser') {
        const muApi2 = await getMuConsoleApi();
        if (muApi2) {
          const tid = gate.principal.tenantId;
          if (p === '/api/pulls') return sendJson(res, 200, { pulls: await muApi2.pulls(tid), source: 'MU_CANONICAL_LIVE' });
          if (p === '/api/pending') return sendJson(res, 200, { pending: await muApi2.pending(tid), source: 'MU_CANONICAL_LIVE' });
          if (p === '/api/tickets') return sendJson(res, 200, { tickets: [], source: 'MU_CANONICAL_LIVE' });
          if (p === '/api/evidence') return sendJson(res, 200, { evidence: [], source: 'MU_CANONICAL_LIVE' });
          if (p === '/api/audit') return sendJson(res, 200, { audit: await muApi2.audit(tid), source: 'MU_CANONICAL_LIVE' });
        }
      }
      const auth = gate.principal.legacyAuth ?? gate.principal;
      if (p === '/api/pulls' && q.repo && !auth.repos.includes(q.repo)) {
        return sendJson(res, 403, { error: { reason: 'repo_not_in_allowlist', repo: q.repo } });
      }
      const state = await corePilotState();
      const allow = new Set(auth.repos);
      const withErr = { source: state.source, ...(state.error ? { error: state.error } : {}) };
      if (p === '/api/pulls') {
        const rows = state.pulls.filter((x) => allow.has(x.repo) && (!q.repo || x.repo === q.repo));
        return sendJson(res, 200, { pulls: rows, ...withErr });
      }
      if (p === '/api/pending') {
        return sendJson(res, 200, { pending: state.pending.filter((x) => allow.has(x.repo)), ...withErr });
      }
      if (p === '/api/tickets') {
        return sendJson(res, 200, { tickets: state.tickets.filter((x) => allow.has(x.repo)), ...withErr });
      }
      if (p === '/api/evidence') {
        return sendJson(res, 200, { evidence: state.evidence.filter((x) => !x.repo || allow.has(x.repo)), ...withErr });
      }
      // /api/audit：snapshot 审计语义保持（本控制台无 replay 审计聚合），gate 决策
      // 为会话域数据——按 allowlist 过滤 decision.repo 已知行
      const gates = (state.gate_decisions || []).filter((g) => {
        const repo = g.decision && g.decision.repo;
        return !repo || allow.has(repo);
      });
      return sendJson(res, 200, { gate_decisions: gates, core_source: state.source,
        ...(state.error ? { error: state.error } : {}) });
    }

    if (p === '/api/health') return sendJson(res, 200, apiHealth());

    // R4（OVERVIEW_REMEDIATION 一）：snapshot 运行查询按会话边界过滤。
    // 已认证 → 仅返回 allowlist 内仓库的记录（repo 未知的 pack 一并隐藏，不泄露存在性）；
    // 未认证 → 维持登录页明示的只读演示语义（本地历史快照，非授权范围数据）。
    // 实时数据查询一律走 /api/overview、/api/pulls（服务端 allowlist 强制）。
    const runsGate = await authGate(req);
    const runsScopeAuth = runsGate.principal?.legacyAuth ?? (runsGate.principal?.authenticated ? runsGate.principal : null);
    const runsScope = runsScopeAuth ? new Set(runsScopeAuth.repos) : null;
    const runsRepoAllowed = (repo) => !runsScope || (repo ? runsScope.has(repo) : false);

    if (p === '/api/runs' && process.env.MU_MODE === 'multiuser') {
      const gate = await authGate(req);
      if (gate.denied) return sendJson(res, gate.denied, anonymousBody());
      const api = await getMuConsoleApi();
      const runs = api ? await api.runs(gate.principal.tenantId) : [];
      return sendJson(res, 200, { runs });
    }
    if (p === '/api/runs') {
      const body = apiRuns(q);
      const items = (body.items ?? []).filter((r) => runsRepoAllowed(r.repo ?? null));
      return sendJson(res, 200, { ...body, items,
        scope: runsScope ? 'session_allowlist' : 'demo_snapshot' });
    }

    const runMatch = p.match(/^\/api\/runs\/([\w.-]+)(?:\/(.*))?$/);
    if (runMatch) {
      const [, packId, sub] = runMatch;
      if (!sub) {
        const detail = buildRunDetail(packId, packDirOf(packId));
        if (!runsRepoAllowed(detail?.run?.repo ?? detail?.repo ?? null)) {
          // 不泄露存在性：越权 pack 对已认证会话同样返回 404
          return sendError(res, 404, `unknown run pack: ${packId}`);
        }
        return sendJson(res, 200, detail);
      }
      {
        // 证据子资源与 pack 同边界：先按 pack 记录的 repo 校验
        const rec = buildRunRecord(packId, packDirOf(packId));
        if (!runsRepoAllowed(rec?.repo ?? null)) {
          return sendError(res, 404, `unknown run pack: ${packId}`);
        }
      }
      if (sub === 'evidence') {
        const dir = packDirOf(packId);
        const sums = parseSha256Sums(dir);
        const files = listPackFiles(dir).map((f) => ({
          ...f,
          sums_status: !sums ? 'no_sums' : sums.has(f.path) ? 'listed' : 'unlisted',
        }));
        return sendJson(res, 200, { data_mode: 'snapshot', pack_id: packId, items: files });
      }
      if (sub === 'integrity') return sendJson(res, 200, await verifyPack(packId, packDirOf(packId)));
      if (sub === 'evidence/content') return apiEvidenceContent(packId, q.path, res);
      if (sub === 'evidence/download') return apiEvidenceContent(packId, q.path, res, { download: true });
      return sendError(res, 404, `unknown api path: ${p}`);
    }

    // R4（OVERVIEW_REMEDIATION 三）：PR 详情正式端点（与 /api/overview 同一 live 数据源，
    // 同一会话 allowlist 边界）。repo 寻址走查询参数（契约 §0.5 同形）。
    const pullMatch = p.match(/^\/api\/pulls\/(\d+)$/);
    if (pullMatch && req.method === 'GET') {
      const gate = await authGate(req);
      if (gate.denied) return sendJson(res, gate.denied, anonymousBody());
      const auth = gate.principal.legacyAuth ?? gate.principal;
      const prNumber = Number(pullMatch[1]);
      const repo = q.repo;
      if (!repo || !auth.repos.includes(repo)) {
        return sendJson(res, 403, { error: { reason: 'repo_not_in_allowlist', repo: repo ?? null } });
      }
      const ov = await overviewState(auth.repos);
      const rows = (ov.prs || []).filter((r) => r.repo === repo && r.pr_number === prNumber);
      if (rows.length === 0) {
        return sendJson(res, 404, { error: { reason: 'no_live_record', repo, pr_number: prNumber } });
      }
      // 最新 run 为当前结论（overview 已按 updated_at 降序）；其余为历史行
      const [cur, ...history] = rows;
      const allow = new Set(auth.repos);
      // receipts / gate audit（同数据源；allowlist 内 + 本 PR 的 run 集）
      const runIds = new Set(rows.map((r) => r.run_id));
      const st = await corePilotState();
      const receipts = (st.evidence || []).filter(
        (r) => runIds.has(r.run_id) && r.repo && allow.has(r.repo));
      const gateAudit = (st.gate_decisions || []).filter((g) => {
        const grepo = g.decision && g.decision.repo;
        return runIds.has(g.run_id) && grepo && allow.has(grepo);
      });
      return sendJson(res, 200, {
        repo, pr_number: prNumber,
        title: null,
        // GitHub 当前 head 权威未接入——如实 null，不以最近审查 head 冒充
        current_head_sha: null,
        stage: cur.stage, stage_source: cur.stage_source,
        head_sha: cur.head_sha, run_id: cur.run_id, updated_at: cur.updated_at,
        runs: rows.map((r) => ({
          run_id: r.run_id, created_at: r.updated_at,
          class: null, exec_seq: null, mode: null, status: null,
          stage: r.stage, stage_source: r.stage_source,
          outcome: r.stage, head_sha: r.head_sha,
          stale: r.head_sha !== cur.head_sha,
        })),
        receipts: {
          total: receipts.length,
          ok: receipts.filter((r) => r.status === 'OK' && (!r.integrity || r.integrity === 'OK')).length,
          integrity_conflicts: receipts.filter((r) => r.integrity && r.integrity !== 'OK').length,
        },
        gate_audit: gateAudit.map((g) => ({
          run_id: g.run_id, decision: g.decision, created_at: g.created_at,
        })),
        has_pending_tickets: false,
        merge_panel: { enabled: false, reasons: ['merge_disabled'], github_url: `https://github.com/${repo}/pull/${prNumber}` },
        source: ov.source,
      });
    }

    // ── Developer Edition 多用户面（MU；MU_MODE=multiuser 启用，默认 legacy 不生效）──
    if (p.startsWith('/api/mu/')) {
      return muApi(req, res, {
        p, q,
        sendJson,
        readJsonBody,
        requireSession: async () => getSession(tokenFromCookieHeader(req.headers.cookie)),
      });
    }

    // ── RAG 本地试验（LOCAL_RAG_TRIAL；与 A 链 /api/rag/org-search、C 链 /api/cchain/* 并行且隔离）──
    if (p.startsWith('/api/rag-trial/')) {
      return ragTrialApi(req, res, {
        p, q,
        sendJson,
        readJsonBody,
        // Dogfooding P1 修复：legacy 会话优先（非 multiuser 零回归），MU 会话回退
        // （multiuser 模式下 rag-trial 端点可达——逐请求 DB 解析+角色/仓库服务端校验）
        requireSession: async () => {
          const legacy = getSession(tokenFromCookieHeader(req.headers.cookie));
          if (legacy) return legacy;
          const { resolveMuAuthForRag } = await import('./lib/ragtrial/muAuthBridge.mjs');
          return resolveMuAuthForRag(req, process.env);
        },
      });
    }

    return sendError(res, 404, `unknown api path: ${p}`);
  };

  const server = http.createServer((req, res) => {
    Promise.resolve(route(req, res)).catch((e) => {
      try {
        sendError(res, e.status ?? 500, e.message ?? String(e));
      } catch { /* headers sent — nothing more we can do */ }
    });
  });

  return { server, route, apiHealth, apiRuns, evidenceRoot };
}

const isMain = process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href;
if (isMain) {
  const { server } = createConsole();
  server.listen(PORT, HOST, () => {
    console.log(`[console] MergePilot 管理控制台 V0`);
    console.log(`[console] 数据模式: snapshot（真实历史证据包，只读）`);
    console.log(`[console] evidence root: ${DEFAULT_EVIDENCE_ROOT}`);
    console.log(`[console] listening: http://${HOST}:${PORT} （仅本地回环）`);
  });
}
