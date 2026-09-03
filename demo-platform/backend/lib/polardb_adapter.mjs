// backend/lib/polardb_adapter.mjs — Phase 16 env-gated PolarDB adapter.
// Safe by construction:
//   - connection is OFF unless every LIVE prerequisite is present
//   - credentials are NEVER read into this process: only the NAME of a secret
//     reference (Windows Credential Manager entry / Docker secret / env-ref)
//     is checked for existence; values never enter logs, traces or evidence
//   - when any prerequisite is missing the adapter stays NOT_CONNECTED and all
//     operations run against the in-memory SIMULATED fixture
//   - LIVE mode is restricted to read-only, isolated branch operations:
//     create_branch / validate_migration / assert_data / rollback_check

import { createHash } from 'node:crypto';
import { appendToolSpan } from './rag.mjs';

export const POLARDB_CONNECTION = 'NOT CONNECTED';
export const BRANCH_MODE = 'SIMULATED';

// ---------------------------------------------------------------- adapter config

export function adapterConfig() {
  const env = process.env;
  return {
    endpoint: env.POLARDB_ENDPOINT || '',
    database: env.POLARDB_DATABASE || '',
    readonly_user: env.POLARDB_READONLY_USER || '',
    secret_ref: env.POLARDB_SECRET_REF || '',      // NAME of the secret reference only
    allowlist_confirmed: env.POLARDB_ALLOWLIST_CONFIRMED === '1' || env.POLARDB_ALLOWLIST_CONFIRMED === 'true',
    branch_api: env.POLARDB_BRANCH_API || '',
    mode: env.POLARDB_MODE || 'auto',              // auto | force_simulated
  };
}

// The eight LIVE gates (Phase 16 §二). All must be green to leave NOT_CONNECTED.
export function evaluateLiveGates() {
  const cfg = adapterConfig();
  const gates = [
    { key: 'endpoint_exists',          ok: Boolean(cfg.endpoint),           note: cfg.endpoint ? 'set' : 'POLARDB_ENDPOINT 未设置' },
    { key: 'readonly_account',         ok: Boolean(cfg.readonly_user),      note: cfg.readonly_user ? 'set' : 'POLARDB_READONLY_USER 未设置' },
    { key: 'secret_resolvable',        ok: Boolean(cfg.secret_ref),         note: cfg.secret_ref ? 'secret reference named (value never enters this process)' : 'POLARDB_SECRET_REF 未设置' },
    { key: 'network_connectivity',     ok: false,                           note: 'NOT_ATTEMPTED — 无 endpoint，不发起连接' },
    { key: 'allowlist_confirmed',      ok: cfg.allowlist_confirmed,         note: cfg.allowlist_confirmed ? 'confirmed' : 'POLARDB_ALLOWLIST_CONFIRMED 未确认' },
    { key: 'schema_query_ok',          ok: false,                           note: 'NOT_ATTEMPTED — 需真实连接' },
    { key: 'branch_api_reachable',     ok: Boolean(cfg.branch_api),         note: cfg.branch_api ? 'set' : 'POLARDB_BRANCH_API 未设置' },
    { key: 'readonly_privilege_check', ok: false,                           note: 'NOT_ATTEMPTED — 需真实连接' },
  ];
  const allLive = gates.every((g) => g.ok);
  return { allLive, gates, mode: cfg.mode };
}

export function connectionState() {
  const { allLive, gates, mode } = evaluateLiveGates();
  const live = allLive && mode !== 'force_simulated';
  return {
    connection: live ? 'LIVE' : POLARDB_CONNECTION,
    branch_mode: live ? 'LIVE' : BRANCH_MODE,
    missing: gates.filter((g) => !g.ok).map((g) => g.key),
    gates,
    disclosure: live
      ? 'LIVE — 只读 + 隔离 Branch 操作'
      : 'NOT CONNECTED — 前置条件缺失，使用 SIMULATED fixture；不创建假连接',
  };
}

// ------------------------------------------------------------ PR #4 schema case

