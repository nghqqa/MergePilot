import React, { useState } from 'react';
import { useAppConfig } from '../App.jsx';
import { useAuth } from '../auth.jsx';
import { BrandMark } from '../ui.jsx';

// 登录页：只做会话交互结构，不伪造登录成功。
// 契约 v2（API-AUTH-MERGE-V0 @ 7ccecb9）：服务端会话（Cookie mp_session 为权威）。
// CANONICAL_CONSOLE_PROMOTION：后端已交付具名 pilot 操作员凭据登录
// （POST /api/auth/login，服务端校验 + allowlist）——本页提供真实表单，
// 成功与否完全由服务端会话决定；GitHub OAuth 仍是生产形态路线（D-9）。
// 只读演示预览 = 明确标注的非认证浏览，仅本地脱敏 snapshot 数据，不发起 live API 调用。
const STATUS_COPY = {
  unavailable: {
    box: ['state-error', '服务不可达 — 无法连接控制台后端。服务不可用时不伪装成未登录循环跳转。'],
    action: 'retry', label: '重试连接',
  },
  auth_unavailable: {
    box: ['state-error', '登录服务不可用（auth_unavailable）— 按契约产品不开放（含只读），不降级放行；只读演示预览亦不可进入。'],
    action: 'retry', label: '重试',
  },
  expired: {
    box: ['state-warn', '会话已过期（session_expired）— 请重新登录。会话状态由服务端给出，前端不自行判定。'],
    action: 'retry', label: '重新检查会话',
  },
  forbidden: {
    box: ['state-warn', '当前账号未获得准入 — 是否准入由服务端判定；如需访问请联系管理员。'],
    action: 'retry', label: '重新检查',
  },
};

export default function LoginPage() {
  const auth = useAuth();
  const config = useAppConfig();
  const pgMode = config?.mode === 'console-pg';
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
        setFormError(res.status === 503
          ? '登录服务未配置（auth_unavailable）'
          : '凭据无效 — 服务端校验未通过');
        return;
      }
      setPassword('');
      auth.refresh();   // 会话权威在服务端；重新探测后进入已认证态
    } catch {
      setFormError('网络错误 — 无法连接控制台后端');
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
            <div className="brand-name">MergePilot 管理控制台</div>
            <div className="brand-sub">运行取证台 · {pgMode ? '隔离 PG fixture' : 'snapshot + live 核心 API'}</div>
          </div>
        </div>

        {copy ? (
          <>
            <div className={`state-box ${copy.box[0]}`} role="alert">{copy.box[1]}</div>
            <button type="button" className="btn btn-primary login-main" onClick={auth.refresh}>{copy.label}</button>
          </>
        ) : (
          <>
            <form onSubmit={submit} style={{ display: 'grid', gap: 10, marginTop: 4 }}>
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
                {busy ? '登录中…' : '登录（服务端会话）'}
              </button>
            </form>
            {formError && <div className="state-box state-error" role="alert" style={{ marginTop: 10 }}>{formError}</div>}
            <p className="login-note" style={{ marginTop: 12 }}>
              服务端会话（mp_session，HttpOnly）+ 仓库 allowlist；凭据校验与数据边界全部在服务端。
              GitHub OAuth 为生产形态路线（D-9），当前交付的是已验证 pilot 的具名操作员路径。
            </p>
            <button type="button" className="btn login-main" onClick={auth.enterDemo} style={{ marginTop: 6 }}>
              以只读演示预览进入
            </button>
            <p className="login-sub">
              演示预览 = 明确标注的未认证浏览：{pgMode
                ? '仅隔离 PG 测试记录（fixture），不发起真实审批/写操作'
                : '仅本地脱敏历史快照，不绕过 live API 认证、不携带真实写权限'}；
              页面顶部全程显示未认证标识，可随时在设置中退出。
            </p>
          </>
        )}
      </div>
    </div>
  );
}
