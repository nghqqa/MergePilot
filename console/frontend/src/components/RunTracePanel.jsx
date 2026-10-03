// RunTracePanel — 运行详情"Skill/RAG 调用留痕"（C 波 C2）。
// 数据域：MU live 域（/api/mu/runs/:runId/...，会话内租户收窄、服务端 RBAC），
// 挂载在审查管线面板（PipelinePanel）的 ready 运行之下——MU 用户从真实运行到达本区域。
//
// 红线：
// - 只渲染契约白名单字段：响应对象先经 pickFields() 白名单投影再进 DOM，
//   契约之外的任何键（如 prompt/响应/代码正文）即使被串入也永不渲染；
// - 不伪造：legacy 运行（not_available=true）如实显示不可用（不冒充 v2 数据）；
//   真实空集如实显示空；未知 status/invocation_kind 原值兜底显示（不吞机器值）；
// - 留痕语义边界：调用记录 ≠ 审查通过/测试通过/合并资格；Skill 已激活 ≠ 必然被调用；
// - 绝不显示 RAG query 原文与文档正文（契约只有 digest 与计数，UI 无展示原文的口子）。
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Badge, SkeletonRows } from '../ui.jsx';
import { runTraceMap, runTraceKindMap } from '../status-map.js';
import { fmtTime } from '../format.js';

const TRACE_LIMIT = 200;

// 契约白名单（与 C1 逐字对齐；skill 与 rag 的基础字段一致，rag 另含三个检索字段）。
const SKILL_FIELDS = ['event_id', 'agent_role', 'skill_key', 'skill_version', 'invocation_kind',
  'status', 'started_at', 'completed_at', 'latency_ms', 'input_digest', 'output_digest', 'error_code'];
const RAG_FIELDS = ['event_id', 'agent_role', 'skill_key', 'skill_version', 'invocation_kind',
  'status', 'started_at', 'completed_at', 'latency_ms', 'query_digest', 'result_count',
  'source_digest_list', 'error_code'];

// 白名单投影：仅复制声明过的键（且值非 undefined）——多出来的键到不了渲染层。
function pickFields(obj, keys) {
  const out = {};
  if (!obj || typeof obj !== 'object') return out;
  for (const k of keys) {
    if (obj[k] !== undefined) out[k] = obj[k];
  }
  return out;
}

// Agent 角色人话标签（与审查管线面板既有 ROLE_LABEL 口径一致；未知角色原值显示）。
const ROLE_LABEL = {
  leader: 'Leader（裁定）',
  reviewer: 'Reviewer（审查）',
  fixer: 'Fixer（修复建议）',
  verifier: 'Verifier（独立验证）',
};
const roleLabel = (r) => ROLE_LABEL[r] ?? (r == null || r === '' ? '—' : String(r));

// latency_ms → 人话耗时（契约数值毫秒；缺失/非数值如实 '—'，不估算）。
function fmtLatency(ms) {
  const n = Number(ms);
  if (ms == null || ms === '' || !Number.isFinite(n) || n < 0) return '—';
  if (n < 1000) return `${n} ms`;
  if (n < 60 * 1000) return `${(n / 1000).toFixed(1)} 秒`;
  const m = Math.floor(n / 60000);
  const s = Math.round((n % 60000) / 1000);
  return s ? `${m} 分 ${s} 秒` : `${m} 分`;
}

// 截断摘要：前 12 位 + title 全文（复制省略——title 已携带全文，保持表格轻量）。
function Digest({ value }) {
  if (!value) return <span>—</span>;
  const s = String(value);
  return <code className="trace-digest" title={s}>{s.slice(0, 12)}</code>;
}

// 摘要列表（RAG 来源 digest / 稳定 ID）：逐条截断 + title 全文；空数组 '—'。
function DigestList({ values }) {
  const list = Array.isArray(values) ? values : [];
  if (!list.length) return <span>—</span>;
  return (
    <span className="trace-digest-list">
      {list.map((d, i) => <Digest key={i} value={d} />)}
    </span>
  );
}

// 错误码：等宽截断 + title 全文（长错误码不破坏布局）。
function ErrorCode({ value }) {
  if (!value) return <span>—</span>;
  return <code className="trace-ellipsis trace-code" title={String(value)}>{String(value)}</code>;
}

function StatusBadge({ status }) {
  const m = runTraceMap(status);
  return <Badge tone={m.tone} title={m.note}>{m.label}</Badge>;
}

function KindText({ kind }) {
  const label = runTraceKindMap(kind);
  return <span title={label ?? ''}>{label ?? '—'}</span>;
}

