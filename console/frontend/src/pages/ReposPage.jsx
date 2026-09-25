import React from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight, FolderGit2 } from 'lucide-react';
import { Button, Table, Tag, Typography } from 'antd';
import { useAppConfig } from '../App.jsx';
import { useDataSource, useSourceQuery } from '../hooks.js';
import { fmtTime } from '../format.js';
import { ErrorBox, SkeletonRows } from '../ui.jsx';

// 仓库工作台（默认入口）。
// snapshot 源：仓库列表来自历史数据（标注"历史数据中的仓库"，PR/run 分口径统计）；
// contract 源：仓库清单由可信服务配置声明（未来来自 installation 映射），无虚构计数。
export default function ReposPage() {
  const config = useAppConfig();
  const { source } = useDataSource(config);
  const [attempt, setAttempt] = React.useState(0);
  const reposQ = useSourceQuery(() => source.listRepos(), [source, attempt]);
  const repos = reposQ.status === 'done' ? reposQ.data : [];
  const contract = source.kind === 'contract';
  const totalPrs = repos.reduce((n, r) => n + (r.prCount ?? 0), 0);
  const totalRuns = repos.reduce((n, r) => n + (r.runCount ?? 0), 0);
  const latestActivity = repos.reduce((a, r) => (r.activityAt && (!a || r.activityAt > a) ? r.activityAt : a), null);

  return (
    <div>
      <Typography.Title level={1} style={{ fontSize: 24, marginBottom: 4 }}>仓库</Typography.Title>
      <Typography.Paragraph type="secondary">
        以仓库和 PR 为中心的管理工作台。
        {contract
          ? ' 数据源为正式契约端点。'
          : source.kind === 'console-pg'
            ? ' 数据源为隔离 PG 只读服务：以下为 fixture 测试记录（非真实运行）。'
            : ' 当前数据模式 snapshot：以下仓库来自历史数据中的运行记录，不是已授权接入的实时连接。'}
      </Typography.Paragraph>

      {reposQ.status === 'error' ? (
        <ErrorBox error={reposQ.error} onRetry={() => setAttempt((n) => n + 1)} />
      ) : reposQ.status !== 'done' ? (
        <SkeletonRows rows={4} cols={3} />
      ) : (
        <>
          <Typography.Paragraph type="secondary" style={{ marginBottom: 8 }}>
            {repos.length} 个仓库
            {contract ? '（由服务配置声明）'
              : ` · ${totalPrs} 个 PR · ${totalRuns} 次运行记录${latestActivity ? ` · 数据截至最近记录 ${fmtTime(latestActivity)}` : ''}`}
          </Typography.Paragraph>
          <Table
            size="small" rowKey="repo" dataSource={repos}
            pagination={false}
            locale={{ emptyText: '没有可展示的仓库' }}
            columns={[
              { title: '仓库', ellipsis: true,
                render: (_, r) => (
                  <Link className="repo-card-name truncate" style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}
                        to={`/repos/${encodeURIComponent(r.owner)}/${encodeURIComponent(r.name)}`}>
                    <FolderGit2 size={16} strokeWidth={1.75} aria-hidden /> {r.repo}
                  </Link>
                ) },
              { title: '来源', width: 190,
                render: (_, r) => (
                  <>
                    <Tag>{contract ? '契约数据源'
                      : source.kind === 'console-pg' ? 'PG Fixture 测试记录' : '历史数据中的仓库'}</Tag>
                    {contract && config?.dataMode === 'fixture' ? <Tag>Fixture 数据</Tag> : null}
                  </>
                ) },
              { title: 'PR', dataIndex: 'prCount', width: 90 },
              { title: '运行记录', dataIndex: 'runCount', width: 100 },
              { title: '最近活动', dataIndex: 'activityAt', width: 170,
                render: (v) => (v ? <span className="mono">{fmtTime(v)}</span> : '—') },
              { title: '', width: 130,
                render: (_, r) => (
                  <Link to={`/repos/${encodeURIComponent(r.owner)}/${encodeURIComponent(r.name)}`}
                        aria-label={`查看 ${r.repo} 的 PR 列表`}>
                    <Button size="small">查看 PR 列表 <ArrowRight size={12} strokeWidth={1.75} aria-hidden /></Button>
                  </Link>
                ) },
            ]}
          />
        </>
      )}
    </div>
  );
}
