import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Alert, Spin, Table, Tag, Typography } from 'antd';
import { Column, Line } from '@ant-design/plots';
import { useAuth } from '../auth.jsx';

// 运营总览（默认工作台入口）。全部数据来自 GET /api/overview 的后端权威推导；
// source/机器码/数据来源放详情；无数据/未接线/错误/401 诚实显示；
// 图表不使用 fixture/snapshot 填充。阶段 = 人话标签 + 颜色双表达。
const REFRESH_MS = 30_000;

const STAGE_LABEL = {
  REVIEWING: { label: '审查中', color: 'processing' },
  ACTION_REQUIRED: { label: '需人工处理', color: 'warning' },
  REMEDIATING: { label: '修复中', color: 'processing' },
  VERIFYING: { label: '验证中', color: 'processing' },
  PASSED: { label: '已通过', color: 'success' },
  BLOCKED: { label: '已阻断', color: 'error' },
  STALE: { label: '已过期(head)', color: 'default' },
};
const STAGE_ORDER = ['REVIEWING', 'ACTION_REQUIRED', 'REMEDIATING', 'VERIFYING', 'PASSED', 'BLOCKED', 'STALE'];

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

function SourceDetail({ source, error, generatedAt }) {
  return (
    <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
      <details>
        <summary style={{ cursor: 'pointer' }}>数据来源详情</summary>
        source: <span className="mono">{source}</span>
        {error ? <> · 错误: <span className="mono">{error}</span></> : null}
        {generatedAt ? <> · 生成于 <span className="mono">{new Date(generatedAt).toLocaleString()}</span></> : null}
        · 阶段由后端权威状态推导（票据 / gate 审计 / 回执完整性 / head 排序）
      </details>
    </Typography.Paragraph>
  );
}

