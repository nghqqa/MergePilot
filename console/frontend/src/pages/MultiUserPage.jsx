import { Alert, Button, Input, InputNumber, Modal, Popconfirm, Segmented, Select, Space, Switch, Table, Tag, Typography } from 'antd';
import { ReloadOutlined, CheckCircleOutlined } from '@ant-design/icons';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { MuPrDetailContent } from './MuPrDetailContent.jsx';
import { readCsrfCookie } from '../api-live.js';

// 多用户面（Developer Edition Beta onboarding）。
// 渐进式工作流：按后端真实状态只突出当前一步（登录 → 成员资格 → 安装 GitHub App →
// 绑定仓库 → PR 审查），已完成步骤折叠为紧凑摘要。所有状态使用后端 API reason /
// configured:false 语义；按钮仅反映权限，授权一律由后端执行。不显示 token/private key/webhook secret。

const ROLE_TONE = {
  contributor: 'default', reviewer: 'blue', maintainer: 'green',
  platform_admin: 'purple', auditor: 'orange',
};

// 权限动作的人话标签；raw action id 只出现在"技术详情"中
const ACTION_LABELS = {
  read_repository: '查看仓库',
  read_pull_request: '查看 PR',
  read_code_content: '读取代码内容',
  rag_query: 'RAG 检索',
  request_review: '发起只读审查',
  decide_review: '审批决定（通过/驳回）',
  request_repair: '发起受控修复',
  manage_repository_binding: '管理仓库绑定',
};

const LOGIN_ERROR_MAP = {
  not_invited: '身份未被邀请（无公共自动注册）——请联系管理员以你的 GitHub 数字 user id 创建邀请',
  state_invalid: 'state 无效或已使用（请重新发起登录）',
  state_expired: '流程已过期（10 分钟）——请重新发起',
  oauth_exchange_failed: 'GitHub 授权交换失败——请重试',
  oauth_identity_invalid: 'GitHub 身份读取失败——请重试',
  oauth_not_configured: 'OAuth 未配置——联系管理员设置 MU_GITHUB_OAUTH_* 三项',
  no_active_membership: '无有效成员关系——邀请可能已过期，请联系管理员',
  user_disabled: '账户已停用——请联系管理员',
};

const GHAPP_ERROR_MAP = {
  state_invalid: '安装流程 state 无效——请重新点击安装按钮',
  installation_unreadable: 'GitHub App 安装信息不可读——请检查 App 权限或重试',
  installation_revoked: '此 installation 已被撤销——请重新安装 GitHub App',
  installation_suspended: '此 installation 已被暂停——请在 GitHub 设置中恢复',
  repository_not_authorized: '此仓库不在 installation 授权范围内——请在 GitHub App 设置中添加',
  repository_already_bound: '此仓库已被其他组织绑定——每个仓库只能属于一个组织',
  github_read_failed: 'GitHub API 读取失败——请稍后重试',
};

async function muGet(path) {
  const res = await fetch(path);
  let body = null; try { body = await res.json(); } catch { /* */ }
  return { status: res.status, body };
}
async function muPost(path, payload) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'X-CSRF-Token': readCsrfCookie() },
    body: JSON.stringify(payload ?? {}),
  });
  let body = null; try { body = await res.json(); } catch { /* */ }
  return { status: res.status, body };
}
async function muDelete(path) {
  const res = await fetch(path, {
    method: 'DELETE',
    headers: { 'X-CSRF-Token': readCsrfCookie() },
  });
  let body = null; try { body = await res.json(); } catch { /* */ }
  return { status: res.status, body };
}

// ── Onboarding 阶段推导：只消费后端状态，前端不做任何权限推断 ──
// 阶段：login / install / install_blocked / bind / waiting / review
function deriveOnboarding({ session, ghStatus, installations, repos, prs, prsState }) {
  if (!session) {
    return { stage: 'login', done: { login: false, member: false, install: false, bind: false, review: false } };
  }
  const hasGhApp = ghStatus?.configured === true;
  const hasInstallation = (installations ?? []).some((i) => !i.revoked && !i.suspended);
  const hasBinding = (repos ?? []).some((r) => r.binding_id && r.installation_state === 'active');
  const hasPrs = prsState === 'ready' && (prs ?? []).length > 0;
  const done = {
    login: true, member: true,
    install: hasInstallation, bind: hasBinding, review: hasPrs,
  };
  let stage;
  if (ghStatus == null) stage = 'loading'; // 接入状态未加载完成——不误报"未配置"
  else if (!hasInstallation) stage = hasGhApp ? 'install' : 'install_blocked';
  else if (!hasBinding) stage = 'bind';
  else stage = hasPrs ? 'review' : 'waiting';
  return { stage, done };
}

const STEP_TITLES = {
  login: '登录', member: '成员资格', install: '安装 GitHub App',
  bind: '绑定仓库', review: 'PR 审查',
};

// ── 审查管线面板（Wave 3 PR-E 移植：只读展示 review_run 全链状态）──
// 13 态人话 + attempt/retry + finding 定位 + fix dry-run/verifier verdict + blocked 原因。
// 无 approve/merge/write 按钮（人工审批仍走上方既有决策区）；只消费只读 /api/mu/runs 端点，
// 授权由后端 read_pull_request 判定，403 时如实显示权限不足。
const PIPE_LABEL = {
  RECEIVED: '已接收', REVIEW_QUEUED: '审查排队', REVIEWING: '审查中', REVIEWED: '已审查',
  FIX_QUEUED: '修复排队', FIXING: '修复预演', VERIFY_QUEUED: '验证排队', VERIFYING: '验证中',
  VERIFIED: '已验证', REWORK_REQUIRED: '需返工', BLOCKED: '受阻', FAILED: '失败', COMPLETED: '已完成',
};
const SEV_TONE = { P0: 'red', P1: 'volcano', P2: 'orange', P3: 'gold' };

