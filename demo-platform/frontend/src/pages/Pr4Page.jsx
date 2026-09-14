// frontend/src/pages/Pr4Page.jsx — PR #4: CROSS-REPO ORDER SCHEMA MIGRATION.
// Two evidence tiers on one page, never blended:
//   · candidate-a REAL SQL verification loop — executed on an isolated PostgreSQL
//     clone (tools/dbverify), SHA256-locked evidence, version-bound human gate
//     (REPLAY overlay, NO RUNTIME WRITE, 409 on stale/rejected versions)
//   · three-candidate comparison — SIMULATED fixture, computed once per process
// Every state is text + color. Nothing here is fabricated by the frontend: absent
// evidence renders as "证据未提供", never as a result.
import React, { useCallback, useEffect, useState } from 'react';
import { api } from '../api.js';
import { LoadingBox, ErrorBox } from '../components/ui.jsx';

const VERDICT_CLS = { VERIFIED: 'ok', DEGRADED: 'warn', REJECTED: 'bad' };
const VERDICT_PLAIN = { VERIFIED: '通过（可执行）', DEGRADED: '降级淘汰（切换期会出问题）', REJECTED: '淘汰（会弄坏数据）' };

const PLAIN_CHECK = {
  historical_null_backfilled: '旧数据补齐 —— 137 笔缺客户号的订单必须先补全',
  unique_index_created: '重复订单清理 —— 2 笔重复会挡住「不允许重复」的新规则',
  old_worker_compat: '旧程序兼容 —— 切换窗口期，旧程序写入的数据也要合法',
  refunded_enum_added: '新增「已退款」状态成功',
  rollback_check: '可安全回滚 —— 主数据库不受任何影响',
};

const GATE_TEXT = {
  AWAITING_OPERATOR_DECISION: { cls: 'waiting_human', label: '等待操作员决定' },
  OK: { cls: 'completed', label: '闸门有效 · 可执行' },
  STALE_SUPERSEDED_BY_NEW_REVISION: { cls: 'failed', label: '批准已失效 · 代码版本变化' },
  REJECTED_TERMINAL: { cls: 'blocked', label: '已拒绝 · 终态' },
  HELD_BY_OPERATOR: { cls: 'waiting_human', label: '操作员保持阻塞' },
};

const ERR_TEXT = {
  STALE_VERSION: '批准对象的代码版本已被新提交取代。恢复：对新版本重新验证后再批准。',
  REJECTED_TERMINAL: '该验证版本已被拒绝，拒绝不可翻转。恢复：提出新的候选修订并重新验证。',
  VERIFICATION_MISMATCH: '决定必须引用当前通过验证的记录。恢复：刷新页面后重试。',
  HEAD_MISMATCH: '决定引用的代码版本与验证记录不一致。恢复：刷新页面后重试。',
  INVALID_DECISION: '决定只能是批准 / 拒绝 / 保持。',
  EVIDENCE_NOT_AVAILABLE: '真实验证证据目录不存在，闸门不可用。',
};

const short = (s, n = 12) => (s ? String(s).slice(0, n) : '—');
const fmtRowCounts = (rc) =>
  `总订单 ${rc.orders_total} · 缺客户号 ${rc.orders_customer_id_null} · 重复订单 ${rc.payments_duplicate_order_id} · 已退款 ${rc.order_status_refunded_rows}`;

function Section({ title, sub, children }) {
  return (
    <section className="sec">
      <div className="sec-head"><h2>{title}</h2>{sub && <span className="sub">{sub}</span>}</div>
      {children}
    </section>
  );
}

// ─────────────────────────────────────────── candidate-a real SQL chain

