import React, { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight, ArrowUpRight, Inbox, Search } from 'lucide-react';
import { useAppConfig } from '../App.jsx';
import { fetchRunsSnapshotOnce, useDataSource, useSourceQuery } from '../hooks.js';
import { groupRunsByPr } from '../pr-model.js';
import { fmtTime } from '../format.js';
import { Empty, ErrorBox, SkeletonRows } from '../ui.jsx';

// 工作队列：需要人工处理的事项集中在这一页。
// 口径（人话）：待决策的审查发现 / 失败或卡住的执行 / 等待审批的票据。
// 历史 HIGH 只代表"最近一次记录"，不自动生成当前待办（无权威当前 head 时如实说明）。

const SEV_LABEL = { HIGH: '高', MEDIUM: '中', LOW: '低' };
const KIND_LABEL = {
  approval: '审批待办',
  review: '审查发现',
  execution: '执行异常',
  gate: '门已拒绝',
  publish: '回写需核对',
};

function snapshotQueue(prs) {
  const items = [];
  for (const pr of prs) {
    const r = pr.latest;
    if (!r) continue;
    const base = {
      id: r.run_id ?? r.pack_id,
      repo: pr.repo, prNumber: pr.prNumber,
      title: pr.title ?? `PR #${pr.prNumber}`,
      href: pr.href ?? `/repos/${encodeURIComponent(pr.owner)}/${encodeURIComponent(pr.name)}/pr/${pr.prNumber}`,
      at: r.created_at ?? null,
    };
    const v = String(r.review?.verdict ?? '').toUpperCase();
    if (v === 'FINDING_CONFIRMED') {
      items.push({ ...base, kind: 'review', kindLabel: KIND_LABEL.review,
        severity: String(r.review?.severity ?? '').toUpperCase() || '未分级',
        statusLabel: `发现待决策（${r.review?.severity ?? '未分级'}）` });
    }
    if (String(r.execution?.status ?? '').toUpperCase() === 'ERROR') {
      items.push({ ...base, id: base.id + ':exec', kind: 'execution', kindLabel: KIND_LABEL.execution,
        severity: null, statusLabel: '执行出错，需人工处理' });
    }
    if (String(r.review?.human_gate ?? '').toUpperCase() === 'REJECTED') {
      items.push({ ...base, id: base.id + ':gate', kind: 'gate', kindLabel: KIND_LABEL.gate,
        severity: null, statusLabel: '修复已被人工拒绝（流程受控停止）' });
    }
  }
  return items;
}

function pgQueue(items) {
  return (items ?? []).map((t) => ({
    id: t.ticket_id,
    kind: 'approval', kindLabel: KIND_LABEL.approval,
    repo: t.repo ?? '未记录',
    prNumber: null, title: `${t.action}（run ${t.run_id ?? '未记录'}）`,
    href: '/approvals',
    severity: null,
    statusLabel: '等待审批（隔离联调库）',
    at: null,
    fixture: true,
  }));
}

function contractQueue(prs) {
  return (prs ?? [])
    .filter((v) => (v.attention?.flag === 'pending_tickets') || v.hasPendingTickets > 0)
    .map((v) => ({
      id: v.key, kind: 'approval', kindLabel: KIND_LABEL.approval,
      repo: v.repo, prNumber: v.prNumber, title: v.title ?? `PR #${v.prNumber}`,
      href: v.href ?? '#', severity: null,
      statusLabel: '有待审批票据（后端权威）', at: v.activityAt, fixture: false,
    }));
}

function matches(it, f) {
  if (f.repo && it.repo !== f.repo) return false;
  if (f.kind && it.kind !== f.kind) return false;
  if (f.severity) {
    const s = String(it.severity ?? '未分级');
    if (s !== f.severity) return false;
  }
  if (f.q) {
    const needle = f.q.trim().toLowerCase();
    const hay = [it.title, it.repo, it.prNumber != null ? `#${it.prNumber}` : '', it.statusLabel]
      .join(' ').toLowerCase();
    if (!hay.includes(needle.toLowerCase())) return false;
  }
  return true;
}

