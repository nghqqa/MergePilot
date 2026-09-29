// console/backend/lib/multiuser/agents/agentteams-executor.mjs — Wave 3.3：外部 AgentTeams 运行时 adapter。
//
// 来源（阶段 1 实证）：github.com/agentscope-ai/AgentTeams（Apache-2.0，v1.2.x）。
// 调用契约（docs/usage/{project-workflow-api,resource-management}.md）：
//  * PUT  /api/v1/workers/{name}        merge-patch 幂等创建/更新具名 Worker
//  * POST /api/v1/projects               创建项目（project_id 客户端指定，409 冲突）
//  * POST /api/v1/projects/{id}/replan   任务图（taskId/title/assignedTo/dependsOn）
//  * GET  /api/v1/projects/{id}/workflow?includeTasks=true  轮询状态
//  * POST /api/v1/projects/{id}/tasks/{taskId}/cancel       幂等取消（submissionId 围栏）
//  * Bearer 认证（K8s SA token 或 Matrix token——AGENTS_TOKEN 为部署级 secret，仅 env）
//
// 安全契约（任务书阶段 2/3）：
//  * 默认 disabled（MU_EXECUTOR=internal）——零外部请求；未知值/缺配置/健康检查失败
//    一律拒绝启用或回退 internal，绝不静默宣称 AgentTeams 已运行；
//  * MergePilot 的 DB/GitHub/LLM 凭据永不出站；任务载荷只含脱敏 finding 摘要
//    （与 LLM egress 同白名单，≤2KiB/任务），无 diff/源码/webhook body/PII；
//  * 外部输出必须过 schema（findings/verdict 白名单）——失败 fail-closed；
//  * AgentTeams 的结果仅是建议：MergePilot Leader 策略保留终裁，Fixer 产物
//    仅 dry-run 建议（本地校验，不自动应用/push），Verifier 不采信自述；
//  * 外部任务与内部 run/attempt 的映射：project_id=mp-{run 前缀}，task ID 摘要
//    记入 evidence_ref（不记任务正文）；审计只含 reason/角色/耗时/ID 摘要。
import crypto from 'node:crypto';

export const AGENTTEAMS_WORKERS = Object.freeze({
  leader: { name: 'mergepilot-leader', identity: 'MergePilot 审查编排 Leader：只依据结构化 finding 摘要给出裁定建议，不执行任何写操作。' },
  reviewer: { name: 'mergepilot-reviewer', identity: 'MergePilot 审查 Reviewer：基于脱敏 finding 摘要给出语义审查建议（JSON）。' },
  fixer: { name: 'mergepilot-fixer', identity: 'MergePilot 修复 Fixer：仅产出 dry-run 修复建议文本，禁止执行命令或写仓库。' },
  verifier: { name: 'mergepilot-verifier', identity: 'MergePilot 验证 Verifier：独立判断修复建议是否解决 finding，不信任 Fixer 自述。' },
});

export const AT_LIMITS = Object.freeze({
  briefMaxChars: 2048, pollTimeoutMs: 120_000, pollIntervalMs: 3_000,
  summaryMaxBytes: 16 * 1024,
});

/** 部署级配置解析：仅 MU_EXECUTOR/MU_AGENTTEAMS_BASE_URL/MU_AGENTTEAMS_TOKEN/MU_AGENTTEAMS_TIMEOUT_MS。 */
export function resolveAgentTeamsConfig(env = process.env) {
  const mode = String(env.MU_EXECUTOR ?? 'internal').trim();
  if (mode !== 'agentteams') return { kind: 'internal' };
  const base = String(env.MU_AGENTTEAMS_BASE_URL ?? '').trim();
  const token = String(env.MU_AGENTTEAMS_TOKEN ?? '').trim();
  if (!base || !token) return { kind: 'internal', reason: 'AT_NOT_CONFIGURED' };
  if (!/^https?:\/\//.test(base)) return { kind: 'internal', reason: 'AT_BAD_URL' };
  return { kind: 'agentteams', baseUrl: base.replace(/\/+$/, ''), token,
    timeout: Math.min(Number(env.MU_AGENTTEAMS_TIMEOUT_MS) || AT_LIMITS.pollTimeoutMs, 300_000) };
}

const atFetch = async (cfg, path, opts = {}, fetchImpl = fetch) => {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), cfg.timeout);
  try {
    return await fetchImpl(`${cfg.baseUrl}${path}`, { ...opts, signal: ctrl.signal,
      headers: { 'content-type': 'application/json',
        authorization: `Bearer ${cfg.token ?? process.env.MU_AGENTTEAMS_TOKEN ?? ''}`,
        ...(opts.headers ?? {}) } });
  } finally { clearTimeout(t); }
};

