import React, { useState } from 'react';
import { AlertTriangle, Check, Copy, Inbox, Loader2 } from 'lucide-react';

// 语义化状态徽章：色点 + 文字（颜色永不单独承载状态）。
const TONES = {
  ok: 'var(--c-ok)',
  info: 'var(--c-info)',
  warn: 'var(--c-warn)',
  bad: 'var(--c-bad)',
  neutral: 'var(--c-neutral)',
  accent: 'var(--c-accent)',
};

export function Badge({ tone = 'neutral', title, dot = true, children }) {
  return (
    <span className={`badge badge-${tone}`} title={title}>
      {dot ? <span className="badge-dot" aria-hidden /> : null}
      {children}
    </span>
  );
}

export function Chip({ icon: Icon, children, title, mono = false }) {
  return (
    <span className="chip" title={title}>
      {Icon ? <Icon size={13} strokeWidth={1.75} aria-hidden /> : null}
      {mono ? <code>{children}</code> : children}
    </span>
  );
}

export function Sha({ value, n = 8 }) {
  const [copied, setCopied] = useState(false);
  if (!value) return <span className="muted">未记录</span>;
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch { /* clipboard unavailable — full value remains in title */ }
  };
  return (
    <button className="sha" title={value} onClick={copy}>
      <code>{value.slice(0, n)}</code>
      {copied ? <Check size={12} strokeWidth={2} className="sha-ico ok" aria-hidden /> : <Copy size={12} strokeWidth={1.75} className="sha-ico" aria-hidden />}
    </button>
  );
}

export function KV({ label, children, title }) {
  return (
    <div className="kv" title={title}>
      <div className="kv-label">{label}</div>
      <div className="kv-value">{children}</div>
    </div>
  );
}

export function Spinner({ label = '加载中…' }) {
  return (
    <div className="state-box" role="status">
      <Loader2 size={15} strokeWidth={2} className="spin" aria-hidden /> {label}
    </div>
  );
}

export function SkeletonRows({ rows = 8 }) {
  return (
    <div className="table-scroll" aria-hidden>
      <table className="runs-table">
        <thead>
          <tr>{Array.from({ length: 8 }).map((_, i) => <th key={i}>&nbsp;</th>)}</tr>
        </thead>
        <tbody>
          {Array.from({ length: rows }).map((_, r) => (
            <tr key={r}>
              {Array.from({ length: 8 }).map((_, c) => (
                <td key={c}><span className="skeleton" style={{ width: `${45 + ((r * 7 + c * 13) % 40)}%` }} /></td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ErrorBox({ error }) {
  return (
    <div className="state-box state-error" role="alert">
      <AlertTriangle size={16} strokeWidth={1.75} aria-hidden />
      <div>
        <strong>加载失败</strong>
        <div>{String(error?.message ?? error)}</div>
      </div>
    </div>
  );
}

export function Empty({ icon: Icon = Inbox, children }) {
  return (
    <div className="state-box state-empty">
      <Icon size={18} strokeWidth={1.5} aria-hidden />
      <span>{children ?? '无数据'}</span>
    </div>
  );
}

export function Section({ title, icon: Icon, actions, children, note }) {
  return (
    <section className="section">
      <div className="section-head">
        {Icon ? <Icon size={14} strokeWidth={1.75} aria-hidden className="section-ico" /> : null}
        <h3>{title}</h3>
        {actions}
      </div>
      {note ? <p className="section-note">{note}</p> : null}
      {children}
    </section>
  );
}

// 证据文本查看：所有内容作为纯文本节点渲染（React 自动转义），
// 绝不 innerHTML / 绝不执行其中脚本或链接。
export function EvidencePre({ text, truncated }) {
  return (
    <div className="evidence-text-wrap">
      {truncated ? (
        <div className="truncated-note">内容超过 512 KB，仅显示前 512 KB — 完整内容请下载</div>
      ) : null}
      <pre className="evidence-pre">{text}</pre>
    </div>
  );
}
