// identity.js — 身份与能力映射（数据可信度加固 2026-10-05 三波；登录来源波修订）。
// 单一权威映射：footer / 工作区面板 / 设置 / 诊断 共用，不再各自文案。
//
// 认证 provider 只认后端会话的显式标记（不得按 multiuser 模式推断 OAuth，
// 也不按身份绑定列表/用户名/前端缓存推断）：
//   user.login_type === 'github-oauth'  → GitHub OAuth 会话
//   user.login_type === 'gitee-oauth'   → Gitee OAuth 会话
//     （两个 session 端点按 lib/muLoginType.mjs 白名单契约显式下发；白名单外
//      与缺失一律不下发——见 backend 契约与 mu-session-source.integration.mjs）
//   user.login_type === 'operator_password' → 操作员账号密码登录
//   fixture / console-pg 测试主体（dataMode 声明）          → 隔离测试主体
//   已登录但无显式标记                                       → 来源未知（诚实，不猜）
//   （旧版这里存在 session_source==='mu_session' → GitHub 的兜底——已删除：
//    该兜底在 Gitee 会话上恰好构成「误标 GitHub」的冒充路径；旧会话兼容由
//    mu.session.provider 列 v1 NOT NULL + 后端白名单下发保证，不需要前端猜测。）
//
// 能力边界（不随 provider 变化的恒定红线；2026-10-16 事实校正——
// 应用补丁/合并 PR 已交付：均为独立预览+确认后由系统机器人执行远端写入；
// 旧「不写 GitHub」宣称与实现冲突，已废除。「不自动合并」仍为真实约束）：
//   操作经服务端授权；远端写入逐项预览确认；不自动合并。

const CAPABILITY_LINE = '操作经服务端授权 · 远端写入逐项预览确认 · 不自动合并';

// 会话证据 → 身份来源（纯函数）。@param session 形如 /api/auth/session 200 body。
export function deriveIdentitySource({ session, dataMode, authed } = {}) {
  if (!authed) return { key: 'anonymous', label: '未认证', realGithubIdentity: false };
  const user = session?.user ?? null;
  const loginType = String(user?.login_type ?? user?.mu?.provider ?? '').toLowerCase();
  // 测试主体：数据面被可信配置声明为 fixture（test-auth/test-principal）
  if (dataMode === 'fixture') {
    return { key: 'test_principal', label: '隔离测试主体', realGithubIdentity: false };
  }
  if (loginType === 'gitee-oauth') {
    return { key: 'gitee_oauth', label: 'Gitee OAuth 会话', realGithubIdentity: true };
  }
  if (loginType === 'github-oauth') {
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
    case 'gitee_oauth':
      return `Gitee OAuth 会话 · ${CAPABILITY_LINE}`;
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
    case 'gitee_oauth':
      return `${userName ?? '已登录'}（Gitee OAuth 会话，身份键=数字 user id）`;
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
