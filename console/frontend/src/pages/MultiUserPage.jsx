import { Alert, Button, Input, Select, Space, Steps, Table, Tag, Typography } from 'antd';
import { ReloadOutlined, CheckCircleOutlined, CloseCircleOutlined } from '@ant-design/icons';
import React, { useCallback, useEffect, useState } from 'react';
import { readCsrfCookie } from '../api-live.js';

// 多用户面（Developer Edition Beta onboarding）。
// 引导步骤：登录 → 接受邀请 → 安装 GitHub App → 选择 installation → 选择仓库 →
// 完成绑定 → 返回 PR 审查视图。所有状态使用后端 API reason / configured:false 语义；
// 按钮仅反映权限，授权一律由后端执行。不显示 token/private key/webhook secret。

const ROLE_TONE = {
  contributor: 'default', reviewer: 'blue', maintainer: 'green',
  platform_admin: 'purple', auditor: 'orange',
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
  repository_already_bound: '此仓库已被其他租户绑定——每个仓库只允许一个租户',
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

// ── Onboarding Steps 计算 ──
function computeOnboardingSteps({ session, providers, ghStatus, installations, repos }) {
  if (!session) return { current: 0, steps: [
    { title: '登录', description: '使用 GitHub OAuth 登录' },
    { title: '成员资格', description: '接受邀请获得角色' },
    { title: '安装 GitHub App', description: '只读接入你的仓库' },
    { title: '绑定仓库', description: '选择 installation 与仓库' },
    { title: 'PR 审查', description: '开始使用' },
  ]};
  const hasGhApp = ghStatus?.configured === true;
  const hasInstallation = (installations ?? []).some((i) => !i.revoked && !i.suspended);
  const hasBinding = (repos ?? []).some((r) => r.binding_id && r.installation_state === 'active');
  const current = hasBinding ? 4 : hasInstallation ? 3 : hasGhApp ? 2 : session ? 1 : 0;
  return { current, steps: [
    { title: '登录', description: session ? `✓ ${session.user?.login}` : '未登录' },
    { title: '成员资格', description: session ? `✓ ${session.role}` : '需邀请' },
    { title: '安装 GitHub App', description: hasGhApp
      ? hasInstallation ? '✓ 已安装' : '点击安装' : providers?.github?.configured !== false ? '未配置' : '未配置' },
    { title: '绑定仓库', description: hasBinding ? '✓ 已绑定' : hasInstallation ? '选择仓库' : '需先安装' },
    { title: 'PR 审查', description: hasBinding ? '✓ 就绪' : '待完成' },
  ]};
}

// ── 审查管线面板（Wave 3 PR-E：只读展示 review_run 全链状态）──
// 九态人话 + attempt/retry + finding 定位 + fix dry-run/verifier verdict + blocked 原因。
// 无 approve/merge/write 按钮（人工审批仍走上方既有决策区）。
const PIPE_LABEL = {
  RECEIVED: '已接收', REVIEW_QUEUED: '审查排队', REVIEWING: '审查中', REVIEWED: '已审查',
  FIX_QUEUED: '修复排队', FIXING: '修复预演', VERIFY_QUEUED: '验证排队', VERIFYING: '验证中',
  VERIFIED: '已验证', REWORK_REQUIRED: '需返工', BLOCKED: '受阻', FAILED: '失败', COMPLETED: '已完成',
};
const SEV_TONE = { P0: 'red', P1: 'volcano', P2: 'orange', P3: 'gold' };

function PipelinePanel({ prNumber, repoId }) {
  const [detail, setDetail] = useState(null);
  const [noRun, setNoRun] = useState(false);
  useEffect(() => {
    if (!prNumber || !repoId) return;
    setDetail(null); setNoRun(false);
    (async () => {
      try {
        const lr = await fetch(`/api/mu/runs?repo_id=${encodeURIComponent(repoId)}`, { credentials: 'same-origin' })
          .then((r) => r.json().catch(() => null));
        const run = (lr?.runs ?? []).find((r) => Number(r.provider_pr_number) === Number(prNumber));
        if (!run) { setNoRun(true); return; }
        const d = await fetch(`/api/mu/runs/${run.run_id}`, { credentials: 'same-origin' })
          .then((r) => r.json().catch(() => null));
        setDetail(d);
      } catch { setNoRun(true); }
    })();
  }, [prNumber, repoId]);
  if (noRun) return <Alert style={{ marginTop: 12 }} type="info" showIcon
    message="审查管线：该 PR 暂无自动审查运行（webhook 触发后自动开始）" />;
  if (!detail?.run) return null;
  const r = detail.run;
  return (
    <div style={{ marginTop: 16 }}>
      <Typography.Title level={5} style={{ marginBottom: 8 }}>审查管线（自动化 Agent 运行）</Typography.Title>
      <Space size="large" wrap>
        <Tag color={r.status === 'COMPLETED' ? 'green' : ['BLOCKED', 'FAILED'].includes(r.status) ? 'red' : 'blue'}>
          {PIPE_LABEL[r.status] ?? r.status}
        </Tag>
        <span>触发：<Tag>{r.trigger_source === 'manual' ? '手动' : 'GitHub 事件'}</Tag></span>
        <span>head：<code>{String(r.head_sha ?? '').slice(0, 12)}</code></span>
        {(detail.dead_letters ?? []).length > 0 ? (
          <Tag color="red">受阻原因：{detail.dead_letters[0].reason}</Tag>) : null}
      </Space>
      {(detail.findings ?? []).length > 0 ? (
        <Table style={{ marginTop: 8 }} rowKey={(f) => `${f.rule_id}-${f.path}-${f.line_start}`}
          size="small" pagination={false} dataSource={detail.findings}
          columns={[
            { title: '级别', dataIndex: 'severity', width: 60,
              render: (v) => <Tag color={SEV_TONE[v] ?? 'default'}>{v}</Tag> },
            { title: '规则', dataIndex: 'rule_id', render: (v) => <code>{v}</code> },
            { title: '位置', render: (f) => <code>{f.path}{f.line_start ? `:${f.line_start}` : ''}</code> },
            { title: '摘要', dataIndex: 'summary_masked', ellipsis: true },
            { title: '建议', dataIndex: 'remediation', ellipsis: true },
          ]} />
      ) : <Typography.Text type="secondary" style={{ fontSize: 12 }}>未发现风险项。</Typography.Text>}
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8, marginBottom: 0 }}>
        Agent 执行：{(detail.attempts ?? []).map((a) =>
          `${a.agent_role}#${a.attempt}(${a.status})`).join(' · ') || '—'}
        {(detail.fixes ?? []).length ? `；修复预演：${detail.fixes.map((f) => `${f.status}`).join('/')}` : ''}
        {(detail.verifications ?? []).length ? `；验证：${detail.verifications.map((v) => v.verdict).join('/')}` : ''}
        。修复为 dry-run（不写 GitHub）；AI 审查不构成 GitHub required review。
      </Typography.Paragraph>
    </div>
  );
}

