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
    anonymous: auth.sessionSupported ? '未登录' : '后端未提供会话接口（GET /api/session = 404）',
    authed: `已登录：${auth.user?.name ?? auth.user?.login ?? '未知用户'}`,
    expired: '会话已过期',
    unavailable: '服务不可达',
  }[auth.status] ?? '未知';

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>设置</h1>
          <p className="page-sub">数据模式、会话与运行边界。</p>
        </div>
      </div>

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
            <div className="kv-label">认证方案</div>
            <div className="kv-value">
              由后端会话拍板（提案 C-8）—— 控制台不自建账号库；路由守卫只改善交互，授权以后端为准。
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
          <li>服务仅监听 127.0.0.1 回环；全站只读，无写操作接口，不下发凭证。</li>
          <li>快照数据可展示真实历史结果，但不提供针对历史数据的真实审批或合并操作。</li>
          <li>站内审批：等待后端决策接口（C-11 提案）。站内合并：范围变更已记录（C-12），启用条件由后端证明。</li>
          <li>GitHub App 安装授权与用户登录是两条流程，控制台不混用、不代持凭证。</li>
        </ul>
      </section>
    </div>
  );
}
