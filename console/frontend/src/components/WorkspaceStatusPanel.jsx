import React, { useState } from 'react';
import { useAuth } from '../auth.jsx';

// Workspace Status：统一的工作区状态表达。
// - 顶部 chip：一行人类可读状态（不出现工程术语）
// - 面板：数据来源 / 是否只读 / 写操作 / GitHub 身份 / 生产后端 / 接线状态 + 重试
// 状态判定只来自可信服务配置（/api/health sources）与会话探测，不用 URL/sessionStorage。

const MODE_LABEL = {
  snapshot: '只读快照',
  contract: '契约数据',
  'console-pg': '隔离联调',
};

const SOURCE_LABEL = {
  snapshot: '本地快照证据包（真实历史运行，锁定只读）',
  contract: '契约数据源（按 API-AUTH-MERGE-V0 形状）',
  'console-pg': '隔离 PG 只读服务（fixture 测试记录）',
};

// R4（FB-02）：live 已配置时数据模式如实标注，不再把契约数据统称"Fixture"
function isLive(config) {
  return config?.dataMode === 'live' || config?.raw?.data_mode === 'live';
}

export function resolveWorkspaceState(config, authStatus) {
  if (!config) return { key: 'loading', label: '正在获取状态…', tone: 'neutral' };
  if (config.mode === 'snapshot' && !config.raw) {
    return { key: 'unavailable', label: '后端不可用', tone: 'bad' };
  }
  if (authStatus === 'unavailable') {
    return { key: 'unavailable', label: '后端不可用', tone: 'bad' };
  }
  if (config.mode === 'console-pg') {
    return authStatus === 'authed'
      ? { key: 'fixture-auth', label: '隔离联调（测试主体）', tone: 'warn' }
      : { key: 'fixture', label: '隔离联调（未认证）', tone: 'warn' };
  }
  if (config.mode === 'contract') {
    if (isLive(config)) {
      return authStatus === 'authed'
        ? { key: 'live-auth', label: 'PG 实时（staging · 会话 allowlist）', tone: 'ok' }
        : { key: 'live', label: 'PG 实时（未认证）', tone: 'warn' };
    }
    return { key: 'contract', label: '契约数据（Fixture）', tone: 'warn' };
  }
  // snapshot
  if (authStatus === 'forbidden' || authStatus === 'expired') {
    return { key: 'degraded', label: '快照只读（会话受限）', tone: 'warn' };
  }
  return { key: 'snapshot', label: '只读快照', tone: 'ok' };
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

// 完整状态面板（顶栏弹出与"数据源与联调"页面共用）
export function WorkspacePanel({ config, auth, onRetry }) {
  const state = resolveWorkspaceState(config, auth.status);
  const mode = config?.mode ?? 'snapshot';
  const health = config?.raw ?? {};
  const live = isLive(config);
  const testAuth = auth.status === 'authed' && config?.dataMode === 'fixture';
  const pgMode = mode === 'console-pg';

  const wiring = [
    { name: '运行查询（快照）', state: mode === 'snapshot' ? '已接入' : '不适用（当前为实时源；run 证据详情页仍为快照）', ok: true },
    { name: 'PR 聚合（/api/pulls 正式契约）', state: live ? '已接入（PG 实时，会话 allowlist 过滤）' : '未交付——等待后端（C-10）', ok: live },
    { name: '审批只读', state: pgMode ? '已接入（隔离 test-auth）' : live ? '已接入（实时票据，会话 allowlist）' : '未接线（C-4/C-11）', ok: pgMode || live },
    { name: '审批决策', state: pgMode ? '隔离 test-auth（仅 fixture 票据）' : '未接线（C-11 + D-1/D-2/D-3）', ok: false },
    { name: 'OAuth 登录', state: '未接线（C-8 + D-9）——当前为具名操作员密码登录', ok: false },
    { name: '站内合并', state: '关闭（C-12，仅 GitHub 外链）', ok: false },
    { name: '知识库 / 用量', state: '未接线（数据源待交付）', ok: false },
  ];

  return (
    <div className="ws-panel" role="region" aria-label="工作区状态详情">
      <div className={`ws-state-line ws-tone-${state.tone}`}>
        当前状态：<strong>{state.label}</strong>
      </div>
      <Row label="数据来源">
        {sourceLabelOf(mode)}
        {health?.evidence_root ? (
          <div className="ws-sub mono">{health.evidence_root}</div>
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
          ? <><span className="ws-yes">已连接</span>——PG 实时（staging 隔离库）；run 证据详情页仍为快照</>
          : <><span className="ws-no">未连接</span>——当前全部为快照 / 隔离 fixture；生产接线由后端交付后经配置切换</>}
      </Row>
      <Row label="是否真实 GitHub 操作">
        <YesNo yes={false} yesText="有" noText="无——任何模式都不写 GitHub" />
      </Row>
      <div className="ws-section">接线状态</div>
      <ul className="ws-wiring">
        {wiring.map((w) => (
          <li key={w.name}>
            <span className={`ws-dot ${w.ok ? 'ws-dot-ok' : 'ws-dot-no'}`} aria-hidden />
            <span className="ws-wire-name">{w.name}</span>
            <span className="ws-wire-state">{w.state}</span>
          </li>
        ))}
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
