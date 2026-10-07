import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Card, Form, Input } from 'antd';
import { EyeInvisibleOutlined, EyeOutlined, LockOutlined, UserOutlined } from '@ant-design/icons';
import { useAppConfig } from '../App.jsx';
import { useAuth } from '../auth.jsx';
import { BrandMark } from '../ui.jsx';

// 登录页（/login = 日常登录入口）。渲染完全由可信后端能力驱动：
//   GET /api/auth/session（匿名）→ capabilities { legacy_login, multiuser }
//   GET /api/mu/auth/providers  → { github: { configured, callback_url }, fixture: { configured } }
// 两个密码端点严格区分，禁止混用门禁：
//   POST /api/auth/login    = 旧操作员账号密码（MU_MODE / MU_LEGACY_LOGIN 门禁；本页 legacy 形态使用）
//   POST /api/mu/auth/login = fixture 身份登录（MU_ALLOW_FIXTURE_LOGIN 门禁；本页不使用）
// multiuser 生产形态：仅「使用 GitHub 登录」主按钮 + 邀请制提示；操作员表单与演示入口不显示。
// 邀请链接 /login?invite=<invite_id>：按钮经 GET /api/mu/auth/oauth/github/start?invite=…
// 换取 authorize_url 后跳转。邀请在后端绑定 GitHub 数字 id/租户/角色/有效期——链接本身
// 不是可转让授权凭证；无效（已领取/过期/不存在）由 start 端点 404 invitation_not_found 判定。
// 演示标记（sessionStorage）在登录页挂载时清理——入口与状态守卫一致，不改变真实认证权限。

const STATUS_COPY = {
  unavailable: { type: 'error', text: '服务不可达 — 无法连接控制台后端。', label: '重试连接' },
  auth_unavailable: { type: 'error', text: '登录服务未配置（auth_unavailable）— 按契约不开放。', label: '重试' },
  expired: { type: 'warning', text: '会话已过期 — 请重新登录。', label: '重新检查会话' },
  forbidden: { type: 'warning', text: '当前账号未获准入 — 由服务端判定；如需访问请联系管理员。', label: '重新检查' },
};

// OAuth 回调失败经 302 /multiuser?mu_login_error=<白名单 reason> 落回；未认证路由守卫
// 渲染本页——错误提示必须在本页呈现，不能被守卫遮蔽。
const LOGIN_ERROR_COPY = {
  not_invited: '身份未被邀请（无公共自动注册）——请联系管理员以你的 GitHub 数字 user id 创建邀请',
  no_active_membership: '无有效成员关系——邀请可能已过期，请联系管理员',
  user_disabled: '账号已被禁用——请联系管理员',
  invitation_ambiguous: '存在多条待认领邀请——请联系管理员清理后重试',
  state_invalid: '登录状态校验未通过（过期/重放/更换浏览器）——请重新发起登录',
  oauth_exchange_failed: 'GitHub 授权交换失败——请稍后重试',
  oauth_identity_invalid: 'GitHub 身份无效——请重试',
  oauth_not_configured: 'GitHub OAuth 未配置——联系管理员设置 MU_GITHUB_OAUTH_* 三项',
};

