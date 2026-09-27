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

// 严重度（独立键空间）：审查发现 severity 的唯一权威映射（PendingPage 筛选/着色/文案同源）。
const SEVERITY = {
  CRITICAL: { tone: 'bad', label: '危急', note: '危急（CRITICAL）— 最高严重级，必须优先处理' },
  HIGH: { tone: 'bad', label: '高', note: '高风险（HIGH）' },
  MEDIUM: { tone: 'warn', label: '中', note: '中风险（MEDIUM）' },
  LOW: { tone: 'warn', label: '低', note: '低风险（LOW）' },
};
const SEVERITY_ORDER = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'];

// ── FXV 修复编排状态机（独立键空间；权威定义 lib/fxv/orchestrator.mjs STATES，23 态）──
// 语义红线：VERIFIED=独立测试证据支撑的成功终态；DRY_RUN_*=隔离验证，≠生产验证；
// APPROVED 在 FXV 语境=审批通过（进入补丁生成），≠问题已修复。
const FXV = {
  FILED: { tone: 'neutral', label: '已立案', note: '票据立案，绑定字段已固化（fxv: FILED）' },
  AWAITING_APPROVAL: { tone: 'warn', label: '待审批', note: '人工等待：审批（fxv: AWAITING_APPROVAL）' },
  APPROVED: { tone: 'info', label: '审批通过', note: '审批通过，将进入补丁生成（fxv: APPROVED）— 不代表问题已修复' },
  REJECTED: { tone: 'warn', label: '已拒绝', note: '人工终态：审批拒绝（fxv: REJECTED）— 受控停止' },
  PATCH_GENERATING: { tone: 'info', label: '生成补丁中', note: 'Fixer 生成补丁（fxv: PATCH_GENERATING）' },
  PATCH_READY: { tone: 'neutral', label: '补丁就绪', note: '补丁已生成待应用（fxv: PATCH_READY）' },
  DRY_RUN_APPLY: { tone: 'info', label: '隔离应用中', note: '隔离工作区应用补丁（fxv: DRY_RUN_APPLY）' },
  DRY_RUN_VERIFIED: { tone: 'ok', label: '隔离验证通过', note: '隔离测试通过（fxv: DRY_RUN_VERIFIED）— 隔离验证，不等于生产验证' },
  DRY_RUN_COMPLETE: { tone: 'ok', label: '试运行完成', note: 'dry-run 终态（fxv: DRY_RUN_COMPLETE，默认路径）— 未执行真实 GitHub 写入' },
  AWAITING_GITHUB_GRANT: { tone: 'warn', label: '待写入授权', note: '人工等待：GitHub 写入授权（fxv: AWAITING_GITHUB_GRANT）' },
  GRANTED: { tone: 'info', label: '已授权写入', note: '写入授权已发放（fxv: GRANTED）— 一次性 grant' },
  COMMITTING: { tone: 'info', label: '提交中', note: '真实提交+推送执行中（fxv: COMMITTING）' },
  COMMITTED: { tone: 'neutral', label: '已提交', note: '真实提交完成，待复核测试（fxv: COMMITTED）' },
  TEST_RUNNING: { tone: 'info', label: '复核测试中', note: '推送后 CI/harness 复核（fxv: TEST_RUNNING）' },
  VERIFIED: { tone: 'ok', label: '已验证', note: '成功终态（fxv: VERIFIED）— 由独立测试证据支撑' },
  EXPIRED: { tone: 'warn', label: '已过期', note: '审批/流程 TTL 到期终态（fxv: EXPIRED）' },
  STALE_HEAD: { tone: 'warn', label: 'head 已漂移', note: 'head 漂移终态（fxv: STALE_HEAD）— run 绑定的 head 已非最新' },
  DIGEST_DRIFT: { tone: 'bad', label: '摘要漂移', note: '补丁摘要漂移终态（fxv: DIGEST_DRIFT）— 绑定不变量被破坏' },
  TEST_FAILED: { tone: 'bad', label: '测试失败', note: '测试失败终态（fxv: TEST_FAILED）' },
  ROLLED_BACK: { tone: 'neutral', label: '已回滚', note: '回滚完成终态（fxv: ROLLED_BACK）' },
  TIMEOUT: { tone: 'bad', label: '已超时', note: '步骤超时终态（fxv: TIMEOUT）' },
  ERROR_FATAL: { tone: 'bad', label: '致命错误', note: '不可恢复错误终态（fxv: ERROR_FATAL）— 需人工介入' },
  MANUAL_WAIT: { tone: 'warn', label: '等待人工', note: '人工等待（fxv: MANUAL_WAIT）— handler 缺失或恢复需人工' },
};