// Synthetic/redacted schema baseline and candidate definitions for the
// cross-repo order-schema migration demo (PR #4).
export const SCHEMA_BASELINE = {
  tables: {
    orders: { columns: ['id PK', 'customer_id NULL', 'status', 'created_at'], rows_synthetic: 10000, dirty_rows: { customer_id_null: 137 } },
    payments: { columns: ['id PK', 'order_id (dup risk)', 'amount', 'paid_at'], rows_synthetic: 9600, dirty_rows: { duplicate_order_id: 2 } },
  },
  enums: { order_status: ['created', 'paid', 'shipped', 'cancelled'] },
  changes: [
    'orders.customer_id → NOT NULL（需历史回填 137 行）',
    'payments.order_id → UNIQUE（存在 2 条重复）',
    'order_status 枚举增加 refunded',
  ],
  compat: 'API/Worker 新旧版本并行：旧 Worker 写入的行必须仍满足新约束',
};

export const CANDIDATES = [
  {
    candidate_id: 'candidate-a',
    title: '直接加约束',
    strategy: 'ALTER TABLE 直接加 NOT NULL / UNIQUE，不做回填',
    repos_touched: ['fastapi-demo-schema'],
    expected: 'REJECTED',
    failure_reasons: ['历史 orders.customer_id 存在 137 行 NULL → NOT NULL 约束直接失败', 'payments.order_id 重复行未清理 → UNIQUE 索引创建失败'],
    assertions: [
      { name: 'historical_null_backfilled', passed: false, detail: '137 rows with NULL customer_id remain' },
      { name: 'unique_index_created', passed: false, detail: 'duplicate order_id x2 blocks UNIQUE' },
      { name: 'old_worker_compat', passed: true, detail: 'n/a — migration never applied' },
    ],
  },
  {
    candidate_id: 'candidate-b',
    title: '先回填再加约束',
    strategy: '一次性回填 NULL 后立即加约束（无版本缓冲）',
    repos_touched: ['fastapi-demo-schema', 'fastapi-demo-worker'],
    expected: 'REJECTED 或 DEGRADED',
    failure_reasons: ['回填窗口内旧版 Worker 仍写入 NULL customer_id → 约束加完后写入失败（DEGRADED）', '新旧 Worker 并行期缺乏兼容层'],
    assertions: [
      { name: 'historical_null_backfilled', passed: true, detail: '137 rows backfilled' },
      { name: 'unique_index_created', passed: true, detail: 'duplicates merged' },
      { name: 'old_worker_compat', passed: false, detail: 'legacy worker writes NULL during deploy window' },
    ],
  },
  {
    candidate_id: 'candidate-c',
    title: '分阶段兼容新旧应用',
    strategy: '阶段1 回填+双写兼容 → 阶段2 Worker 全量升级 → 阶段3 加约束',
    repos_touched: ['fastapi-demo-schema', 'fastapi-demo-api', 'fastapi-demo-worker'],
    expected: 'VERIFIED',
    failure_reasons: [],
    assertions: [
      { name: 'historical_null_backfilled', passed: true, detail: '137 rows backfilled' },
      { name: 'unique_index_created', passed: true, detail: 'duplicates merged' },
      { name: 'old_worker_compat', passed: true, detail: 'compat layer active during parallel window' },
      { name: 'refunded_enum_added', passed: true, detail: 'order_status += refunded' },
      { name: 'rollback_check', passed: true, detail: 'branch droppable, main untouched' },
    ],
  },
];

function argsHash(obj) {
  return createHash('sha256').update(JSON.stringify(obj), 'utf8').digest('hex').slice(0, 16);
}

// In-memory branch state (SIMULATED). LIVE mode would proxy to the real
// Branch API with server-side credentials — not implemented without them.
const branches = new Map();

function audit(rec) {
  try { appendToolSpan(rec); } catch { /* best-effort */ }
}

