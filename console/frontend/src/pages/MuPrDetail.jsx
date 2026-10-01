// MultiUser PrDetail 分支：复用 MultiUserPage 的详情/操作/管线（Wave 3.15 拆页）。
// 数据：/api/mu/prs/:prId（编号双寻址）+ PipelinePanel（/api/mu/runs）。
import React, { useCallback, useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { Alert, Button, Space, Table, Tag, Typography } from 'antd';
import { PipelinePanel } from './MultiUserPage.jsx';

export function MuPrDetail() {
  const params = useParams();
  const owner = params.owner ?? '';
  const name = params.name ?? '';
  const prNumber = Number(params.prNumber);
  const [detail, setDetail] = useState(null);
  const [state, setState] = useState('loading');
  const [actionMsg, setActionMsg] = useState(null);
  const [repoId, setRepoId] = useState(null);

  const load = useCallback(async () => {
    setState('loading');
    try {
      const repos = await fetch('/api/mu/repositories', { credentials: 'same-origin' }).then((r) => r.json());
      const repo = (repos?.repositories ?? []).find((r) => r.owner === owner && r.name === name);
      if (!repo) { setState('repo_not_found'); return; }
      setRepoId(repo.repo_id);
      const d = await fetch(`/api/mu/prs/${prNumber}?repo_id=${repo.repo_id}`, { credentials: 'same-origin' }).then((r) => r.json());
      if (d?.pull_request) { setDetail(d); setState('ready'); }
      else setState('not_found');
    } catch { setState('error'); }
  }, [owner, name, prNumber]);

  useEffect(() => { load(); }, [load]);

  const runAction = async (label, path, payload) => {
    setActionMsg(null);
    const csrf = (document.cookie.match(/(?:^|; )mp_csrf=([^;]*)/) ?? [])[1] ?? '';
    const r = await fetch(path, { method: 'POST', credentials: 'same-origin',
      headers: { 'content-type': 'application/json', 'X-CSRF-Token': csrf },
      body: JSON.stringify(payload ?? {}) });
    const body = await r.json().catch(() => null);
    setActionMsg({ ok: r.status === 200, label, status: r.status,
      reason: body?.error?.reason ?? null });
    if (r.status === 200) await load();
  };

  const backTo = `/multiuser`;

  if (state === 'loading') return <Typography.Text type="secondary">加载 PR 详情…</Typography.Text>;
  if (state === 'repo_not_found' || state === 'not_found') {
    return (
      <div>
        <div className="breadcrumb"><Link className="crumb-back" to={backTo}>返回组织与接入</Link></div>
        <div className="state-box state-warn">
          未找到 {owner}/{name} PR #{prNumber} 的记录——不回退其他数据源（404 如实）。
        </div>
      </div>
    );
  }
  if (state === 'error') {
    return (
      <div>
        <div className="breadcrumb"><Link className="crumb-back" to={backTo}>返回组织与接入</Link></div>
        <ErrorBoxRetry onRetry={load} />
      </div>
    );
  }

  const pr = detail.pull_request;
  return (
    <div>
      <div className="breadcrumb"><Link className="crumb-back" to={backTo}>返回组织与接入</Link></div>
      <h1 className="mono">{owner}/{name} #{pr.provider_pr_number}</h1>
      <Space size="large" wrap style={{ marginBottom: 8 }}>
        <span>head：<code>{String(pr.head_sha ?? '').slice(0, 12)}</code></span>
        <span>protection：<Tag color={pr.branch_protection_status === 'known_clean' ? 'green' : 'orange'}>
          {pr.branch_protection_status}</Tag></span>
        {pr.title ? <span className="muted">{String(pr.title).slice(0, 60)}</span> : null}
      </Space>

      <Space wrap style={{ marginBottom: 12 }}>
        <Button size="small" onClick={() => runAction('触发只读审查', `/api/mu/prs/${pr.pr_id}/review`)}>触发只读审查</Button>
        <Button size="small" onClick={() => runAction('审批通过', `/api/mu/prs/${pr.pr_id}/decision`, { action: 'approve' })}>审批通过</Button>
        <Button size="small" onClick={() => runAction('驳回', `/api/mu/prs/${pr.pr_id}/decision`, { action: 'reject' })}>驳回</Button>
        <Button size="small" onClick={() => runAction('发起受控修复', `/api/mu/prs/${pr.pr_id}/repair`)}>发起受控修复</Button>
      </Space>
      {actionMsg ? (
        <Alert style={{ marginBottom: 12 }} type={actionMsg.ok ? 'success' : 'warning'} showIcon
          message={`${actionMsg.label} → HTTP ${actionMsg.status}${actionMsg.reason ? `（${actionMsg.reason}）` : ''}`} />
      ) : null}

      {(detail.review_records ?? []).length > 0 ? (
        <>
          <Typography.Title level={5}>审查记录（人工操作历史）</Typography.Title>
          <Table size="small" rowKey="review_id" pagination={false}
            dataSource={detail.review_records ?? []}
            columns={[
              { title: 'kind', dataIndex: 'kind', render: (v) => <code>{v}</code> },
              { title: 'decision', dataIndex: 'decision' },
              { title: 'actor', dataIndex: 'actor_login' },
              { title: 'head', dataIndex: 'head_sha', render: (v) => <code>{String(v).slice(0, 10)}</code> },
              { title: 'protection', dataIndex: 'branch_protection_status' },
              { title: '时间', dataIndex: 'created_at', render: (v) => String(v ?? '').slice(0, 19).replace('T', ' ') },
            ]} />
        </>
      ) : (
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          审查记录（人工操作）暂无——上方"审批/审查"按钮的操作会记录在这里。
        </Typography.Paragraph>
      )}

      <PipelinePanel prNumber={Number(pr.provider_pr_number)} repoId={repoId} />
    </div>
  );
}

function ErrorBoxRetry({ onRetry }) {
  return (
    <div className="state-box state-warn">
      读取失败，可重试。
      <Button size="small" style={{ marginLeft: 8 }} onClick={onRetry}>重试</Button>
    </div>
  );
}
