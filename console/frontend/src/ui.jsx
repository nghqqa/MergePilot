import React, { useState } from 'react';

// 语义化状态徽章。三态（执行/审查/发布）各自独立配色与解释文案，
// 永不合并为一个"成功"。
const TONES = {
  ok: 'var(--c-ok)',
  info: 'var(--c-info)',
  warn: 'var(--c-warn)',
  bad: 'var(--c-bad)',
  neutral: 'var(--c-neutral)',
  accent: 'var(--c-accent)',
};

export function Badge({ tone = 'neutral', title, children }) {
  return (
    <span className={`badge badge-${tone}`} title={title}>
      {children}
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
      setTimeout(() => setCopied(false), 1200);
    } catch { /* clipboard unavailable — the full value is still in title */ }
  };
  return (
    <button className="sha" title={value} onClick={copy}>
      <code>{value.slice(0, n)}</code>
      <span className="sha-hint">{copied ? '已复制' : '复制'}</span>
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
      <span className="spin" aria-hidden /> {label}
    </div>
  );
}

export function ErrorBox({ error }) {
  return (
    <div className="state-box state-error" role="alert">
      <strong>加载失败</strong>
      <div>{String(error?.message ?? error)}</div>
    </div>
  );
}

export function Empty({ children }) {
  return <div className="state-box state-empty">{children ?? '无数据'}</div>;
}

export function Section({ title, actions, children, note }) {
  return (
    <section className="section">
      <div className="section-head">
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

export function Mono({ children, title }) {
  return <code title={title}>{children}</code>;
}
