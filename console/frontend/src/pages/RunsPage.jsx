import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { ArrowRight, Clock, RotateCcw, Search } from 'lucide-react';
import { api } from '../api.js';
import { ErrorBox, Empty, SkeletonRows } from '../ui.jsx';
import { ExecutionBadge, VerdictBadge, GateBadge, PublishBadge } from '../status.jsx';
import { fmtTime } from '../format.js';

const QUICK_FILTERS = [
  { key: '', label: '全部' },
  { key: 'published', label: '已回写 GitHub' },
  { key: 'not_recorded', label: '无发布记录' },
];

export default function RunsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const filters = {
    q: searchParams.get('q') ?? '',
    repo: searchParams.get('repo') ?? '',
    execution: searchParams.get('execution') ?? '',
    verdict: searchParams.get('verdict') ?? '',
    publish: searchParams.get('publish') ?? '',
  };
  const [data, setData] = useState(null);
  const [allData, setAllData] = useState(null);
  const [error, setError] = useState(null);
  const tableWrapRef = useRef(null);
  const [scrollable, setScrollable] = useState(false);

  const setFilter = (key, value) => {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(key, value);
    else next.delete(key);
    setSearchParams(next, { replace: true });
  };
  const hasFilter = Object.values(filters).some(Boolean);
  const clear = () => setSearchParams(new URLSearchParams(), { replace: true });

  useEffect(() => {
    setData(null);
    setError(null);
    api.runs({ ...filters, limit: 200 }).then(setData).catch(setError);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  useEffect(() => {
    api.runs({ limit: 200 }).then(setAllData).catch(() => {});
  }, []);

  useEffect(() => {
    const el = tableWrapRef.current;
    if (!el) return undefined;
    const check = () => setScrollable(el.scrollWidth > el.clientWidth + 4);
    check();
    window.addEventListener('resize', check);
    return () => window.removeEventListener('resize', check);
  }, [data]);

  // 计数 chips 口径：当前其他筛选（除发布维度）范围内的数量
  const baseItems = useMemo(() => {
    const items = allData?.items ?? [];
    return items.filter((r) => {
      if (filters.q && ![r.pack_id, r.run_id, r.repo, r.pr_title, r.head_sha, String(r.pr_number ?? '')]
        .some((v) => (v ?? '').toLowerCase().includes(filters.q.toLowerCase()))) return false;
      if (filters.repo && !(r.repo ?? '').toLowerCase().includes(filters.repo.toLowerCase())) return false;
      if (filters.execution && (r.execution?.status ?? '').toUpperCase() !== filters.execution.toUpperCase()) return false;
      if (filters.verdict && (r.review?.verdict ?? '').toUpperCase() !== filters.verdict.toUpperCase()) return false;
      return true;
    });
  }, [allData, filters.q, filters.repo, filters.execution, filters.verdict]);

  const counts = useMemo(() => ({
    total: baseItems.length,
    published: baseItems.filter((r) => r.publish?.status === 'published').length,
    not_recorded: baseItems.filter((r) => r.publish?.status !== 'published').length,
  }), [baseItems]);

  const repos = useMemo(
    () => [...new Set((allData?.items ?? []).map((r) => r.repo).filter(Boolean))].sort(),
    [allData]
  );

  const shownFrom = data?.total ? (data.offset ?? 0) + 1 : 0;
  const shownTo = data ? Math.min((data.offset ?? 0) + (data.items?.length ?? 0), data.total) : 0;
  const listState = { from: `${location.pathname}${location.search}` };

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>审查运行</h1>
          <p className="page-sub">
            先看 PR 与审查结论，再核对执行、人工确认与 GitHub 检查。
            各状态悬停或聚焦可查看字段来源与语义边界；缺失字段如实显示"未记录"。
          </p>
        </div>
      </div>

      <div className="quick-filters" role="group" aria-label="按发布状态快筛">
        {QUICK_FILTERS.map((f) => (
          <button
            key={f.key}
            className={`qf-chip${filters.publish === f.key ? ' qf-active' : ''}`}
            onClick={() => setFilter('publish', f.key)}
          >
            {f.label}
            <span className="qf-count">{f.key === '' ? counts.total : counts[f.key] ?? '—'}</span>
          </button>
        ))}
      </div>

      <div className="filter-bar" role="search" aria-label="运行筛选">
        <label className="f-field f-grow">
          <span className="f-label">搜索</span>
          <span className="search-wrap">
            <Search size={14} strokeWidth={1.75} aria-hidden />
            <input
              placeholder="run_id / SHA / PR / 标题"
              value={filters.q}
              onChange={(e) => setFilter('q', e.target.value)}
              aria-label="搜索 run_id、SHA、PR 号或标题"
            />
          </span>
        </label>
        <label className="f-field">
          <span className="f-label">仓库</span>
          <select value={filters.repo} onChange={(e) => setFilter('repo', e.target.value)} aria-label="按仓库筛选">
            <option value="">全部仓库</option>
            {repos.map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
        </label>
        <label className="f-field">
          <span className="f-label">执行状态</span>
          <select value={filters.execution} onChange={(e) => setFilter('execution', e.target.value)} aria-label="按执行状态筛选">
            <option value="">全部</option>
            {['PROCESSED', 'RUNNING', 'PENDING', 'ERROR', 'COMPLETED', 'BLOCKED'].map((s) => <option key={s}>{s}</option>)}
          </select>
        </label>
        <label className="f-field">
          <span className="f-label">审查结论</span>
          <select value={filters.verdict} onChange={(e) => setFilter('verdict', e.target.value)} aria-label="按审查结论筛选">
            <option value="">全部</option>
            <option value="FINDING_CONFIRMED">发现确认</option>
            <option value="NOT_CONFIRMED">未发现问题</option>
          </select>
        </label>
        {hasFilter ? (
          <button className="btn btn-ghost f-clear" onClick={clear}>
            <RotateCcw size={13} strokeWidth={1.75} aria-hidden /> 清除筛选
          </button>
        ) : null}
      </div>

      {error ? <ErrorBox error={error} /> : !data ? <SkeletonRows /> : data.total === 0 ? (
        <Empty>没有匹配的运行 — 调整或清除筛选后重试</Empty>
      ) : (
        <>
          <div className="table-meta">
            显示 {shownFrom}–{shownTo} / 共 {data.total} 个运行（快照共 {allData?.total ?? data.total} 个）
          </div>
          <div className={`table-wrap${scrollable ? ' is-scrollable' : ''}`}>
            {scrollable ? <span className="scroll-hint" aria-hidden>横向滚动查看更多 →</span> : null}
            <div className="table-scroll panel" ref={tableWrapRef}>
              <table className="runs-table">
                <thead>
                  <tr>
                    <th scope="col">PR / 仓库</th>
                    <th scope="col" title="独立审查结论 — 与执行、发布、人工确认相互独立">审查结论</th>
                    <th scope="col" title="投递台账（webhook 轮）或项目执行（Matrix 轮）状态">执行状态</th>
                    <th scope="col" title="人工安全门决策">人工确认</th>
                    <th scope="col" title="GitHub check-run：回写事实 + 检查结论">GitHub 检查</th>
                    <th scope="col" title="开始时间（投递接收 / kickoff）与耗时">开始 · 耗时</th>
                    <th scope="col" className="th-right">详情</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((r) => {
                    const titleText = r.pr_title ?? (r.pr_number ? `PR #${r.pr_number}` : (r.repo ?? '仓库未记录'));
                    return (
                      <tr key={r.pack_id}>
                        <td className="cell-pr">
                          <span className="cell-title-line">
                            <Link
                              className="row-link cell-title"
                              to={`/runs/${r.pack_id}`}
                              state={listState}
                              title={r.pr_title ? `${r.pr_title}（run_id：${r.run_id ?? '未记录'}）` : `run_id：${r.run_id ?? '未记录'}`}
                            >
                              {titleText}
                            </Link>
                            {r.pr_url ? (
                              <a
                                className="gh-link"
                                href={r.pr_url}
                                target="_blank"
                                rel="noreferrer"
                                aria-label={`在 GitHub 打开 PR #${r.pr_number} 页面（新窗口）`}
                                title="GitHub PR 页面链接 — 是 PR 当前状态页，不是绑定该 head 的永久证据链接（证据以 head SHA 为准）"
                              >
                                ↗
                              </a>
                            ) : null}
                          </span>
                          <div className="cell-sub">
                            {r.repo ? <span title={`仓库：${r.repo}`}>{r.repo}</span> : <span className="muted">仓库未记录</span>}
                            <span className="cell-sub-dot">·</span>
                            <span className="muted">{r.trigger === 'webhook' ? 'webhook' : r.trigger === 'matrix' ? 'Matrix' : '触发未记录'}</span>
                            {r.head_sha ? <span className="cell-sha mono" title={`结论绑定的完整 head SHA：${r.head_sha}`}>{r.head_sha.slice(0, 8)}</span> : null}
                          </div>
                        </td>
                        <td><VerdictBadge review={r.review} /></td>
                        <td><ExecutionBadge execution={r.execution} /></td>
                        <td><GateBadge gate={r.review.human_gate} source={r.review.human_gate_source} /></td>
                        <td><PublishBadge publish={r.publish} /></td>
                        <td className="cell-time">
                          <div>{fmtTime(r.created_at) ?? '未记录'}</div>
                          <div className={r.duration_human ? 'cell-dur' : 'cell-dur muted'} title="投递接收→处理完成，或 kickoff→最后任务提交">
                            <Clock size={11} strokeWidth={1.75} aria-hidden />
                            {r.duration_human ?? '耗时未记录'}
                          </div>
                        </td>
                        <td className="th-right">
                          <Link
                            className="btn btn-sm"
                            to={`/runs/${r.pack_id}`}
                            state={listState}
                            aria-label={`查看运行详情：${r.pr_title ?? `PR #${r.pr_number ?? '?'}`}（${r.run_id ?? r.pack_id}）`}
                          >
                            查看详情 <ArrowRight size={12} strokeWidth={1.75} aria-hidden />
                          </Link>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
