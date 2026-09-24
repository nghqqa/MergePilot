import React from 'react';
import { Link } from 'react-router-dom';
import { useAppConfig } from '../App.jsx';
import { useAuth } from '../auth.jsx';
import { resolveWorkspaceState } from '../components/WorkspaceStatusPanel.jsx';

// 审计诊断：接线状态与健康摘要的集中呈现 + 审计记录入口。
// 只如实列出已接入与未接入项；未接线项等待后端交付（对应 INTEGRATION-REQUESTS R-1~R-4）。
export default function DiagnosticsPage() {
  const config = useAppConfig();
  const auth = useAuth();
  const state = resolveWorkspaceState(config, auth.status);
  const health = config?.raw ?? {};

  const wiring = [
    ['运行查询（快照/隔离 PG）', '已接入', true],
    ['PR 聚合（/api/pulls 正式契约）', '等待后端交付（C-10）', false],
    ['审批只读 + 决策（test-auth）', '已接入（隔离 fixture；生产主体待 D-1/D-2/D-3）', true],
    ['审批 TTL / params / PR 字段', '响应未携带——缺口已记录（C-11）', false],
    ['决策 head 冲突语义', '决策接口暂无 expected_head 参数（D-3 阶段）', false],
    ['OAuth 登录', '等待后端 + D-9', false],
    ['站内合并', '关闭（C-12，仅 GitHub 外链）', false],
    ['findings / validations PG 查询面', '等待后端交付', false],
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>审计诊断</h1>
          <p className="page-sub">
            接线状态、后端健康与审计记录入口。异常时先看这里判断"是坏了"还是"还没接"。
          </p>
        </div>
      </div>

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
          <li>审批决策审计：票据决策在隔离库 approval.ticket_audit 留痕（控制台暂只读展示状态，审计明细查询待后端交付）。</li>
        </ul>
      </section>
    </div>
  );
}
