// frontend/src/pages/demo/EvidenceLibraryPage.jsx — “查看全部证据” entry.
// Integrity per finals dir, every evidence item of both cases (traceable
// 案例 → PR → SHA → 文件/补丁 → 测试报告 → 终态), the honest NOT_EXECUTED
// boundary, and the legacy Phase-14 replay views.
import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { FolderSearch, ShieldCheck, ChevronRight, Ban } from 'lucide-react';
import { api } from '../../api.js';
import { LevelChip } from '../../components/demo/bits.jsx';
import EvidenceDrawer from '../../components/demo/EvidenceDrawer.jsx';

const LEGACY = [
  ['/cases/pr1-normal-review', 'PR #1 普通协同（自动闭环）'],
  ['/cases/pr2-high-risk-human-gate', 'PR #2 高危人工门（批准）'],
  ['/cases/pr3-high-risk-human-reject', 'PR #3 高危人工门（拒绝）'],
  ['/pr4', 'PR #4 跨仓 Schema（模拟对比 + 真实 SQL 循环）'],
  ['/rework', '返工闭环全量证据页'],
  ['/rag', 'RAG 检索（SYNTHETIC 语料）'],
  ['/audit', '审计 · 完整性与残余风险'],
  ['/ops', 'Operations · 数据模式'],
];

export default function EvidenceLibraryPage() {
  const [o, setO] = useState(null);
  const [a, setA] = useState(null);
  const [b, setB] = useState(null);
  const [err, setErr] = useState(null);
  const [drawer, setDrawer] = useState(null); // [caseId, evidenceId]

  useEffect(() => {
    api('/api/demo/overview').then(setO).catch(setErr);
    api('/api/demo/cases/rework-payments').then(setA).catch(() => setA(null));
    api('/api/demo/cases/db-migration-orders').then(setB).catch(() => setB(null));
  }, []);

  if (err) return <main className="dwrap"><div className="dstate error">证据接口不可达：{err.message}</div></main>;
  if (!o) return <main className="dwrap"><div className="dstate"><span className="spin">◌</span> 读取证据目录…</div></main>;

  const idxOf = (c) => Object.fromEntries((c?.items || []).map((x) => [x.id, x]));
  const aIdx = idxOf(a);
  const bIdx = idxOf(b);

  const boundary = [];
  for (const c of [a, b]) {
    for (const n of c?.honesty?.not_executed || []) if (!boundary.includes(n)) boundary.push(n);
  }

  return (
    <main className="dwrap">
      <div className="eyebrow"><FolderSearch size={13} /> Evidence Library · 全部证据</div>
      <div className="dhead-row">
        <div className="grow">
          <h1 className="dh1">全部证据</h1>
          <p className="dsub">每条证据可追溯：案例 → PR → commit/tree SHA → 文件/补丁 → 测试报告 → 最终状态。数据来源在每条证据中标注，缺失字段显示「未提供」，不用默认值填充。</p>
        </div>
      </div>

      <section className="panel" style={{ marginTop: 16 }}>
        <div className="panel-head"><ShieldCheck size={15} className="faint" /><h2 className="dh2">证据完整性</h2><span className="spacer" /><span className="small faint">启动时逐文件重算 SHA256</span></div>
        <div className="panel-body" style={{ paddingTop: 6 }}>
          {o.platform.integrity.map((d) => (
            <div className="evrow" key={d.key}>
              <div className="t">
                {d.dir}
                <div className="small faint mono" style={{ marginTop: 2 }}>{d.tier} · {d.files} files</div>
              </div>
              <span className={`chip ${d.verified ? 'green' : 'red'}`}>{d.verified ? 'SHA256 校验通过' : '校验未通过'}</span>
            </div>
          ))}
        </div>
      </section>

      {[{ c: a, idx: aIdx, label: '主案例 · 退款幂等缺陷修复闭环' }, { c: b, idx: bIdx, label: '次案例 · 订单库迁移 · 历史数据兼容' }].map(({ c, idx, label }) => c && (
        <section className="panel" style={{ marginTop: 16 }} key={c.case_id}>
          <div className="panel-head">
            <h2 className="dh2">{label}</h2>
            <span className="spacer" />
            <LevelChip level={c.evidence_level} />
            <Link className="evlink" to={`/demo/${c.case_id}`}>打开演示页 <ChevronRight size={12} /></Link>
          </div>
          <div className="panel-body" style={{ paddingTop: 6 }}>
            {c.items.map((it) => (
              <div className="evrow" key={it.id}>
                <div className="t">
                  {it.title}
                  <div className="small faint mono" style={{ marginTop: 2 }}>{it.source_ref}</div>
                </div>
                <LevelChip level={it.level} zh={false} />
                <button className="evlink" onClick={() => setDrawer([c.case_id, it.id])}>打开 <ChevronRight size={13} /></button>
              </div>
            ))}
          </div>
        </section>
      ))}

      {/* dynamic evidence index for every portfolio case (drawer-backed) */}
      <section className="panel" style={{ marginTop: 16 }}>
        <div className="panel-head"><ShieldCheck size={15} className="faint" /><h2 className="dh2">全部案例 · 证据索引</h2><span className="spacer" /><span className="small faint">点击「打开」查看证据原文</span></div>
        <div className="panel-body" style={{ paddingTop: 6 }}>
          {o.cases.map((c) => (
            <div key={c.case_id} style={{ marginBottom: 14 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                <Link className="evlink" style={{ paddingLeft: 0, fontWeight: 600 }} to={c.shape === 'guided' ? `/demo/${c.case_id}?step=1` : `/demo/${c.case_id}`}>{c.name}</Link>
                <LevelChip level={c.evidence_level} />
                <span className="small faint">{c.portfolio_role || ''}</span>
              </div>
              <div style={{ marginTop: 4 }}>
                {(c.evidence_index || []).map((it) => (
                  <div className="evrow" key={c.case_id + '/' + it.id}>
                    <div className="t">
                      {it.title}
                      <div className="small faint mono" style={{ marginTop: 2 }}>{it.level}</div>
                    </div>
                    <button className="evlink" onClick={() => setDrawer([c.case_id, it.id])}>打开 <ChevronRight size={13} /></button>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className="panel" style={{ marginTop: 16 }}>
        <div className="panel-head"><Ban size={15} className="faint" /><h2 className="dh2">明确边界 · NOT_EXECUTED</h2></div>
        <div className="panel-body">
          <div className="notice gray">{boundary.join('；')}。</div>
          <div className="small muted" style={{ marginTop: 8 }}>以上能力在本演示包中未执行；平台不为缺失字段伪造数据（显示「未提供 / 未执行」）。</div>
        </div>
      </section>

      <section className="panel" style={{ marginTop: 16 }}>
        <div className="panel-head"><h2 className="dh2">历史回放包 · Phase 14（旧版视图）</h2><span className="spacer" /><span className="chip slate">HISTORICAL_REPLAY 等</span></div>
        <div className="panel-body" style={{ paddingTop: 6 }}>
          {LEGACY.map(([to, label]) => (
            <div className="evrow" key={to}>
              <div className="t"><Link to={to} className="evlink" style={{ paddingLeft: 0 }}>{label}</Link></div>
            </div>
          ))}
        </div>
      </section>

      {drawer && <EvidenceDrawer caseId={drawer[0]} evidenceId={drawer[1]} onClose={() => setDrawer(null)} />}
    </main>
  );
}
