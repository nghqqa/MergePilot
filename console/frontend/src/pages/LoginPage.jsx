import React, { useState } from 'react';
import { useAppConfig } from '../App.jsx';
import { useAuth } from '../auth.jsx';
import { BrandMark } from '../ui.jsx';

// 登录页（审查工作台入口）。三个区域职责分明：
//   ① 登录（具名操作员，服务端校验）  ② 当前数据范围  ③ 只读演示（次级路径）
// 成功与否完全由服务端会话决定；本页不模拟、不承诺任何未接通能力。
const STATUS_COPY = {
  unavailable: {
    box: ['state-error', '服务不可达 — 无法连接控制台后端。'],
    action: 'retry', label: '重试连接',
  },
  auth_unavailable: {
    box: ['state-error', '登录服务未配置（auth_unavailable）— 按契约不开放，含只读演示。'],
    action: 'retry', label: '重试',
  },
  expired: {
    box: ['state-warn', '会话已过期 — 请重新登录。'],
    action: 'retry', label: '重新检查会话',
  },
  forbidden: {
    box: ['state-warn', '当前账号未获准入 — 由服务端判定；如需访问请联系管理员。'],
    action: 'retry', label: '重新检查',
  },
};

export default function LoginPage() {
  const auth = useAuth();
  const config = useAppConfig();
  const copy = STATUS_COPY[auth.status];
  const [user, setUser] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState(null);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setFormError(null);
    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ user, password }),
      });
      if (!res.ok) {
        setFormError(res.status === 503 ? '登录服务未配置' : '凭据无效');
        return;
      }
      setPassword('');
      auth.refresh();
    } catch {
      setFormError('网络错误 — 无法连接后端');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-wrap">
      <div className="login-card">
        <div className="login-brand">
          <BrandMark />
          <div>
            <div className="brand-name">MergePilot</div>
            <div className="brand-sub">审查工作台</div>
          </div>
        </div>

        {copy ? (
          <>
            <div className={`state-box ${copy.box[0]}`} role="alert">{copy.box[1]}</div>
            <button type="button" className="btn btn-primary login-main" onClick={auth.refresh}>{copy.label}</button>
          </>
        ) : (
          <>
            {/* ① 登录 */}
            <form onSubmit={submit} style={{ display: 'grid', gap: 10 }}>
              <div>
                <label htmlFor="login-user" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>具名操作员</label>
                <input id="login-user" className="input" value={user} onChange={(e) => setUser(e.target.value)}
                       autoComplete="username" placeholder="用户名" />
              </div>
              <div>
                <label htmlFor="login-pass" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>密码</label>
                <input id="login-pass" className="input" type="password" value={password}
                       onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" placeholder="密码" />
              </div>
              <button type="submit" className="btn btn-primary login-main" disabled={busy || !user || !password}>
                {busy ? '登录中…' : '登录'}
              </button>
            </form>
            {formError && <div className="state-box state-error" role="alert" style={{ marginTop: 10 }}>{formError}</div>}

            {/* ② 当前数据范围 */}
            <div className="login-scope" role="note">
              <strong>当前数据范围</strong>
              <span>登录后：授权仓库的 PR 审查、证据与审计（实时只读）。</span>
              <span>未登录或演示：仅本地历史快照，不含实时数据。</span>
            </div>

            {/* ③ 只读演示（次级路径） */}
            <div className="login-demo">
              <button type="button" className="btn login-main" onClick={auth.enterDemo}>
                以只读演示进入
              </button>
              <p className="login-sub">演示 = 未认证浏览脱敏快照；顶部常显未认证标识，设置页可退出。</p>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