// FXV 工件归档状态（artifact_status；OK/COMPLETE 语义强度：COMPLETE 最强=终态全量校验通过）
const FXV_ARTIFACT = {
  OK: { tone: 'ok', label: '已归档', note: '工件逐条归档成功（artifact: OK）' },
  COMPLETE: { tone: 'ok', label: '归档校验通过', note: '终态全量校验通过（artifact: COMPLETE）— 最强归档状态' },
  FAILED: { tone: 'bad', label: '归档失败', note: '工件归档失败（artifact: FAILED）' },
  NONE: { tone: 'neutral', label: '无工件', note: '该 attempt 无工件（artifact: NONE）' },
  UNKNOWN: { tone: 'neutral', label: '未知', note: '工件状态未知（artifact: UNKNOWN）' },
};

// ── C 链组件状态（独立键空间；权威定义 lib/cchain/index.mjs）──
// 语义红线：缺真实依赖 = BLOCKED/缺失态如实显示，绝不伪装 READY。
const CCHAIN = {
  READY: { tone: 'ok', label: '就绪', note: '组件就绪（cchain: READY）' },
  ATTESTED: { tone: 'ok', label: '已公证', note: 'provider 在线 attestation 通过（cchain: ATTESTED）' },
  NOT_CONFIGURED: { tone: 'neutral', label: '未配置', note: '相关环境变量未设置（cchain: NOT_CONFIGURED）' },
  MISSING: { tone: 'warn', label: '缺失', note: '依赖文件/记录不存在（cchain: MISSING）— 具体条件见 blocked_condition' },
  CORRUPT: { tone: 'bad', label: '损坏', note: '内容寻址校验失败（cchain: CORRUPT）' },
  UNREACHABLE: { tone: 'bad', label: '不可达', note: 'provider 网络不可达（cchain: UNREACHABLE）' },
  INVALID: { tone: 'bad', label: '无效', note: 'attestation 形状校验失败（cchain: INVALID）' },
};
const CCHAIN_OVERALL = {
  READY: { tone: 'ok', label: '全部就绪', note: '三组件均 READY/ATTESTED（overall: READY）' },
  BLOCKED: { tone: 'bad', label: '已阻断', note: '存在未就绪组件（overall: BLOCKED）— 具体条件见 blocked_conditions' },
};

// ── RAG 检索服务状态（独立键空间；权威定义 lib/ragtrial/store.mjs QUERY_STATES + API 层附加态）──
// 语义红线：六状态如实返回，绝不伪装空成功；命中=hit（A 链端点同词发射，不再改写为 ok）。
const RAG = {
  hit: { tone: 'ok', label: '命中', note: '检索命中（ragtrial: hit）— 结果为 reference-only 辅助引用' },
  empty: { tone: 'neutral', label: '无结果', note: '检索无命中（ragtrial: empty）— 如实空，非降级' },
  model_missing: { tone: 'warn', label: '模型未注册', note: '无激活模型（ragtrial: model_missing）— 需先注册/灌入模型' },
  index_stale: { tone: 'warn', label: '索引过期', note: '语料与索引版本不一致（ragtrial: index_stale）— 需 re-ingest' },
  provider_unavailable: { tone: 'bad', label: '嵌入服务不可用', note: '嵌入 provider 不可达（ragtrial: provider_unavailable）' },
  error: { tone: 'bad', label: '检索错误', note: '检索显式失败（ragtrial: error）— 不伪装' },
  backend_not_wired: { tone: 'neutral', label: '后端未接线', note: 'ragtrial 后端未接线（backend_not_wired）' },
  ready: { tone: 'ok', label: '服务就绪', note: 'ragtrial 服务就绪（ready）' },
  degraded: { tone: 'bad', label: '已降级', note: '检索服务显式降级（degraded）— 不伪装为空成功' },
  a_chain_disabled: { tone: 'neutral', label: 'A 链未启用', note: 'A 链 feature flag 关闭（a_chain_disabled）' },
};

