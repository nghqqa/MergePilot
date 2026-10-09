import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Table, Tag, Typography } from 'antd';
import { useAuth } from '../auth.jsx';
import { STATUS_META } from '../theme.js';
import { fxvMap, fxvArtifactMap, ticketMap, ticketActionMap, toneToColor, muRunMap, MU_RUN } from '../status-map.js';

// 系统状态与接线（antd 版）：五个 Core API 的实时只读视图，供排查"是坏了还是没接"。
// 诚实语义：未登录 401 → 引导；无 DSN → 未接线；连接失败 → 后端错误；成功 → 实时数据。
// 人话标签为主显示，机器值（source/error）入标签内次级文本。
const REFRESH_MS = 10_000;

// 健康数据源集合：legacy PG 读模型与 MU 规范库（Wave 3.7 起 MU 部署返回
// MU_CANONICAL_LIVE）都是"实时数据"——只认 POSTGRESQL_LIVE 会把健康接线误报成错误。
const LIVE_SOURCES = ['POSTGRESQL_LIVE', 'MU_CANONICAL_LIVE'];
const isLive = (s) => LIVE_SOURCES.includes(s);

// MU pending 行的 run 状态（mu-console-api.pending 投影）→ 人话标签。
// PR-2 单源收敛：14 态共享词表在 status-map.js MU_RUN（含 WAITING_FOR_HUMAN_APPROVAL）；
// 本页不再自持 4 键表。legacy 票据域行（ticket status）仍回退 ticketMap；
// 未知枚举 fail-closed（unknownEntry 保留原始值），不吞键。
const runStatusMap = (v) => (v && MU_RUN[String(v).toUpperCase()] ? muRunMap(v) : ticketMap(v));

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
  // rc.10 文案修复：人话标签与内部标识粘连（"MU 实时数据MU_CANONICAL_LIVE"）→
  // 正文语义化（数据源：人话标签（语义副标）），内部标识入 title 技术详情。
  return (
    <Tag color={meta.tone} title={`数据源内部标识：${source}`}>
      数据源：{meta.label}{meta.desc ? `（${meta.desc}）` : ''}
      {error ? <Typography.Text type="danger" style={{ fontSize: 12, marginLeft: 6 }}>{error}</Typography.Text> : null}
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
      const [pulls, pending, tickets, evidence, audit, fxv] = await Promise.all([
        apiGet('/api/pulls'), apiGet('/api/pending'), apiGet('/api/tickets'),
        apiGet('/api/evidence'), apiGet('/api/audit'), Promise.all([apiGet('/api/fxv/attempts'), apiGet('/api/fxv/metrics').catch(() => null)]).then(([a, m]) => ({ ...a, metrics: m })),
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
        error.status === 403 ? (
          // 授权态非故障态：403=角色无此动作（fail-closed 设计），不得渲染为"请求失败"误导用户（2026-10-09 反馈）
          <Alert type="warning" showIcon message="无权限查看"
            description={`当前登录的账号角色没有查看此页的权限（需要平台管理员）。请切换平台管理员账号查看，或联系管理员为当前账号开通权限。`} />
        ) : (
          <Alert type="error" showIcon message="请求失败"
            description={`${error.message}${error.status === 401 ? '（会话可能已过期，请刷新重登）' : ''}`} />
        )
      ) : !data ? (
        <Alert message="加载中…" />
      ) : isLive(src) ? (
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
            locale={{ emptyText: isLive(data.pulls.source) ? '没有 PR/head 记录（诚实零值）' : `数据源=${data.pulls.source}（不伪造记录）` }}
            dataSource={data.pulls.pulls || []}
            columns={[
              { title: '仓库', dataIndex: 'repo', ellipsis: true },
              // legacy 行 pr_number；MU 行 pr（mu-console-api.pulls 投影）——两者都读，缺值诚实 '—'
              { title: 'PR', width: 80,
                render: (_, r) => { const n = r.pr_number ?? r.pr; return n != null ? `#${n}` : '—'; } },
              { title: 'Head', dataIndex: 'head_sha', width: 130, ellipsis: true,
                render: (v) => <span className="sha">{v?.slice(0, 12)}</span> },
              { title: 'Run', dataIndex: 'run_id', ellipsis: true,
                render: (v) => v ? <span className="mono">{v}</span> : '—' },
            ]}
          />
          <Typography.Title level={3} style={{ marginTop: 20 }}>待处理</Typography.Title>
          <Table
            size="small" rowKey="ticket_id" pagination={false}
            dataSource={data.pending.pending || []}
            locale={{ emptyText: '队列为空（诚实零值 — 无伪造门）' }}
            columns={[
              // 票据域（legacy：ticket_id）与 run 域（MU：run_id）同列诚实展示，缺值 '—'
              { title: '票据 / Run', ellipsis: true,
                render: (_, r) => { const id = r.ticket_id ?? r.run_id; return id ? <span className="mono">{id.slice(0, 16)}…</span> : '—'; } },
              { title: '仓库 / PR', ellipsis: true,
                render: (_, r) => `${r.repo} ${r.pr_number != null ? '#' + r.pr_number : r.pr != null ? '#' + r.pr : ''}`.trim() },
              { title: '动作', dataIndex: 'action', width: 130,
                render: (v) => {
                  if (!v) return '—';
                  const m = ticketActionMap(v);
                  return <Tag color={toneToColor(m.tone)} title={m.note}>{m.label}</Tag>;
                } },
              { title: 'TTL', width: 90,
                render: (_, r) => (r.approval_expires_at
                  ? (new Date(r.approval_expires_at) < new Date()
                    ? <Tag color="warning">已过期</Tag> : '有效')
                  : '—') },
              { title: '状态', dataIndex: 'status', width: 110,
                render: (v) => {
                  const m = runStatusMap(v);
                  return <Tag color={toneToColor(m.tone)} title={m.note}>{m.label}</Tag>;
                } },
            ]}
          />
          <Typography.Title level={3} style={{ marginTop: 20 }}>
            {Array.isArray(data.audit.audit) ? '审计事件' : 'Gate 审计'}
          </Typography.Title>
          {Array.isArray(data.audit.audit) ? (
            // MU 域（mu.audit_event 经 /api/audit）：kind/detail 投影，无 gate 决策语义
            <Table
              size="small" rowKey={(r) => `${r.kind}|${r.created_at}|${r.detail ? JSON.stringify(r.detail).length : 0}`} pagination={false}
              dataSource={data.audit.audit}
              locale={{ emptyText: isLive(data.audit.source) ? '没有审计事件（诚实零值）' : `数据源=${data.audit.source ?? '未知'}（不伪造记录）` }}
              columns={[
                { title: '类型', dataIndex: 'kind', width: 200, ellipsis: true,
                  render: (v) => <span className="mono">{v}</span> },
                { title: '详情', ellipsis: true,
                  render: (_, r) => <span className="mono" style={{ fontSize: 12 }}>
                    {r.detail ? JSON.stringify(r.detail).slice(0, 90) : '—'}
                  </span> },
                { title: '时间', dataIndex: 'created_at', width: 170,
                  render: (v) => (v ? new Date(v).toLocaleString() : '') },
              ]}
            />
          ) : (
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
          )}
          <Typography.Title level={3} style={{ marginTop: 20 }}>自动修复编排</Typography.Title>
          {data.fxv?.capability === 'fxv_persistence_not_tenant_scoped' ? (
            <Alert type="info" showIcon style={{ marginBottom: 12 }}
              message="自动修复的全局统计在多用户控制台不展示"
              description="自动修复的记录库未按组织隔离——展示全局统计会跨组织泄露。修复执行记录见「组织与接入」页的 PR 审查管线。" />
          ) : null}
          {data.fxv?.metrics?.alerts?.length ? (
            <Alert type="warning" showIcon style={{ marginBottom: 12 }} message={`自动修复告警：${data.fxv.metrics.alerts.join('；')}`} />
          ) : null}
          {data.fxv?.metrics?.metrics ? (
            <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
              成功率 {data.fxv.metrics.metrics.success_rate ?? '—'} · 失败 {data.fxv.metrics.metrics.failed ?? 0} · 超时 {data.fxv.metrics.metrics.timeouts ?? 0} · digest 漂移 {data.fxv.metrics.metrics.digest_drifts ?? 0} · 并发冲突 {data.fxv.metrics.metrics.concurrent_conflicts ?? 0} · 恢复 {data.fxv.metrics.metrics.recoveries ?? 0} · P50/P95 {data.fxv.metrics.metrics.duration_s?.p50 ?? '—'}/{data.fxv.metrics.metrics.duration_s?.p95 ?? '—'}s
            </Typography.Paragraph>
          ) : null}
          <Table
            size="small" rowKey="attempt_id" pagination={false}
            dataSource={data.fxv?.attempts || []}
            locale={{ emptyText: data.fxv?.source === 'POSTGRESQL_LIVE' ? '没有修复编排记录（诚实零值）' : data.fxv?.capability === 'fxv_persistence_not_tenant_scoped' ? '多用户控制台不展示自动修复全局记录（组织隔离）' : data.fxv?.source === 'FXV_PERSISTENCE_ABSENT' ? '自动修复记录库未初始化（暂无记录）' : `自动修复数据源=${data.fxv?.source || '未知'}（不伪造记录）` }}
            columns={[
              { title: 'Attempt', dataIndex: 'attempt_id', ellipsis: true, render: (v) => <span className="mono">{v}</span> },
              { title: '仓库/分支', ellipsis: true, render: (_, r) => `${r.repo}@${r.branch}` },
              { title: '状态', dataIndex: 'state', width: 150,
                render: (v) => {
                  const m = fxvMap(v);
                  return <Tag color={toneToColor(m.tone)} title={m.note}>{m.label}</Tag>;
                } },
              { title: '最近原因', dataIndex: 'last_reason', ellipsis: true },
              { title: 'Artifact', dataIndex: 'artifact_status', width: 130,
                render: (v) => {
                  const m = fxvArtifactMap(v);
                  return <Tag color={toneToColor(m.tone)} title={m.note}>{m.label}</Tag>;
                } },
              { title: '审计', dataIndex: 'audit_events', width: 70, render: (v) => v ?? 0 },
              { title: '更新', dataIndex: 'updated_at', width: 170,
                render: (v) => (v ? new Date(v).toLocaleString() : '—') },
            ]}
          />
        </>
      )}
    </div>
  );
}