export default function LoginPage() {
  const auth = useAuth();
  const copy = STATUS_COPY[auth.status];
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState(null);
  // 邀请参数（本次新增入口）：/login?invite=<invite_id>
  const [inviteId, setInviteId] = useState(() => new URLSearchParams(window.location.search).get('invite'));
  const [inviteError, setInviteError] = useState(null);
  const loginError = new URLSearchParams(window.location.search).get('mu_login_error');
  const caps = auth.capabilities; // { legacy_login, multiuser } | null（后端能力未知时保持最保守渲染）

  // 演示标记处理：登录页挂载即清（已有 sessionStorage 标记不跨登录态残留）。
  useEffect(() => {
    if (auth.demo) auth.exitDemo();
    // 仅挂载时执行一次；依赖 auth.demo 会与退出动作竞态重复清理。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // providers（无秘密端点）：GitHub 是否已配置 + callback 公开地址来源。失败仅重试，不回退备用登录。
  const [prov, setProv] = useState({ phase: 'loading', data: null });
  const loadProviders = useCallback(async () => {
    setProv({ phase: 'loading', data: null });
    try {
      const r = await fetch('/api/mu/auth/providers', { credentials: 'same-origin' });
      const body = await r.json().catch(() => null);
      if (r.ok && body?.github) setProv({ phase: 'ready', data: body });
      else setProv({ phase: 'error', data: null });
    } catch { setProv({ phase: 'error', data: null }); }
  }, []);
  useEffect(() => { loadProviders(); }, [loadProviders]);

  const startGithub = async () => {
    setBusy(true); setFormError(null); setInviteError(null);
    try {
      const qs = inviteId ? `?invite=${encodeURIComponent(inviteId)}` : '';
      const r = await fetch('/api/mu/auth/oauth/github/start' + qs, { credentials: 'same-origin' });
      const body = await r.json().catch(() => null);
      if (r.ok && body?.authorize_url) {
        window.location.href = body.authorize_url;
        return;
      }
      if (body?.error?.reason === 'invitation_not_found') {
        setInviteError('邀请无效——可能已被领取或已过期。可返回普通登录，或联系管理员重发邀请');
        return;
      }
      if (body?.error?.reason === 'oauth_not_configured') {
        setFormError('GitHub OAuth 未配置——联系管理员设置 MU_GITHUB_OAUTH_* 三项');
        return;
      }
      setFormError('无法发起 GitHub 登录');
    } catch { setFormError('网络错误 — 无法连接后端'); } finally { setBusy(false); }
  };

  const submitLegacy = async ({ user, password }) => {
    setBusy(true); setFormError(null);
    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ user, password }),
      });
      if (!res.ok) {
        if (res.status === 403) setFormError('多用户模式已禁用操作员账号登录——请使用 GitHub 登录');
        else if (res.status === 503) setFormError('登录服务未配置');
        else setFormError('凭据无效');
        return;
      }
      auth.refresh();
    } catch {
      setFormError('网络错误 — 无法连接后端');
    } finally {
      setBusy(false);
    }
  };

  const clearInvite = () => {
    setInviteId(null); setInviteError(null);
    try { window.history.replaceState(null, '', window.location.pathname); } catch { /* ignore */ }
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
            {loginError && (
              <Alert type="warning" showIcon style={{ marginTop: 16 }}
                     message={LOGIN_ERROR_COPY[loginError] ?? '登录未完成——请重新发起登录'} />
            )}
            {inviteId && !inviteError && (
              <Alert type="info" showIcon style={{ marginTop: 16 }}
                     message="检测到受邀访问——请使用受邀的 GitHub 账号登录" />
            )}
            {inviteError && (
              <Alert type="warning" showIcon style={{ marginTop: 16 }} message={inviteError}
                     action={<Button size="small" onClick={clearInvite}>返回普通登录</Button>} />
            )}

            {/* 日常登录主入口（品牌主色）。providers 加载中禁用；配置失败只重试，不放开备用登录。 */}
            <Button type="primary" block size="large"
                    style={{ marginTop: 16, marginBottom: 8 }}
                    loading={busy || prov.phase === 'loading'}
                    disabled={prov.phase === 'loading'}
                    onClick={startGithub}>
              {inviteId ? '使用受邀的 GitHub 账号登录' : '使用 GitHub 登录'}
            </Button>
            {prov.phase === 'error' && (
              <Alert type="error" showIcon style={{ marginTop: 8 }}
                     message="登录配置获取失败——无法确认可用登录方式（不会回退到其他入口）"
                     action={<Button size="small" onClick={loadProviders}>重试</Button>} />
            )}
            {prov.phase === 'ready' && prov.data?.github?.configured === false && (
              <Alert type="warning" showIcon style={{ marginTop: 8 }}
                     message="GitHub OAuth 未配置——联系管理员设置 MU_GITHUB_OAUTH_CLIENT_ID/_CLIENT_SECRET/_CALLBACK_URL 三项" />
            )}

            {/* 邀请制提示（multiuser 生产形态） */}
            {caps?.multiuser ? (
              <div className="login-scope" role="note" style={{ marginTop: 12 }}>
                <strong>邀请制</strong>
                <span>仅受邀成员可登录，需要访问权限请联系管理员。</span>
              </div>
            ) : null}

            {/* legacy 操作员表单：仅 legacy 形态渲染（capability 来自 /api/auth/session 匿名响应）。
                生产 multiuser 下 MU_ALLOW_FIXTURE_LOGIN 与 MU_LEGACY_LOGIN 均未开启——本表单不显示。 */}
            {caps?.legacy_login ? (
              <>
                <div style={{ textAlign: 'center', color: '#999', margin: '10px 0 0' }}>—— 或使用操作员账号 ——</div>
                <Form layout="vertical" onFinish={submitLegacy} disabled={busy} style={{ marginTop: 16 }}>
                  {/* rc.10 可访问性：prefix 图标为装饰（字段已有 label）→ aria-hidden；
                      密码可见性切换图标经 iconRender 补有效可访问名 */}
                  <Form.Item label="操作员账号" name="user" rules={[{ required: true, message: '请输入用户名' }]}>
                    <Input id="login-user" prefix={<UserOutlined aria-hidden="true" />} placeholder="用户名"
                           autoComplete="username" size="large" />
                  </Form.Item>
                  <Form.Item label="密码" name="password" rules={[{ required: true, message: '请输入密码' }]}>
                    <Input.Password id="login-pass" prefix={<LockOutlined aria-hidden="true" />} placeholder="密码"
                           autoComplete="current-password" size="large"
                           iconRender={(visible) => (visible
                             ? <EyeOutlined aria-label="隐藏密码" />
                             : <EyeInvisibleOutlined aria-label="显示密码" />)} />
                  </Form.Item>
                  <Button type="primary" htmlType="submit" block size="large" loading={busy}>
                    登录
                  </Button>
                </Form>
              </>
            ) : null}
            {formError && <Alert type="error" message={formError} showIcon style={{ marginTop: 12 }} />}

            {/* 数据范围说明与演示入口：仅 legacy/本地形态（multiuser 生产登录页不显示） */}
            {!caps?.multiuser && (
              <>
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
          </>
        )}
      </Card>
    </div>
  );
}
