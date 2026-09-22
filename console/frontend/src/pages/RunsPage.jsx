import React, { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { Clock, RotateCcw, Search } from 'lucide-react';
import { api } from '../api.js';
import { Spinner, ErrorBox, Empty, Sha, SkeletonRows } from '../ui.jsx';
import { ExecutionBadge, VerdictBadge, GateBadge, PublishBadge } from '../status.jsx';
import { fmtTime } from '../format.js';

const QUICK_FILTERS = [
  { key: '', label: '全部' },
  { key: 'published', label: '已回写 GitHub' },
  { key: 'not_recorded', label: '未回写' },
];

export default function RunsPage() {
  const [data, setData] = useState(null);
  const [allData, setAllData] = useState(null); // 未过滤全集，用于计数 chips
  const [error, setError] = useState(null);
  const [filters, setFilters] = useState({ repo: '', q: '', execution: '', verdict: '', publish: '' });
  const tableWrapRef = React.useRef(null);
  const [scrollable, setScrollable] = useState(false);

  useEffect(() => {
    setData(null);
    setError(null);
    api
      .runs({ repo: filters.repo, q: filters.q, execution: filters.execution, verdict: filters.verdict, publish: filters.publish, limit: 200 })
      .then(setData)
      .catch(setError);
  }, [filters]);

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

  const counts = useMemo(() => {
    const items = allData?.items ?? [];
    return {
      total: items.length,
      published: items.filter((r) => r.publish?.status === 'published').length,
      not_recorded: items.filter((r) => r.publish?.status !== 'published').length,
    };
  }, [allData]);

  const set = (k) => (e) => setFilters((f) => ({ ...f, [k]: e.target.value }));
  const hasFilter = Object.values(filters).some(Boolean);
  const clear = () => setFilters({ repo: '', q: '', execution: '', verdict: '', publish: '' });
  const shownFrom = data?.total ? (data.offset ?? 0) + 1 : 0;
  const shownTo = data ? Math.min((data.offset ?? 0) + (data.items?.length ?? 0), data.total) : 0;

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>运行</h1>
          <p className="page-sub">
            真实历史运行的证据包索引（snapshot）。每个字段取自包内锁定文件，缺失显示"未记录"。
            执行状态／审查结论／发布状态为三个独立事实，悬停各徽章查看语义与来源。
          </p>
        </div>
      </div>

      <div className="quick-filters" role="group" aria-label="发布状态快筛">
        {QUICK_FILTERS.map((f) => (
          <button
            key={f.key}
            className={`qf-chip${filters.publish === f.key ? ' qf-active' : ''}`}
            onClick={() => setFilters((x) => ({ ...x, publish: f.key }))}
          >
            {f.label}
            <span className="qf-count">{f.key === '' ? counts.total : counts[f.key] ?? '—'}</span>
          </button>
        ))}
      </div>

      <div className="filter-bar" role="search">
        <div className="search-wrap">
          <Search size={14} strokeWidth={1.75} aria-hidden />
          <input placeholder="搜索 run_id / 仓库 / SHA / PR" value={filters.q} onChange={set('q')} aria-label="搜索" />
        </div>
        <input className="filter-input-repo" placeholder="仓库（包含匹配）" value={filters.repo} onChange={set('repo')} aria-label="仓库过滤" />
        <select value={filters.execution} onChange={set('execution')} aria-label="执行状态">
          <option value="">执行状态：全部</option>
          {['PROCESSED', 'RUNNING', 'PENDING', 'ERROR', 'COMPLETED', 'BLOCKED'].map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
        <select value={filters.verdict} onChange={set('verdict')} aria-label="审查结论">
          <option value="">审查结论：全部</option>
          <option value="FINDING_CONFIRMED">发现确认</option>
          <option value="NOT_CONFIRMED">无发现</option>
        </select>
        {hasFilter ? (
          <button className="btn btn-ghost" onClick={clear}>
            <RotateCcw size={13} strokeWidth={1.75} aria-hidden /> 重置
          </button>
        ) : null}
      </div>

      {error ? <ErrorBox error={error} /> : !data ? <SkeletonRows /> : data.total === 0 ? (
        <Empty>没有匹配的运行 — 调整筛选条件后重试</Empty>
      ) : (
        <>
          <div className="table-meta">
            显示 {shownFrom}–{shownTo} / 共 {data.total} 个运行 · 快照生成于 {fmtTime(data.generated_at)}
          </div>
          <div className={`table-wrap${scrollable ? ' is-scrollable' : ''}`}>
            {scrollable ? <span className="scroll-hint" aria-hidden>横向滚动查看更多 →</span> : null}
            <div className="table-scroll panel" ref={tableWrapRef}>
              <table className="runs-table">
                <thead>
                  <tr>
                    <th>仓库 / PR</th>
                    <th title="run_id；副行为结论绑定的完整 head SHA（点击复制）">run_id · head SHA</th>
                    <th title="投递台账状态（webhook 轮）或项目 meta 状态（Matrix 轮）">执行状态</th>
                    <th title="独立审查结论 — 与发布/审批相互独立">审查结论</th>
                    <th title="人工安全门决策">门</th>
                    <th title="GitHub check-run 回写事实">发布状态</th>
                    <th title="首行为开始时间（投递接收 / kickoff），副行为耗时">开始 · 耗时</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((r) => (
                    <tr key={r.pack_id}>
                      <td className="cell-repo">
                        <Link className="row-link" to={`/runs/${r.pack_id}`}>
                          {r.repo ?? <span className="muted">未记录</span>}
                        </Link>
                        <div className="cell-sub">
                          {r.pr_number ? (
                            r.pr_url ? <a href={r.pr_url} target="_blank" rel="noreferrer">PR #{r.pr_number} ↗</a> : `PR #${r.pr_number}`
                          ) : <span className="muted">PR 未记录</span>}
                          <span className="cell-sub-dot">·</span>
                          <span className="muted">{r.trigger === 'webhook' ? 'webhook' : r.trigger === 'matrix' ? 'Matrix' : '触发未记录'}</span>
                        </div>
                      </td>
                      <td className="cell-ident">
                        <Link className="row-link mono cell-runid" to={`/runs/${r.pack_id}`} title={r.run_id ?? 'run_id 未记录'}>
                          {r.run_id ?? <span className="muted">未记录</span>}
                        </Link>
                        <div className="cell-sub" title={r.head_sha ? `结论绑定的完整 head SHA：${r.head_sha}` : 'head SHA 未记录'}>
                          {r.head_sha ? <Sha value={r.head_sha} n={8} /> : <span className="muted">head SHA 未记录</span>}
                        </div>
                      </td>
                      <td><ExecutionBadge execution={r.execution} /></td>
                      <td><VerdictBadge review={r.review} /></td>
                      <td><GateBadge gate={r.review.human_gate} /></td>
                      <td><PublishBadge publish={r.publish} /></td>
                      <td className="cell-time">
                        <div>{fmtTime(r.created_at) ?? '未记录'}</div>
                        <div className={r.duration_human ? 'cell-dur' : 'cell-dur muted'} title="投递接收→处理完成，或 kickoff→最后任务提交">
                          <Clock size={11} strokeWidth={1.75} aria-hidden />
                          {r.duration_human ?? '耗时未记录'}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
