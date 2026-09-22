import React from 'react';
import { useAuth } from '../auth.jsx';
import { BrandMark } from '../ui.jsx';

// 登录页：只做会话交互结构，不伪造登录成功。
// 后端认证方案未定（C-8 提案，GET /api/session=404）——不渲染假表单、不自建账号库。
// 提供"只读演示预览"：明确标注的非认证浏览方式，使用现有脱敏快照数据。
export default function LoginPage() {
  const auth = useAuth();

  return (
    <div className="login-wrap">
      <div className="login-card">
        <div className="login-brand">
          <BrandMark />
          <div>
            <div className="brand-name">MergePilot 管理控制台</div>
            <div className="brand-sub">运行取证台 · snapshot</div>
          </div>
        </div>

        {auth.status === 'unavailable' ? (
          <>
            <div className="state-box state-error" role="alert">服务不可达 — 无法连接控制台后端。服务不可用时不伪装成未登录循环跳转。</div>
            <button type="button" className="btn btn-primary login-main" onClick={auth.refresh}>重试连接</button>
          </>
        ) : auth.status === 'expired' ? (
          <>
            <div className="state-box state-warn" role="alert">会话已过期 — 请重新登录。会话状态由服务端给出（401），前端不自行判定。</div>
            <button type="button" className="btn btn-primary login-main" onClick={auth.refresh}>重新检查会话</button>
          </>
        ) : (
          <>
            <p className="login-note">
              登录能力由后端提供：身份与仓库权限的服务端方案尚未拍板
              （控制台不自建账号密码库，需求见 docs/productization/console/INTEGRATION-REQUESTS.md C-8）。
              因此本页没有可提交的登录表单 —— 不模拟成功登录。
            </p>
            <button type="button" className="btn btn-primary login-main" onClick={auth.enterDemo}>
              以只读演示预览进入
            </button>
            <p className="login-sub">
              演示预览 = 明确标注的未认证浏览：仅现有脱敏历史快照，页面顶部全程显示
              「只读演示预览 · 未认证」，可随时在设置中退出。真实 live 页面必须经过后端认证边界。
            </p>
          </>
        )}
      </div>
    </div>
  );
}
