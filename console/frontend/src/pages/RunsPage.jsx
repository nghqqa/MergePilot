import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { ArrowDown, ArrowRight, ArrowUpRight, Clock, RotateCcw, Search } from 'lucide-react';
import { api } from '../api.js';
import { ErrorBox, Empty, SkeletonRows } from '../ui.jsx';
import { ExecutionBadge, VerdictBadge, GateBadge, PublishBadge } from '../status.jsx';
import { executionMap } from '../status-map.js';
import { fmtTime } from '../format.js';

const QUICK_FILTERS = [
  { key: '', label: '全部' },
  { key: 'published', label: '已回写 GitHub' },
  { key: 'not_recorded', label: '无发布记录' },
];

// 严重度档位：默认排序"严重度 desc → 时间 desc"与结论列排序共用一套档位
const SEVERITIES = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'];
const SEVERITY_RANK = { CRITICAL: 4, HIGH: 3, MEDIUM: 2, LOW: 1 };
const severityOf = (r) =>
  String(r.review?.verdict ?? '').toUpperCase() === 'FINDING_CONFIRMED'
    ? String(r.review?.severity ?? '').toUpperCase()
    : '';
const EXECUTION_STATUSES = ['PROCESSED', 'RUNNING', 'PENDING', 'ERROR', 'COMPLETED', 'BLOCKED'];

// 表头排序按钮：点击在 降序 → 升序 → 恢复默认 之间循环
function SortTh({ label, title, sortKey, sort, onToggle }) {
  const active = sort?.key === sortKey;
  const dir = active ? sort.dir : null;
  return (
    <th
      scope="col"
      title={title}
      aria-sort={active ? (dir === 'desc' ? 'descending' : 'ascending') : undefined}
    >
      <button
        type="button"
        className={`th-sort${active ? ' th-sort-active' : ''}${dir === 'asc' ? ' th-sort-asc' : ''}`}
        onClick={() => onToggle(sortKey)}
        aria-label={`按${label}排序（当前：${active ? (dir === 'desc' ? '降序' : '升序') : '默认排序'}）`}
      >
        {label}
        <ArrowDown size={11} strokeWidth={2} aria-hidden className="sort-ico" />
      </button>
    </th>
  );
}

