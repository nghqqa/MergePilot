import React, { useState } from 'react';
import { Alert, Button, Card, Form, Input } from 'antd';
import { LockOutlined, UserOutlined } from '@ant-design/icons';
import { useAppConfig } from '../App.jsx';
import { useAuth } from '../auth.jsx';
import { BrandMark } from '../ui.jsx';

// 登录页（审查工作台入口，antd 版）。三区：① 登录 ② 当前数据范围 ③ 只读演示。
// 成败完全由服务端会话决定；不模拟、不承诺未接通能力。
const STATUS_COPY = {
  unavailable: { type: 'error', text: '服务不可达 — 无法连接控制台后端。', label: '重试连接' },
  auth_unavailable: { type: 'error', text: '登录服务未配置（auth_unavailable）— 按契约不开放，含只读演示。', label: '重试' },
  expired: { type: 'warning', text: '会话已过期 — 请重新登录。', label: '重新检查会话' },
  forbidden: { type: 'warning', text: '当前账号未获准入 — 由服务端判定；如需访问请联系管理员。', label: '重新检查' },
};

export default function LoginPage() {
  const auth = useAuth();
  const config = useAppConfig();
  const copy = STATUS_COPY[auth.status];
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState(null);

  const submit = async ({ user, password }) => {
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
      auth.refresh();
    } catch {
      setFormError('网络错误 — 无法连接后端');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-wrap">
      <Card className="login-card" variant="borderless" styles={{ body: { padding: 26 } }}>
        <div className="login-brand">
          <BrandMark />
          <div>
            <div className="brand-name">MergePilot</div>
            <div className="brand-sub">审查工作台</div>
          </div>
        </div>

        {copy ? (
          <>
            <Alert type={copy.type} message={copy.text} showIcon style={{ marginTop: 16 }} />
            <Button type="primary" block size="large" style={{ marginTop: 14 }}
                    onClick={auth.refresh}>{copy.label}</Button>
          </>
        ) : (
          <>
            <Form layout="vertical" onFinish={submit} disabled={busy} style={{ marginTop: 16 }}>
              <Form.Item label="具名操作员" name="user" rules={[{ required: true, message: '请输入用户名' }]}>
                <Input id="login-user" prefix={<UserOutlined aria-label />} placeholder="用户名"
                       autoComplete="username" size="large" />
              </Form.Item>
              <Form.Item label="密码" name="password" rules={[{ required: true, message: '请输入密码' }]}>
                <Input.Password id="login-pass" prefix={<LockOutlined aria-label />} placeholder="密码"
                       autoComplete="current-password" size="large" />
              </Form.Item>
              <Button type="primary" htmlType="submit" block size="large" loading={busy}>
                登录
              </Button>
            </Form>
            {formError && <Alert type="error" message={formError} showIcon style={{ marginTop: 12 }} />}

            <div className="login-scope" role="note">
              <strong>当前数据范围</strong>
              <span>登录后：授权仓库的 PR 审查、证据与审计（实时只读）。</span>
              <span>未登录或演示：仅本地历史快照，不含实时数据。</span>
            </div>

            <div className="login-demo">
              <Button block onClick={auth.enterDemo}>以只读演示进入</Button>
              <p className="login-sub">演示 = 未认证浏览脱敏快照；顶部常显未认证标识，设置页可退出。</p>
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
