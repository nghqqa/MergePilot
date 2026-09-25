import { Typography } from 'antd';
import React, { useEffect, useState } from 'react';
import { useAuth } from '../auth.jsx';
import { api } from '../api.js';

// 设置：数据模式、会话状态、运行边界 —— 一次讲清，不在各页面重复长篇工程说明。
export default function SettingsPage() {
  const auth = useAuth();
  const [health, setHealth] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => {
    api.health().then(setHealth).catch(setErr);
  }, [auth.status]);

  const statusLabel = {
    checking: '检查中…',
    anonymous: '未登录',
    authed: `已登录：${auth.user?.display_name ?? auth.user?.github_login ?? auth.user?.name ?? '未知用户'}`,
    expired: '会话已过期（session_expired）',
    forbidden: '未获准入（not_a_member）',
    auth_unavailable: '登录服务不可用（auth_unavailable）',
    not_implemented: '会话端点未实现（GET /api/auth/session = 404）',
    unavailable: '服务不可达',
  }[auth.status] ?? '未知';

  return (
    <div>
      <Typography.Title level={1} style={{ fontSize: 24, marginBottom: 4 }}>设置</Typography.Title>
      <Typography.Paragraph type="secondary">数据模式、会话与运行边界。</Typography.Paragraph>

      <section className="section">
        <div className="section-head"><h3>会话</h3></div>
        <div className="kv-grid">
          <div className="kv"><div className="kv-label">当前状态</div><div className="kv-value">{statusLabel}</div></div>
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
            <div className="kv-label">登录方案（契约已定）</div>
            <div className="kv-value">
              GitHub OAuth + 服务端会话（API-AUTH-MERGE-V0 v2 @ 7ccecb9）——等待后端实现与 D-9 配置。
              会话 Cookie（mp_session）为权威；浏览器不保存 App token / 私钥 / 长期凭证；
              路由守卫只改善交互，授权以后端为准。
            </div>
          </div>
        </div>
      </section>

      <section className="section">
        <div className="section-head"><h3>数据与模式</h3></div>
        <div className="kv-grid">
          <div className="kv">
            <div className="kv-label">数据模式</div>
            <div className="kv-value">{health?.data_mode ?? '—'}（真实历史运行证据包，锁定只读）</div>
          </div>
          <div className="kv"><div className="kv-label">服务版本</div><div className="kv-value mono">{health?.version ?? '—'}</div></div>
          <div className="kv"><div className="kv-label">证据包</div><div className="kv-value num">{health?.runs ?? '—'} 个 · 含 SHA256SUMS {health?.packs_with_sums ?? '—'} 个</div></div>
          <div className="kv"><div className="kv-label">live 模式</div><div className="kv-value">{health?.live?.configured ? '已配置' : '未接入'}</div></div>
        </div>
        {err ? <p className="section-note">健康检查失败：{String(err.message ?? err)}</p> : null}
      </section>

      <section className="section">
        <div className="section-head"><h3>运行边界</h3></div>
        <ul className="compact-list">
          <li>服务仅监听 127.0.0.1 回环；不下发凭证。</li>
          <li>snapshot / contract 页面验收模式：全站只读（控制台本体接口仅 GET）。</li>
          <li>console-pg 联调模式（test-auth）：审批决策请求仅写隔离 fixture 库（X-Test-Principal 测试主体），不触达任何真实系统或 GitHub。</li>
          <li>快照数据显示真实历史结果，但不提供针对历史数据的真实审批或合并操作。</li>
          <li>站内审批：待后端决策接口与 D-1/D-2/D-3 授权策略（生产主体须经认证与授权校验）。站内合并：范围变更已记录（C-12），启用条件由后端证明。</li>
          <li>GitHub App 安装授权与用户登录是两条流程，控制台不混用、不代持凭证。</li>
        </ul>
      </section>
    </div>
  );
}
