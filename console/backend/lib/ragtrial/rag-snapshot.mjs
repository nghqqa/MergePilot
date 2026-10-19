// rag-snapshot.mjs — run 页知识库引用快照（RAG V2：审查执行期获取并持久化）。
//
// 写入纪律（#491 独立复核修正）：唯一写入方 = 审查 worker 执行期（worker-rag.mjs，
// 由 review-service 在真实审查链内调用）。页面 review-aux 实时查询【不写本表】——
// 动态查询不得创建或覆盖「审查执行时快照」，旧 run 不通过打开页面补造历史。
//
// 合同（不变量，rag-snapshot.test.mjs / rag-snapshot-pool.integration.mjs 锁定）：
//  * 幂等不覆盖：UNIQUE (run_id, corpus_branch)，同一次任务重试首写即定，
//    ON CONFLICT DO NOTHING——已保存结果绝不被后续调用改写；
//  * 不可变：已保存快照的知识库后续更新/删除/重索引不得改写历史引用；
//  * 检索成功 ≠ 保存成功：保存失败时无行可读（页面如实显示「未记录」），
//    另行写 query_log 审计（RAG_WORKER_SAVE_FAILED）供诊断，不伪造已留存；
//  * 任务身份全部服务端同源：run/tenant/repo/head/base_ref 来自真实审查任务，
//    绝不采用客户端自报值（请求自带 run_id 不作为绑定证明）；
//  * reference only：不构成 finding/ticket/gate/VERIFIED/fixer/verifier 输入；
//  * 旧 run 无快照如实返回（不用当前查询补造历史）。
import { createRagTrialStore } from './store.mjs';
import { toAuxEvidence, RAG_EVIDENCE_KIND } from './review.mjs';
import { sessionRepoAllowed } from './api.mjs';

let storeSingleton = null;
let storeEnv = undefined;
export function resetRagSnapshotStore() { storeSingleton = null; storeEnv = undefined; }

export async function getRagSnapshotStore(env) {
  if (!storeSingleton || storeEnv !== env) {
    storeSingleton = await createRagTrialStore({ pool: null, env });
    storeEnv = env;
  }
  return storeSingleton;
}

// 快照保存（首写即定，幂等不覆盖）：UNIQUE (run_id, corpus_branch) 冲突时
// DO NOTHING——返回 { saved:true, row }（本次写入）或 { saved:false, row:既有行 }
// （同 key 已有快照，本调用为重试/重复获取——既有结果保持不变）。
export async function saveRunRagSnapshot(pool, { runId, tenantId, repo, branch,
  reviewHead, baseRef = null, corpusBranch, modelId, modelDigest, indexVersion,
  queryText, queryStrategy = 'pr_context_v1', citations, serviceState }) {
  const ins = await pool.query(
    `INSERT INTO ragtrial.run_rag_snapshots
       (run_id, tenant_id, repo, branch, review_head, base_ref, corpus_branch,
        model_id, model_digest, index_version, query_text, query_strategy,
        citations, service_state)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14)
     ON CONFLICT (run_id, corpus_branch) DO NOTHING
     RETURNING *`,
    [runId, tenantId, repo, branch, reviewHead, baseRef, corpusBranch,
      modelId, modelDigest, indexVersion, queryText, queryStrategy,
      JSON.stringify(citations ?? []), serviceState]);
  if (ins.rows[0]) return { saved: true, row: ins.rows[0] };
  const cur = await pool.query(
    `SELECT * FROM ragtrial.run_rag_snapshots WHERE run_id=$1 AND corpus_branch=$2`,
    [runId, corpusBranch]);
  return { saved: false, row: cur.rows[0] ?? null };
}

// 快照读取：按 (run_id, corpus_branch) 取唯一行——旧 run 无快照如实返回 null。
export async function loadRunRagSnapshot(pool, { runId, corpusBranch }) {
  const r = await pool.query(
    `SELECT * FROM ragtrial.run_rag_snapshots
      WHERE run_id=$1 AND corpus_branch=$2
      ORDER BY snapshot_at DESC LIMIT 1`,
    [runId, corpusBranch]);
  return r.rows[0] ?? null;
}

// 快照读取授权（纯函数，#491 复核补齐）：会话租户必须与快照 tenant_id 一致，
// 且会话授权仓库面含快照 repo（与 api.mjs sessionRepoAllowed 同一契约）。
// 两层缺一不可；不满足 → 403（不泄露存在性以外的任何内容）。
export function authorizeSnapshotRead(auth, snap) {
  if (!auth?._mu?.tenantId || !snap?.tenant_id
      || String(auth._mu.tenantId) !== String(snap.tenant_id)) {
    return { ok: false, reason: 'run_tenant_mismatch' };
  }
  if (!sessionRepoAllowed(auth, snap.repo)) {
    return { ok: false, reason: 'repo_not_in_allowlist' };
  }
  return { ok: true };
}

// 引用条目规范化：toAuxEvidence 输出 + 语料分支标注（页面双源区分用）。
export function toSnapshotCitations(searchResults) {
  return (searchResults ?? []).map((h) => {
    const ev = { ...toAuxEvidence(h) };
    ev.citation.corpus_branch = h.citation?.branch ?? null;
    return ev;
  });
}

export { RAG_EVIDENCE_KIND };
