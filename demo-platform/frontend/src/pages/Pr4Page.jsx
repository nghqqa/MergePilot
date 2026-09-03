// frontend/src/pages/Pr4Page.jsx — Phase 16 PR #4: CROSS-REPO ORDER SCHEMA MIGRATION.
// DISCOVER → RAG IMPACT → CANDIDATES → BRANCH VALIDATION → REJECTED → VERIFIED → HUMAN GATE → COMPLETE
// 大白话优先：每个技术检查都配一句"人话"；原始键名以小字保留供评委核对。
// All data SYNTHETIC / SIMULATED unless labeled otherwise. Failed candidates are never promoted.
import React, { useEffect, useState } from 'react';
import { api } from '../api.js';
import { LoadingBox, ErrorBox } from '../components/ui.jsx';

const VERDICT_CLS = { VERIFIED: 'ok', DEGRADED: 'warn', REJECTED: 'bad' };
const VERDICT_PLAIN = { VERIFIED: '通过（可执行）', DEGRADED: '降级淘汰（切换期会出问题）', REJECTED: '淘汰（会弄坏数据）' };

// 断言键名 → 大白话
const PLAIN_CHECK = {
  historical_null_backfilled: '旧数据补齐 —— 137 笔缺客户号的订单必须先补全',
  unique_index_created: '重复订单清理 —— 2 笔重复会挡住「不允许重复」的新规则',
  old_worker_compat: '旧程序兼容 —— 切换窗口期，旧程序写入的数据也要合法',
  refunded_enum_added: '新增「已退款」状态成功',
  rollback_check: '可安全回滚 —— 主数据库不受任何影响',
};

const PLAIN_ASSERT = {
  assert_data: '数据核对',
  rollback_check: '回滚检查',
};

const fmtRowCounts = (rc) =>
  `总订单 ${rc.orders_total} · 缺客户号 ${rc.orders_customer_id_null} · 重复订单 ${rc.payments_duplicate_order_id} · 已退款 ${rc.order_status_refunded_rows}`;

