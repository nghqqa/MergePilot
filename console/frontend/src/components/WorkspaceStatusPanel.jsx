import React, { useState } from 'react';
import { useAuth } from '../auth.jsx';
import { WIRE, wireMap } from '../status-map.js';

// Workspace Status：统一的工作区状态表达。
// - 顶部 chip：一行人类可读状态（不出现工程术语）
// - 面板：数据来源 / 是否只读 / 写操作 / GitHub 身份 / 生产后端 / 接线状态 + 重试
// 状态判定只来自可信服务配置（/api/health sources）与会话探测，不用 URL/sessionStorage。

const MODE_LABEL = {
  snapshot: '只读快照',
  contract: '契约数据',
  'console-pg': '隔离联调',
  multiuser: 'MU 实时',
};

const SOURCE_LABEL = {
  snapshot: '本地快照证据包（真实历史运行，锁定只读）',
  contract: '契约数据源（按 API-AUTH-MERGE-V0 形状）',
  'console-pg': '隔离 PG 只读服务（fixture 测试记录）',
  multiuser: '多用户实时数据（按登录组织隔离）',
};

// R4（FB-02）：live 已配置时数据模式如实标注，不再把契约数据统称"Fixture"
function isLive(config) {
  return config?.dataMode === 'live' || config?.raw?.data_mode === 'live';
}

// 顶部 chip 用短标签（UX 收敛审查：768px 以下顶栏不拥挤），完整含义放 tooltip（full）
// 与点击展开的状态面板。label ≤ 6 字；full 一句话讲清"这是什么、按什么隔离"。
export function resolveWorkspaceState(config, authStatus) {
  if (!config) return { key: 'loading', label: '获取中…', full: '正在读取服务配置（/api/health）', tone: 'neutral' };
  if (config.mode === 'snapshot' && !config.raw) {
    return { key: 'unavailable', label: '后端不可用', full: '无法连接 console 后端——数据不可用，可点击展开并重试连接', tone: 'bad' };
  }
  if (authStatus === 'unavailable') {
    return { key: 'unavailable', label: '后端不可用', full: '会话探测失败：console 后端不可达——可点击展开并重试连接', tone: 'bad' };
  }
  if (config.mode === 'console-pg') {
    return authStatus === 'authed'
      ? { key: 'fixture-auth', label: '隔离联调', full: '隔离联调环境（测试主体）：真实 HTTP → 隔离 fixture 库，非生产数据', tone: 'warn' }
      : { key: 'fixture', label: '隔离联调', full: '隔离联调环境（未认证）：数据为隔离 fixture 测试记录', tone: 'warn' };
  }
  if (config.mode === 'contract') {
    if (isLive(config)) {
      return authStatus === 'authed'
        ? { key: 'live-auth', label: '实时数据', full: 'PG 实时数据（live）：按登录会话 allowlist 过滤的实时读取', tone: 'ok' }
        : { key: 'live', label: '实时（未登录）', full: 'PG 实时数据源已连接，但当前未登录——登录后按会话 allowlist 读取', tone: 'warn' };
    }
    return { key: 'contract', label: '联调数据', full: '契约数据源（fixture 形状）：隔离联调数据，非生产实时', tone: 'warn' };
  }
  if (config.mode === 'multiuser') {
    return authStatus === 'authed'
      ? { key: 'mu-auth', label: '实时', full: '多用户实时（按组织隔离）：登录组织的实时数据，仅见本组织内容', tone: 'ok' }
      : { key: 'mu', label: '实时（未登录）', full: '多用户实时数据源已连接，但当前未登录——登录后按组织隔离读取', tone: 'warn' };
  }
  // snapshot
  if (authStatus === 'forbidden' || authStatus === 'expired') {
    return { key: 'degraded', label: '快照只读', full: '只读快照（会话受限）：本地脱敏历史证据包，锁定只读', tone: 'warn' };
  }
  return { key: 'snapshot', label: '快照只读', full: '只读快照：本地脱敏历史证据包（真实历史运行，锁定只读）', tone: 'ok' };
}

function Row({ label, children }) {
  return (
    <div className="ws-row">
      <div className="ws-row-label">{label}</div>
      <div className="ws-row-value">{children}</div>
    </div>
  );
}

function YesNo({ yes, yesText = '是', noText = '否' }) {
  return <span className={yes ? 'ws-yes' : 'ws-no'}>{yes ? yesText : noText}</span>;
}

