import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useLocation, useParams } from 'react-router-dom';
import {
  ArrowUpRight, Braces, Check, ChevronsLeft, CloudUpload, Download, File, FileCode, FileDiff,
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
  const drawerRef = React.useRef(null);
  const loadContent = useCallback(() => {
    setContent(null);
    setError(null);
    if (filePath) api.evidenceContent(packId, filePath).then(setContent).catch(setError);
  }, [packId, filePath]);
  useEffect(() => {
    loadContent();
    // 焦点移入对话框容器（Esc 已有全局监听； Tab 循环圈闭属后续 a11y 轮）
    if (filePath) setTimeout(() => drawerRef.current?.focus(), 0);
  }, [loadContent, filePath]);
  if (!filePath) return null;
  return (
    <div className="drawer-backdrop" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={drawerRef} className="drawer" role="dialog" tabIndex={-1} aria-label={`证据查看 ${filePath}`}>
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
            <button className="btn btn-icon" onClick={onClose} aria-label="关闭（Esc）" title="关闭（Esc）">
              <X size={15} strokeWidth={1.75} />
            </button>
          </div>
        </div>
        <div className="drawer-body">
          {error ? (
            <ErrorBox error={error} onRetry={loadContent} />
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

function OverviewTab({ run, onOpenEvidence }) {
  const e = run.execution;
  const reviewTask = (run.tasks ?? []).find((t) => /-review-\d+/.test(t.task_id ?? ''));
  const basisLinks = [
    { label: '审查结果原文', path: run.review.source },
    { label: 'findings 明细', path: reviewTask?.findings_path ?? null },
  ].filter((l) => l.path);
  const openDag = (run.dag ?? []).filter((d) => d.mark !== 'x');
  const doneDag = (run.dag ?? []).filter((d) => d.mark === 'x');
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
          label="人工确认"
          title="人工安全门决策"
          source={run.review.human_gate_source ?? (run.review.human_gate ? null : '包内无门记录')}
        >
          <GateBadge gate={run.review.human_gate} source={run.review.human_gate_source} />
        </StatusCell>
        <StatusCell
          icon={CloudUpload}
          label="GitHub 检查"
          title="GitHub 发布：check-run 回写事实 + 检查结论"
          source={run.publish.url ? 'check-run 已发布' : '未找到发布记录'}
        >
          <PublishBadge publish={run.publish} />
        </StatusCell>
      </div>

      <Section title="审查发现" icon={FileSearch} note="先看发现与依据：结论来自包内锁定的 reviewer 结果，链接可打开原文核对">
        <div className="finding-statement">
          {run.review.verdict ? (
            <>
              <VerdictBadge review={run.review} />
              <span className="finding-text">
                {run.review.cwe ? <code className="mono">{run.review.cwe}</code> : null}
                {basisLinks.length ? <span className="muted">依据：</span> : <span className="muted">（包内无可链接的结论原文）</span>}
                {basisLinks.map((l) => (
                  <button key={l.path} className="link-btn" onClick={() => onOpenEvidence(l.path)} title={`打开 ${l.path}`}>
                    {l.label}
                  </button>
                ))}
              </span>
            </>
          ) : (
            <span className="muted">结论未记录 — 包内无独立审查结论，无依据可展示；这不等于"无问题"。</span>
          )}
        </div>
        {(/finding[_-]confirmed/i.test(run.review.verdict ?? '') || run.publish?.status === 'published') ? (
          <p className="section-note">
            语义边界：
            {/finding[_-]confirmed/i.test(run.review.verdict ?? '') ? '发现确认 ≠ 已修复；' : null}
            {run.publish?.status === 'published' ? 'GitHub 回写成功 ≠ 补丁已验证或已合并；' : null}
            执行、结论、发布三个状态相互独立，需逐项核对。
          </p>
        ) : null}
        {openDag.length ? (
          <div className="dag-remaining">
            <span className="dag-remaining-label">未执行 / 未授权节点：</span>
            <ul className="compact-list mono">
              {openDag.map((d, i) => <li key={i}>[{d.mark}] {d.task_id}</li>)}
            </ul>
          </div>
        ) : doneDag.length ? (
          <p className="section-note">DAG 全部节点均已执行（{doneDag.length} 个）。</p>
        ) : null}
      </Section>

      <div className="kv-grid">
        <div className="kv"><div className="kv-label">run_id</div><div className="kv-value mono">{run.run_id ?? '未记录'}</div></div>
        <div className="kv"><div className="kv-label">证据包</div><div className="kv-value mono">{run.pack_id}</div></div>
        <div className="kv"><div className="kv-label">仓库</div><div className="kv-value mono">{run.repo ?? '未记录'}</div></div>
        <div className="kv">
          <div className="kv-label">PR</div>
          <div className="kv-value">
            {run.pr_number ? (run.pr_url ? <a href={run.pr_url} target="_blank" rel="noreferrer" aria-label="GitHub PR 页面（新窗口）" title="GitHub PR 页面 — 非 head 绑定的永久证据链接">#{run.pr_number}<ArrowUpRight size={11} strokeWidth={1.75} aria-hidden /></a> : `#${run.pr_number}`) : '未记录'}
            {run.pr_title ? <div className="kv-note" title="标题来自证据包内 PR-METADATA 记录">{run.pr_title}</div> : null}
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

      {e?.note ? (
        <Section title="投递备注（原文）" note="来自投递台账 error/note 字段，原样展示">
          <p className="result-line mono">{e.note}</p>
        </Section>
      ) : null}

      <details className="tech-details">
        <summary>技术详情（DAG 原文标记 / span 汇总 / 归属声明）</summary>
        <div className="tech-body">
          {run.dag?.length ? (
            <>
              <h4>DAG 节点（project/result.md 原文）</h4>
              <ul className="compact-list mono">
                {run.dag.map((d, i) => <li key={i}>[{d.mark}] {d.task_id} — {d.rest}</li>)}
              </ul>
            </>
          ) : null}
          {run.versions?.span_summary ? (
            <>
              <h4>span 汇总（原始 JSON）</h4>
              <pre className="result-line mono">{JSON.stringify(run.versions.span_summary, null, 2)}</pre>
            </>
          ) : null}
          <h4>归属一致性</h4>
          <ul className="compact-list">
            <li>本页全部结论、证据、时间线均归属于上方 head SHA 与 run_id；该 PR 若有新 commit，将以新运行记录呈现，不会覆盖本记录。</li>
            <li>数据来源：证据包 <code>{run.pack_id}</code>（锁定只读快照，数据模式 snapshot）。</li>
          </ul>
        </div>
      </details>
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
  const location = useLocation();
  const backTo = location.state?.from ?? '/runs';
  const [run, setRun] = useState(null);
  const [error, setError] = useState(null);
  const [tab, setTab] = useState('概览');
  const [evidenceFile, setEvidenceFile] = useState(null);
  const [integrity, setIntegrity] = useState(null);
  const openTriggerRef = React.useRef(null); // 抽屉关闭后焦点恢复到触发元素

  const loadRun = useCallback(() => {
    setRun(null); setError(null); setIntegrity(null); setTab('概览');
    api.run(packId).then(setRun).catch(setError);
  }, [packId]);

  const openEvidence = (filePath) => {
    openTriggerRef.current = document.activeElement;
    setEvidenceFile(filePath);
  };
  const closeEvidence = () => {
    setEvidenceFile(null);
    setTimeout(() => openTriggerRef.current?.focus?.(), 0);
  };

  useEffect(() => {
    loadRun();
  }, [loadRun]);

  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && closeEvidence();
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

  // 空数据维度在 tab 上标注 0：不用逐个点开排雷（有数据的维度不标，保持安静）
  const tabZero = run ? {
    时间线: run.timeline?.length ?? 0,
    任务: run.tasks?.length ?? 0,
    Skill: (run.versions?.skills?.length ?? 0) + (run.skill_audit?.invocations?.length ?? 0),
    RAG: run.rag?.calls?.length ?? 0,
    用量: run.usage ? Object.keys(run.usage.windows ?? {}).length : 0,
  } : {};

  return (
    <div>
      <div className="breadcrumb">
        <Link to={backTo} className="crumb-back" title="返回运行历史（保留返回时的筛选条件）">
          <ChevronsLeft size={14} strokeWidth={1.75} aria-hidden /> 运行历史
        </Link>
      </div>

      {error ? <ErrorBox error={error} onRetry={loadRun} /> : !run ? (
        <div className="detail-skeleton">
          <SkeletonRows rows={5} />
        </div>
      ) : (
        <>
          <div className="detail-head">
            <div className="detail-head-main">
              <h1 className="mono detail-title" title={run.run_id ?? ''}>{run.run_id ?? 'run_id 未记录'}</h1>
              <div className="detail-chips">
                <Chip title="数据模式：来自仓库内锁定的真实历史证据包，只读、非实时">历史快照</Chip>
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
                {tabZero[t.key] === 0 ? (
                  <span className="tab-zero" title="本证据包在此维度无数据（0 条）— 如实标注，不以其他数据冒充">0</span>
                ) : null}
              </button>
            ))}
          </div>

          {tab === '概览' && <OverviewTab run={run} onOpenEvidence={setEvidenceFile} />}
          {tab === '时间线' && <TimelineTab run={run} />}
          {tab === '任务' && <TasksTab run={run} onOpenEvidence={openEvidence} />}
          {tab === '证据' && <EvidenceTab packId={packId} onOpenEvidence={openEvidence} />}
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

          <EvidenceDrawer packId={packId} filePath={evidenceFile} onClose={closeEvidence} />
        </>
      )}
    </div>
  );
}
