// frontend/src/components/demo/bits.jsx — shared primitives for the finals
// guided-demo world (light workspace + dark evidence areas).
import React, { useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronRight, Copy, Check, ArrowRight } from 'lucide-react';

export const LEVEL_ZH = {
  REAL_EXECUTED: '真实执行',
  LOCAL_REAL_SQL: '本地真实 SQL',
  CONTROL_PLANE_MECHANISM: '控制面机制验证',
  SYNTHETIC: '合成数据',
  HISTORICAL_REPLAY: '历史回放',
  NOT_EXECUTED: '未执行',
};

export function LevelChip({ level, zh = true }) {
  if (!level) return <span className="chip gray">未标注</span>;
  return (
    <span className={`chip lv-${level}`}>
      <span className="cdot" aria-hidden="true" />
      <span className="mono">{level}</span>
      {zh && <span style={{ fontWeight: 500 }}>{LEVEL_ZH[level] || ''}</span>}
    </span>
  );
}

export function Verdict({ v, children }) {
  const cls = v === true || v === 'PASS' ? 'pass' : v === false || v === 'FAIL' ? 'fail' : v === 'WARN' ? 'warn' : 'na';
  return <span className={`verdict ${cls}`}>{children}</span>;
}

// Scrollable dark code/log/diff block with a copy button. Never stretches the
// page: the <pre> scrolls internally in both axes.
export function CodeBlock({ title, text, lang = 'text', max = 420 }) {
  const [copied, setCopied] = useState(false);
  const t = typeof text === 'string' ? text : JSON.stringify(text, null, 2);
  if (!t || t === '未提供') return <div className="notice gray">未提供</div>;
  const doCopy = async () => {
    try {
      await navigator.clipboard.writeText(t);
    } catch {
      const ta = document.createElement('textarea');
      ta.value = t; document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); } catch { /* clipboard unavailable */ }
      ta.remove();
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1400);
  };
  return (
    <div className="codeblock dark-scroll">
      <div className="codeblock-head">
        <span className="fn">{title}</span>
        <span className="spacer" />
        <button className={`copybtn ${copied ? 'ok' : ''}`} onClick={doCopy}>
          {copied ? <Check size={12} /> : <Copy size={12} />}{copied ? '已复制' : '复制'}
        </button>
      </div>
      <pre style={{ maxHeight: max }} aria-label={title}>
        {lang === 'diff'
          ? t.split('\n').map((line, i) => (
            <span key={i} className={line.startsWith('+') && !line.startsWith('+++') ? 'add' : line.startsWith('-') && !line.startsWith('---') ? 'del' : line.startsWith('@@') ? 'hunk' : ''}>{line + '\n'}</span>
          ))
          : t.split('\n').map((line, i) => (
            <span key={i} className={/FAIL|AssertionError|ERROR|failed/i.test(line) && lang === 'log' ? 'warnline' : ''}>{line + '\n'}</span>
          ))}
      </pre>
    </div>
  );
}

export function KV({ rows }) {
  return (
    <dl className="kv">
      {rows.filter(Boolean).map(([k, v, mono]) => (
        <React.Fragment key={k}>
          <dt className="k">{k}</dt>
          <dd className={`v ${mono ? 'mono' : ''}`}>{v ?? '未提供'}</dd>
        </React.Fragment>
      ))}
    </dl>
  );
}

export function Points({ rows }) {
  return (
    <div className="gpoints">
      {rows.filter(Boolean).map((r) => (
        <div className="row" key={r.k}>
          <div className="k">{r.k}</div>
          <div className={`v ${r.mono ? 'mono' : ''}`}>{r.v}</div>
        </div>
      ))}
    </div>
  );
}

export function HonestTriple({ real, controlled, not_executed }) {
  const join = (a) => (a && a.length ? a.join('；') : '—');
  return (
    <div className="honesty">
      <div className="h real"><span className="hk">真实执行</span><span className="hv">{join(real)}</span></div>
      <div className="h ctrl"><span className="hk">受控输入</span><span className="hv">{join(controlled)}</span></div>
      <div className="h nexec"><span className="hk">未执行</span><span className="hv">{join(not_executed)}</span></div>
    </div>
  );
}

export function Collapsed({ title, children, defaultOpen = false }) {
  return (
    <details className="dcollapse" open={defaultOpen}>
      <summary>
        <span className="chev"><ChevronDown size={14} /></span>
        {title}
      </summary>
      <div className="dc-body">{children}</div>
    </details>
  );
}

// Five-node step bar; every node is clickable (jump to any key node).
export function StepBar({ steps, current, onJump }) {
  return (
    <nav className="stepbar" aria-label="演示步骤">
      {steps.map((s, i) => (
        <React.Fragment key={s.id}>
          {i > 0 && <span className={`stepline ${i <= current ? 'done' : ''}`} aria-hidden="true" />}
          <button
            className={`stepnode ${i === current ? 'current' : i < current ? 'done' : ''}`}
            onClick={() => onJump(i)}
            aria-current={i === current ? 'step' : undefined}
            title={`跳到第 ${i + 1} 步 · ${s.title}`}
          >
            <span className="num">{i < current ? <Check size={13} /> : i + 1}</span>
            <span className="lbl">{s.title}</span>
          </button>
        </React.Fragment>
      ))}
    </nav>
  );
}

export function Chain({ nodes }) {
  return (
    <div className="chain" aria-label="证据链">
      {nodes.map((n, i) => (
        <React.Fragment key={`${n}-${i}`}>
          {i > 0 && <span className="carr"><ArrowRight size={12} /></span>}
          <span className="cnode mono">{n}</span>
        </React.Fragment>
      ))}
    </div>
  );
}

// Compact evidence list for brief blocks: title + level chip + open-drawer link.
export function EvidenceLinkRow({ items, itemsById, onOpen }) {
  if (!items?.length) return null;
  return (
    <div style={{ borderTop: '1px solid var(--p-sunken)', marginTop: 4 }}>
      {items.map((id) => {
        const it = itemsById?.[id];
        if (!it) return null;
        return (
          <div className="evrow" key={id}>
            <div className="t">{it.title}<div style={{ marginTop: 3 }}><LevelChip level={it.level} zh={false} /></div></div>
            <button className="evlink" onClick={() => onOpen(id)}>查看证据 <ChevronRight size={13} /></button>
          </div>
        );
      })}
    </div>
  );
}

// Locks page scroll while `on` is true (drawer open).
export function useScrollLock(on) {
  useEffect(() => {
    if (!on) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => { document.body.style.overflow = prev; };
  }, [on]);
}

export function useEsc(onClose, active) {
  const ref = useRef(onClose);
  ref.current = onClose;
  useEffect(() => {
    if (!active) return;
    const h = (e) => { if (e.key === 'Escape') ref.current(); };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [active]);
}

export { ChevronRight };
