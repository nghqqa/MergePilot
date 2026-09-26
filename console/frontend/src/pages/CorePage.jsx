import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Table, Tag, Typography } from 'antd';
import { useAuth } from '../auth.jsx';
import { STATUS_META } from '../theme.js';

// 系统状态与接线（antd 版）：五个 Core API 的实时只读视图，供排查"是坏了还是没接"。
// 诚实语义：未登录 401 → 引导；无 DSN → 未接线；连接失败 → 后端错误；成功 → 实时数据。
// 人话标签为主显示，机器值（source/error）入标签内次级文本。
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

function SourceTag({ source, error }) {
  const meta = STATUS_META[source] || { label: source, tone: 'default' };
  return (
    <Tag color={meta.tone}>
      {meta.label}
      <Typography.Text type="secondary" style={{ fontSize: 11, marginLeft: 6 }}>{source}</Typography.Text>
      {error ? <Typography.Text type="danger" style={{ fontSize: 11, marginLeft: 6 }}>{error}</Typography.Text> : null}
    </Tag>
  );
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
        apiGet('/api/evidence'), apiGet('/api/audit'), apiGet('/api/fxv/attempts').catch(() => ({ source: 'FETCH_ERROR', attempts: [] })),
      ]);
      setData({ pulls, pending, tickets, evidence, audit, fxv });
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
        <Typography.Title level={1} style={{ fontSize: 24 }}>系统状态与接线</Typography.Title>
        <Alert type="warning" showIcon
          message="需要登录"
          description="本页数据受服务端会话与仓库 allowlist 保护（未认证返回 401）。" />
      </div>
    );
  }

  const src = data?.pulls?.source;
  const srcError = data?.pulls?.error;

  return (
    <div>
      <Typography.Title level={1} style={{ fontSize: 24, marginBottom: 4 }}>系统状态与接线</Typography.Title>
      <Typography.Paragraph type="secondary">
        核心 API 接线与健康视图：供排查"是坏了还是没接"，非日常工作流。
        授权仓库：{auth.user?.repos?.join(' · ') || '（allowlist 未配置）'}
        {lastRefresh ? ` · 刷新于 ${lastRefresh}` : ''}
      </Typography.Paragraph>

      {error ? (
        <Alert type="error" showIcon message="请求失败"
          description={`${error.message}${error.status === 401 ? '（会话可能已过期，请刷新重登）' : ''}`} />
      ) : !data ? (
        <Alert message="加载中…" />
      ) : src === 'POSTGRESQL_LIVE' ? (
        <Alert type="success" showIcon message={<SourceTag source={src} />} />
      ) : src === 'BACKEND_NOT_WIRED' ? (
        <Alert type="warning" showIcon message={<SourceTag source={src} />}
          description="CONSOLE_PG_DSN 未配置；以下为空状态，非真实数据。" />
      ) : (
        <Alert type="error" showIcon message={<SourceTag source={src} error={srcError} />}
          description="请检查 PG 连接。" />
      )}

      <Typography.Title level={3} style={{ marginTop: 24 }}>Gate / Bridge 分层</Typography.Title>
      <Typography.Paragraph type="secondary">
        Skill Gate PRODUCE = receipts 齐备有效绑定（不等于安全已批准）；HIGH 人工门由 Bridge 执行 →
        action_required（≠ success）。RAG=EXCLUDED · Fixer/Verifier=DISABLED · merge/push=DISABLED。
      </Typography.Paragraph>

      {data && (
        <>
          <Typography.Title level={3}>PR / Head</Typography.Title>
          <Table
            size="small" rowKey={(r) => r.repo + r.head_sha}
            pagination={false}
            locale={{ emptyText: data.pulls.source === 'POSTGRESQL_LIVE' ? '没有 PR/head 记录（诚实零值）' : `数据源=${data.pulls.source}（不伪造记录）` }}
            dataSource={data.pulls.pulls || []}
            columns={[
              { title: '仓库', dataIndex: 'repo', ellipsis: true },
              { title: 'PR', dataIndex: 'pr_number', width: 80,
                render: (v) => (v != null ? `#${v}` : '—') },
              { title: 'Head', dataIndex: 'head_sha', width: 130, ellipsis: true,
                render: (v) => <span className="sha">{v?.slice(0, 12)}</span> },
              { title: 'Run', dataIndex: 'run_id', ellipsis: true,
                render: (v) => <span className="mono">{v}</span> },
            ]}
          />
          <Typography.Title level={3} style={{ marginTop: 20 }}>待处理</Typography.Title>
          <Table
            size="small" rowKey="ticket_id" pagination={false}
            dataSource={data.pending.pending || []}
            locale={{ emptyText: '队列为空（诚实零值 — 无伪造门）' }}
            columns={[
              { title: '票据', dataIndex: 'ticket_id', ellipsis: true, render: (v) => <span className="mono">{v?.slice(0, 16)}…</span> },
              { title: '仓库 / PR', ellipsis: true,
                render: (_, r) => `${r.repo} ${r.pr_number != null ? '#' + r.pr_number : ''}` },
              { title: '动作', dataIndex: 'action', width: 130 },
              { title: 'TTL', width: 90,
                render: (_, r) => (r.approval_expires_at && new Date(r.approval_expires_at) < new Date()
                  ? <Tag color="warning">已过期</Tag> : '有效') },
              { title: '状态', dataIndex: 'status', width: 110,
                render: (v) => <Tag color={STATUS_META[v]?.tone}>{STATUS_META[v]?.label ?? v}</Tag> },
            ]}
          />
          <Typography.Title level={3} style={{ marginTop: 20 }}>Gate 审计</Typography.Title>
          <Table
            size="small" rowKey="run_id" pagination={false}
            locale={{ emptyText: data.audit.core_source === 'POSTGRESQL_LIVE' ? (data.audit.audit_table === 'missing' ? '审计表缺失（skill_gate_audit）——如实报告，不冒充零决策' : '没有 gate 决策记录（诚实零值）') : `数据源=${data.audit.core_source}（不伪造记录）` }}
            dataSource={data.audit.gate_decisions || []}
            columns={[
              { title: 'Run', dataIndex: 'run_id', ellipsis: true, render: (v) => <span className="mono">{v}</span> },
              { title: '决策', ellipsis: true,
                render: (_, r) => <span className="mono">{JSON.stringify(r.decision).slice(0, 90)}</span> },
              { title: '时间', dataIndex: 'created_at', width: 170,
                render: (v) => (v ? new Date(v).toLocaleString() : '') },
            ]}
          />
          <Typography.Title level={3} style={{ marginTop: 20 }}>FXV 修复编排</Typography.Title>
          <Table
            size="small" rowKey="attempt_id" pagination={false}
            dataSource={data.fxv?.attempts || []}
            locale={{ emptyText: data.fxv?.source === 'POSTGRESQL_LIVE' ? '没有修复编排记录（诚实零值）' : `FXV 数据源=${data.fxv?.source || '未知'}（不伪造记录）` }}
            columns={[
              { title: 'Attempt', dataIndex: 'attempt_id', ellipsis: true, render: (v) => <span className="mono">{v}</span> },
              { title: '仓库/分支', ellipsis: true, render: (_, r) => `${r.repo}@${r.branch}` },
              { title: '状态', dataIndex: 'state', width: 150,
                render: (v) => <Tag color={STATUS_META[v]?.tone || 'default'}>{STATUS_META[v]?.label ?? v}</Tag> },
              { title: '最近原因', dataIndex: 'last_reason', ellipsis: true },
              { title: '更新', dataIndex: 'updated_at', width: 170,
                render: (v) => (v ? new Date(v).toLocaleString() : '—') },
            ]}
          />
        </>
      )}
    </div>
  );
}
