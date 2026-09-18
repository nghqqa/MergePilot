// frontend/src/components/Filmstrip.jsx — horizontal strip of real evidence events.
// The current replay position is the expanded card; future cards are dimmed.
// Click / Enter on a card jumps the replay there (state from backend, never local).
import React, { useEffect, useRef, useState } from 'react';
import { api } from '../api.js';
import { useDemo } from '../store.jsx';
import { LoadingBox, ErrorBox, SourceRef } from './ui.jsx';
import { IconFilm } from '../icons.jsx';

const ROLE_NAME = {
  leader: 'LEADER', reviewer: 'REVIEWER', fixer: 'FIXER', verifier: 'VERIFIER', human: 'HUMAN', system: 'SYSTEM',
};
const ROLE_CLS = {
  leader: 'info', reviewer: 'off', fixer: 'warn', verifier: 'ok', human: 'bad', system: 'off',
};

export default function Filmstrip({ caseId, idx }) {
  const { setIdx } = useDemo();
  const [tl, setTl] = useState(null);
  const [err, setErr] = useState(null);
  const stripRef = useRef(null);
  const currentRef = useRef(null);

  useEffect(() => {
    api(`/api/cases/${caseId}/timeline`).then((r) => { setTl(r); setErr(null); }).catch(setErr);
  }, [caseId]);

  useEffect(() => {
    // keep the current card in view as replay advances
    if (currentRef.current && stripRef.current) {
      const el = currentRef.current;
      const strip = stripRef.current;
      const target = el.offsetLeft - strip.clientWidth / 2 + el.clientWidth / 2;
      strip.scrollTo({ left: Math.max(0, target), behavior: 'smooth' });
    }
  }, [idx, tl]);

  if (err) return <ErrorBox e={err} text="事件时间线加载失败" />;
  if (!tl) return <LoadingBox text="加载事件…" />;

  const gateHit = tl.events[idx - 1]?.event_type === 'approval';

  return (
    <div className="filmstrip-wrap">
      <div className="filmstrip-head">
        <h2><IconFilm /> 事件胶片</h2>
        <span className="count">{idx}/{tl.total_events} · 真实证据时间线 · UTC</span>
        <span className="count faint">点击卡片跳转回放位置</span>
      </div>
      <div className="filmstrip" ref={stripRef} role="listbox" aria-label="事件回放胶片">
        {tl.events.map((e, i) => {
          const seq = i + 1;
          const state = seq <= idx ? 'done' : 'future';
          const current = seq === idx;
          return (
            <button
              key={e.event_id}
              ref={current ? currentRef : null}
              className={`film-card ${state} ${current ? 'current' : ''} ${current && gateHit ? 'gate-hit' : ''}`}
              onClick={() => setIdx(seq)}
              role="option"
              aria-selected={current}
              aria-label={`事件 ${seq}: ${e.summary}`}
            >
              <span className="fc-top">
                <span className="fc-seq">{String(seq).padStart(2, '0')}</span>
                <span>{e.timestamp}</span>
                <span className={`chip ${ROLE_CLS[e.agent_role] || 'off'}`} style={{ padding: '1px 6px', fontSize: 10 }}>{ROLE_NAME[e.agent_role]}</span>
                {e.timestamp_precision !== 'exact' && <span title={e.timestamp_note || ''}>{e.timestamp_precision}</span>}
              </span>
              <span className="fc-sum">{e.summary}</span>
              {current && <span className="fc-src"><SourceRef>{e.source_ref}</SourceRef></span>}
            </button>
          );
        })}
      </div>
      <SourceRef>全部 {tl.total_events} 条事件均锚定证据文件；事件详情含 Matrix event id 与来源引用（点击当前卡片查看）</SourceRef>
    </div>
  );
}