export function PipelinePanel({ prNumber, repoId }) {
  const [state, setState] = useState({ phase: 'idle' }); // idle | loading | ready | empty | denied | error
  useEffect(() => {
    if (!prNumber || !repoId) { setState({ phase: 'idle' }); return undefined; }
    let dead = false;
    setState({ phase: 'loading' });
    (async () => {
      try {
        const lr = await fetch(`/api/mu/runs?repo_id=${encodeURIComponent(repoId)}`, { credentials: 'same-origin' });
        if (dead) return;
        if (lr.status === 403) { setState({ phase: 'denied' }); return; }
        if (!lr.ok) { setState({ phase: 'error', status: lr.status }); return; }
        const body = await lr.json().catch(() => null);
        const run = (body?.runs ?? []).find((x) => Number(x.provider_pr_number) === Number(prNumber));
        if (!run) { setState({ phase: 'empty' }); return; }
        const d = await fetch(`/api/mu/runs/${encodeURIComponent(run.run_id)}`, { credentials: 'same-origin' });
        if (dead) return;
        if (!d.ok) { setState({ phase: 'error', status: d.status }); return; }
        const detail = await d.json().catch(() => null);
        if (!detail?.run) { setState({ phase: 'empty' }); return; }
        if (!dead) setState({ phase: 'ready', detail });
      } catch {
        if (!dead) setState({ phase: 'error', status: null });
      }
    })();
    return () => { dead = true; };
  }, [prNumber, repoId]);

  if (state.phase === 'idle' || state.phase === 'loading') {
    return <Typography.Text type="secondary" style={{ display: 'block', marginTop: 12 }}>
      正在读取审查管线…
    </Typography.Text>;
  }
  if (state.phase === 'denied') {
    return <Alert style={{ marginTop: 12 }} type="info" showIcon
      message="当前角色无权查看审查管线——后端授权为准，可联系管理员调整角色。" />;
  }
  if (state.phase === 'error') {
    return <Alert style={{ marginTop: 12 }} type="warning" showIcon
      message={`审查管线暂时无法读取${state.status ? `（HTTP ${state.status}）` : ''}——可稍后重试；若持续失败请联系管理员检查 GitHub 通知与运行记录。`} />;
  }
  if (state.phase === 'empty') {
    return <Alert style={{ marginTop: 12 }} type="info" showIcon
      message="审查管线：该 PR 暂无自动审查运行（PR 更新后自动开始）" />;
  }
  const r = state.detail.run;
  const ROLE_LABEL = { leader: 'Leader（裁定）', reviewer: 'Reviewer（审查）', fixer: 'Fixer（修复建议）', verifier: 'Verifier（独立验证）' };
  const ROLE_TONE = { leader: 'purple', reviewer: 'blue', fixer: 'cyan', verifier: 'geekblue' };
  const ATT_LABEL = { DONE: '完成', FAILED: '失败', RUNNING: '运行中', SKIPPED: '已跳过' };
  const attempts = state.detail.attempts ?? [];
  const decisions = state.detail.decisions ?? [];
  return (
    <div style={{ marginTop: 16 }}>
      <Typography.Title level={5} style={{ marginBottom: 8 }}>审查管线（自动化 Agent 运行）</Typography.Title>
      <Space size="large" wrap>
        <Tag color={r.status === 'COMPLETED' ? 'green' : ['BLOCKED', 'FAILED'].includes(r.status) ? 'red' : 'blue'}>
          {PIPE_LABEL[r.status] ?? r.status}
        </Tag>
        <span>触发：<Tag>{r.trigger_source === 'manual' ? '手动' : 'GitHub 事件'}</Tag></span>
        <span>head：<code>{String(r.head_sha ?? '').slice(0, 12)}</code></span>
        {(state.detail.dead_letters ?? []).length > 0 ? (
          <Tag color="red">受阻原因：{state.detail.dead_letters[0].reason}</Tag>) : null}
      </Space>
      {attempts.length > 0 ? (
        <div className="table-scroll" style={{ marginTop: 8 }}>
          <Table rowKey={(a) => `${a.agent_role}-${a.attempt}`} size="small" pagination={false}
            dataSource={attempts} aria-label="四 Agent 执行记录"
            columns={[
              { title: 'Agent', dataIndex: 'agent_role', width: 180,
                render: (v) => <Tag color={ROLE_TONE[v] ?? 'default'}>{ROLE_LABEL[v] ?? v}</Tag> },
              { title: '轮次', dataIndex: 'attempt', width: 60 },
              { title: '状态', dataIndex: 'status', width: 90,
                render: (v) => <Tag color={v === 'DONE' ? 'green' : v === 'FAILED' ? 'red' : v === 'RUNNING' ? 'blue' : 'default'}>{ATT_LABEL[v] ?? v}</Tag> },
              { title: '执行器', dataIndex: 'provider', width: 130,
                render: (v) => v === 'agentteams' ? 'AgentTeams（外部）' : v === 'deterministic' ? '规则引擎' : v },
              { title: '耗时', width: 80, render: (a) => a.latency_ms ? `${(Number(a.latency_ms) / 1000).toFixed(1)}s` : '—' },
              { title: '开始时间', width: 110, render: (a) => a.created_at ? new Date(a.created_at).toLocaleTimeString() : '—' },
              { title: '错误码', dataIndex: 'error_code', ellipsis: true,
                render: (v) => v ? <Tag color="red"><code>{v}</code></Tag> : '—' },
            ]} />
        </div>
      ) : null}
      {(state.detail.findings ?? []).length > 0 ? (
        <div className="table-scroll">
          <Typography.Title level={5} style={{ marginTop: 12, marginBottom: 4 }}>风险项（Reviewer 发现）</Typography.Title>
          <Table rowKey={(f) => `${f.rule_id}-${f.path}-${f.line_start}`}
            size="small" pagination={false} scroll={{ x: true }} dataSource={state.detail.findings}
            columns={[
              { title: '级别', dataIndex: 'severity', width: 60,
                render: (v) => <Tag color={SEV_TONE[v] ?? 'default'}>{v}</Tag> },
              { title: '规则', dataIndex: 'rule_id', render: (v) => <code>{v}</code> },
              { title: '位置', render: (f) => <code>{f.path}{f.line_start ? `:${f.line_start}` : ''}</code> },
              { title: '摘要', dataIndex: 'summary_masked', ellipsis: true },
              { title: '建议', dataIndex: 'remediation', ellipsis: true },
            ]} />
        </div>
      ) : <Typography.Text type="secondary" style={{ fontSize: 12 }}>未发现风险项。</Typography.Text>}
      {(state.detail.fixes ?? []).length > 0 ? (
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8, marginBottom: 0 }}>
          修复预演（dry-run，不写 GitHub）：{state.detail.fixes.map((f, i) =>
            <Tag key={i} color="cyan">{`第${f.attempt}轮 ${f.status}`}</Tag>)}
        </Typography.Paragraph>) : null}
      {(state.detail.verifications ?? []).length > 0 ? (
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 4, marginBottom: 0 }}>
          独立验证：{state.detail.verifications.map((v, i) =>
            <Tag key={i} color={v.verdict === 'PASS' ? 'green' : 'orange'}>{`第${v.attempt}轮 ${v.verdict}`}</Tag>)}
        </Typography.Paragraph>) : null}
      {decisions.length > 0 ? (
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 4, marginBottom: 0 }}>
          Leader 终裁：{decisions.map((d, i) =>
            <Tag key={i}>{`${d.stage}=${d.decision}`}</Tag>)}
        </Typography.Paragraph>) : null}
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8, marginBottom: 0 }}>
        {attempts.some((a) => a.provider === 'agentteams')
          ? '执行器：外部 AgentTeams 四 Agent（正式路径）。'
          : attempts.length ? '执行器：internal 规则引擎（开发/测试路径，非生产）。' : ''}
        修复为 dry-run（不写 GitHub）；AI 审查不构成 GitHub required review。
      </Typography.Paragraph>
    </div>
  );
}

