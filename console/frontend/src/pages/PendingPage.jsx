import React, { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight, ArrowUpRight, Inbox, Search } from 'lucide-react';
import { Alert, Button, Input, Select, Table, Tag, Typography } from 'antd';
import { useAppConfig } from '../App.jsx';
import { fetchRunsSnapshotOnce, useDataSource, useSourceQuery } from '../hooks.js';
import { groupRunsByPr } from '../pr-model.js';
import { fmtTime } from '../format.js';
import { Empty, ErrorBox, SkeletonRows } from '../ui.jsx';

// 工作队列：需要人工处理的事项集中在这一页。
// 口径（人话）：待决策的审查发现 / 失败或卡住的执行 / 等待审批的票据。
// 历史 HIGH 只代表"最近一次记录"，不自动生成当前待办（无权威当前 head 时如实说明）。

const SEV_LABEL = { HIGH: '高', MEDIUM: '中', LOW: '低' };
const KIND_LABEL = {
  approval: '审批待办',
  review: '审查发现',
  execution: '执行异常',
  gate: '门已拒绝',
  publish: '回写需核对',
};

function snapshotQueue(prs) {
  const items = [];
  for (const pr of prs) {
    const r = pr.latest;
    if (!r) continue;
    const base = {
      id: r.run_id ?? r.pack_id,
      repo: pr.repo, prNumber: pr.prNumber,
      title: pr.title ?? `PR #${pr.prNumber}`,
      href: pr.href ?? `/repos/${encodeURIComponent(pr.owner)}/${encodeURIComponent(pr.name)}/pr/${pr.prNumber}`,
      at: r.created_at ?? null,
    };
    const v = String(r.review?.verdict ?? '').toUpperCase();
    if (v === 'FINDING_CONFIRMED') {
      items.push({ ...base, kind: 'review', kindLabel: KIND_LABEL.review,
        severity: String(r.review?.severity ?? '').toUpperCase() || '未分级',
        statusLabel: `发现待决策（${r.review?.severity ?? '未分级'}）` });
    }
    if (String(r.execution?.status ?? '').toUpperCase() === 'ERROR') {
      items.push({ ...base, id: base.id + ':exec', kind: 'execution', kindLabel: KIND_LABEL.execution,
        severity: null, statusLabel: '执行出错，需人工处理' });
    }
    if (String(r.review?.human_gate ?? '').toUpperCase() === 'REJECTED') {
      items.push({ ...base, id: base.id + ':gate', kind: 'gate', kindLabel: KIND_LABEL.gate,
        severity: null, statusLabel: '修复已被人工拒绝（流程受控停止）' });
    }
  }
  return items;
}

function pgQueue(items) {
  return (items ?? []).map((t) => ({
    id: t.ticket_id,
    kind: 'approval', kindLabel: KIND_LABEL.approval,
    repo: t.repo ?? '未记录',
    prNumber: null, title: `${t.action}（run ${t.run_id ?? '未记录'}）`,
    href: '/approvals',
    severity: null,
    statusLabel: '等待审批（隔离联调库）',
    at: null,
    fixture: true,
  }));
}

function contractQueue(prs) {
  return (prs ?? [])
    .filter((v) => (v.attention?.flag === 'pending_tickets') || v.hasPendingTickets > 0)
    .map((v) => ({
      id: v.key, kind: 'approval', kindLabel: KIND_LABEL.approval,
      repo: v.repo, prNumber: v.prNumber, title: v.title ?? `PR #${v.prNumber}`,
      href: v.href ?? '#', severity: null,
      statusLabel: '有待审批票据（后端权威）', at: v.activityAt, fixture: false,
    }));
}

function matches(it, f) {
  if (f.repo && it.repo !== f.repo) return false;
  if (f.kind && it.kind !== f.kind) return false;
  if (f.severity) {
    const s = String(it.severity ?? '未分级');
    if (s !== f.severity) return false;
  }
  if (f.q) {
    const needle = f.q.trim().toLowerCase();
    const hay = [it.title, it.repo, it.prNumber != null ? `#${it.prNumber}` : '', it.statusLabel]
      .join(' ').toLowerCase();
    if (!hay.includes(needle.toLowerCase())) return false;
  }
  return true;
}

