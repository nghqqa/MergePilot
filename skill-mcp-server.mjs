#!/usr/bin/env node
// skill-mcp-server.mjs — exposes the M4-F deterministic Skills as MCP tools so
// AgentTeam members can call them exactly like rag_retrieve (same stdio channel,
// same tracing path: every call lands in the AgentLoop as tool.* span).
//
// Wrapped skills (implemented under skills/, deterministic pure-compute, no
// network / no DB / no LLM — safe to run inside the worker container):
//   skill_diff_parse     skills.diff_parse      diff text -> structured change_context
//   skill_risk_classify  skills.risk_classify   change_context -> advisory L0/L2 + recommended controls
//   skill_test_runner    skills.test_runner     deterministic regression baseline (parse profile; runs pytest)
//   skill_case_retrieval skills.case_retrieval  historical case lookup
//
// Pipeline note: skill_diff_parse output.change_context feeds skill_risk_classify
// input.change_context directly (the two contracts are designed to chain).
//
// MCP stdio protocol (same minimal impl as rag-mcp-server.mjs). Zero npm deps.
import http from 'node:http';
import readline from 'node:readline';
import { spawnSync } from 'node:child_process';

const REPO = process.env.SKILL_REPO_ROOT || '/opt/mergepilot';   // repo checkout inside the worker image
const PY = process.env.SKILL_PYTHON || '/opt/venv/standard/bin/python';

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
          const text = Buffer.concat(chunks).toString('utf-8');
          try { resolve(JSON.parse(text)); } catch { reject(new Error('non-JSON upstream: ' + text.slice(0, 120))); }
        });
      });
    req.on('error', reject);
    if (body) req.write(body);
    req.end();
  });
}

const AUDIT_ENDPOINT = process.env.SKILL_AUDIT_ENDPOINT || 'http://host.docker.internal:4184/api/rag/toolspan-audit';
function audit(record) {
  const body = JSON.stringify({ ...record, data_mode: 'DETERMINISTIC_SKILL' });
  httpJson(AUDIT_ENDPOINT, { method: 'POST', body }).catch(() => {});
}

// Run one skill via the common runtime CLI (validates envelope in-process).
// We spawn a fresh python per call: pure compute, cold start ~100ms, and the
// worker process stays stateless.
function runSkill(module, inputObj, viaHandle) {
  const call = viaHandle
    ? `import os
from skills.case_retrieval.embedding.fastembed_provider import DeterministicFakeProvider
prov = DeterministicFakeProvider()
import skills.case_retrieval.run as _run_mod
_orig_run = _run_mod.core.run
def _patched_run(inp, **kw):
    kw.setdefault('embedding_provider', prov)
    return _orig_run(inp, **kw)
_run_mod.core.run = _patched_run
env = _run_mod.handle({'input': json.loads(sys.argv[1]), 'deadline': None})`
    : `import hashlib, time
rid = 'req-' + hashlib.md5(str(time.time_ns()).encode()).hexdigest()[:10]
req = {'contract_version': '1', 'request_id': rid, 'trace_id': 'mcp-' + rid, 'input': json.loads(sys.argv[1])}
env, _ = mod.run.run_request(req, mod.run.handle)`;
  const code = `
import sys, json
sys.path.insert(0, ${JSON.stringify(REPO)})
mod = __import__(${JSON.stringify(module)}, fromlist=['run'])
${call}
print(json.dumps(env))
`.trim();
  const proc = spawnSync(PY, ['-c', code, JSON.stringify(inputObj)], {
    cwd: REPO,
    env: { ...process.env, PYTHONPATH: REPO },
    encoding: 'utf-8',
    timeout: 120_000,
    maxBuffer: 16 * 1024 * 1024,
  });
  if (proc.error) throw proc.error;
  const out = (proc.stdout || '').trim().split('\n').filter(Boolean).pop() || '';
  let env;
  try { env = JSON.parse(out); } catch {
    throw new Error('skill produced no envelope (stderr: ' + (proc.stderr || '').slice(-200) + ')');
  }
  return env;
}

