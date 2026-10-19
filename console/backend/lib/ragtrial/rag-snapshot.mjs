// rag-snapshot.mjs — run 页知识库引用快照（RAG V2：审查时获取并持久化）。
//
// 合同（不变量，rag-snapshot.test.mjs 锁定）：
//  * 幂等：同 (run_id, corpus_branch, model_id, index_version) 重复保存=单条 upsert；
//  * 不可变：已保存快照的知识库后续更新/删除/重索引不得改写历史引用；
//  * reference only：不构成 finding/ticket/gate/VERIFIED/fixer/verifier 输入；
//  * 旧 run 无快照如实返回（不用当前查询补造历史）。
import { createRagTrialStore } from './store.mjs';
import { toAuxEvidence, RAG_EVIDENCE_KIND } from './review.mjs';

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

// 查询构造（纯函数）：发现标题拼接（finding_titles_v1 策略），400 字符截断。
export function buildSnapshotQuery(prTitle, findings) {
  const parts = [];
  if (prTitle) parts.push(String(prTitle));
  for (const f of (findings ?? [])) {
    if (f?.title) parts.push(String(f.title));
  }
  const joined = parts.join('；').slice(0, 400);
  return joined || null;
}

// 快照保存（幂等 upsert）：同 (run_id, corpus_branch, model_id, index_version)
// 重复调用不产生第二条；语料后续变化不改写已保存 citations。
export async function saveRunRagSnapshot(pool, { runId, tenantId, repo, branch,
  reviewHead, corpusBranch, modelId, modelDigest, indexVersion, queryText,
  queryStrategy = 'finding_titles_v1', citations, serviceState }) {
  const r = await pool.query(
    `INSERT INTO ragtrial.run_rag_snapshots
       (run_id, tenant_id, repo, branch, review_head, corpus_branch,
        model_id, model_digest, index_version, query_text, query_strategy,
        citations, service_state)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13)
     ON CONFLICT (run_id, corpus_branch, model_id, index_version)
     DO UPDATE SET citations=EXCLUDED.citations,
                   service_state=EXCLUDED.service_state,
                   snapshot_at=now()
     RETURNING *`,
    [runId, tenantId, repo, branch, reviewHead, corpusBranch,
      modelId, modelDigest, indexVersion, queryText, queryStrategy,
      JSON.stringify(citations ?? []), serviceState]);
  return r.rows[0] ?? null;
}

// 快照读取：按 (run_id, corpus_branch) 取最新——旧 run 无快照如实返回 null。
export async function loadRunRagSnapshot(pool, { runId, corpusBranch }) {
  const r = await pool.query(
    `SELECT * FROM ragtrial.run_rag_snapshots
      WHERE run_id=$1 AND corpus_branch=$2
      ORDER BY snapshot_at DESC LIMIT 1`,
    [runId, corpusBranch]);
  return r.rows[0] ?? null;
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
