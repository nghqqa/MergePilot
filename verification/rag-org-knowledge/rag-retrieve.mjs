// rag-retrieve.mjs — worker rag_retrieve 的受控调用客户端（A 链）。
// 硬边界：结果仅为组织规范参考——不产生 finding、不写 gate/stage/ticket/receipt。
// feature flag（隔离 staging 显式启用）：MERGEPILOT_ORG_RAG_A_CHAIN=1；
// 服务不可达/损坏 → 显式 degraded 输出，绝不静默伪装成功检索。
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const AUDIT = process.env.ORG_RAG_AUDIT || path.join(__dirname, 'audit', 'worker-retrievals.jsonl');
const liveUrl = () => process.env.ORG_RAG_LIVE_URL || 'http://127.0.0.1:48210';

export async function ragRetrieve({ query, runId, k = 5, actor = 'worker' }) {
  const flag = process.env.MERGEPILOT_ORG_RAG_A_CHAIN === '1';
  if (!flag) {
    return { ok: false, service_state: 'a_chain_disabled',
      note: 'A 链未启用（feature flag MERGEPILOT_ORG_RAG_A_CHAIN 非 1）——不伪装检索' };
  }
  let res;
  try {
    const url = `${liveUrl()}/api/rag/search?q=${encodeURIComponent(query)}&k=${k}`
      + (runId ? `&run_id=${encodeURIComponent(runId)}` : '');
    res = await fetch(url);
  } catch (e) {
    const rec = { snapshot_id: null, query_hash: createHash('sha256').update(query).digest('hex').slice(0, 32),
      source_refs: [], service_state: 'degraded', degraded_reason: 'service_unreachable',
      run_id: runId ?? null, actor, error: String(e.cause?.code || e.message).slice(0, 80),
      at: new Date().toISOString() };
    audit(rec);
    return { ok: false, service_state: 'degraded', degraded_reason: 'service_unreachable', audit: rec };
  }
  const body = await res.json().catch(() => ({}));
  const rec = {
    snapshot_id: body.snapshot_id ?? null, corpus_digest: body.corpus_digest ?? null,
    query_hash: body.query_hash ?? createHash('sha256').update(query).digest('hex').slice(0, 32),
    source_refs: body.source_refs ?? [], service_state: body.service_state ?? 'unknown',
    http_status: res.status, run_id: runId ?? null, actor,
    degraded_reason: body.degraded_reason, at: new Date().toISOString(),
  };
  audit(rec);
  return {
    ok: res.status === 200 && body.service_state === 'ok',
    service_state: body.service_state, http_status: res.status,
    retrieval_version: body.retrieval_version, snapshot_id: body.snapshot_id,
    corpus_digest: body.corpus_digest, source_refs: body.source_refs,
    results: (body.results ?? []).map((r) => ({ ...r, knowledge_type: 'org_knowledge' })),
    empty_reason: body.empty_reason, degraded_reason: body.degraded_reason,
    // 风险隔离声明：仅组织规范参考，不构成 finding/gate/ticket 输入
    usage_note: 'org-standard reference only — MUST NOT create findings or alter gate/stage/ticket/success',
  };
}

function audit(rec) {
  try { fs.mkdirSync(path.dirname(AUDIT), { recursive: true }); fs.appendFileSync(AUDIT, JSON.stringify(rec) + '\n'); }
  catch { /* 审计写失败时响应中仍携带记录 */ }
}

// CLI: node rag-retrieve.mjs <query> <run_id>
const isMain = process.argv[1] && import.meta.url === new URL(`file://${path.resolve(process.argv[1]).replace(/\\/g, '/')}`).href;
if (isMain) {
  const [query, runId] = process.argv.slice(2);
  ragRetrieve({ query: query || '密码 轮换', runId: runId || null })
    .then((r) => { console.log(JSON.stringify(r, null, 1)); process.exit(r.ok ? 0 : 3); });
}
