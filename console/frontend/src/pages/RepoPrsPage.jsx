import React, { useMemo, useState } from 'react';
import { Link, useLocation, useParams, useSearchParams } from 'react-router-dom';
import { ArrowRight, ArrowUpRight, Search } from 'lucide-react';
import { useAllRuns, useScrollRestore } from '../hooks.js';
import { groupRunsByPr, paginate } from '../pr-model.js';
import { VerdictBadge } from '../status.jsx';
import { fmtTime } from '../format.js';
import { Empty, ErrorBox, SkeletonRows } from '../ui.jsx';

const PER_PAGE = 10;

function prMatches(pr, q) {
  const needle = q.trim().toLowerCase();
  if (!needle) return true;
  return [
    pr.title, `#${pr.prNumber}`, String(pr.prNumber), pr.repo,
    ...pr.heads.map((h) => h.head ?? ''),
    ...pr.runs.flatMap((r) => [r.run_id, r.pack_id, r.head_sha]),
  ].some((v) => (v ?? '').toLowerCase().includes(needle));
}

// 仓库内的 PR 列表：一个 PR 一行（同 PR 多次 run 聚合，历史收进详情）。
// 筛选/分页写入 URL，返回时保留；滚动位置按路由 entry 恢复。
export default function RepoPrsPage() {
  const params = useParams();
  const owner = params.owner ?? '';
  const name = params.name ?? '';
  const repo = `${owner}/${name}`;
  const [searchParams, setSearchParams] = useSearchParams();
  const location = useLocation();
  useScrollRestore();
  const { data, error, retry } = useAllRuns();
  const [toast, setToast] = useState('');

  const q = searchParams.get('q') ?? '';
  const attentionOnly = searchParams.get('attention') === '1';
  const page = Number(searchParams.get('page') ?? '1') || 1;

  const setParam = (key, value) => {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(key, value);
    else next.delete(key);
    if (key !== 'page') next.delete('page');
    setSearchParams(next, { replace: false });
  };

  const allPrs = useMemo(() => groupRunsByPr(data?.items ?? []), [data]);
  const known = useMemo(() => data ? allPrs.some((p) => p.repo === repo) || (data.items ?? []).some((r) => r.repo === repo) : null, [data, allPrs, repo]);
  const prs = useMemo(
    () => allPrs
      .filter((p) => p.repo === repo)
      .filter((p) => (attentionOnly ? p.attention.flag === 'decision' : true))
      .filter((p) => prMatches(p, q)),
    [allPrs, repo, attentionOnly, q]
  );
  const view = paginate(prs, page, PER_PAGE);

  const copyHead = async (sha) => {
    try {
      await navigator.clipboard.writeText(sha);
      setToast('已复制完整 head SHA');
      setTimeout(() => setToast(''), 1400);
    } catch { /* clipboard unavailable */ }
  };

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="breadcrumb">
            <Link to="/repos" className="crumb-back">仓库</Link>
          </div>
          <h1 className="mono">{repo}</h1>
          <p className="page-sub">
            历史数据中的仓库 · PR 摘要基于各 PR 最近一次运行记录 —— 快照无 GitHub 当前 head 权威数据，
            不代表当前 head 状态（接口需求 C-10）。
          </p>
        </div>
      </div>

      {toast ? <div className="state-box state-ok" role="status">{toast}</div> : null}
      {error ? <ErrorBox error={error} onRetry={retry} /> : !data ? <SkeletonRows rows={6} cols={6} /> : known === false ? (
        <div className="state-box state-warn">
          该仓库不在历史快照中 — 仓库列表来自历史运行记录，可能从未产生过运行。返回
          <Link to="/repos">仓库工作台</Link>。
        </div>
      ) : (
        <>
          <div className="filter-bar" role="search" aria-label="PR 筛选">
            <label className="f-field f-grow">
              <span className="f-label">搜索</span>
              <span className="search-wrap">
                <Search size={14} strokeWidth={1.75} aria-hidden />
                <input
                  placeholder="PR 标题 / 编号 / head SHA / run_id"
                  value={q}
                  onChange={(e) => setParam('q', e.target.value)}
                  aria-label="在本仓库的 PR 中搜索"
                />
              </span>
            </label>
            <button
              type="button"
              className={`qf-chip${attentionOnly ? ' qf-active' : ''}`}
              onClick={() => setParam('attention', attentionOnly ? '' : '1')}
              aria-pressed={attentionOnly}
            >
              需要处理
            </button>
          </div>

          <div className="table-meta">
            共 {view.total} 个 PR（按 PR 统计 · 背后 {prs.reduce((n, p) => n + p.runs.length, 0)} 次运行记录）
            {view.pages > 1 ? <> · 第 {view.page} / {view.pages} 页</> : null}
          </div>

          {!view.items.length ? (
            <Empty>{attentionOnly ? '当前筛选范围内没有"有待处理发现"的 PR' : '没有匹配的 PR'}</Empty>
          ) : (
            <div className="panel">
              <table className="pr-table">
                <thead>
                  <tr>
                    <th scope="col">PR</th>
                    <th scope="col">上下文</th>
                    <th scope="col" title="基于该 PR 最近一次运行记录，非当前 head 结论">最近审查（最近记录）</th>
                    <th scope="col">需要处理</th>
                    <th scope="col">最近活动</th>
                    <th scope="col" className="th-right">详情</th>
                  </tr>
                </thead>
                <tbody>
                  {view.items.map((pr) => {
                    const detailTo = `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/pr/${pr.prNumber}`;
                    return (
                      <tr key={pr.key}>
                        <td className="cell-pr">
                          <span className="cell-title-line">
                            <Link
                              className="row-link cell-title"
                              to={detailTo}
                              state={{ from: `${location.pathname}${location.search}` }}
                              title={pr.title ?? `PR #${pr.prNumber}`}
                            >
                              {pr.title ?? `PR #${pr.prNumber}`}
                            </Link>
                            {pr.prUrl ? (
                              <a
                                className="gh-link"
                                href={pr.prUrl}
                                target="_blank"
                                rel="noreferrer"
                                aria-label={`在 GitHub 打开 PR #${pr.prNumber}（新窗口）`}
                                title="GitHub PR 当前状态页 — 当前 head 与可合并性以 GitHub 为准"
                              >
                                <ArrowUpRight size={11} strokeWidth={1.75} aria-hidden />
                              </a>
                            ) : null}
                          </span>
                          <div className="cell-sub muted">#{pr.prNumber}</div>
                        </td>
                        <td className="cell-context">
                          <div>{pr.heads.length} 个 head</div>
                          <div className="cell-sub">
                            {pr.heads.slice(0, 2).map((h) => (
                              h.head ? (
                                <button
                                  key={h.head}
                                  type="button"
                                  className="head-chip mono"
                                  onClick={() => copyHead(h.head)}
                                  title={`复制完整 head SHA：${h.head}（该 head 有 ${h.runs.length} 次记录）`}
                                >
                                  {h.head.slice(0, 8)}
                                </button>
                              ) : (
                                <span key="null" className="head-chip mono">head 未记录</span>
                              )
                            ))}
                            {pr.heads.length > 2 ? <span className="muted">+{pr.heads.length - 2}</span> : null}
                          </div>
                        </td>
                        <td><VerdictBadge review={pr.latest?.review} /></td>
                        <td className="cell-attention">
                          {pr.attention.flag === 'decision' ? (
                            <span className="attention-flag"><span className="attention-dot" aria-hidden />{pr.attention.label}</span>
                          ) : (
                            <span className="muted">{pr.attention.label}</span>
                          )}
                        </td>
                        <td className="cell-time">{fmtTime(pr.activityAt) ?? '未记录'}</td>
                        <td className="th-right">
                          <Link
                            className="btn btn-sm"
                            to={detailTo}
                            state={{ from: `${location.pathname}${location.search}` }}
                            aria-label={`查看 PR #${pr.prNumber} 详情`}
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
          )}

          {view.pages > 1 ? (
            <nav className="pager" aria-label="PR 分页">
              <button
                type="button" className="btn btn-sm" disabled={view.page <= 1}
                onClick={() => setParam('page', String(view.page - 1))}
              >
                上一页
              </button>
              <span className="pager-status">第 {view.page} / {view.pages} 页</span>
              <button
                type="button" className="btn btn-sm" disabled={view.page >= view.pages}
                onClick={() => setParam('page', String(view.page + 1))}
              >
                下一页
              </button>
            </nav>
          ) : null}
        </>
      )}
    </div>
  );
}
