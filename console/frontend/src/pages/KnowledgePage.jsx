import React from 'react';
import { Link } from 'react-router-dom';
import { CloudUpload, Database, Wrench } from 'lucide-react';

const ITEMS = [
  {
    icon: Database,
    title: 'RAG 检索',
    status: '服务未接入',
    body: 'RAG 检索服务未接入控制台。每次运行的 RAG 调用记录（含数据模式标注）已在 run 详情内保留历史快照。',
    tech: '技术详情：内部检索服务 rag-live（端口 :4184）尚未接入控制台。',
    note: '后续接入时将标注 RAG 索引版本，保证结论可追溯到具体索引。',
  },
  {
    icon: Wrench,
    title: 'Skill 版本',
    status: '版本治理已上线',
    body: '技能版本治理已在「技能」页上线：注册技能、发布带完整性指纹的不可变版本、激活/回滚当前生效版本、停用/启用，全程审计。',
    to: '/skills',
    linkLabel: '前往技能治理',
    note: '数据面仍未接入：MinIO skill store / worker 上报未接入。run 内实际 Skill 调用审计在 run 详情「Skill」标签（包内记录，不以 worker 当前版本冒充）。',
  },
  {
    icon: CloudUpload,
    title: '用量',
    status: '数据源未接入',
    body: '逐 run 计量数据源未接入。历史包内的 usage-summary（含窗口匹配口径说明）在 run 详情「用量」标签。',
    note: '无价目表不显示金额，不估算。',
  },
];

// 知识库：数据面未接入的诚实标注集中一处；治理面（技能版本）已上线并给入口，不谎报数据面。
export default function KnowledgePage() {
  return (
    <div>
      <div className="page-head">
        <div>
          <h1>知识库</h1>
          <p className="page-sub">
            RAG / Skill / 用量集中在此：技能版本治理已上线（见下方入口），其余数据面未接入实时数据；
            历史证据在对应 run 详情内。
          </p>
        </div>
      </div>
      <div className="knowledge-list">
        {ITEMS.map(({ icon: Icon, title, status, body, tech, note, to, linkLabel }) => (
          <div key={title} className="panel knowledge-card">
            <div className="knowledge-head">
              <span className="stub-ico knowledge-ico"><Icon size={18} strokeWidth={1.75} aria-hidden /></span>
              <div>
                <h3 className="knowledge-title">{title}</h3>
                <span className="chip">{status}</span>
              </div>
            </div>
            <p className="knowledge-body" title={tech}>
              {body}
              {to ? <> <Link to={to}>{linkLabel} →</Link></> : null}
            </p>
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
