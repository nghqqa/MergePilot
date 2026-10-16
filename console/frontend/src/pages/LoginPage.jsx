import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Card, Form, Input } from 'antd';
import { EyeInvisibleOutlined, EyeOutlined, LockOutlined, UserOutlined } from '@ant-design/icons';
import { useAppConfig } from '../App.jsx';
import { useAuth } from '../auth.jsx';
import { BrandMark } from '../ui.jsx';

// 登录页（/login = 日常登录入口）。渲染完全由可信后端能力驱动：
//   GET /api/auth/session（匿名）→ capabilities { legacy_login, multiuser }
//   GET /api/mu/auth/providers  → { github: {configured,callback_url}, gitee: {…}, fixture: {…} }
// 两个密码端点严格区分，禁止混用门禁：
//   POST /api/auth/login    = 旧操作员账号密码（MU_MODE / MU_LEGACY_LOGIN 门禁；本页 legacy 形态使用）
//   POST /api/mu/auth/login = fixture 身份登录（MU_ALLOW_FIXTURE_LOGIN 门禁；仅当
//   providers 披露 fixture 开放时本页渲染「试用身份」区块——隔离试用/开发部署自助登录）
// multiuser 生产形态：主按钮「使用 GitHub 登录」+（gitee.configured 时）「使用 Gitee 登录」
// 次按钮 + 邀请制提示；操作员表单与演示入口不显示。
// 邀请链接 /login?invite=<invite_id>：按钮经对应 provider 的 start?invite=… 端点换取
// authorize_url 后跳转。邀请在后端绑定 provider+数字 id/租户/角色/有效期——链接本身
// 不是可转让授权凭证；无效（已领取/过期/不存在）由 start 端点 404 判定；provider 错配
// （GitHub 入口对 Gitee 邀请）由 start 端点 409 invite_provider_mismatch 判定。
// 演示标记（sessionStorage）在登录页挂载时清理——入口与状态守卫一致，不改变真实认证权限。

const STATUS_COPY = {
  unavailable: { type: 'error', text: '服务不可达 — 无法连接控制台后端。', label: '重试连接' },
  auth_unavailable: { type: 'error', text: '登录服务暂未开放——请稍后重试，或联系管理员。', label: '重试' },
  expired: { type: 'warning', text: '会话已过期 — 请重新登录。', label: '重新检查会话' },
  forbidden: { type: 'warning', text: '当前账号未获准入 — 由服务端判定；如需访问请联系管理员。', label: '重新检查' },
};

// OAuth 回调失败经 302 /multiuser?mu_login_error=<白名单 reason> 落回；未认证路由守卫
// 渲染本页——错误提示必须在本页呈现，不能被守卫遮蔽。文案 provider 中性（同一原因
// 在 GitHub/Gitee 入口语义一致；具体平台由页面上下文与按钮呈现）。
// 面向用户措辞：不含部署环境变量名与内部实现术语（登录来源波），重试/联系管理员路径明确。
const LOGIN_ERROR_COPY = {
  not_invited: '该账号暂未获邀——本站采用邀请制；如需访问请联系管理员，以你的平台数字 user id 创建邀请',
  no_active_membership: '暂无有效成员关系——邀请可能已过期，请联系管理员重新邀请',
  user_disabled: '账号已被停用——如需恢复请联系管理员',
  invitation_ambiguous: '存在多条待认领邀请——请联系管理员清理后重试',
  state_invalid: '登录状态校验未通过（可能已过期或非同一浏览器发起）——请返回重试',
  oauth_exchange_failed: '登录未完成——平台授权交换失败，请稍后重试',
  oauth_identity_invalid: '登录未完成——平台返回的身份无效，请重试',
  oauth_not_configured: '该登录方式尚未开放——请联系管理员启用后重试',
};

// 平台显示名（登录来源波）：仅用于按钮/提示文案，来自 providers/409 的可信字段，
// 不据此推断会话来源（来源只认后端会话显式标记，见 identity.js）。
const PLATFORM_NAME = { 'github-oauth': 'GitHub', 'gitee-oauth': 'Gitee' };

