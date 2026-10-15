// data/mu-pr-merge.js — MU 仓库 PR 列表归并（纯函数，node --test 直测，无 React/fetch）。
//
// 后端契约事实（cc64462，console/backend/lib/multiuser）：
//   GET /api/mu/prs?repo_id=        → mu.pull_request 行（每个 head 一行，updated_at DESC，
//                                     LIMIT 50，附 head_basis='event_order'）。行内只有 PR 身份与
//                                     head/保护字段——不含任何 run/结论字段（读 run_status 恒
//                                     undefined 是 UX-01「结论未记录」假象的根因）。
//   GET /api/mu/runs?repo_id=       → review_run 行（created_at DESC，LIMIT 50），含
//                                     provider_pr_number/head_sha/status/findings_count/top_severity。
//   GET /api/mu/approvals?status=PENDING → 审批票（created_at DESC，LIMIT 200），含
//                                     repo_owner/repo_name/pr_number/head_sha/severity。
//
// 归并规则（语义红线与 CONTRACT §3、status-map 红线一致）：
//   1. PR 身份键 = (仓库, provider_pr_number)——本函数按单一仓库调用，仓库维度由调用方保证；
//      绝不按编号全局去重（不同仓库同编号互不合并）。
//   2. 当前 head = 组内 updated_at 最新的 head 行（服务端事件序），head_basis='event_order'
//      无 GitHub 权威对照 → currentHeadConfirmed 恒 false，展示方必须带「未确认」。
//   3. 当前 head 的运行事实只来自绑定该 head 的 run；旧 head 的 run 不冒充当前结论
//      （仅以 staleRunHint 提示「仅旧 head 有运行」）。
//   4. REVIEWED/COMPLETED 等 run 状态 ≠ 通过/无问题——本模块只输出运行状态词表映射
//      （runState）与发现计数/最重级别，审查结论一律指向 PR 详情。
//   5. 待办标记仅来自权威 PENDING 审批票，并区分「绑定当前 head」与「绑定旧 head」。
//   6. 三个端点均为窗口截断（LIMIT）——到帽时输出 truncated 标记，调用方必须表达
//      「可能少计」，不得声称全量；PR 键空间取 pr 行 ∪ run ∪ 票的并集，尽量不因
//      单一来源截断而整行丢失。
//   7. 缺数据 = 未取得（null + 提示字段），不伪造 0/通过/无待办。
import { muRunMap, SEVERITY, unknownEntry } from '../status-map.js';

const PR_CAP = 50;      // findPullRequests LIMIT
const RUN_CAP = 50;     // /api/mu/runs LIMIT
const TICKET_CAP = 200; // listFixApprovals LIMIT

const normHead = (s) => String(s ?? '').toLowerCase();
const byCreatedDesc = (a, b) => String(b.created_at ?? '').localeCompare(String(a.created_at ?? ''));

