import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { AlertTriangle, Check, Copy, Inbox, Loader2, RotateCcw } from 'lucide-react';

// 品牌标：两条分支经中央控制节点汇入闸门输出（M / gate 形态）。
// variant: 'light'（浅色背景）| 'dark'（深色背景）| 'mono'（单色，跟随 currentColor）
const BRAND = {
  light: { in: '#0e6b62', out: '#14b8a6', node: '#0e6b62' },
  dark: { in: '#5eead4', out: '#2dd4bf', node: '#f8fafc' },
  mono: { in: 'currentColor', out: 'currentColor', node: 'currentColor' },
};

export function BrandMark({ variant = 'light', size = 22 }) {
  const c = BRAND[variant] ?? BRAND.light;
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M5 3.5v5.4a4.6 4.6 0 0 0 4.6 4.6h.4" stroke={c.in} strokeWidth="2.2" strokeLinecap="round"/>
      <path d="M19 3.5v5.4a4.6 4.6 0 0 1-4.6 4.6h-.4" stroke={c.out} strokeWidth="2.2" strokeLinecap="round"/>
      <circle cx="12" cy="13.5" r="2.5" fill={c.node}/>
      <path d="M12 16v5" stroke={c.in} strokeWidth="2.2" strokeLinecap="round"/>
      <path d="M9.6 18.9h4.8" stroke={c.out} strokeWidth="1.8" strokeLinecap="round"/>
    </svg>
  );
}

// 文字标（用于登录页/文档头）：mark + 字标，浅色背景用深字，深色背景用浅字
export function Wordmark({ variant = 'light', size = 22 }) {
  const c = BRAND[variant] ?? BRAND.light;
  const textColor = variant === 'dark' ? '#f8fafc' : '#131a26';
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, color: textColor }}>
      <BrandMark variant={variant} size={size} />
      <span style={{ fontSize: 16, fontWeight: 600, letterSpacing: '0.2px', fontFamily: "system-ui, 'Segoe UI', 'Microsoft YaHei', sans-serif" }}>
        MergePilot
      </span>
    </span>
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

// ── 会话/权限/网络异常的统一恢复入口 ──
// 红线（UX 收敛审查）：401、403、session 过期、数据源不可达不允许只显示状态文字——
// 必须给出可执行恢复按钮。恢复路径按数据源模式分流：
//   multiuser → GitHub OAuth 重新登录（GET /api/mu/auth/oauth/github/start → authorize_url）
//   其余模式  → 跳转登录页（legacy 账号密码）+ 刷新会话探测
// status: 401（会话过期/未登录）| 403（权限不足）| number（其他 HTTP）| null（网络不可达）
export async function startRelogin(mode) {
  if (mode === 'multiuser') {
    try {
      const res = await fetch('/api/mu/auth/oauth/github/start', { credentials: 'same-origin' });
      const body = await res.json().catch(() => null);
      if (res.ok && body?.authorize_url) {
        window.location.href = body.authorize_url;
        return true;
      }
    } catch { /* 落到失败提示 */ }
    return false;
  }
  window.location.href = '/login';
  return true;
}

export function RecoveryBox({ error, onRetry, mode, subject = '数据' }) {
  const [busy, setBusy] = useState(false);
  const status = error?.status ?? null;
  const msg = String(error?.message ?? error ?? '网络不可达');
  const relogin = async () => {
    setBusy(true);
    const ok = await startRelogin(mode);
    if (!ok) setBusy(false); // 跳转失败时保持按钮可点，并提示
  };
  const actions = (
    <div className="recovery-actions">
      {status === 401 ? (
        <>
          <button type="button" className="btn btn-primary btn-sm" disabled={busy} onClick={relogin}>
            {busy ? '正在跳转登录…' : '重新登录'}
          </button>
          {onRetry ? (
            <button type="button" className="btn btn-sm" onClick={onRetry}>
              <RotateCcw size={12} strokeWidth={1.75} aria-hidden /> 刷新重试
            </button>
          ) : null}
        </>
      ) : status === 403 ? (
        <>
          <Link className="btn btn-sm" to="/multiuser">查看组织与接入（角色说明）</Link>
          {onRetry ? (
            <button type="button" className="btn btn-sm" onClick={onRetry}>
              <RotateCcw size={12} strokeWidth={1.75} aria-hidden /> 重试
            </button>
          ) : null}
        </>
      ) : (
        <>
          {onRetry ? (
            <button type="button" className="btn btn-primary btn-sm" onClick={onRetry}>
              <RotateCcw size={12} strokeWidth={1.75} aria-hidden /> 重试
            </button>
          ) : null}
          <button type="button" className="btn btn-sm" onClick={() => window.location.reload()}>刷新页面</button>
        </>
      )}
    </div>
  );
  return (
    <div className="state-box state-error" role="alert">
      <AlertTriangle size={16} strokeWidth={1.75} aria-hidden />
      <div>
        <strong>
          {status === 401 ? '登录已过期或未登录'
            : status === 403 ? '当前角色无权访问'
              : status ? `服务返回错误（HTTP ${status}）` : '数据源不可达'}
        </strong>
        <div className="error-hint">
          {status === 401 && <>读取{subject}需要有效会话（401 如实）——重新登录后恢复。</>}
          {status === 403 && <>会话有效但角色权限不足（403 如实）——请联系管理员调整角色，或查看你在"组织与接入"页的角色。</>}
          {status != null && status !== 401 && status !== 403 && <>{msg}——可重试；持续失败请联系管理员检查服务状态。</>}
          {status == null && <>无法连接到服务（{msg}）——请检查网络或确认 console 后端已启动。</>}
        </div>
        {actions}
      </div>
    </div>
  );
}

// ── 工作台统计卡：数字即入口 ──
// 必须是真实 <Link>/<button>（不允许 div role=link 模拟）；active 态标注 aria-current。
// to 有值 → Link（可中键/新开）；否则 onClick → button。
export function StatCard({ label, count, tone = 'neutral', to, onClick, active = false, title, loading = false }) {
  const cls = `stat-card stat-tone-${tone}${active ? ' stat-active' : ''}`;
  const inner = (
    <>
      <span className="stat-count">
        {loading ? <Loader2 size={18} className="spin" aria-label="统计加载中" /> : count}
      </span>
      <span className="stat-label">{label}</span>
    </>
  );
  if (to) {
    return (
      <Link to={to} className={cls} title={title} aria-current={active ? 'true' : undefined}
        onClick={onClick}>
        {inner}
      </Link>
    );
  }
  return (
    <button type="button" className={cls} title={title} aria-pressed={active}
      aria-current={active ? 'true' : undefined} onClick={onClick}>
      {inner}
    </button>
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