// 接线状态行统一结构：{ name, wire: status-map WIRE 词表条目, detail: 该行补充说明 }。
// 可见文本 = 词表 label + detail（detail 保留各数据源口径的既有事实描述）；
// 词表 note（"这是什么、不是什么"）挂在行状态 title 上——颜色/圆点不单独承载状态。
// rc.10：MU 实时源下接线清单按真实能力渲染（OAuth/审批决策已上线），不再复用 legacy 未交付口径。
function wiringRowsOf(mode, live, pgMode) {
  if (mode === 'multiuser') {
    return [
      { name: 'OAuth 登录', wire: WIRE.wired, detail: '——GitHub OAuth 已上线（多用户生产会话，按邀请获得角色）' },
      { name: '审批只读 + 决策', wire: WIRE.wired, detail: '——/api/mu/approvals 只读与决策端点已接线（高危修复审批门；批准仅生成 DRY_RUN 建议，需 maintainer）' },
      { name: '数据面', wire: WIRE.wired, detail: '——多用户实时数据，按登录会话的租户隔离（仅见本组织数据）' },
      { name: 'PR 审查', wire: WIRE.wired, detail: '——GitHub App 只读接入，PR 创建后自动同步审查' },
      { name: '站内合并', wire: WIRE.closed, detail: '——不自动合并、不绕过 branch protection（仅 GitHub 外链）' },
      { name: '知识库 / 用量', wire: WIRE.not_wired, detail: '（数据源待交付）' },
    ];
  }
  return [
    { name: '运行查询（快照）', wire: mode === 'snapshot' ? WIRE.wired : WIRE.na, detail: mode === 'snapshot' ? '' : '（当前为实时源；run 证据详情页仍为快照）' },
    { name: 'PR 聚合（/api/pulls 正式契约）', wire: live ? WIRE.wired : WIRE.pending_delivery, detail: live ? '（PG 实时，会话 allowlist 过滤）' : '——等待后端' },
    { name: '审批只读', wire: pgMode || live ? WIRE.wired : WIRE.not_wired, detail: pgMode ? '（隔离 test-auth）' : live ? '（实时票据，会话 allowlist）' : '——暂无审批票只读数据源' },
    { name: '审批决策', wire: pgMode ? WIRE.wired_test : WIRE.not_wired, detail: pgMode ? '——隔离 test-auth（仅 fixture 票据）' : '——决策接口与授权策略待后端交付' },
    { name: 'OAuth 登录', wire: WIRE.not_wired, detail: '——当前为操作员账号密码登录' },
    { name: '站内合并', wire: WIRE.closed, detail: '——仅提供 GitHub 外链' },
    { name: '知识库 / 用量', wire: WIRE.not_wired, detail: '（数据源待交付）' },
  ];
}

// 完整状态面板（顶栏弹出与"数据源与联调"页面共用）
export function WorkspacePanel({ config, auth, onRetry }) {
  const state = resolveWorkspaceState(config, auth.status);
  const mode = config?.mode ?? 'snapshot';
  const health = config?.raw ?? {};
  const live = isLive(config);
  const testAuth = auth.status === 'authed' && config?.dataMode === 'fixture';
  const pgMode = mode === 'console-pg';

  const wiring = wiringRowsOf(mode, live, pgMode);

  return (
    <div className="ws-panel" role="region" aria-label="工作区状态详情">
      <div className={`ws-state-line ws-tone-${state.tone}`}>
        当前状态：<strong>{state.label}</strong>
      </div>
      <Row label="数据来源">
        {sourceLabelOf(mode)}
        {health?.evidence_root ? (
          // 容器内路径属技术详情：折叠收纳，不进正文（rc.10 信息正确性 PR-C）
          <details className="tech-details">
            <summary>技术详情</summary>
            <span className="ws-sub mono" title="容器内路径">{health.evidence_root}</span>
          </details>
        ) : null}
      </Row>
      <Row label="是否只读">
        {pgMode ? <>查询只读；审批决策仅写隔离 fixture 库（非真实系统）</> : <YesNo yes />}
      </Row>
      <Row label="允许写操作">
        {pgMode
          ? <>仅审批决策（POST → 隔离 fixture 库票据，X-Test-Principal 测试主体）</>
          : <>无——控制台本体接口仅 GET{mode === 'contract' ? '；fixture 演练不触达后端' : ''}</>}
      </Row>
      <Row label="GitHub 身份">
        {auth.status === 'authed'
          ? (config?.dataMode === 'fixture'
              ? <>test-principal（隔离测试主体，非真实 GitHub 身份）</>
              : <>{auth.user?.name ?? auth.user?.display_name ?? auth.user?.github_login ?? '已登录'}（控制台会话；非 GitHub OAuth）</>)
          : <>未认证——无真实 GitHub 身份</>}
      </Row>
      <Row label="生产后端">
        {live
          ? <><span className="ws-yes">已连接</span>——PG 实时（live）·隔离 staging 库；run 证据详情页仍为快照</>
          : <><span className="ws-no">未连接</span>——当前全部为快照 / 隔离 fixture；生产接线由后端交付后经配置切换</>}
      </Row>
      <Row label="是否真实 GitHub 操作">
        <YesNo yes={false} yesText="有" noText="无——任何模式都不写 GitHub" />
      </Row>
      <div className="ws-section">接线状态</div>
      <ul className="ws-wiring">
        {wiring.map((w) => {
          // 行状态 = status-map WIRE 词表条目（容忍传键名字符串）；note 挂 title
          const wm = (w.wire && typeof w.wire === 'object') ? w.wire : wireMap(w.wire);
          return (
            <li key={w.name}>
              <span className={`ws-dot ${wm.tone === 'ok' ? 'ws-dot-ok' : 'ws-dot-no'}`} aria-hidden />
              <span className="ws-wire-name">{w.name}</span>
              <span className="ws-wire-state" title={wm.note}>{`${wm.label}${w.detail}`}</span>
            </li>
          );
        })}
      </ul>
      {state.key === 'unavailable' ? (
        <div className="ws-retry">
          <button type="button" className="btn btn-primary" onClick={onRetry}>重试连接</button>
          <span className="ws-sub">仍不可用：确认 console 后端已启动（node console/backend/server.mjs）</span>
        </div>
      ) : null}
    </div>
  );
}

function sourceLabelOf(mode) {
  return SOURCE_LABEL[mode] ?? '未知数据来源';
}