// 三段语义边界文案（逐字，渲染于留痕区域所有有内容状态）。
function TraceDisclosures() {
  return (
    <ul className="trace-notes">
      <li>仅显示调用元数据与摘要，不包含 prompt、响应、代码正文或 RAG 文档正文。</li>
      <li>调用记录不等于审查通过、测试通过或合并资格。</li>
      <li>Skill 已激活不代表本次运行一定调用了该 Skill。</li>
    </ul>
  );
}

// 状态计数行：按 by_status（缺失时从 items 推导）逐状态渲染"标签 × 数量"。
// 未知状态走 runTraceMap 兜底（原值 + 未知状态 title）。
function countsByStatus(items) {
  const by = {};
  for (const it of items ?? []) {
    const k = String(it?.status ?? 'UNKNOWN');
    by[k] = (by[k] ?? 0) + 1;
  }
  return by;
}

function StatusCounts({ byStatus }) {
  const entries = Object.entries(byStatus ?? {});
  if (!entries.length) return <span className="muted">—</span>;
  return (
    <span className="trace-status-counts">
      {entries.map(([k, n]) => {
        const m = runTraceMap(k);
        return <span key={k} className="trace-count-chip" title={m.note}>{m.label}×{n}</span>;
      })}
    </span>
  );
}

function RoleCounts({ byRole }) {
  const entries = Object.entries(byRole ?? {}).filter(([, n]) => Number(n) > 0);
  if (!entries.length) return <span className="muted">—</span>;
  return (
    <span className="trace-role-counts">
      {entries.map(([k, n]) => (
        <span key={k} className="trace-count-chip" title={`Agent 角色 ${roleLabel(k)} 的调用次数`}>
          {roleLabel(k)}×{n}
        </span>
      ))}
    </span>
  );
}

// ── 主面板 ──
// phase: loading | ready | legacy | unauthorized(401) | forbidden(403) | gone(404) | error
// 数据三端点并发读取；403 > 401 > 404 > 传输错误 > legacy/ready 的呈现优先级：
// 任一端点 403/401 时整块降级为对应文案（授权以服务端为准，不展示半份数据冒充全量）。
export function RunTracePanel({ runId }) {
  const [phase, setPhase] = useState('loading');
  const [errorDetail, setErrorDetail] = useState(null);
  const [data, setData] = useState(null);
  const seqRef = useRef(0); // 竞态守卫：runId 切换/卸载后晚到的响应作废

  const load = useCallback(async () => {
    if (!runId) { setPhase('gone'); return; }
    const seq = ++seqRef.current;
    const alive = () => seqRef.current === seq;
    setPhase('loading');
    setErrorDetail(null);
    const get = async (path) => {
      const res = await fetch(`/api/mu/runs/${encodeURIComponent(runId)}/${path}?limit=${TRACE_LIMIT}&offset=0`,
        { credentials: 'same-origin' });
      const body = await res.json().catch(() => null);
      return { status: res.status, body };
    };
    try {
      const [skill, rag, summary] = await Promise.all([get('skill-invocations'), get('rag-retrievals'), get('call-summary')]);
      if (!alive()) return;
      const statuses = [skill.status, rag.status, summary.status];
      if (statuses.includes(403)) { setPhase('forbidden'); return; }
      if (statuses.includes(401)) { setPhase('unauthorized'); return; }
      if (statuses.includes(404)) { setPhase('gone'); return; }
      if (statuses.some((s) => s < 200 || s >= 300)) {
        setPhase('error');
        setErrorDetail(`HTTP ${statuses.filter((s) => s < 200 || s >= 300).join(' / ')}`);
        return;
      }
      // legacy 运行：not_available=true（或 run.legacy=true）——如实显示不可用，不冒充 v2 数据
      const legacy = skill.body?.not_available === true || rag.body?.not_available === true
        || skill.body?.run?.legacy === true || rag.body?.run?.legacy === true;
      if (legacy) { setPhase('legacy'); return; }
      setData({
        skill: (skill.body?.items ?? []).map((it) => pickFields(it, SKILL_FIELDS)),
        skillTotal: Number(skill.body?.total ?? 0),
        rag: (rag.body?.items ?? []).map((it) => pickFields(it, RAG_FIELDS)),
        ragTotal: Number(rag.body?.total ?? 0),
        summary: summary.body ?? null,
      });
      setPhase('ready');
    } catch (e) {
      if (alive()) { setPhase('error'); setErrorDetail('网络错误'); }
    }
  }, [runId]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => () => { seqRef.current += 1; }, []); // 卸载后晚到响应作废

  // aria-live 反馈（视觉隐藏）：状态变化对读屏可感知
  const announce = {
    loading: '正在读取 Skill 与 RAG 调用留痕',
    ready: `调用留痕已加载：Skill ${data?.skillTotal ?? 0} 条，RAG ${data?.ragTotal ?? 0} 条`,
    legacy: '该运行产生于 v2 管线之前，调用留痕不可用',
    forbidden: '当前角色无权查看调用留痕',
    unauthorized: '登录状态已失效，无法查看调用留痕',
    gone: '该运行不存在或不在当前组织范围内',
    error: '调用留痕读取失败',
  }[phase] ?? '';

  return (
    <section className="section trace-section" aria-label="Skill 与 RAG 调用留痕">
      <div className="section-head">
        <h3>Skill / RAG 调用留痕</h3>
      </div>
      <p className="section-note">
        来自本次运行（<code className="mono">{runId}</code>）的调用元数据留痕；仅显示元数据与摘要，不含任何正文。
      </p>
      <span className="mu-visually-hidden" role="status" aria-live="polite">{announce}</span>

      {phase === 'loading' ? (
        <div role="status" aria-label="调用留痕加载中">
          <p className="section-note">正在读取调用留痕…</p>
          <SkeletonRows rows={4} cols={6} />
        </div>
      ) : null}

      {phase === 'forbidden' ? (
        <div className="state-box state-warn" role="status">
          当前角色无权查看调用留痕（403）——以服务端授权为准，可联系管理员调整角色后重试。
        </div>
      ) : null}

      {phase === 'unauthorized' ? (
        <div className="state-box state-warn" role="status">
          登录状态已失效（401）——请重新登录后再查看调用留痕。
        </div>
      ) : null}

      {phase === 'gone' ? (
        <div className="state-box state-warn" role="status">
          该运行不存在或不在当前组织范围内（404）——跨租户运行不可见。
        </div>
      ) : null}

      {phase === 'error' ? (
        <div className="state-box state-error" role="alert">
          <div>
            <strong>调用留痕读取失败{errorDetail ? `（${errorDetail}）` : ''}</strong>
            <div>网络或服务暂时不可用——可重试；持续失败请联系管理员。</div>
          </div>
          <button type="button" className="btn btn-sm" onClick={load}>重试</button>
        </div>
      ) : null}

      {phase === 'legacy' ? (
        <div>
          <div className="state-box state-warn" role="status">
            该运行产生于 v2 管线之前，调用留痕不可用——历史运行无逐调用记录，本区域不以其他数据冒充。
          </div>
          <TraceDisclosures />
        </div>
      ) : null}

      {phase === 'ready' && data ? <TraceBody data={data} /> : null}
    </section>
  );
}

