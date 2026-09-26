import { Typography } from 'antd';
import React, { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../auth.jsx';
import { api } from '../api.js';

// 设置：数据模式、会话状态、运行边界 —— 一次讲清，不在各页面重复长篇工程说明。
// R4（FB-02/FB-06）：数据模式文案随 /api/health 真实值切换（live 时不再显示 snapshot）；
// 会话区显示真实用户名并提供退出登录（POST /api/auth/logout + X-CSRF-Token）。
function csrfFromCookie() {
  const m = (typeof document !== 'undefined' ? document.cookie : '').match(/mp_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : '';
}

const DATA_MODE_COPY = {
  live: 'live（PG 实时，staging 隔离库）：overview/待处理/仓库/PR 详情按会话 allowlist 实时读取；run 证据详情页仍为快照只读。',
  snapshot: 'snapshot（真实历史运行证据包，锁定只读）。',
};

export default function SettingsPage() {
  const auth = useAuth();
  const navigate = useNavigate();
  const [health, setHealth] = useState(null);
  const [err, setErr] = useState(null);
  const [logoutBusy, setLogoutBusy] = useState(false);
  const [logoutErr, setLogoutErr] = useState(null);
  useEffect(() => {
    api.health().then(setHealth).catch(setErr);
  }, [auth.status]);

  const userName = auth.user?.name ?? auth.user?.display_name
    ?? auth.user?.github_login ?? auth.user?.name ?? null;
  const statusLabel = {
    checking: '检查中…',
    anonymous: '未登录',
    authed: `已登录：${userName ?? '未知用户'}`,
    expired: '会话已过期（session_expired）',
    forbidden: '未获准入（not_a_member）',
    auth_unavailable: '登录服务不可用（auth_unavailable）',
    not_implemented: '会话端点未实现（GET /api/auth/session = 404）',
    unavailable: '服务不可达',
  }[auth.status] ?? '未知';

  const doLogout = useCallback(async () => {
    setLogoutBusy(true); setLogoutErr(null);
    try {
      const res = await fetch('/api/auth/logout', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'X-CSRF-Token': csrfFromCookie() },
      });
      if (!res.ok) {
        setLogoutErr(`退出失败（HTTP ${res.status}）`);
        return;
      }
      await auth.refresh();
      navigate('/overview', { replace: true });
    } catch (e) {
      setLogoutErr(`退出失败：${String(e).slice(0, 80)}`);
    } finally {
      setLogoutBusy(false);
    }
  }, [auth, navigate]);

  const dataMode = health?.data_mode ?? null;

  return (
    <div>
      <Typography.Title level={1} style={{ fontSize: 24, marginBottom: 4 }}>设置</Typography.Title>
      <Typography.Paragraph type="secondary">数据模式、会话与运行边界。</Typography.Paragraph>

      <section className="section">
        <div className="section-head"><h3>会话</h3></div>
        <div className="kv-grid">
          <div className="kv"><div className="kv-label">当前状态</div><div className="kv-value">{statusLabel}</div></div>
          <div className="kv">
            <div className="kv-label">退出登录</div>
            <div className="kv-value">
              {auth.status === 'authed' ? (
                <>
                  <button type="button" className="btn btn-sm" disabled={logoutBusy} onClick={doLogout}>
                    {logoutBusy ? '退出中…' : '退出登录'}
                  </button>
                  <div className="section-note">结束服务端会话（POST /api/auth/logout，携带 CSRF）；退出后需重新登录。</div>
                </>
              ) : (
                <>未登录——无需退出</>
              )}
              {logoutErr ? <div className="section-note">{logoutErr}</div> : null}
            </div>
          </div>
          <div className="kv">
            <div className="kv-label">只读演示预览</div>
            <div className="kv-value">
              {auth.demo ? '已进入（未认证，sessionStorage 标记，非登录）' : '未进入'}
              {auth.demo ? (
                <button type="button" className="btn btn-sm" style={{ marginLeft: 8 }} onClick={auth.exitDemo}>
                  退出演示预览
                </button>
              ) : null}
            </div>
          </div>
          <div className="kv">
            <div className="kv-label">登录方案</div>
            <div className="kv-value">
              具名操作员密码登录（已交付，服务端会话）。GitHub OAuth 方案（API-AUTH-MERGE-V0 v2 @ 7ccecb9）
              等待后端实现与 D-9 配置。会话 Cookie（mp_session）为权威；浏览器不保存 App token / 私钥 /
              长期凭证；授权以后端为准。
            </div>
          </div>
        </div>
      </section>

      <section className="section">
        <div className="section-head"><h3>数据与模式</h3></div>
        <div className="kv-grid">
          <div className="kv">
            <div className="kv-label">数据模式</div>
            <div className="kv-value">{dataMode ? (DATA_MODE_COPY[dataMode] ?? dataMode) : '—'}</div>
          </div>
          <div className="kv"><div className="kv-label">服务版本</div><div className="kv-value mono">{health?.version ?? '—'}</div></div>
          <div className="kv"><div className="kv-label">证据包</div><div className="kv-value num">{health?.runs ?? '—'} 个 · 含 SHA256SUMS {health?.packs_with_sums ?? '—'} 个</div></div>
          <div className="kv"><div className="kv-label">live 模式</div><div className="kv-value">{health?.live?.configured ? '已配置（PG 实时，会话 allowlist 过滤）' : '未接入'}</div></div>
        </div>
        {err ? <p className="section-note">健康检查失败：{String(err.message ?? err)}</p> : null}
      </section>

      <section className="section">
        <div className="section-head"><h3>运行边界</h3></div>
        <ul className="compact-list">
          <li>服务仅监听 127.0.0.1 回环；不下发凭证。</li>
          <li>全站只读：控制台本体接口仅 GET；不写 GitHub、不派发 Fixer/Verifier。</li>
          <li>console-pg 联调模式（test-auth）：审批决策请求仅写隔离 fixture 库（X-Test-Principal 测试主体），不触达任何真实系统或 GitHub。</li>
          <li>快照数据显示真实历史结果，但不提供针对历史数据的真实审批或合并操作。</li>
          <li>站内审批：待后端决策接口与 D-1/D-2/D-3 授权策略（生产主体须经认证与授权校验）。站内合并：范围变更已记录（C-12），启用条件由后端证明。</li>
          <li>GitHub App 安装授权与用户登录是两条流程，控制台不混用、不代持凭证。</li>
        </ul>
      </section>
    </div>
  );
}
