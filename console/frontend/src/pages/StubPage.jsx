import React from 'react';
import { Link } from 'react-router-dom';

const REASONS = {
  仓库: '依赖 GitHub App installation / 仓库授权数据源，当前未接入任何实时接口。运行列表中的仓库字段来自历史证据包。',
  RAG: 'RAG 服务状态页依赖 rag-live 服务（:4184）与检索索引版本标识，当前未接入。历史 run 的检索调用快照已在各运行详情页呈现。',
  Skill: 'Skill 版本总览依赖 MinIO skill store 与 worker 侧上报（备忘九.2 run-manifest），历史包内仅有逐 run 调用审计。',
  审批: '真实批准/拒绝需 M2 决策项（D-1 动作集 / D-2 审批人映射 / D-3 TTL）拍板 + 票据存储落地 + 后端权威校验，均由后端工作流负责。拍板前不提供任何可点击的审批操作，避免"用户批准了 A、系统执行了 B"。',
  用量: '用量总览与预算状态依赖 usage 数据源接入（OTel collector 或 worker 台账，R6 二选一未决）与预算金额拍板。历史包内实测用量已在各运行详情页呈现。',
  页面: '该路由不存在。',
};

export default function StubPage({ section }) {
  return (
    <div>
      <div className="page-head">
        <h1>{section}</h1>
      </div>
      <div className="state-box state-stub">
        <div className="stub-title">未接入</div>
        <p>{REASONS[section] ?? '该功能依赖未接入的后端能力，如实标注，不以演示数据填充。'}</p>
        <p className="muted">详见 docs/productization/console/INTEGRATION-REQUESTS.md（给后端会话的接口需求清单）。</p>
        <Link className="btn" to="/runs">前往运行列表（已接入）</Link>
      </div>
    </div>
  );
}