function TraceBody({ data }) {
  const { skill, skillTotal, rag, ragTotal, summary } = data;
  const summarySkill = summary?.skill ?? null;
  const summaryRag = summary?.rag ?? null;
  const skillByStatus = summarySkill?.by_status ?? countsByStatus(skill);
  const ragByStatus = summaryRag?.by_status ?? countsByStatus(rag);
  const byRole = summarySkill?.by_role ?? countsByRole(skill);
  // 当前 Skill version（本 run 出现过的全部版本，原样显示；无记录则"未记录"）
  const versions = [...new Set(skill.map((s) => s.skill_version).filter((v) => v != null && v !== ''))];
  const partial = [...skill, ...rag].some((it) => ['RUNNING', 'INTERRUPTED'].includes(String(it.status ?? '').toUpperCase()));

  return (
    <div>
      <TraceDisclosures />
      {partial ? (
        <div className="state-box state-warn trace-partial" role="status">
          部分调用仍在进行或被恢复补记——进行中/已恢复补记的记录可能缺少耗时与输出摘要，以服务端后续状态为准。
        </div>
      ) : null}

      <div className="kv-grid trace-summary">
        <div className="kv" title="本次运行的 Skill 调用留痕总条数（服务端 total 口径）">
          <div className="kv-label">Skill 调用次数</div>
          <div className="kv-value num">{skillTotal}</div>
        </div>
        <div className="kv" title="本次运行的 RAG 检索留痕总条数（服务端 total 口径）">
          <div className="kv-label">RAG 检索次数</div>
          <div className="kv-value num">{ragTotal}</div>
        </div>
        <div className="kv" title="Skill 调用按状态计数（成功/失败等；未知状态原值显示）">
          <div className="kv-label">Skill 状态计数</div>
          <div className="kv-value"><StatusCounts byStatus={skillByStatus} /></div>
        </div>
        <div className="kv" title="RAG 检索按状态计数">
          <div className="kv-label">RAG 状态计数</div>
          <div className="kv-value"><StatusCounts byStatus={ragByStatus} /></div>
        </div>
        <div className="kv" title="按 Agent 角色分组的 Skill 调用计数（reviewer/leader/fixer/verifier；未知角色原值）">
          <div className="kv-label">按 Agent 角色分组</div>
          <div className="kv-value"><RoleCounts byRole={byRole} /></div>
        </div>
        <div className="kv" title="本 run 调用记录中出现的 Skill version（原样显示，不代表 worker 当前安装版本）">
          <div className="kv-label">Skill version</div>
          <div className="kv-value mono">{versions.length ? versions.join(', ') : '未记录'}</div>
        </div>
      </div>

      <h4 className="trace-subtitle">Skill 调用明细</h4>
      {skill.length === 0 ? (
        <div className="state-box state-empty" role="status">本次运行没有 Skill 调用记录（真实空集，非降级）。</div>
      ) : (
        <SkillTable items={skill} total={skillTotal} />
      )}

      <h4 className="trace-subtitle">RAG 检索明细</h4>
      {rag.length === 0 ? (
        <div className="state-box state-empty" role="status">本次运行没有 RAG 检索记录（真实空集，非降级）。</div>
      ) : (
        <RagTable items={rag} total={ragTotal} />
      )}
    </div>
  );
}

