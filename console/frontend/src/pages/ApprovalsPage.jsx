import React, { useEffect, useMemo, useReducer, useState } from 'react';
import { Link } from 'react-router-dom';
import fixture from '../fixtures/approvals.fixture.json';
import { SUBMIT_PHASE_LABEL, submissionReducer, validateDecision } from '../approvals-model.js';
import { fmtTime } from '../format.js';
import { useAuth } from '../auth.jsx';
import { ErrorBox, SkeletonRows } from '../ui.jsx';
import { useAppConfig } from '../App.jsx';
import { useDataSource, useSourceQuery } from '../hooks.js';
import { ticketMap, ticketActionMap, SEVERITY, toneToColor, unknownEntry } from '../status-map.js';

// 决策响应映射（console_pg 0.2.0 test-auth 语义，实测）：
//   200 {ok:true,status[,reason:NOOP]} → 已记录（NOOP=幂等重放：对已处目标态重复决策返回既有状态）
//   409 {ok:false,status,reason:EXPIRED} → 票据过期
//   409 {ok:false,status,reason:INVALID_TRANSITION:*} → 状态已变化（冲突/不可迁移）
//   5xx/网络 → 结果未知：先查询服务端状态，不盲目重发
function decisionEvent(httpStatus, body) {
  if (httpStatus === 200 && body?.ok) return { type: 'RESOLVE', outcome: 'approved', at: new Date().toISOString() };
  if (httpStatus === 409 && body?.reason === 'EXPIRED') return { type: 'EXPIRE' };
  if (httpStatus === 409) return { type: 'CONFLICT' };
  return { type: 'TIMEOUT' };
}

function queryOutcomeOf(statusStr) {
  const s = String(statusStr ?? '').toUpperCase();
  if (s === 'APPROVED') return 'approved';
  if (s === 'REJECTED') return 'rejected';
  if (s === 'EXPIRED') return 'expired';
  return 'conflict';
}

// fixture 模拟提交：按票据的 _outcome 演练对应路径（隔离内存变换，零真实写操作）。
function simulateSubmit(t, decision) {
  void decision; // 决策值随 SUBMIT 已入状态机；fixture 的结果路径由 _outcome 决定
  return new Promise((resolve) => setTimeout(() => resolve(t._outcome), 700));
}
const outcomeEvent = (outcome) => ({
  approved: { type: 'RESOLVE', outcome: 'approved' },
  rejected: { type: 'RESOLVE', outcome: 'rejected' },
  conflict: { type: 'CONFLICT' },
  expired: { type: 'EXPIRE' },
  timeout_then_approved: { type: 'TIMEOUT' },
}[outcome]);

