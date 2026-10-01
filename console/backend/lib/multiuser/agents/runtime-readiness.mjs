// lib/multiuser/agents/runtime-readiness.mjs — Wave 3.13 AgentTeams 运行时 readiness gate。
//
// 背景（Wave 3.12 BLOCKED_BY_FRESH_INSTALL）：controller 容器重启/recreate 后，
// worker Matrix 桥接可能不可消费（上游 copaw 恢复缺陷）——此状态下启动 AgentTeams
// 轮次必然 MT_REPLY_TIMEOUT 并把 run 打入 BLOCKED。本模块在 fixVerifyRound 入口
// fail-closed：runtime 不可用 → 不启动轮次（run 停留 FIX_QUEUED 可重试态 + 审计）。
//
// 判定（低成本、零 LLM 调用）：
//   AT_CTRL_NOT_READY        controller API 不可达/非 2xx
//   AT_WORKER_NOT_RUNNING    四具名 worker 任一非 Running（controller 权威视图）
//   AT_OK                    全部就绪（结果缓存 ttlMs，避免每 run 打爆 API）
//
// 纪律：只读探测（GET）；不输出 token/配置正文；缓存进程内。
const FOUR = ['mergepilot-leader', 'mergepilot-reviewer', 'mergepilot-fixer', 'mergepilot-verifier'];

let cache = { at: 0, ok: false, reason: 'AT_NEVER_CHECKED' };
let inflight = null;

export function resetRuntimeReadinessCacheForTests() { cache = { at: 0, ok: false, reason: 'AT_NEVER_CHECKED' }; inflight = null; }
export function runtimeReadinessState() { return { ...cache, four: FOUR }; }

export async function checkAgentTeamsRuntimeReadiness(
  { baseUrl, token, workerNames = FOUR, fetchImpl, ttlMs = 30_000 } = {},
) {
  const now = Date.now();
  if (cache.ok && now - cache.at < ttlMs) return { ok: true, reason: 'AT_OK', cached: true };
  if (inflight) return inflight;
  inflight = (async () => {
    try {
      const f = fetchImpl ?? fetch;
      const res = await f(`${baseUrl}/api/v1/workers`, {
        headers: { Authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(8000),
      });
      if (!res.ok) { cache = { at: now, ok: false, reason: 'AT_CTRL_NOT_READY' }; return { ...cache }; }
      const body = await res.json().catch(() => null);
      const list = Array.isArray(body?.workers) ? body.workers : (Array.isArray(body) ? body : []);
      const byName = new Map(list.map((w) => [w.name, w]));
      for (const name of workerNames) {
        const w = byName.get(name);
        // controller 223ddc2 契约：worker 对象的运行态字段为 phase（'Running'）
        const phase = String(w?.phase ?? w?.state ?? w?.status ?? '').toLowerCase();
        if (!w || phase !== 'running') {
          cache = { at: now, ok: false, reason: 'AT_WORKER_NOT_RUNNING', worker: name };
          return { ...cache };
        }
      }
      cache = { at: now, ok: true, reason: 'AT_OK' };
      return { ...cache };
    } catch {
      cache = { at: now, ok: false, reason: 'AT_CTRL_NOT_READY' };
      return { ...cache };
    } finally { inflight = null; }
  })();
  return inflight;
}
