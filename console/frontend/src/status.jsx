import React from 'react';
import { Badge } from './ui.jsx';

// 三态语义映射 —— 每个徽章的 title 都写明状态来源与含义，
// 防止"审查完成/发布成功/审批通过"被读成同一个成功。

export function ExecutionBadge({ execution }) {
  if (!execution?.status) {
    return <Badge tone="neutral" title="该运行无投递台账记录（早期/Matrix 轮）">执行状态 未记录</Badge>;
  }
  const s = String(execution.status).toUpperCase();
  const source =
    execution.source === 'delivery_ledger' ? '投递台账 delivery-ledger' : '项目 meta（Matrix 轮，无台账）';
  const map = {
    PROCESSED: ['ok', '投递 PROCESSED — 结果已发布并记账'],
    RUNNING: ['info', '投递 RUNNING — 处理中'],
    PENDING: ['neutral', '投递 PENDING — 等待认领'],
    CLAIMED: ['info', '投递 CLAIMED — 桥已认领'],
    ERROR: ['bad', '投递 ERROR — 需人工处理（note 前缀区分可恢复/人工）'],
    COMPLETED: ['ok', '项目 completed — 全部 DAG 节点完成（Matrix 轮）'],
    BLOCKED: ['bad', '项目 blocked — 流程被阻断（人工拒绝或验证失败）'],
  };
  const [tone, desc] = map[s] ?? ['neutral', `状态 ${s}`];
  return (
    <Badge tone={tone} title={`${desc}｜来源：${source}`}>
      {s}
    </Badge>
  );
}

export function VerdictBadge({ review }) {
  if (!review?.verdict) {
    return <Badge tone="neutral" title="包内无审查结论记录">结论 未记录</Badge>;
  }
  if (review.verdict === 'FINDING_CONFIRMED' || review.verdict === 'finding-confirmed') {
    return (
      <Badge tone="bad" title={`独立审查确认存在安全发现｜严重度 ${review.severity ?? '未记录'}${review.cwe ? `｜${review.cwe}` : ''}`}>
        发现确认{review.severity ? ` · ${review.severity}` : ''}
      </Badge>
    );
  }
  if (review.verdict === 'NOT_CONFIRMED' || review.verdict === 'pass') {
    return (
      <Badge tone="ok" title="独立审查未确认新的安全发现（不等于发布成功，也不等于审批通过）">
        无发现{review.severity ? ` · ${review.severity}` : ''}
      </Badge>
    );
  }
  if (review.verdict === 'rejected' || review.verdict === 'blocked') {
    return (
      <Badge tone="bad" title="check-run 摘要标记为 rejected/blocked — 审查层面最终未放行（人工拒绝或阻断）">
        {review.verdict === 'rejected' ? '已拒绝' : '已阻断'}
      </Badge>
    );
  }
  return <Badge tone="neutral">{review.verdict}</Badge>;
}

export function GateBadge({ gate }) {
  if (!gate) return <Badge tone="neutral" title="包内无人工门记录">门 未记录</Badge>;
  const map = {
    APPROVED: ['ok', '人工门已批准（仅授权修复流程，非代码推送）'],
    REJECTED: ['bad', '人工门已拒绝 — 后续 Agent 保持锁定'],
    NOT_REQUIRED: ['neutral', '低风险自动路径，未触发人工门'],
    RECORDED: ['info', '存在门决策记录，结论见证据文件'],
  };
  const [tone, desc] = map[gate] ?? ['neutral', gate];
  return <Badge tone={tone} title={desc}>{gate}</Badge>;
}

export function PublishBadge({ publish }) {
  if (!publish) return <Badge tone="neutral">发布 未记录</Badge>;
  if (publish.status === 'published') {
    const ok = publish.conclusion === 'success';
    return (
      <Badge tone={ok ? 'ok' : 'bad'} title={`GitHub check-run ${publish.conclusion}（id ${publish.check_run_id}）— 发布事实与审查结论相互独立`}>
        check-run {publish.conclusion}
      </Badge>
    );
  }
  if (publish.status === 'processed_no_checkrun_record') {
    return (
      <Badge tone="warn" title="台账 PROCESSED 但包内无 check-run.json — 该轮发布凭据未随包保存">
        已处理·回写未随包记录
      </Badge>
    );
  }
  return <Badge tone="neutral" title="该轮为 Matrix 手动触发或无 GitHub 回写（零 GitHub 写入设计）">未回写 GitHub</Badge>;
}

export function RagStateBadge({ state }) {
  const map = {
    called: ['info', '本次运行存在 RAG 检索调用记录（见下方明细）'],
    no_calls: ['neutral', '存在 RAG 记录文件但无调用'],
    counted_only: ['info', '仅有 span 计数，无逐条记录'],
    not_called: ['neutral', 'span 汇总显示本次未调用 RAG'],
    insufficient_data: ['neutral', '包内无 RAG 相关数据，无法判断'],
  };
  const [tone, desc] = map[state] ?? ['neutral', state];
  const label = {
    called: '已调用',
    no_calls: '未调用',
    counted_only: '仅计数',
    not_called: '未调用',
    insufficient_data: '数据不足',
  }[state] ?? state;
  return <Badge tone={tone} title={desc}>{`RAG ${label}`}</Badge>;
}