export default function Pr4Page() {
  const [pr4, setPr4] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    api('/api/pr4').then(setPr4).catch(setErr);
  }, []);

  if (err) return <main className="page"><ErrorBox e={err} text="PR #4 数据加载失败" /></main>;
  if (!pr4) return <main className="page" style={{ paddingTop: 40 }}><LoadingBox text="加载 PR #4 跨仓案例…" /></main>;

  const verified = pr4.candidates.find((c) => c.validation.verdict === 'VERIFIED');

  return (
    <main className="page" style={{ paddingTop: 0 }}>
      <div className="case-rail">
        <span className="seg">PR #4 · 一次改表，牵动三个系统</span>
        <span className="spacer" />
        <span className="chip info">RAG: <b>SYNTHETIC</b></span>
        <span className="chip warn">Branch: <b>SIMULATED</b></span>
        <span className="chip off">PolarDB: <b>NOT CONNECTED</b></span>
      </div>

      <div className="resrisk" style={{ marginBottom: 14 }}>
        <div className="item" style={{ fontSize: '1.05em' }}>
          <b>一句话：</b>数据库要改表，但不能直接改——系统先查出历史经验，再在一份<b>隔离副本</b>上把每种改法各试一遍：
          会弄坏数据的改法<b>自动淘汰</b>，能安全执行的改法<b>交给人批准</b>后才真正执行。
        </div>
      </div>

      <div className="badge-rail" style={{ margin: '4px 0 14px' }}>
        {['① 发现影响', '② 查历史经验', '③ 列出改法', '④ 副本试错', '⑤ 淘汰坏方案', '⑥ 好方案出炉', '⑦ 人工批准', '⑧ 收尾'].map((f) => (
          <span key={f} className="chip info">{f}</span>
        ))}
      </div>

      <div className="resrisk">
        <div className="item">① 发现影响 —— 这次改表会牵动哪三个系统、哪些数据有「历史遗留问题」</div>
        <dl className="kv small detail">
          <dt>关联系统</dt><dd className="mono">{pr4.repos.join(' · ')}</dd>
          <dt>要改什么</dt><dd className="small">{pr4.schema_baseline.changes.join('；')}</dd>
          <dt>历史遗留</dt>
          <dd className="small">orders 表有 <b>137 笔订单缺客户号</b> · payments 表有 <b>2 笔重复订单</b>（直接改规则会立刻报错）</dd>
          <dt>兼容要求</dt><dd className="small">{pr4.schema_baseline.compat}</dd>
        </dl>
      </div>

      <div className="resrisk">
        <div className="item">② 查历史经验 —— 从过往案例里找「这次该注意什么」（合成数据集，只记检索编号不记原文）</div>
        <dl className="kv small detail">
          <dt>检索编号</dt><dd className="mono">{pr4.rag_impact.query_hash}</dd>
          <dt>命中的经验</dt>
          <dd className="mono small">{pr4.rag_impact.results.map((r) => `${r.document_id} (${r.score})`).join(' · ')}</dd>
          <dt>引用来源</dt>
          <dd className="mono faint small">{pr4.rag_impact.results.map((r) => r.source_ref).join(' · ')}</dd>
        </dl>
      </div>

      <div className="resrisk">
        <div className="item">③④⑤ 三种改法，在隔离副本上各试一遍 —— 逐项安全检查，坏的自动淘汰</div>
        <div className="detail">
          {pr4.candidates.map((c) => (
            <div key={c.candidate_id} style={{ marginBottom: 14 }}>
              <span className={`chip ${VERDICT_CLS[c.validation.verdict]}`}>
                {c.candidate_id}：{VERDICT_PLAIN[c.validation.verdict]}
              </span>{' '}
              <span className="small"><b>{c.title}</b> — {c.strategy}</span>
              <span className="mono faint small"> · 涉及 {c.repos_touched.join('/')} · 副本 {c.branch.branch_id}</span>
              {c.failure_reasons.length > 0 && (
                <ul className="small muted" style={{ margin: '4px 0 0', paddingLeft: 18 }}>
                  {c.failure_reasons.map((fr, i) => <li key={i}>{fr}</li>)}
                </ul>
              )}
              <table className="mono small" style={{ width: '100%', borderCollapse: 'collapse', marginTop: 4 }}>
                <tbody>
                  {c.validation.assertions.map((a) => (
                    <tr key={a.name}>
                      <td style={{ width: '42%' }}>{PLAIN_CHECK[a.name] || a.name}</td>
                      <td style={{ width: '10%', color: a.passed ? 'var(--green)' : 'var(--red)', fontWeight: 700 }}>
                        {a.passed ? '通过' : '未通过'}
                      </td>
                      <td style={{ width: '38%' }}>{PLAIN_CHECK[a.name] ? '' : a.detail}</td>
                      <td className="faint" style={{ width: '10%' }}>{a.name}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <dl className="kv small">
                <dt>{PLAIN_ASSERT.assert_data}</dt>
                <dd className="mono small">{fmtRowCounts(c.assert_data.row_counts)} → {c.assert_data.passed ? '通过' : '未通过'}</dd>
                <dt>{PLAIN_ASSERT.rollback_check}</dt>
                <dd className="mono small">主库未动 {String(c.rollback_check.main_untouched)} · 副本可删 {String(c.rollback_check.branch_droppable)} → 通过</dd>
              </dl>
            </div>
          ))}
        </div>
      </div>

      <div className="resrisk">
        <div className="item">⑥ 结果 —— 坏方案挡在门外，好方案进入人工批准</div>
        <div className="detail">
          {pr4.rejected.map((id) => <span key={id} className="chip bad" style={{ marginRight: 6 }}>已淘汰: {id}</span>)}
          {verified && <span className="chip ok">可执行: {verified.candidate_id}（5/5 检查通过，唯一候选）</span>}
        </div>
      </div>

      <div className="resrisk">
        <div className="item">⑦ 人工批准 —— 就算是通过所有检查的方案，真正动数据库之前也要人点头</div>
        <dl className="kv small detail">
          <dt>当前状态</dt><dd><span className="chip warn">{pr4.human_gate.state}</span></dd>
          <dt>说明</dt><dd className="small">{pr4.human_gate.note}</dd>
          <dt>待批准方案</dt><dd className="mono">{pr4.promoted ? `${pr4.promoted.candidate_id}（${pr4.promoted.verdict}）` : '无'}</dd>
        </dl>
      </div>

      <div className="resrisk">
        <div className="item">⑧ 全程留痕 —— 每一步都可以回查</div>
        <dl className="kv small detail">
          <dt>权威 Trace</dt><dd className="mono">{pr4.agentloop.authoritative_trace_id}</dd>
          <dt>RAG 关联 Trace</dt><dd className="mono">{pr4.agentloop.rag_correlation_trace_id}</dd>
          <dt>DB 工具关联</dt><dd className="small">{pr4.agentloop.db_tool_correlation}</dd>
          <dt>数据模式</dt><dd className="mono small">{Object.entries(pr4.data_modes).map(([k, v]) => `${k}=${v}`).join(' · ')}</dd>
          <dt>integrity_status</dt><dd className="small">{pr4.integrity_status}</dd>
        </dl>
      </div>
    </main>
  );
}