export default function OverviewPage() {
  const auth = useAuth();
  const navigate = useNavigate();
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [lastRefresh, setLastRefresh] = useState(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setData(await apiGet('/api/overview'));
      setLastRefresh(new Date().toLocaleTimeString());
    } catch (e) { setError(e); }
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
        <Typography.Title level={1} style={{ fontSize: 24 }}>运营总览</Typography.Title>
        <Alert type="warning" showIcon message="需要登录"
          description="总览数据受服务端会话与仓库 allowlist 保护（未认证返回 401）。" />
      </div>
    );
  }

  if (error) {
    return (
      <div>
        <Typography.Title level={1} style={{ fontSize: 24 }}>运营总览</Typography.Title>
        <Alert type="error" showIcon message="请求失败"
          description={`${error.message}${error.status === 401 ? '（会话可能已过期，请刷新重登）' : ''}`} />
      </div>
    );
  }
  if (!data) return <div style={{ padding: 60, textAlign: 'center' }}><Spin tip="加载中…" /></div>;

  const notWired = data.source === 'BACKEND_NOT_WIRED';
  const isErr = data.source === 'BACKEND_ERROR';
  const totalPrs = data.repository_counts.reduce((n, r) => n + r.prs, 0);
  const totalRuns = data.repository_counts.reduce((n, r) => n + r.runs, 0);

  const stageData = STAGE_ORDER
    .map((s) => ({ stage: STAGE_LABEL[s].label, key: s, count: data.stage_counts[s] || 0 }))
    .filter((d) => d.count > 0 || ['REVIEWING', 'ACTION_REQUIRED', 'PASSED'].includes(d.key));

  const baseCol = {
    height: 220,
    xAxis: { label: { style: { fontSize: 11 } } },
    yAxis: { label: { style: { fontSize: 11 } } },
  };

  return (
    <div>
      <Typography.Title level={1} style={{ fontSize: 24, marginBottom: 4 }}>运营总览</Typography.Title>
      <Typography.Paragraph type="secondary">
        PR 当前阶段、待处理与系统健康。{lastRefresh ? `刷新于 ${lastRefresh} · 每 ${REFRESH_MS / 1000}s` : ''}
      </Typography.Paragraph>
      <SourceDetail source={data.source} error={data.error} generatedAt={data.generated_at} />

      {notWired ? (
        <Alert type="warning" showIcon message="后端未接线"
          description="CONSOLE_PG_DSN 未配置——以下为空状态（诚实零值），不是真实数据。" />
      ) : isErr ? (
        <Alert type="error" showIcon message="后端错误"
          description={<>连接失败：<span className="mono">{data.error}</span>。请检查 PG。</>} />
      ) : (
        <>
          {/* 概要行（文字+数字，克制排版；非装饰大数卡） */}
          <Typography.Paragraph style={{ marginBottom: 16 }}>
            <strong>{totalPrs}</strong> 个 PR · <strong>{totalRuns}</strong> 个运行 ·
            待处理 <strong>{data.pending_summary.count}</strong>
            {data.pending_summary.oldest_wait_minutes != null
              ? <>（最长等待 <strong>{data.pending_summary.oldest_wait_minutes}</strong> 分钟）</> : null}
            {' '}· 异常：stale <strong>{data.incidents.stale_count}</strong> / 失败回执 <strong>{data.incidents.failed_receipts}</strong> / 完整性冲突 <strong>{data.incidents.integrity_conflicts}</strong>
          </Typography.Paragraph>

          {/* 健康状态 */}
          <Typography.Paragraph>
            健康：
            <Tag color={data.health.postgres === 'LIVE' ? 'success' : data.health.postgres === 'ERROR' ? 'error' : 'warning'}>
              {data.health.postgres === 'LIVE' ? 'PG 实时' : data.health.postgres === 'ERROR' ? 'PG 错误' : 'PG 未接线'}
            </Tag>
            <Tag title={data.health.minio.note}>MinIO {data.health.minio.state === 'NOT_WIRED' ? '未接线' : data.health.minio.state}</Tag>
            <Tag color="success">后端只读 OK</Tag>
          </Typography.Paragraph>

          {/* 图表区（移动端纵向 + 容器横向滚动） */}
          <div className="ov-charts" role="region" aria-label="运营图表（键盘可用，柱体可点击跳转）">
            <section aria-label="各阶段 PR 数量（点击柱体跳转待处理）">
              <Typography.Title level={3}>各阶段 PR</Typography.Title>
              <div className="ov-chart-scroll">
                <Column
                  {...baseCol} width={460}
                  data={stageData}
                  xField="stage" yField="count" colorField="stage"
                  onEvent={(chart, event) => {
                    if (event.type === 'element:click') navigate('/pending');
                  }}
                />
              </div>
            </section>
            <section aria-label="最近运行趋势（14 天）">
              <Typography.Title level={3}>最近运行趋势（14 天）</Typography.Title>
              <div className="ov-chart-scroll">
                <Line
                  {...baseCol} width={520}
                  data={data.trend} xField="date" yField="runs"
                  point={{ size: 3 }}
                />
              </div>
            </section>
            <section aria-label="各仓库分布（点击柱体跳转仓库）">
              <Typography.Title level={3}>各仓库分布</Typography.Title>
              <div className="ov-chart-scroll">
                <Column
                  {...baseCol} width={360}
                  data={data.repository_counts}
                  xField="repo" yField="runs" colorField="repo"
                  onEvent={(chart, event) => {
                    if (event.type === 'element:click') navigate('/repos');
                  }}
                />
              </div>
            </section>
          </div>

          {/* PR 阶段表（明细 + 跳转） */}
          <Typography.Title level={3} style={{ marginTop: 20 }}>PR 阶段明细</Typography.Title>
          <Table
            size="small" rowKey={(r) => r.repo + r.run_id + r.head_sha}
            pagination={{ pageSize: 10, hideOnSinglePage: true }}
            dataSource={data.prs}
            locale={{ emptyText: '没有 PR 记录（诚实零值）' }}
            columns={[
              { title: '仓库 / PR', ellipsis: true,
                render: (_, r) => (
                  <a role="link" tabIndex={0} style={{ cursor: 'pointer' }}
                     onClick={() => navigate(`/repos/${r.repo.split('/')[0]}/${r.repo.split('/')[1]}/pr/${r.pr_number}`)}
                     onKeyDown={(e) => { if (e.key === 'Enter') navigate(`/repos/${r.repo.split('/')[0]}/${r.repo.split('/')[1]}/pr/${r.pr_number}`); }}>
                    {r.repo} #{r.pr_number}
                  </a>
                ) },
              { title: 'Head', dataIndex: 'head_sha', width: 120, ellipsis: true,
                render: (v) => <span className="sha">{v?.slice(0, 12) || '—'}</span> },
              { title: 'Run', dataIndex: 'run_id', ellipsis: true,
                render: (v) => v ? <span className="mono">{v}</span> : '—' },
              { title: '阶段', dataIndex: 'stage', width: 130,
                render: (v) => <Tag color={STAGE_LABEL[v]?.color}>{STAGE_LABEL[v]?.label ?? v}</Tag> },
              { title: '阶段来源', dataIndex: 'stage_source', ellipsis: true,
                render: (v) => <span className="mono" style={{ fontSize: 11 }}>{v}</span> },
              { title: '更新时间', dataIndex: 'updated_at', width: 160,
                render: (v) => (v ? <span className="mono">{new Date(v).toLocaleString()}</span> : '—') },
            ]}
          />
          <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8 }}>
            <details><summary style={{ cursor: 'pointer' }}>审计入口</summary>
              gate 审计与票据明细见 <a role="link" tabIndex={0} style={{ cursor: 'pointer' }}
                onClick={() => navigate('/core')} onKeyDown={(e) => { if (e.key === 'Enter') navigate('/core'); }}>系统状态</a>。
              REMEDIATING / VERIFYING 需要 Fixer/Verifier（本部署禁用）——计数恒 0，不虚构。
            </details>
          </Typography.Paragraph>
        </>
      )}
    </div>
  );
}
