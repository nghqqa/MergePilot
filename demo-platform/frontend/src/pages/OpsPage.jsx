// frontend/src/pages/OpsPage.jsx — Phase 15 operations view: RAG status,
// PolarDB audit (NOT CONNECTED) + SIMULATED fixture, data modes, AgentLoop link.
import React, { useEffect, useState } from 'react';
import { api } from '../api.js';
import { LoadingBox, ErrorBox } from '../components/ui.jsx';

export default function OpsPage() {
  const [ops, setOps] = useState(null);
  const [polardb, setPolardb] = useState(null);
  const [fx, setFx] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    api('/api/ops').then(setOps).catch(setErr);
    api('/api/polardb/status').then(setPolardb).catch(setErr);
  }, []);

  const runFixture = async (table, params = {}) => {
    const qs = new URLSearchParams({ table, ...params }).toString();
    setFx(await api(`/api/polardb/fixture?${qs}`));
  };

  if (err) return <main className="page"><ErrorBox e={err} text="运维数据加载失败" /></main>;
  if (!ops || !polardb) return <main className="page" style={{ paddingTop: 40 }}><LoadingBox text="加载运维状态…" /></main>;

  return (
    <main className="page" style={{ paddingTop: 0 }}>
      <div className="case-rail">
        <span className="seg">Operations · RAG · PolarDB · AgentLoop 关联</span>
        <span className="spacer" />
        <span className="chip info">RAG: <b>SYNTHETIC DEMO</b></span>
        <span className="chip off">PolarDB: <b>NOT CONNECTED</b></span>
        <span className="chip warn">Fixture: <b>SIMULATED</b></span>
      </div>

      <div className="resrisk">
        <div className="item">RAG 数据流（SYNTHETIC）</div>
        <dl className="kv small detail">
          <dt>status / data_mode</dt><dd className="mono">{ops.rag.status} · {ops.rag.data_mode}</dd>
          <dt>embedding</dt><dd className="mono small">{ops.rag.embedding_backend}</dd>
          <dt>最近一次查询</dt>
          <dd className="mono small">{ops.rag.last_query ? `${ops.rag.last_query.tool} · arguments_hash=${ops.rag.last_query.arguments_hash} · ${ops.rag.last_query.ts}` : '（尚无查询）'}</dd>
          <dt>tool span 关联</dt><dd className="small">{ops.agentloop.tool_span_correlation}</dd>
        </dl>
      </div>

      <div className="resrisk">
        <div className="item">PolarDB 连接审计 — POLARDB CONNECTION: {polardb.connection}（只读审计，未发起连接）</div>
        <dl className="kv small detail">
          {(polardb.adapter?.gates || []).map((g) => (
            <React.Fragment key={g.key}>
              <dt className="mono">{g.key}</dt>
              <dd>
                <span className={`chip ${g.ok ? 'ok' : 'off'}`}>{String(g.ok)}</span>{' '}
                <span className="faint small">{g.note}</span>
              </dd>
            </React.Fragment>
          ))}
          <dt>verdict</dt><dd className="mono">{polardb.connection}</dd>
          <dt>disclosure</dt><dd className="small">{polardb.adapter?.disclosure || polardb.disclosure}</dd>
        </dl>
      </div>

      <div className="resrisk">
        <div className="item">PolarDB-compatible 只读 fixture — <span className="chip warn">DATA MODE: SIMULATED</span> <span className="chip off">POLARDB CONNECTION: NOT CONNECTED</span></div>
        <div className="detail">
          <p className="small faint" style={{ marginTop: 4 }}>固定形状只读快照（表 + 等值过滤），不是 SQL 引擎，不得表述为真实数据库连接。</p>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <button className="btn ghost" onClick={() => runFixture('pr_cases')}>pr_cases 全表</button>
            <button className="btn ghost" onClick={() => runFixture('pr_cases', { risk_level: 'high' })}>pr_cases · high</button>
            <button className="btn ghost" onClick={() => runFixture('review_findings', { risk_level: 'high' })}>review_findings · high</button>
          </div>
          {fx && (
            <>
              <dl className="kv small" style={{ marginTop: 8 }}>
                <dt>arguments（表/过滤）</dt><dd className="mono">{fx.fixture_table} · rows={fx.row_count} · {fx.latency_ms}ms</dd>
              </dl>
              <table className="mono small" style={{ width: '100%', borderCollapse: 'collapse', marginTop: 6 }}>
                <tbody>
                  {fx.rows.map((r, i) => <tr key={i}><td>{JSON.stringify(r)}</td></tr>)}
                </tbody>
              </table>
              <span className="mono faint small">{fx.disclosure}</span>
            </>
          )}
        </div>
      </div>

      <div className="resrisk">
        <div className="item">AgentLoop 关联</div>
        <dl className="kv small detail">
          <dt>trace_id</dt><dd className="mono">{ops.agentloop.trace_id}</dd>
          <dt>status</dt><dd><span className="chip ok">{ops.agentloop.status} · {ops.agentloop.trace_mode}</span></dd>
          <dt>数据模式总览</dt>
          <dd className="mono small">{Object.entries(ops.data_modes).map(([k, v]) => `${k}: ${v}`).join(' · ')}</dd>
        </dl>
      </div>
    </main>
  );
}
