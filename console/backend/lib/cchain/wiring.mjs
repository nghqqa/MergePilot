// cchain/wiring.mjs — C 链接线层（feat/core-b-parallel，2026-09-26 B 轨）。
// 职责：把 lib/cchain/index.mjs 的三个真实接口接进 console 的
// API / metrics / audit / FXV run 启动路径。
// 原则延续：缺真实依赖 → 状态 BLOCKED + 具体阻塞条件，绝不伪造 READY；
// 审计写入 fxv.audit_events（attempt_id 命名空间 'cchain'，actor 永远非空）；
// 审计失败不静默：调用方拿到 audit_written=false 如实上报。

import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { createRequire } from 'node:module';
import { cchainStatus, createRunBindingAuth, runBindingAuthStatus } from './index.mjs';

// pg 解析：容器/镜像内走标准 node_modules；host 测试进程回退到 test/support
// 安装的 dev-only pg（与 fxv-pg.integration.mjs 的 createRequire 锚定惯例一致）。
let pgMod = undefined;
async function loadPg() {
  if (pgMod !== undefined) return pgMod;
  try { pgMod = await import('pg'); return pgMod; } catch { /* fall through */ }
  try {
    pgMod = createRequire(new URL('../../test/support/noop.js', import.meta.url))('pg');
  } catch { pgMod = null; }
  return pgMod;
}

// ── 审计（复用 fxv.audit_events；fxv 命名空间，无 schema 变更）──────────
// best-effort：PG 不可达时返回 {written:false, error}，调用方必须透传诚实标志。
export async function auditEvent(dsn, e) {
  if (!e?.actor) throw new Error('auditEvent: actor required (audit_events.actor NOT NULL)');
  if (!dsn) return { written: false, error: 'CONSOLE_PG_DSN 未配置（审计无落点）' };
  const pg = await loadPg();
  if (!pg) return { written: false, error: 'pg module unavailable' };
  const Client = pg.default?.Client ?? pg.Client;
  const client = new Client({ connectionString: dsn, connectionTimeoutMillis: 3000 });
  try {
    await client.connect();
    await client.query(
      `INSERT INTO fxv.audit_events (attempt_id, kind, from_state, to_state, actor, reason, meta)
       VALUES ($1,$2,$3,$4,$5,$6,$7)`,
      [e.attempt_id ?? 'cchain', e.kind, e.from_state ?? null, e.to_state ?? null,
       e.actor, e.reason ?? null, JSON.stringify(e.meta ?? {})]);
    return { written: true };
  } catch (err) {
    return { written: false, error: String(err?.message || err).slice(0, 200) };
  } finally { await client.end().catch(() => {}); }
}

// ── 状态观测（带变化审计：overall 变化时写一条 CCHAIN_STATE_CHANGED）────
let lastOverall = null;
export function resetCchainObserver() { lastOverall = null; } // 仅供测试隔离
export function resetDenyAuditLimiter() { denyAuditBySource.clear(); } // 仅供测试隔离

export async function cchainStatusObserved(env = process.env, dsn = process.env.CONSOLE_PG_DSN) {
  const status = await cchainStatus(env);
  let audit = null;
  if (status.overall !== lastOverall) {
    audit = await auditEvent(dsn, {
      kind: 'CCHAIN_STATE_CHANGED', actor: 'system:cchain',
      reason: `overall ${lastOverall ?? '(首次观测)'} → ${status.overall}`,
      meta: { blocked_conditions: status.blocked_conditions },
    });
    lastOverall = status.overall;
  }
  return { ...status, audit_note: audit ? audit : { written: false, note: '状态未变化，未重复审计' } };
}

// ── RUN_BINDING_AUTH 入站验签（机器对机器：HMAC 替代会话）──────────────
// 计数器（进程内存，重启归零——诚实语义）。
const counters = { run_binding_verify_ok: 0, run_binding_verify_denied: 0,
  run_binding_verify_denied_suppressed: 0, key_rotations: 0 };
export function cchainCounters() { return { ...counters }; }