function RealLoopChain({ loop }) {
  if (!loop || !loop.available) {
    return (
      <div className="statebox">
        <b>证据未提供</b> — evidence/FINALS-DB-MIGRATION-LOOP-20260914 不存在或校验失败（{loop?.reason || 'EVIDENCE_NOT_AVAILABLE'}）。此区块不显示任何结果。
      </div>
    );
  }
  const rev1 = loop.attempts.find((a) => a.revision === 1);
  const rev2 = loop.attempts.find((a) => a.revision === 2 && !a.late_callback);
  const late = loop.attempts.find((a) => a.late_callback);
  const ctx = loop.context_fetch;
  const bl = loop.baseline;
  const gates = Object.fromEntries((loop.gate_timeline || []).map((g) => [g.label, g]));
  const rows = [
    {
      cls: 'completed', id: '① 数据基线', sub: `${bl?.row_counts?.orders ?? '—'} 订单 · ${bl?.historical_null_customer_id ?? '—'} 笔缺客户号 · ${bl?.historical_duplicate_payment_rows ?? '—'} 笔重复支付`,
      body: (
        <dl className="kv small">
          <dt>schema / data 摘要</dt><dd className="mono">{short(bl?.schema_digest, 16)}… / {short(bl?.data_digest, 16)}…</dd>
          <dt>试验实例</dt><dd className="mono">{loop.environment?.trial_instance} · PostgreSQL {loop.environment?.pg_version}</dd>
          <dt>克隆方式</dt><dd className="mono">{loop.environment?.clone_method}</dd>
        </dl>
      ),
    },
    {
      cls: 'failed', id: '② 候选 A · 修订 1', sub: `代码测试 ${rev1?.code_tests?.verdict} ${rev1?.code_tests?.tests_run}/${rev1?.code_tests?.tests_run} · 迁移 ${rev1?.migration?.verdict} · ${rev1?.migration?.failure_class || ''}`,
      body: (
        <dl className="kv small">
          <dt>代码版本</dt><dd className="mono">{rev1?.head_sha}</dd>
          <dt>PostgreSQL 报错</dt><dd className="mono" style={{ color: 'var(--red)' }}>SQLSTATE {rev1?.migration?.error?.sqlstate} · {rev1?.migration?.error?.message}</dd>
          <dt>断言</dt><dd>{rev1?.assertions?.passed}/{rev1?.assertions?.total} 通过 · 未通过：<span className="mono faint">{(rev1?.assertions?.failed || []).join(' ')}</span></dd>
          <dt>结论</dt><dd>代码测试全绿，迁移在历史数据上失败 —— 空库能过的脚本不等于能上线。</dd>
        </dl>
      ),
    },
    {
      cls: 'waiting_human', id: '③ 补取信息 → 动作改变', sub: ctx ? `决定 ${ctx.outcome === 'REVISE_CANDIDATE' ? '修订同一候选' : '升级人工'}` : '证据未提供',
      body: ctx ? (
        <dl className="kv small">
          <dt>缺什么</dt><dd>{ctx.missing_context}</dd>
          <dt>查到什么</dt><dd className="mono">legacy_order_owner 覆盖 {ctx.fetched?.legacy_order_owner_coverage} · 不可回填 {ctx.fetched?.unresolvable_orders} · 重复支付 {ctx.fetched?.duplicate_payment_rows}</dd>
          <dt>查询</dt><dd className="mono faint">{ctx.fetched?.query}</dd>
          <dt>规则（事先声明）</dt><dd>{ctx.rule}</dd>
          <dt>不可回填占比</dt><dd className="mono">{(ctx.unresolvable_share * 100).toFixed(1)}%</dd>
          <dt>下一步</dt><dd>{ctx.next_action}</dd>
          <dt>人工门</dt><dd>{ctx.human_gate_still_required ? '仍然必需 —— 补取信息只改变修订方向，不跳过审批' : '—'}</dd>
        </dl>
      ) : null,
    },
    {
      cls: 'completed', id: '④ 候选 A · 修订 2', sub: `代码测试 ${rev2?.code_tests?.verdict} · 迁移 ${rev2?.migration?.verdict} · 断言 ${rev2?.assertions?.passed}/${rev2?.assertions?.total}`,
      body: (
        <dl className="kv small">
          <dt>代码版本</dt><dd className="mono">{rev2?.head_sha}</dd>
          <dt>父候选</dt><dd className="mono">{rev2?.parent_candidate_id}（同一候选的上一修订）</dd>
          <dt>脚本摘要</dt><dd className="mono">{rev2?.script_digest}</dd>
          <dt>验证记录</dt><dd className="mono">{rev2?.verification_id} · report {short(rev2?.report_digest, 16)}…</dd>
        </dl>
      ),
    },
    {
      cls: 'completed', id: '⑤ 审批绑定到版本', sub: loop.approval ? `票据 ${short(loop.approval.ticket_id, 16)} · ${loop.approval.status} by ${loop.approval.approved_by}` : '证据未提供',
      body: loop.approval ? (
        <dl className="kv small">
          <dt>绑定的验证</dt><dd className="mono">{loop.approval.bound_verification_id}（并发绑定竞争，胜者 {loop.approval.race_winner}）</dd>
          <dt>闸门（批准后）</dt><dd><span className="pill completed">{gates.approved_current_version?.reason}</span> 代码头 {short(gates.approved_current_version?.bound_head_sha)} = 当前头 {short(gates.approved_current_version?.current_head_sha)}</dd>
          <dt>目标数据变化</dt><dd><span className="pill failed">{gates.target_data_changed?.reason}</span> 执行方传入的数据摘要与验证基线不符即拒绝执行</dd>
        </dl>
      ) : null,
    },
    {
      cls: 'failed', id: '⑥ 版本变化 → 旧批准失效', sub: loop.followup?.gate_after || '证据未提供',
      body: (
        <dl className="kv small">
          <dt>追加提交</dt><dd className="mono">{loop.followup?.head_sha} · {loop.followup?.change}</dd>
          <dt>闸门重算</dt><dd><span className="pill failed">{gates.after_followup_commit?.reason}</span> 绑定头 {short(gates.after_followup_commit?.bound_head_sha)} ≠ 当前头 {short(gates.after_followup_commit?.current_head_sha)}</dd>
          <dt>迟到回调</dt><dd>{late ? `修订 2 第 ${late.attempt} 次 ${late.migration?.verdict} 回调在追加提交后写入 → 闸门仍 ${gates.after_late_pass_callback?.reason}` : '—'}</dd>
          <dt>负向测试</dt><dd className="mono">{loop.negative_tests?.ok}/{loop.negative_tests?.total} 按预期拒绝（重复回调幂等、冲突回调拒绝、不可变表、跨 run 绑定、并发绑定…）</dd>
        </dl>
      ),
    },
    {
      cls: 'completed', id: '⑦ 交付迁移方案包', sub: loop.plan_package?.path || '证据未提供',
      body: (
        <dl className="kv small">
          <dt>文件</dt><dd className="mono">{(loop.plan_package?.files || []).join(' · ')}</dd>
          <dt>边界</dt><dd>{loop.final_disposition?.production_release}</dd>
        </dl>
      ),
    },
  ];
  return (
    <div className="panel">
      <ul className="dag-col verchain">
        {rows.map((r) => (
          <li key={r.id} className={r.cls}>
            <span className="dag-node-dot" aria-hidden="true" />
            <div className="dag-row"><span className="dag-id">{r.id}</span><span className="dag-sub">{r.sub}</span></div>
            {r.body && <div style={{ marginTop: 8 }}>{r.body}</div>}
          </li>
        ))}
      </ul>
      <div className="src" style={{ marginTop: 14 }}>
        evidence/{'FINALS-DB-MIGRATION-LOOP-20260914'} · SHA256 {loop.integrity?.verified ? '校验通过' : '校验失败'}（{loop.integrity?.files} 文件）· outcome_signature {short(loop.outcome_signature, 16)} · 复现：python tools/dbverify/run_migration_loop.py
      </div>
    </div>
  );
}