// 尺寸一致的单色平台图标（currentColor 跟随按钮文字色；装饰性 aria-hidden）。
const GitHubMark = () => (
  <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"
       focusable="false" style={{ flexShrink: 0 }}>
    <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z"/>
  </svg>
);
const GiteeMark = () => (
  <svg width="16" height="16" viewBox="0 0 1024 1024" fill="currentColor" aria-hidden="true"
       focusable="false" style={{ flexShrink: 0 }}>
    <path d="M512 1024C229.222 1024 0 794.778 0 512S229.222 0 512 0s512 229.222 512 512-229.222 512-512 512z m259.149-568.883h-290.74a25.293 25.293 0 0 0-25.292 25.293l-0.026 63.206c0 13.952 11.315 25.293 25.267 25.293h177.024c12.678 0 22.906 10.544 22.906 23.22v12.855c0 58.062-47.105 105.216-105.167 105.216h-240.23a23.27 23.27 0 0 1-23.27-23.271v-240.23A105.216 105.216 0 0 1 411.75 316.617h357.356a25.293 25.293 0 0 0 25.267-25.293l0.077-63.207a25.293 25.293 0 0 0-25.268-25.293H411.75c-105.318 0-190.787 85.494-190.787 190.837v351.7A25.293 25.293 0 0 0 246.26 770.5h351.942c105.344 0 190.736-85.494 190.736-190.837v-103.25a25.293 25.293 0 0 0-25.293-25.293z"/>
  </svg>
);

