#!/usr/bin/env node
// rag-mcp-server.mjs — minimal MCP stdio server exposing the SYNTHETIC demo RAG
// as a CoPaw-callable tool "rag.retrieve". Spawned by the CoPaw worker
// (StdIOStatefulClient -> mcp SDK stdio_client), so every execution happens
// inside a real agent run and is traced by the agentscope toolkit.
// Data: 8 platform-owned synthetic docs via the host read-only forwarder.
// Audit: each call appends a contract-shaped record to the platform JSONL.
import http from 'node:http';
import readline from 'node:readline';

const RAG_ENDPOINT = process.env.RAG_ENDPOINT || 'http://host.docker.internal:4174/api/rag/search';
const AUDIT_ENDPOINT = process.env.RAG_AUDIT_ENDPOINT || 'http://host.docker.internal:4174/api/rag/toolspan-audit';

function httpJson(url, { method = 'GET', body } = {}) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const req = http.request(
      { host: u.hostname, port: u.port || 80, path: u.pathname + u.search, method,
        headers: body ? { 'content-type': 'application/json', 'content-length': Buffer.byteLength(body) } : {} },
      (res) => {
        const chunks = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8');
          try { resolve(JSON.parse(text)); } catch { reject(new Error('non-JSON upstream: ' + text.slice(0, 120))); }
        });
      });
    req.on('error', reject);
    if (body) req.write(body);
    req.end();
  });
}

const TOOLS = [{
  name: 'rag_retrieve',
  description: 'SYNTHETIC demo RAG retrieval over 8 platform-owned demo docs (no enterprise data). '
    + 'Returns query_hash, top_k and cited chunks with document_id, chunk_id, score, source_ref, retrieval_mode, data_mode=SYNTHETIC. Read-only.',
  inputSchema: {
    type: 'object',
    properties: {
      query: { type: 'string', description: '检索问题（中文或英文）' },
      top_k: { type: 'number', description: '返回条数，默认 3' },
    },
    required: ['query'],
  },
},
{
  name: 'database_create_branch',
    description: 'Create an isolated database branch for a migration candidate (PolarDB-compatible; SIMULATED fixture unless LIVE gates met). Returns branch_id.',
    inputSchema: { type: 'object', properties: { candidate_id: { type: 'string', description: 'candidate-a | candidate-b | candidate-c' } }, required: ['candidate_id'] },
  },
  {
    name: 'database_validate_migration',
    description: 'Run migration assertions on the isolated branch. Returns assertion_count, failed_assertions, verdict (VERIFIED | DEGRADED | REJECTED).',
    inputSchema: { type: 'object', properties: { branch_id: { type: 'string' }, candidate_id: { type: 'string' } }, required: ['branch_id', 'candidate_id'] },
  },
  {
    name: 'database_assert_data',
    description: 'Assert data quality on the branch (NULL customer_id rows, duplicate order_id, refunded rows). Returns row_counts and passed.',
    inputSchema: { type: 'object', properties: { branch_id: { type: 'string' }, candidate_id: { type: 'string' } }, required: ['branch_id', 'candidate_id'] },
  },
  {
    name: 'database_rollback_check',
    description: 'Verify the branch is droppable and main DB untouched (rollback safety). Returns passed and main_untouched.',
    inputSchema: { type: 'object', properties: { branch_id: { type: 'string' }, candidate_id: { type: 'string' } }, required: ['branch_id', 'candidate_id'] },
  }];

const DB_BASE = process.env.DB_BASE || 'http://host.docker.internal:4174/api/db';

async function dbCall(op, payload) {
  const body = JSON.stringify(payload);
  const r = await httpJson(DB_BASE + '/' + op, { method: 'POST', body });
  if (r.error) throw new Error('upstream error: ' + r.error + ' ' + (r.detail || ''));
  return r;
}

async function callTool(name, args) {
  if (name === 'database_create_branch' || name === 'database_validate_migration' || name === 'database_assert_data' || name === 'database_rollback_check') {
    const opMap = { database_create_branch: 'branch/create', database_validate_migration: 'branch/validate', database_assert_data: 'branch/assert', database_rollback_check: 'branch/rollback' };
    const payload = name === 'database_create_branch'
      ? { candidate_id: String((args && args.candidate_id) || '') }
      : { branch_id: String((args && args.branch_id) || ''), candidate_id: String((args && args.candidate_id) || '') };
    const r = await dbCall(opMap[name], payload);
    return { content: [{ type: 'text', text: JSON.stringify(r) }] };
  }
  if (name !== 'rag_retrieve') throw new Error('unknown tool: ' + name);
  // logical name rag.retrieve -> runtime name rag_retrieve (OpenAI tool pattern ^[a-zA-Z0-9_-]+$ forbids dots)
  const query = String((args && args.query) || '').trim();
  if (!query) throw new Error('query is required');
  const topK = Number((args && args.top_k) || 3);
  const t0 = Date.now();
  const r = await httpJson(`${RAG_ENDPOINT}?q=${encodeURIComponent(query)}&k=${topK}`);
  if (r.error) throw new Error('upstream error: ' + r.error);
  const audit = {
    tool: 'rag.retrieve',
    arguments_hash: r.query_hash,
    result_status: (r.results || []).length ? 'OK' : 'EMPTY',
    document_count: (r.results || []).length,
    latency_ms: Date.now() - t0,
    data_mode: 'SYNTHETIC',
    source_refs: (r.results || []).map((x) => x.source_ref),
  };
  httpJson(AUDIT_ENDPOINT, { method: 'POST', body: JSON.stringify(audit) }).catch(() => {});
  const compact = {
    query_hash: r.query_hash,
    top_k: r.top_k,
    data_mode: r.data_mode,
    retrieval_mode: r.retrieval_mode,
    citation_rule: 'answers must cite source_refs; uncited answers are UNVERIFIED',
    results: (r.results || []).map(({ document_id, chunk_id, score, source_ref, retrieval_mode, data_mode }) => ({ document_id, chunk_id, score, source_ref, retrieval_mode, data_mode })),
  };
  return { content: [{ type: 'text', text: JSON.stringify(compact) }] };
}

const rl = readline.createInterface({ input: process.stdin, terminal: false });
rl.on('line', (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
  const { id, method, params } = msg || {};
  const respond = (result) => {
    if (id === undefined || id === null) return;
    process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id, result }) + '\n');
  };
  const fail = (code, message) => {
    if (id === undefined || id === null) return;
    process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id, error: { code, message } }) + '\n');
  };
  if (method === 'initialize') {
    respond({ protocolVersion: (params && params.protocolVersion) || '2024-11-05', capabilities: { tools: {} }, serverInfo: { name: 'rag-synthetic-mcp', version: '1.0.0' } });
  } else if (method === 'notifications/initialized' || (method || '').startsWith('notifications/')) {
    // notifications: no response
  } else if (method === 'ping') {
    respond({});
  } else if (method === 'tools/list') {
    respond({ tools: TOOLS });
  } else if (method === 'tools/call') {
    callTool(params && params.name, params && params.arguments)
      .then((result) => respond({ content: result.content, isError: false }))
      .catch((e) => respond({ content: [{ type: 'text', text: String(e && e.message || e) }], isError: true }));
  } else if (id !== undefined && id !== null) {
    fail(-32601, 'method not found: ' + method);
  }
});
process.stdin.on('end', () => process.exit(0));
