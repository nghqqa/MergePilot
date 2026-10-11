// muLoginType.mjs — MU 会话 provider → 前端 login_type 的唯一契约映射（登录来源波）。
//
// 契约（两个 session 端点必须一致，见 test/mu-session-source.integration.mjs）：
//  * 已知 provider 白名单：'github-oauth' | 'gitee-oauth'（trim+小写归一后逐字匹配）；
//  * 白名单内 → 返回该值：/api/auth/session MU 桥与 /api/mu/session 下发一致的
//    login_type（/api/mu/session 同步 provider 字段），前端 identity.js 据实呈现；
//  * 白名单外（fixture / 未来 provider / 非字符串 / 空）→ 返回 null：端点**不得下发**
//    login_type（/api/mu/session 同步省略 provider），前端按「登录来源未知」诚实呈现——
//    绝不回退 'github-oauth' 冒充，也不按身份绑定列表/用户名推断来源。
//
// 旧会话兼容依据（契约而非猜测）：
//  * mu.session.provider 自 v1 建表即 TEXT NOT NULL（schema.mjs Phase 1）——不存在
//    需要回填的 NULL 行；GitHub 唯一签发时代产生的全部存量行即 'github-oauth'，
//    经本映射原样下发，旧会话显示不变；
//  * fixture 会话（provider='fixture'，MU_ALLOW_FIXTURE_LOGIN 门控）认证行为不变，
//    仅不再被误标为 GitHub（此前 legacy 桥硬编码 'github-oauth' 的缺陷）。

export const KNOWN_MU_LOGIN_TYPES = new Set(['github-oauth', 'gitee-oauth']);

// provider → login_type；未知/缺失一律 null（调用端据此省略字段，不冒充已知来源）。
export function knownMuLoginType(provider) {
  const p = typeof provider === 'string' ? provider.trim().toLowerCase() : '';
  return KNOWN_MU_LOGIN_TYPES.has(p) ? p : null;
}

// 邀请 expected_subject（'github-oauth:<id>' / 'gitee-oauth:<id>'）→ 所属平台 provider；
// 其余/缺失 → null（调用端 409 响应不下发 invite_provider，前端走中性文案）。
export function invitePlatformOf(expectedSubject) {
  const s = typeof expectedSubject === 'string' ? expectedSubject : '';
  if (s.startsWith('github-oauth:')) return 'github-oauth';
  if (s.startsWith('gitee-oauth:')) return 'gitee-oauth';
  return null;
}