// ── 审批票据（独立键空间；与 FXV/门禁的 APPROVED 语义互相独立，不共用键）──
const TICKET = {
  PENDING: { tone: 'info', label: '待审批', note: '票据等待审批（approval.tickets: PENDING）' },
  APPROVED: { tone: 'ok', label: '已批准', note: '票据已批准（approval.tickets: APPROVED）— 仅授权后续动作，不代表已执行' },
  REJECTED: { tone: 'warn', label: '已拒绝', note: '票据已拒绝（approval.tickets: REJECTED）— 受控停止' },
  USED: { tone: 'neutral', label: '已使用', note: '票据授权已被消费（approval.tickets: USED）' },
  EXPIRED: { tone: 'warn', label: '已过期', note: '票据 TTL 已过期（approval.tickets: EXPIRED）— 需后端重签' },
};
const TICKET_ACTION = {
  APPROVE_REMEDIATION: { tone: 'info', label: '批准修复授权', note: '人工批准修复流程（action: APPROVE_REMEDIATION）' },
};

// 未知状态兜底（统一契约）：label 保留原始机器值，note 标注"未知状态"——永不吞掉机器值。
function unknownEntry(v) {
  return { tone: 'neutral', label: v ?? 'UNKNOWN', note: `未知状态（原始枚举：${v ?? 'null/undefined'}）` };
}

// tone → antd Tag 预设色（页面统一着色口径；status-map 五档语义色 → antd 色）
function toneToColor(tone) {
  return { ok: 'success', info: 'processing', warn: 'warning', bad: 'error', neutral: 'default' }[tone] ?? 'default';
}

function fxvMap(state) { return FXV[state] ?? unknownEntry(state); }
function fxvArtifactMap(state) { return state ? (FXV_ARTIFACT[state] ?? unknownEntry(state)) : { tone: 'neutral', label: '无工件', note: '该 attempt 无工件记录' }; }
function cchainMap(state) { return CCHAIN[state] ?? unknownEntry(state); }
function cchainOverallMap(state) { return CCHAIN_OVERALL[state] ?? unknownEntry(state); }
function ragMap(state) { return RAG[state] ?? unknownEntry(state); }
function ticketMap(state) { return TICKET[state] ?? unknownEntry(state); }
function ticketActionMap(action) { return action ? (TICKET_ACTION[action] ?? unknownEntry(action)) : unknownEntry(action); }

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
  return EXECUTION[String(execution.status).toUpperCase()] ?? unknownEntry(execution.status);
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
  return unknownEntry(v);
}

function gateMap(gate, source) {
  if (!gate) {
    return { tone: 'neutral', label: '无确认记录', note: '包内无人工门/人工确认记录' };
  }
  const entry = GATE[gate] ?? unknownEntry(gate);
  if (source) return { ...entry, note: `${entry.note}｜来源 ${source}` };
  return entry;
}

export {
  executionMap, verdictMap, gateMap, publishMap, EXECUTION, GATE, VERDICT_SEVERITY_TONE,
  SEVERITY, SEVERITY_ORDER, FXV, FXV_ARTIFACT, CCHAIN, CCHAIN_OVERALL, RAG, TICKET, TICKET_ACTION,
  fxvMap, fxvArtifactMap, cchainMap, cchainOverallMap, ragMap, ticketMap, ticketActionMap,
  toneToColor, unknownEntry,
};