// ── GitHub App 面板（含仓库列表与绑定操作；API 调用与错误语义不变） ──
// ── Agent 与模型策略面板（Wave 3.2；PlatformAdmin 操作，其余角色只读/无权提示）──
// 红线：无 API Key 输入框；无"测试真实模型"按钮；无 approve/merge/push/写仓库控件；
// deploy 状态只显示布尔/host 摘要/模型白名单（后端已脱敏）。
const RUNTIME_STATE_COPY = {
  env_not_configured: { tone: 'info', text: '内置 LLM 辅助通道未配置（可选功能）——四 Agent 审查不受影响（AgentTeams 执行器独立运行自带模型）' },
  policy_enabled_but_env_invalid: { tone: 'warning', text: '策略已启用 LLM 但环境配置非法/缺失——新任务 fail-closed 回落 deterministic（四 Agent 审查不受影响）' },
  configured_disabled_or_det_only: { tone: 'info', text: 'LLM 已配置但策略未启用（当前走 deterministic 规则 + AgentTeams 四 Agent）' },
  llm_assist_active: { tone: 'success', text: 'LLM assist 已启用（新审查任务生效；AgentTeams 四 Agent 管线不变）' },
};

function AgentPolicyPanel({ can, actions }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [draft, setDraft] = useState(null);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState(null);
  const canManage = Array.isArray(actions) && actions.includes('manage_instance');
  const [atStatus, setAtStatus] = useState(null);
  const load = useCallback(async () => {
    setErr(null);
    fetch('/api/mu/agentteams-status', { credentials: 'same-origin' })
      .then((r) => (r.ok ? r.json() : null)).then(setAtStatus).catch(() => setAtStatus(null));
    const r = await fetch('/api/mu/agent-policy', { credentials: 'same-origin' });
    if (r.status === 200) {
      const j = await r.json().catch(() => null);
      setData(j); setDraft(j?.policy ? { ...j.policy } : null); setMsg(null);
    } else setErr(r.status === 403 ? 'forbidden' : `HTTP ${r.status}`);
  }, []);
  useEffect(() => { load(); }, [load]);

  const save = async () => {
    setSaving(true); setMsg(null);
    const r = await fetch('/api/mu/agent-policy', { method: 'PUT', credentials: 'same-origin',
      headers: { 'content-type': 'application/json', 'X-CSRF-Token': readCsrfCookie() },
      body: JSON.stringify({ ...draft, expected_version: data.policy.policy_version, confirm: true }) });
    const j = await r.json().catch(() => null);
    setSaving(false);
    if (r.status === 200) setMsg({ type: 'success', text: `已保存（策略版本 v${j.policy.policy_version}——仅影响新建审查任务）` });
    else if (r.status === 409) setMsg({ type: 'warning', text: `版本冲突：他人已更新到 v${j?.error?.current_policy_version}——已刷新，请基于新版本重试` });
    else setMsg({ type: 'error', text: `保存失败：${j?.error?.reason ?? r.status}` });
    await load();
  };

  if (err === 'forbidden') {
    return <Alert type="info" showIcon message="Agent 与模型策略"
      description="仅平台管理员可查看与修改运行策略（当前角色无此权限）。" />;
  }
  if (err) return <Alert type="error" showIcon message="Agent 策略加载失败" description={err} />;
  if (!data || !draft) return <Typography.Text type="secondary">加载中…</Typography.Text>;
  const st = RUNTIME_STATE_COPY[data.runtime_state] ?? { tone: 'info', text: data.runtime_state };
  const dirty = JSON.stringify(draft) !== JSON.stringify(data.policy);
  return (
    <div>
      <Space size="large" wrap style={{ marginBottom: 8 }}>
        <Alert type={st.tone} showIcon message={st.text} style={{ padding: '2px 8px' }} />
        <Tag>策略版本 v{data.policy.policy_version}</Tag>
        <Tag color={data.deploy.provider_configured ? 'green' : 'orange'}>
          {data.deploy.provider_configured ? `部署已配置（${data.deploy.provider_host_summary ?? 'host 未解析'}）` : '部署未配置'}</Tag>
        {atStatus ? (
          <Tag color={atStatus.runtime_state === 'active' ? 'green' : atStatus.runtime_state === 'degraded' ? 'orange' : 'red'}>
            {'AgentTeams：' + (atStatus.runtime_state === 'active' ? 'active' : atStatus.runtime_state === 'degraded'
              ? 'degraded（有未解决死信）' : atStatus.runtime_state === 'internal_non_production'
                ? 'internal（非生产）' : 'unavailable/fail-closed')
              + (atStatus.controller ? ' · controller ' + atStatus.controller : '')
              + (typeof atStatus.workers_ready === 'number' ? ' · worker ' + atStatus.workers_ready + '/' + atStatus.workers_total : '')}</Tag>) : null}
        {data.executor ? (
          <Tag color={data.executor.mode === 'agentteams' ? 'green' : data.executor.mode === 'internal' ? 'orange' : 'red'}>
            {data.executor.mode === 'agentteams'
              ? `执行器：AgentTeams（正式路径，${data.executor.runtime ?? ''}/${data.executor.model ?? ''}）`
              : data.executor.mode === 'internal'
                ? `执行器：internal（${data.executor.internal_scope}—非生产路径）`
                : `执行器：未配置/拒绝（${data.executor.fail_reason}—fail-closed）`}</Tag>) : null}
      </Space>
      <Space direction="vertical" size="middle" style={{ width: '100%', maxWidth: 640 }} aria-label="Agent 运行策略设置">
        <div>
          <Typography.Text type="secondary">审查模式</Typography.Text>
          <div>
            <Segmented value={draft.mode} disabled={!canManage} onChange={(v) => setDraft({ ...draft, mode: v })}
              options={[{ label: '仅确定性规则', value: 'deterministic_only' },
                { label: 'LLM 辅助（建议通道）', value: 'llm_assist' }]} />
          </div>
        </div>
        <Space wrap>
          <span>
            <Typography.Text type="secondary">启用 LLM</Typography.Text>
            <div><Switch checked={draft.enabled} disabled={!canManage || draft.mode !== 'llm_assist'}
              onChange={(v) => setDraft({ ...draft, enabled: v, mode: v ? 'llm_assist' : draft.mode })}
              aria-label="启用 LLM 辅助" /></div>
          </span>
          <span>
            <Typography.Text type="secondary">模型（白名单）</Typography.Text>
            <div><Select style={{ minWidth: 200 }} value={draft.model} disabled={!canManage}
              onChange={(v) => setDraft({ ...draft, model: v })}
              options={(data.deploy.allowed_models ?? []).map((m) => ({ value: m, label: m }))}
              aria-label="模型选择" /></div>
          </span>
        </Space>
        <Space wrap>
          <span>
            <Typography.Text type="secondary">超时（ms，1000-120000）</Typography.Text>
            <div><InputNumber min={1000} max={120000} step={1000} value={draft.timeout_ms}
              disabled={!canManage} onChange={(v) => setDraft({ ...draft, timeout_ms: v })}
              aria-label="LLM 超时毫秒" /></div>
          </span>
          <span>
            <Typography.Text type="secondary">输出上限（tokens，64-4096）</Typography.Text>
            <div><InputNumber min={64} max={4096} step={64} value={draft.max_output_tokens}
              disabled={!canManage} onChange={(v) => setDraft({ ...draft, max_output_tokens: v })}
              aria-label="LLM 最大输出 tokens" /></div>
          </span>
        </Space>
        {canManage ? (
          <Popconfirm title="保存运行策略？" description="仅影响新建审查任务；运行中的任务沿用创建时配置。"
            onConfirm={save} okText="确认保存" cancelText="取消" disabled={!dirty || saving}>
            <Button type="primary" loading={saving} disabled={!dirty}>保存策略（确认门）</Button>
          </Popconfirm>
        ) : <Tag>只读（需平台管理员）</Tag>}
        {msg ? <Alert type={msg.type} showIcon message={msg.text} /> : null}
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
          API Key 与接入端点属于部署级机密，仅由服务器环境变量或 Secret Manager 管理——本页面不提供输入、查看或测试。
          LLM 输出仅为审查建议，不构成 GitHub required review，不触发任何仓库写操作。
        </Typography.Paragraph>
      </Space>
    </div>
  );
}

