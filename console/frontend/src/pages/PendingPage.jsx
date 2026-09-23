import React, { useMemo } from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight, ArrowUpRight } from 'lucide-react';
import { useAppConfig } from '../App.jsx';
import { useAllRuns, useDataSource } from '../hooks.js';
import { groupRunsByPr } from '../pr-model.js';
import { VerdictBadge } from '../status.jsx';
import { fmtTime } from '../format.js';
import { ErrorBox, SkeletonRows } from '../ui.jsx';

// 待处理：跨仓库汇聚"有待处理发现（最近记录）"的 PR（仅 snapshot 历史口径）。
// 真实待办由后端有效票据/当前状态提供（C-4/C-11 未实现）——其他数据源如实说明。
export default function PendingPage() {
  const config = useAppConfig();
  const { source } = useDataSource(config);
  const { data, error, retry } = useAllRuns();
  const pending = useMemo(
    () => groupRunsByPr(data?.items ?? []).filter((p) => p.attention.flag === 'decision'),
    [data]
  );

  if (source.kind !== 'snapshot') {
    return (
      <div>
        <div className="page-head">
          <div>
            <h1>待处理</h1>
            <p className="page-sub">
              真实待办由后端的有效票据/当前状态提供（审批只读与决策接口均未实现，C-4/C-11）。
            </p>
          </div>
        </div>
        <div className="state-box state-warn" role="status">
          当前数据源（{source.kind}）不提供结论与票据字段——无法汇总待办，也不从历史 HIGH/APPROVED
          推导当前待办。历史记录中需关注的 PR 请到对应仓库的 PR 列表查看。
          前往 <Link to="/repos">仓库工作台</Link> 或 <Link to="/approvals">审批（fixture 演练）</Link>。
        </div>
      </div>
    );
  }

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>待处理</h1>
          <p className="page-sub">
            历史模式：以下为"历史记录中需关注"的 PR（判定：各自最近一次运行记录存在未闭环发现；
            历史 HIGH 不生成当前待办）。真实待办由后端的有效票据/当前状态提供（C-4/C-11 未实现）——
            本页不提供可执行的真实审批操作。
          </p>
        </div>
      </div>

      {error ? <ErrorBox error={error} onRetry={retry} /> : !data ? <SkeletonRows rows={4} cols={6} /> : (
        <>
          <div className="table-meta">{pending.length} 个 PR 需关注（历史口径，按 PR 统计，依据 = 各自最近一次运行记录；非当前 head 结论）</div>
          {!pending.length ? (
            <div className="state-box state-ok">当前快照范围内没有"有待处理发现"的 PR。</div>
          ) : (
            <div className="panel">
              <table className="pr-table">
                <thead>
                  <tr>
                    <th scope="col">PR</th>
                    <th scope="col">仓库</th>
                    <th scope="col" title="基于该 PR 最近一次运行记录">最近审查（最近记录）</th>
                    <th scope="col">最近活动</th>
                    <th scope="col" className="th-right">详情</th>
                  </tr>
                </thead>
                <tbody>
                  {pending.map((pr) => {
                    const to = `/repos/${encodeURIComponent(pr.owner)}/${encodeURIComponent(pr.name)}/pr/${pr.prNumber}`;
                    return (
                      <tr key={pr.key}>
                        <td className="cell-pr">
                          <span className="cell-title-line">
                            <Link className="row-link cell-title" to={to}>
                              {pr.title ?? `PR #${pr.prNumber}`}
                            </Link>
                            {pr.prUrl ? (
                              <a className="gh-link" href={pr.prUrl} target="_blank" rel="noreferrer"
                                aria-label={`在 GitHub 打开 ${pr.repo} PR #${pr.prNumber}（新窗口）`}>
                                <ArrowUpRight size={11} strokeWidth={1.75} aria-hidden />
                              </a>
                            ) : null}
                          </span>
                          <div className="cell-sub muted">#{pr.prNumber} · {pr.attention.label}</div>
                        </td>
                        <td className="mono cell-context">{pr.repo}</td>
                        <td><VerdictBadge review={pr.latest?.review} /></td>
                        <td className="cell-time">{fmtTime(pr.activityAt) ?? '未记录'}</td>
                        <td className="th-right">
                          <Link className="btn btn-sm" to={to}>
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

          <div className="panel approvals-entry">
            <div>
              <strong>审批服务未接入</strong>
              <div className="muted">
                票据存储已拍板 SQLite WAL（C-4/P-1）；站内决策接口未实现（C-11）。
                可在测试数据模式下演练审批交互（合成票据，不产生真实审批）。
              </div>
            </div>
            <Link className="btn" to="/approvals">打开审批（fixture 演练）</Link>
          </div>
        </>
      )}
    </div>
  );
}
