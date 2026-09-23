import React from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight, FolderGit2 } from 'lucide-react';
import { useAppConfig } from '../App.jsx';
import { useDataSource, useSourceQuery } from '../hooks.js';
import { fmtTime } from '../format.js';
import { ErrorBox, SkeletonRows } from '../ui.jsx';

// 仓库工作台（默认入口）。
// snapshot 源：仓库列表来自历史数据（标注"历史数据中的仓库"，PR/run 分口径统计）；
// contract 源：仓库清单由可信服务配置声明（未来来自 installation 映射），无虚构计数。
export default function ReposPage() {
  const config = useAppConfig();
  const { source } = useDataSource(config);
  const [attempt, setAttempt] = React.useState(0);
  const reposQ = useSourceQuery(() => source.listRepos(), [source, attempt]);
  const repos = reposQ.status === 'done' ? reposQ.data : [];
  const contract = source.kind === 'contract';
  const totalPrs = repos.reduce((n, r) => n + (r.prCount ?? 0), 0);
  const totalRuns = repos.reduce((n, r) => n + (r.runCount ?? 0), 0);
  const latestActivity = repos.reduce((a, r) => (r.activityAt && (!a || r.activityAt > a) ? r.activityAt : a), null);

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>仓库</h1>
          <p className="page-sub">
            以仓库和 PR 为中心的管理工作台。
            {contract
              ? ' 数据源为正式契约端点。'
              : source.kind === 'console-pg'
                ? ' 数据源为隔离 PG 只读服务：以下为 fixture 测试记录（非真实运行），仓库含 run 的 pr/run 计数。'
                : ' 当前数据模式 snapshot：以下仓库来自历史数据中的运行记录，不是已授权接入的实时连接。'}
          </p>
        </div>
      </div>

      {reposQ.status === 'error' ? (
        <ErrorBox error={reposQ.error} onRetry={() => setAttempt((n) => n + 1)} />
      ) : reposQ.status !== 'done' ? (
        <SkeletonRows rows={4} cols={3} />
      ) : (
        <>
          <div className="table-meta">
            {repos.length} 个仓库
            {contract
              ? '（由服务配置声明）'
              : ` · ${totalPrs} 个 PR · ${totalRuns} 次运行记录（PR / run 分别统计）${latestActivity ? ` · 数据截至最近记录 ${fmtTime(latestActivity)}` : ''}`}
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
                      {contract ? (
                        <>
                          <span className="chip">已接入仓库（契约数据源）</span>
                          {config?.dataMode === 'fixture' ? <span className="chip">Fixture 数据</span> : null}
                        </>
                      ) : source.kind === 'console-pg' ? (
                        <>
                          <span className="chip">PG Fixture 测试记录</span>
                          <span className="chip">{r.prCount} 个 PR</span>
                          <span className="chip">{r.runCount} 次运行记录</span>
                        </>
                      ) : (
                        <>
                          <span className="chip">历史数据中的仓库</span>
                          <span className="chip">{r.prCount} 个 PR</span>
                          <span className="chip">{r.runCount} 次运行记录</span>
                        </>
                      )}
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
            {!repos.length ? <div className="state-box state-empty">没有可展示的仓库</div> : null}
          </div>
        </>
      )}
    </div>
  );
}