function TicketCard({ t, authed }) {
  const [open, setOpen] = useState(false);
  const [state, dispatch] = useReducer(submissionReducer, { phase: 'idle' });
  const expired = t.expires_at && Date.parse(t.expires_at) <= Date.now();
  // head 一致性预检：fixture 提供假设的当前头，仅用于演练（真实权威在服务端，C-10）
  const precheck = validateDecision(t, {
    decision: state.decision ?? 'APPROVED',
    now: Date.now(),
    assumeCurrentHead: t._assume_current_head ?? null,
  });

  const submit = async (decision) => {
    dispatch({ type: 'SUBMIT', decision });
    const outcome = await simulateSubmit(t, decision);
    dispatch(outcomeEvent(outcome));
  };

  const binding = [
    ['动作', t.action_label ?? t.action],
    ['仓库', t.repo],
    ['PR', `#${t.pr_number}`],
    ['head', <code key="h" className="mono">{t.head_sha.slice(0, 12)}</code>],
    ['run', t.run_id ?? '未记录'],
    ['参数', <code key="p" className="mono">{JSON.stringify(t.params)}</code>],
    ['有效期至', fmtTime(t.expires_at) ?? '未记录'],
    // rc.10：票据状态走 status-map TICKET 词表（与筛选器文案一致），raw 枚举入 title
    ['票据状态', <span key="ts" title={`approval status=${t.status}｜${ticketMap(t.status).note}`}>{ticketMap(t.status).label}{expired ? '（已过有效期）' : ''}</span>],
    ['审批人', authed ? '当前登录用户' : '未认证 —— 演示预览下无法确认审批人身份'],
  ];

  return (
    <div className="panel ticket-card">
      <button type="button" className="ticket-head" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        <span className="mono">{t.id}</span>
        <span className="muted">{t.action_label} · {t.repo} #{t.pr_number} · head {t.head_sha.slice(0, 8)}</span>
        <span className="chip">{SUBMIT_PHASE_LABEL[state.phase]}</span>
      </button>
      {open ? (
        <div className="ticket-body">
          <p className="section-note">{t.summary}</p>
          <dl className="kv-grid ticket-binding">
            {binding.map(([k, v]) => (
              <div key={k} className="kv"><div className="kv-label">{k}</div><div className="kv-value">{v}</div></div>
            ))}
          </dl>
          {state.phase === 'idle' && !precheck.ok ? (
            <div className="state-box state-warn" role="status">
              预检不通过（{precheck.code}）：{precheck.message}
              {precheck.code === 'HEAD_CONFLICT' ? ' — 真实场景应刷新服务端权威状态后重签票据。' : ''}
            </div>
          ) : null}
          {state.phase === 'unknown' ? (
            <div className="state-box state-warn" role="status">
              提交超时，结果未知。不得盲目重发 —— 先查询服务端实际结果。
              <button
                type="button" className="btn btn-sm"
                onClick={() => dispatch({ type: 'QUERY', outcome: 'approved' })}
              >
                查询票据状态
              </button>
            </div>
          ) : null}
          {state.phase === 'decided' ? (
            <div className="state-box state-ok" role="status">
              已记录：{state.outcome === 'approved' ? '批准' : '拒绝'}{state.via === 'query' ? '（经查询确认）' : ''} · fixture 内存结果，非真实审批。
            </div>
          ) : null}
          {state.phase === 'conflict' ? (
            <div className="state-box state-warn" role="status">head 冲突：票据绑定旧 head。刷新权威状态后由后端重签。
              <button type="button" className="btn btn-sm" onClick={() => dispatch({ type: 'REFRESH' })}>刷新状态</button>
            </div>
          ) : null}
          {state.phase === 'expired' ? (
            <div className="state-box state-warn" role="status">票据过期：需后端重签，控制台不续期。
              <button type="button" className="btn btn-sm" onClick={() => dispatch({ type: 'REFRESH' })}>刷新状态</button>
            </div>
          ) : null}
          <div className="ticket-actions">
            <button
              type="button" className="btn btn-primary" disabled={state.phase !== 'idle' || !precheck.ok}
              onClick={() => submit('APPROVED')}
            >
              {state.phase === 'submitting' && state.decision === 'APPROVED' ? '提交中…' : '批准'}
            </button>
            <button
              type="button" className="btn" disabled={state.phase !== 'idle' || !precheck.ok}
              onClick={() => submit('REJECTED')}
            >
              {state.phase === 'submitting' && state.decision === 'REJECTED' ? '提交中…' : '拒绝'}
            </button>
            {state.phase === 'idle' && !precheck.ok ? (
              <span className="muted">预检不通过，操作禁用（真实场景由服务端最终校验）。</span>
            ) : (
              <span className="muted">测试数据模式：操作仅作用于合成票据 FIXTURE-*，不产生真实审批。</span>
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}

// ---- 真实 test-auth 审批（隔离联调 PG fixture 库，HTTP→PG；与上面的内存 fixture 演练彻底分离） ----

function PgQueryButton({ source, ticketId, dispatch }) {
  const [busy, setBusy] = useState(false);
  const query = async () => {
    setBusy(true);
    try {
      const d = await source.getApproval(ticketId);
      dispatch({ type: 'QUERY', outcome: queryOutcomeOf(d?.status) });
    } catch {
      dispatch({ type: 'QUERY', outcome: 'conflict' });
    } finally {
      setBusy(false);
    }
  };
  return <button type="button" className="btn btn-sm" disabled={busy} onClick={query}>{busy ? '查询中…' : '查询票据状态'}</button>;
}

function PgTicketRow({ t, source }) {
  const [open, setOpen] = useState(false);
  const [state, dispatch] = useReducer(submissionReducer, { phase: 'idle' });

  const submit = async (decision) => {
    dispatch({ type: 'SUBMIT', decision });
    try {
      const { httpStatus, body } = await source.decideApproval(t.ticket_id, decision);
      dispatch(decisionEvent(httpStatus, body));
    } catch {
      dispatch({ type: 'TIMEOUT' }); // 网络/服务异常 → 结果未知，先查询不重发
    }
  };

  return (
    <div className="panel ticket-card">
      <button type="button" className="ticket-head" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        <span className="mono">{t.ticket_id.slice(0, 18)}…</span>
        <span className="muted">{ticketActionMap(t.action).label} · {t.repo} · head {(t.head_sha ?? '').slice(0, 8)}</span>
        <span className="chip">{ticketMap(t.status).label}{state.phase !== 'idle' ? ` → ${SUBMIT_PHASE_LABEL[state.phase]}` : ''}</span>
      </button>
      {open ? (
        <div className="ticket-body">
          <dl className="kv-grid ticket-binding">
            <div className="kv"><div className="kv-label">动作</div><div className="kv-value" title={ticketActionMap(t.action).note}>{ticketActionMap(t.action).label}<span className="muted">（{t.action}）</span></div></div>
            <div className="kv"><div className="kv-label">仓库</div><div className="kv-value mono">{t.repo}</div></div>
            <div className="kv"><div className="kv-label">run_id</div><div className="kv-value mono">{t.run_id ?? '未记录'}</div></div>
            <div className="kv"><div className="kv-label" title="票据绑定的完整 head SHA——决策对象以它为准">绑定 head</div><div className="kv-value mono">{t.head_sha ?? '未记录'}</div></div>
            <div className="kv"><div className="kv-label">finding</div><div className="kv-value mono">{t.finding_id ?? '未记录（run 级动作）'}</div></div>
            <div className="kv"><div className="kv-label">有效期（TTL）</div><div className="kv-value">后端响应未携带 expires_at——暂无法显示具体到期时间，过期与否以服务端判定为准</div></div>
            <div className="kv"><div className="kv-label">审批人</div><div className="kv-value">test-principal（隔离联调主体，非真实 GitHub 身份）</div></div>
          </dl>
          {state.phase === 'decided' ? (
            <div className="state-box state-ok" role="status">
              已记录：{state.outcome === 'approved' ? '批准' : '拒绝'}
              {state.via === 'query' ? '（经查询确认）' : ''} —— 后端语义：批准只生成后续动作意图，
              不代表补丁已生成、PR 已修改或已合并。
            </div>
          ) : null}
          {state.phase === 'expired' ? (
            <div className="state-box state-warn" role="status">票据已过期（后端 409 EXPIRED）——需后端重签，控制台不续期。
              <PgQueryButton source={source} ticketId={t.ticket_id} dispatch={dispatch} />
            </div>
          ) : null}
          {state.phase === 'conflict' ? (
            <div className="state-box state-warn" role="status">
              决策未生效（409：{state.reason ?? '状态已变化'}）——以服务端权威状态为准。
              <button type="button" className="btn btn-sm" onClick={() => dispatch({ type: 'REFRESH' })}>刷新状态</button>
            </div>
          ) : null}
          {state.phase === 'unknown' ? (
            <div className="state-box state-warn" role="status">
              提交结果未知——先查询服务端实际状态，不盲目重发。
              <PgQueryButton source={source} ticketId={t.ticket_id} dispatch={dispatch} />
            </div>
          ) : null}
          {t.status === 'PENDING' ? (
            <div className="ticket-actions">
              <button type="button" className="btn btn-primary" disabled={state.phase !== 'idle'}
                onClick={() => submit('APPROVED')}>
                {state.phase === 'submitting' && state.decision === 'APPROVED' ? '提交中…' : '批准'}
              </button>
              <button type="button" className="btn" disabled={state.phase !== 'idle'}
                onClick={() => submit('REJECTED')}>
                {state.phase === 'submitting' && state.decision === 'REJECTED' ? '提交中…' : '拒绝'}
              </button>
              <span className="muted">隔离联调（PG fixture）· data_mode=fixture · test-auth 主体——非真实 GitHub 操作</span>
            </div>
          ) : (
            <p className="section-note">该票据不在待审批状态（{ticketMap(t.status).label} / {t.status}）——决策端点仅对 PENDING 生效。</p>
          )}
        </div>
      ) : null}
    </div>
  );
}

function PgApprovalsSection({ source }) {
  const [attempt, setAttempt] = useState(0);
  const listQ = useSourceQuery(() => source.listApprovals(), [source, attempt]);
  return (
    <div>
      <div className="state-box state-warn" role="status">
        Test-auth 审批联调：真实 HTTP → 隔离 PG（data_mode=fixture）。测试主体 = test-principal
        （非真实 GitHub 身份、非生产账户）；批准/拒绝只作用于隔离 fixture 库中的票据，
        不产生任何真实 GitHub 写入；批准 = 后续动作意图，不代表补丁已生成、PR 已修改或已合并。
      </div>
      {listQ.status === 'error' ? (
        listQ.error?.status === 501 ? (
          <div className="state-box state-empty" role="status">
            后端未配置审批存储（501 not_implemented）——无法列出票据。
          </div>
        ) : listQ.error?.status === 401 ? (
          <div className="state-box state-error" role="alert">未认证（401）——审批接口要求有效会话。</div>
        ) : (
          <ErrorBox error={listQ.error} onRetry={() => setAttempt((n) => n + 1)} />
        )
      ) : listQ.status !== 'done' ? (
        <SkeletonRows rows={4} cols={4} />
      ) : (
        <>
          <div className="table-meta">
            {listQ.data?.items?.length ?? 0} 张待审批票据（PENDING，存储于隔离联调数据库）
            —— 后端响应暂未携带有效期（TTL）字段，控制台暂无法显示剩余时间。
          </div>
          {(listQ.data?.items ?? []).length === 0 ? (
            <div className="empty-note" style={{ padding: '24px 0', color: '#888', textAlign: 'center' }}>
              当前没有待审批票据（诚实零值——PENDING 状态的审批票记录为零）
            </div>
          ) : null}
          {(listQ.data?.items ?? []).map((t) => (
            <PgTicketRow key={t.ticket_id} t={t} source={source} />
          ))}
        </>
      )}
    </div>
  );
}

// ---- 页面入口：console-pg 联调源 → 真实 test-auth 审批（隔离 PG）；其余 → 未接入 + fixture 演练 ----
// 两条路径彻底分离：真实票据适配器（data/sources.js consolePgSource.decideApproval，X-Test-Principal
// 隔离主体）与内存 fixture 演练（approvals-model 纯状态机）互不共享状态。
// ── MU 模式（v16 高危修复审批票）：/api/mu/approvals 直读（服务端 tenant 收窄）──
// 读取结果四态分离（PR-2）：401 登录过期 / 403 无权限 / 网络失败（显式错误块+重试钮）
// / 真空态（诚实零值）。网络失败绝不渲染成"没有审批票"。状态列与筛选下拉走
// status-map.js TICKET 词表的中文文案（enum 值仅作请求参数与 title 技术详情）。
function MuApprovals() {
  const [rows, setRows] = useState(null);
  const [statusFilter, setStatusFilter] = useState('');
  const [err, setErr] = useState(null);
  const [attempt, setAttempt] = useState(0); // 重试只重新触发既有 load，不改请求契约
  useEffect(() => {
    let dead = false;
    setErr(null);
    fetch(`/api/mu/approvals${statusFilter ? `?status=${statusFilter}` : ''}`, { credentials: 'same-origin' })
      .then(async (r) => {
        if (r.status === 401) { if (!dead) { setRows([]); setErr('unauthorized'); } return; }
        if (r.status === 403) { if (!dead) { setRows([]); setErr('forbidden'); } return; }
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const b = await r.json().catch(() => null);
        if (!dead) setRows(b?.approvals ?? []);
      })
      .catch(() => { if (!dead) { setRows([]); setErr('network'); } });
    return () => { dead = true; };
  }, [statusFilter, attempt]);
  const STATUS = ['', 'PENDING', 'APPROVED', 'REJECTED', 'EXPIRED', 'STALE', 'CONSUMED'];
  return (
    <div>
      <div className="page-head">
        <div>
          <h1>待审批</h1>
          <p className="page-sub">
            高危修复审批票（P0/P1 逐条）——批准仅允许生成 DRY_RUN 修复建议：不写 GitHub、
            不自动合并、branch protection 保持有效。操作入口在对应 PR 详情页。
          </p>
        </div>
        <select aria-label="按状态筛选" value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          style={{ minWidth: 160 }}>
          {STATUS.map((sv) => (
            <option key={sv || 'all'} value={sv}>{sv ? ticketMap(sv).label : '全部状态'}</option>
          ))}
        </select>
      </div>
      {err === 'unauthorized' ? (
        <div className="panel approvals-entry">
          <div>
            <strong>登录已过期——请刷新页面重新登录。</strong>
            <div className="muted">审批票读取需要有效会话（401 如实），不显示任何占位数据。</div>
          </div>
        </div>
      ) : err === 'forbidden' ? (
        <div className="panel approvals-entry"><div className="muted">当前角色无审批票读取权限（403 如实）。</div></div>
      ) : err === 'network' ? (
        <div className="panel approvals-entry">
          <div>
            <strong>审批票读取失败（网络或服务不可用）。</strong>
            <div className="muted">
              未取得任何审批票数据——这不代表当前没有审批票；持续失败请联系管理员检查服务状态。
            </div>
            <button type="button" className="btn btn-sm" style={{ marginTop: 8 }}
              onClick={() => setAttempt((n) => n + 1)}>重试</button>
          </div>
        </div>
      ) : rows === null ? (
        <div className="muted">读取审批票…</div>
      ) : rows.length === 0 ? (
        <div className="panel approvals-entry">
          <div>
            <strong>当前没有审批票（诚实零值）。</strong>
            <div className="muted">
              P0/P1 审查结果出现时，此处与「待处理」页会列出待办；P2/P3 不产生审批票（人工裁量入口在 PR 详情）。
            </div>
          </div>
        </div>
      ) : (
        <div className="table-meta">{rows.length} 张审批票 · 状态 {statusFilter ? ticketMap(statusFilter).label : '全部'}</div>
      )}
      {rows && rows.length > 0 ? (
        <table className="qf-table" style={{ width: '100%', fontSize: 13 }}>
          <thead><tr>
            <th>级别</th><th>仓库 / PR</th><th>发现</th><th>head</th><th>状态</th><th>决定人 / 过期</th><th>run</th><th></th>
          </tr></thead>
          <tbody>
            {rows.map((t) => {
              const sev = SEVERITY[String(t.severity ?? '').toUpperCase()] ?? unknownEntry(t.severity);
              const sm = ticketMap(t.status);
              return (
                <tr key={t.approval_id}>
                  <td><span className={`sev-badge sev-${t.severity}`} title={sev.note}>{sev.label}</span></td>
                  <td className="mono">{t.repo_owner}/{t.repo_name} #{t.pr_number}</td>
                  <td className="mono" style={{ maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                    {t.rule_id} · {t.path}{t.line_start ? `:${t.line_start}` : ''}
                    {t.summary_masked ? <span className="muted">（{t.summary_masked}）</span> : null}
                  </td>
                  <td className="mono">{String(t.head_sha ?? '').slice(0, 8)}</td>
                  <td><span className={`tag tag-${toneToColor(sm.tone)}`} title={sm.note}>{sm.label}</span></td>
                  <td style={{ fontSize: 12 }}>
                    {t.decided_by ? `${t.decided_by} · ${String(t.decided_at ?? '').slice(0, 16).replace('T', ' ')}` : `过期 ${String(t.expires_at ?? '').slice(0, 16).replace('T', ' ')}`}
                  </td>
                  <td className="mono" style={{ fontSize: 12 }}>{String(t.run_id ?? '').slice(0, 8)}</td>
                  <td>
                    <Link className="btn btn-sm" to={`/mu/repos/${encodeURIComponent(t.repo_owner)}/${encodeURIComponent(t.repo_name)}/pr/${t.pr_number}`}>打开 PR</Link>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : null}
    </div>
  );
}

export default function ApprovalsPage({ mode = 'auto' }) {
  const config = useAppConfig();
  const { source } = useDataSource(config);
  const resolved = mode === 'auto' ? (source.kind === 'console-pg' ? 'pg' : (source.kind === 'multiuser' ? 'mu' : 'legacy')) : mode;
  if (resolved === 'mu') return <MuApprovals />;
  const [fixtureMode, setFixtureMode] = useState(false);
  const tickets = useMemo(() => fixture.tickets ?? [], []);
  const auth = useAuth();

  if (resolved === 'pg') {
    return (
      <div>
        <div className="page-head">
          <div>
            <h1>待审批</h1>
            <p className="page-sub">
              人工安全门决策入口（隔离 test-auth 联调）。后端 0.2.0 已提供审批只读与决策端点；
              主体为 X-Test-Principal 测试 principal（非真实 GitHub 身份），数据为隔离 PG fixture。
            </p>
          </div>
          <div className="detail-actions">
            <button
              type="button"
              className={`qf-chip${fixtureMode ? ' qf-active' : ''}`}
              aria-pressed={fixtureMode}
              onClick={() => setFixtureMode((v) => !v)}
              title="内存合成票据演练（FIXTURE-*），与本节真实 HTTP→PG 联调彻底分离"
            >
              合成票据演练{fixtureMode ? '：开' : '：关'}
            </button>
          </div>
        </div>

        <PgApprovalsSection source={source} />

        {fixtureMode ? (
          <>
            <div className="state-box state-warn" role="status">
              合成票据演练：以下交互在本页内存中进行，不触达任何后端或数据库（与上方真实联调无关）。
            </div>
            <div className="table-meta">{tickets.length} 张合成票据 · 覆盖 正常 / head 冲突 / 过期 / 超时查证 四条路径</div>
            {tickets.map((t) => <TicketCard key={t.id} t={t} />)}
          </>
        ) : null}
      </div>
    );
  }

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>待审批</h1>
          <p className="page-sub">
            人工安全门的决策入口。当前部署的审批服务仅提供审批票的存储与审计留痕（可追溯），
            只读票视图与决策接口尚未接入——真实审批在接口与权限就绪前不启用。
          </p>
        </div>
        <div className="detail-actions">
          <button
            type="button"
            className={`qf-chip${fixtureMode ? ' qf-active' : ''}`}
            aria-pressed={fixtureMode}
            onClick={() => setFixtureMode((v) => !v)}
          >
            测试数据模式{fixtureMode ? '：开' : '：关'}
          </button>
        </div>
      </div>

      <div className={`state-box ${fixtureMode ? 'state-warn' : 'state-empty'}`} role="status">
        {fixtureMode
          ? '测试数据模式：以下为合成票据（FIXTURE-*），交互全链路在本页内存中演练，不触达任何后端或数据库。'
          : '审批服务未接入 —— 暂无审批票只读数据源，也无决策接口；本页不显示真实待审批项。'}
      </div>

      {fixtureMode ? (
        <>
          <div className="table-meta">{tickets.length} 张合成票据 · 覆盖 正常 / head 冲突 / 过期 / 超时查证 四条路径</div>
          {tickets.map((t) => <TicketCard key={t.id} t={t} authed={auth.status === 'authed'} />)}
        </>
      ) : (
        <div className="panel approvals-entry">
          <div>
            <strong>这里不会出现假的「批准」按钮。</strong>
            <div className="muted">
              待后端提供只读票视图与决策接口后，本页将按服务端权限渲染真实操作；
              操作必须绑定准确的票据 ID、run、head 与幂等版本。
            </div>
          </div>
          <Link className="btn" to="/pending">查看待处理 PR</Link>
        </div>
      )}
    </div>
  );
}
