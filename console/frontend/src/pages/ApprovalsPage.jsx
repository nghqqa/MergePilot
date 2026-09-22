import React, { useMemo, useReducer, useState } from 'react';
import { Link } from 'react-router-dom';
import fixture from '../fixtures/approvals.fixture.json';
import { SUBMIT_PHASE_LABEL, submissionReducer, validateDecision } from '../approvals-model.js';
import { fmtTime } from '../format.js';
import { useAuth } from '../auth.jsx';

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
    ['票据状态', `${t.status}${expired ? '（已过有效期）' : ''}`],
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

// 审批：真实批准/拒绝仅在后端接口与权限就绪时启用；默认显示"未接入"。
// 测试数据模式是明确标注的 fixture 演练，不是审批功能。
export default function ApprovalsPage() {
  const [fixtureMode, setFixtureMode] = useState(false);
  const tickets = useMemo(() => fixture.tickets ?? [], []);
  const auth = useAuth();

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>待审批</h1>
          <p className="page-sub">
            人工安全门的决策入口。当前后端仅有票据存储的落点（SQLite WAL，C-4/P-1），
            只读票视图与决策接口均为提案（C-11）—— 真实审批在接口与权限就绪前不启用。
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
          : '审批服务未接入 —— 无只读票据数据源（C-4 只读视图未实现），无决策接口（C-11 提案）。'}
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
              待后端提供只读票视图（C-4）与决策接口（C-11）后，本页将按服务端权限渲染真实操作；
              操作必须绑定准确的票据 ID、run、head 与幂等版本。
            </div>
          </div>
          <Link className="btn" to="/pending">查看待处理 PR</Link>
        </div>
      )}
    </div>
  );
}
