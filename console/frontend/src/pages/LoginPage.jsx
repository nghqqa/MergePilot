import React from 'react';
import { useAppConfig } from '../App.jsx';
import { useAuth } from '../auth.jsx';
import { BrandMark } from '../ui.jsx';

// 登录页：只做会话交互结构，不伪造登录成功。
// 契约 v2（API-AUTH-MERGE-V0 @ 7ccecb9）已定：GitHub OAuth + 服务端会话（Cookie mp_session 为权威）。
// 后端尚未实现该端点（当前 GET /api/auth/session = 404）——如实标注"等待后端实现 + D-9 配置"。
// 只读演示预览 = 明确标注的非认证浏览，仅本地脱敏 snapshot 数据，不发起 live API 调用。
const STATUS_COPY = {
  unavailable: {
    box: ['state-error', '服务不可达 — 无法连接控制台后端。服务不可用时不伪装成未登录循环跳转。'],
    action: 'retry', label: '重试连接',
  },
  auth_unavailable: {
    box: ['state-error', '登录服务不可用（auth_unavailable）— 按契约产品不开放（含只读），不降级放行；只读演示预览亦不可进入。'],
    action: 'retry', label: '重试',
  },
  expired: {
    box: ['state-warn', '会话已过期（session_expired）— 请重新登录。会话状态由服务端给出，前端不自行判定。'],
    action: 'retry', label: '重新检查会话',
  },
  forbidden: {
    box: ['state-warn', '当前 GitHub 账号未获得准入（not_a_member）— 是否成员由服务端判定；如需访问请联系管理员。'],
    action: 'retry', label: '重新检查',
  },
};

export default function LoginPage() {
  const auth = useAuth();
  const config = useAppConfig();
  const pgMode = config?.mode === 'console-pg';
  const copy = STATUS_COPY[auth.status];

  return (
    <div className="login-wrap">
      <div className="login-card">
        <div className="login-brand">
          <BrandMark />
          <div>
            <div className="brand-name">MergePilot 管理控制台</div>
            <div className="brand-sub">运行取证台 · {pgMode ? '隔离 PG fixture' : 'snapshot'}</div>
          </div>
        </div>

        {copy ? (
          <>
            <div className={`state-box ${copy.box[0]}`} role="alert">{copy.box[1]}</div>
            <button type="button" className="btn btn-primary login-main" onClick={auth.refresh}>{copy.label}</button>
          </>
        ) : (
          <>
            <p className="login-note">
              {pgMode ? (
                <>
                  当前连接的是只读隔离 PG fixture 服务（tools/console_pg）：
                  认证未实现（GET /api/auth/session → 401 not_authenticated）——
                  页面将以未认证 fixture 状态浏览隔离 PG 测试记录（非真实运行）。
                </>
              ) : (
                <>
                  登录方式已定：GitHub OAuth + 服务端会话（契约 API-AUTH-MERGE-V0 v2 @ 7ccecb9；
                  会话 Cookie 为权威，浏览器不保存 App token / 私钥）。
                  后端尚未实现会话端点（当前 GET /api/auth/session = 404）——
                  本页没有可提交的登录表单，不模拟成功登录；真实启用依赖后端实现与 D-9 配置。
                </>
              )}
            </p>
            <button type="button" className="btn btn-primary login-main" onClick={auth.enterDemo}>
              以只读演示预览进入
            </button>
            <p className="login-sub">
              演示预览 = 明确标注的未认证浏览：{pgMode
                ? '仅隔离 PG 测试记录（fixture），不发起真实审批/写操作'
                : '仅本地脱敏历史快照，不绕过 live API 认证、不携带真实写权限'}；
              页面顶部全程显示未认证标识，可随时在设置中退出。
            </p>
          </>
        )}
      </div>
    </div>
  );
}
