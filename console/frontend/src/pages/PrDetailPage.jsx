import React, { useMemo } from 'react';
import { Link, useParams } from 'react-router-dom';
import {
  ArrowRight, ArrowUpRight, Clock, FileSearch, Hand, ShieldCheck, Workflow,
} from 'lucide-react';
import { useAllRuns } from '../hooks.js';
import { groupRunsByPr } from '../pr-model.js';
import { ExecutionBadge, VerdictBadge, GateBadge, PublishBadge } from '../status.jsx';
import { fmtTime } from '../format.js';
import { Empty, ErrorBox, SkeletonRows } from '../ui.jsx';

function MiniState({ icon: Icon, label, children }) {
  return (
    <div className="mini-state">
      <div className="mini-state-label">
        <Icon size={12} strokeWidth={1.75} aria-hidden /> {label}
      </div>
      <div>{children}</div>
    </div>
  );
}

// PR 详情：以 PR 为主对象，run 降为历史。
// 摘要一律标注基于哪条记录（最近/最近完成）；审批与合并是两件事，站内均未启用。
export default function PrDetailPage() {
  const params = useParams();
  const owner = params.owner ?? '';
  const name = params.name ?? '';
  const prNumber = Number(params.prNumber);
  const repo = `${owner}/${name}`;
  const { data, error, retry } = useAllRuns();

  const pr = useMemo(() => groupRunsByPr(data?.items ?? [])
    .find((p) => p.repo === repo && p.prNumber === prNumber), [data, repo, prNumber]);

  if (error) return <div><ErrorBox error={error} onRetry={retry} /></div>;
  if (!data) return <div className="detail-skeleton"><SkeletonRows rows={6} /></div>;
  if (!pr) {
    return (
      <div>
        <div className="breadcrumb"><Link className="crumb-back" to={`/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}`}>返回 {repo}</Link></div>
        <Empty>历史快照中没有该仓库 PR #{prNumber || '？'} 的运行记录</Empty>
      </div>
    );
  }

  const summaryRun = pr.latestCompleted ?? pr.latest;
  const summaryBasis = pr.latestCompleted
    ? (pr.latest === pr.latestCompleted ? '基于最近一次运行记录' : '基于最近一次完成的运行记录（最近一次记录仍在进行中）')
    : '基于最近一次运行记录（快照内无完成态记录）';
  const confirmed = /FINDING_CONFIRMED/i.test(String(summaryRun?.review?.verdict ?? ''));
  const repoTo = `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}`;
  const runTo = (packId) => `/runs/${packId}`;

  return (
    <div>
      <div className="breadcrumb">
        <Link to={repoTo} className="crumb-back">{repo}</Link>
        <span className="crumb-sep">/</span>
        <span className="crumb-current">PR #{pr.prNumber}</span>
      </div>

      <div className="detail-head">
        <div className="detail-head-main">
          <h1 className="detail-title">{pr.title ?? `PR #${pr.prNumber}`}</h1>
          <div className="detail-chips">
            <span className="chip">PR #{pr.prNumber}</span>
            <span className="chip mono">{repo}</span>
            <span className="chip">{pr.heads.length} 个历史 head</span>
            <span className="chip">{pr.runs.length} 次运行记录</span>
            <span className="chip">历史快照</span>
          </div>
        </div>
        <div className="detail-actions">
          {pr.prUrl ? (
            <a className="btn btn-primary" href={pr.prUrl} target="_blank" rel="noreferrer">
              <ArrowUpRight size={13} strokeWidth={1.75} aria-hidden /> 在 GitHub 查看 PR
            </a>
          ) : null}
        </div>
      </div>

      <p className="section-note">
        当前 head 与 PR 状态以 GitHub 为准；快照无当前 head 权威数据（C-10），
        以下摘要{summaryBasis}，不自动代表当前 head 结论。
        站内审批未接入（C-11 提案）；站内合并为已记录的范围变更（C-12），未启用。
      </p>

      <section className="section">
        <div className="section-head">
          <FileSearch size={14} strokeWidth={1.75} aria-hidden className="section-ico" />
          <h3>审查摘要</h3>
        </div>
        <div className="finding-statement">
          <VerdictBadge review={summaryRun?.review} />
          <span className="finding-text muted">
            {confirmed && summaryRun?.review?.cwe ? <code className="mono">{summaryRun.review.cwe}</code> : null}
            {summaryBasis} · run {summaryRun?.run_id ?? summaryRun?.pack_id ?? '未记录'}
          </span>
          <Link className="link-btn" to={runTo(summaryRun?.pack_id)}>查看该次运行</Link>
        </div>
      </section>

      <section className="section">
        <div className="section-head">
          <Hand size={14} strokeWidth={1.75} aria-hidden className="section-ico" />
          <h3>等待用户处理</h3>
        </div>
        {pr.attention.flag === 'decision' ? (
          <div className="finding-statement">
            <span className="attention-flag"><span className="attention-dot" aria-hidden />{pr.attention.label}</span>
            <span className="finding-text muted">人工确认/修复授权尚未闭环（历史 HIGH 不代表当前 head 仍有风险）。</span>
            <Link className="link-btn" to="/approvals">前往审批（fixture 演练）</Link>
          </div>
        ) : (
          <p className="section-note">无基于最近记录的待处理事项。</p>
        )}
      </section>

      <section className="section">
        <div className="section-head">
          <Workflow size={14} strokeWidth={1.75} aria-hidden className="section-ico" />
          <h3>阶段状态</h3>
        </div>
        <div className="mini-states">
          <MiniState icon={Workflow} label="执行状态"><ExecutionBadge execution={summaryRun?.execution} /></MiniState>
          <MiniState icon={FileSearch} label="审查结论"><VerdictBadge review={summaryRun?.review} /></MiniState>
          <MiniState icon={Hand} label="人工确认"><GateBadge gate={summaryRun?.review?.human_gate} source={summaryRun?.review?.human_gate_source} /></MiniState>
          <MiniState icon={ShieldCheck} label="GitHub 检查"><PublishBadge publish={summaryRun?.publish} /></MiniState>
        </div>
        <p className="section-note">
          修复补丁与验证结果以对应 run 证据包为准（见该次运行的证据与版本清单）；
          GitHub check-run 的 conclusion 是审查结论事实，不代表已合并。
        </p>
      </section>

      <section className="section">
        <div className="section-head">
          <Clock size={14} strokeWidth={1.75} aria-hidden className="section-ico" />
          <h3>历史运行（{pr.runs.length} 次）</h3>
        </div>
        <details className="tech-details">
          <summary>按 head 展开 {pr.heads.length} 组历史记录</summary>
          <div className="tech-body">
            {pr.heads.map((h) => (
              <div key={h.head ?? 'null'} className="head-group">
                <h4 className="mono">
                  {h.head ? `head ${h.head.slice(0, 12)}` : 'head 未记录'}
                  <span className="muted"> · {h.runs.length} 次记录 · 最近 {fmtTime(h.latest?.created_at) ?? '未记录'}</span>
                </h4>
                <table className="data-table">
                  <thead>
                    <tr><th>时间</th><th>run_id</th><th>审查结论（该 head 最近）</th><th>耗时</th><th className="th-right">运行详情</th></tr>
                  </thead>
                  <tbody>
                    {h.runs.map((r, i) => (
                      <tr key={r.pack_id}>
                        <td className="cell-time">{fmtTime(r.created_at) ?? '未记录'}</td>
                        <td className="mono">{r.run_id ?? <span className="muted">未记录</span>}</td>
                        <td>{i === 0 ? <VerdictBadge review={r.review} /> : <span className="muted">同上组内更早</span>}</td>
                        <td className="cell-time">{r.duration_human ?? '—'}</td>
                        <td className="th-right">
                          <Link className="link-btn" to={runTo(r.pack_id)}>打开 <ArrowRight size={11} strokeWidth={1.75} aria-hidden /></Link>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <p className="section-note">
                  该组结论只代表此 head：不同 head 的结果互不覆盖，也不代表 PR 当前状态。
                </p>
              </div>
            ))}
          </div>
        </details>
      </section>
    </div>
  );
}