export default function RunsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const filters = {
    q: searchParams.get('q') ?? '',
    repo: searchParams.get('repo') ?? '',
    execution: searchParams.get('execution') ?? '',
    verdict: searchParams.get('verdict') ?? '',
    severity: searchParams.get('severity') ?? '',
    publish: searchParams.get('publish') ?? '',
  };
  const [data, setData] = useState(null);
  const [allData, setAllData] = useState(null);
  const [error, setError] = useState(null);
  const tableWrapRef = useRef(null);
  const [scrollable, setScrollable] = useState(false);
  // null = 默认排序（严重度 desc → 时间 desc）；否则 { key: 'verdict'|'time', dir }
  const [sort, setSort] = useState(null);
  const toggleSort = (key) =>
    setSort((s) => (s?.key === key ? (s.dir === 'desc' ? { key, dir: 'asc' } : null) : { key, dir: 'desc' }));

  const setFilter = (key, value) => {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(key, value);
    else next.delete(key);
    setSearchParams(next, { replace: true });
  };
  // 结论维度 chips / 下拉共用：选中再点一次即取消；severity 只随"发现确认"存在
  const setVerdictFilter = (verdict, severity = '') => {
    const next = new URLSearchParams(searchParams);
    const wasActive = filters.verdict === verdict
      && (verdict === 'FINDING_CONFIRMED' ? filters.severity === severity : !filters.severity);
    if (wasActive) {
      next.delete('verdict');
      next.delete('severity');
    } else {
      next.set('verdict', verdict);
      if (severity) next.set('severity', severity);
      else next.delete('severity');
    }
    setSearchParams(next, { replace: true });
  };
  const hasFilter = Object.values(filters).some(Boolean);
  const clear = () => {
    setSort(null);
    setSearchParams(new URLSearchParams(), { replace: true });
  };

  const load = useCallback(() => {
    setData(null);
    setError(null);
    // severity 与 verdict=NOT_RECORDED（结论未记录）为客户端维度，后端不识别，不下发
    api.runs({
      q: filters.q,
      repo: filters.repo,
      execution: filters.execution,
      verdict: filters.verdict === 'NOT_RECORDED' ? '' : filters.verdict,
      publish: filters.publish,
      limit: 200,
    }).then(setData).catch(setError);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  useEffect(() => {
    load();
  }, [load]);

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
      if (filters.verdict === 'NOT_RECORDED') { if (r.review?.verdict) return false; }
      else if (filters.verdict && (r.review?.verdict ?? '').toUpperCase() !== filters.verdict.toUpperCase()) return false;
      if (filters.severity && severityOf(r) !== filters.severity) return false;
      return true;
    });
  }, [allData, filters.q, filters.repo, filters.execution, filters.verdict, filters.severity]);

  const counts = useMemo(() => ({
    total: baseItems.length,
    published: baseItems.filter((r) => r.publish?.status === 'published').length,
    not_recorded: baseItems.filter((r) => r.publish?.status !== 'published').length,
  }), [baseItems]);

  // 体检条口径：搜索/仓库/执行状态范围内（不含结论维度）的结论分布
  const scopeNoVerdict = useMemo(() => {
    const items = allData?.items ?? [];
    return items.filter((r) => {
      if (filters.q && ![r.pack_id, r.run_id, r.repo, r.pr_title, r.head_sha, String(r.pr_number ?? '')]
        .some((v) => (v ?? '').toLowerCase().includes(filters.q.toLowerCase()))) return false;
      if (filters.repo && !(r.repo ?? '').toLowerCase().includes(filters.repo.toLowerCase())) return false;
      if (filters.execution && (r.execution?.status ?? '').toUpperCase() !== filters.execution.toUpperCase()) return false;
      return true;
    });
  }, [allData, filters.q, filters.repo, filters.execution]);

  const verdictCounts = useMemo(() => {
    const bySev = {};
    let ok = 0;
    let none = 0;
    for (const r of scopeNoVerdict) {
      const sev = severityOf(r);
      if (sev) bySev[sev] = (bySev[sev] ?? 0) + 1;
      else if (r.review?.verdict) ok += 1;
      else none += 1;
    }
    return { bySev, ok, none };
  }, [scopeNoVerdict]);

  // 结论未记录 / 严重度过滤在客户端完成；排序在过滤后的本页数据上进行
  const pageItems = useMemo(() => {
    let items = data?.items ?? [];
    if (filters.verdict === 'NOT_RECORDED') items = items.filter((r) => !r.review?.verdict);
    if (filters.severity) items = items.filter((r) => severityOf(r) === filters.severity);
    return items;
  }, [data, filters.verdict, filters.severity]);

  const sortedItems = useMemo(() => {
    const items = [...pageItems];
    const t = (r) => (r.created_at ? new Date(r.created_at).getTime() : 0);
    if (!sort) {
      items.sort((a, b) =>
        ((SEVERITY_RANK[severityOf(b)] ?? 0) - (SEVERITY_RANK[severityOf(a)] ?? 0)) || (t(b) - t(a)));
    } else if (sort.key === 'verdict') {
      const mul = sort.dir === 'desc' ? -1 : 1;
      items.sort((a, b) =>
        (mul * ((SEVERITY_RANK[severityOf(a)] ?? 0) - (SEVERITY_RANK[severityOf(b)] ?? 0))) || (t(b) - t(a)));
    } else {
      items.sort((a, b) => (sort.dir === 'desc' ? t(b) - t(a) : t(a) - t(b)));
    }
    return items;
  }, [pageItems, sort]);

  const sortLabel = !sort ? '严重度 → 时间'
    : sort.key === 'verdict' ? (sort.dir === 'desc' ? '结论严重度降序' : '结论严重度升序')
      : (sort.dir === 'desc' ? '开始时间新在前' : '开始时间旧在前');

  const repos = useMemo(
    () => [...new Set((allData?.items ?? []).map((r) => r.repo).filter(Boolean))].sort(),
    [allData]
  );

  const shownFrom = pageItems.length ? 1 : 0;
  const shownTo = pageItems.length;
  const listState = { from: `${location.pathname}${location.search}` };

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>审查运行</h1>
          <p className="page-sub">
            全部运行记录（按 run 维度，时间倒序）。PR 维度的聚合视图见仓库工作台；
            同一 PR 的多次运行在 PR 详情按 head 分组。各状态徽章可悬停或键盘聚焦查看语义边界。
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

      <div className="quick-filters" role="group" aria-label="按审查结论快筛（计数随搜索、仓库、执行状态筛选实时重算）">
        {SEVERITIES.filter((s) => verdictCounts.bySev[s]).map((s) => (
          <button
            key={s}
            className={`qf-chip${filters.verdict === 'FINDING_CONFIRMED' && filters.severity === s ? ' qf-active' : ''}`}
            onClick={() => setVerdictFilter('FINDING_CONFIRMED', s)}
            title={`审查结论 = 发现确认，且严重度 ${s}`}
          >
            确认发现 · {s}
            <span className="qf-count">{verdictCounts.bySev[s]}</span>
          </button>
        ))}
        {verdictCounts.ok ? (
          <button
            className={`qf-chip${filters.verdict === 'NOT_CONFIRMED' ? ' qf-active' : ''}`}
            onClick={() => setVerdictFilter('NOT_CONFIRMED')}
            title="审查结论 = 未发现问题（指本次审查未发现，不等于绝对无风险）"
          >
            未发现问题
            <span className="qf-count">{verdictCounts.ok}</span>
          </button>
        ) : null}
        {verdictCounts.none ? (
          <button
            className={`qf-chip${filters.verdict === 'NOT_RECORDED' ? ' qf-active' : ''}`}
            onClick={() => setVerdictFilter('NOT_RECORDED')}
            title="包内无独立审查结论记录 — 不等于无问题"
          >
            结论未记录
            <span className="qf-count">{verdictCounts.none}</span>
          </button>
        ) : null}
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
            {EXECUTION_STATUSES.map((s) => {
              const m = executionMap({ status: s });
              return <option key={s} value={s}>{m.label}（{s}）</option>;
            })}
          </select>
        </label>
        <label className="f-field">
          <span className="f-label">审查结论</span>
          <select
            value={filters.verdict}
            onChange={(e) => {
              const next = new URLSearchParams(searchParams);
              if (e.target.value) next.set('verdict', e.target.value);
              else next.delete('verdict');
              if (e.target.value !== 'FINDING_CONFIRMED') next.delete('severity');
              setSearchParams(next, { replace: true });
            }}
            aria-label="按审查结论筛选"
          >
            <option value="">全部</option>
            <option value="FINDING_CONFIRMED">发现确认</option>
            <option value="NOT_CONFIRMED">未发现问题</option>
            <option value="NOT_RECORDED">结论未记录</option>
          </select>
        </label>
        {hasFilter ? (
          <button className="btn btn-ghost f-clear" onClick={clear}>
            <RotateCcw size={13} strokeWidth={1.75} aria-hidden /> 清除筛选
          </button>
        ) : null}
      </div>

      {error ? <ErrorBox error={error} onRetry={load} /> : !data ? <SkeletonRows /> : !pageItems.length ? (
        <Empty>没有匹配的运行 — 调整或清除筛选后重试</Empty>
      ) : (
        <>
          <div className="table-meta">
            显示 {shownFrom}–{shownTo} / 共 {pageItems.length} 个运行 · 排序：{sortLabel}
            （快照共 {allData?.total ?? data.total} 个）
          </div>
          <div className={`table-wrap${scrollable ? ' is-scrollable' : ''}`}>
            {scrollable ? <span className="scroll-hint" aria-hidden>横向滚动查看更多 →</span> : null}
            <div className="table-scroll panel" ref={tableWrapRef}>
              <table className="runs-table">
                <thead>
                  <tr>
                    <th scope="col">PR / 仓库</th>
                    <SortTh
                      sortKey="verdict"
                      label="审查结论"
                      title="独立审查结论 — 与执行、发布、人工确认相互独立；点击按严重度排序"
                      sort={sort}
                      onToggle={toggleSort}
                    />
                    <th scope="col" title="投递台账（webhook 轮）或项目执行（Matrix 轮）状态">执行状态</th>
                    <th scope="col" title="人工安全门决策">人工确认</th>
                    <th scope="col" title="GitHub check-run：回写事实 + 检查结论">GitHub 检查</th>
                    <SortTh
                      sortKey="time"
                      label="开始 · 耗时"
                      title="开始时间（投递接收 / kickoff）与耗时；点击按开始时间排序"
                      sort={sort}
                      onToggle={toggleSort}
                    />
                    <th scope="col" className="th-right">详情</th>
                  </tr>
                </thead>
                <tbody>
                  {sortedItems.map((r) => {
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
                                <ArrowUpRight size={11} strokeWidth={1.75} aria-hidden />
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