// M-2（2026-09-27 加固波）：拒绝侧审计防膨胀。verify 端点未认证即可调用，
// 若每次拒绝都落一行审计，单一来源可无限制造 audit 行（表膨胀/存储放大）。
// 策略：按来源（remoteAddress）+ 滚动窗口对"拒绝审计"封顶（默认 30 条/10min）；
// 超限后拒绝结果照常返回（语义不变），仅不再写审计行并在响应/metrics 中标注
// suppressed。成功验签不受限（能通过 HMAC 的必持有效密钥，不可伪造灌入）。
// env：MERGEPILOT_VERIFY_AUDIT_DENY_CAP（默认 30，<=0 显式关闭封顶）、
//      MERGEPILOT_VERIFY_AUDIT_WINDOW_MS（默认 600000）。
const denyAuditBySource = new Map(); // source -> { window_start, count }
function denyAuditAllowed(env, source) {
  const cap = Number(env.MERGEPILOT_VERIFY_AUDIT_DENY_CAP ?? 30);
  const win = Number(env.MERGEPILOT_VERIFY_AUDIT_WINDOW_MS ?? 600_000);
  if (!(cap > 0)) return true;
  const now = Date.now();
  if (denyAuditBySource.size > 10_000) { // 来源表自身防膨胀：清掉窗口外条目
    for (const [k, st] of denyAuditBySource) if (now - st.window_start >= win) denyAuditBySource.delete(k);
  }
  const key = source ?? 'unknown';
  let st = denyAuditBySource.get(key);
  if (!st || now - st.window_start >= win) { st = { window_start: now, count: 0 }; denyAuditBySource.set(key, st); }
  st.count += 1;
  return st.count <= cap;
}

let verifierSingleton = null; let verifierDir = undefined;
function verifier(env) {
  // 生产：keystore 目录固定，单例持有防重放 Map；目录被运维切换时重建
  //（旧 nonce 记录随之丢弃——单实例语义，见 index.mjs 注释）。
  const dir = env.MERGEPILOT_RUN_BINDING_KEYSTORE ?? null;
  if (env === process.env) {
    if (!verifierSingleton || verifierDir !== dir) {
      verifierSingleton = createRunBindingAuth(env);
      verifierDir = dir;
    }
    return verifierSingleton;
  }
  return createRunBindingAuth(env); // 测试注入的隔离 env：每次独立实例
}

const DENY_STATUS = { BAD_REQUEST: 400 };

export async function verifyRunBindingAndAudit(env = process.env, dsn = process.env.CONSOLE_PG_DSN, payload = {}, { source } = {}) {
  const r = verifier(env).verify({
    run_id: payload.run_id, nonce: payload.nonce,
    timestamp: payload.timestamp, signature: payload.signature,
  });
  if (r.ok) counters.run_binding_verify_ok += 1; else counters.run_binding_verify_denied += 1;
  const suppress = !r.ok && !denyAuditAllowed(env, source);
  if (suppress) counters.run_binding_verify_denied_suppressed += 1;
  const audit = suppress
    ? { written: false, suppressed: true, note: 'deny-audit cap（同源窗口内拒绝审计已封顶）' }
    : await auditEvent(dsn, {
        kind: r.ok ? 'RUN_BINDING_VERIFY_OK' : 'RUN_BINDING_VERIFY_DENIED',
        actor: `run-binding:${String(payload.run_id ?? 'unknown').slice(0, 64)}`,
        reason: r.ok ? 'verified' : r.reason,
        meta: { nonce_prefix: String(payload.nonce ?? '').slice(0, 8), ...(source ? { source } : {}) },
      });
  const body = { ok: r.ok, ...(r.ok ? {} : { reason: r.reason, ...(r.detail ? { detail: r.detail } : {}) }), audit_written: audit.written, ...(audit.suppressed ? { audit_suppressed: true } : {}), ...(audit.error ? { audit_error: audit.error } : {}) };
  return { status: r.ok ? 200 : (DENY_STATUS[r.reason] ?? 401), body };
}