function countsByRole(items) {
  const by = {};
  for (const it of items ?? []) {
    const k = String(it?.agent_role ?? 'UNKNOWN');
    by[k] = (by[k] ?? 0) + 1;
  }
  return by;
}

function Truncated({ values, total }) {
  if (Number(total) > values.length) {
    return <p className="section-note">仅显示前 {values.length} 条（共 {total} 条）——其余未加载。</p>;
  }
  return <p className="section-note">共 {total} 条。</p>;
}

function SkillTable({ items, total }) {
  return (
    <div>
      <Truncated values={items} total={total} />
      <div className="table-scroll">
        <table className="data-table trace-table">
          <thead>
            <tr>
              <th>Agent</th>
              <th>Skill</th>
              <th>版本</th>
              <th>调用类型</th>
              <th>状态</th>
              <th className="th-right">耗时</th>
              <th>时间</th>
              <th>错误码</th>
              <th title="输入摘要（SHA256 前 12 位，title 为全文）">输入摘要</th>
              <th title="输出摘要（SHA256 前 12 位，title 为全文）">输出摘要</th>
            </tr>
          </thead>
          <tbody>
            {items.map((it, i) => (
              <tr key={it.event_id ?? `ev-${i}`}>
                <td title={roleLabel(it.agent_role)}>{roleLabel(it.agent_role)}</td>
                <td className="mono"><span className="trace-ellipsis trace-skill" title={it.skill_key ?? ''}>{it.skill_key ?? '—'}</span></td>
                <td className="mono">{it.skill_version ?? '—'}</td>
                <td><KindText kind={it.invocation_kind} /></td>
                <td><StatusBadge status={it.status} /></td>
                <td className="num" title={it.latency_ms != null ? `${it.latency_ms} ms` : undefined}>{fmtLatency(it.latency_ms)}</td>
                <td className="cell-time num">{fmtTime(it.started_at) ?? '—'}</td>
                <td><ErrorCode value={it.error_code} /></td>
                <td><Digest value={it.input_digest} /></td>
                <td><Digest value={it.output_digest} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function RagTable({ items, total }) {
  return (
    <div>
      <Truncated values={items} total={total} />
      <div className="table-scroll">
        <table className="data-table trace-table">
          <thead>
            <tr>
              <th>Agent</th>
              <th>状态</th>
              <th className="th-right" title="检索返回的文档/片段数">命中数</th>
              <th title="查询摘要（digest，非 query 原文）">查询摘要</th>
              <th title="来源摘要 / 稳定 ID（截断显示，title 为全文；不含文档正文）">来源摘要</th>
              <th className="th-right">耗时</th>
              <th>时间</th>
              <th>错误码</th>
            </tr>
          </thead>
          <tbody>
            {items.map((it, i) => (
              <tr key={it.event_id ?? `ev-${i}`}>
                <td title={roleLabel(it.agent_role)}>{roleLabel(it.agent_role)}</td>
                <td><StatusBadge status={it.status} /></td>
                <td className="num">{it.result_count ?? '—'}</td>
                <td><Digest value={it.query_digest} /></td>
                <td><DigestList values={it.source_digest_list} /></td>
                <td className="num" title={it.latency_ms != null ? `${it.latency_ms} ms` : undefined}>{fmtLatency(it.latency_ms)}</td>
                <td className="cell-time num">{fmtTime(it.started_at) ?? '—'}</td>
                <td><ErrorCode value={it.error_code} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
