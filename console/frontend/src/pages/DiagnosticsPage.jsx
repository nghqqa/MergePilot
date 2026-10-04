import { Typography } from 'antd';
import React from 'react';
import { Link } from 'react-router-dom';
import { useAppConfig } from '../App.jsx';
import { useAuth } from '../auth.jsx';
import { resolveWorkspaceState } from '../components/WorkspaceStatusPanel.jsx';

// 诊断：接线状态与健康摘要的集中呈现 + 审计记录入口。
// 命名契约：本页=「诊断」（含审计记录入口）；「审批」=/approvals 的人工放行页——两者不混用。
// 接线清单按数据源口径切换（rc.10 信息正确性）：
//   multiuser 实时源 → MU 面真实接线（GitHub OAuth 与审批只读+决策已上线，数据面按租户隔离）；
//   legacy 快照/联调源 → 保留历史口径（该源下 OAuth/生产审批确实未接线，如实呈现）。
export default function DiagnosticsPage() {
  const config = useAppConfig();
  const auth = useAuth();
  const state = resolveWorkspaceState(config, auth.status);
  const health = config?.raw ?? {};
  const isMu = config?.mode === 'multiuser';

  // MU 实时源（v16 审批门 + Wave 2A OAuth 已上线）的真实接线
  const muWiring = [
    ['OAuth 登录', '已接入——GitHub OAuth 已上线（多用户生产会话，按邀请获得角色）', true],
    ['审批只读 + 决策', '已接入——/api/mu/approvals 只读与决策端点已接线（高危修复审批门；批准仅生成 DRY_RUN 建议）', true],
    ['数据面', '已接入——多用户实时数据，按登录会话的租户隔离（仅见本组织数据）', true],
    ['PR 审查', '已接入——GitHub App 只读接入，PR 创建后自动同步审查', true],
    ['站内合并', '关闭——不自动合并、不绕过 branch protection', false],
  ];

  // legacy 快照/隔离联调源的接线清单（历史口径——该源下确实如此）
  const legacyWiring = [
    ['运行查询（快照/隔离 PG）', '已接入', true],
    ['PR 聚合（/api/pulls 正式契约）', '等待后端交付', false],
    ['审批只读 + 决策（test-auth）', '已接入（隔离联调测试主体；生产主体的授权策略待后端交付）', true],
    ['审批 TTL / params / PR 字段', '响应未携带——后端响应暂缺这些字段', false],
    ['决策 head 冲突语义', '决策接口暂不支持指定期望 head——冲突语义待后端交付', false],
    ['OAuth 登录', '等待后端交付', false],
    ['站内合并', '关闭——仅提供 GitHub 外链', false],
    ['findings / validations PG 查询面', '等待后端交付', false],
  ];
  const wiring = isMu ? muWiring : legacyWiring;

  return (
    <div>
      <Typography.Title level={1} style={{ fontSize: 24, marginBottom: 4 }}>诊断</Typography.Title>
      <Typography.Paragraph type="secondary">
            接线状态、后端健康与审计记录入口。异常时先看这里判断"是坏了"还是"还没接"。
          </Typography.Paragraph>

      <section className="section">
        <div className="section-head"><h3>当前状态</h3></div>
        <div className={`state-box ${state.tone === 'bad' ? 'state-error' : state.tone === 'warn' ? 'state-warn' : 'state-ok'}`} role="status">
          {state.label}
          {auth.reason ? `（${auth.reason}）` : ''}
        </div>
      </section>

      <section className="section">
        <div className="section-head"><h3>接线状态</h3></div>
        <div className="panel">
          <table className="data-table">
            <thead><tr><th>能力</th><th>状态</th></tr></thead>
            <tbody>
              {wiring.map(([name, st, ok]) => (
                <tr key={name}>
                  <td>{name}</td>
                  <td className={ok ? 'ws-yes' : 'ws-no'}>{st}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="section">
        <div className="section-head"><h3>后端健康摘要</h3></div>
        <div className="kv-grid">
          <div className="kv"><div className="kv-label">服务</div><div className="kv-value mono">{health.service ?? '—'}</div></div>
          <div className="kv"><div className="kv-label">版本</div><div className="kv-value mono">{health.version ?? '—'}</div></div>
          <div className="kv"><div className="kv-label">数据模式</div><div className="kv-value">{health.data_mode ?? '—'}</div></div>
          <div className="kv"><div className="kv-label">认证</div><div className="kv-value">{health.auth ?? '未实现（401 如实返回）'}</div></div>
          {health.runs != null ? (
            <div className="kv"><div className="kv-label">运行记录</div><div className="kv-value num">{health.runs}</div></div>
          ) : null}
        </div>
      </section>

      <section className="section">
        <div className="section-head"><h3>审计记录入口</h3></div>
        <ul className="compact-list">
          <li>运行级审计与证据链：<Link to="/runs">运行</Link>（每个运行详情含时间线、任务、证据与 SHA256SUMS 校验）。</li>
          <li>PR 维度结论与关联：<Link to="/repos">仓库</Link> → 选择仓库 → PR 详情。</li>
          <li>审批决策审计：{isMu
            ? '审批票决策与 PR 审批/驳回在多用户库留痕（可追溯）；控制台只读展示，审计明细查询面待后端交付。'
            : '票据决策在隔离库 approval.ticket_audit 留痕（控制台暂只读展示状态，审计明细查询待后端交付）。'}</li>
        </ul>
      </section>
    </div>
  );
}
