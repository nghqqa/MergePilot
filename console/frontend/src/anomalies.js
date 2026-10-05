// anomalies.js — 异常状态建模（纯函数，无 JSX；node --test 可直接验证）。
//
// 审查工作台把"异常"从一串裸数字升级为四类独立建模的异常状态：
// 每类必须给出 原因（cause）/ 影响（impact）/ 下一步（next：按钮文案 + 去向），
// 不允许只显示状态文字而没有操作路径。
//
// 键空间说明（与 status-map.js 的独立键空间纪律一致）：
//   stale / failed_receipt / integrity 三类来自 /api/overview 的 incidents 计数
//   （legacy 控制面口径；MU 投影层当前恒 0——如实显示 0，不虚构）。
//   protection_unknown 来自 MU 域 /api/mu/prs 的 branch_protection_status='unknown'
//   （fail-closed 语义：未知≠未受保护，合并资格恒未知）。
//
// 保护状态未知四分（未配置 / 权限不足 / 接口失败 / 检查中）：
//   后端 mu.pull_request 只落 'unknown|known_clean|blocked'，不携带细分原因码
//   （ghprovider 探测层知道 404=未配置 / 403=权限，但未落库——缺口已登记）。
//   前端按可得证据诚实推导：
//     checking  = 该 PR 有进行中的审查 run（探测随审查进行——可确证的子态）
//     undetermined = 其余——列出三种可能原因，各配操作路径，并明示"后端未记录细分原因"。

// ── 四类异常（工作台异常区主条目）──
export const ANOMALIES = {
  stale: {
    key: 'stale',
    label: 'Head 已过期（stale）',
    short: 'Head 过期',
    tone: 'neutral',
    cause: '同一 PR 推进了更新的 head，既有审查运行仍绑定旧 head。',
    impact: '旧 head 的审查结论不再代表当前代码，不能作为合并依据。',
    next: { label: '查看过期项', filter: 'anomaly',
      hint: '在最新 head 上重新发起审查后，旧记录自动归档为过期。' },
  },
  failed_receipt: {
    key: 'failed_receipt',
    label: '失败回执',
    short: '失败回执',
    tone: 'warn',
    cause: '审查链路的某个技能回执状态非 OK（执行中断、超时或技能报错）。',
    impact: '该 run 的证据链不完整，审查结论不可信，需要重跑或人工核查。',
    next: { label: '查看失败项', filter: 'anomaly',
      hint: '打开对应 run 明细确认失败环节后重跑审查。' },
  },
  integrity: {
    key: 'integrity',
    label: '完整性冲突',
    short: '完整性冲突',
    tone: 'bad',
    cause: '回执摘要（integrity）校验不一致——记录可能被篡改或写入异常。',
    impact: '流程被受控阻断（fail-closed）：在人工核查前，该 run 的结论不生效。',
    next: { label: '查看冲突项', filter: 'blocked',
      hint: '人工核对证据与审计后裁定：确认无篡改可放行，否则按安全事件处理。' },
  },
  protection_unknown: {
    key: 'protection_unknown',
    label: '保护状态未知',
    short: '保护未知',
    tone: 'warn',
    cause: 'GitHub 分支保护状态未能确认为"受保护"（探测未完成或不可达）。',
    impact: '合并资格 fail-closed 恒为未知——不是判定为未受保护，而是暂不可下合并结论。',
    next: { label: '查看未知项', filter: 'anomaly',
      hint: '按四分子态（未配置/权限不足/接口失败/检查中）逐项处理。' },
  },
};

export const ANOMALY_ORDER = ['stale', 'failed_receipt', 'integrity', 'protection_unknown'];

