import React from 'react';
import { Badge } from './ui.jsx';
import { executionMap, verdictMap, gateMap, publishMap } from './status-map.js';

// 状态徽章：全部语义来自 status-map.js（纯模块，node --test 覆盖）。
// 颜色永不单独承载状态：色点 + 文字 + title 解释三元组。

export function ExecutionBadge({ execution }) {
  const m = executionMap(execution);
  const source =
    execution?.source === 'delivery_ledger' ? '｜来源 delivery-ledger.json'
      : execution?.source === 'project_meta' ? '｜来源 project/meta.json（Matrix 轮无台账）' : '';
  return <Badge tone={m.tone} title={`${m.note}${source}`}>{m.label}</Badge>;
}

export function VerdictBadge({ review }) {
  const m = verdictMap(review);
  return <Badge tone={m.tone} title={m.note}>{m.label}</Badge>;
}

export function GateBadge({ gate, source }) {
  const m = gateMap(gate, source);
  return <Badge tone={m.tone} title={m.note}>{m.label}</Badge>;
}

export function PublishBadge({ publish }) {
  const m = publishMap(publish);
  return <Badge tone={m.tone} title={m.note}>{m.label}</Badge>;
}

export function RagStateBadge({ state }) {
  const map = {
    called: ['info', 'RAG 已调用', '本次运行存在 RAG 检索调用记录（见下方明细）'],
    no_calls: ['neutral', 'RAG 未调用', '存在 RAG 记录文件但无调用'],
    counted_only: ['info', 'RAG 仅计数', '仅有 span 计数，无逐条记录'],
    not_called: ['neutral', 'RAG 未调用', 'span 汇总显示本次未调用 RAG'],
    insufficient_data: ['neutral', 'RAG 数据不足', '包内无 RAG 相关数据，无法判断 — 与"服务不可用"不同'],
  };
  const [tone, label, desc] = map[state] ?? ['neutral', `RAG ${state}`, state];
  return <Badge tone={tone} title={desc}>{label}</Badge>;
}
