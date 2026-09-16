// frontend/src/pages/demo/DecisionPanel.jsx — final disposition answers, honest
// by construction: the five questions (verified / human approval needed / merge
// allowed / actually written to GitHub / replay-simulated-real) are answered from
// the evidence payload, never from button state. The demo approval action below
// is explicitly a replay-layer, in-memory, refresh-resets interaction.
import React, { useState } from 'react';
import { ShieldCheck, Check, X, RotateCcw, MousePointerClick } from 'lucide-react';
import { LevelChip, Verdict, Chain } from '../../components/demo/bits.jsx';

export default function DecisionPanel({ decision, chain, onResettable }) {
  const [demoApproved, setDemoApproved] = useState(false); // in-memory only; refresh resets
  if (!decision) return null;
  const d = decision;

  return (
    <section className="panel" style={{ marginTop: 18 }}>
      <div className="panel-head">
        <ShieldCheck size={16} className="faint" />
        <h2 className="dh2">最终处置 · Decision</h2>
        <span className="spacer" />
        <LevelChip level={d.execution_nature.level} />
      </div>
      <div className="panel-body">
        <div className="decision">
          <div className="drow">
            <span className="q">验证是否通过</span>
            <span className="a">{d.verified.text}</span>
            <span className="mark"><Verdict v={d.verified.verdict === 'PASS' ? 'PASS' : 'FAIL'}>{d.verified.verdict}</Verdict></span>
          </div>
          <div className="drow">
            <span className="q">是否需要人工审批</span>
            <span className="a">{d.human_approval.text}</span>
            <span className="mark"><LevelChip level={d.human_approval.level} zh={false} /></span>
          </div>
          <div className="drow">
            <span className="q">是否允许合并</span>
            <span className="a">{d.merge_allowed.text}</span>
            <span className="mark"><Verdict v={d.merge_allowed.allowed ? 'PASS' : 'WARN'}>{d.merge_allowed.allowed ? '允许（按策略）' : '有条件'}</Verdict></span>
          </div>
          <div className="drow">
            <span className="q">是否实际写入 GitHub</span>
            <span className="a">{d.github_write.text}</span>
            <span className="mark"><LevelChip level={d.github_write.level} zh={false} /></span>
          </div>
          <div className="drow">
            <span className="q">本次演示性质</span>
            <span className="a">{d.execution_nature.text}</span>
            <span className="mark"><LevelChip level={d.execution_nature.level} zh={false} /></span>
          </div>
        </div>

        <div className="approval-box">
          <div className="ab-head"><MousePointerClick size={14} /> 演示审批，不写入运行系统</div>
          <div className="ab-note">
            合并权限未在本演示中执行：审批按钮仅在本页面内存中记录一次演示决定（REPLAY ACTION — NO RUNTIME WRITE），
            刷新或点击重置即恢复。历史证据与运行系统零写入。
          </div>
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginTop: 10, flexWrap: 'wrap' }}>
            {!demoApproved ? (
              <button className="btn ghost small" onClick={() => setDemoApproved(true)}><Check size={14} /> 记录演示审批（仅演示内存）</button>
            ) : (
              <>
                <span className="verdict warn">已记录演示审批 · REPLAY ACTION — NO RUNTIME WRITE</span>
                <button className="btn ghost small" onClick={() => setDemoApproved(false)}><RotateCcw size={13} /> 重置演示审批</button>
              </>
            )}
          </div>
        </div>

        {chain && <Chain nodes={chain} />}
        {onResettable}
      </div>
    </section>
  );
}

export function NotExecutedNote({ items }) {
  if (!items || !items.length) return null;
  return (
    <div className="notice gray" style={{ marginTop: 14 }}>
      <X size={14} style={{ flex: 'none', marginTop: 2 }} />
      <span>本演示未执行：{items.join('；')}。界面对以上能力如实标注 NOT_EXECUTED，不用默认值填充。</span>
    </div>
  );
}
