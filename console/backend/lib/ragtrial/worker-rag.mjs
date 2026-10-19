// worker-rag.mjs — 审查 worker 执行期知识库参考获取（RAG V2 快照唯一写入方）。
//
// 调用方：multiuser/review-service.mjs（真实审查链内，findings 落库后）。
// 任务身份全部服务端同源：runId/tenantId 来自本进程 DB 解析的 run 行，repo 来自
// binding 行（owner/name），base_ref 来自 provider 上下文——绝不采用客户端自报值。
//
// 语义（#491 独立复核定案）：
//  * 手工调用 machine/query 或页面 review-aux 不产生本模块产出——只有真实审查
//    任务执行到这里才算 worker 证据；
//  * 检索成功 ≠ 保存成功：保存失败不写行（页面如实「未记录」），另写
//    query_log 审计（RAG_WORKER_SAVE_FAILED）供诊断；
//  * 模型未配置 → 不检索不落行（attempted=false, reason=model_unresolved）；
//    模型配置但服务不可用 → search 返回 provider_unavailable 六态之一，
//    快照行如实记录该状态（citations=[]），页面显示「曾尝试但服务不可用」；
//  * 本模块绝不抛出——任何失败都降级为结构化返回，绝不阻断审查主链；
//  * reference only：产出不进入 finding/审批/门禁/VERIFIED/fixer/verifier。
import { createRagTrialStore } from './store.mjs';
import { resolveRequestModelId } from './api.mjs';
import { saveRunRagSnapshot, loadRunRagSnapshot, toSnapshotCitations } from './rag-snapshot.mjs';

export const WORKER_QUERY_STRATEGY = 'pr_context_v1';

// 查询构造（纯函数）：PR 标题 + 发现标题（执行期任务上下文），400 字符截断。
export function buildWorkerRagQuery({ prTitle, findingTitles }) {
  const parts = [];
  if (prTitle) parts.push(String(prTitle));
  for (const t of (findingTitles ?? [])) {
    if (t) parts.push(String(t));
  }
  return parts.join('；').slice(0, 400) || null;
}

const storeCache = new WeakMap(); // pool -> { env, store }
function err1line(e) { return String(e?.message ?? e).replace(/\s+/g, ' ').slice(0, 160); }

/**
 * 审查执行期知识库参考获取 + 快照保存（fail-soft，绝不抛出）。
 * @returns {Promise<{attempted:boolean, saved?:boolean, snapshot?:object,
 *   service_state?:string, reason?:string, save_error?:string}>}
 *   attempted=false：获取未发生（关闭/未接线/模型未配置）——不落任何行；
 *   saved=true：本次写入成功；saved=false 且 snapshot：同 key 既有快照（重试幂等，
 *   未覆盖）；saved=false 且无 snapshot：检索发生但保存失败（query_log 有审计）。
 */
export async function maybeSnapshotRunReferences(pool, env, task) {
  const actor = `worker-review:${String(task.runId).slice(0, 64)}`;
  try {
    if (String(env.RAGTRIAL_WORKER_RAG ?? '').trim().toLowerCase() === 'off') {
      return { attempted: false, reason: 'disabled' };
    }
    let store;
    try {
      const cached = storeCache.get(pool);
      store = (cached && cached.env === env) ? cached.store
        : await createRagTrialStore({ pool, env });
      storeCache.set(pool, { env, store });
    } catch (e) {
      return { attempted: false, reason: 'unwired', error: err1line(e) };
    }
    const modelId = resolveRequestModelId(env, null);
    if (!modelId) {
      await store.audit('RAG_WORKER_SKIP', actor, {
        detail: { reason: 'model_unresolved', note: 'RAGTRIAL_EMBED_MODEL_ID 未配置——执行期参考获取关闭（如实不落快照）' },
      }).catch(() => {});
      return { attempted: false, reason: 'model_unresolved' };
    }
    const q = buildWorkerRagQuery(task);
    const corpusBranch = String(task.baseRef || task.corpusBranch || 'main');
    let r;
    try {
      r = await store.search({ q, repo: String(task.repo), branch: corpusBranch,
        k: 5, actor, modelId, tenantId: task.tenantId ?? null });
    } catch (e) {
      await store.audit('RAG_WORKER_SKIP', actor, {
        detail: { reason: 'search_error', error: err1line(e) },
      }).catch(() => {});
      return { attempted: true, saved: false, reason: 'search_error', save_error: err1line(e) };
    }
    const citations = toSnapshotCitations(r.results ?? []);
    try {
      const { saved, row } = await saveRunRagSnapshot(pool, {
        runId: String(task.runId), tenantId: String(task.tenantId ?? 'unknown'),
        repo: String(task.repo), branch: corpusBranch,
        reviewHead: String(task.reviewHead ?? ''), baseRef: task.baseRef ?? null,
        corpusBranch, modelId: r.model_id ?? modelId, modelDigest: r.model_digest ?? '',
        indexVersion: r.index_version ?? 1, queryText: q,
        queryStrategy: WORKER_QUERY_STRATEGY, citations,
        serviceState: r.service_state ?? 'hit' });
      return { attempted: true, saved, snapshot: row, service_state: r.service_state ?? 'hit' };
    } catch (e) {
      await store.audit('RAG_WORKER_SAVE_FAILED', actor, {
        detail: { reason: 'snapshot_save_failed', service_state: r.service_state ?? 'hit',
          error: err1line(e) },
      }).catch(() => {});
      return { attempted: true, saved: false,
        service_state: r.service_state ?? 'hit', save_error: err1line(e) };
    }
  } catch (e) {
    return { attempted: false, reason: 'internal', error: err1line(e) };
  }
}

// 诊断辅助（隔离验证/运维用）：读回某 run 的执行期快照与审计行数。
export async function readbackWorkerSnapshot(pool, { runId, corpusBranch }) {
  const snapshot = await loadRunRagSnapshot(
    { query: (t, p) => pool.query(t, p) }, { runId, corpusBranch: String(corpusBranch || 'main') });
  return snapshot;
}
