// frontend/src/pages/RagPage.jsx — Phase 15 synthetic RAG demo: dataset list,
// retrieval with mandatory citations, tool-span audit tail. Every view carries
// the SYNTHETIC label — this is demo documentation, never enterprise data.
import React, { useEffect, useState } from 'react';
import { api } from '../api.js';
import { LoadingBox, ErrorBox } from '../components/ui.jsx';

export default function RagPage() {
  const [dataset, setDataset] = useState(null);
  const [status, setStatus] = useState(null);
  const [spans, setSpans] = useState(null);
  const [query, setQuery] = useState('高危变更的人工审批流程是什么');
  const [result, setResult] = useState(null);
  const [answer, setAnswer] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const load = () => {
    api('/api/rag/dataset').then(setDataset).catch(setErr);
    api('/api/rag/status').then(setStatus).catch(() => {});
    api('/api/rag/toolspans').then(setSpans).catch(() => {});
  };
  useEffect(load, []);

  const run = async (e) => {
    e.preventDefault();
    if (!query.trim()) return;
    setBusy(true); setErr(null);
    try {
      const [r, a] = await Promise.all([
        api(`/api/rag/search?q=${encodeURIComponent(query)}&k=4`),
        api(`/api/rag/answer?q=${encodeURIComponent(query)}`),
      ]);
      setResult(r); setAnswer(a);
      api('/api/rag/toolspans').then(setSpans).catch(() => {});
    } catch (ex) { setErr(ex); }
    setBusy(false);
  };

  if (err && !dataset) return <main className="page"><ErrorBox e={err} text="RAG 数据加载失败" /></main>;
  if (!dataset) return <main className="page" style={{ paddingTop: 40 }}><LoadingBox text="加载 RAG 数据集…" /></main>;

  return (
    <main className="page" style={{ paddingTop: 0 }}>
      <div className="case-rail">
        <span className="seg">RAG · 合成数据集 · 检索 · 引用</span>
        <span className="spacer" />
        <span className="chip info">Data Mode: <b>SYNTHETIC</b></span>
        <span className="chip off">企业生产数据: <b>未接入</b></span>
      </div>

      {status?.agent_correlation && (
        <div className="resrisk" style={{ marginBottom: 14 }}>
          <div className="item">Executed inside real Agent run — <span className="chip warn">{status.agent_correlation.status}</span></div>
          <dl className="kv small detail">
            <dt>last_agent_execution</dt><dd className="mono">{status.agent_correlation.last_agent_execution_utc} · tool rag.retrieve（runtime: rag_retrieve）</dd>
            <dt>evidence</dt><dd className="small">{status.agent_correlation.evidence}</dd>
            <dt>cloud_span_confirmation</dt><dd className="small">{status.agent_correlation.cloud_span_confirmation}</dd>
          </dl>
        </div>
      )}
      <div className="badge-rail" style={{ margin: '4px 0 14px' }}>
        <span className="chip info">状态: <b>{status?.status ?? 'SYNTHETIC DEMO'}</b></span>
        <span className="chip info">文档: <b>{dataset.document_count}</b></span>
        <span className="chip info">分块: <b>{dataset.chunk_count}</b></span>
        <span className="chip info">嵌入: <b className="mono small">{dataset.embedding_backend}</b></span>
        <span className="chip off">Tool span 契约: <b>rag.retrieve / database.query（本阶段为平台侧本地审计）</b></span>
      </div>

      <div className="resrisk">
        <div className="item">检索演示 — 返回 query_hash、top_k、引用 source_ref；不保存 query 原文</div>
        <div className="detail">
          <form onSubmit={run} style={{ display: 'flex', gap: 8 }}>
            <input
              className="mono small" style={{ flex: 1, padding: '6px 10px', background: 'transparent', border: '1px solid var(--line, #333)', borderRadius: 6, color: 'inherit' }}
              value={query} onChange={(e) => setQuery(e.target.value)} placeholder="输入检索问题…"
            />
            <button className="btn ghost" disabled={busy}>{busy ? '检索中…' : '检索'}</button>
          </form>
          {result && (
            <dl className="kv small" style={{ marginTop: 12 }}>
              <dt>query_hash</dt><dd className="mono">{result.query_hash}</dd>
              <dt>top_k</dt><dd className="mono">{result.top_k} · retrieval_mode {result.retrieval_mode} · {result.latency_ms}ms</dd>
            </dl>
          )}
          {result && (
            <table className="mono small" style={{ width: '100%', borderCollapse: 'collapse', marginTop: 8 }}>
              <thead><tr style={{ textAlign: 'left', opacity: 0.7 }}><th>document_id</th><th>chunk_id</th><th>score</th><th>source_ref</th></tr></thead>
              <tbody>
                {result.results.map((r) => (
                  <tr key={r.chunk_id}>
                    <td>{r.document_id}</td><td>{r.chunk_id}</td><td>{r.score}</td>
                    <td className="faint">{r.source_ref}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {answer && answer.status === 'CITED' && (
            <div className="detail" style={{ marginTop: 12 }}>
              <span className="chip ok">Answer: <b>CITED（{answer.citations.length} 条引用）</b></span>{' '}
              <span className="faint small">{answer.note}</span>
              <p className="small" style={{ margin: '8px 0 4px' }}>{answer.answer_text}</p>
              <span className="mono faint small">source_refs: {answer.source_refs.join(' · ')}</span>
            </div>
          )}
          {answer && answer.status !== 'CITED' && (
            <div className="detail" style={{ marginTop: 12 }}>
              <span className="chip bad">Answer: <b>UNVERIFIED（无引用不得标记为 verified）</b></span>
            </div>
          )}
        </div>
      </div>

      <section className="sec" aria-label="数据集清单">
        <div className="sec-head"><h2>数据集清单（全部 SYNTHETIC）</h2><span className="sub">document_id · source_type · classification · content_sha256</span></div>
        <table className="mono small" style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead><tr style={{ textAlign: 'left', opacity: 0.7 }}><th>document_id</th><th>title</th><th>type</th><th>chunks</th><th>content_sha256</th><th>data_mode</th></tr></thead>
          <tbody>
            {dataset.documents.map((d) => (
              <tr key={d.document_id}>
                <td>{d.document_id}</td><td>{d.title}</td><td>{d.source_type}</td><td>{d.chunk_count}</td>
                <td className="faint">{String(d.content_sha256).slice(0, 16)}…</td>
                <td><span className="chip info">{d.data_mode}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {spans && spans.recent?.length > 0 && (
        <section className="sec" aria-label="Tool span 审计">
          <div className="sec-head"><h2>Tool span 审计（契约字段）</h2><span className="sub">rag.retrieve / database.query — 只含契约字段，无正文无凭据</span></div>
          <table className="mono small" style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead><tr style={{ textAlign: 'left', opacity: 0.7 }}><th>ts</th><th>tool</th><th>arguments_hash</th><th>status</th><th>count</th><th>latency</th><th>data_mode</th></tr></thead>
            <tbody>
              {spans.recent.slice().reverse().map((s, i) => (
                <tr key={i}>
                  <td className="faint">{String(s.ts).slice(11, 23)}</td><td>{s.tool}</td><td>{s.arguments_hash}</td>
                  <td>{s.result_status}</td><td>{s.document_count ?? s.row_count}</td><td>{s.latency_ms}ms</td>
                  <td><span className={`chip ${s.data_mode === 'SYNTHETIC' ? 'info' : 'warn'}`}>{s.data_mode}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </main>
  );
}
