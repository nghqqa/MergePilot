// rag-metric-chip.js — RAG 指标徽章的诚实呈现（rc8.2 前端修复）。
// 缺陷史：错误态（provider_unavailable 等）按全期累计计数 + tone:'bad' 渲染，
// 三周前的一次历史失败被渲染得像"当前故障"（误导）。
// 语义：错误态计数是【累计值】——若整体最新事件晚于该错误态最新事件，说明此后
// 已有成功检索，应以"历史"中性呈现并标注最近发生时间；否则维持警示色并标注时间。
import { ragMap } from './status-map.js';

export const RAG_ERROR_STATES = new Set(['provider_unavailable', 'error', 'degraded']);

function fmtTime(iso) {
  try {
    const d = new Date(iso);
    if (!Number.isFinite(d.getTime())) return String(iso);
    return d.toLocaleString('zh-CN', { hour12: false });
  } catch { return String(iso); }
}

/**
 * @param {string} state 查询状态（ragtrial 键空间）
 * @param {number} n 该状态累计次数
 * @param {string|null} latest 该状态最近一次发生时间（ISO）
 * @param {string|null} latestOverall 整体最近一次事件时间（ISO）
 * @returns {{state:string,n:number,label:string,tone:string,text:string,note:string}}
 */
export function metricChipFor(state, n, latest, latestOverall) {
  const m = ragMap(state);
  const base = { state, n, label: m.label, note: m.note };
  if (!RAG_ERROR_STATES.has(state)) {
    return { ...base, tone: m.tone, text: `${m.label} × ${n}` };
  }
  const latestMs = latest ? Date.parse(latest) : NaN;
  const overallMs = latestOverall ? Date.parse(latestOverall) : NaN;
  const hasTime = Number.isFinite(latestMs);
  const recovered = hasTime && Number.isFinite(overallMs) && overallMs > latestMs;
  if (recovered) {
    return { ...base, tone: 'neutral',
      text: `${m.label} × ${n}（历史 · 最近 ${fmtTime(latest)}；此后检索正常）`,
      note: `${m.note}（历史累计：最近一次之后检索已正常）` };
  }
  return { ...base, tone: m.tone,
    text: `${m.label} × ${n}${hasTime ? `（最近 ${fmtTime(latest)}）` : ''}`,
    note: m.note + (hasTime ? `（最近发生 ${fmtTime(latest)}）` : '') };
}
