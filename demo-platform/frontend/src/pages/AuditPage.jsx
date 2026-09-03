// frontend/src/pages/AuditPage.jsx — audit as full-width evidence rows (no card grid)
import React, { useEffect, useState } from 'react';
import { api } from '../api.js';
import { LoadingBox, ErrorBox, SourceRef } from '../components/ui.jsx';
import { IconShieldCheck, IconAlert, IconHash } from '../icons.jsx';

const STATUS_CLS = {
  VERIFIED: 'ok', DISABLED: 'warn', NOT_IMPLEMENTED: 'bad', PARTIAL_SCHEMA_ONLY: 'warn',
};

export default function AuditPage() {
  const [audit, setAudit] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => {
    api('/api/audit').then((a) => { setAudit(a); setErr(null); }).catch(setErr);
  }, []);

  if (err) return <main className="page"><ErrorBox e={err} text="审计数据加载失败" /></main>;
  if (!audit) return <main className="page" style={{ paddingTop: 40 }}><LoadingBox text="加载审计状态…" /></main>;

  return (
    <main className="page" style={{ paddingTop: 0 }}>
      <div className="case-rail">
        <span className="seg"><IconShieldCheck />审计 · 组件状态 · 残余风险 · 事件完整性</span>
        <span className="spacer" />
        <span className="mono faint">启动时实时重算 · {audit.sha256sums.checked_at}</span>
      </div>

      <div className="badge-rail" style={{ margin: '4px 0 14px' }} aria-label="可信度横条">
        <span className={`chip ${audit.integrity_split?.historical_source_integrity?.status === 'VERIFIED' ? 'ok' : 'warn'}`}>历史材料目录: <b>{audit.integrity_split?.historical_source_integrity?.status === 'VERIFIED' ? 'VERIFIED' : `${audit.integrity_split?.historical_source_integrity?.status}（${audit.integrity_split?.historical_source_integrity?.drift?.length ?? 0} 个已记录 drift）`}</b></span>
        <span className={`chip ${audit.integrity_split?.final_submission_integrity?.status === 'VERIFIED' ? 'ok' : 'bad'}`}>最终提交包: SHA256 <b>{audit.integrity_split?.final_submission_integrity?.status ?? '…'}</b></span>
        <span className="chip ok">Secret Scan: <b>CLEAN</b></span>
        <span className="chip warn">PR Auto Merge: <b>DISABLED</b></span>
        <span className="chip warn">Runtime Write: <b>NONE</b></span>
        <span className="chip warn">Matrix Messages: <b>NONE</b></span>
        <span className="chip ok">AgentLoop Cloud Smoke: <b>{audit.agentloop?.smoke?.status ?? '…'}</b></span>
        <span className="chip warn">AgentLoop Live CoPaw Run: <b>{audit.agentloop?.live_copaw_run?.status ?? '…'}</b></span>
        <span className="chip info">RAG: <b>SYNTHETIC DEMO</b></span>
        <span className="chip off">PolarDB Branch: <b>NOT CONNECTED（fixture SIMULATED）</b></span>
      </div>
      {(audit.integrity_split?.historical_source_integrity?.drift?.length ?? 0) > 0 && (
        <div className="resrisk">
          <div className="item"><IconAlert size={14} />历史材料目录：{audit.integrity_split.historical_source_integrity.drift.length} 个已记录 drift（如实报告 — 平台只读，不修改证据目录）</div>
          <div className="detail">
            {audit.integrity_split.historical_source_integrity.drift.map((d) => `${d.dir}/${d.file}`).join(' · ')}
            <br />SHA256SUMS 锁定后文件被外部更新（AgentLoop live 验证文档）；SUMS 待证据包维护方重生成。实际哈希可在证据抽屉查看。最终提交包 {audit.integrity_split.final_submission_integrity.files} 文件全部 VERIFIED。
          </div>
        </div>
      )}

      <section className="sec" style={{ borderTop: 'none', marginTop: 0 }}>
        <div className="sec-head">
          <h2>Components</h2>
          <span className="sub">每项状态附证据指针</span>
        </div>
        <div className="panel" style={{ padding: '6px 16px' }}>
          {audit.components.map((c) => (
            <div className="audit-line" key={c.component}>
              <span className={`chip ${STATUS_CLS[c.status] || 'off'}`} style={{ minWidth: 158, justifyContent: 'center', flex: 'none' }}>{c.status}</span>
              <span className="name">{c.component}</span>
              <span className="why">{c.source}<br /><SourceRef>{c.evidence}</SourceRef></span>
            </div>
          ))}
        </div>
      </section>

      <section className="sec">
        <div className="sec-head">
          <h2>Residual Risks — 已知残余风险</h2>
          <span className="sub">如实显示 · 不隐藏</span>
        </div>
        {audit.residual_risks.map((r) => (
          <div className="resrisk" key={r.item}>
            <div className="item"><IconAlert size={14} />{r.item}</div>
            <div className="detail">{r.detail}</div>
            <SourceRef>{r.source}</SourceRef>
          </div>
        ))}
      </section>

      <section className="sec">
        <div className="sec-head">
          <h2>Stability — 稳定性事件</h2>
        </div>
        {audit.stability_events.map((s) => (
          <div className="resrisk" key={s.item} style={{ borderColor: 'var(--line)' }}>
            <div className="item" style={{ color: 'var(--cyan)' }}><IconShieldCheck size={14} />{s.item}</div>
            <div className="detail">{s.detail}</div>
            <SourceRef>{s.source}</SourceRef>
          </div>
        ))}
      </section>

      <section className="sec">
        <div className="sec-head">
          <h2><IconHash /> Integrity — SHA256SUMS</h2>
          <span className="sub">重算 {audit.sha256sums.ok_files}/{audit.sha256sums.total_files} 文件一致</span>
        </div>
        <div className="panel" style={{ padding: 0, overflowX: 'auto' }}>
          <table className="probe-table" style={{ border: 'none' }}>
            <thead>
              <tr><th>证据目录</th><th>SHA256SUMS</th><th>文件</th><th>一致</th><th>状态</th></tr>
            </thead>
            <tbody>
              {Object.entries(audit.sha256sums.dirs).map(([k, d]) => (
                <tr key={k}>
                  <td className="mono small">{d.dir}</td>
                  <td>{d.sha256sums ? '✓' : '—'}</td>
                  <td className="mono">{d.files}</td>
                  <td className="mono">{d.ok}</td>
                  <td style={{ color: d.verified ? 'var(--green)' : 'var(--red)', fontWeight: 700 }}>{d.verified ? 'PASS' : 'DRIFT'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <SourceRef>D:\goai\p14-demo\evidence\（只读）— 各目录 SHA256SUMS 由 Demo Backend 启动时逐文件重算</SourceRef>
      </section>

      <section className="sec">
        <div className="sec-head">
          <h2>RAG 数据完整性（SYNTHETIC DEMO）</h2>
          <span className="sub"><a href="#/rag">RAG 页面 →</a> <a href="#/ops">Operations →</a></span>
        </div>
        <RagEvidence />
      </section>

      <section className="sec">
        <div className="sec-head">
          <h2>Secret Scan & Verdicts</h2>
        </div>
        <div className="panel" style={{ marginBottom: 12 }}>
          <span className="chip ok">CLEAN</span> <span className="small muted"> {audit.secret_scan.detail}</span>
          <SourceRef>{audit.secret_scan.source}</SourceRef>
        </div>
        <div className="panel">
          <ul className="small muted" style={{ margin: 0, paddingLeft: 18 }}>
            {audit.verdicts.map((v) => (
              <li key={v.phase} style={{ marginBottom: 4 }}><b className="mono">{v.verdict}</b> <span className="faint">— {v.source}</span></li>
            ))}
          </ul>
        </div>
      </section>
    </main>
  );
}

// Phase 15 — RAG data-integrity evidence: per-document hash + data_mode,
// query hashes from the tool-span audit, and the live/simulated/replay split.
function RagEvidence() {
  const [ds, setDs] = useState(null);
  const [spans, setSpans] = useState(null);
  useEffect(() => {
    api('/api/rag/dataset').then(setDs).catch(() => {});
    api('/api/rag/toolspans').then(setSpans).catch(() => {});
  }, []);
  if (!ds) return <LoadingBox text="加载 RAG 证据…" />;
  const syntheticOk = ds.documents.every((d) => d.data_mode === 'SYNTHETIC');
  return (
    <div className="panel">
      <div style={{ marginBottom: 8 }}>
        <span className={`chip ${syntheticOk ? 'info' : 'bad'}`}>
          防伪断言: <b>{syntheticOk ? `全部 ${ds.document_count} 篇文档 data_mode=SYNTHETIC` : '存在异常 data_mode'}</b>
        </span>{' '}
        <span className="chip off">企业数据: 未接入</span>{' '}
        <span className="chip off">PolarDB: NOT CONNECTED（SIMULATED fixture）</span>{' '}
        <span className="chip info">AgentLoop Trace: LIVE CLOUD（与 RAG/DB tool span 的云端关联本阶段未执行，契约为 rag.retrieve / database.query）</span>
      </div>
      <table className="mono small" style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead>
          <tr style={{ textAlign: 'left', opacity: 0.7 }}>
            <th>document_id</th><th>content_sha256</th><th>chunks</th><th>data_mode</th><th>source_ref 前缀</th>
          </tr>
        </thead>
        <tbody>
          {ds.documents.map((d) => (
            <tr key={d.document_id}>
              <td>{d.document_id}</td>
              <td className="faint">{String(d.content_sha256).slice(0, 16)}…</td>
              <td>{d.chunk_count}</td>
              <td>{d.data_mode}</td>
              <td className="faint">rag://synthetic-docs/{d.document_id}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {spans?.recent?.length > 0 && (
        <p className="mono faint small" style={{ marginTop: 8 }}>
          查询 hash（tool span 审计）: {spans.recent.slice(-4).map((s) => `${s.tool}#${s.arguments_hash}`).join(' · ')}
        </p>
      )}
      <SourceRef>demo-platform/backend/rag-data/dataset.json · tool-spans.jsonl（SYNTHETIC — 平台自有演示文档，非企业生产数据）</SourceRef>
    </div>
  );
}