function GHAppPanel({ can, session, repos, onBound, onUnbound }) {
  const [status, setStatus] = useState(null);
  const [insts, setInsts] = useState(null);
  const [reposForInst, setReposForInst] = useState(null);
  const [selectedInst, setSelectedInst] = useState(null);
  const [msg, setMsg] = useState(null);
  const [loadingRepos, setLoadingRepos] = useState(false);

  const load = useCallback(async () => {
    const st = await muGet('/api/mu/github/app/status');
    setStatus(st.body);
    if (st.body?.configured) {
      const list = await muGet('/api/mu/github/installations');
      setInsts(list.body?.installations ?? []);
    }
  }, []);
  useEffect(() => { load(); }, [load]);

  const canBind = can('manage_repository_binding');
  const boundRepoIds = new Set((repos ?? []).filter((r) => r.binding_id).map((r) => r.provider_repo_id));

  const loadRepos = async (instId) => {
    setSelectedInst(instId); setReposForInst(null); setLoadingRepos(true); setMsg(null);
    const r = await muGet(`/api/mu/github/installations/${instId}/repositories`);
    setLoadingRepos(false);
    if (r.status === 200) setReposForInst(r.body?.repositories ?? []);
    else setMsg({ type: 'warning', text: `仓库列表获取失败：${GHAPP_ERROR_MAP[r.body?.error?.reason] ?? r.body?.error?.reason ?? r.status}` });
  };

  const bindRepo = async (repo) => {
    setMsg(null);
    // 先在本地创建 repo 记录，再绑定（两次调用与错误语义与原实现一致）
    const createRes = await muPost('/api/mu/repositories', {
      provider_repo_id: String(repo.id), owner: repo.owner_login, name: repo.name,
      default_branch: repo.default_branch,
    });
    if (createRes.status !== 200) {
      setMsg({ type: 'error', text: `仓库注册失败：${createRes.body?.error?.reason ?? createRes.status}` });
      return;
    }
    const repoId = createRes.body?.repository?.repo_id;
    const bindRes = await muPost(`/api/mu/repositories/${repoId}/ghapp-binding`, {
      installation_id: selectedInst, github_repo_id: repo.id,
    });
    if (bindRes.status === 200) {
      setMsg({ type: 'success', text: `✓ ${repo.owner_login}/${repo.name} 绑定成功` });
      if (onBound) onBound();
    } else {
      const reason = bindRes.body?.error?.reason;
      setMsg({ type: 'error', text: `绑定失败：${GHAPP_ERROR_MAP[reason] ?? reason ?? bindRes.status}` });
    }
  };

  const unbindRepo = async (repoId) => {
    const r = await muDelete(`/api/mu/repositories/${repoId}/ghapp-binding`);
    if (r.status === 200) {
      setMsg({ type: 'success', text: '✓ 已解绑' });
      if (onUnbound) onUnbound();
    } else setMsg({ type: 'error', text: `解绑失败：${r.body?.error?.reason ?? r.status}` });
  };

  return (
    <div>
      {status?.configured === false ? (
        <Alert type="info" showIcon message="GitHub App 未配置（fail-closed）"
          description={`需 ${status?.reason === 'github_app_slug_not_configured'
            ? 'MU_GITHUB_APP_SLUG（GitHub App URL slug）' : 'MU_GITHUB_APP_ID/_APP_SLUG/_PRIVATE_KEY/_WEBHOOK_SECRET/_INSTALL_CALLBACK_URL'} 显式配置。参见 BETA-GUIDE §4-§5。`} />
      ) : (
        <>
          <Space size="large" wrap style={{ marginBottom: 12 }}>
            <span>App #{status?.app_id} · 只读权限：{(status?.permissions ?? []).map((x) => <Tag key={x}><code>{x}</code></Tag>)}</span>
            {canBind ? (
              <Button size="small" onClick={async () => {
                const r = await muPost('/api/mu/github/install/start');
                if (r.body?.install_url) window.location.href = r.body.install_url;
                else setMsg({ type: 'warning', text: `安装发起失败：${GHAPP_ERROR_MAP[r.body?.error?.reason] ?? r.body?.error?.reason ?? r.status}` });
              }}>安装 / 更新 GitHub App</Button>
            ) : (
              <Tag color="orange">需 Maintainer 角色才能安装和绑定</Tag>
            )}
          </Space>

          {insts !== null && insts.length > 0 ? (
            <div style={{ marginBottom: 12 }}>
              <Typography.Text strong>Installations：</Typography.Text>
              <Space wrap style={{ marginTop: 4 }}>
                {insts.map((i) => (
                  <Button key={i.installation_id} size="small"
                    type={selectedInst === i.installation_id ? 'primary' : 'default'}
                    disabled={i.revoked || i.suspended}
                    onClick={() => loadRepos(i.installation_id)}>
                    #{i.installation_id} {i.account_login}
                    {i.revoked ? '（已撤销）' : i.suspended ? '（已暂停）' : ''}
                  </Button>
                ))}
              </Space>
            </div>
          ) : insts !== null && insts.length === 0 ? (
            <Alert type="info" showIcon message="尚无 installation"
              description="点击上方「安装 / 更新 GitHub App」跳转到 GitHub 完成安装，授权你的测试仓库。" style={{ marginBottom: 12 }} />
          ) : null}

          {loadingRepos ? <Typography.Text type="secondary">加载授权仓库列表…</Typography.Text> : null}

          {reposForInst !== null ? (
            <Table rowKey="id" size="small" pagination={false} dataSource={reposForInst} scroll={{ x: true }}
              columns={[
                { title: '仓库', render: (_, r) => <code>{r.owner_login}/{r.name}</code> },
                { title: '默认分支', dataIndex: 'default_branch' },
                { title: '状态', render: (_, r) => boundRepoIds.has(String(r.id))
                  ? <Tag color="green" icon={<CheckCircleOutlined />}>已绑定</Tag>
                  : <Tag>未绑定</Tag> },
                { title: '', render: (_, r) => canBind && !boundRepoIds.has(String(r.id)) ? (
                  <Button size="small" onClick={() => bindRepo(r)}>绑定</Button>
                ) : null },
              ]} />
          ) : null}

          {(repos ?? []).filter((r) => r.binding_id).length > 0 ? (
            <div style={{ marginTop: 12 }}>
              <Typography.Text strong>已绑定仓库：</Typography.Text>
              <Table rowKey="repo_id" size="small" pagination={false} scroll={{ x: true }}
                dataSource={(repos ?? []).filter((r) => r.binding_id)}
                columns={[
                  { title: '仓库', render: (_, r) => <code>{r.owner}/{r.name}</code> },
                  { title: '绑定状态', render: (_, r) => (
                    <Tag color={r.installation_state === 'active' ? 'green' : r.installation_state === 'revoked' ? 'red' : 'orange'}>
                      {r.installation_state ?? r.binding_state ?? 'unknown'}</Tag>
                  ) },
                  { title: '', render: (_, r) => canBind ? (
                    <Button size="small" danger onClick={() => unbindRepo(r.repo_id)}>解绑</Button>
                  ) : null },
                ]} />
            </div>
          ) : null}

          {msg ? <Alert style={{ marginTop: 12 }} type={msg.type} showIcon message={msg.text} /> : null}

          <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
            只读 Developer Edition 接入——不支持自动 approve/merge、不支持绕过 branch protection。
            仓库绑定与解绑经后端 Maintainer 校验执行。
          </Typography.Paragraph>
        </>
      )}
    </div>
  );
}