export default function LoginPage() {
  const auth = useAuth();
  const copy = STATUS_COPY[auth.status];
  // 登录来源波：进行中状态按平台区分——仅被点击的入口显示「正在前往 …」，
  // 另一个禁用但保持原文；任何失败路径复位（可重试）。非 boolean 的原因：
  // 全局 boolean 无法区分是哪个入口在跳转，也无法在双入口间给出差异化反馈。
  const [pending, setPending] = useState(null); // 'github' | 'gitee' | null
  const [formError, setFormError] = useState(null);
  // 邀请参数（本次新增入口）：/login?invite=<invite_id>
  const [inviteId, setInviteId] = useState(() => new URLSearchParams(window.location.search).get('invite'));
  const [inviteError, setInviteError] = useState(null);
  const loginError = new URLSearchParams(window.location.search).get('mu_login_error');
  const caps = auth.capabilities; // { legacy_login } 旧密码表单能力（后端匿名会话下发）
  // multiuser 形态判定来自 /api/health（服务声明，与会话状态无关）——已登录用户
  // 打开 /login 时会话响应不含 capabilities，改用 health 判定邀请制提示/演示隐藏。
  const appConfig = useAppConfig();
  const multiuserMode = appConfig?.mode === 'multiuser';

  // 演示标记处理：登录页挂载即清（已有 sessionStorage 标记不跨登录态残留）。
  useEffect(() => {
    if (auth.demo) auth.exitDemo();
    // 仅挂载时执行一次；依赖 auth.demo 会与退出动作竞态重复清理。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // providers（无秘密端点）：各平台是否已配置 + callback 公开地址来源。失败仅重试，不回退备用登录。
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

  // 提供商已配置（按钮渲染与 stale 错误清理的唯一依据；后端 fail-closed，前端不猜）
  const githubReady = prov.phase === 'ready' && prov.data?.github?.configured === true;
  const giteeReady = prov.phase === 'ready' && prov.data?.gitee?.configured === true;
  // fixture 自助登录（2026-10-16）：仅当服务端 providers 披露 fixture 已开放（=部署显式
  // MU_ALLOW_FIXTURE_LOGIN=1）时渲染——沿用「未配置不伪装可用」同一门禁来源，页面不猜。
  // 面向隔离试用栈/开发部署：真实用户从 UI 独立完成登录（此前只能维护者 curl 代登录，
  // 违反试用「不得代操作」边界）。生产多用户部署未开放时本区块不渲染。
  const fixtureReady = prov.phase === 'ready' && prov.data?.fixture?.configured === true;
  // 过时的「未配置」错误：providers 现已就绪任一平台时不再复现（保留邀请上下文不清）。
  const staleNotConfigured = loginError === 'oauth_not_configured' && (githubReady || giteeReady);

  // 重入守卫（ref）：同 tick 内的连点也拦得住——进行中不接受第二次发起，
  // 不产生第二个 OAuth flow（竞态矩阵回归项，见四）。
  const pendingRef = React.useRef(null);

  // 邀请平台错配提示：只依据 start 端点 409 的可信字段 invite_provider 渲染，
  // 不在页面侧猜测邀请平台；字段缺失时退化为中性指引（不写死任何平台名）。
  const mismatchCopy = (invProvider) => {
    const name = invProvider ? PLATFORM_NAME[invProvider] : null;
    return name
      ? `该邀请绑定的是 ${name} 账号——请改用「使用 ${name} 登录」入口（邀请未被消耗，可从该入口重试）`
      : '该邀请绑定的平台与所选入口不一致——请改用另一个平台入口登录（邀请未被消耗）';
  };

  const startOAuth = (platform) => {
    if (pendingRef.current !== null || pending !== null) return; // 重入守卫：ref+state 双保险
    pendingRef.current = platform;
    setPending(platform); setFormError(null); setInviteError(null);
    const finish = () => { pendingRef.current = null; setPending(null); };
    (async () => {
      try {
        const qs = inviteId ? `?invite=${encodeURIComponent(inviteId)}` : '';
        const r = await fetch(`/api/mu/auth/oauth/${platform}/start${qs}`, { credentials: 'same-origin' });
        const body = await r.json().catch(() => null);
        if (r.ok && body?.authorize_url) {
          window.location.href = body.authorize_url; // 成功跳转：保持 pending（页面即将离开）
          return;
        }
        if (body?.error?.reason === 'invitation_not_found') {
          setInviteError('邀请无效——可能已被领取或已过期。可返回普通登录，或联系管理员重发邀请');
        } else if (body?.error?.reason === 'invite_provider_mismatch') {
          setInviteError(mismatchCopy(body?.error?.invite_provider));
        } else if (body?.error?.reason === 'oauth_not_configured') {
          setFormError('该登录方式尚未开放——请联系管理员启用后重试');
        } else {
          setFormError(`无法发起 ${PLATFORM_NAME[platform === 'github' ? 'github-oauth' : 'gitee-oauth'] ?? '登录'}，请稍后重试；持续失败请联系管理员`);
        }
      } catch { setFormError('网络错误 — 无法连接后端，请检查网络后重试'); }
      finish();
    })();
  };
  const startGithub = () => startOAuth('github');
  const startGitee = () => startOAuth('gitee');

  const submitLegacy = async ({ user, password }) => {
    setPending("legacy"); setFormError(null);
    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ user, password }),
      });
      if (!res.ok) {
        if (res.status === 403) setFormError('操作员账号登录在此部署未开放——请使用平台账号入口登录');
        else if (res.status === 503) setFormError('登录服务暂未开放——请稍后重试');
        else setFormError('用户名或密码不正确');
        return;
      }
      auth.refresh();
    } catch {
      setFormError('网络错误 — 无法连接后端');
    } finally {
      setPending(null);
    }
  };

  const clearInvite = () => {
    setInviteId(null); setInviteError(null);
    try { window.history.replaceState(null, '', window.location.pathname); } catch { /* ignore */ }
  };

  // fixture 自助登录提交：身份名 → subject=fixture:<名称>（与管理员预置身份同一命名）。
  // 后端门禁（MU_ALLOW_FIXTURE_LOGIN）为最终授权方：已关闭/身份未预置/无有效成员关系
  // 均如实报错，不猜测原因。
  const submitFixture = async ({ name }) => {
    setPending('fixture'); setFormError(null);
    try {
      const res = await fetch('/api/mu/auth/login', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ provider: 'fixture', subject: `fixture:${String(name).trim()}` }),
      });
      const body = await res.json().catch(() => null);
      if (!res.ok) {
        const why = { fixture_login_disabled: '此部署未开放身份登录',
          identity_unknown: '该身份未由管理员预置——请核对名称或联系管理员',
          no_active_membership: '该身份暂无有效成员关系——请联系管理员' };
        setFormError(why[body?.error?.reason] ?? '身份登录未完成——请稍后重试或联系管理员');
        return;
      }
      // 登录成功直接进入系统：会话已建立（cookie 已设），停留在 /login 只会看到
      // 「已登录」提示而无可去之处（2026-10-16 用户实测死端）。
      auth.refresh();
      window.location.assign('/');
    } catch {
      setFormError('网络错误 — 无法连接后端');
    } finally {
      setPending(null);
    }
  };

  return (
    <div className="login-wrap">
      {/* padding 单一来源：body padding 由 console.css 的 .login-card 规则统一
          （此前内联 body padding 与卡片类 padding 叠加造成双重留白，登录来源波消除） */}
      <Card className="login-card" variant="borderless">
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
            {loginError && !staleNotConfigured && (
              <Alert type="warning" showIcon style={{ marginTop: 16 }}
                     message={LOGIN_ERROR_COPY[loginError] ?? '登录未完成——请重新发起登录'} />
            )}
            {/* /login 对已登录用户也渲染（受邀认领需以受邀身份重走 OAuth）；行为如实告知。
                文案 provider 中性——受邀身份核验走的是用户实际点击的平台入口，不写死。
                非受邀上下文的已登录用户给出「进入系统」入口——/login 不自动跳转是受邀
                场景的设计，但已登录用户停留本页不应形成死端（2026-10-16 用户实测卡点）。 */}
            {auth.status === 'authed' && (
              inviteId ? (
                <Alert type="info" showIcon style={{ marginTop: 16 }}
                       message="你当前已登录——继续操作将以你点击的平台入口核验受邀身份" />
              ) : (
                <Alert type="info" showIcon style={{ marginTop: 16 }} message="你当前已登录"
                       action={<Button type="primary" size="small"
                                 onClick={() => window.location.assign('/')}>进入系统</Button>} />
              )
            )}
            {inviteId && !inviteError && (
              <Alert type="info" showIcon style={{ marginTop: 16 }}
                     message="检测到受邀访问——请使用邀请指定的平台入口登录；选错平台不会消耗邀请" />
            )}
            {inviteError && (
              <Alert type="warning" showIcon style={{ marginTop: 16 }} message={inviteError}
                     action={<Button size="small" onClick={clearInvite}>返回普通登录</Button>} />
            )}

            {/* 日常登录主入口（品牌主色）。渲染与可用完全由 providers 驱动：
                未配置的平台不展示入口（不伪装可用）；配置获取失败只重试，不放开备用登录。
                触控目标 ≥44px（login-oauth-btn）。不用 antd loading 图标（CSSMotion 在测试环境
                脆弱）——进行中用「正在前往 …」文案表达，另一入口禁用但保持原文。 */}
            {githubReady ? (
              <Button type="primary" block size="large" className="login-oauth-btn"
                      style={{ marginTop: 16, marginBottom: 8 }}
                      icon={<GitHubMark />}
                      disabled={pending !== null}
                      onClick={startGithub}>
                {pending === 'github' ? '正在前往 GitHub…'
                  : (inviteId ? '使用受邀的 GitHub 账号登录' : '使用 GitHub 登录')}
              </Button>
            ) : prov.phase === 'ready' ? (
              <Alert type="warning" showIcon style={{ marginTop: 16, marginBottom: 8 }}
                     message="GitHub 登录暂未开放——请联系管理员启用后重试" />
            ) : null}
            {/* Gitee 次入口：与主入口同规则（服务端 providers 配置驱动，未配置不渲染） */}
            {giteeReady && (
              <Button block size="large" className="login-oauth-btn"
                      style={{ marginBottom: 8 }}
                      icon={<GiteeMark />}
                      disabled={pending !== null}
                      onClick={startGitee}>
                {pending === 'gitee' ? '正在前往 Gitee…'
                  : (inviteId ? '使用受邀的 Gitee 账号登录' : '使用 Gitee 登录')}
              </Button>
            )}
            {prov.phase === 'loading' && (
              <Button type="primary" block size="large" style={{ marginTop: 16, marginBottom: 8 }}
                      disabled>正在获取登录方式…</Button>
            )}
            {prov.phase === 'error' && (
              <>
                <Alert type="error" showIcon style={{ marginTop: 8 }}
                       message="登录配置获取失败——无法确认可用登录方式（不会回退到其他入口）" />
                <Button block style={{ marginTop: 8 }} onClick={loadProviders}>重试获取登录配置</Button>
              </>
            )}

            {/* fixture 自助登录（仅服务端披露已开放的部署渲染；隔离试用/开发身份，非平台 OAuth）。
                与 legacy 表单同形：字段 label 明确、触控目标 ≥44px、进行中禁用。 */}
            {fixtureReady && multiuserMode ? (
              <div style={{ marginTop: 16 }}>
                <div style={{ textAlign: 'center', color: '#999', margin: '10px 0 0' }}>—— 或使用试用身份 ——</div>
                <Form layout="vertical" onFinish={submitFixture} disabled={pending !== null} style={{ marginTop: 12 }}>
                  <Form.Item label="试用身份名" name="name"
                             rules={[{ required: true, message: '请输入管理员预置的身份名（如 trial-maintainer）' }]}
                             style={{ marginBottom: 10 }}>
                    <Input id="login-fixture-name" prefix={<UserOutlined aria-hidden="true" />}
                           placeholder="例如 trial-maintainer" autoComplete="username" size="large" />
                  </Form.Item>
                  <Button type="default" htmlType="submit" block size="large" className="login-oauth-btn"
                          disabled={pending !== null}>
                    {pending === 'fixture' ? '正在登录…' : '以试用身份登录'}
                  </Button>
                </Form>
                <div className="login-scope" role="note" style={{ marginTop: 8 }}>
                  <span>隔离试用/开发身份（非平台账号登录）；可用身份由管理员预置。</span>
                </div>
              </div>
            ) : null}

            {/* 邀请制提示（multiuser 生产形态） */}
            {multiuserMode ? (
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
                <Form layout="vertical" onFinish={submitLegacy} disabled={pending !== null} style={{ marginTop: 16 }}>
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
                  <Button type="primary" htmlType="submit" block size="large" loading={pending !== null}>
                    登录
                  </Button>
                </Form>
              </>
            ) : null}
            {formError && <Alert type="error" message={formError} showIcon style={{ marginTop: 12 }} />}

            {/* 数据范围说明与演示入口：仅 legacy/本地形态（multiuser 生产登录页不显示） */}
            {!multiuserMode && (
              <>
                <div className="login-scope" role="note">
                  <strong>当前数据范围</strong>
                  <span>登录后：授权仓库的 PR 审查、证据与审计（实时只读）。</span>
                  <span>未登录或演示：不加载实时数据（未认证请求会被接口拒绝）。</span>
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