export default function PendingPage() {
  const config = useAppConfig();
  const { source } = useDataSource(config);
  const kind = source.kind;
  const [attempt, setAttempt] = useState(0);
  const [filters, setFilters] = useState({ q: '', repo: '', severity: '', kind: '' });
  const setF = (k, v) => setFilters((s) => ({ ...s, [k]: v }));

  const load = async () => {
    if (kind === 'console-pg') {
      const r = await source.listApprovals();
      return pgQueue(r?.items);
    }
    if (kind === 'contract') {
      const repos = await source.listRepos();
      const out = [];
      for (const r of repos) {
        const prs = await source.listPrs(r.repo);
        out.push(...contractQueue(prs));
      }
      return out;
    }
    // snapshot
    const data = await fetchRunsSnapshotOnce();
    return snapshotQueue(groupRunsByPr(data?.items ?? []));
  };

  const q = useSourceQuery(load, [source, kind, attempt]);

  const items = q.status === 'done' ? q.data ?? [] : [];
  const repoOptions = [...new Set(items.map((i) => i.repo))];
  const filtered = items.filter((i) => matches(i, filters));
  filtered.sort((a, b) => {
    const sev = (x) => (x.severity === 'HIGH' ? 3 : x.severity === 'MEDIUM' ? 2 : x.severity === 'LOW' ? 1 : 0);
    return (sev(b) - sev(a)) || String(b.at ?? '').localeCompare(String(a.at ?? ''));
  });

  const setFResetPage = (k, v) => setF(k, v);

  return (
    <div>
      <Typography.Title level={1} style={{ fontSize: 24, marginBottom: 4 }}>待处理</Typography.Title>
      <Typography.Paragraph type="secondary">
        需要人工处理的事项：等待决策的审查发现、失败或卡住的执行、等待审批的票据。
        历史记录里的旧结论不会出现在这里。
      </Typography.Paragraph>

      {q.status === 'error' ? (
        kind !== 'snapshot' && q.error?.status === 404 ? (
          <Alert type="warning" showIcon message="当前数据源没有待办数据（404）——不回退到历史快照。"
            description={<Link to="/repos">返回仓库</Link>} />
        ) : (
          <ErrorBox error={q.error} onRetry={() => setAttempt((n) => n + 1)} />
        )
      ) : q.status !== 'done' ? (
        <SkeletonRows rows={5} cols={5} />
      ) : (
        <>
          <div className="filter-bar" role="search" aria-label="待处理事项筛选" style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 12 }}>
            <Input allowClear placeholder="仓库 / PR / 状态" prefix={<Search size={14} aria-hidden />}
                   value={filters.q} onChange={(e) => setFResetPage('q', e.target.value)}
                   aria-label="搜索待处理事项" style={{ maxWidth: 280 }} />
            {repoOptions.length > 1 ? (
              <Select allowClear placeholder="全部仓库" value={filters.repo || undefined}
                      onChange={(v) => setFResetPage('repo', v ?? '')} aria-label="按仓库筛选"
                      style={{ minWidth: 180 }}
                      options={repoOptions.map((r) => ({ value: r, label: r }))} />
            ) : null}
            <Select allowClear placeholder="全部严重度" value={filters.severity || undefined}
                    onChange={(v) => setFResetPage('severity', v ?? '')} aria-label="按严重度筛选"
                    style={{ minWidth: 120 }}
                    options={[['HIGH', '高'], ['MEDIUM', '中'], ['LOW', '低'], ['未分级', '未分级']].map(([v, l]) => ({ value: v, label: l }))} />
            <Select allowClear placeholder="全部类型" value={filters.kind || undefined}
                    onChange={(v) => setFResetPage('kind', v ?? '')} aria-label="按类型筛选"
                    style={{ minWidth: 130 }}
                    options={Object.entries(KIND_LABEL).map(([k, label]) => ({ value: k, label }))} />
          </div>

          <Typography.Paragraph type="secondary" style={{ marginBottom: 8 }}>
            {filtered.length} 项待处理
            {kind === 'console-pg' ? '（审批待办来自隔离联调库——非真实生产待办）' : '（依据最近一次运行记录，非当前 head 结论）'}
          </Typography.Paragraph>

          <Table
            size="small" rowKey={(it) => it.id + (it.kind ?? '')}
            pagination={{ pageSize: 20, hideOnSinglePage: true }}
            dataSource={filtered}
            locale={{ emptyText: '没有需要处理的事项' }}
            columns={[
              { title: '类型', dataIndex: 'kindLabel', width: 100,
                render: (v, it) => <Tag color={it.kind === 'approval' ? 'processing' : 'warning'}>{v}</Tag> },
              { title: '事项', ellipsis: true,
                render: (_, it) => (
                  <Link className="row-link truncate" to={it.href}>{it.title}</Link>
                ) },
              { title: '仓库', dataIndex: 'repo', width: 220, ellipsis: true,
                render: (v) => <span className="mono">{v}</span> },
              { title: '状态', dataIndex: 'statusLabel', ellipsis: true },
              { title: '严重度', dataIndex: 'severity', width: 100,
                render: (v) => (v && SEV_LABEL[v] ? <Tag color={v === 'HIGH' ? 'error' : v === 'MEDIUM' ? 'warning' : 'processing'}>{SEV_LABEL[v]}</Tag> : '—') },
              { title: '时间', dataIndex: 'at', width: 160,
                render: (v) => (v ? <span className="mono">{fmtTime(v)}</span> : '—') },
              { title: '', width: 90,
                render: (_, it) => (
                  <Link to={it.href} aria-label={`打开：${it.title ?? it.repo}`}>
                    <Button size="small">打开 <ArrowRight size={12} strokeWidth={1.75} aria-hidden /></Button>
                  </Link>
                ) },
            ]}
          />
        </>
      )}
    </div>
  );
}
