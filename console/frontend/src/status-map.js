// status-map.js — 状态语义映射（纯函数，无 JSX；node --test 可直接验证）
//
// 每条映射 = { tone, label, note }：
//   tone  语义色档：ok(明确正向) / info(进行或授权) / warn(等待·中止·覆盖不足) /
//         bad(确认的高风险或执行错误) / neutral(普通事实·完成态·缺失)
//   label 列表显示文案（≤8 字，附原始枚举于 note）
//   note  可访问解释（badge title / 详情说明），写明"这是什么、不是什么、来自哪个文件"
//
// 语义红线（提示词六）：PROCESSED≠审查通过；COMPLETED≠无问题；APPROVED≠已修复/已合并；
// check conclusion=failure≠回写失败；无发布记录≠一定未回写；结论缺失≠无问题。

const EXECUTION = {
  PROCESSED: {
    tone: 'neutral',
    label: '处理完成',
    note: '投递处理完成（delivery-ledger: PROCESSED）— 投递生命周期事实，不代表审查通过或检查通过',
  },
  RUNNING: { tone: 'info', label: '处理中', note: '投递处理中（delivery-ledger: RUNNING）' },
  PENDING: { tone: 'neutral', label: '待处理', note: '投递等待认领（delivery-ledger: PENDING）' },
  CLAIMED: { tone: 'info', label: '已认领', note: '桥已认领该投递（delivery-ledger: CLAIMED）' },
  ERROR: { tone: 'bad', label: '处理出错', note: '投递处理出错（delivery-ledger: ERROR）— 需人工区分可恢复/人工处理，详见投递备注' },
  COMPLETED: {
    tone: 'neutral',
    label: '执行完成',
    note: '项目全部节点执行完毕（project/meta: completed）— 执行事实，不代表无安全问题',
  },
  BLOCKED: {
    tone: 'warn',
    label: '已阻断',
    note: '流程被阻断停止（project/meta: blocked）— 人工拒绝或验证失败后的受控停止，PR 保持 OPEN',
  },
};

const VERDICT_SEVERITY_TONE = { CRITICAL: 'bad', HIGH: 'bad', MEDIUM: 'warn', LOW: 'warn' };

const GATE = {
  APPROVED: {
    tone: 'info',
    label: '已批准修复',
    note: '人工批准了修复授权（gate 记录: APPROVED）— 仅授权修复流程，不代表问题已修复、更不代表已合并',
  },
  REJECTED: {
    tone: 'warn',
    label: '已拒绝修复',
    note: '人工拒绝修复授权（gate 记录: REJECTED）— 流程受控停止，后续任务锁定，PR 保持 OPEN',
  },
  NOT_REQUIRED: {
    tone: 'neutral',
    label: '无需人工确认',
    note: 'NOT_REQUIRED — 低风险自动路径，未触发人工安全门',
  },
  RECORDED: {
    tone: 'info',
    label: '有确认记录',
    note: '包内存在人工门决策记录（RECORDED）— 具体结论见证据文件',
  },
};

// publish.status ∈ published | processed_no_checkrun_record | not_recorded
// （publish.conclusion 仅在 published 时有值，是 GitHub check-run 的 conclusion）
function publishMap(publish) {
  if (!publish) return { tone: 'neutral', label: '无发布记录', note: '该运行无发布状态对象（数据缺失）' };
  if (publish.status === 'published') {
    const c = String(publish.conclusion ?? 'unknown');
    if (c === 'success') {
      return {
        tone: 'ok',
        label: '已回写 · 检查通过',
        note: `check-run 已成功回写 GitHub（id ${publish.check_run_id}），conclusion=success — 回写成功不证明补丁已验证或已合并`,
      };
    }
    if (c === 'failure') {
      return {
        tone: 'warn',
        label: '已回写 · 检查未通过',
        note: `check-run 已成功回写 GitHub（id ${publish.check_run_id}），conclusion=failure — 回写通道成功；"检查未通过"是审查结论事实，不是回写失败`,
      };
    }
    return {
      tone: 'info',
      label: `已回写 · ${c}`,
      note: `check-run 已回写 GitHub（id ${publish.check_run_id}），conclusion=${c}`,
    };
  }
  if (publish.status === 'processed_no_checkrun_record') {
    return {
      tone: 'warn',
      label: '无发布记录',
      note: '投递台账为 PROCESSED，但证据包内未随附 check-run 记录 — 无法确认回写细节；不代表未回写',
    };
  }
  return {
    tone: 'neutral',
    label: '未找到发布记录',
    note: '包内无发布记录（Matrix 手动轮为设计上的零 GitHub 写入；webhook 轮缺失属历史证据不完整）— 不推导为"未回写"',
  };
}

function executionMap(execution) {
  if (!execution?.status) {
    return { tone: 'neutral', label: '执行未记录', note: '该运行无投递台账/项目状态记录（早期证据包）' };
  }
  return EXECUTION[String(execution.status).toUpperCase()] ?? {
    tone: 'neutral', label: execution.status, note: `原始枚举：${execution.status}`,
  };
}

function verdictMap(review) {
  if (!review?.verdict) {
    return { tone: 'neutral', label: '结论未记录', note: '包内无独立审查结论记录 — 不等于"无问题"' };
  }
  const v = String(review.verdict);
  if (v === 'FINDING_CONFIRMED' || v === 'finding-confirmed') {
    const sev = review.severity ?? '未分级';
    const tone = VERDICT_SEVERITY_TONE[sev] ?? 'warn';
    return {
      tone,
      label: `确认发现 · ${sev}`,
      note: `独立审查确认存在安全发现（severity=${sev}${review.cwe ? `，${review.cwe}` : ''}）— 来源 ${review.source ?? 'reviewer 结果'}；发现确认不等于已修复`,
    };
  }
  if (v === 'NOT_CONFIRMED' || v === 'pass') {
    return {
      tone: 'ok',
      label: '未发现问题',
      note: '独立审查未确认新的安全问题（NOT_CONFIRMED）— 指"本次审查未发现"，不等于绝对无风险，也不等于已发布/已合并',
    };
  }
  if (v === 'rejected' || v === 'blocked') {
    return {
      tone: 'bad',
      label: '审查未放行',
      note: `check-run 摘要结论为 ${v} — 审查层面最终未放行（人工拒绝或阻断）`,
    };
  }
  return { tone: 'neutral', label: v, note: `原始枚举：${v}` };
}

function gateMap(gate, source) {
  if (!gate) {
    return { tone: 'neutral', label: '无确认记录', note: '包内无人工门/人工确认记录' };
  }
  const entry = GATE[gate] ?? { tone: 'neutral', label: gate, note: `原始枚举：${gate}` };
  if (source) return { ...entry, note: `${entry.note}｜来源 ${source}` };
  return entry;
}

export { executionMap, verdictMap, gateMap, publishMap, EXECUTION, GATE };