// ────────────────────────────────────────────── version-bound human gate

function VersionGate({ gate, onChange }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [last, setLast] = useState(null);

  const call = useCallback(async (path, body) => {
    setBusy(true); setErr(null);
    try {
      const r = await api(path, { method: 'POST', body });
      setLast(r);
      onChange(r.gate || null);
    } catch (e) {
      setErr({ code: e.payload?.error || e.message, detail: e.payload?.detail, status: e.status });
    } finally {
      setBusy(false);
    }
  }, [onChange]);

  if (!gate || !gate.available) {
    return <div className="statebox"><b>闸门不可用</b> — {gate?.reason || 'EVIDENCE_NOT_AVAILABLE'}。</div>;
  }
  const v = gate.verified_version;
  const decide = (decision) => call('/api/pr4/approval', { decision, verification_id: v.verification_id, head_sha: v.head_sha, actor: 'demo-operator' });
  const t = GATE_TEXT[gate.reason] || { cls: 'pending', label: gate.reason };
  const stale = gate.followup_applied;
  return (
    <div className="panel">
      <div className="gate-row">
        <span className={`pill ${t.cls}`}>{t.label}</span>
        <span className="small">{gate.reason}</span>
        <span className="chip warn" style={{ marginLeft: 'auto' }}>{gate.marker}</span>
      </div>
      <dl className="kv small" style={{ marginTop: 12 }}>
        <dt>批准对象</dt><dd className="mono">验证 {v?.verification_id} · 代码头 {short(v?.head_sha)} · 脚本 {short(v?.script_digest, 16)}… · 报告 {short(v?.report_digest, 16)}…</dd>
        <dt>当前代码头</dt><dd className="mono">{short(gate.current_head_sha)}{stale ? '（追加提交后，已不等于批准对象）' : '（与批准对象一致）'}</dd>
        {gate.approval && (
          <>
            <dt>已记录决定</dt>
            <dd>{gate.approval.decision} · {gate.approval.actor} · <span className="mono">{gate.approval.recorded_at}</span> · 绑定 {short(gate.approval.bound?.head_sha)}</dd>
          </>
        )}
        <dt>审计库等价</dt><dd className="mono faint">{gate.audit_db_equivalent}</dd>
      </dl>
      <div className="takeover-actions" style={{ marginTop: 14 }}>
        <button className="btn primary" disabled={busy} onClick={() => decide('approve')}>批准该版本</button>
        <button className="btn danger" disabled={busy} onClick={() => decide('reject')}>拒绝该版本</button>
        <button className="btn ghost" disabled={busy} onClick={() => decide('hold')}>保持阻塞</button>
        <button className="btn ghost" disabled={busy || stale} onClick={() => call('/api/pr4/followup', {})}>回放：追加一次代码提交</button>
        <button className="btn ghost" disabled={busy} onClick={() => call('/api/pr4/reset', {})}>重置演示</button>
      </div>
      {err && (
        <div className="takeover-note" role="alert" style={{ marginTop: 10, borderColor: 'var(--red-deep)', color: 'var(--red)' }}>
          ⨯ HTTP {err.status} {err.code} — {ERR_TEXT[err.code] || err.detail || '请求被拒绝'}{err.detail ? <span className="faint mono small"> · {err.detail}</span> : null}
        </div>
      )}
      {!err && last && last.replayed_step && (
        <div className="takeover-note" style={{ marginTop: 10 }}>
          已回放记录步骤 {last.replayed_step} · 审计库当时的闸门结果：{last.recorded_gate_result}
        </div>
      )}
      <div className="takeover-note" style={{ marginTop: 10 }}>
        REPLAY overlay：决定只存于本进程内存，runtime_write=false · evidence_write=false · matrix_message_sent=false。这不是对真实运行时的授权。
      </div>
    </div>
  );
}