// ── 密钥轮换（admin 会话操作；secret 只写入 keystore 文件，绝不回显）──
export async function rotateKeystore(env = process.env, dsn = process.env.CONSOLE_PG_DSN, { operator, grace_ms = 0 } = {}) {
  const dir = env.MERGEPILOT_RUN_BINDING_KEYSTORE;
  const st = runBindingAuthStatus(env);
  if (st.state !== 'READY') {
    return { status: 503, body: { ok: false, reason: 'KEYSTORE_NOT_READY', state: st.state, blocked_condition: st.blocked_condition } };
  }
  const now = Date.now();
  const keyId = `rk-${new Date(now).toISOString().replace(/[-:TZ.]/g, '').slice(0, 14)}-${crypto.randomBytes(3).toString('hex')}`;
  const secretVal = crypto.randomBytes(32).toString('hex');
  const newFile = path.join(dir, `${keyId}.key.json`);
  fs.writeFileSync(newFile, JSON.stringify({
    key_id: keyId, secret: secretVal, created_at: new Date(now).toISOString(),
    expires_at: new Date(now + 90 * 86400_000).toISOString(), revoked: false,
  }, null, 2), { mode: 0o600 });
  // 既有在用密钥按 grace 收紧到期（grace=0 即刻失效；轮换期间签名方需切换新 key）
  const expired = [];
  for (const f of fs.readdirSync(dir).filter((f) => f.endsWith('.key.json'))) {
    if (f === `${keyId}.key.json`) continue;
    const fp = path.join(dir, f);
    let k; try { k = JSON.parse(fs.readFileSync(fp, 'utf8')); } catch { continue; }
    if (k.revoked) continue;
    const oldExp = new Date(k.expires_at).getTime();
    const nextExp = Math.min(oldExp, now + Math.max(0, grace_ms));
    if (nextExp < oldExp) {
      k.expires_at = new Date(nextExp).toISOString();
      fs.writeFileSync(fp, JSON.stringify(k, null, 2), { mode: 0o600 });
      expired.push(k.key_id ?? f);
    }
  }
  counters.key_rotations += 1;
  await auditEvent(dsn, {
    kind: 'KEY_ROTATED', actor: operator || 'unknown-operator',
    reason: `keystore rotated: new=${keyId} expired=${expired.length} grace_ms=${grace_ms}`,
    meta: { new_key_id: keyId, expired_key_ids: expired, grace_ms },
  });
  // 注意：secret 不出现在返回值/日志——操作员经受控通道从 keystore 文件取。
  return { status: 200, body: { ok: true, new_key_id: keyId, key_file: newFile, expired_key_ids: expired, grace_ms } };
}

// ── metrics（计数器 + 最近一次状态快照；全部真实值，无伪造 READY）──────
let lastStatusSnapshot = null;
export function rememberStatusForMetrics(status) { lastStatusSnapshot = status; }
export function cchainMetricsSnapshot(env = process.env) {
  const snap = lastStatusSnapshot;
  const ready = (state) => (state === 'READY' || state === 'ATTESTED' ? 1 : 0);
  return {
    source: 'cchain', generated_at: new Date().toISOString(),
    note: '计数器为进程内存（重启归零）；组件状态为最近一次真实观测快照',
    counters: cchainCounters(),
    gauges: {
      cchain_overall_ready: snap ? (snap.overall === 'READY' ? 1 : 0) : null,
      cchain_model_cache_ready: snap?.components?.find((c) => c.component === 'model_cache') ? ready(snap.components.find((c) => c.component === 'model_cache').state) : null,
      cchain_provider_attested: snap?.components?.find((c) => c.component === 'provider_attestation') ? ready(snap.components.find((c) => c.component === 'provider_attestation').state) : null,
      cchain_run_binding_ready: snap?.components?.find((c) => c.component === 'run_binding_auth') ? ready(snap.components.find((c) => c.component === 'run_binding_auth').state) : null,
    },
    last_status: snap ? { overall: snap.overall, blocked_conditions: snap.blocked_conditions } : null,
    enforce_flag: env.MERGEPILOT_CCHAIN_ENFORCE === '1',
  };
}

// ── FXV run 启动 enforcement gate（默认 off；on 时 BLOCKED→拒绝启动）───
let gateCache = { at: 0, status: null };
export function resetGateCache() { gateCache = { at: 0, status: null }; }

export async function enforceGate(env = process.env, enforceOverride) {
  const enforced = enforceOverride ?? (env.MERGEPILOT_CCHAIN_ENFORCE === '1');
  if (!enforced) return { enforced: false, allowed: true };
  // 15s 缓存：provider attestation 有网络往返，避免每个 run 都打外部端点
  if (!gateCache.status || Date.now() - gateCache.at > 15_000) {
    gateCache = { at: Date.now(), status: await cchainStatus(env) };
  }
  const allowed = gateCache.status.overall === 'READY';
  return { enforced: true, allowed, blocked_conditions: allowed ? [] : gateCache.status.blocked_conditions };
}
