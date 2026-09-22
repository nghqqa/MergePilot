import React, { useMemo } from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight, FolderGit2 } from 'lucide-react';
import { useAllRuns } from '../hooks.js';
import { groupRunsByPr, reposFromPrs } from '../pr-model.js';
import { fmtTime } from '../format.js';
import { ErrorBox, SkeletonRows } from '../ui.jsx';

// 仓库工作台（默认入口）。仓库列表来自历史快照 —— 标注"历史数据中的仓库"，
// 不展示虚构的连接状态或活跃度；PR 数与 run 数分开统计。
export default function ReposPage() {
  const { data, error, retry } = useAllRuns();
  const prs = useMemo(() => groupRunsByPr(data?.items ?? []), [data]);
  const repos = useMemo(() => reposFromPrs(prs), [prs]);
  const latestActivity = prs[0]?.activityAt ?? null;
  const totalRuns = repos.reduce((n, r) => n + r.runCount, 0);

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>仓库</h1>
          <p className="page-sub">
            以仓库和 PR 为中心的管理工作台。当前数据模式 snapshot：以下仓库来自历史数据中的运行记录，
            不是已授权接入的实时连接。
          </p>
        </div>
      </div>

      {error ? <ErrorBox error={error} onRetry={retry} /> : !data ? <SkeletonRows rows={4} cols={3} /> : (
        <>
          <div className="table-meta">
            {repos.length} 个仓库 · {prs.length} 个 PR · {totalRuns} 次运行记录
            （PR / run 分别统计）{latestActivity ? <> · 数据截至最近记录 {fmtTime(latestActivity)}</> : null}
          </div>
          <div className="repo-list">
            {repos.map((r) => (
              <div key={r.repo} className="panel repo-card">
                <div className="repo-card-main">
                  <FolderGit2 size={18} strokeWidth={1.75} aria-hidden className="repo-card-ico" />
                  <div>
                    <Link className="repo-card-name" to={`/repos/${encodeURIComponent(r.owner)}/${encodeURIComponent(r.name)}`}>
                      {r.repo}
                    </Link>
                    <div className="repo-card-meta">
                      <span className="chip">历史数据中的仓库</span>
                      <span className="chip">{r.prCount} 个 PR</span>
                      <span className="chip">{r.runCount} 次运行记录</span>
                    </div>
                  </div>
                </div>
                <Link
                  className="btn"
                  to={`/repos/${encodeURIComponent(r.owner)}/${encodeURIComponent(r.name)}`}
                  aria-label={`查看 ${r.repo} 的 PR 列表`}
                >
                  查看 PR 列表 <ArrowRight size={12} strokeWidth={1.75} aria-hidden />
                </Link>
              </div>
            ))}
            {!repos.length ? <div className="state-box state-empty">历史快照中没有可聚合的 PR 运行记录</div> : null}
          </div>
        </>
      )}
    </div>
  );
}