// ── Onboarding 面板：轨道 + 已完成摘要 + 当前步骤单一主 CTA ──
function OnboardingPanel({ ob, session, providers, loginError, prCount, boundCount, onLogin, onInstall, onCheckSync, onOpenLatest, latestPr, can }) {
  const order = ['login', 'member', 'install', 'bind', 'review'];
  const stepStateText = {
    login: session ? `已登录 ${session.user?.login ?? ''}` : '未登录',
    member: session ? String(session.role ?? '') : '需邀请',
    install: ob.done.install ? '已安装' : ob.stage === 'install_blocked' ? '管理员未配置' : '待安装',
    bind: ob.done.bind ? `已绑定 ${boundCount} 个仓库` : ob.done.install ? '待选择' : '需先安装',
    review: ob.done.review ? `${prCount} 个 PR` : ob.done.bind ? '等待 PR 同步' : '待完成',
  };
  const doneKeys = order.filter((k) => ob.done[k]);
  const currentKey = ob.stage === 'install_blocked' ? 'install' : ob.stage;

  const cta = (() => {
    switch (ob.stage) {
      case 'login': {
        return (
          <>
            <Button type="primary" disabled={providers?.github?.configured !== true}
              onClick={onLogin}>使用 GitHub 登录</Button>
            {providers?.github && providers.github.configured === false ? (
              <Typography.Text type="secondary" style={{ display: 'block', marginTop: 8 }}>
                GitHub OAuth 未配置（configured:false，fail-closed）——需 MU_GITHUB_OAUTH_CLIENT_ID/_CLIENT_SECRET/_CALLBACK_URL。
                参见 BETA-GUIDE §5。
              </Typography.Text>
            ) : null}
            {providers?.fixture?.configured ? <Typography.Text type="secondary">fixture 登录通道开启（测试配置）。</Typography.Text> : null}
          </>
        );
      }
      case 'install':
        return can('manage_repository_binding') ? (
          <Button type="primary" onClick={onInstall}>安装 GitHub App</Button>
        ) : (
          <Space wrap>
            <Button type="primary" disabled>安装 GitHub App</Button>
            <Tag color="orange">需 Maintainer 角色才能安装——当前角色无此权限</Tag>
          </Space>
        );
      case 'install_blocked':
        return (
          <Alert type="info" showIcon
            message="管理员尚未配置 GitHub App——配置完成后此步骤自动解锁"
            description="需要管理员设置 MU_GITHUB_APP_* 环境项（见 BETA-GUIDE §4-§5）。此步骤在本页无法自助完成。" />
        );
      case 'bind':
        return <Button type="primary" href="#mu-ghapp">前往选择仓库并绑定</Button>;
      case 'waiting':
        return (
          <>
            <Button type="primary" onClick={onCheckSync}>检查 PR 同步</Button>
            <Typography.Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0 }}>
              仓库已绑定。PR 创建后由 GitHub App webhook 自动同步（需 App 订阅 pull_request 事件），
              通常几秒内出现；长时间未同步时请检查 App 的事件订阅设置。
            </Typography.Paragraph>
          </>
        );
      case 'review':
        return latestPr ? (
          <Button type="primary" onClick={() => onOpenLatest(latestPr.pr_id)}>
            打开最新 PR（#{latestPr.provider_pr_number}）
          </Button>
        ) : null;
      default:
        return null;
    }
  })();

  const currentExplain = {
    login: '使用 GitHub OAuth 登录本工作台。登录后按邀请获得角色。',
    member: '成员资格由管理员邀请建立——登录即视为已具备。',
    install: '安装只读 GitHub App，授权工作台读取你组织的仓库与 PR（不写仓库）。',
    bind: '在下方面板选择一个 installation，并从授权仓库中选出要审查的仓库完成绑定。',
    review: 'PR 已同步——在下方列表选择要审查的 PR。',
  }[ob.stage] ?? '';

  return (
    <div className="panel onb-panel">
      <ol className="onb-track" aria-label="接入进度">
        {order.map((k) => (
          <li key={k}
            className={`onb-step ${ob.done[k] ? 'onb-step-done' : ''} ${k === currentKey && ob.stage !== 'install_blocked' ? 'onb-step-current' : ''}`}
            aria-current={k === currentKey ? 'step' : undefined}>
            <span className="onb-ico" aria-hidden>{ob.done[k] ? '✓' : k === currentKey ? '●' : '○'}</span>
            <span>{STEP_TITLES[k]}</span>
            <span className="onb-state">{stepStateText[k]}</span>
          </li>
        ))}
      </ol>

      {doneKeys.length > 0 ? (
        <details className="onb-done tech-details">
          <summary>已完成 {doneKeys.length} 步：{doneKeys.map((k) => STEP_TITLES[k]).join(' · ')}</summary>
          <ul className="compact-list">
            {doneKeys.map((k) => <li key={k}>{STEP_TITLES[k]}——{stepStateText[k]}</li>)}
          </ul>
        </details>
      ) : null}

      <div className="onb-current-card">
        {ob.stage !== 'login' ? <div className="onb-current-label">当前步骤</div> : null}
        <div className="onb-current-title">
          {ob.stage === 'install_blocked' ? '安装 GitHub App（等待管理员配置）'
            : ob.stage === 'loading' ? '正在读取接入状态…' : STEP_TITLES[currentKey]}
        </div>
        {currentExplain ? <p className="onb-note">{currentExplain}</p> : null}
        {ob.stage === 'login' && loginError ? (
          <Alert type="warning" showIcon style={{ marginBottom: 10 }}
            message={`登录未完成：${LOGIN_ERROR_MAP[loginError] ?? loginError}`} />
        ) : null}
        {cta}
      </div>
    </div>
  );
}