/** 健康检查（未启用/不可达/未授权 → 不启用；绝不静默宣称已运行）。 */
export async function agentTeamsHealthy(cfg, fetchImpl = fetch) {
  if (cfg.kind !== 'agentteams') return { ok: false, reason: 'AT_DISABLED' };
  try {
    const r = await atFetch(cfg, '/api/v1/projects?limit=1', {}, fetchImpl);
    if (r.status === 401 || r.status === 403) return { ok: false, reason: 'AT_AUTH_FAILED' };
    if (!r.ok) return { ok: false, reason: `AT_HTTP_${r.status}` };
    return { ok: true };
  } catch (e) {
    return { ok: false, reason: String(e?.name ?? '').toLowerCase().includes('abort') ? 'AT_TIMEOUT' : 'AT_UNREACHABLE' };
  }
}

/** 幂等创建四个具名 Agent（merge-patch；不暴露端口、不挂 MCP、无凭据注入）。 */
export async function ensureFourAgents(cfg, { model, fetchImpl = fetch } = {}) {
  if (cfg.kind !== 'agentteams') return { ok: false, reason: 'AT_DISABLED' };
  const created = [];
  for (const def of Object.values(AGENTTEAMS_WORKERS)) {
    try {
      const r = await atFetch(cfg, `/api/v1/workers/${def.name}`, { method: 'PUT',
        body: JSON.stringify({ spec: { runtime: 'qwenpaw', model: String(model ?? 'deepseek-flash'),
          identity: def.identity, state: 'Running', skills: [], mcpServers: [], expose: [] } }) }, fetchImpl);
      if (!r.ok && r.status !== 409) return { ok: false, reason: `AT_WORKER_HTTP_${r.status}`, worker: def.name };
      created.push(def.name);
    } catch { return { ok: false, reason: 'AT_WORKER_UNREACHABLE', worker: def.name }; }
  }
  return { ok: true, created };
}

// 任务简报：与 LLM egress 同白名单——仅 finding 脱敏摘要，≤2KiB，无 diff/源码/凭据
export function sanitizeBrief(findings) {
  const brief = (findings ?? []).slice(0, 20).map((f) => ({
    rule_id: String(f.rule_id ?? '').slice(0, 60), severity: f.severity,
    path: String(f.path ?? '').slice(0, 200), line_start: f.line_start ?? null,
    masked: String(f.summary_masked ?? '').slice(0, 120) }));
  let text = JSON.stringify({ untrusted_findings: brief });
  if (text.length > AT_LIMITS.briefMaxChars) text = JSON.stringify({ untrusted_findings: brief.slice(0, 5) });
  return text;
}

// 外部输出 schema（严格 allowlist；失败 fail-closed）
export function validateAgentTeamsOutput(role, parsed) {
  if (!parsed || typeof parsed !== 'object') return { ok: false, code: 'AT_SCHEMA_INVALID' };
  if (role === 'verifier') {
    if (!['PASS', 'FAIL', 'BLOCKED'].includes(parsed.verdict)) return { ok: false, code: 'AT_SCHEMA_INVALID' };
    for (const k of Object.keys(parsed)) if (!['verdict', 'note'].includes(k)) return { ok: false, code: 'AT_SCHEMA_INVALID' };
    if (parsed.note && String(parsed.note).length > 500) return { ok: false, code: 'AT_SCHEMA_INVALID' };
    return { ok: true };
  }
  if (role === 'fixer') {
    for (const k of Object.keys(parsed)) if (!['suggestion', 'patch_hint'].includes(k)) return { ok: false, code: 'AT_SCHEMA_INVALID' };
    if (parsed.suggestion && String(parsed.suggestion).length > 2000) return { ok: false, code: 'AT_SCHEMA_INVALID' };
    if (parsed.patch_hint && String(parsed.patch_hint).length > 4000) return { ok: false, code: 'AT_SCHEMA_INVALID' };
    if (/git\s+push|rm\s+-rf\s+\//i.test(String(parsed.patch_hint ?? '') + String(parsed.suggestion ?? ''))) {
      return { ok: false, code: 'AT_FORBIDDEN_CONTENT' };
    }
    return { ok: true };
  }
  if (role === 'reviewer') {
    if (!Array.isArray(parsed.findings)) return { ok: false, code: 'AT_SCHEMA_INVALID' };
    for (const f of parsed.findings) {
      for (const k of Object.keys(f)) if (!['severity', 'path', 'summary'].includes(k)) return { ok: false, code: 'AT_SCHEMA_INVALID' };
    }
    return { ok: true };
  }
  if (role === 'leader') {
    for (const k of Object.keys(parsed)) if (!['recommendation', 'confidence'].includes(k)) return { ok: false, code: 'AT_SCHEMA_INVALID' };
    return { ok: true };
  }
  return { ok: false, code: 'AT_ROLE_UNKNOWN' };
}

/**
 * 提交一轮外部任务（fix+verify 两 Agent；reviewer/leader 由 replan 首任务串起）。
 * 幂等：project_id 由 run 派生（409 → 复用）。返回 {ok, project_id} 或稳定 reason。
 */
