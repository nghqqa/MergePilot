import React, { useCallback, useEffect, useState } from 'react';
import { useAuth } from '../auth.jsx';

// 核心控制面（CANONICAL_CONSOLE_PROMOTION 迁移页）：五个 Core API 的实时视图。
// 诚实语义：未登录 401 → 引导登录；无 DSN → BACKEND_NOT_WIRED；连接失败 →
// BACKEND_ERROR；成功 → POSTGRESQL_LIVE。不伪造任何状态。
const REFRESH_MS = 10_000;

async function apiGet(path) {
  const res = await fetch(path, { credentials: 'same-origin' });
  let body = null;
  try { body = await res.json(); } catch { /* non-json */ }
  if (!res.ok) {
    const err = new Error(body?.error?.reason || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return body;
}

function Dot({ tone }) {
  return <span className={`dot dot-${tone}`} aria-hidden style={{
    display: 'inline-block', width: 8, height: 8, borderRadius: '50%',
    background: tone === 'ok' ? 'var(--c-ok)' : tone === 'warn' ? 'var(--c-warn)' : 'var(--c-bad)',
    marginRight: 6,
  }} />;
}

export default function CorePage() {
  const auth = useAuth();
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [lastRefresh, setLastRefresh] = useState(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [pulls, pending, tickets, evidence, audit] = await Promise.all([
        apiGet('/api/pulls'), apiGet('/api/pending'), apiGet('/api/tickets'),
        apiGet('/api/evidence'), apiGet('/api/audit'),
      ]);
      setData({ pulls, pending, tickets, evidence, audit });
      setLastRefresh(new Date().toLocaleTimeString());
    } catch (e) {
      setError(e);
    }
  }, []);

  useEffect(() => {
    if (auth.status !== 'authed') return;
    load();
    const t = setInterval(load, REFRESH_MS);
    return () => clearInterval(t);
  }, [auth.status, load]);

  if (auth.status !== 'authed') {
    return (
      <div>
        <div className="page-head"><div><h1>系统状态与接线</h1>
          <p className="page-sub">核心 API 接线与健康视图：供排查"是坏了还是没接"，非日常工作流。</p>
        </div></div>
        <section className="section">
          <div className="state-box state-warn" role="status">
            需要登录 — 本页数据受服务端会话与仓库 allowlist 保护。
          </div>
        </section>
      </div>
    );
  }

  const source = data?.pulls?.source;
  const tone = source === 'POSTGRESQL_LIVE' ? 'ok' : source === 'BACKEND_ERROR' ? 'bad' : 'warn';

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>系统状态与接线</h1>
          <p className="page-sub">
            授权仓库：{auth.user?.repos?.join(' · ') || '（allowlist 未配置）'}
            {lastRefresh ? ` · 刷新于 ${lastRefresh} · 每 ${REFRESH_MS / 1000}s` : ''}
          </p>
        </div>
      </div>

      <section className="section">
        <div className="section-head"><h3>接线状态</h3></div>
        {error ? (
          <div className="state-box state-error" role="status">
            请求失败 — {String(error.message || error)}（{error.status === 401 ? '会话可能已过期，请刷新重登' : 'API 错误'}）
          </div>
        ) : !data ? (
          <div className="state-box" role="status">加载中…</div>
        ) : source === 'POSTGRESQL_LIVE' ? (
          <div className="state-box state-ok" role="status"><Dot tone="ok" />
            PostgreSQL 实时查询 · <code>source: POSTGRESQL_LIVE</code>
          </div>
        ) : source === 'BACKEND_NOT_WIRED' ? (
          <div className="state-box state-warn" role="status"><Dot tone="warn" />
            后端未接线 — CONSOLE_PG_DSN 未配置；以下为空状态，非真实数据。
          </div>
        ) : (
          <div className="state-box state-error" role="status"><Dot tone="bad" />
            后端错误 — {data.pulls?.error}。请检查 PG 连接。
          </div>
        )}
      </section>

      <section className="section">
        <div className="section-head"><h3>Gate / Bridge 分层</h3></div>
        <div className="panel" style={{ fontSize: 13, lineHeight: 1.9 }}>
          <strong>Skill Gate PRODUCE</strong> = Skill receipts 齐备、有效、绑定正确 —
          <em>不等于安全风险已批准</em>；<br />
          <strong>HIGH finding 人工门</strong> 由 Bridge 风险策略执行 → 输出
          <code> action_required</code>（≠ success）· 当前 pilot 禁止 approve/reject；<br />
          RAG = <strong>EXCLUDED</strong> · Fixer/Verifier = <strong>DISABLED</strong> ·
          merge/push = <strong>DISABLED</strong>。
        </div>
      </section>

      {data && (
        <>
          <section className="section">
            <div className="section-head"><h3>PR / Head（{data.pulls.pulls?.length ?? 0}）</h3></div>
            <div className="panel">
              <table className="data-table">
                <thead><tr><th>Repo</th><th>PR#</th><th>Head SHA</th><th>Run ID</th></tr></thead>
                <tbody>
                  {(data.pulls.pulls || []).map((p, i) => (
                    <tr key={i}>
                      <td>{p.repo}</td>
                      <td>{p.pr_number != null ? `#${p.pr_number}` : '—'}</td>
                      <td><code>{p.head_sha?.slice(0, 12)}</code></td>
                      <td><code>{p.run_id}</code></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <section className="section">
            <div className="section-head"><h3>待处理队列（{data.pending.pending?.length ?? 0}）</h3></div>
            <div className="panel">
              <table className="data-table">
                <thead><tr><th>Ticket</th><th>Repo / PR</th><th>Action</th><th>TTL</th><th>状态</th></tr></thead>
                <tbody>
                  {(data.pending.pending || []).map((t, i) => {
                    const expired = t.approval_expires_at ? new Date(t.approval_expires_at) < new Date() : false;
                    return (
                      <tr key={i}>
                        <td><code>{t.ticket_id?.slice(0, 16)}…</code></td>
                        <td>{t.repo} {t.pr_number != null ? `#${t.pr_number}` : ''}</td>
                        <td>{t.action}</td>
                        <td>{expired ? '已过期' : '有效'}</td>
                        <td>{expired ? 'EXPIRED' : t.status}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {!data.pending.pending?.length && (
                <p className="page-sub" style={{ padding: 8 }}>队列为空（诚实零值 — 无伪造门）。</p>
              )}
            </div>
          </section>

          <section className="section">
            <div className="section-head"><h3>Gate 审计（{data.audit.gate_decisions?.length ?? 0}）</h3></div>
            <div className="panel">
              <table className="data-table">
                <thead><tr><th>Run</th><th>Decision</th><th>时间</th></tr></thead>
                <tbody>
                  {(data.audit.gate_decisions || []).map((g, i) => (
                    <tr key={i}>
                      <td><code>{g.run_id}</code></td>
                      <td><code>{JSON.stringify(g.decision).slice(0, 80)}</code></td>
                      <td>{g.created_at ? new Date(g.created_at).toLocaleString() : ''}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