export function mergeMuRepoPrs({ repo, prRows = [], runRows = [], pendingTickets = [] }) {
  const owner = String(repo ?? '').split('/')[0] ?? '';
  const name = String(repo ?? '').split('/').slice(1).join('/');

  // run 索引：pr → head → [runs 最新在前]
  const runsByPr = new Map();
  for (const r of runRows) {
    const n = Number(r.provider_pr_number);
    if (!Number.isFinite(n)) continue;
    if (!runsByPr.has(n)) runsByPr.set(n, new Map());
    const byHead = runsByPr.get(n);
    const h = normHead(r.head_sha) || '(unknown)';
    if (!byHead.has(h)) byHead.set(h, []);
    byHead.get(h).push(r);
  }
  for (const byHead of runsByPr.values()) {
    for (const arr of byHead.values()) arr.sort(byCreatedDesc);
  }

  // 票索引：pr → [PENDING 票]（租户级端点——只保留本仓库的票，跨仓库同编号互不混淆）
  const ticketsByPr = new Map();
  for (const t of pendingTickets) {
    const n = Number(t.pr_number);
    if (!Number.isFinite(n)) continue;
    if (`${String(t.repo_owner ?? '')}/${String(t.repo_name ?? '')}` !== repo) continue;
    if (!ticketsByPr.has(n)) ticketsByPr.set(n, []);
    ticketsByPr.get(n).push(t);
  }

  // PR 行索引：pr → [head 行]（服务端 updated_at DESC——组内首行即当前 head 推定）
  const rowsByPr = new Map();
  for (const p of prRows) {
    const n = Number(p.provider_pr_number);
    if (!Number.isFinite(n)) continue;
    if (!rowsByPr.has(n)) rowsByPr.set(n, []);
    rowsByPr.get(n).push(p);
  }

  // PR 键空间并集（窗口截断下不因单一来源缺行而丢 PR）
  const prNumbers = new Set([...rowsByPr.keys(), ...runsByPr.keys(), ...ticketsByPr.keys()]);

  const prs = [...prNumbers].sort((a, b) => b - a).map((n) => {
    const headRows = rowsByPr.get(n) ?? [];
    const cur = headRows[0] ?? null;
    const currentHead = cur?.head_sha ?? null;
    const byHead = runsByPr.get(n) ?? new Map();
    const currentHeadRuns = currentHead ? (byHead.get(normHead(currentHead)) ?? []) : [];
    const allRuns = [...byHead.values()].flat().sort(byCreatedDesc);
    const currentHeadRun = currentHeadRuns[0] ?? null;
    const latestRunAnyHead = allRuns[0] ?? null;

    const tickets = ticketsByPr.get(n) ?? [];
    const curTickets = currentHead
      ? tickets.filter((t) => normHead(t.head_sha) === normHead(currentHead))
      : [];
    const oldTickets = tickets.filter((t) => !curTickets.includes(t));

    // 待办标记：区分绑定当前 head / 绑定旧 head / 当前 head 未取得（三者都是真实待办，
    // 绑定关系如实标注——不把取不到 head 的票谎称为「旧 head」）
    let attention = { flag: null, label: '—' };
    if (currentHead == null && tickets.length > 0) {
      attention = { flag: 'pending_tickets',
        label: `${tickets.length} 张待审批票（绑定 head 未取得，以详情页为准）` };
    } else if (curTickets.length > 0 && oldTickets.length > 0) {
      attention = { flag: 'pending_tickets',
        label: `${curTickets.length + oldTickets.length} 张待审批票（${curTickets.length} 张绑定当前 head，${oldTickets.length} 张绑定旧 head）` };
    } else if (curTickets.length > 0) {
      attention = { flag: 'pending_tickets', label: `${curTickets.length} 张待审批票（绑定当前 head）` };
    } else if (oldTickets.length > 0) {
      attention = { flag: 'pending_tickets', label: `${oldTickets.length} 张待审批票（绑定旧 head，以详情页为准）` };
    }

    const runStateEntry = currentHeadRun
      ? muRunMap(currentHeadRun.status)
      : null;
    const findingsCount = currentHeadRun ? Number(currentHeadRun.findings_count ?? 0) : null;
    const severityEntry = findingsCount != null && findingsCount > 0
      ? (SEVERITY[String(currentHeadRun.top_severity ?? '').toUpperCase()] ?? unknownEntry(currentHeadRun.top_severity))
      : null;

    const provider = cur?.provider ?? null;
    const prUrl = provider === 'gitee' ? `https://gitee.com/${repo}/pull/${n}`
      : provider === 'github' ? `https://github.com/${repo}/pull/${n}`
      : null; // provider 未记录时不猜 URL

    return {
      kind: 'multiuser',
      key: `${repo}#${n}`,
      repo, owner, name,
      prNumber: n,
      title: cur?.title ?? null,
      prUrl,
      activityAt: cur?.updated_at ?? cur?.created_at ?? latestRunAnyHead?.created_at ?? null,
      currentHead,
      currentHeadConfirmed: false, // head_basis=event_order——一律未确认
      state: cur?.state ?? null,
      heads: headRows.map((h) => ({ head: h.head_sha ?? null, isCurrent: h === cur, runs: [] })),
      runs: allRuns,
      // 当前 head 的最新 run（旧 head run 不冒充）
      latestRun: currentHeadRun
        ? { runId: currentHeadRun.run_id, status: String(currentHeadRun.status ?? ''),
            created_at: currentHeadRun.created_at ?? null, head_sha: currentHeadRun.head_sha ?? null }
        : null,
      // 当前 head 无 run 但旧 head 有——如实提示，不作为当前事实
      staleRunHint: !currentHeadRun && latestRunAnyHead
        ? { run_id: latestRunAnyHead.run_id, head_sha: latestRunAnyHead.head_sha ?? null,
            status: String(latestRunAnyHead.status ?? '') }
        : null,
      runState: runStateEntry ? { label: runStateEntry.label, tone: runStateEntry.tone, note: runStateEntry.note } : null,
      findingsCount,
      severityLabel: severityEntry ? severityEntry.label : null,
      pendingTickets: { current: curTickets.length, historical: oldTickets.length },
      attention,
      // 兼容旧视图形状：MU 列表无独立审查结论字段——review.review 留空，绝不伪造 verdict
      review: {
        basis: currentHeadRun ? 'run-status' : (latestRunAnyHead ? 'stale-run-only' : 'no-run'),
        review: {},
        stale: false,
      },
    };
  });

  return {
    prs,
    meta: {
      prRowsTruncated: prRows.length >= PR_CAP,
      runsTruncated: runRows.length >= RUN_CAP,
      ticketsTruncated: pendingTickets.length >= TICKET_CAP,
    },
  };
}
