import React, { useState } from 'react';
import { AlertTriangle, Check, Copy, Inbox, Loader2, RotateCcw } from 'lucide-react';

// 品牌标：两条分支汇入一条主干（merge），自绘 SVG，深青单色。
export function BrandMark() {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M6 4v6c0 3 2 5 5 5h7" stroke="#2dd4bf" strokeWidth="2" strokeLinecap="round" />
      <path d="M18 4v6c0 1.5-.6 2.8-1.6 3.8" stroke="#0e6b62" strokeWidth="2" strokeLinecap="round" opacity="0.85" />
      <circle cx="6" cy="4" r="2.2" fill="#2dd4bf" />
      <circle cx="18" cy="4" r="2.2" fill="#0e6b62" />
      <circle cx="18" cy="15" r="2.2" fill="#e6f2f0" />
    </svg>
  );
}

// 单页渲染异常兜底：一页出错不白屏整个控制台（错误只落到当前内容区）。
export class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }
  static getDerivedStateFromError(error) {
    return { error };
  }
  render() {
    if (this.state.error) {
      return (
        <div className="state-box state-error" role="alert">
          页面渲染出错：{String(this.state.error?.message ?? this.state.error)} —
          {' '}可<a href="/repos">返回仓库工作台</a>或刷新页面。
        </div>
      );
    }
    return this.props.children;
  }
}

// 语义化状态徽章：色点 + 文字（颜色永不单独承载状态）。
// tabIndex=0 兑现"悬停或聚焦可查看"的承诺：键盘 Tab 到徽章即可读到 title 里的语义边界与来源。
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
    <span className={`badge badge-${tone}`} title={title} tabIndex={0}>
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

export function SkeletonRows({ rows = 8, cols = 7 }) {
  return (
    <div className="table-scroll" aria-hidden>
      <table className="runs-table">
        <thead>
          <tr>{Array.from({ length: cols }).map((_, i) => <th key={i}>&nbsp;</th>)}</tr>
        </thead>
        <tbody>
          {Array.from({ length: rows }).map((_, r) => (
            <tr key={r}>
              {Array.from({ length: cols }).map((_, c) => (
                <td key={c}><span className="skeleton" style={{ width: `${45 + ((r * 7 + c * 13) % 40)}%` }} /></td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ErrorBox({ error, onRetry }) {
  return (
    <div className="state-box state-error" role="alert">
      <AlertTriangle size={16} strokeWidth={1.75} aria-hidden />
      <div>
        <strong>加载失败</strong>
        <div>{String(error?.message ?? error)}</div>
        {onRetry ? <div className="error-hint">快照读取失败时可重试；若持续失败，请检查证据包目录是否完整。</div> : null}
      </div>
      {onRetry ? (
        <button type="button" className="btn btn-sm" onClick={onRetry}>
          <RotateCcw size={12} strokeWidth={1.75} aria-hidden /> 重试
        </button>
      ) : null}
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
