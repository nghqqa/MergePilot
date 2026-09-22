// data/pr-view.js — 两种数据源 → 页面共用的 PR 视图模型（归一形状 + 来源标记）。
//
// kind: 'snapshot'（锁定证据包，客户端聚合；无当前 head 权威）
//     | 'contract'（契约 v2 /api/pulls：current_head_sha 为 GitHub 权威，latest_result.stale 语义）
// 页面只读本形状 + kind，不做数据来源判断以外的分支。

export function prViewFromSnapshot(agg) {
  return {
    kind: 'snapshot',
    key: agg.key,
    repo: agg.repo,
    owner: agg.owner,
    name: agg.name,
    prNumber: agg.prNumber,
    title: agg.title,
    prUrl: agg.prUrl,
    activityAt: agg.activityAt,
    attention: agg.attention, // {flag,label} —— 依据最近一次运行记录（历史口径）
    review: { basis: 'latest-record', review: agg.latest?.review ?? {}, stale: null },
    currentHead: null, // snapshot 无当前 head 权威
    heads: agg.heads,
    runs: agg.runs,
    latest: agg.latest,
    latestCompleted: agg.latestCompleted,
    runCountExact: true,
  };
}

// 契约 v2 §2 列表项 → PrView。
// 语义红线：
// - current_head_sha 是权威当前 head；latest_result.stale=true 表示结论属于旧 head，
//   不得作为当前结论展示（标注"旧 head 结论"）；
// - has_pending_tickets 是后端权威的有效待批票据数——真实待办只来自它；
//   历史 FINDING_CONFIRMED 仅在 !stale（属于当前 head）时才提示"有待处理发现"，
//   stale 的历史 HIGH 一律降为"历史记录中需关注"，不生成当前待办；
// - latest_run.status=RUNNING 且无完成结果 → 当前 head 审查进行中，不用旧成功顶替。
export function attentionFromContract(item) {
  if ((item.has_pending_tickets ?? 0) > 0) {
    return { flag: 'pending_tickets', label: `${item.has_pending_tickets} 张有效待批票据（后端权威）` };
  }
  const lr = item.latest_result ?? null;
  const v = String(lr?.verdict ?? '').toUpperCase();
  if (lr && !lr.stale && v === 'FINDING_CONFIRMED') {
    return { flag: 'decision', label: '当前 head 有待处理发现' };
  }
  if (lr && lr.stale && v === 'FINDING_CONFIRMED') {
    return { flag: 'stale_attention', label: '历史记录中需关注（旧 head 结论，非当前结论）' };
  }
  if (lr && !lr.stale && v === 'NOT_CONFIRMED') return { flag: 'clear', label: '当前 head 未发现问题' };
  if (lr && lr.stale) return { flag: 'stale_only', label: '当前 head 无完成结果（仅有旧 head 记录）' };
  return { flag: 'unknown', label: '暂无审查结果记录' };
}

export function prViewFromContract(item) {
  const lr = item.latest_result ?? null;
  const lrun = item.latest_run ?? null;
  const runs = [];
  if (lr) {
    runs.push({
      run_id: lr.run_id,
      head_sha: lr.head_sha ?? null,
      created_at: null, // 列表契约不携带结果时间——activityAt 用 latest_run.started_at
      execution: { status: 'COMPLETED' },
      review: { verdict: lr.verdict ?? null, severity: lr.severity ?? null },
      published: lr.published ?? null,
      stale: Boolean(lr.stale),
    });
  }
  if (lrun) {
    runs.push({
      run_id: lrun.run_id,
      head_sha: null,
      created_at: lrun.started_at ?? null,
      execution: { status: String(lrun.status ?? 'RUNNING').toUpperCase() },
      review: {},
    });
  }
  const review = lr
    ? { basis: lr.stale ? 'stale-record' : 'current-head-result', review: { verdict: lr.verdict ?? null, severity: lr.severity ?? null }, stale: Boolean(lr.stale) }
    : { basis: 'no-result', review: {}, stale: null };
  return {
    kind: 'contract',
    key: `${item.repo}#${item.pr_number}`,
    repo: item.repo,
    owner: String(item.repo ?? '').split('/')[0],
    name: String(item.repo ?? '').split('/').slice(1).join('/'),
    prNumber: item.pr_number,
    title: item.title ?? null,
    prUrl: item.repo && item.pr_number != null ? `https://github.com/${item.repo}/pull/${item.pr_number}` : null,
    activityAt: lrun?.started_at ?? null,
    attention: attentionFromContract(item),
    review,
    currentHead: item.current_head_sha ?? null, // GitHub 权威
    heads: [],
    runs,
    latest: runs[0] ?? null,
    latestCompleted: lr ? runs[0] : null,
    runCountExact: false, // 列表契约不含全量 run 数——页面不得显示"共 N 次运行"
  };
}
