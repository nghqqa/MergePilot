// backend/lib/polardb.mjs — Phase 15 PolarDB connectivity audit + demo fixture.
// Honest by construction:
//   - audit is read-only: it only reports what is configured in the environment;
//     it never attempts a connection when no endpoint exists
//   - if any prerequisite is missing, status stays NOT_CONNECTED — no "looks
//     like a real PolarDB" fake connection is ever created
//   - the fixture branch is a read-only in-memory snapshot labeled SIMULATED in
//     every response (data_mode + polardb_connection), in UI and audit records

import { createHash } from 'node:crypto';
import { appendToolSpan } from './rag.mjs';

export const POLARDB_CONNECTION = 'NOT CONNECTED';

// Read-only environment probe. Returns what was checked and what was found.
// No connection is attempted (nothing to connect to). Credential material, if
// it ever exists, must come from protected storage and is never read here.
function auditConditions() {
  const env = process.env;
  const endpoint = env.POLARDB_ENDPOINT || env.POLARDB_DB_ENDPOINT || env.DATABASE_URL || env.DB_HOST || '';
  return {
    endpoint_configured: {
      value: Boolean(endpoint),
      evidence: 'POLARDB_ENDPOINT / POLARDB_DB_ENDPOINT / DATABASE_URL / DB_HOST 均未设置；agentloop-secure.env 与 p14h2 ctrl 容器 env 扫描无 PolarDB/RDS 线索（2026-08-30 只读扫描）',
    },
    readonly_account: {
      value: false,
      evidence: '未配置任何 PolarDB 账号；凭据规范要求只读账号从受保护存储读取，当前受保护存储中无此类条目',
    },
    network_reachability: {
      value: 'NOT_ATTEMPTED',
      evidence: '无 endpoint 可达性目标，未发起任何网络连接（避免伪造探测结果）',
    },
    whitelist: {
      value: 'NOT_ASSESSABLE',
      evidence: '白名单是 endpoint 侧属性，需真实 endpoint 与账号后才能验证',
    },
    schema_table_access: {
      value: 'NOT_ASSESSABLE',
      evidence: '需只读连接后执行 information_schema 只读查询，当前不可达',
    },
    agentic_branch_api: {
      value: 'NOT_ASSESSABLE',
      evidence: 'Agentic Database Branch 接入接口需云端凭据（受保护存储），当前未配置',
    },
  };
}

let cached = null;
export function polardbAudit() {
  if (cached) return cached;
  const conditions = auditConditions();
  const allMet = conditions.endpoint_configured.value
    && conditions.readonly_account.value
    && conditions.network_reachability.value === 'OK';
  cached = {
    connection: POLARDB_CONNECTION,
    probe_mode: 'read-only audit, connection never attempted',
    audited_at: new Date().toISOString(),
    conditions,
    verdict: allMet ? 'PREREQUISITES_PRESENT_REVIEW_REQUIRED' : 'PREREQUISITES_MISSING',
    disclosure: '缺少任一前置条件即保持 NOT CONNECTED；不搭建“看起来像真实 PolarDB”的假连接。演示使用 SIMULATED 只读 fixture。',
  };
  return cached;
}

// ------------------------------------------------------------- SIMULATED fixture

// Read-only snapshot shaped like the demo's public case facts. Values are the
// platform's own synthetic demo facts (PR #1/#2/#3), not enterprise data.
const FIXTURE_TABLES = {
  pr_cases: [
    { pr: 1, case_id: 'pr1-normal-review', risk_level: 'normal', gate: 'none', final_status: 'SUCCESS' },
    { pr: 2, case_id: 'pr2-high-risk-human-gate', risk_level: 'high', gate: 'approved', final_status: 'SUCCESS' },
    { pr: 3, case_id: 'pr3-high-risk-human-reject', risk_level: 'high', gate: 'rejected', final_status: 'REJECTED' },
  ],
  review_findings: [
    { pr: 1, finding: 'none', category: null, risk_level: 'normal' },
    { pr: 2, finding: 'path-traversal', category: 'CWE-22', risk_level: 'high' },
    { pr: 3, finding: 'high-risk-pattern', category: 'CWE-22', risk_level: 'high' },
  ],
};

function argsHash(obj) {
  return createHash('sha256').update(JSON.stringify(obj), 'utf8').digest('hex').slice(0, 16);
}

// Fixed-shape read-only query: table + equality filters only. This is NOT a
// SQL engine and must never be presented as a live database connection.
export function fixtureQuery(table, where = {}) {
  const t0 = Date.now();
  const rows = FIXTURE_TABLES[table] || [];
  const filtered = rows.filter((row) => Object.entries(where).every(([k, v]) => String(row[k]) === String(v)));
  const result = {
    data_mode: 'SIMULATED',
    polardb_connection: POLARDB_CONNECTION,
    fixture_table: table,
    row_count: filtered.length,
    rows: filtered,
    latency_ms: Date.now() - t0,
    disclosure: 'PolarDB-compatible read-only fixture — DATA MODE: SIMULATED, POLARDB CONNECTION: NOT CONNECTED',
  };
  // database.query tool-span audit record — contract fields only
  appendToolSpan({
    tool: 'database.query',
    arguments_hash: argsHash({ table, where }),
    result_status: 'OK',
    row_count: filtered.length,
    latency_ms: result.latency_ms,
    data_mode: 'SIMULATED',
    source_refs: [`fixture://polardb-compatible/${table}`],
  });
  return result;
}

export function fixtureInventory() {
  return {
    data_mode: 'SIMULATED',
    polardb_connection: POLARDB_CONNECTION,
    tables: Object.fromEntries(Object.entries(FIXTURE_TABLES).map(([k, v]) => [k, { row_count: v.length, columns: Object.keys(v[0] || {}) }])),
  };
}
