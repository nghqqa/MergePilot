// invite-link.js — 邀请链接适用范围判定（纯函数，供成员邀请面板与单测复用）。
// 仅做地址格式判定，不探测网络可达性：
//   https + 非回环 = 格式符合对外要求（不代表已验证公网可达；内网 HTTPS 同样适用）；
//   http 非回环    = 不符合推荐的对外 HTTPS 配置（内网 HTTP/HTTPS 场景可用）；
//   回环           = 仅本机可用；
//   缺失/不可解析  = 不生成链接（禁止静默回退到 window.origin 冒充外部链接）。
const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '::1']);

export function inviteLinkScope(callbackUrl) {
  if (!callbackUrl) {
    return { scope: 'unconfigured', origin: null, shareable: false,
      note: 'OAuth Callback 未配置——无法生成邀请链接；请先配置 MU_GITHUB_OAUTH_CALLBACK_URL（对外为公网 HTTPS）。' };
  }
  let u;
  try { u = new URL(callbackUrl, 'http://placeholder.invalid'); } catch {
    return { scope: 'invalid', origin: null, shareable: false,
      note: 'OAuth Callback 地址无法解析——请检查 MU_GITHUB_OAUTH_CALLBACK_URL。' };
  }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') {
    return { scope: 'invalid', origin: null, shareable: false,
      note: 'OAuth Callback 协议异常——须为 http(s)。' };
  }
  if (LOOPBACK_HOSTS.has(u.hostname) || u.hostname === '[::1]') {
    return { scope: 'loopback', origin: u.origin, shareable: false,
      note: `当前 Console 地址为回环（${u.origin}）——邀请链接仅本机可用；对外邀请须配置公网 HTTPS 的 MU_GITHUB_OAUTH_CALLBACK_URL。` };
  }
  if (u.protocol === 'http:') {
    return { scope: 'http', origin: u.origin, shareable: false,
      note: `当前 Console 地址为 HTTP（${u.origin}）——不符合推荐的对外 HTTPS 配置；该链接适用于内网环境。` };
  }
  return { scope: 'https', origin: u.origin, shareable: true,
    note: '地址格式符合对外 HTTPS 要求（未探测公网可达性；内网 HTTPS 同样适用）。' };
}