export function createBranch(candidateId) {
  const t0 = Date.now();
  const cand = CANDIDATES.find((c) => c.candidate_id === candidateId);
  if (!cand) return { error: 'UNKNOWN_CANDIDATE' };
  const branch_id = 'branch_' + candidateId + '_' + Date.now().toString(36);
  branches.set(branch_id, { candidate_id: candidateId, created_at: new Date().toISOString(), validated: false });
  const latency = Date.now() - t0;
  audit({
    tool: 'database.create_branch', arguments_hash: argsHash({ candidateId }),
    result_status: 'OK', branch_id, latency_ms: latency,
    data_mode: 'SIMULATED', source_refs: ['fixture://polardb-branch/' + branch_id],
  });
  return { branch_id, candidate_id: candidateId, data_mode: 'SIMULATED', polardb_connection: POLARDB_CONNECTION, latency_ms: latency, disclosure: 'isolated branch — SIMULATED' };
}

export function validateMigration(branchId, candidateId) {
  const t0 = Date.now();
  const cand = CANDIDATES.find((c) => c.candidate_id === candidateId);
  if (!cand) return { error: 'UNKNOWN_CANDIDATE' };
  const failed = cand.assertions.filter((a) => !a.passed);
  const result = {
    candidate_id: candidateId,
    assertion_count: cand.assertions.length,
    failed_assertions: failed.map((f) => f.name),
    verdict: failed.length === 0 ? 'VERIFIED' : (candidateId === 'candidate-b' ? 'DEGRADED' : 'REJECTED'),
    assertions: cand.assertions,
    data_mode: 'SIMULATED',
    polardb_connection: POLARDB_CONNECTION,
    latency_ms: Date.now() - t0,
  };
  if (branches.has(branchId)) branches.get(branchId).validated = true;
  audit({
    tool: 'database.validate_migration', arguments_hash: argsHash({ branchId, candidateId }),
    result_status: failed.length === 0 ? 'OK' : 'ASSERTIONS_FAILED', branch_id: branchId,
    assertion_count: cand.assertions.length, failed_assertions: result.failed_assertions,
    latency_ms: result.latency_ms, data_mode: 'SIMULATED',
    source_refs: ['fixture://polardb-branch/schema-baseline'],
  });
  return result;
}

export function assertData(branchId, candidateId) {
  const t0 = Date.now();
  const result = {
    branch_id: branchId, candidate_id: candidateId,
    row_counts: {
      orders_total: 10000,
      orders_customer_id_null: candidateId === 'candidate-a' ? 137 : 0,
      payments_duplicate_order_id: candidateId === 'candidate-a' ? 2 : 0,
      order_status_refunded_rows: candidateId === 'candidate-c' ? 42 : 0,
    },
    passed: candidateId === 'candidate-c',
    data_mode: 'SIMULATED', polardb_connection: POLARDB_CONNECTION,
    latency_ms: Date.now() - t0,
  };
  audit({
    tool: 'database.assert_data', arguments_hash: argsHash({ branchId, candidateId }),
    result_status: result.passed ? 'OK' : 'FAILED', branch_id: branchId,
    row_count: result.row_counts.orders_total, latency_ms: result.latency_ms,
    data_mode: 'SIMULATED', source_refs: ['fixture://polardb-branch/assert-data'],
  });
  return result;
}

export function rollbackCheck(branchId, candidateId) {
  const t0 = Date.now();
  const result = {
    branch_id: branchId, candidate_id: candidateId,
    main_untouched: true, branch_droppable: true,
    passed: true, data_mode: 'SIMULATED', polardb_connection: POLARDB_CONNECTION,
    latency_ms: Date.now() - t0,
  };
  audit({
    tool: 'database.rollback_check', arguments_hash: argsHash({ branchId, candidateId }),
    result_status: 'OK', branch_id: branchId, latency_ms: result.latency_ms,
    data_mode: 'SIMULATED', source_refs: ['fixture://polardb-branch/rollback'],
  });
  return result;
}

export function branchState() {
  return {
    branches: Array.from(branches.entries()).map(([id, b]) => ({ branch_id: id, ...b })),
    data_mode: 'SIMULATED', polardb_connection: POLARDB_CONNECTION,
  };
}
