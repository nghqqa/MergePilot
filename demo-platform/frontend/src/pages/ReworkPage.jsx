// frontend/src/pages/ReworkPage.jsx — 返工闭环：verify FAIL → 退回 Fixer → 重修 → PASS.
// Evidence tier: MECHANISM VERIFICATION. What is real: the Workflow Controller's
// process_event code, the PostgreSQL audit chain, the acceptance-test execution
// that decides every VERDICT. What is controlled input: the Reviewer's findings
// and the Fixer's two patches. What is NOT here: Matrix/Element handoff, CoPaw
// containers, LLM calls, GitHub writes. The page says so at the top and never
// upgrades the tier.
import React, { useEffect, useState } from 'react';
import { api } from '../api.js';
import { LoadingBox, ErrorBox } from '../components/ui.jsx';

const short = (s, n = 12) => (s ? String(s).slice(0, n) : '—');

const STAGE_CLS = (s) => {
  if (s.verdict === 'PASS') return 'completed';
  if (s.verdict === 'FAIL' || s.verdict === 'blocked-needs-approval') return 'blocked';
  if (s.status === 'COMPLETED') return 'completed';
  return 'running';
};
const STAGE_LABEL = (s) => {
  const agent = { review: 'Reviewer', fix: 'Fixer', verify: 'Verifier' }[s.stage] || s.agent;
  const v = s.verdict ? ` · VERDICT=${s.verdict}` : '';
  return `${agent} · ${s.stage} #${s.attempt} · ${s.status}${v}`;
};

function Diff({ text }) {
  if (!text) return <div className="statebox">补丁未提供</div>;
  return (
    <pre className="diff" tabIndex={0} aria-label="unified diff">
      {text.split('\n').map((line, i) => {
        const cls = line.startsWith('+') && !line.startsWith('+++') ? 'add'
          : line.startsWith('-') && !line.startsWith('---') ? 'del'
            : line.startsWith('@@') ? 'hunk' : '';
        return <span key={i} className={cls}>{line}{'\n'}</span>;
      })}
    </pre>
  );
}

function Section({ title, sub, children }) {
  return (
    <section className="sec">
      <div className="sec-head"><h2>{title}</h2>{sub && <span className="sub">{sub}</span>}</div>
      {children}
    </section>
  );
}

function Check({ ok, label }) {
  return <span className={`chip ${ok ? 'ok' : 'bad'}`}>{ok ? '通过' : '未通过'} · {label}</span>;
}

