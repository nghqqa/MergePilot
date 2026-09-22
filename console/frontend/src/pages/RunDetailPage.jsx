import React, { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api } from '../api.js';
import { Spinner, ErrorBox, Empty, Sha, Badge, KV, Section, EvidencePre } from '../ui.jsx';
import { ExecutionBadge, VerdictBadge, GateBadge, PublishBadge, RagStateBadge } from '../status.jsx';
import { fmtTime, fmtBytes, fmtInt, NULL_TEXT } from '../format.js';

const TABS = ['概览', '时间线', '任务', '证据', '版本清单', 'Skill', 'RAG', '用量'];

function EvidenceDrawer({ packId, filePath, onClose }) {
  const [content, setContent] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    setContent(null);
    setError(null);
    if (filePath) api.evidenceContent(packId, filePath).then(setContent).catch(setError);
  }, [packId, filePath]);
  if (!filePath) return null;
  return (
    <div className="drawer-backdrop" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="drawer" role="dialog" aria-label={`证据查看 ${filePath}`}>
        <div className="drawer-head">
          <div>
            <div className="drawer-title mono">{filePath}</div>
            <div className="drawer-sub">
              归属 {packId} ·{' '}
              {content
                ? `${fmtBytes(content.bytes)} · ${content.encoding}${content.sums_status !== 'no_sums' ? ` · SUMS: ${content.sums_status}` : ' · 无 SUMS'}`
                : '加载中'}
            </div>
          </div>
          <div className="drawer-actions">
            <a
              className="btn"
              href={api.evidenceDownloadUrl(packId, filePath)}
              download
              title="下载原始文件（不推送、不修改任何远端）"
            >
              下载
            </a>
            <button className="btn" onClick={onClose}>关闭 (Esc)</button>
          </div>
        </div>
        <div className="drawer-body">
          {error ? (
            <ErrorBox error={error} />
          ) : !content ? (
            <Spinner />
          ) : content.encoding === 'binary' ? (
            <Empty>{content.note ?? '二进制文件'}</Empty>
          ) : (
            <EvidencePre text={content.text} truncated={content.truncated} />
          )}
        </div>
      </div>
    </div>
  );
}

function OverviewTab({ run }) {
  const e = run.execution;
  return (
    <div>
      <div className="status-strip">
        <div className="status-cell" title="执行状态：投递台账（webhook 轮）或项目 meta（Matrix 轮）">
          <div className="status-label">执行状态</div>
          <ExecutionBadge execution={e} />
          <div className="status-source">
            {e?.source === 'delivery_ledger' ? '来源 delivery-ledger.json' : e?.source === 'project_meta' ? '来源 project/meta.json（无台账）' : NULL_TEXT}
          </div>
        </div>
        <div className="status-cell" title="审查结论：独立安全审查结果，绑定下方 head SHA">
          <div className="status-label">审查结论</div>
          <VerdictBadge review={run.review} />
          <div className="status-source">
            {run.review.source ? `来源 ${run.review.source}` : NULL_TEXT}
            {run.review.cwe ? ` · ${run.review.cwe}` : ''}
          </div>
        </div>
        <div className="status-cell" title="人工安全门">
          <div className="status-label">人工门</div>
          <GateBadge gate={run.review.human_gate} />
          <div className="status-source">{'\u00a0'}</div>
        </div>
        <div className="status-cell" title="GitHub 发布：check-run 回写事实">
          <div className="status-label">发布状态</div>
          <PublishBadge publish={run.publish} />
          <div className="status-source">
            {run.publish.url ? (
              <a href={run.publish.url} target="_blank" rel="noreferrer">check-run {run.publish.check_run_id} ↗</a>
            ) : (
              '本轮零 GitHub 写入'
            )}
          </div>
        </div>
      </div>

      <div className="kv-grid">
        <KV label="run_id"><span className="mono">{run.run_id ?? NULL_TEXT}</span></KV>
        <KV label="证据包"><span className="mono">{run.pack_id}</span></KV>
        <KV label="仓库"><span className="mono">{run.repo ?? NULL_TEXT}</span></KV>
        <KV label="PR">
          {run.pr_number ? (
            run.pr_url ? <a href={run.pr_url} target="_blank" rel="noreferrer">#{run.pr_number} ↗</a> : `#${run.pr_number}`
          ) : NULL_TEXT}
        </KV>
        <KV label="head SHA（结论绑定）"><Sha value={run.head_sha} n={12} /></KV>
        <KV label="base SHA"><Sha value={run.base_sha} n={12} /></KV>
        <KV label="触发方式">{run.trigger === 'webhook' ? 'webhook（GitHub 投递）' : run.trigger === 'matrix' ? 'Matrix 手动 kickoff' : NULL_TEXT}</KV>
        <KV label="开始时间">{fmtTime(run.created_at) ?? NULL_TEXT}</KV>
        <KV label="耗时">{run.duration_human ?? NULL_TEXT}</KV>
        <KV label="项目"><span className="mono">{run.project?.project_id ?? NULL_TEXT}</span></KV>
      </div>

      {run.review.status_line ? (
        <Section title="结果摘要">
          <p className="result-line">{run.review.status_line}</p>
        </Section>
      ) : null}

      {e?.note ? (
        <Section title="投递备注（原文）">
          <p className="result-line mono">{e.note}</p>
        </Section>
      ) : null}

      <Section title="归属一致性">
        <ul className="compact-list">
          <li>本页全部结论、证据、时间线均归属于上方 head SHA 与 run_id；该 PR 若有新 commit，将以新运行记录呈现，不会覆盖本记录。</li>
          <li>数据来源：证据包 {run.pack_id}（锁定只读快照）。</li>
        </ul>
      </Section>
    </div>
  );
}