// ── 保护状态未知 · 四分子态 ──
// 每个子态：label / cause（原因）/ impact（影响）/ next（下一步）。
// 操作动词表（数据可信度修复 2026-10-05）：
//   refresh       = 仅重新读取页面数据（如实命名「刷新状态」，绝不冒充"重探/重试检查"——
//                   当前部署没有独立探测端点，探测随审查运行发生）
//   repo_settings = 真实仓库设置地址 https://github.com/{owner}/{repo}/settings/branches
//                   （owner/name 缺失时降级为「查看配置指南」文档外链）
//   multiuser     = 组织与接入页核对 App 权限（站内路由）
//   detail        = 打开 PR 详情（需调用方提供 onOpenDetail，否则只留 hint 文字——
//                   不渲染看似可执行但实际 disabled 的按钮）
export const PROTECTION_UNKNOWN_STATES = {
  checking: {
    key: 'checking',
    label: '检查中',
    tone: 'info',
    cause: '该 PR 正在审查中——分支保护探测随审查一起进行，尚未返回结果。',
    impact: '等待本次审查完成后自动更新；无需人工处理。',
    next: { kind: 'refresh', label: '刷新状态',
      hint: '探测随审查进行——稍后刷新页面即可看到结果。' },
  },
  not_configured: {
    key: 'not_configured',
    label: '未配置保护',
    tone: 'warn',
    cause: 'base 分支可能没有启用分支保护（GitHub 探测 404 时的典型含义）。',
    impact: '没有保护规则的分支可被直接 push——建议为主分支启用保护。',
    next: { kind: 'repo_settings', label: '打开仓库保护设置',
      hint: '仓库 Settings → Branches → Add branch protection rule。' },
  },
  permission: {
    key: 'permission',
    label: '权限不足',
    tone: 'warn',
    cause: 'GitHub App 可能未获得 administration:read 只读权限，探测被 GitHub 拒绝（403）。',
    impact: '工作台无法确认保护状态，合并资格保持未知（fail-closed）。',
    next: { kind: 'multiuser', label: '检查 App 权限',
      hint: '在"组织与接入"页确认 GitHub App 权限包含 administration:read（只读）。' },
  },
  api_failed: {
    key: 'api_failed',
    label: '接口失败',
    tone: 'bad',
    cause: 'GitHub API 网络失败或限流——探测请求未得到有效响应。',
    impact: '状态暂时不可得；恢复后随下一次审查自动重探。',
    next: { kind: 'refresh', label: '刷新状态',
      hint: '恢复后随下一次审查自动重探；持续失败时检查 GitHub 服务状态。' },
  },
  undetermined: {
    key: 'undetermined',
    label: '原因未细分',
    tone: 'warn',
    cause: '后端当前未记录细分原因码——无法区分未配置/权限不足/接口失败。',
    impact: '合并资格保持未知（fail-closed）；按以下三种可能原因逐项排查。',
    // 候选原因（各自可执行）：后端补 reason 码后本条自动收敛为三选一精确显示
    candidates: ['not_configured', 'permission', 'api_failed'],
    next: { kind: 'detail', label: '逐项排查',
      hint: 'PR 详情页的保护状态面板列出每种原因的核对与处理路径。' },
  },
};

// 保护状态未知子态推导（纯函数）。
// @param pr  MU PR 行（branch_protection_status / provider_pr_number）
// @param hasActiveRun  该 PR 是否存在进行中的审查 run（REVIEWING/REVIEW_QUEUED/RECEIVED 等）
export function protectionUnknownKind(pr, hasActiveRun = false) {
  if (hasActiveRun) return 'checking';
  void pr; // 当前无更多后端证据可用——落 undetermined（候选三态在 UI 展开）
  return 'undetermined';
}

// MU run 状态 → 是否"探测进行中"（保护子态 checking 依据）
const ACTIVE_RUN_STATUSES = new Set(['RECEIVED', 'REVIEW_QUEUED', 'REVIEWING']);
export function isRunChecking(status) {
  return ACTIVE_RUN_STATUSES.has(String(status ?? '').toUpperCase());
}

// 从 /api/overview 推导四类异常计数（纯函数）。
// 返回 [{...ANOMALIES[kind], count}]（含 0 值项——调用方决定是否展示）。
export function deriveAnomalies(overview, protectionUnknownCount = 0) {
  const inc = overview?.incidents ?? {};
  const counts = {
    stale: Number(inc.stale_count ?? 0),
    failed_receipt: Number(inc.failed_receipts ?? 0),
    integrity: Number(inc.integrity_conflicts ?? 0),
    protection_unknown: Number(protectionUnknownCount ?? 0),
  };
  return ANOMALY_ORDER.map((k) => ({ ...ANOMALIES[k], count: counts[k] }));
}

