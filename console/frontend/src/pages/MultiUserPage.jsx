import { Alert, Button, Input, Select, Space, Table, Tag, Typography } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import React, { useCallback, useEffect, useState } from 'react';
import { readCsrfCookie } from '../api-live.js';

// 多用户面（Developer Edition 最小闭环，MU Phase 4）。
// 数据全部来自 /api/mu/*（真实后端）：当前用户/tenant/角色与动作面、成员只读列表、
// 仓库绑定状态、PR 视图（tenant/repo/head SHA/审查记录/权限态）。
// 按钮只反映权限（disabled 由 /api/mu/session 的 actions 决定）——真正授权一律由
// 后端执行；未启用多用户模式时如实提示，不渲染任何推断数据。
// 视觉沿用现有体系（panel/section/antd Table+Tag），不重做视觉系统。

const ROLE_TONE = {
  contributor: 'default', reviewer: 'blue', maintainer: 'green',
  platform_admin: 'purple', auditor: 'orange',
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

  const refresh = useCallback(async () => {
    setLoading(true); setError(null); setActionMsg(null);
    try {
      const s = await muGet('/api/mu/session');
      if (s.status === 200 && s.body?.user) {
        setSession(s.body); setNotEnabled(false);
        const [m, r] = await Promise.all([muGet('/api/mu/members'), muGet('/api/mu/repositories')]);
        if (m.status === 200) setMembers(m.body?.members ?? []);
        if (r.status === 200) setRepos(r.body?.repositories ?? []);
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
      ok: r.status === 200,
      label,
      status: r.status,
      reason: r.body?.error?.reason ?? null,
      note: r.body?.note ?? (r.status === 200 ? '已受理（后端授权为准）' : null),
    });
    if (r.status === 200) await openPr(path.split('/').slice(0, 5).join('/'));
  }, [openPr]);

  return (
    <div>
      <Typography.Title level={1} style={{ fontSize: 24, marginBottom: 4 }}>多用户（Developer Edition）</Typography.Title>
      <Typography.Paragraph type="secondary">
        多用户 RBAC 最小闭环：会话身份/角色、成员只读视图、仓库绑定状态与 PR 权限态。
        按钮仅反映权限——授权一律由后端执行；GitHub 真实 OAuth/App 接入为未完成项。
      </Typography.Paragraph>

      {notEnabled ? (
        <Alert type="info" showIcon message="多用户面未启用"
          description="MU_MODE != multiuser（当前为 legacy 模式）。启用需设置 MU_MODE=multiuser 并配置 CONSOLE_PG_DSN——本页不显示任何推断数据。" />
      ) : null}

      {error ? (
        <Alert type="error" showIcon message="多用户会话不可用"
          description={`${error.message} — 未登录多用户面或后端异常时本页保持空态（登录入口：POST /api/mu/auth/login）。`} />
      ) : null}

      {session ? (
        <>
          <div className="panel" style={{ padding: 'var(--sp-4)', marginBottom: 16 }}>
            <Space size="large" wrap>
              <span>用户：<b>{session.user?.login}</b></span>
              <span>tenant：<b>{session.tenant?.slug}</b>（{session.tenant?.display_name}
                {session.tenant?.is_migration_tenant ? '，迁移租户' : ''}）</span>
              <span>角色：<Tag color={ROLE_TONE[session.role] ?? 'default'}>{session.role}</Tag></span>
            </Space>
            <div style={{ marginTop: 8 }}>
              动作面（服务端判定）：
              {(session.actions ?? []).map((a) => <Tag key={a}><code>{a}</code></Tag>)}
            </div>
          </div>

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
            <div className="section-head"><h3>仓库绑定</h3></div>
            <Table rowKey="repo_id" size="small" pagination={false} dataSource={repos ?? []}
              columns={[
                { title: '仓库', render: (_, r) => <code>{r.owner}/{r.name}</code> },
                { title: 'provider repo id', dataIndex: 'provider_repo_id', render: (v) => <code>{v}</code> },
                { title: '绑定', render: (_, r) => (r.binding_id
                  ? <Tag color={r.installation_state === 'active' ? 'green' : 'red'}>{r.binding_kind} · {r.installation_state}</Tag>
                  : <Tag>未绑定</Tag>) },
                { title: '权限快照', render: (_, r) => (r.granted_scopes ?? []).map((s) => <Tag key={s}><code>{s}</code></Tag>) },
              ]} />
          </section>

          <section className="section">
            <div className="section-head"><h3>PR 视图</h3></div>
            <Space style={{ marginBottom: 12 }} wrap>
              <Select style={{ minWidth: 260 }} placeholder="选择仓库（会话 tenant 内）"
                value={prQuery.repoId} onChange={(v) => setPrQuery((s) => ({ ...s, repoId: v }))}
                options={(repos ?? []).map((r) => ({ value: r.repo_id, label: `${r.owner}/${r.name}` }))} />
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
                  <span>tenant：<b>{session.tenant?.slug}</b></span>
                  <span>repo：<code>{prDetail.pull_request?.repo_owner}/{prDetail.pull_request?.repo_name}</code></span>
                  <span>head：<code>{prDetail.pull_request?.head_sha}</code></span>
                  <span>protection：<Tag color={prDetail.pull_request?.branch_protection_status === 'known_clean' ? 'green' : 'orange'}>
                    {prDetail.pull_request?.branch_protection_status}</Tag></span>
                </Space>
                <div style={{ marginTop: 8 }}>
                  <Space wrap>
                    <Button size="small" disabled={!can('request_review')}
                      onClick={() => runAction('触发只读审查', `/api/mu/prs/${prDetail.pull_request.pr_id}/review`)}>
                      触发只读审查{!can('request_review') ? '（无权限）' : ''}</Button>
                    <Button size="small" disabled={!can('decide_review')}
                      onClick={() => runAction('审批通过', `/api/mu/prs/${prDetail.pull_request.pr_id}/decision`, { action: 'approve' })}>
                      Approve{!can('decide_review') ? '（无权限）' : ''}</Button>
                    <Button size="small" disabled={!can('decide_review')}
                      onClick={() => runAction('驳回', `/api/mu/prs/${prDetail.pull_request.pr_id}/decision`, { action: 'reject' })}>
                      Reject{!can('decide_review') ? '（无权限）' : ''}</Button>
                    <Button size="small" disabled={!can('request_repair')}
                      onClick={() => runAction('发起受控修复', `/api/mu/prs/${prDetail.pull_request.pr_id}/repair`)}>
                      受控修复{!can('request_repair') ? '（无权限）' : ''}</Button>
                  </Space>
                  <Typography.Paragraph type="secondary" style={{ marginTop: 4, marginBottom: 0 }}>
                    按钮仅反映权限，真正授权由后端执行；protection 非 known_clean 时后端拒绝可合并结论。
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
              </div>
            ) : null}

            {actionMsg ? (
              <Alert style={{ marginTop: 12 }} type={actionMsg.ok ? 'success' : 'warning'} showIcon
                message={`${actionMsg.label} → HTTP ${actionMsg.status}${actionMsg.reason ? `（${actionMsg.reason}）` : ''}`}
                description={actionMsg.note ?? undefined} />
            ) : null}
          </section>
        </>
      ) : null}

      <div style={{ marginTop: 16 }}>
        <Button icon={<ReloadOutlined />} loading={loading} onClick={refresh}>立即刷新</Button>
      </div>
    </div>
  );
}