export default function PendingPage() {
  const config = useAppConfig();
  const { source } = useDataSource(config);
  const kind = source.kind;
  const [attempt, setAttempt] = useState(0);
  const [filters, setFilters] = useState({ q: '', repo: '', severity: '', kind: '' });
  const setF = (k, v) => setFilters((s) => ({ ...s, [k]: v }));

  const load = async () => {
    if (kind === 'console-pg') {
      const r = await source.listApprovals();
      return pgQueue(r?.items);
    }
    if (kind === 'contract') {
      const repos = await source.listRepos();
      const out = [];
      for (const r of repos) {
        const prs = await source.listPrs(r.repo);
        out.push(...contractQueue(prs));
      }
      return out;
    }
    // snapshot
    const data = await fetchRunsSnapshotOnce();
    return snapshotQueue(groupRunsByPr(data?.items ?? []));
  };

  const q = useSourceQuery(load, [source, kind, attempt]);

  const items = q.status === 'done' ? q.data ?? [] : [];
  const repoOptions = [...new Set(items.map((i) => i.repo))];
  const filtered = items.filter((i) => matches(i, filters));
  filtered.sort((a, b) => {
    const sev = (x) => (x.severity === 'HIGH' ? 3 : x.severity === 'MEDIUM' ? 2 : x.severity === 'LOW' ? 1 : 0);
    return (sev(b) - sev(a)) || String(b.at ?? '').localeCompare(String(a.at ?? ''));
  });

  const setFResetPage = (k, v) => setF(k, v);

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>待处理</h1>
          <p className="page-sub">
            需要人工处理的事项：等待决策的审查发现、失败或卡住的执行、等待审批的票据。
            历史记录里的旧结论不会出现在这里。
          </p>
        </div>
      </div>

      {q.status === 'error' ? (
        kind !== 'snapshot' && q.error?.status === 404 ? (
          <div className="state-box state-warn" role="status">
            当前数据源没有待办数据（404）——不回退到历史快照。返回 <Link to="/repos">仓库</Link>。
          </div>
        ) : (
          <ErrorBox error={q.error} onRetry={() => setAttempt((n) => n + 1)} />
        )
      ) : q.status !== 'done' ? (
        <SkeletonRows rows={5} cols={5} />
      ) : (
        <>
          <div className="filter-bar" role="search" aria-label="待处理事项筛选">
            <label className="f-field f-grow">
              <span className="f-label">搜索</span>
              <span className="search-wrap">
                <Search size={14} strokeWidth={1.75} aria-hidden />
                <input placeholder="仓库 / PR / 状态" value={filters.q}
                  onChange={(e) => setFResetPage('q', e.target.value)} aria-label="搜索待处理事项" />
              </span>
            </label>
            {repoOptions.length > 1 ? (
              <label className="f-field">
                <span className="f-label">仓库</span>
                <select value={filters.repo} onChange={(e) => setFResetPage('repo', e.target.value)}
                  aria-label="按仓库筛选">
                  <option value="">全部仓库</option>
                  {repoOptions.map((r) => <option key={r} value={r}>{r}</option>)}
                </select>
              </label>
            ) : null}
            <label className="f-field">
              <span className="f-label">严重度</span>
              <select value={filters.severity} onChange={(e) => setFResetPage('severity', e.target.value)}
                aria-label="按严重度筛选">
                <option value="">全部</option>
                <option value="HIGH">高</option>
                <option value="MEDIUM">中</option>
                <option value="LOW">低</option>
                <option value="未分级">未分级</option>
              </select>
            </label>
            <label className="f-field">
              <span className="f-label">类型</span>
              <select value={filters.kind} onChange={(e) => setFResetPage('kind', e.target.value)}
                aria-label="按类型筛选">
                <option value="">全部类型</option>
                {Object.entries(KIND_LABEL).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
              </select>
            </label>
          </div>

          <div className="table-meta">
            {filtered.length} 项待处理
            {kind === 'console-pg' ? '（审批待办来自隔离联调库——非真实生产待办）' : '（依据最近一次运行记录，非当前 head 结论）'}
          </div>

          {!filtered.length ? (
            <div className="panel" style={{ padding: 'var(--sp-6)', textAlign: 'center' }}>
              <Inbox size={22} strokeWidth={1.5} aria-hidden style={{ color: 'var(--c-text-3)' }} />
              <p style={{ fontWeight: 600, margin: '8px 0 4px' }}>没有需要处理的事项</p>
              <p className="muted" style={{ margin: '0 0 var(--sp-3)' }}>
                可以去仓库按 PR 浏览，或在运行历史里检索完整记录。
              </p>
              <div style={{ display: 'flex', gap: 8, justifyContent: 'center', flexWrap: 'wrap' }}>
                <Link className="btn" to="/repos">前往仓库</Link>
                <Link className="btn" to="/runs">打开运行历史</Link>
              </div>
            </div>
          ) : (
            <div className="queue-list">
              {filtered.map((it) => (
                <div key={it.id + (it.kind ?? '')} className={`panel queue-item${it.fixture ? ' queue-item-fixture' : ''}`}>
                  <div className="queue-main">
                    <div className="queue-title-line">
                      <span className={`chip ${it.kind === 'approval' ? 'chip-kind-approval' : 'chip-kind-review'}`}>
                        {it.kindLabel}
                      </span>
                      <Link className="row-link cell-title" to={it.href}>{it.title}</Link>
                      {it.prNumber != null ? <span className="muted">#{it.prNumber}</span> : null}
                      {it.fixture ? <span className="chip chip-fixture">联调数据</span> : null}
                    </div>
                    <div className="queue-sub">
                      <span className="mono">{it.repo}</span>
                      {it.prNumber != null ? null : null}
                      <span> · {it.statusLabel}</span>
                      {it.severity && SEV_LABEL[it.severity] ? (
                        <span className={`sev-tag sev-${it.severity}`}>严重度：{SEV_LABEL[it.severity]}</span>
                      ) : null}
                      {it.at ? <span> · {fmtTime(it.at)}</span> : null}
                    </div>
                  </div>
                  <Link className="btn btn-sm" to={it.href} aria-label={`打开：${it.title ?? it.repo}`}>
                    打开 <ArrowRight size={12} strokeWidth={1.75} aria-hidden />
                  </Link>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