// 工作台表格行的"待处理原因"人话化（纯函数）。
// 输入行 = /api/overview prs 行（stage / stage_source / run_id）。
// 返回 { text, detail }；detail 供抽屉/tooltip 展示技术来源。
export function pendingReasonOf(row) {
  const stage = String(row?.stage ?? '').toUpperCase();
  const src = String(row?.stage_source ?? '');
  switch (stage) {
    case 'ACTION_REQUIRED':
      return { text: '等待人工审批/裁定', detail: src || '最新 run 存在待处理票据或需人工裁定' };
    case 'BLOCKED':
      if (src.includes('integrity')) {
        return { text: '回执完整性冲突——受控阻断', detail: src };
      }
      return { text: '审查门拒绝或验证失败——受控停止', detail: src };
    case 'STALE':
      return { text: '已有更新的 head，本 run 绑定旧 head', detail: src || 'head-ordering' };
    case 'UNKNOWN':
      return { text: src.includes('unrecognized') || src.includes('missing')
        ? '审查门决策缺失（fail-closed）'
        : '运行失败或状态无法判定', detail: src };
    case 'REVIEWING':
      return { text: row?.run_id ? '审查进行中，尚无门决策' : '已入库，尚无审查运行', detail: src };
    case 'PENDING':
      return { text: '尚未开始审查', detail: src };
    case 'REMEDIATING':
      return { text: '修复预演进行中', detail: src };
    case 'VERIFYING':
      return { text: '独立验证进行中', detail: src };
    case 'PASSED':
      return { text: '无需处理', detail: src };
    default:
      return { text: '状态待确认', detail: src || String(row?.stage ?? '') };
  }
}

// 行 → 阶段桶（纯函数，阶段维度）。
// 'attention'=需要人处理；'blocked'=已阻断；'reviewing'=真实进行中（审查/修复/验证）；
// 'anomaly'=阶段维异常（head 过期 / 决策缺失）；其余（PENDING/PASSED）归 'normal'。
// 注意：保护未知不在此判定——它是 PR 级集合（protectionUnknownKeySet），
// 由调用方并集进异常桶（见 OverviewPage），保证统计卡与列表同源。
export function bucketOf(row) {
  const stage = String(row?.stage ?? '').toUpperCase();
  if (stage === 'ACTION_REQUIRED') return 'attention';
  if (stage === 'BLOCKED') return 'blocked';
  if (REVIEWING_STAGES.includes(stage)) return 'reviewing';
  if (stage === 'STALE' || stage === 'UNKNOWN') return 'anomaly';
  return 'normal';
}

// 真实进行中的阶段（run 在途）：审查中 / 修复预演中 / 验证中。
// （PENDING=尚未开始审查——不是进行中；PASSED=终态。）
export const REVIEWING_STAGES = ['REVIEWING', 'REMEDIATING', 'VERIFYING'];

// ── PR 实体分组：/api/overview 行（每 head/run 一行）→ 每 PR 一实体 ──
// 后端（legacy 与 MU 投影均）按 head/run 出行：同一 PR 多个 head 各占一行。
// 工作台口径统一为 PR：current = updated_at 最新的行（当前 head）；
// history = 其余行（历史 head/run，按时间倒序，收进详情抽屉展开）。
// 返回 [{ key, repo, n, owner, name, detailTo, current, history, bucket, reason, ... }]
export function groupRowsByPr(rows, { isMu = false } = {}) {
  const byPr = new Map();
  for (const r of rows ?? []) {
    const repo = String(r.repo ?? '');
    if (!repo) continue;
    const n = r.pr_number ?? r.pr;
    const key = `${repo}#${n}`;
    if (!byPr.has(key)) byPr.set(key, []);
    byPr.get(key).push(r);
  }
  const out = [];
  for (const [key, group] of byPr) {
    const sorted = [...group].sort((a, b) =>
      String(b.updated_at ?? '').localeCompare(String(a.updated_at ?? '')));
    const current = sorted[0];
    const [owner, name] = String(current.repo).split('/');
    const n = current.pr_number ?? current.pr;
    const detailTo = (n != null && owner && name)
      ? (isMu
        ? `/mu/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/pr/${n}`
        : `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/pr/${n}`)
      : null;
    out.push({
      ...current,
      key,
      n,
      owner,
      name,
      detailTo,
      current,
      history: sorted.slice(1).map((h) => ({
        head_sha: h.head_sha, run_id: h.run_id, stage: h.stage,
        stage_source: h.stage_source, updated_at: h.updated_at ?? null,
      })),
      updated_at: current.updated_at ?? null,
      reason: pendingReasonOf(current),
      bucket: bucketOf(current),
    });
  }
  return out;
}
