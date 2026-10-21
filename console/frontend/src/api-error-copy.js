'use strict';
// 共享 API 错误文案（纯函数）：按服务端机器码/HTTP 状态区分会话、授权、对象缺失、
// 依赖缺失、状态冲突与系统故障。纪律：直白语言、不泄露无权对象存在性、给可用处理方式。
export function apiErrorCopy({ status, message } = {}) {
  const code = String(message ?? '');
  if (status === 401) {
    return { tone: 'warning', text: '登录已过期', detail: '请刷新页面重新登录。' };
  }
  if (status === 403) {
    if (code === 'binding_required') {
      return { tone: 'warning', text: '仓库尚未绑定或绑定已失效', detail: '请先在仓库页完成绑定，再执行此操作。' };
    }
    if (code === 'membership_inactive') {
      return { tone: 'warning', text: '成员资格已停用', detail: '请联系管理员恢复成员资格后再试。' };
    }
    return { tone: 'warning', text: '无权限', detail: '当前账号的角色没有执行此操作的权限。请切换有相应权限的账号，或联系管理员开通。' };
  }
  if (status === 404) {
    return { tone: 'warning', text: '不存在或无权访问', detail: '请确认编号是否正确；如应存在而看不到，请联系管理员确认权限。' };
  }
  if (status === 400) {
    if (code === 'cannot_change_self_role') {
      return { tone: 'warning', text: '不能修改自己的角色', detail: '为防误操作锁死管理入口，页面上不能调整自己的角色；如需变更请联系其他平台管理员或 DBA。' };
    }
    if (code === 'cannot_revoke_self') {
      return { tone: 'warning', text: '不能撤销自己的成员资格', detail: '为防误操作锁死管理入口，页面上不能撤销自己；如需变更请联系其他平台管理员或 DBA。' };
    }
    if (code === 'platform_admin_not_assignable') {
      return { tone: 'warning', text: '不能授予 platform_admin', detail: 'platform_admin 仅限 DBA 直接授权——页面上的角色选项不含它（与邀请同一条边界）。' };
    }
  }
  if (status === 409) {
    return { tone: 'warning', text: '当前状态不允许此操作', detail: '请刷新页面按最新状态重试。' };
  }
  if (status >= 500) {
    return { tone: 'error', text: '服务暂时不可用', detail: '请稍后重试；持续失败请联系管理员检查服务状态。' };
  }
  if (status === 0 || status == null) {
    return { tone: 'error', text: '网络异常', detail: '请检查网络后重试。' };
  }
  return { tone: 'error', text: `请求失败（${code || status}）`, detail: '' };
}
