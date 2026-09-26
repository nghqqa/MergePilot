import { Typography } from 'antd';
import React, { useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import {
  ArrowRight, ArrowUpRight, Clock, FileSearch, Hand, ShieldCheck, Workflow,
} from 'lucide-react';
import { useAppConfig } from '../App.jsx';
import { fetchRunsSnapshotOnce, useDataSource, useSourceQuery } from '../hooks.js';
import { groupRunsByPr } from '../pr-model.js';
import { ExecutionBadge, VerdictBadge, GateBadge, PublishBadge } from '../status.jsx';
import { fmtTime } from '../format.js';
import { Empty, ErrorBox, SkeletonRows, Spinner } from '../ui.jsx';

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

// PR 详情入口：按数据源分派（页面结构两种来源共用，数据形状各自映射）。
export default function PrDetailPage() {
  const params = useParams();
  const owner = params.owner ?? '';
  const name = params.name ?? '';
  const prNumber = Number(params.prNumber);
  const config = useAppConfig();
  const { source } = useDataSource(config);

  if (source.kind === 'contract') {
    return <ContractPrDetail owner={owner} name={name} prNumber={prNumber} />;
  }
  if (source.kind === 'console-pg') {
    return <PgPrDetail owner={owner} name={name} prNumber={prNumber} />;
  }
  return <SnapshotPrDetail owner={owner} name={name} prNumber={prNumber} />;
}

// ---- console_pg 源（DEV/隔离联调适配：tools/console_pg 只读服务，PG fixture 记录） ----

function PgPrDetail({ owner, name, prNumber }) {
  const repo = `${owner}/${name}`;
  const config = useAppConfig();
  const { source } = useDataSource(config);
  const [attempt, setAttempt] = useState(0);
  const [openRun, setOpenRun] = useState(null);
  const q = useSourceQuery(() => source.getPr(repo, prNumber), [source, repo, prNumber, attempt]);
  const detailQ = useSourceQuery(() => source.getRunDetail(openRun), [openRun], { enabled: !!openRun });

  const repoTo = `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}`;

  if (q.status === 'error') {
    return (
      <div>
        <div className="breadcrumb"><Link className="crumb-back" to={repoTo}>返回 {repo}</Link></div>
        {q.error?.status === 404 ? (
          <div className="state-box state-warn">PG 服务未提供 {repo} PR #{prNumber || '？'} 的记录（404）——不回退到历史快照数据。</div>
        ) : (
          <ErrorBox error={q.error} onRetry={() => setAttempt((n) => n + 1)} />
        )}
      </div>
    );
  }
  if (q.status !== 'done') return <div className="detail-skeleton"><SkeletonRows rows={6} /></div>;
  const { view, detail } = q.data ?? {};
  if (!view) {
    return (
      <div>
        <div className="breadcrumb"><Link className="crumb-back" to={repoTo}>返回 {repo}</Link></div>
        <Empty>PG 服务未返回该 PR 的记录</Empty>
      </div>
    );
  }
  const runs = view.runs ?? [];

  return (
    <div>
      <div className="breadcrumb">
        <Link to={repoTo} className="crumb-back">{repo}</Link>
        <span className="crumb-sep">/</span>
        <span className="crumb-current">PR #{view.prNumber}</span>
      </div>

      <div className="detail-head">
        <div className="detail-head-main">
          <h1 className="detail-title" style={{ fontSize: 24 }}>{view.title ?? `PR #${view.prNumber}`}</h1>
          <div className="detail-chips">
            <span className="chip">PR #{view.prNumber}</span>
            <span className="chip mono truncate">{repo}</span>
            <span className="chip">最近 head <span className="sha">{String(currentHead ?? lr?.head_sha ?? view.currentHead ?? '').slice(0, 8) || '—'}</span></span>
            <span className="chip">{runs.length} 次运行记录</span>
            <span className="chip">PG 只读 · Fixture（隔离测试记录，非真实运行）</span>
          </div>
        </div>
        <div className="detail-actions">
          {view.prUrl ? (
            <a className="btn btn-primary" href={view.prUrl} target="_blank" rel="noreferrer">
              <ArrowUpRight size={13} strokeWidth={1.75} aria-hidden /> 在 GitHub 查看 PR
            </a>
          ) : null}
        </div>
      </div>

      <p className="section-note">
        数据来自隔离 PG 只读服务 GET /api/prs + /api/runs（data_mode=fixture——隔离测试记录，
        非真实 PR 审查完成）。PG 读模型暂无独立审查结论字段与 GitHub 当前 head 权威——
        本页仅呈现执行记录（最近记录口径），不显示当前结论；站内审批/合并均未接入。
      </p>

      <section className="section">
        <div className="section-head">
          <Workflow size={14} strokeWidth={1.75} aria-hidden className="section-ico" />
          <h3>运行历史（{runs.length} 次）— 点击展开阶段/事件/证据</h3>
        </div>
        <div className="panel">
          <table className="data-table">
            <thead>
              <tr><th>时间</th><th>run_id</th><th>chain / class</th><th>mode</th><th>状态</th><th>结果</th><th>head</th></tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.run_id} className={openRun === r.run_id ? 'row-highlight' : ''}>
                  <td className="cell-time">{fmtTime(r.created_at) ?? '—'}</td>
                  <td className="mono">
                    <button type="button" className="link-btn" onClick={() => setOpenRun(openRun === r.run_id ? null : r.run_id)}>
                      {r.run_id}
                    </button>
                    {r.superseded ? <span className="muted">（已被取代）</span> : null}
                  </td>
                  <td>{r.runClass ?? '—'} / {r.mode ?? '—'}</td>
                  <td><span className="chip">{r.mode ?? '—'}</span></td>
                  <td><span className="chip">{r.execution.status ?? '—'}</span></td>
                  <td>{r.outcome ?? '—'}</td>
                  <td className="mono">{(r.head_sha ?? '').slice(0, 8) || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {openRun ? (
          <div className="panel" style={{ marginTop: 10 }}>
            <div className="section-head" style={{ padding: '10px 14px 0' }}>
              <h3 className="mono">{openRun}</h3>
            </div>
            {detailQ.status === 'loading' ? <Spinner /> : detailQ.status === 'error' ? (
              <div style={{ padding: '0 14px 10px' }}><ErrorBox error={detailQ.error} onRetry={() => setAttempt((n) => n + 1)} /></div>
            ) : (
              <div style={{ padding: '0 14px 12px' }}>
                {(() => {
                  const d = detailQ.data ?? {};
                  const stages = Object.entries(d.stages ?? {});
                  return (
                    <>
                      <h4 className="muted" style={{ margin: '8px 0 4px' }}>阶段（{stages.length}）</h4>
                      {stages.length ? (
                        <table className="data-table">
                          <thead><tr><th>stage</th><th>状态</th><th>attempts</th><th>error</th></tr></thead>
                          <tbody>
                            {stages.map(([dim, s]) => (
                              <tr key={dim}>
                                <td className="mono">{dim}</td>
                                <td>{s.status ?? '—'}</td>
                                <td className="num">{s.attempts ?? '—'}</td>
                                <td className="cell-src">{s.error ?? '—'}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      ) : <p className="section-note">该 run 无阶段记录。</p>}
                      <h4 className="muted" style={{ margin: '10px 0 4px' }}>事件（{(d.events ?? []).length}）</h4>
                      {(d.events ?? []).length ? (
                        <ul className="compact-list mono">
                          {(d.events ?? []).slice(0, 10).map((e, i) => (
                            <li key={i}>{e.created_at} · {e.event_type}</li>
                          ))}
                        </ul>
                      ) : <p className="section-note">无事件记录。</p>}
                      <h4 className="muted" style={{ margin: '10px 0 4px' }}>证据与结论字段（如实）</h4>
                      <ul className="compact-list">
                        <li>evidence：manifest {d.evidence?.manifest_sha256 ?? '未记录'} / path {d.evidence?.evidence_path ?? '未记录'}——{d.evidence?.note ?? 'MinIO 证据未接线'}</li>
                        <li>findings：{typeof d.findings === 'string' ? d.findings : `共 ${(d.findings ?? []).length} 条`}</li>
                        <li>validations：{typeof d.validations === 'string' ? d.validations : `${(d.validations ?? []).length} 条`}</li>
                        <li>站内合并：关闭（{(d.merge_panel?.reasons ?? ['merge_disabled']).join(', ')}）</li>
                      </ul>
                    </>
                  );
                })()}
              </div>
            )}
          </div>
        ) : null}
        <p className="section-note">
          以上为隔离 PG 测试记录（data_mode=fixture）：仅执行编排事实，不含审查结论与发布状态；
          不构成"真实 PR 审查完成"。
        </p>
      </section>
    </div>
  );
}

// ---- snapshot 源：历史证据包聚合（原有路径） ----

function SnapshotPrDetail({ owner, name, prNumber }) {
  const repo = `${owner}/${name}`;
  const { data, error, retry } = useSourceQuery(() => fetchRunsSnapshotOnce(), [repo, prNumber]);

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
          <h1 className="detail-title" style={{ fontSize: 24 }}>{pr.title ?? `PR #${pr.prNumber}`}</h1>
          <div className="detail-chips">
            <span className="chip">PR #{pr.prNumber}</span>
            <span className="chip mono truncate">{repo}</span>
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
        站内审批未接入（C-11 未实现）；站内合并为已记录的范围变更（C-12），未启用。
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

// ---- contract 源：正式契约 v2 /api/pulls/:n（当前由 fixture harness 供数） ----

function ContractPrDetail({ owner, name, prNumber }) {
  const repo = `${owner}/${name}`;
  const config = useAppConfig();
  const { source } = useDataSource(config);
  const [attempt, setAttempt] = useState(0);
  const q = useSourceQuery(() => source.getPr(repo, prNumber), [source, repo, prNumber, attempt]);

  const repoTo = `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}`;

  if (q.status === 'error') {
    return (
      <div>
        <div className="breadcrumb"><Link className="crumb-back" to={repoTo}>返回 {repo}</Link></div>
        {q.error?.status === 404 ? (
          <div className="state-box state-warn">
            实时数据源中无 {repo} PR #{prNumber || '？'} 的记录
            {q.error?.reason ? <code className="mono">（{q.error.reason}）</code> : null}
            ——不回退到历史快照数据，也不伪装为成功。
          </div>
        ) : (
          <ErrorBox error={q.error} onRetry={() => setAttempt((n) => n + 1)} />
        )}
      </div>
    );
  }
  if (q.status !== 'done') return <div className="detail-skeleton"><SkeletonRows rows={6} /></div>;

  const { view, detail } = q.data ?? {};
  if (!view) {
    return (
      <div>
        <div className="breadcrumb"><Link className="crumb-back" to={repoTo}>返回 {repo}</Link></div>
        <Empty>契约端点未返回该 PR 的数据</Empty>
      </div>
    );
  }

  const lr = detail?.latest_result ?? null;
  const lrun = detail?.latest_run ?? null;
  const runs = detail?.runs ?? [];
  const currentHead = detail?.current_head_sha ?? view.currentHead;
  const githubUrl = detail?.merge_panel?.github_url ?? view.prUrl;
  const patch = detail?.patch_delivery ?? null;
  const basis = !lr
    ? '当前 head 暂无完成结果'
    : (lr.stale
      ? `旧 head 结论（head ${(lr.head_sha ?? '').slice(0, 8)}）——当前 head 无完成结果，以下不代表当前状态`
      : '当前 head 的完成结果');

  return (
    <div>
      <div className="breadcrumb">
        <Link to={repoTo} className="crumb-back">{repo}</Link>
        <span className="crumb-sep">/</span>
        <span className="crumb-current">PR #{view.prNumber}</span>
      </div>

      <div className="detail-head">
        <div className="detail-head-main">
          <h1 className="detail-title" style={{ fontSize: 24 }}>{view.title ?? `PR #${view.prNumber}`}</h1>
          <div className="detail-chips">
            <span className="chip">PR #{view.prNumber}</span>
            <span className="chip mono truncate">{repo}</span>
            {/* R4（FB-03）：控制面实时事实（与 /api/overview 同源同边界） */}
            {detail?.stage ? (
              <span className="chip" title={`阶段来源：${detail.stage_source ?? '—'}`}>
                控制面阶段：{detail.stage}
              </span>
            ) : null}
            {detail?.head_sha ? (
              <span className="chip mono" title="最近一次审查的 head（非 GitHub 当前 head 权威）">
                审查 head {(detail.head_sha ?? '').slice(0, 8)}
              </span>
            ) : null}
            {detail?.receipts ? (
              <span className="chip" title="skill 回执：总数 / OK / 完整性冲突">
                回执 {detail.receipts.total}（OK {detail.receipts.ok} / 冲突 {detail.receipts.integrity_conflicts}）
              </span>
            ) : null}
            {detail?.gate_audit?.length ? (
              <span className="chip" title="gate 审计记录数（本 PR 的 run 集）">
                gate 审计 {detail.gate_audit.length}
              </span>
            ) : null}
            <span className="chip mono" title="GitHub 当前 head（权威）">当前 head {(currentHead ?? '').slice(0, 8) || '未记录'}</span>
            <span className="chip">{runs.length} 次运行（执行历史）</span>
            <span className="chip">{config?.dataMode === 'fixture' ? 'Fixture 数据' : 'PG 实时（staging）'}</span>
          </div>
        </div>
        <div className="detail-actions">
          {githubUrl ? (
            <a className="btn btn-primary" href={githubUrl} target="_blank" rel="noreferrer">
              <ArrowUpRight size={13} strokeWidth={1.75} aria-hidden /> 在 GitHub 查看 PR
            </a>
          ) : null}
        </div>
      </div>

      <p className="section-note">
        数据来自正式契约端点 GET /api/pulls/{view.prNumber}（data_mode={config?.dataMode ?? 'unknown'}
        {config?.dataMode === 'fixture' ? '——合成数据，非真实运行' : ''}）。
        站内审批未接入（C-11 未实现）；站内合并默认关闭（C-12），仅提供 GitHub 入口。
      </p>

      <section className="section">
        <div className="section-head">
          <FileSearch size={14} strokeWidth={1.75} aria-hidden className="section-ico" />
          <h3>审查摘要</h3>
        </div>
        <div className="finding-statement">
          <VerdictBadge review={lr ?? view.review.review} />
          <span className="finding-text muted">
            {lr?.cwe ? <code className="mono">{lr.cwe}</code> : null}
            {basis}
            {lr ? <> · run {lr.run_id}</> : null}
          </span>
        </div>
        {lrun && String(lrun.status).toUpperCase() === 'RUNNING' ? (
          <p className="section-note">当前 head 审查进行中（run {lrun.run_id}）——完成前不显示结论，不用旧 head 结果顶替。</p>
        ) : null}
      </section>

      <section className="section">
        <div className="section-head">
          <Workflow size={14} strokeWidth={1.75} aria-hidden className="section-ico" />
          <h3>阶段与运行历史（{runs.length} 次）</h3>
        </div>
        <div className="panel">
          <table className="data-table">
            <thead>
              <tr>
                <th>时间</th><th>run_id</th><th>class / 序列</th><th>mode</th><th>状态</th><th>结果</th><th>head</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={`${r.run_id}-${r.exec_seq ?? ''}`}>
                  <td className="cell-time">{fmtTime(r.created_at) ?? '—'}</td>
                  <td className="mono">{r.run_id}</td>
                  <td>{r.class ?? '—'} #{r.exec_seq ?? '—'}</td>
                  <td><span className="chip">{r.mode ?? '—'}</span></td>
                  <td>{r.status ?? '—'}</td>
                  <td>{r.outcome ?? '—'}</td>
                  <td className="mono">
                    {(r.head_sha ?? '').slice(0, 8) || '—'}
                    {r.stale ? <span className="muted">（旧 head）</span> : <span className="muted">（当前）</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="section-note">
          stale 行是旧 head 的历史结果，仅作记录保留，不代表当前 head；无完成结果的 run 如实显示状态。
        </p>
      </section>

      <section className="section">
        <div className="section-head">
          <ShieldCheck size={14} strokeWidth={1.75} aria-hidden className="section-ico" />
          <h3>修复补丁与合并面板</h3>
        </div>
        <div className="mini-states">
          <MiniState icon={ShieldCheck} label="补丁交付">
            {patch?.download_url
              ? <a className="link-btn" href={patch.download_url} download>{patch.label ?? '下载补丁'}</a>
              : <span className="muted">补丁未生成（无下载入口）</span>}
          </MiniState>
          <MiniState icon={ShieldCheck} label="是否已应用到 PR">
            {patch ? `${patch.applied_to_pr === true ? '已应用' : '未应用'}——下载 ≠ 已应用，以 GitHub 记录为准` : '未记录'}
          </MiniState>
          <MiniState icon={Workflow} label="站内合并">
            {(detail?.merge_panel?.enabled ?? false)
              ? '已启用'
              : `关闭（${(detail?.merge_panel?.reasons ?? ['merge_disabled']).join(', ')}）— 使用 GitHub 原生合并`}
          </MiniState>
        </div>
        {detail?.findings ? (
          <p className="section-note">findings：共 {detail.findings.total} 条{detail.findings.by_source ? `（来源：${Object.entries(detail.findings.by_source).map(([k, v]) => `${k} ${v}`).join('，')}）` : ''}——明细与验证以 run 记录为准。</p>
        ) : (
          <p className="section-note">本响应未携带 findings 明细——如实显示，不制造入口。</p>
        )}
      </section>
    </div>
  );
}