const TOOLS = [
  {
    name: 'skill_diff_parse',
    description: '确定性 Skill（DETERMINISTIC，纯计算、无 LLM/网络/DB）：把 unified diff 解析为结构化 '
      + 'change_context（files/hunks/stats/categories）。其 output.change_context 可直接作为 skill_risk_classify '
      + '的输入。必填：repo、base_sha、head_sha、diff_format、diff_text。',
    inputSchema: {
      type: 'object',
      properties: {
        repo: { type: 'string' }, base_sha: { type: 'string' }, head_sha: { type: 'string' },
        diff_format: { type: 'string', description: 'unified（当前仅支持 unified diff 格式）' },
        diff_text: { type: 'string', description: '完整 unified diff 正文' },
      },
      required: ['repo', 'base_sha', 'head_sha', 'diff_format', 'diff_text'],
    },
  },
  {
    name: 'skill_risk_classify',
    description: '确定性 Skill（DETERMINISTIC）：对结构化 change_context（来自 skill_diff_parse）做可解释的 '
      + 'L0/L1/L2 咨询级风险分级，并给出原因与建议控制（如 HUMAN_REVIEW）。仅咨询参考——'
      + '永不构成授权决策，人工门（human gate）才是决定者。',
    inputSchema: {
      type: 'object',
      properties: { change_context: { type: 'object', description: '来自 skill_diff_parse 输出的 change_context' } },
      required: ['change_context'],
    },
  },
  {
    name: 'skill_test_runner',
    description: '确定性 Skill（DETERMINISTIC）：按 profile 运行仓库测试基线，返回结构化判定'
      + '（通过/失败计数与失败明细）。提交修复前先跑本工具。',
    inputSchema: {
      type: 'object',
      properties: { profile: { type: 'string', description: '测试 profile 名' }, head_sha: { type: 'string' } },
      required: ['profile'],
    },
  },
  {
    name: 'skill_case_retrieval',
    description: '确定性 Skill（DETERMINISTIC）：按查询文本检索相似的历史 PR 审查/修复/验证案例。'
      + '必填 query；可选 top_k。返回 severity、score、issue/fix 摘要与可核验引用'
      + '（PR url / commit sha）。',
    inputSchema: {
      type: 'object',
      properties: {
        query: { type: 'string', description: '检索文本（与历史案例的 issue/fix 匹配）' },
        change_context: { type: 'object', description: '可选：结构化变更上下文' },
        top_k: { type: 'number' },
      },
      required: ['query'],
    },
  },
  {
    name: 'skill_sast_scan',
    description: '确定性 Skill（DETERMINISTIC，纯计算、无网络）：对文件内容做静态分析——'
      + '密钥检测（PAT token/API key/AWS key）+ Python AST 规则（eval/exec、subprocess shell=True、'
      + 'SQL 注入、路径穿越）+ 依赖漏洞检查。返回含 rule_id、severity、risk_level、行号与修复建议的'
      + '结构化 findings。',
    inputSchema: {
      type: 'object',
      properties: {
        mode: { type: 'string', enum: ['inline', 'paths'], description: 'inline = 直接传入文件内容' },
        files: {
          type: 'array',
          description: 'inline 模式：{path, content} 对象数组',
          items: {
            type: 'object',
            properties: {
              path: { type: 'string', description: '文件路径（如 backend/src/api.py）' },
              content: { type: 'string', description: '完整文件内容' },
            },
            required: ['path', 'content'],
          },
        },
        paths: { type: 'array', items: { type: 'string' }, description: 'paths 模式：workspace 下的文件路径' },
      },
      required: ['mode'],
    },
  },
];

const MODULES = {
  skill_diff_parse: 'skills.diff_parse',
  skill_risk_classify: 'skills.risk_classify',
  skill_test_runner: 'skills.test_runner',
  skill_sast_scan: 'skills.sast_scan',
  skill_case_retrieval: 'skills.case_retrieval',
};
const HANDLE_ONLY = new Set(['skill_case_retrieval']);

async function callTool(name, args) {
  const mod = MODULES[name];
  if (!mod) throw new Error('unknown tool: ' + name);
  const t0 = Date.now();
  const env = runSkill(mod, args || {}, name === 'skill_case_retrieval');
  audit({
    tool: name, arguments_hash: String((env.request_id || '')).slice(0, 16),
    result_status: env.status === 'OK' ? 'OK' : 'ERROR', document_count: 0,
    latency_ms: Date.now() - t0, data_mode: 'DETERMINISTIC_SKILL',
    source_refs: ['skills/' + (MODULES[name].split('.')[1])],
  });
  const compact = {
    skill: name, status: env.status, message: env.message || '',
    output: env.output || {},                       // full structured output (deterministic compute, no bodies)
    request_id: env.request_id, duration_ms: env.duration_ms,
    citation_rule: 'deterministic compute — cite skill name + request_id; advisory output never replaces human approval',
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
    respond({ protocolVersion: (params && params.protocolVersion) || '2024-11-05', capabilities: { tools: {} }, serverInfo: { name: 'skill-mcp', version: '1.0.0' } });
  } else if ((method || '').startsWith('notifications/')) {
    // no response
  } else if (method === 'ping') {
    respond({});
  } else if (method === 'tools/list') {
    respond({ tools: TOOLS });
  } else if (method === 'tools/call') {
    callTool(params && params.name, params && params.arguments)
      .then((result) => respond({ content: result.content, isError: false }))
      .catch((e) => respond({ content: [{ type: 'text', text: String((e && e.message) || e) }], isError: true }));
  } else if (id !== undefined && id !== null) {
    fail(-32601, 'method not found: ' + method);
  }
});
process.stdin.on('end', () => process.exit(0));