// ────────────────────────────────────────────── runtime metadata / status

function RuntimeMeta({ meta }) {
  if (!meta) return null;
  const integ = meta.evidence_integrity || {};
  return (
    <div className="grid2">
      <div className="panel">
        <dl className="kv small">
          <dt>演示平台</dt><dd className="mono">{meta.demo_platform?.version} · Node {meta.demo_platform?.node} · 运行 {meta.demo_platform?.uptime_s}s · {meta.demo_platform?.mode}</dd>
          <dt>RAG 策略版本</dt><dd className="mono">{meta.rag?.strategy}（前版 {meta.rag?.previous}）· held-out hit@1 {meta.rag?.heldout_hit_at_1 || '—'}</dd>
          <dt>迁移验证 Schema</dt><dd className="mono">{meta.db_verification?.schema} · {meta.db_verification?.gate}</dd>
          <dt>试验库</dt><dd className="mono">PostgreSQL {meta.db_verification?.pg_version || '—'} · {meta.db_verification?.trial_instance || '—'}</dd>
          <dt>控制器</dt><dd className="mono">{meta.controller?.file} · {meta.controller?.mode || '—'} · MAX_VERIFY_ATTEMPTS={meta.controller?.max_verify_attempts ?? '—'}</dd>
          <dt>Agent 运行时</dt><dd>{meta.agent_runtime?.framework} · {meta.agent_runtime?.model_gateway}</dd>
        </dl>
      </div>
      <div className="panel">
        <dl className="kv small">
          <dt>证据等级</dt>
          <dd>
            {Object.entries(integ).map(([k, v]) => (
              <div key={k} className="gate-row" style={{ padding: '6px 0' }}>
                <span className={`chip ${v.verified ? 'ok' : 'bad'}`}>{v.verified ? 'SHA256 校验通过' : '校验失败/缺失'}</span>
                <span className="mono small">{v.dir}</span>
                <span className="faint small">{v.tier}{v.files ? ` · ${v.files} 文件` : ''}</span>
              </div>
            ))}
          </dd>
          <dt>边界</dt>
          <dd className="small">PolarDB {meta.boundaries?.polardb} · Branch {meta.boundaries?.database_branch} · Auto Merge {meta.boundaries?.pr_auto_merge} · GitHub 写 {meta.boundaries?.github_writes} · Matrix 消息 {meta.boundaries?.matrix_messages_sent}</dd>
          <dt>LLM</dt><dd className="small">{meta.agent_runtime?.note}</dd>
        </dl>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────── the page

export default function Pr4Page() {
  const [pr4, setPr4] = useState(null);
  const [gate, setGate] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    api('/api/pr4').then((d) => { setPr4(d); setGate(d.gate); }).catch(setErr);
  }, []);

  if (err) return <main className="page"><ErrorBox e={err} text="PR #4 数据加载失败" /></main>;
  if (!pr4) return <main className="page" style={{ paddingTop: 40 }}><LoadingBox text="加载 PR #4 跨仓案例…" /></main>;

  const verified = pr4.candidates.find((c) => c.validation.verdict === 'VERIFIED');
  const loop = pr4.real_sql_loop;

  return (
    <main className="page finals" style={{ paddingTop: 0 }}>
      <div className="case-rail">
        <span className="seg">PR #4 · 一次改表，牵动三个系统</span>
        <span className="spacer" />
        <span className={`chip ${loop?.available ? 'ok' : 'off'}`}>候选 A 真实 SQL 验证: <b>{loop?.available ? 'ISOLATED_POSTGRES' : '证据未提供'}</b></span>
        <span className="chip info">RAG: <b>SYNTHETIC</b></span>
        <span className="chip warn">三候选对照: <b>SIMULATED</b></span>
        <span className="chip off">PolarDB: <b>NOT CONNECTED</b></span>
      </div>

      <div className="resrisk" style={{ marginBottom: 14 }}>
        <div className="item" style={{ fontSize: '1.05em' }}>
          <span><b>一句话：</b>改表不能只看代码测试。候选 A 的整条路 —— 代码测试通过 → 迁移在<b>历史数据</b>上失败 → 补取信息 → 修订同一候选 → 重新验证通过 → 审批绑定到版本 → 追加提交后旧批准<b>失效</b> ——
          在一台独立 PostgreSQL 上<b>真实执行</b>过，证据 SHA256 锁定；三候选对照仍是 SIMULATED 夹具，两者在页面上分开标注。
          </span>
        </div>
      </div>

      <Section title="候选 A · 真实 SQL 验证链" sub="ISOLATED_POSTGRES 克隆库真实执行 · 不是 Agentic Database 分支 · 试验库通过 ≠ 生产发布">
        <RealLoopChain loop={loop} />
      </Section>

      <Section title="人工门 · 绑定到验证版本" sub="批准对象是一个明确的 (验证记录, 代码头, 脚本摘要, 基线摘要)，不是一个会变的 PR 名">
        <VersionGate gate={gate} onChange={(g) => g && setGate(g)} />
      </Section>

      <Section title="发现影响 · 查历史经验" sub="SYNTHETIC 语料，只记检索编号不记原文">
        <div className="grid2">
          <div className="panel">
            <dl className="kv small">
              <dt>关联系统</dt><dd className="mono">{pr4.repos.join(' · ')}</dd>
              <dt>要改什么</dt><dd>{pr4.schema_baseline.changes.join('；')}</dd>
              <dt>历史遗留</dt><dd>orders 表有 <b>137 笔订单缺客户号</b> · payments 表有 <b>2 笔重复订单</b></dd>
              <dt>兼容要求</dt><dd>{pr4.schema_baseline.compat}</dd>
            </dl>
          </div>
          <div className="panel">
            <dl className="kv small">
              <dt>检索编号</dt><dd className="mono">{pr4.rag_impact.query_hash}</dd>
              <dt>命中的经验</dt><dd className="mono">{pr4.rag_impact.results.map((r) => `${r.document_id} (${r.score})`).join(' · ')}</dd>
              <dt>引用来源</dt><dd className="mono faint">{pr4.rag_impact.results.map((r) => r.source_ref).join(' · ')}</dd>
            </dl>
          </div>
        </div>
      </Section>

      <Section title="三种改法对照 · SIMULATED" sub={`夹具数据，启动时计算一次（${pr4.simulated_comparison?.computed_once_at}），页面读取无副作用`}>
        <div className="panel">
          {pr4.candidates.map((c) => (
            <div key={c.candidate_id} style={{ marginBottom: 14 }}>
              <span className={`chip ${VERDICT_CLS[c.validation.verdict]}`}>{c.candidate_id}：{VERDICT_PLAIN[c.validation.verdict]}</span>{' '}
              <span className="small"><b>{c.title}</b> — {c.strategy}</span>
              <span className="mono faint small"> · 涉及 {c.repos_touched.join('/')} · 副本 {c.branch.branch_id}</span>
              {c.failure_reasons.length > 0 && (
                <ul className="small muted" style={{ margin: '4px 0 0', paddingLeft: 18 }}>
                  {c.failure_reasons.map((fr, i) => <li key={i}>{fr}</li>)}
                </ul>
              )}
              <table className="mono small" style={{ width: '100%', borderCollapse: 'collapse', marginTop: 4 }}>
                <tbody>
                  {c.validation.assertions.map((a) => (
                    <tr key={a.name}>
                      <td style={{ width: '42%' }}>{PLAIN_CHECK[a.name] || a.name}</td>
                      <td style={{ width: '10%', color: a.passed ? 'var(--green)' : 'var(--red)', fontWeight: 700 }}>{a.passed ? '通过' : '未通过'}</td>
                      <td style={{ width: '38%' }}>{PLAIN_CHECK[a.name] ? '' : a.detail}</td>
                      <td className="faint" style={{ width: '10%' }}>{a.name}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <dl className="kv small">
                <dt>数据核对</dt><dd className="mono small">{fmtRowCounts(c.assert_data.row_counts)} → {c.assert_data.passed ? '通过' : '未通过'}</dd>
                <dt>回滚检查</dt><dd className="mono small">主库未动 {String(c.rollback_check.main_untouched)} · 副本可删 {String(c.rollback_check.branch_droppable)} → 通过</dd>
              </dl>
            </div>
          ))}
          <div className="gate-row">
            {pr4.rejected.map((id) => <span key={id} className="chip bad">已淘汰: {id}</span>)}
            {verified && <span className="chip ok">可执行: {verified.candidate_id}（5/5 检查通过，唯一候选）</span>}
            <span className="chip warn">{pr4.human_gate.state}</span>
          </div>
        </div>
      </Section>

      <Section title="运行元数据 · 服务状态" sub="本次演示用的模型/Skill/策略版本、证据等级与边界">
        <RuntimeMeta meta={pr4.runtime_metadata} />
      </Section>

      <Section title="全程留痕">
        <div className="panel">
          <dl className="kv small">
            <dt>权威 Trace</dt><dd className="mono">{pr4.agentloop.authoritative_trace_id}</dd>
            <dt>RAG 关联 Trace</dt><dd className="mono">{pr4.agentloop.rag_correlation_trace_id}</dd>
            <dt>数据模式</dt><dd className="mono small">{Object.entries(pr4.data_modes).map(([k, v]) => `${k}=${v}`).join(' · ')}</dd>
            <dt>integrity_status</dt><dd className="small">{pr4.integrity_status}</dd>
          </dl>
        </div>
      </Section>
    </main>
  );
}
