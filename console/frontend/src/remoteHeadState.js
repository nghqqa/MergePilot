// remoteHeadState.js — 远端 head 核验三态归一（纯函数，无 React/无 IO）。
// 背景（2026-10-17 首次使用模拟定案）：apply 预览 target 的错误键是 remote_error，
// 前端四处读 remote_head_error——键名错位使错误信息恒丢失，head_match=false 且
// remote_head 为空（远端不可达）时被二值渲染成「已变化——确认将被拒绝」。
// 真实语义三态（对用户承诺）：
//   match   = 已核实一致（版本匹配——不等于可应用/可合并，资格以服务端结果为准）；
//   changed = 已核实不同（有可信远端 head 证据）；
//   unreadable/unknown = 无法核实（缺错误键、缺远端 head、一致性未判定）——不得说「已变化」。

export function remoteHeadState(ctx = {}) {
  const err = ctx.remote_head_error ?? ctx.remote_error ?? null;
  const head = ctx.remote_head == null ? '' : String(ctx.remote_head);
  if (err) return { key: 'unreadable', reason: String(err) };
  if (!head) return { key: 'unknown', reason: '未能取得远端 head（远端不可达或未返回）' };
  if (ctx.head_match === true) return { key: 'match', head };
  if (ctx.head_match === false) return { key: 'changed', head };
  return { key: 'unknown', reason: '远端 head 已取得，但一致性未判定' };
}
