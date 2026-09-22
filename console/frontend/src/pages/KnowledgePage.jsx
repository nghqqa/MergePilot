import React from 'react';
import { Link } from 'react-router-dom';
import { CloudUpload, Database, Wrench } from 'lucide-react';

const ITEMS = [
  {
    icon: Database,
    title: 'RAG 检索',
    status: '服务未接入',
    body: 'rag-live（:4184）未接入控制台。每次运行的 RAG 调用记录（含数据模式标注）已在 run 详情内保留历史快照。',
    note: '数据源需求见 C-6（RAG 索引版本标识）。',
  },
  {
    icon: Wrench,
    title: 'Skill 版本',
    status: '总览未接入',
    body: 'MinIO skill store / worker 上报未接入。run 内实际 Skill 调用审计在 run 详情「Skill」标签（包内记录，不以 worker 当前版本冒充）。',
    note: 'Skill 与内部执行信息放在详情/设置层，不占一级导航。',
  },
  {
    icon: CloudUpload,
    title: '用量',
    status: '数据源未接入',
    body: '逐 run 计量数据源未接（C-5）。历史包内的 usage-summary（含窗口匹配口径说明）在 run 详情「用量」标签。',
    note: '无价目表不显示金额，不估算。',
  },
];

// 知识库：未接入的数据面集中一处，诚实标注，不铺成多个看似可用的空模块。
export default function KnowledgePage() {
  return (
    <div>
      <div className="page-head">
        <div>
          <h1>知识库</h1>
          <p className="page-sub">RAG / Skill / 用量等数据面集中在此。均未接入实时数据；历史证据在对应 run 详情内。</p>
        </div>
      </div>
      <div className="knowledge-list">
        {ITEMS.map(({ icon: Icon, title, status, body, note }) => (
          <div key={title} className="panel knowledge-card">
            <div className="knowledge-head">
              <span className="stub-ico knowledge-ico"><Icon size={18} strokeWidth={1.75} aria-hidden /></span>
              <div>
                <h3 className="knowledge-title">{title}</h3>
                <span className="chip">{status}</span>
              </div>
            </div>
            <p className="knowledge-body">{body}</p>
            <p className="section-note">{note}</p>
          </div>
        ))}
      </div>
      <p className="section-note">
        逐 run 的历史证据（RAG 调用 / Skill 审计 / 用量窗口）见<Link to="/runs">运行历史</Link>。
      </p>
    </div>
  );
}