export default function ReworkPage() {
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => { api('/api/rework-loop').then(setD).catch(setErr); }, []);

  if (err) return <main className="page"><ErrorBox e={err} text="返工闭环证据加载失败" /></main>;
  if (!d) return <main className="page" style={{ paddingTop: 40 }}><LoadingBox text="加载返工闭环证据…" /></main>;
  if (!d.available) {
    return (
      <main className="page">
        <div className="statebox"><b>证据未提供</b> — evidence/FINALS-REWORK-LOOP-20260914 不存在（{d.reason}）。本页不显示任何结果。</div>
      </main>
    );
  }

  const A = d.scenarios.A_rework_then_pass;
  const B = d.scenarios.B_retry_cap_hold;
  const C = d.scenarios.C_missing_context_blocked_escalation;
  const D = d.scenarios.D_conflicts_and_invalid_inputs;
  const finding = d.risk_basis?.findings?.[0];
  const a1 = A?.attempts?.[0];
  const a2 = A?.attempts?.[1];

  return (
    <main className="page finals" style={{ paddingTop: 0 }}>
      <div className="case-rail">
        <span className="seg">返工闭环 · 验证失败退回重修</span>
        <span className="spacer" />
        <span className="chip info">证据等级: <b>MECHANISM VERIFICATION</b></span>
        <span className="chip ok">真实控制器</span>
        <span className="chip ok">真实 PostgreSQL</span>
        <span className="chip ok">真实测试决定 VERDICT</span>
        <span className="chip warn">Agent 输出: <b>受控输入</b></span>
        <span className="chip off">Element 交接: <b>NOT_EXECUTED</b></span>
      </div>

      <div className="resrisk" style={{ marginBottom: 14 }}>
        <div className="item" style={{ fontSize: '1.05em' }}>
          <span><b>一句话：</b>Verifier 只认验收测试，不认 Fixer 的自述。第一次修复"看起来合理"但测试没过，<b>控制器</b>把任务退回 Fixer 并留下退回原因；第二次修复通过测试，任务才结束。
          这一页的每个状态都来自真实控制器代码写进真实 PostgreSQL 的行；Reviewer 结论和两个补丁是本 harness 提供的受控输入，<b>没有 LLM，也没有 Element 上的真实交接</b>——那是另一等级的证据，本页不替代。
          </span>
        </div>
      </div>

      <Section title="场景 A · 退回一次后通过" sub={`run ${A.run_id} · 最终 ${A.final_task?.status} · ${d.summary?.events_injected} 个事件注入`}>
        <div className="grid2">
          <div className="panel">
            <ul className="dag-col">
              {A.stage_runs.map((s, i) => (
                <li key={i} className={STAGE_CLS(s)}>
                  <span className="dag-node-dot" aria-hidden="true" />
                  <div className="dag-row"><span className="dag-id">{STAGE_LABEL(s)}</span></div>
                  {s.stage === 'verify' && s.attempt === 1 && (
                    <div className="dag-lock">退回原因（dispatch_outbox）：<span className="mono">{a1?.rework_reason}</span></div>
                  )}
                  {s.stage === 'verify' && s.attempt === 1 && (
                    <div className="dag-lock">task_runs 快照：<span className="mono">{a1?.controller_state_after_verify?.status} / {a1?.controller_state_after_verify?.current_stage} / verify_attempt={a1?.controller_state_after_verify?.verify_attempt} / {a1?.controller_state_after_verify?.last_error}</span></div>
                  )}
                </li>
              ))}
            </ul>
          </div>
          <div className="panel">
            <dl className="kv small">
              <dt>风险依据（Reviewer）</dt>
              <dd>{finding?.id} · {finding?.category} · {finding?.severity} · {finding?.risk_level} · <span className="mono">{finding?.file}:{finding?.line}</span><br />{finding?.description}</dd>
              <dt>修复目标（Fixer）</dt><dd>{finding?.suggestion}</dd>
              <dt>验收测试（Verifier 唯一依据）</dt><dd className="mono">{finding?.acceptance_test}</dd>
              <dt>预期行为</dt><dd>{finding?.expected_behavior}</dd>
            </dl>
          </div>
        </div>
      </Section>

      <Section title="尝试 1 · 看似合理，测试不过" sub={`代码树 ${short(a1?.tree_sha)} · ${a1?.tests?.tests_run} 个测试 · ${a1?.tests?.failures} 个失败`}>
        <div className="grid2">
          <div className="panel">
            <dl className="kv small">
              <dt>命令</dt><dd className="mono">{a1?.tests?.command}</dd>
              <dt>失败的测试</dt><dd className="mono" style={{ color: 'var(--red)' }}>{(a1?.tests?.failed_tests || []).join(' ')}</dd>
              <dt>VERDICT</dt><dd><span className="pill failed">FAIL</span> 由测试结果决定，不是预置</dd>
              <dt>控制器动作</dt><dd>建 fix #2（stage_runs UNIQUE CAS）+ outbox「回退修复」+ verify_attempt=1/3</dd>
            </dl>
          </div>
          <div className="panel"><Diff text={d.patches?.attempt1} /></div>
        </div>
      </Section>

      <Section title="尝试 2 · 按 order_id 幂等，测试通过" sub={`代码树 ${short(a2?.tree_sha)} · ${a2?.tests?.tests_run}/${a2?.tests?.tests_run} 通过`}>
        <div className="grid2">
          <div className="panel">
            <dl className="kv small">
              <dt>命令</dt><dd className="mono">{a2?.tests?.command}</dd>
              <dt>VERDICT</dt><dd><span className="pill completed">PASS</span></dd>
              <dt>任务终态</dt><dd className="mono">{a2?.controller_state_after_verify?.status} · verdict={a2?.controller_state_after_verify?.verdict}</dd>
            </dl>
          </div>
          <div className="panel"><Diff text={d.patches?.attempt2} /></div>
        </div>
      </Section>

      <Section title="场景 B / C / D · 上限、上下文不足、非法输入" sub="重试上限转人工 · BLOCKED 退回与升级 · 冲突与重复事件">
        <div className="panel">
          <div className="gate-row">
            <span className="pill failed">B</span>
            <div>
              <b>连续 {B.verdict_sequence?.length} 次 FAIL → {B.final_task?.status} / {B.final_task?.current_stage}</b>
              <div className="small muted">{B.final_task?.last_error} · 序列 {B.verdict_sequence?.map((v) => `${v[0]}→${v[1]}/${v[2]}`).join(' · ')}</div>
              <div style={{ marginTop: 6 }}>{Object.entries(B.checks).map(([k, ok]) => <Check key={k} ok={ok} label={k} />)}</div>
            </div>
          </div>
          <div className="gate-row">
            <span className="pill blocked">C</span>
            <div>
              <b>Verifier 缺验收测试 → VERDICT=BLOCKED → 退回 Fixer → 上限后 {C.final_task?.status}（转人工）</b>
              <div className="small muted">{C.observation}</div>
              <div style={{ marginTop: 6 }}>{Object.entries(C.checks).map(([k, ok]) => <Check key={k} ok={ok} label={k} />)}</div>
            </div>
          </div>
          <div className="gate-row">
            <span className="pill pending">D</span>
            <div>
              <b>非 verifier 发 verify 完成 / 重复 event_id / 缺 VERDICT 快照 / 非 admin 提交</b>
              <table className="mono small" style={{ width: '100%', borderCollapse: 'collapse', marginTop: 6 }}>
                <tbody>
                  {Object.entries(D.observations || {}).map(([k, v]) => (
                    <tr key={k}>
                      <td style={{ width: '34%' }}>{k}</td>
                      <td style={{ width: '46%' }} className="faint">{JSON.stringify({ ...v, ok: undefined })}</td>
                      <td style={{ width: '20%', color: v.ok ? 'var(--green)' : 'var(--red)', fontWeight: 700 }}>{v.ok ? '状态机不动 · 符合预期' : '不符合预期'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="small muted" style={{ marginTop: 6 }}>观察：被忽略的事件在 stage_events 里标为 PROCESSED 而非 ERROR —— 状态机正确，但审计可读性可改进（已记入响应文档，未改控制器）。</div>
            </div>
          </div>
        </div>
      </Section>

      <Section title="事件链 · 任务 / 阶段 / 事件的对应关系" sub="stage_events（event_id ↔ run ↔ stage）· 场景 A">
        <div className="panel">
          <table className="mono small" style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead><tr><th style={{ textAlign: 'left' }}>event_id</th><th style={{ textAlign: 'left' }}>sender</th><th style={{ textAlign: 'left' }}>type</th><th style={{ textAlign: 'left' }}>stage</th><th style={{ textAlign: 'left' }}>status</th></tr></thead>
            <tbody>
              {A.stage_events.map((e) => (
                <tr key={e.event_id}><td>{e.event_id}</td><td>{e.sender}</td><td>{e.event_type}</td><td>{e.stage || '—'}</td><td>{e.status}</td></tr>
              ))}
            </tbody>
          </table>
          <div className="src" style={{ marginTop: 12 }}>
            {d.controller?.file} · {d.controller?.mode} · MAX_VERIFY_ATTEMPTS={d.controller?.max_verify_attempts} · evidence/FINALS-REWORK-LOOP-20260914 SHA256 {d.integrity?.verified ? '校验通过' : '校验失败'} · outcome_signature {short(d.outcome_signature, 16)} · 复现：python tools/agentteams/rework_loop_harness.py
          </div>
        </div>
      </Section>
    </main>
  );
}