// ── GitHub App 面板（含仓库列表与绑定操作） ──
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
    // 先在本地创建 repo 记录，再绑定
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
              <Button size="small" type="primary" onClick={async () => {
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
              description="点击上方「安装 GitHub App」跳转到 GitHub 完成安装，授权你的测试仓库。" style={{ marginBottom: 12 }} />
          ) : null}

          {loadingRepos ? <Typography.Text type="secondary">加载授权仓库列表…</Typography.Text> : null}

          {reposForInst !== null ? (
            <Table rowKey="id" size="small" pagination={false} dataSource={reposForInst}
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
              <Table rowKey="repo_id" size="small" pagination={false}
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

export default function MultiUserPage() {
  const [session, setSession] = useState(null);
  const [notEnabled, setNotEnabled] = useState(false);
  const [error, setError] = useState(null);
  const [members, setMembers] = useState(null);
  const [repos, setRepos] = useState(null);
  const [prQuery, setPrQuery] = useState({ repoId: null, number: '' });
  const [prList, setPrList] = useState(null);
  const [prDetail, setPrDetail] = useState(null);
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
        if (gs?.status === 200) {
          setGhStatus(gs.body);
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

  const searchPrs = useCallback(async () => {
    setPrDetail(null); setPrList(null);
    if (!prQuery.repoId || !prQuery.number) return;
    const r = await muGet(`/api/mu/prs?repo_id=${encodeURIComponent(prQuery.repoId)}&number=${encodeURIComponent(prQuery.number)}`);
    if (r.status === 200) setPrList(r.body?.pull_requests ?? []);
  }, [prQuery]);

  const openPr = useCallback(async (prId) => {
    setActionMsg(null);
    const d = await muGet(`/api/mu/prs/${prId}`);
    if (d.status === 200) setPrDetail(d.body);
  }, []);

  const runAction = useCallback(async (label, path, payload) => {
    setActionMsg(null);
    const r = await muPost(path, payload);
    setActionMsg({
      ok: r.status === 200, label, status: r.status,
      reason: r.body?.error?.reason ?? null,
      note: r.body?.note ?? (r.status === 200 ? '已受理（后端授权为准）' : null),
    });
    if (r.status === 200) await openPr(path.split('/').slice(0, 5).join('/'));
  }, [openPr]);

  const onboarding = computeOnboardingSteps({ session, providers, ghStatus, installations, repos });
  const loginError = new URLSearchParams(window.location.search).get('mu_login_error');
  const ghappError = new URLSearchParams(window.location.search).get('ghapp_error');

  return (
    <div>
      <Typography.Title level={1} style={{ fontSize: 24, marginBottom: 4 }}>
        多用户工作台（Developer Edition Beta）
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

      {/* Onboarding Steps */}
      {!notEnabled ? (
        <div className="panel" style={{ padding: 'var(--sp-4)', marginBottom: 16 }}>
          <Steps size="small" current={onboarding.current} items={onboarding.steps} />
        </div>
      ) : null}

      {/* 登录盒 */}
      {!session && !notEnabled && !error ? (
        <div className="panel" style={{ padding: 'var(--sp-4)', marginBottom: 16 }}>
          <Space direction="vertical" size="small">
            <b>登录多用户面</b>
            <Button type="primary" disabled={providers?.github?.configured !== true}
              onClick={async () => {
                const r = await muGet('/api/mu/auth/oauth/github/start');
                if (r.status === 200 && r.body?.authorize_url) window.location.href = r.body.authorize_url;
              }}>
              使用 GitHub 登录
            </Button>
            {providers?.github && providers.github.configured === false ? (
              <Typography.Text type="secondary">
                GitHub OAuth 未配置（configured:false，fail-closed）——需 MU_GITHUB_OAUTH_CLIENT_ID/_CLIENT_SECRET/_CALLBACK_URL。
                参见 BETA-GUIDE §5。
              </Typography.Text>
            ) : null}
            {providers?.fixture?.configured ? <Typography.Text type="secondary">fixture 登录通道开启（测试配置）。</Typography.Text> : null}
            {loginError ? (
              <Alert type="warning" showIcon message={`登录未完成：${LOGIN_ERROR_MAP[loginError] ?? loginError}`} />
            ) : null}
          </Space>
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
              <span>tenant：<b>{session.tenant?.slug}</b></span>
              <span>角色：<Tag color={ROLE_TONE[session.role] ?? 'default'}>{session.role}</Tag></span>
            </Space>
            <div style={{ marginTop: 8 }}>
              动作面（服务端判定）：{(session.actions ?? []).map((a) => <Tag key={a}><code>{a}</code></Tag>)}
            </div>
          </div>

          <section className="section">
            <div className="section-head"><h3>GitHub App 与仓库绑定</h3></div>
            <GHAppPanel can={can} session={session} repos={repos}
              onBound={refresh} onUnbound={refresh} />
          </section>

          <section className="section">
            <div className="section-head"><h3>成员（只读）</h3></div>
            <Table rowKey="membership_id" size="small" pagination={false} dataSource={members ?? []}
              columns={[
                { title: '成员', dataIndex: 'login' },
                { title: '角色', dataIndex: 'role', render: (v) => <Tag color={ROLE_TONE[v] ?? 'default'}>{v}</Tag> },
                { title: '状态', dataIndex: 'state' },
                { title: '加入时间', dataIndex: 'created_at', render: (v) => String(v ?? '').slice(0, 19).replace('T', ' ') },
              ]} />
          </section>

          <section className="section">
            <div className="section-head"><h3>PR 审查</h3></div>
            {(repos ?? []).filter((r) => r.binding_id && r.installation_state === 'active').length === 0 ? (
              <Alert type="info" showIcon message="尚无已绑定的活跃仓库"
                description="请先在上方 GitHub App 面板完成安装与仓库绑定，PR 将自动同步。" />
            ) : (
              <>
                <Space style={{ marginBottom: 12 }} wrap>
                  <Select style={{ minWidth: 260 }} placeholder="选择仓库"
                    value={prQuery.repoId} onChange={(v) => setPrQuery((s) => ({ ...s, repoId: v }))}
                    options={(repos ?? []).filter((r) => r.binding_id).map((r) => ({ value: r.repo_id, label: `${r.owner}/${r.name}` }))} />
                  <Input style={{ width: 140 }} placeholder="PR number" value={prQuery.number}
                    onChange={(e) => setPrQuery((s) => ({ ...s, number: e.target.value }))} />
                  <Button onClick={searchPrs}>查询</Button>
                </Space>

                {prList ? (
                  <Table rowKey="pr_id" size="small" pagination={false} dataSource={prList}
                    onRow={(r) => ({ onClick: () => openPr(r.pr_id), style: { cursor: 'pointer' } })}
                    columns={[
                      { title: 'PR', dataIndex: 'provider_pr_number' },
                      { title: 'head SHA', dataIndex: 'head_sha', render: (v) => <code>{String(v).slice(0, 12)}</code> },
                      { title: 'branch protection', dataIndex: 'branch_protection_status',
                        render: (v) => <Tag color={v === 'known_clean' ? 'green' : 'orange'}>{v}</Tag> },
                      { title: '更新', dataIndex: 'updated_at', render: (v) => String(v ?? '').slice(0, 19).replace('T', ' ') },
                    ]} />
                ) : null}

                {prDetail ? (
                  <div className="panel" style={{ padding: 'var(--sp-4)', marginTop: 12 }}>
                    <Space size="large" wrap>
                      <span>repo：<code>{prDetail.pull_request?.repo_owner}/{prDetail.pull_request?.repo_name}</code></span>
                      <span>head：<code>{prDetail.pull_request?.head_sha?.slice(0, 12)}</code></span>
                      <span>protection：<Tag color={prDetail.pull_request?.branch_protection_status === 'known_clean' ? 'green' : 'orange'}>
                        {prDetail.pull_request?.branch_protection_status}</Tag></span>
                    </Space>
                    <div style={{ marginTop: 8 }}>
                      <Space wrap>
                        <Button size="small" disabled={!can('request_review')}
                          onClick={() => runAction('触发只读审查', `/api/mu/prs/${prDetail.pull_request.pr_id}/review`)}>
                          触发只读审查{!can('request_review') ? '（需 Reviewer）' : ''}</Button>
                        <Button size="small" disabled={!can('decide_review')}
                          onClick={() => runAction('审批通过', `/api/mu/prs/${prDetail.pull_request.pr_id}/decision`, { action: 'approve' })}>
                          Approve{!can('decide_review') ? '（需 Maintainer）' : ''}</Button>
                        <Button size="small" disabled={!can('decide_review')} danger
                          onClick={() => runAction('驳回', `/api/mu/prs/${prDetail.pull_request.pr_id}/decision`, { action: 'reject' })}>
                          Reject{!can('decide_review') ? '（需 Maintainer）' : ''}</Button>
                        <Button size="small" disabled={!can('request_repair')}
                          onClick={() => runAction('发起受控修复', `/api/mu/prs/${prDetail.pull_request.pr_id}/repair`)}>
                          受控修复{!can('request_repair') ? '（需 Maintainer）' : ''}</Button>
                      </Space>
                      <Typography.Paragraph type="secondary" style={{ marginTop: 4, marginBottom: 0 }}>
                        按钮仅反映权限；protection 非 known_clean 时后端拒绝可合并结论（fail-closed）。
                      </Typography.Paragraph>
                    </div>
                    <Table style={{ marginTop: 12 }} rowKey="review_id" size="small" pagination={false}
                      dataSource={prDetail.review_records ?? []}
                      columns={[
                        { title: 'kind', dataIndex: 'kind', render: (v) => <code>{v}</code> },
                        { title: 'decision', dataIndex: 'decision' },
                        { title: 'actor', dataIndex: 'actor_login' },
                        { title: 'head', dataIndex: 'head_sha', render: (v) => <code>{String(v).slice(0, 10)}</code> },
                        { title: 'protection', dataIndex: 'branch_protection_status' },
                        { title: '时间', dataIndex: 'created_at', render: (v) => String(v ?? '').slice(0, 19).replace('T', ' ') },
                      ]} />
                    <PipelinePanel prNumber={Number(prDetail.pull_request?.provider_pr_number)}
                      repoId={prQuery.repoId} />
                  </div>
                ) : null}

                {actionMsg ? (
                  <Alert style={{ marginTop: 12 }} type={actionMsg.ok ? 'success' : 'warning'} showIcon
                    message={`${actionMsg.label} → HTTP ${actionMsg.status}${actionMsg.reason ? `（${actionMsg.reason}）` : ''}`}
                    description={actionMsg.note ?? undefined} />
                ) : null}
              </>
            )}
          </section>
        </>
      ) : null}

      <div style={{ marginTop: 16 }}>
        <Button icon={<ReloadOutlined />} loading={loading} onClick={refresh}>立即刷新</Button>
      </div>
    </div>
  );
}
