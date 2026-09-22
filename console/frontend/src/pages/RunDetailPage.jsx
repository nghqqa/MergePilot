import React, { useEffect, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import {
  Braces, Check, ChevronsLeft, CloudUpload, Download, File, FileCode, FileDiff,
  FileSearch, FileText, FolderOpen, Hand, History, LayoutDashboard, ListChecks,
  ScrollText, Search, ShieldCheck, Tag, Wrench, X, Workflow,
} from 'lucide-react';
import { api } from '../api.js';
import { Spinner, ErrorBox, Empty, Sha, Chip, Section, EvidencePre, SkeletonRows } from '../ui.jsx';
import { ExecutionBadge, VerdictBadge, GateBadge, PublishBadge, RagStateBadge } from '../status.jsx';
import { fmtTime, fmtBytes, fmtInt } from '../format.js';

const TABS = [
  { key: '概览', icon: LayoutDashboard },
  { key: '时间线', icon: History },
  { key: '任务', icon: ListChecks },
  { key: '证据', icon: FolderOpen },
  { key: '版本清单', icon: Tag },
  { key: 'Skill', icon: Wrench },
  { key: 'RAG', icon: FileSearch },
  { key: '用量', icon: CloudUpload },
];

function fileIcon(path) {
  const ext = path.split('.').pop().toLowerCase();
  if (ext === 'md' || ext === 'txt') return [FileText, 'ft-doc'];
  if (ext === 'log') return [ScrollText, 'ft-log'];
  if (ext === 'json' || ext === 'jsonl') return [Braces, 'ft-json'];
  if (ext === 'diff' || ext === 'patch') return [FileDiff, 'ft-diff'];
  if (['py', 'js', 'mjs', 'sh'].includes(ext)) return [FileCode, 'ft-code'];
  return [File, 'ft-other'];
}

function EvidenceDrawer({ packId, filePath, onClose }) {
  const [content, setContent] = useState(null);
  const [error, setError] = useState(null);
  const closeBtnRef = React.useRef(null);
  useEffect(() => {
    setContent(null);
    setError(null);
    if (filePath) {
      api.evidenceContent(packId, filePath).then(setContent).catch(setError);
      // 焦点移入对话框（Esc 已有全局监听； Tab 循环圈闭属后续 a11y 轮）
      requestAnimationFrame(() => closeBtnRef.current?.focus());
    }
  }, [packId, filePath]);
  if (!filePath) return null;
  return (
    <div className="drawer-backdrop" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="drawer" role="dialog" aria-label={`证据查看 ${filePath}`}>
        <div className="drawer-head">
          <div className="drawer-head-main">
            <div className="drawer-title mono">{filePath}</div>
            <div className="drawer-meta">
              <Chip title={`证据归属运行包 ${packId}`}>{packId}</Chip>
              {content ? (
                <>
                  <Chip title="文件大小">{fmtBytes(content.bytes)}</Chip>
                  <Chip title="传输编码（内容按纯文本转义渲染）">{content.encoding}</Chip>
                  {content.sums_status !== 'no_sums' ? (
                    <Chip title="是否列入包内 SHA256SUMS 锁定清单">SUMS: {content.sums_status}</Chip>
                  ) : (
                    <Chip title="该包无 SHA256SUMS 清单">无 SUMS</Chip>
                  )}
                </>
              ) : null}
            </div>
          </div>
          <div className="drawer-actions">
            <a
              className="btn btn-primary"
              href={api.evidenceDownloadUrl(packId, filePath)}
              download
              title="下载原始文件（不推送、不修改任何远端）"
            >
              <Download size={13} strokeWidth={1.75} aria-hidden /> 下载
            </a>
            <button ref={closeBtnRef} className="btn btn-icon" onClick={onClose} aria-label="关闭（Esc）" title="关闭（Esc）">
              <X size={15} strokeWidth={1.75} />
            </button>
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

function StatusCell({ icon: Icon, label, children, source, title }) {
  return (
    <div className="status-cell" title={title}>
      <div className="status-head">
        <span className="status-ico" aria-hidden><Icon size={13} strokeWidth={1.75} /></span>
        <span className="status-label">{label}</span>
      </div>
      <div className="status-badge">{children}</div>
      {source ? <div className="status-source">{source}</div> : null}
    </div>
  );
}

function OverviewTab({ run }) {
  const e = run.execution;
  return (
    <div>
      <div className="status-strip">
        <StatusCell
          icon={Workflow}
          label="执行状态"
          title="执行状态：投递台账（webhook 轮）或项目 meta（Matrix 轮）"
          source={e?.source === 'delivery_ledger' ? 'delivery-ledger.json' : e?.source === 'project_meta' ? 'project/meta.json（无台账）' : null}
        >
          <ExecutionBadge execution={e} />
        </StatusCell>
        <StatusCell
          icon={FileSearch}
          label="审查结论"
          title="审查结论：独立安全审查结果，绑定下方 head SHA"
          source={run.review.source ?? null}
        >
          <VerdictBadge review={run.review} />
        </StatusCell>
        <StatusCell
          icon={Hand}
          label="人工门"
          title="人工安全门决策"
          source={run.review.human_gate_source ?? (run.review.human_gate ? null : '包内无门记录')}
        >
          <GateBadge gate={run.review.human_gate} />
        </StatusCell>
        <StatusCell
          icon={CloudUpload}
          label="发布状态"
          title="GitHub 发布：check-run 回写事实"
          source={run.publish.url ? 'check-run 已发布' : '本轮零 GitHub 写入'}
        >
          <PublishBadge publish={run.publish} />
        </StatusCell>
      </div>

      <div className="kv-grid">
        <div className="kv"><div className="kv-label">run_id</div><div className="kv-value mono">{run.run_id ?? '未记录'}</div></div>
        <div className="kv"><div className="kv-label">证据包</div><div className="kv-value mono">{run.pack_id}</div></div>
        <div className="kv"><div className="kv-label">仓库</div><div className="kv-value mono">{run.repo ?? '未记录'}</div></div>
        <div className="kv">
          <div className="kv-label">PR</div>
          <div className="kv-value">
            {run.pr_number ? (run.pr_url ? <a href={run.pr_url} target="_blank" rel="noreferrer">#{run.pr_number} ↗</a> : `#${run.pr_number}`) : '未记录'}
          </div>
        </div>
        <div className="kv" title="该运行的全部结论、证据、时间线绑定此 commit">
          <div className="kv-label">head SHA（结论绑定）</div><div className="kv-value"><Sha value={run.head_sha} n={12} /></div>
        </div>
        <div className="kv"><div className="kv-label">base SHA</div><div className="kv-value"><Sha value={run.base_sha} n={12} /></div></div>
        <div className="kv"><div className="kv-label">触发方式</div><div className="kv-value">{run.trigger === 'webhook' ? 'webhook（GitHub 投递）' : run.trigger === 'matrix' ? 'Matrix 手动 kickoff' : '未记录'}</div></div>
        <div className="kv"><div className="kv-label">开始时间</div><div className="kv-value num">{fmtTime(run.created_at) ?? '未记录'}</div></div>
        <div className="kv"><div className="kv-label">耗时</div><div className="kv-value num">{run.duration_human ?? '未记录'}</div></div>
        <div className="kv"><div className="kv-label">项目</div><div className="kv-value mono">{run.project?.project_id ?? '未记录'}</div></div>
      </div>

      {run.review.status_line ? (
        <Section title="结果摘要">
          <p className="result-line">{run.review.status_line}</p>
        </Section>
      ) : null}

      {e?.note ? (
        <Section title="投递备注（原文）" note="来自投递台账 error/note 字段，原样展示">
          <p className="result-line mono">{e.note}</p>
        </Section>
      ) : null}

      <Section title="归属一致性">
        <ul className="compact-list">
          <li>本页全部结论、证据、时间线均归属于上方 head SHA 与 run_id；该 PR 若有新 commit，将以新运行记录呈现，不会覆盖本记录。</li>
          <li>数据来源：证据包 <code>{run.pack_id}</code>（锁定只读快照）。</li>
        </ul>
      </Section>
    </div>
  );
}

function TimelineTab({ run }) {
  if (!run.timeline?.length) return <Empty>时间线未记录</Empty>;
  return (
    <ol className="timeline panel">
      {run.timeline.map((t, i) => (
        <li key={i} className={t.ts ? '' : 'timeline-nts'}>
          <div className="timeline-ts mono num">{fmtTime(t.ts) ?? '时间未记录'}</div>
          <div className="timeline-rail" aria-hidden>
            <span className="timeline-dot" />
          </div>
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
    <div>
      <div className="table-scroll panel">
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
                <td>{t.role ?? '未记录'}</td>
                <td>{t.status ?? '未记录'}</td>
                <td className="cell-time num">{fmtTime(t.assigned_at) ?? '—'}</td>
                <td className="cell-time num">{fmtTime(t.acknowledged_at) ?? '—'}</td>
                <td className="cell-time num">{fmtTime(t.submitted_at) ?? '—'}</td>
                <td>
                  {t.result_path ? (
                    <button className="link-btn" onClick={() => onOpenEvidence(t.result_path)}>result.md</button>
                  ) : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {run.dag?.length ? (
        <Section title="DAG 节点" note="project/result.md 原文标记，原样展示">
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
  const items = useMemo(() => {
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
      <div className="search-wrap evidence-search">
        <Search size={14} strokeWidth={1.75} aria-hidden />
        <input
          placeholder="按路径过滤（包含匹配）"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          aria-label="证据文件过滤"
        />
      </div>
      {error ? <ErrorBox error={error} /> : !items ? <SkeletonRows rows={6} /> : !items.length ? <Empty>无匹配文件</Empty> : (
        <div className="table-scroll panel">
          <table className="data-table">
            <thead>
              <tr><th>文件</th><th className="th-right">大小</th><th title="是否列入包内 SHA256SUMS">SUMS</th><th>操作</th></tr>
            </thead>
            <tbody>
              {items.map((f) => {
                const [Icon, tone] = fileIcon(f.path);
                return (
                  <tr key={f.path}>
                    <td className="cell-path">
                      <span className={`ft-ico ${tone}`} aria-hidden><Icon size={14} strokeWidth={1.6} /></span>
                      <span className="mono" title={f.path}>{f.path}</span>
                    </td>
                    <td className="num th-right">{fmtBytes(f.bytes)}</td>
                    <td>
                      {f.sums_status === 'listed' ? <span className="sums-chip" title="已列入锁定清单">已列</span>
                        : f.sums_status === 'unlisted' ? <span className="sums-chip sums-none" title="未列入锁定清单">未列</span>
                        : <span className="sums-chip sums-unknown" title="该包无 SHA256SUMS">无 SUMS</span>}
                    </td>
                    <td className="cell-actions">
                      <button className="link-btn" onClick={() => onOpenEvidence(f.path)}>查看</button>
                      <span className="cell-actions-sep">·</span>
                      <a className="link-btn" href={api.evidenceDownloadUrl(packId, f.path)} download>下载</a>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function VersionsTab({ run }) {
  const v = run.versions;
  return (
    <div>
      <div className="kv-grid">
        <div className="kv" title="桥自 81e0045 起在派发前写入 MinIO 的 run 级版本清单；历史证据包早于该机制">
          <div className="kv-label">run-manifest</div>
          <div className="kv-value">
            {v?.run_manifest ? <span className="mono">{v.run_manifest}</span> : <span className="missing-chip" title="历史证据包早于 manifest 机制">未记录（早于 manifest 机制）</span>}
          </div>
        </div>
        <div className="kv" title={v?.model_basis ?? ''}>
          <div className="kv-label">模型标识</div>
          <div className="kv-value mono">{v?.model ?? '未记录'}</div>
          {v?.model_basis ? <div className="kv-note">{v.model_basis}</div> : null}
        </div>
        <div className="kv" title={v?.image_basis ?? ''}>
          <div className="kv-label">worker 镜像</div>
          <div className="kv-value mono">{v?.image ?? '未记录'}</div>
          {v?.image_basis ? <div className="kv-note">{v.image_basis}</div> : null}
        </div>
        <div className="kv">
          <div className="kv-label">RAG 快照标识</div>
          <div className="kv-value"><span className="missing-chip" title="历史包内无 RAG 索引版本标识（rag-live 未运行期）">未记录</span></div>
        </div>
      </div>
      {v?.note ? <p className="section-note">{v.note}</p> : null}
      {v?.skills?.length ? (
        <Section title="本 run Skill 调用聚合" note="来自包内 skill-audit.json">
          <div className="table-scroll panel">
            <table className="data-table">
              <thead><tr><th>工具</th><th className="th-right">次数</th><th>数据模式</th><th>来源</th></tr></thead>
              <tbody>
                {v.skills.map((s) => (
                  <tr key={s.tool}>
                    <td className="mono">{s.tool}</td><td className="num">{s.count}</td>
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
          <div className="table-scroll panel">
            <table className="data-table">
              <thead><tr><th>角色</th><th className="th-right">spans</th><th className="th-right">skill 调用</th><th className="th-right">rag 调用</th></tr></thead>
              <tbody>
                {Object.entries(v.span_summary).map(([role, s]) => (
                  <tr key={role}>
                    <td>{role}</td><td className="num">{fmtInt(s.spans)}</td>
                    <td className="num">{fmtInt(s.skill_total)}</td><td className="num">{fmtInt(s.rag)}</td>
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
          <div className="table-scroll panel">
            <table className="data-table">
              <thead>
                <tr><th>时间</th><th>工具</th><th>状态</th><th className="th-right" title="命中返回的文档/片段数">命中数</th><th>来源 refs</th><th>数据模式</th><th className="th-right">耗时</th></tr>
              </thead>
              <tbody>
                {ragOnly.map((c, i) => (
                  <tr key={i}>
                    <td className="cell-time num">{c.ts ?? '—'}</td>
                    <td className="mono">{c.tool}</td>
                    <td>{c.result_status}</td>
                    <td className="num">{c.document_count ?? '—'}</td>
                    <td className="mono cell-src" title={(c.source_refs ?? []).join(', ')}>{(c.source_refs ?? []).join(', ') || '—'}</td>
                    <td>
                      {c.data_mode === 'SYNTHETIC' ? (
                        <span className="sums-chip sums-warn" title="该轮检索语料为合成演示语料，非真实案例库">SYNTHETIC</span>
                      ) : c.data_mode ?? '—'}
                    </td>
                    <td className="num">{c.latency_ms != null ? `${c.latency_ms}ms` : '—'}</td>
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
          <div className="kv"><div className="kv-label">匹配窗口</div><div className="kv-value mono">{w.key}</div></div>
          <div className="kv"><div className="kv-label">调用次数</div><div className="kv-value num">{fmtInt(w.calls)}</div></div>
          <div className="kv"><div className="kv-label">输入 tokens</div><div className="kv-value num">{fmtInt(w.inp)}</div></div>
          <div className="kv"><div className="kv-label">其中缓存命中</div><div className="kv-value num">{fmtInt(w.cached)}</div></div>
          <div className="kv"><div className="kv-label">输出 tokens</div><div className="kv-value num">{fmtInt(w.outp)}</div></div>
        </div>
      ) : (
        <p className="section-note">包内 usage 为会话级多窗口，无法唯一对应本 run — 全部窗口见下。</p>
      )}
      <div className="table-scroll panel">
        <table className="data-table">
          <thead><tr><th>窗口</th><th className="th-right">calls</th><th className="th-right">inp</th><th className="th-right">cached</th><th className="th-right">outp</th>{w ? <th>本 run</th> : null}</tr></thead>
          <tbody>
            {Object.entries(u.windows ?? {}).map(([k, v]) => (
              <tr key={k} className={w && k === w.key ? 'row-highlight' : ''}>
                <td className="mono">{k}</td>
                <td className="num">{fmtInt(v.calls)}</td><td className="num">{fmtInt(v.inp)}</td>
                <td className="num">{fmtInt(v.cached)}</td><td className="num">{fmtInt(v.outp)}</td>
                {w ? <td>{k === w.key ? <Check size={14} strokeWidth={2} aria-label="本 run 匹配窗口" /> : ''}</td> : null}
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
        <Link to="/runs" className="crumb-back">
          <ChevronsLeft size={14} strokeWidth={1.75} aria-hidden /> 运行列表
        </Link>
      </div>

      {error ? <ErrorBox error={error} /> : !run ? (
        <div className="detail-skeleton">
          <SkeletonRows rows={5} />
        </div>
      ) : (
        <>
          <div className="detail-head">
            <div className="detail-head-main">
              <h1 className="mono detail-title" title={run.run_id ?? ''}>{run.run_id ?? 'run_id 未记录'}</h1>
              <div className="detail-chips">
                <Chip mono title="证据包目录名（详情路由键）">{run.pack_id}</Chip>
                <Chip title="触发方式">{run.trigger === 'webhook' ? 'webhook 投递' : run.trigger === 'matrix' ? 'Matrix kickoff' : '触发未记录'}</Chip>
                {run.has_sums ? <Chip title="包内有 SHA256SUMS，可执行完整校验">SHA256SUMS</Chip> : null}
                {run.review.cwe ? <Chip title="审查确认的缺陷编号">{run.review.cwe}</Chip> : null}
              </div>
            </div>
            <div className="detail-actions">
              <button className="btn" onClick={runIntegrity} disabled={integrity?.checking}>
                <ShieldCheck size={13} strokeWidth={1.75} aria-hidden />
                {integrity?.checking ? '校验中…' : '校验包完整性'}
              </button>
            </div>
          </div>

          {integrity && !integrity.checking ? (
            <div className={`state-box ${integrity.error ? 'state-error' : integrity.status === 'verified' ? 'state-ok' : 'state-warn'}`}>
              {integrity.error ? `校验失败：${integrity.error}` : (
                <>
                  <strong>SHA256SUMS 校验：{integrity.status === 'verified' ? '通过' : integrity.status === 'mismatch' ? '不一致！' : '无清单'}</strong>
                  <span>清单 {integrity.listed} 项 · 验证 {integrity.verified} 项</span>
                  {integrity.mismatched?.length ? <span>不一致：{integrity.mismatched.map((m) => m.path).join(', ')}</span> : null}
                  {integrity.unlisted_count ? <span>未列入清单 {integrity.unlisted_count} 项</span> : null}
                </>
              )}
            </div>
          ) : null}

          <div className="tab-bar" role="tablist">
            {TABS.map((t) => (
              <button
                key={t.key}
                role="tab"
                aria-selected={tab === t.key}
                className={`tab${tab === t.key ? ' tab-active' : ''}`}
                onClick={() => setTab(t.key)}
              >
                <t.icon size={14} strokeWidth={1.75} aria-hidden />
                {t.key}
              </button>
            ))}
          </div>

          {tab === '概览' && <OverviewTab run={run} />}
          {tab === '时间线' && <TimelineTab run={run} />}
          {tab === '任务' && <TasksTab run={run} onOpenEvidence={setEvidenceFile} />}
          {tab === '证据' && <EvidenceTab packId={packId} onOpenEvidence={setEvidenceFile} />}
          {tab === '版本清单' && <VersionsTab run={run} />}
          {tab === 'Skill' && (
            run.versions?.skills?.length || run.skill_audit ? (
              <div>
                <p className="section-note">以下为 run 实际使用的 Skill 调用记录（包内审计导出），非 worker 当前安装版本。</p>
                <div className="table-scroll panel">
                  <table className="data-table">
                    <thead><tr><th>时间</th><th>工具</th><th>状态</th><th className="th-right">延迟</th><th>数据模式</th><th>来源 refs</th></tr></thead>
                    <tbody>
                      {(run.skill_audit?.invocations ?? []).map((inv, i) => (
                        <tr key={i}>
                          <td className="cell-time num">{inv.ts ?? '—'}</td>
                          <td className="mono">{inv.tool}</td>
                          <td>{inv.result_status ?? '—'}</td>
                          <td className="num">{inv.latency_ms != null ? `${inv.latency_ms}ms` : '—'}</td>
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