export default function MultiUserPage() {
  const [session, setSession] = useState(null);
  const [notEnabled, setNotEnabled] = useState(false);
  const [error, setError] = useState(null);
  const [members, setMembers] = useState(null);
  const [repos, setRepos] = useState(null);
  const [numFilter, setNumFilter] = useState('');
  const [prs, setPrs] = useState(null);
  const [prsState, setPrsState] = useState('idle'); // idle | loading | ready | error
  const [prRepoId, setPrRepoId] = useState(null);
  const [actionMsg, setActionMsg] = useState(null);
  const [loading, setLoading] = useState(false);
  const [providers, setProviders] = useState(null);
  const [ghStatus, setGhStatus] = useState(null);
  const [installations, setInstallations] = useState(null);

  const refresh = useCallback(async () => {
    setLoading(true); setError(null); setActionMsg(null);
    try {
      const s = await muGet('/api/mu/session');
      const pv = await muGet('/api/mu/auth/providers').catch(() => null);
      if (pv?.status === 200) setProviders(pv.body);
      if (s.status === 200 && s.body?.user) {
        setSession(s.body); setNotEnabled(false);
        const [m, r, gs] = await Promise.all([
          muGet('/api/mu/members'),
          muGet('/api/mu/repositories'),
          muGet('/api/mu/github/app/status').catch(() => null),
        ]);
        if (m.status === 200) setMembers(m.body?.members ?? []);
        if (r.status === 200) setRepos(r.body?.repositories ?? []);
        if (gs) {
          setGhStatus(gs.status === 200 ? gs.body : { configured: gs.status >= 500 ? null : false, _status: gs.status });
          if (gs.body?.configured) {
            const il = await muGet('/api/mu/github/installations').catch(() => null);
            if (il?.status === 200) setInstallations(il.body?.installations ?? []);
          }
        }
      } else if (s.status === 401) {
        setSession(null); setError(null);
      } else if (s.body?.service_state === 'multiuser_not_enabled') {
        setNotEnabled(true); setSession(null);
      } else {
        setError(new Error(`HTTP ${s.status}${s.body?.error?.reason ? `（${s.body.error.reason}）` : ''}`));
        setSession(null);
      }
    } catch (e) {
      setError(e); setSession(null);
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const can = (action) => Boolean(session?.actions?.includes(action));

  const activeBindings = useMemo(
    () => (repos ?? []).filter((r) => r.binding_id && r.installation_state === 'active'),
    [repos]);
  const boundRepos = useMemo(
    () => (repos ?? []).filter((r) => r.binding_id),
    [repos]);
  const effectiveRepoId = prRepoId ?? activeBindings[0]?.repo_id ?? null;
  const effectiveRepo = boundRepos.find((r) => r.repo_id === effectiveRepoId) ?? null;

  // 已绑定仓库的 PR 自动同步（既有 GET /api/mu/prs 端点，read_pull_request 权限内）；
  // 失败降级为人话提示 + 重试指引，不打印原始机器 reason
  useEffect(() => {
    if (!session) { setPrs(null); setPrsState('idle'); return undefined; }
    const rid = effectiveRepoId;
    if (!rid) { setPrs(null); setPrsState('ready'); return undefined; }
    let dead = false;
    setPrsState('loading');
    muGet(`/api/mu/prs?repo_id=${encodeURIComponent(rid)}`)
      .then((r) => {
        if (dead) return;
        if (r.status === 200) { setPrs(r.body?.pull_requests ?? []); setPrsState('ready'); }
        else { setPrs(null); setPrsState('error'); }
      })
      .catch(() => { if (!dead) { setPrs(null); setPrsState('error'); } });
    return () => { dead = true; };
  }, [session, effectiveRepoId]);

  // Wave 3.16 master-detail：选中 PR 同步到 URL query（?repo_id=&pr=）——
  // 刷新/直接打开/前进后退均可恢复；setSearchParams 走 router 历史（可回退）。
  const [searchParams, setSearchParams] = useSearchParams();
  const urlRepoId = searchParams.get('repo_id');
  const urlPr = Number(searchParams.get('pr')) || null;
  const selectPr = useCallback((prNumber) => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (prNumber == null) next.delete('pr');
      else next.set('pr', String(prNumber));
      return next;
    }, { replace: false });
  }, [setSearchParams]);
  const selectRepo = useCallback((repoId) => {
    setPrRepoId(repoId);
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (repoId) next.set('repo_id', repoId); else next.delete('repo_id');
      next.delete('pr'); // 换仓库清选中（不同仓库 PR 编号语义变化）
      return next;
    }, { replace: false });
  }, [setSearchParams]);
  // URL 携带 repo_id 时优先（直接打开/刷新恢复）
  useEffect(() => {
    if (urlRepoId && urlRepoId !== prRepoId) setPrRepoId(urlRepoId);
  }, [urlRepoId]); // eslint-disable-line react-hooks/exhaustive-deps
  const selectedPrRef = (urlPr && effectiveRepo)
    ? { repoId: effectiveRepoId, owner: effectiveRepo.owner, name: effectiveRepo.name, prNumber: urlPr }
    : null;

  const runAction = useCallback(async (label, path, payload) => {
    setActionMsg(null);
    const r = await muPost(path, payload);
    setActionMsg({
      ok: r.status === 200, label, status: r.status,
      reason: r.body?.error?.reason ?? null,
      note: r.body?.note ?? (r.status === 200 ? '已受理（后端授权为准）' : null),
    });
    // 详情已拆独立路由页——操作成功后刷新本页 PR 列表即可
  }, []);

  const doInstall = useCallback(async () => {
    const r = await muPost('/api/mu/github/install/start');
    if (r.body?.install_url) window.location.href = r.body.install_url;
  }, []);

  const shownPrs = useMemo(() => {
    if (prsState !== 'ready' || !prs) return [];
    const needle = numFilter.trim();
    const pool = needle
      ? prs.filter((p) => String(p.provider_pr_number ?? '').includes(needle))
      : prs;
    // 同一 PR 多行（每个历史 head 一行——upsert 键含 head_sha）→ 只显最新一行，
    // 历史 head 收进 PR 详情；避免表被同一 PR 的历史行刷长。
    const latest = new Map();
    for (const p of pool) {
      const k = String(p.provider_pr_number);
      const prev = latest.get(k);
      if (!prev || String(p.updated_at ?? '') > String(prev.updated_at ?? '')) latest.set(k, p);
    }
    return [...latest.values()];
  }, [prs, prsState, numFilter]);

  const latestPr = useMemo(() => {
    if (!shownPrs.length) return null;
    return [...shownPrs].sort((a, b) => String(b.updated_at ?? '').localeCompare(String(a.updated_at ?? '')))[0];
  }, [shownPrs]);

  const ob = deriveOnboarding({ session, ghStatus, installations, repos, prs, prsState });
  const loginError = new URLSearchParams(window.location.search).get('mu_login_error');
  const ghappError = new URLSearchParams(window.location.search).get('ghapp_error');
  const boundCount = boundRepos.length;

  return (
    <div>
      <Typography.Title level={1} style={{ fontSize: 24, marginBottom: 4 }}>
        组织与接入（Developer Edition Beta）
      </Typography.Title>
      <Typography.Paragraph type="secondary">
        PR 安全审查 · GitHub App 只读接入 · 多用户 RBAC · 完整审计追踪。
        按钮仅反映权限——授权一律由后端执行。
        完整指南：<a href="https://github.com/nghqqa/MergePilot/blob/main/docs/BETA-GUIDE.md" target="_blank" rel="noopener">BETA-GUIDE.md</a>
      </Typography.Paragraph>

      {notEnabled ? (
        <Alert type="info" showIcon message="多用户面未启用"
          description="MU_MODE != multiuser（当前为 legacy 模式）。启用需设置 MU_MODE=multiuser 并配置 CONSOLE_PG_DSN——本页不显示任何推断数据。" />
      ) : null}

      {!notEnabled && !session && !loading ? (
        <div style={{ marginBottom: 24, padding: '32px 24px', textAlign: 'center',
            border: '1px solid #d9d9d9', borderRadius: 8, background: '#fafafa' }}>
          <Typography.Title level={3} style={{ marginBottom: 8 }}>登录以继续</Typography.Title>
          <Typography.Paragraph type="secondary" style={{ marginBottom: 20 }}>
            使用 GitHub OAuth 登录本工作台。登录后按邀请获得角色。
          </Typography.Paragraph>
          <Button type="primary" size="large"
            disabled={providers?.github?.configured !== true}
            onClick={async () => {
              const r = await muGet('/api/mu/auth/oauth/github/start');
              if (r.status === 200 && r.body?.authorize_url) window.location.href = r.body.authorize_url;
            }}>
            使用 GitHub 登录
          </Button>
          {providers?.github && providers.github.configured === false ? (
            <Typography.Text type="secondary" style={{ display: 'block', marginTop: 8 }}>
              GitHub OAuth 未配置——需 MU_GITHUB_OAUTH_* 三项（BETA-GUIDE §5）。
            </Typography.Text>
          ) : null}
        </div>
      ) : null}

      {!notEnabled && session ? (
        <div style={{ marginBottom: 16 }}>
          <OnboardingPanel
            ob={ob} session={session} providers={providers} loginError={loginError}
            prCount={prsState === 'ready' ? (prs ?? []).length : 0}
            boundCount={boundCount} can={can}
            onLogin={async () => {
              const r = await muGet('/api/mu/auth/oauth/github/start');
              if (r.status === 200 && r.body?.authorize_url) window.location.href = r.body.authorize_url;
            }}
            onInstall={doInstall}
            onCheckSync={refresh}
            onOpenLatest={selectPr}
            latestPr={latestPr} />
        </div>
      ) : null}

      {ghappError ? (
        <Alert type="warning" showIcon style={{ marginBottom: 16 }}
          message={`GitHub App 操作未完成：${GHAPP_ERROR_MAP[ghappError] ?? ghappError}`} />
      ) : null}

      {error ? (
        <Alert type="error" showIcon message="多用户会话不可用"
          description={`${error.message} — 后端异常时本页保持空态。请检查 MU_MODE/CONSOLE_PG_DSN 配置或联系管理员。`} />
      ) : null}

      {session ? (
        <>
          <div className="panel" style={{ padding: 'var(--sp-4)', marginBottom: 16 }}>
            <Space size="large" wrap>
              <span>用户：<b>{session.user?.login}</b></span>
              <span>组织：<b>{session.tenant?.slug}</b></span>
              <span>角色：<Tag color={ROLE_TONE[session.role] ?? 'default'}>{session.role}</Tag></span>
            </Space>
            <div style={{ marginTop: 8 }}>
              可用操作：
              {(session.actions ?? []).map((a) => ACTION_LABELS[a]
                ? <Tag key={a}>{ACTION_LABELS[a]}</Tag>
                : null)}
            </div>
            <details className="tech-details" style={{ marginTop: 10 }}>
              <summary>技术详情（raw action id）</summary>
              <div className="tech-body">
                {(session.actions ?? []).length
                  ? (session.actions ?? []).map((a) => <Tag key={a}><code>{a}</code></Tag>)
                  : <span className="muted">（无）</span>}
              </div>
            </details>
          </div>

          <details className="tech-details mu-admin-drawer">
            <summary>接入管理（GitHub App · 成员 · Agent 策略）——默认折叠，PR 审查为主工作区</summary>
            <div className="tech-body">
          {ob.stage !== 'login' ? (
            ob.done.review ? (
              <details className="tech-details section">
                <summary>GitHub App 与仓库绑定（已就绪）——展开管理绑定</summary>
                <div className="tech-body">
                  <section id="mu-ghapp">
                    <GHAppPanel can={can} session={session} repos={repos}
                      onBound={refresh} onUnbound={refresh} />
                  </section>
                  <section id="mu-agent-policy" style={{ marginTop: 16 }}>
                    <div className="section-head"><h3>Agent 与模型（运行策略）</h3></div>
                    <AgentPolicyPanel can={can} actions={session?.actions} />
                  </section>
                </div>
              </details>
            ) : (
              <section className="section" id="mu-ghapp">
                <div className="section-head"><h3>GitHub App 与仓库绑定</h3></div>
                <GHAppPanel can={can} session={session} repos={repos}
                  onBound={refresh} onUnbound={refresh} />
              </section>
            )
          ) : null}

          <section className="section">
            <details className="tech-details">
              <summary>成员（{members?.length ?? 0}，只读）</summary>
              <div className="tech-body">
                <Table rowKey="membership_id" size="small" pagination={false} dataSource={members ?? []} scroll={{ x: true }}
                  columns={[
                    { title: '成员', dataIndex: 'login' },
                    { title: '角色', dataIndex: 'role', render: (v) => <Tag color={ROLE_TONE[v] ?? 'default'}>{v}</Tag> },
                    { title: '状态', dataIndex: 'state' },
                    { title: '加入时间', dataIndex: 'created_at', render: (v) => String(v ?? '').slice(0, 19).replace('T', ' ') },
                  ]} />
              </div>
            </details>
          </section>
            </div>
          </details>


          {ob.done.bind ? (
            <section className="section mu-workbench" id="mu-prs" aria-label="PR 审查工作台">
              <div className="section-head mu-workbench-head"><h3>PR 审查</h3></div>
              <div className="mu-controls">
                <Select className="mu-repo-select" placeholder="选择仓库"
                  aria-label="选择要查看的仓库"
                  value={effectiveRepoId ?? undefined}
                  onChange={(v) => selectRepo(v)}
                  options={boundRepos.map((r) => ({ value: r.repo_id, label: `${r.owner}/${r.name}` })) } />
                <Input className="mu-pr-filter" placeholder="按 PR 编号过滤" value={numFilter}
                  aria-label="按 PR 编号过滤"
                  onChange={(e) => setNumFilter(e.target.value)} allowClear />
              </div>

              <div className="mu-master-detail">
                <div className="mu-pr-list" role="listbox" aria-label="PR 列表" tabIndex={-1}>
                  {prsState === 'loading' ? <div className="mu-detail-state" role="status">正在读取 PR 列表…</div> : null}
                  {prsState === 'error' ? (
                    <Alert type="warning" showIcon message="PR 列表暂时无法读取"
                      description="可稍后点击「立即刷新」重试；若持续失败请联系管理员检查 GitHub App 通知配置。" />
                  ) : null}
                  {prsState === 'ready' && shownPrs.length === 0 ? (
                    <Alert type="info" showIcon
                      message={numFilter ? '没有匹配该编号的 PR' : '该仓库还没有 PR'}
                      description={numFilter ? '清除编号过滤后查看全部。' : 'PR 创建后会自动同步到这里。'} />
                  ) : null}
                  {shownPrs.map((pr) => {
                    const sha = String(pr.head_sha ?? '');
                    const repoLabel = effectiveRepo ? `${effectiveRepo.owner}/${effectiveRepo.name}` : '';
                    const selected = urlPr === Number(pr.provider_pr_number);
                    const label = `仓库 ${repoLabel}，PR ${pr.provider_pr_number}，head ${sha.slice(0, 12) || '未知'}`;
                    return (
                      <div key={pr.pr_id} role="option" tabIndex={0}
                        aria-selected={selected}
                        aria-label={label}
                        className={`mu-pr-row${selected ? ' is-selected' : ''}`}
                        onClick={() => selectPr(Number(pr.provider_pr_number))}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' || e.key === ' ') {
                            e.preventDefault();
                            selectPr(Number(pr.provider_pr_number));
                          }
                        }}>
                        <span className="mu-pr-row-title">#{pr.provider_pr_number}
                          <code className="mono mu-pr-row-sha">{sha.slice(0, 10) || '未知'}</code>
                        </span>
                        <span className="mu-pr-row-sub">
                          <Tag className="mu-pr-row-tag" color={pr.branch_protection_status === 'known_clean' ? 'green' : 'orange'}>
                            {pr.branch_protection_status === 'known_clean' ? '受保护' : String(pr.branch_protection_status ?? 'unknown')}
                          </Tag>
                          <span className="muted">{String(pr.updated_at ?? '').slice(5, 16).replace('T', ' ')}</span>
                        </span>
                      </div>
                    );
                  })}
                </div>

                <div className="mu-pr-detail" aria-live="polite"
                  onKeyDown={(e) => {
                    if (e.key !== 'Escape') return;
                    const prevNum = document.querySelector('.mu-pr-row.is-selected')?.textContent.match(/#(\d+)/)?.[1];
                    selectPr(null);
                    requestAnimationFrame(() => requestAnimationFrame(() => {
                      const row = prevNum
                        ? [...document.querySelectorAll('.mu-pr-row')].find(x => x.textContent.includes('#' + prevNum))
                        : null;
                      (row ?? document.querySelector('.mu-pr-list'))?.focus?.();
                    }));
                  }}>
                  <button type="button" className="mu-detail-close"
                    aria-label="关闭 PR 详情"
                    onClick={() => {
                      const prevNum = document.querySelector('.mu-pr-row.is-selected')?.textContent.match(/#(\d+)/)?.[1];
                      selectPr(null);
                      requestAnimationFrame(() => requestAnimationFrame(() => {
                        // 重渲染后再取 live 节点（清参后选中行已去高亮——按编号找回）
                        const row = prevNum
                          ? [...document.querySelectorAll('.mu-pr-row')].find(x => x.textContent.includes('#' + prevNum))
                          : null;
                        (row ?? document.querySelector('.mu-pr-list'))?.focus?.();
                      }));
                    }}>关闭详情</button>
                  {selectedPrRef
                    ? <MuPrDetailContent prRef={selectedPrRef} onChanged={refresh} />
                    : <div className="mu-detail-placeholder">
                        <span className="mu-detail-placeholder-glyph" aria-hidden>⌘</span>
                        <span>选择一个 PR 查看详情</span>
                        <span className="muted">审查管线、风险项与操作将在这里展示</span>
                      </div>}
                </div>
              </div>

              {actionMsg ? (
                <Alert className="mu-action-msg" type={actionMsg.ok ? 'success' : 'warning'} showIcon
                  message={`${actionMsg.label} → HTTP ${actionMsg.status}${actionMsg.reason ? `（${actionMsg.reason}）` : ''}`}
                  description={actionMsg.note ?? undefined} />
              ) : null}
            </section>
          ) : ob.stage === 'bind' || ob.stage === 'waiting' ? (
            <section className="section">
              <Typography.Text type="secondary">完成仓库绑定后，PR 会自动同步到这里。</Typography.Text>
            </section>
          ) : null}
        </>
      ) : null}

      <div style={{ marginTop: 16 }}>
        <Button icon={<ReloadOutlined />} loading={loading} onClick={refresh}>立即刷新</Button>
      </div>
    </div>
  );
}
