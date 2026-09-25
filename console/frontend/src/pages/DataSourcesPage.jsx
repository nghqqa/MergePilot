import { Typography } from 'antd';
import React from 'react';
import { Link } from 'react-router-dom';
import { useAppConfig } from '../App.jsx';
import { useAuth } from '../auth.jsx';
import { WorkspacePanel } from '../components/WorkspaceStatusPanel.jsx';
import { resolveWorkspaceState } from '../components/WorkspaceStatusPanel.jsx';

// 数据源与联调状态：工作区状态面板的全页形态（系统二级区）。
// 目标：不要求用户理解工程术语即可判断"我现在看到的数据从哪来、能做什么"。
export default function DataSourcesPage() {
  const config = useAppConfig();
  const auth = useAuth();
  const state = resolveWorkspaceState(config, auth.status);

  return (
    <div>
      <Typography.Title level={1} style={{ fontSize: 24, marginBottom: 4 }}>数据源与联调</Typography.Title>
      <Typography.Paragraph type="secondary">
            当前数据从哪来、能做什么、哪些能力还在等待后端。术语的完整定义见下方说明。
          </Typography.Paragraph>

      <div className="panel" style={{ padding: 'var(--sp-4)' }}>
        <WorkspacePanel config={config} auth={auth} onRetry={auth.refresh} />
      </div>

      <section className="section">
        <div className="section-head"><h3>模式说明（人话版）</h3></div>
        <ul className="compact-list">
          <li><strong>只读快照</strong>——真实历史运行的存档，只能看，不能操作。</li>
          <li><strong>隔离联调</strong>——测试库里的演练数据；批准/拒绝只作用于演练库，
            不会碰真实的 GitHub、PR 或生产数据。</li>
          <li><strong>契约数据</strong>——按正式接口约定提供的验收数据（当前为 fixture）。</li>
          <li><strong>后端不可用</strong>——服务连不上，此时不提供任何数据（包括快照）。</li>
        </ul>
      </section>

      <section className="section">
        <div className="section-head"><h3>前往</h3></div>
        <ul className="compact-list">
          <li>日常处理：<Link to="/pending">待处理</Link></li>
          <li>按仓库浏览：<Link to="/repos">仓库</Link></li>
          <li>完整运行档案：<Link to="/runs">运行</Link></li>
          <li>诊断与健康：<Link to="/diagnostics">审计诊断</Link></li>
        </ul>
        {state.key === 'unavailable' ? (
          <p className="section-note">提示：当前后端不可用，以上页面暂无数据——这不是"没有数据"。</p>
        ) : null}
      </section>
    </div>
  );
}
