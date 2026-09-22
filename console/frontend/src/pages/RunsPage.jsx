import React, { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api.js';
import { Spinner, ErrorBox, Empty, Sha, Badge } from '../ui.jsx';
import { ExecutionBadge, VerdictBadge, GateBadge, PublishBadge } from '../status.jsx';
import { fmtTime } from '../format.js';

export default function RunsPage() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [filters, setFilters] = useState({ repo: '', q: '', execution: '', verdict: '', publish: '' });

  useEffect(() => {
    setData(null);
    setError(null);
    api
      .runs({ repo: filters.repo, q: filters.q, execution: filters.execution, verdict: filters.verdict, publish: filters.publish, limit: 200 })
      .then(setData)
      .catch(setError);
  }, [filters]);

  const set = (k) => (e) => setFilters((f) => ({ ...f, [k]: e.target.value }));

  return (
    <div>
      <div className="page-head">
        <h1>运行</h1>
        <p className="page-sub">
          真实历史运行的证据包索引（snapshot）。每个字段取自包内锁定文件，缺失显示"未记录"。
          执行状态／审查结论／发布状态为三个独立事实，见列说明。
        </p>
      </div>

      <div className="filter-bar" role="search">
        <input placeholder="搜索 run_id / 仓库 / SHA / PR" value={filters.q} onChange={set('q')} aria-label="搜索" />
        <input placeholder="仓库（包含匹配）" value={filters.repo} onChange={set('repo')} aria-label="仓库过滤" />
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
        <select value={filters.publish} onChange={set('publish')} aria-label="发布状态">
          <option value="">发布状态：全部</option>
          <option value="published">已回写 check-run</option>
          <option value="not_recorded">未回写 GitHub</option>
        </select>
      </div>

      {error ? <ErrorBox error={error} /> : !data ? <Spinner /> : data.total === 0 ? <Empty>没有匹配的运行</Empty> : (
        <>
          <div className="table-meta">{data.total} 个运行 · 快照生成于 {fmtTime(data.generated_at)}</div>
          <div className="table-scroll">
            <table className="runs-table">
              <thead>
                <tr>
                  <th>仓库 / PR</th>
                  <th>run_id</th>
                  <th title="被审查 commit 的完整 head SHA（点击复制）">head SHA</th>
                  <th title="投递台账状态（webhook 轮）或项目 meta 状态（Matrix 轮）">执行状态</th>
                  <th title="独立审查结论 — 与发布/审批相互独立">审查结论</th>
                  <th title="人工安全门决策">门</th>
                  <th title="GitHub check-run 回写事实">发布状态</th>
                  <th>开始时间</th>
                  <th title="投递接收→处理完成，或 kickoff→最后任务提交">耗时</th>
                  <th title="证据包是否带 SHA256SUMS 锁定清单">完整性</th>
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
                        {' · '}
                        <span className="muted">{r.trigger === 'webhook' ? 'webhook' : r.trigger === 'matrix' ? 'Matrix' : '触发未记录'}</span>
                      </div>
                    </td>
                    <td>
                      <Link className="row-link mono" to={`/runs/${r.pack_id}`} title={r.run_id ?? 'run_id 未记录'}>
                        {r.run_id ?? <span className="muted">未记录</span>}
                      </Link>
                    </td>
                    <td><Sha value={r.head_sha} /></td>
                    <td><ExecutionBadge execution={r.execution} /></td>
                    <td><VerdictBadge review={r.review} /></td>
                    <td><GateBadge gate={r.review.human_gate} /></td>
                    <td><PublishBadge publish={r.publish} /></td>
                    <td className="cell-time">{fmtTime(r.created_at) ?? <span className="muted">未记录</span>}</td>
                    <td>{r.duration_human ?? <span className="muted">—</span>}</td>
                    <td>
                      {r.has_sums ? (
                        <Badge tone="ok" title="包内有 SHA256SUMS，可在详情页执行完整校验">SUMS</Badge>
                      ) : (
                        <Badge tone="warn" title="包内无 SHA256SUMS">无 SUMS</Badge>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