export async function submitExternalRound(cfg, { runId, findings, fetchImpl = fetch }) {
  if (cfg.kind !== 'agentteams') return { ok: false, reason: 'AT_DISABLED' };
  const projectId = `mp-${String(runId).replace(/-/g, '').slice(0, 20)}`;
  const brief = sanitizeBrief(findings);
  try {
    const create = await atFetch(cfg, '/api/v1/projects', { method: 'POST',
      body: JSON.stringify({ title: `mp-review ${String(runId).slice(0, 8)}`, source: 'mergepilot',
        requester: 'mergepilot-executor', project_id: projectId }) }, fetchImpl);
    if (!create.ok && create.status !== 409) return { ok: false, reason: `AT_PROJECT_HTTP_${create.status}` };
    const plan = await atFetch(cfg, `/api/v1/projects/${projectId}/replan`, { method: 'POST',
      body: JSON.stringify({ tasks: [
        { taskId: 't-review', title: `review:${brief}`, assignedTo: AGENTTEAMS_WORKERS.reviewer.name, dependsOn: [] },
        { taskId: 't-leader', title: 'decide:给出裁定建议(JSON {recommendation,confidence})',
          assignedTo: AGENTTEAMS_WORKERS.leader.name, dependsOn: ['t-review'] },
        { taskId: 't-fix', title: `fix:产出修复建议(JSON {suggestion,patch_hint})——仅 dry-run 文本，禁止执行`,
          assignedTo: AGENTTEAMS_WORKERS.fixer.name, dependsOn: ['t-leader'] },
        { taskId: 't-verify', title: 'verify:独立判断修复建议是否解决 finding(JSON {verdict:PASS|FAIL|BLOCKED,note})',
          assignedTo: AGENTTEAMS_WORKERS.verifier.name, dependsOn: ['t-fix'] },
      ] }) }, fetchImpl);
    if (!plan.ok && plan.status !== 409) return { ok: false, reason: `AT_REPLAN_HTTP_${plan.status}` };
    return { ok: true, project_id: projectId };
  } catch { return { ok: false, reason: 'AT_SUBMIT_UNREACHABLE' }; }
}

/** 状态映射：AgentTeams → 内部语义。 */
export function mapTaskState(at) {
  return ({ pending: 'QUEUED', delegated: 'QUEUED', 'in-progress': 'RUNNING',
    completed: 'DONE', revision: 'REWORK', blocked: 'BLOCKED' }[String(at)] ?? 'UNKNOWN');
}

/** 轮询直至终态或超时（返回每任务状态映射）。 */
export async function pollExternalRound(cfg, { projectId, fetchImpl = fetch, maxWaitMs = AT_LIMITS.pollTimeoutMs }) {
  const deadline = Date.now() + maxWaitMs;
  for (;;) {
    try {
      const r = await atFetch(cfg, `/api/v1/projects/${projectId}/workflow?includeTasks=true`, {}, fetchImpl);
      if (!r.ok) return { ok: false, reason: `AT_POLL_HTTP_${r.status}` };
      const j = await r.json().catch(() => null);
      const nodes = j?.nodes ?? [];
      const states = {};
      let terminal = nodes.length > 0;
      for (const n of nodes) {
        const m = mapTaskState(n.status);
        states[String(n.taskId ?? n.id)] = m;
        if (!['DONE', 'REWORK', 'BLOCKED'].includes(m)) terminal = false;
      }
      if (terminal) return { ok: true, states };
    } catch { return { ok: false, reason: 'AT_POLL_UNREACHABLE' }; }
    if (Date.now() > deadline) return { ok: false, reason: 'AT_POLL_TIMEOUT' };
    await new Promise((r2) => setTimeout(r2, AT_LIMITS.pollIntervalMs));
  }
}

/** 取某任务 summary（外部结果正文，≤16KiB；调用方须过 validateAgentTeamsOutput）。 */
export async function fetchTaskSummary(cfg, { projectId, taskId, fetchImpl = fetch }) {
  try {
    const r = await atFetch(cfg, `/api/v1/projects/${projectId}/tasks/${taskId}`, {}, fetchImpl);
    if (!r.ok) return { ok: false, reason: `AT_TASK_HTTP_${r.status}` };
    const j = await r.json().catch(() => null); // 单次消费 body（text 后不可重复读）
    const summary = String(j?.summary ?? '');
    if (Buffer.byteLength(summary) > AT_LIMITS.summaryMaxBytes) return { ok: false, reason: 'AT_SUMMARY_OVERSIZE' };
    return { ok: true, summary };
  } catch { return { ok: false, reason: 'AT_SUMMARY_UNREACHABLE' }; }
}

/** 幂等取消（submissionId 围栏）。 */
export async function cancelExternalRound(cfg, { projectId, taskId, reason, submissionId, fetchImpl = fetch }) {
  try {
    const r = await atFetch(cfg, `/api/v1/projects/${projectId}/tasks/${taskId}/cancel`, { method: 'POST',
      body: JSON.stringify({ reason: String(reason ?? 'mergepilot-cancel').slice(0, 200), submissionId }) }, fetchImpl);
    if (!r.ok && r.status !== 409) return { ok: false, reason: `AT_CANCEL_HTTP_${r.status}` };
    return { ok: true };
  } catch { return { ok: false, reason: 'AT_CANCEL_UNREACHABLE' }; }
}

export const atDigest = (v) => crypto.createHash('sha256').update(String(v)).digest('hex').slice(0, 16);