function TimelineTab({ run }) {
  if (!run.timeline?.length) return <Empty>时间线未记录</Empty>;
  return (
    <ol className="timeline">
      {run.timeline.map((t, i) => (
        <li key={i} className={t.ts ? '' : 'timeline-nts'}>
          <div className="timeline-ts mono">{fmtTime(t.ts) ?? '时间未记录'}</div>
          <div className="timeline-dot" aria-hidden />
          <div className="timeline-body">
            <div>{t.label}</div>
            <div className="timeline-src">{t.source}{t.detail ? ` · ${t.detail}` : ''}</div>
          </div>
        </li>
      ))}
    </ol>
  );
}

function TasksTab({ run, onOpenEvidence }) {
  if (!run.tasks?.length) return <Empty>包内无任务 meta 记录</Empty>;
  return (
    <div className="table-scroll">
      <table className="data-table">
        <thead>
          <tr>
            <th>task_id</th><th>角色</th><th>状态</th><th>委派</th><th>确认</th><th>提交</th><th>结果</th>
          </tr>
        </thead>
        <tbody>
          {run.tasks.map((t) => (
            <tr key={t.task_id}>
              <td className="mono" title={t.title ?? ''}>{t.task_id}</td>
              <td>{t.role ?? NULL_TEXT}</td>
              <td>{t.status ?? NULL_TEXT}</td>
              <td className="cell-time">{fmtTime(t.assigned_at) ?? '—'}</td>
              <td className="cell-time">{fmtTime(t.acknowledged_at) ?? '—'}</td>
              <td className="cell-time">{fmtTime(t.submitted_at) ?? '—'}</td>
              <td>
                {t.result_path ? (
                  <button className="link-btn" onClick={() => onOpenEvidence(t.result_path)}>result.md</button>
                ) : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {run.dag?.length ? (
        <Section title="DAG 节点（project/result.md 原文标记）">
          <ul className="compact-list mono">
            {run.dag.map((d, i) => (
              <li key={i}>[{d.mark}] {d.task_id} — {d.rest}</li>
            ))}
          </ul>
        </Section>
      ) : null}
    </div>
  );
}

function EvidenceTab({ packId, onOpenEvidence }) {
  const [list, setList] = useState(null);
  const [error, setError] = useState(null);
  const [filter, setFilter] = useState('');
  useEffect(() => {
    api.evidence(packId).then(setList).catch(setError);
  }, [packId]);
  const items = React.useMemo(() => {
    if (!list) return null;
    const f = filter.trim().toLowerCase();
    return f ? list.items.filter((i) => i.path.toLowerCase().includes(f)) : list.items;
  }, [list, filter]);
  return (
    <div>
      <p className="section-note">
        证据为锁定快照；查看按纯文本渲染（不执行脚本/不渲染 HTML）；下载不构成任何代码推送。
        未列入 SHA256SUMS 的文件单独标注。
      </p>
      {error ? <ErrorBox error={error} /> : !list ? <Spinner /> : !items.length ? <Empty>无匹配文件</Empty> : (
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr><th>文件</th><th>大小</th><th title="是否列入包内 SHA256SUMS">SUMS</th><th>操作</th></tr>
            </thead>
            <tbody>
              {items.map((f) => (
                <tr key={f.path}>
                  <td className="mono cell-path" title={f.path}>{f.path}</td>
                  <td>{fmtBytes(f.bytes)}</td>
                  <td>
                    {f.sums_status === 'listed' ? <Badge tone="ok">已列</Badge>
                      : f.sums_status === 'unlisted' ? <Badge tone="warn" title="未列入锁定清单">未列</Badge>
                      : <Badge tone="neutral">无 SUMS</Badge>}
                  </td>
                  <td className="cell-actions">
                    <button className="link-btn" onClick={() => onOpenEvidence(f.path)}>查看</button>
                    {' '}
                    <a className="link-btn" href={api.evidenceDownloadUrl(packId, f.path)} download>下载</a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <input
        className="filter-input"
        placeholder="按路径过滤（包含匹配）"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        aria-label="证据文件过滤"
        style={{ marginTop: 8 }}
      />
    </div>
  );
}

function VersionsTab({ run }) {
  const v = run.versions;
  return (
    <div>
      <div className="kv-grid">
        <KV label="run-manifest" title="桥自 81e0045 起在派发前写入 MinIO 的 run 级版本清单；历史证据包早于该机制">
          {v?.run_manifest ? <span className="mono">{v.run_manifest}</span> : <Badge tone="warn">未记录（早于 manifest 机制）</Badge>}
        </KV>
        <KV label="模型标识" title={v?.model_basis ?? ''}>
          {v?.model ? <span className="mono">{v.model}</span> : NULL_TEXT}
          {v?.model_basis ? <div className="kv-note">{v.model_basis}</div> : null}
        </KV>
        <KV label="worker 镜像" title={v?.image_basis ?? ''}>
          {v?.image ? <span className="mono">{v.image}</span> : NULL_TEXT}
          {v?.image_basis ? <div className="kv-note">{v.image_basis}</div> : null}
        </KV>
        <KV label="RAG 快照标识">
          <Badge tone="warn" title="历史包内无 RAG 索引版本标识（rag-live 未运行期）">未记录</Badge>
        </KV>
      </div>
      {v?.note ? <p className="section-note">{v.note}</p> : null}
      {v?.skills?.length ? (
        <Section title="本 run Skill 调用聚合（来自包内 skill-audit）">
          <div className="table-scroll">
            <table className="data-table">
              <thead><tr><th>工具</th><th>次数</th><th>数据模式</th><th>来源</th></tr></thead>
              <tbody>
                {v.skills.map((s) => (
                  <tr key={s.tool}>
                    <td className="mono">{s.tool}</td><td>{s.count}</td>
                    <td>{s.data_modes.join(', ')}</td>
                    <td className="mono cell-src">{s.source_refs.join(', ')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      ) : null}
      {v?.span_summary ? (
        <Section title="span 汇总（按角色）">
          <div className="table-scroll">
            <table className="data-table">
              <thead><tr><th>角色</th><th>spans</th><th>skill 调用</th><th>rag 调用</th></tr></thead>
              <tbody>
                {Object.entries(v.span_summary).map(([role, s]) => (
                  <tr key={role}>
                    <td>{role}</td><td>{fmtInt(s.spans)}</td>
                    <td>{fmtInt(s.skill_total)}</td><td>{fmtInt(s.rag)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      ) : null}
    </div>
  );
}

function RagTab({ run }) {
  const rag = run.rag;
  if (!rag) return <Empty>RAG 未接入</Empty>;
  const ragOnly = rag.calls ? rag.calls.filter((c) => /^rag\./.test(c.tool ?? '')) : [];
  return (
    <div>
      <div className="rag-state-row">
        <RagStateBadge state={rag.state} />
        <span className="muted">
          {rag.state === 'called' && '以下为包内锁定的真实调用记录（含数据模式标注），非实时查询'}
          {rag.state === 'counted_only' && `span 计数合计 ${rag.total} 次，无逐条记录`}
          {rag.state === 'not_called' && 'span 汇总计数为 0'}
          {rag.state === 'insufficient_data' && '包内无 RAG 数据 — 与"服务不可用"不同，此处只是没有足够数据判断'}
          {rag.state === 'no_calls' && '记录文件存在但无调用行'}
        </span>
      </div>
      {rag.state === 'called' ? (
        <div>
          <p className="section-note">
            口径说明：TRACED/SK5 轮的 rag 导出为<strong>会话累计口径</strong>（四案例共用一个 AgentTeams 会话，未按单 run 窗口重切），
            下表可能包含同会话其他 run 的调用；WH 收官轮的 skill-audit 已按 run 窗口严格重切（见包内 CORRECTIONS.md）。
            仅显示 rag.* 检索调用（{ragOnly.length}/{rag.calls.length} 行）。
          </p>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr><th>时间</th><th>工具</th><th>状态</th><th title="命中返回的文档/片段数">命中数</th><th>来源 refs</th><th>数据模式</th><th>耗时</th></tr>
              </thead>
              <tbody>
                {ragOnly.map((c, i) => (
                  <tr key={i}>
                    <td className="cell-time">{c.ts ?? '—'}</td>
                    <td className="mono">{c.tool}</td>
                    <td>{c.result_status}</td>
                    <td>{c.document_count ?? '—'}</td>
                    <td className="mono cell-src" title={(c.source_refs ?? []).join(', ')}>{(c.source_refs ?? []).join(', ') || '—'}</td>
                    <td>
                      {c.data_mode === 'SYNTHETIC' ? (
                        <Badge tone="warn" title="该轮检索语料为合成演示语料，非真实案例库">SYNTHETIC</Badge>
                      ) : c.data_mode ?? '—'}
                    </td>
                    <td>{c.latency_ms != null ? `${c.latency_ms}ms` : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function UsageTab({ run }) {
  const u = run.usage;
  if (!u) {
    return (
      <div>
        <Empty>本包无 usage-summary.json — 用量缺失（如实展示，不估算）</Empty>
        <p className="section-note">V0 硬门槛要求内测起全量计量；历史包为 2026-09 决赛期运行，未接逐 run 计量。</p>
      </div>
    );
  }
  const w = u.matched_window;
  return (
    <div>
      <p className="section-note">
        实测口径：{u.note ? u.note : '见原始文件'}。金额换算未接入（无价目表），不显示虚构金额。
      </p>
      {w ? (
        <div className="kv-grid">
          <KV label="匹配窗口"><span className="mono">{w.key}</span></KV>
          <KV label="调用次数">{fmtInt(w.calls)}</KV>
          <KV label="输入 tokens">{fmtInt(w.inp)}</KV>
          <KV label="其中缓存命中">{fmtInt(w.cached)}</KV>
          <KV label="输出 tokens">{fmtInt(w.outp)}</KV>
        </div>
      ) : (
        <p className="section-note">包内 usage 为会话级多窗口，无法唯一对应本 run — 全部窗口见下。</p>
      )}
      <div className="table-scroll">
        <table className="data-table">
          <thead><tr><th>窗口</th><th>calls</th><th>inp</th><th>cached</th><th>outp</th>{w ? <th>本 run</th> : null}</tr></thead>
          <tbody>
            {Object.entries(u.windows ?? {}).map(([k, v]) => (
              <tr key={k} className={w && k === w.key ? 'row-highlight' : ''}>
                <td className="mono">{k}</td>
                <td>{fmtInt(v.calls)}</td><td>{fmtInt(v.inp)}</td><td>{fmtInt(v.cached)}</td><td>{fmtInt(v.outp)}</td>
                {w ? <td>{k === w.key ? '✓' : ''}</td> : null}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function RunDetailPage() {
  const { packId } = useParams();
  const [run, setRun] = useState(null);
  const [error, setError] = useState(null);
  const [tab, setTab] = useState('概览');
  const [evidenceFile, setEvidenceFile] = useState(null);
  const [integrity, setIntegrity] = useState(null);

  useEffect(() => {
    setRun(null); setError(null); setIntegrity(null); setTab('概览');
    api.run(packId).then(setRun).catch(setError);
  }, [packId]);

  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && setEvidenceFile(null);
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const runIntegrity = async () => {
    setIntegrity({ checking: true });
    try {
      setIntegrity(await api.integrity(packId));
    } catch (e) {
      setIntegrity({ error: e.message });
    }
  };

  return (
    <div>
      <div className="breadcrumb">
        <Link to="/runs">← 运行列表</Link>
      </div>
      {error ? <ErrorBox error={error} /> : !run ? <Spinner /> : (
        <>
          <div className="page-head">
            <h1 className="mono" title={run.run_id ?? ''}>{run.run_id ?? 'run_id 未记录'}</h1>
            <p className="page-sub mono">{run.pack_id}</p>
          </div>

          <div className="tab-bar" role="tablist">
            {TABS.map((t) => (
              <button
                key={t}
                role="tab"
                aria-selected={tab === t}
                className={`tab${tab === t ? ' tab-active' : ''}`}
                onClick={() => setTab(t)}
              >
                {t}
              </button>
            ))}
            <span className="tab-spacer" />
            <button className="btn" onClick={runIntegrity} disabled={integrity?.checking}>
              {integrity?.checking ? '校验中…' : '校验包完整性'}
            </button>
          </div>
          {integrity && !integrity.checking ? (
            <div className={`state-box ${integrity.error ? 'state-error' : integrity.status === 'verified' ? 'state-ok' : 'state-warn'}`}>
              {integrity.error ? `校验失败：${integrity.error}` : (
                <>
                  SHA256SUMS 校验：<strong>{integrity.status === 'verified' ? '通过' : integrity.status === 'mismatch' ? '不一致！' : '无清单'}</strong>
                  {' '}· 清单 {integrity.listed} 项 · 验证 {integrity.verified} 项
                  {integrity.mismatched?.length ? ` · 不一致: ${integrity.mismatched.map((m) => m.path).join(', ')}` : ''}
                  {integrity.unlisted_count ? ` · 未列入清单 ${integrity.unlisted_count} 项` : ''}
                </>
              )}
            </div>
          ) : null}

          {tab === '概览' && <OverviewTab run={run} />}
          {tab === '时间线' && <TimelineTab run={run} />}
          {tab === '任务' && <TasksTab run={run} onOpenEvidence={setEvidenceFile} />}
          {tab === '证据' && <EvidenceTab packId={packId} onOpenEvidence={setEvidenceFile} />}
          {tab === '版本清单' && <VersionsTab run={run} />}
          {tab === 'Skill' && (
            run.versions?.skills?.length || run.skill_audit ? (
              <div>
                <p className="section-note">以下为 run 实际使用的 Skill 调用记录（包内审计导出），非 worker 当前安装版本。</p>
                <div className="table-scroll">
                  <table className="data-table">
                    <thead><tr><th>时间</th><th>工具</th><th>状态</th><th>延迟</th><th>数据模式</th><th>来源 refs</th></tr></thead>
                    <tbody>
                      {(run.skill_audit?.invocations ?? []).map((inv, i) => (
                        <tr key={i}>
                          <td className="cell-time">{inv.ts ?? '—'}</td>
                          <td className="mono">{inv.tool}</td>
                          <td>{inv.result_status ?? '—'}</td>
                          <td>{inv.latency_ms != null ? `${inv.latency_ms}ms` : '—'}</td>
                          <td>{inv.data_mode ?? '—'}</td>
                          <td className="mono cell-src" title={(inv.source_refs ?? []).join(', ')}>{(inv.source_refs ?? []).join(', ') || '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ) : (
              <Empty>包内无 Skill 调用审计记录（早期轮未导出）— 不以 worker 当前版本冒充</Empty>
            )
          )}
          {tab === 'RAG' && <RagTab run={run} />}
          {tab === '用量' && <UsageTab run={run} />}

          <EvidenceDrawer packId={packId} filePath={evidenceFile} onClose={() => setEvidenceFile(null)} />
        </>
      )}
    </div>
  );
}
