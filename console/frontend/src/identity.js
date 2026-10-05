// identity.js — 身份与能力映射（数据可信度加固 2026-10-05 三波）。
// 单一权威映射：footer / 工作区面板 / 设置 / 诊断 共用，不再各自文案。
//
// 认证 provider 只认后端会话的显式标记（不得按 multiuser 模式推断 OAuth）：
//   user.login_type / user.mu.provider === 'github-oauth'  → GitHub OAuth 会话
//     （mu_session 仅由 GitHub OAuth 回调签发，backend multiuser/oauth.mjs；
//      /api/auth/session 的 MU 桥接与 legacy 桥接均显式下发 login_type）
//   user.login_type === 'operator_password'                → 操作员账号密码登录
//   fixture / console-pg 测试主体（dataMode 声明）           → 隔离测试主体
//   已登录但无任何显式标记                                   → 来源未知（诚实，不猜）
//
// 能力边界（不随 provider 变化的恒定红线）：
//   操作经服务端授权；不写 GitHub、不自动合并、不派发站外执行。

const CAPABILITY_LINE = '操作经服务端授权 · 不写 GitHub · 不自动合并';

// 会话证据 → 身份来源（纯函数）。@param session 形如 /api/auth/session 200 body。
export function deriveIdentitySource({ session, dataMode, authed } = {}) {
  if (!authed) return { key: 'anonymous', label: '未认证', realGithubIdentity: false };
  const user = session?.user ?? null;
  const loginType = String(user?.login_type ?? user?.mu?.provider ?? '').toLowerCase();
  // 测试主体：数据面被可信配置声明为 fixture（test-auth/test-principal）
  if (dataMode === 'fixture') {
    return { key: 'test_principal', label: '隔离测试主体', realGithubIdentity: false };
  }
  if (loginType === 'github-oauth' || session?.session_source === 'mu_session') {
    return { key: 'github_oauth', label: 'GitHub OAuth 会话', realGithubIdentity: true };
  }
  if (loginType === 'operator_password') {
    return { key: 'operator_password', label: '操作员账号密码登录', realGithubIdentity: false };
  }
  // 已登录但无显式 provider 标记——旧会话或契约缺口：如实显示未知，不冒充
  return { key: 'unknown', label: '登录来源未知', realGithubIdentity: false };
}

// 侧栏 footer / 设置 / 诊断共用的一行能力边界（provider 感知）
export function capabilityLine(identity) {
  switch (identity?.key) {
    case 'github_oauth':
      return `GitHub OAuth 会话 · ${CAPABILITY_LINE}`;
    case 'operator_password':
      return `操作员密码登录 · ${CAPABILITY_LINE}`;
    case 'test_principal':
      return '隔离测试主体 · 审批仅写 fixture 库 · 不触达真实系统';
    case 'anonymous':
      return '未认证 · 只读快照 · 无凭证下发';
    default:
      return `登录来源未知 · ${CAPABILITY_LINE}`;
  }
}

// 工作区面板「GitHub 身份」行的口径（区别于 footer 一行式；纯字符串）
export function identityDetail(identity, userName) {
  switch (identity?.key) {
    case 'github_oauth':
      return `${userName ?? '已登录'}（GitHub OAuth 会话，身份键=数字 user id）`;
    case 'operator_password':
      return `${userName ?? '已登录'}（操作员账号密码会话；非 GitHub 身份）`;
    case 'test_principal':
      return 'test-principal（隔离测试主体，非真实 GitHub 身份）';
    case 'anonymous':
      return '未认证——无真实 GitHub 身份';
    default:
      return `${userName ?? '已登录'}（已登录，会话未携带 provider 标记——来源未知，不冒充 GitHub 身份）`;
  }
}
