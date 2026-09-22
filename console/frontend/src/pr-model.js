// pr-model.js — PR 聚合与分页（纯函数，无 JSX；node --test 可直接验证）
//
// 聚合口径：稳定仓库身份（owner/name）+ PR 编号 = 一个 PR 对象；
// 不同仓库相同编号绝不合并；无 repo 或 pr_number 的 run 不进入 PR 聚合。
// 诚实边界：snapshot 无 GitHub 当前 head 权威数据 —— 本模块只输出"最近记录"，
// 不产生"当前审查结果 / 当前 head"类字段（该权威来源见 INTEGRATION-REQUESTS C-10）。

const byTimeDesc = (a, b) =>
  (new Date(b.created_at ?? 0).getTime() - new Date(a.created_at ?? 0).getTime())
    || String(a.pack_id ?? '').localeCompare(String(b.pack_id ?? ''));

export function prKeyOf(run) {
  if (!run?.repo || run.pr_number == null) return null;
  return `${run.repo}#${run.pr_number}`;
}

// 执行终态：webhook 轮 PROCESSED / matrix 轮 COMPLETED（BLOCKED 是受控停止，不算完成）
const EXEC_DONE = new Set(['PROCESSED', 'COMPLETED']);

export function groupRunsByPr(runs) {
  const map = new Map();
  for (const r of runs ?? []) {
    const key = prKeyOf(r);
    if (!key) continue;
    if (!map.has(key)) {
      const [owner, ...rest] = String(r.repo).split('/');
      map.set(key, {
        key,
        repo: r.repo,
        owner,
        name: rest.join('/'),
        prNumber: r.pr_number,
        runs: [],
      });
    }
    map.get(key).runs.push(r);
  }
  const prs = [...map.values()];
  for (const pr of prs) {
    pr.runs.sort(byTimeDesc);
    pr.latest = pr.runs[0] ?? null;
    pr.latestCompleted = pr.runs.find((r) => EXEC_DONE.has(String(r.execution?.status ?? '').toUpperCase())) ?? null;
    pr.heads = groupRunsByHead(pr.runs);
    pr.activityAt = pr.latest?.created_at ?? null;
    pr.title = pr.latest?.pr_title ?? null;
    pr.prUrl = pr.latest?.pr_url ?? null;
    pr.attention = attentionOf(pr.latest);
  }
  prs.sort((a, b) =>
    (new Date(b.activityAt ?? 0).getTime() - new Date(a.activityAt ?? 0).getTime())
      || String(a.key).localeCompare(String(b.key)));
  return prs;
}

// 按 head 分组：快照里同一 PR 可能存在多个历史 head；
// head 未记录的归入 head=null 组，展示为"head 未记录"。
export function groupRunsByHead(runs) {
  const map = new Map();
  for (const r of runs ?? []) {
    const head = r.head_sha ?? null;
    if (!map.has(head)) map.set(head, { head, runs: [] });
    map.get(head).runs.push(r);
  }
  const heads = [...map.values()];
  for (const h of heads) {
    h.runs.sort(byTimeDesc);
    h.latest = h.runs[0] ?? null;
  }
  heads.sort((a, b) => byTimeDesc(a.latest ?? {}, b.latest ?? {}));
  return heads;
}

// 证据型"是否需要用户处理"：只看所给 run（调用方传入最近记录），永不跨 head 推导。
export function attentionOf(run) {
  if (!run) return { flag: 'unknown', label: '无记录' };
  const v = String(run.review?.verdict ?? '').toUpperCase();
  if (v === 'FINDING_CONFIRMED') return { flag: 'decision', label: '有待处理发现（最近记录）' };
  if (String(run.review?.human_gate ?? '').toUpperCase() === 'REJECTED') return { flag: 'blocked', label: '已拒绝修复（最近记录）' };
  if (v === 'NOT_CONFIRMED') return { flag: 'clear', label: '最近记录未发现问题' };
  return { flag: 'unknown', label: '结论未记录（最近记录）' };
}

// 仓库视图：PR 数与 run 数分开统计，调用方必须分别标注口径。
export function reposFromPrs(prs) {
  const map = new Map();
  for (const pr of prs ?? []) {
    if (!map.has(pr.repo)) {
      map.set(pr.repo, { repo: pr.repo, owner: pr.owner, name: pr.name, prCount: 0, runCount: 0, activityAt: null });
    }
    const entry = map.get(pr.repo);
    entry.prCount += 1;
    entry.runCount += pr.runs.length;
    if (pr.activityAt && (!entry.activityAt || pr.activityAt > entry.activityAt)) entry.activityAt = pr.activityAt;
  }
  return [...map.values()].sort((a, b) => a.repo.localeCompare(b.repo));
}

export function paginate(items, page = 1, perPage = 10) {
  const total = items?.length ?? 0;
  const pages = Math.max(1, Math.ceil(total / perPage));
  const p = Math.min(Math.max(1, Number(page) || 1), pages);
  return {
    page: p,
    perPage,
    total,
    pages,
    items: (items ?? []).slice((p - 1) * perPage, p * perPage),
  };
}
