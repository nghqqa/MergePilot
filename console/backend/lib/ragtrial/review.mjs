// ragtrial/review.mjs — Review 联动的安全边界（LOCAL_RAG_TRIAL 硬边界）。
//
// 合同（本试验不可违反）：
//  1. RAG 结果只能作为 Review 的"辅助引用证据"（reference only）；
//  2. RAG 结果永远不能自动变成 finding / ticket / gate / VERIFIED；
//  3. Fixer 不得只依据 RAG 文本修改代码 —— fixerPatchInputs 显式剔除 rag 证据；
//  4. Verifier 只接受独立 harness/test 证据 —— verifierAccepts 对 rag 恒 false；
//  5. 以上由本模块纯函数实现，并有 ragtrial-review.test.mjs 契约测试锁死。
//     （真实 worker 栈的强制不在本波范围，见报告"与生产 C 链差异"。）
// 机器字段契约：reason 一律稳定英文机器码；中文解释放 note（语言策略见 docs/GLOSSARY.md）。

export const RAG_EVIDENCE_KIND = 'rag_auxiliary';
// 排除清单（单一权威常量；所有提及处与本清单同步，含 VERIFIED——
// RAG 证据不得作为 VERIFIED 判定输入，验证只认独立测试证据）
export const RAG_EXCLUDED_FROM = ['finding', 'ticket', 'gate', 'VERIFIED', 'fixer_patch_input', 'verifier_evidence'];

export function toAuxEvidence(hit, { runId = null } = {}) {
  if (!hit?.citation) throw new Error('toAuxEvidence: hit without citation refused');
  return {
    kind: RAG_EVIDENCE_KIND,
    run_id: runId,
    usage: 'reference_only',
    trusted: false,
    citation: hit.citation,
    score: hit.score,
    snippet: hit.snippet,
    policy: {
      may_auto_promote: false,
      excluded_from: RAG_EXCLUDED_FROM,
      note: 'RAG 检索结果仅作人工参考引用；不得作为 finding/ticket/gate/VERIFIED 的自动输入',
    },
  };
}

// 任何证据（含 rag）请求自动晋升 → 恒拒绝（纯策略函数，无例外路径）
export function canAutoPromote(evidence) {
  const reasons = [];
  const items = Array.isArray(evidence) ? evidence : [evidence];
  if (items.some((e) => e?.kind === RAG_EVIDENCE_KIND)) {
    reasons.push('rag_evidence_is_reference_only');
  }
  if (items.some((e) => e?.kind === undefined)) reasons.push('evidence_kind_missing');
  return { allowed: false, auto_promote: 'disabled_by_policy', reasons };
}

// Fixer 输入过滤：剔除 rag 证据；若剔除后为空 → fixer 不可启动（不得只靠 RAG 改码）
export function fixerPatchInputs(evidenceList) {
  const items = Array.isArray(evidenceList) ? evidenceList : [];
  const allowed = items.filter((e) => e?.kind !== RAG_EVIDENCE_KIND);
  const rejected = items.filter((e) => e?.kind === RAG_EVIDENCE_KIND);
  return {
    allowed,
    rejected,
    fixer_may_run: allowed.length > 0,
    reason: allowed.length ? null : 'no_non_rag_evidence',
    note: allowed.length ? null : 'Fixer 不得只依据 RAG 文本修改代码——剔除 rag 证据后无可用输入',
  };
}

// Verifier 证据白名单：只接受独立 harness/test 证据
export const VERIFIER_ACCEPTED_KINDS = ['harness_report', 'test_evidence', 'independent_run_log'];
export function verifierAccepts(evidence) {
  const accepted = VERIFIER_ACCEPTED_KINDS.includes(evidence?.kind);
  return {
    accepted,
    reason: accepted ? null : `verifier_rejected_kind:${evidence?.kind ?? 'unknown'}`,
    note: accepted ? null : `Verifier 只接受 ${VERIFIER_ACCEPTED_KINDS.join('/')}（拒绝 ${evidence?.kind ?? 'unknown'}）`,
  };
}

// 生成 finding/ticket/gate 的统一入口守卫：RAG 证据直接拒绝
const PROMOTION_TARGETS = ['finding', 'ticket', 'gate'];
export function promotionRequest({ target, evidence }) {
  if (!PROMOTION_TARGETS.includes(target)) {
    return { allowed: false, reason: `unknown_promotion_target:${target}` };
  }
  const items = Array.isArray(evidence) ? evidence : [evidence];
  const ragOnly = items.length > 0 && items.every((e) => e?.kind === RAG_EVIDENCE_KIND);
  if (ragOnly) {
    return {
      allowed: false,
      reason: `target=${target}: rag_evidence_cannot_auto_promote`,
      note: 'RAG 证据为 reference-only，不可自动晋升',
    };
  }
  // 本试验栈不提供任何自动晋升路径：即使非 rag 证据也只回"需人工评审"
  return {
    allowed: false,
    reason: `target=${target}: manual_review_only`,
    note: '本试验栈无自动晋升路径——任何晋升都需人工评审',
  };
}

// 把辅助证据挂到 run 视图：只新增 review_context.rag_auxiliary 字段，
// findings/tickets/gates 原样透传（绝不改写）。
export function attachToRun(runView, auxList) {
  const items = Array.isArray(auxList) ? auxList : [];
  if (items.some((e) => e?.kind !== RAG_EVIDENCE_KIND)) {
    throw new Error('attachToRun: only rag_auxiliary evidence allowed here');
  }
  return {
    ...runView,
    review_context: {
      ...(runView?.review_context ?? {}),
      rag_auxiliary: items,
      rag_policy_note: 'reference only — 不构成 finding/ticket/gate/VERIFIED 输入',
    },
  };
}
