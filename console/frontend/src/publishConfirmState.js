// publishConfirmState.js — 发布评论确认门（纯函数，无 React/无 IO）。
// 背景（2026-10-18 发布契约收口）：后端 publishRun 在远端 head 读取失败/不一致时
// 严格拒写（remote_unreachable 502 / stale_head 409，均先于任何远端写调用）；
// 但前端发布抽屉的「确认发布」此前不随远端状态禁用——文案说「确认保持阻塞，
// 最终可执行性以服务端资格结果为准」而按钮可点，交互矛盾（用户点击后必被 502
// 拒绝、抽屉关闭，只剩一条消息）。本模块把预览结果的远端 head 状态归一为确认门：
//   match      → 可用（唯一放行态；服务端仍是最终授权方）；
//   changed    → 禁用（服务端本就 409 拒绝——前端如实呈现，不发必败请求）；
//   unreadable → 禁用（无法核实目标；检查连接器/凭据后重新预览即可恢复，路径不变）；
//   unknown    → 禁用（未取得远端 head 或一致性未判定）。
// 纪律：只收紧前端交互，不放宽任何权限或 head 绑定条件；服务端拒写逻辑零改动。
import { remoteHeadState } from './remoteHeadState.js';

export function publishConfirmState(ctx) {
  const s = remoteHeadState(ctx ?? {});
  if (s.key === 'match') return { blocked: false, reason: null };
  if (s.key === 'changed') {
    return { blocked: true, reason: '远端 head 已变化——服务端将拒绝发布（409，陈旧结果不发出）；PR 更新后重新审查即可获得新结果' };
  }
  if (s.key === 'unreadable') {
    return { blocked: true, reason: `远端 head 无法读取（${s.reason}）——确认保持阻塞；检查连接器/仓库访问条件后重新预览` };
  }
  return { blocked: true, reason: `${s.reason ?? '未能取得远端 head'}——确认保持阻塞；恢复后重新预览` };
}
