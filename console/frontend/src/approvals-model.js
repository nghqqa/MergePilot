// approvals-model.js — 审批交互的纯状态机（node --test 可直接验证）。
//
// 边界：本模块不做任何写操作、不访问任何接口。真实批准/拒绝仅在后端决策接口
// 与权限就绪后启用（INTEGRATION-REQUESTS C-11 提案；票据存储已拍板 SQLite WAL = C-4/P-1）。
// fixture 模式下的"提交"只是本页内存变换，作用于明确的合成测试数据。

// 提交前客户端预检：镜像后端必须强制的规则；预检通过不代表服务端接受。
export function validateDecision(ticket, { decision, now, assumeCurrentHead } = {}) {
  if (!ticket || ticket.status !== 'PENDING') {
    return { ok: false, code: 'NOT_PENDING', message: '票据不在待审批状态' };
  }
  if (decision !== 'APPROVED' && decision !== 'REJECTED') {
    return { ok: false, code: 'BAD_DECISION', message: '决策值非法' };
  }
  if (ticket.expires_at && Date.parse(ticket.expires_at) <= (now ?? Date.now())) {
    return { ok: false, code: 'EXPIRED', message: '票据已过有效期，需后端重签' };
  }
  // head 一致性：仅当存在权威当前 head 时才能判定；snapshot 无权威（C-10）时跳过该项
  if (assumeCurrentHead != null && ticket.head_sha !== assumeCurrentHead) {
    return { ok: false, code: 'HEAD_CONFLICT', message: 'PR 当前 head 与票据绑定 head 不一致，需刷新权威状态' };
  }
  return { ok: true };
}

// 单张票据的提交 UI 状态机：
// idle → submitting → decided | conflict | expired | unknown → (REFRESH) → idle
// 提交超时进入 unknown：必须先查询服务端实际结果，禁止直接判定失败并盲目重发。
export function submissionReducer(state, event) {
  const s = state ?? { phase: 'idle' };
  switch (event.type) {
    case 'SUBMIT':
      if (s.phase !== 'idle') return s; // 防重复点击：仅 idle 可发起
      return { phase: 'submitting', decision: event.decision };
    case 'RESOLVE':
      if (s.phase !== 'submitting') return s;
      return { phase: 'decided', decision: s.decision, outcome: event.outcome, at: event.at ?? null };
    case 'CONFLICT':
      if (s.phase !== 'submitting') return s;
      return { phase: 'conflict', decision: s.decision };
    case 'EXPIRE':
      if (s.phase !== 'submitting') return s;
      return { phase: 'expired', decision: s.decision };
    case 'TIMEOUT':
      if (s.phase !== 'submitting') return s;
      return { phase: 'unknown', decision: s.decision };
    case 'QUERY':
      if (s.phase !== 'unknown') return s;
      if (event.outcome === 'approved' || event.outcome === 'rejected') {
        return { phase: 'decided', decision: s.decision, outcome: event.outcome, via: 'query' };
      }
      if (event.outcome === 'conflict') return { phase: 'conflict', decision: s.decision, via: 'query' };
      if (event.outcome === 'expired') return { phase: 'expired', decision: s.decision, via: 'query' };
      return s;
    case 'REFRESH':
      return { phase: 'idle' };
    default:
      return s;
  }
}

export const SUBMIT_PHASE_LABEL = {
  idle: '待提交',
  submitting: '提交中…',
  decided: '已记录',
  conflict: '冲突（head 已更新）',
  expired: '票据过期',
  unknown: '结果未知 — 请先查询服务端状态',
};
