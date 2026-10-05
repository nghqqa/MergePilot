import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Alert, Button, Segmented, Select } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { CloudUpload, Database, Wrench } from 'lucide-react';
import { Empty, SkeletonRows } from '../ui.jsx';
import { RUN_TRACE, runTraceMap } from '../status-map.js';
import RagModelInstallPanel from './RagModelInstallPanel.jsx';

const ITEMS = [
  {
    icon: Database,
    title: 'RAG 检索',
    status: '模型安装面板已上线',
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
];

// ── 用量区（rc.11 PR-B 最小用量统计面）：真实接入三只读聚合 API ──
// 边界如实声明（设计红线，两条固定声明恒显示）：
//  * 当前未提供 token 计量（后端 token_metering.available=false，无 token 来源）；
//  * 未配置价目表，不显示金额（后端 cost.available=false，不估算）；
//  * 统计仅覆盖 v2 管线运行——此前历史运行无调用留痕，不计入统计（不可用≠零用量）。
// 状态区分：loading（骨架屏）/ 空数据（诚实空态）/ 网络失败（错误+重试）/ 401（引导登录）/
// 403（无权限——后端 read_pull_request 判定）；status 词表全走 status-map.js 既有映射。
const USAGE_WINDOWS = [
  { label: '近 7 天', value: '7d' },
  { label: '近 30 天', value: '30d' },
  { label: '近 90 天', value: '90d' },
];
const USAGE_STATUS_OPTIONS = Object.keys(RUN_TRACE)
  .map((s) => ({ value: s, label: runTraceMap(s).label, title: s }));

async function muGet(path) {
  const res = await fetch(path, { credentials: 'same-origin' });
  let body = null; try { body = await res.json(); } catch { /* non-json */ }
  return { status: res.status, body };
}

const fmtMs = (v) => (v == null ? '—' : `${v} ms`);
const fmtRate = (v) => (v == null ? '—' : `${(v * 100).toFixed(1)}%`);
const fmtTime = (v) => (v == null ? '—' : `${String(v).replace('T', ' ').slice(0, 16)} UTC`);

// 用量统计面板：summary 数值卡 + 按 Skill 聚合表 + 按日分布表（只读 GET，零副作用）
function UsagePanel() {
  const [win, setWin] = useState('30d');
  const [status, setStatus] = useState(null);
  const [phase, setPhase] = useState('loading'); // loading|ready|error|unauthorized|forbidden
  const [errMsg, setErrMsg] = useState(null);
  const [data, setData] = useState(null); // { summary, bySkill, byPeriod }
  const [attempt, setAttempt] = useState(0);
  const [announce, setAnnounce] = useState('用量统计加载中');

  useEffect(() => {
    let alive = true;
    const q = new URLSearchParams({ window: win });
    if (status) q.set('status', status);
    setPhase('loading');
    setAnnounce('用量统计加载中');
    (async () => {
      try {
        const [sum, bySkill, byPeriod] = await Promise.all([
          muGet(`/api/mu/usage/summary?${q.toString()}`),
          muGet(`/api/mu/usage/by-skill?${q.toString()}&limit=50`),
          muGet(`/api/mu/usage/by-period?${q.toString()}`),
        ]);
        if (!alive) return;
        const st = sum.status;
        if (st === 401 || bySkill.status === 401 || byPeriod.status === 401) {
          setPhase('unauthorized'); setAnnounce('用量统计：未登录，请先登录');
        } else if (st === 403 || bySkill.status === 403 || byPeriod.status === 403) {
          setPhase('forbidden'); setAnnounce('用量统计：无权限查看');
        } else if (st !== 200 || bySkill.status !== 200 || byPeriod.status !== 200) {
          const reason = sum.body?.error?.reason ?? `HTTP ${st}`;
          setErrMsg(reason);
          setPhase('error'); setAnnounce('用量统计加载失败');
        } else {
          setData({ summary: sum.body, bySkill: bySkill.body, byPeriod: byPeriod.body });
          setPhase('ready');
          setAnnounce('用量统计已更新');
        }
      } catch {
        if (!alive) return;
        setErrMsg('network');
        setPhase('error'); setAnnounce('用量统计加载失败');
      }
    })();
    return () => { alive = false; };
  }, [win, status, attempt]);

  const s = data?.summary;
  const emptyData = phase === 'ready' && s && Number(s.skill?.total) === 0 && Number(s.rag?.total) === 0;

  const skillRows = (data?.bySkill?.rows ?? []).map((r, i) => ({ ...r, key: `${r.skill_key}:${i}` }));
  const periodRows = (data?.byPeriod?.rows ?? []).map((r) => ({ ...r, key: r.day }));

  return (
    <div className="panel knowledge-card">
      <div className="knowledge-head">
        <span className="stub-ico knowledge-ico"><CloudUpload size={18} strokeWidth={1.75} aria-hidden /></span>
        <div>
          <h3 className="knowledge-title">用量</h3>
          <span className="chip">只读统计已接入（v2 管线）</span>
        </div>
      </div>
      <p className="knowledge-body">
        租户内 Skill 调用与 RAG 检索的只读运营统计（不含任何调用正文——仅计数/耗时/时间）。
      </p>
      {/* 状态播报（aria-live）：屏幕阅读器可感知加载/成功/失败流转 */}
      <div aria-live="polite" style={{ position: 'absolute', left: -9999, width: 1, height: 1, overflow: 'hidden' }}>{announce}</div>

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, alignItems: 'center', marginBottom: 12 }}>
        <Segmented options={USAGE_WINDOWS} value={win} onChange={(v) => setWin(v)} aria-label="统计时间窗" />
        <Select
          allowClear
          placeholder="状态筛选（全部）"
          style={{ minWidth: 160 }}
          value={status}
          onChange={(v) => setStatus(v ?? null)}
          options={USAGE_STATUS_OPTIONS}
          aria-label="按调用状态筛选"
        />
        <Button icon={<ReloadOutlined />} onClick={() => setAttempt((n) => n + 1)}>刷新</Button>
      </div>

      {phase === 'loading' ? (
        <SkeletonRows rows={4} cols={5} />
      ) : phase === 'unauthorized' ? (
        <Alert type="info" showIcon message="未登录或会话已过期"
          description={<span>请先<Link to="/multiuser">前往登录</Link>，登录后即可查看本租户用量统计。</span>} />
      ) : phase === 'forbidden' ? (
        <Alert type="warning" showIcon message="无权限查看用量统计"
          description="当前成员角色缺少 PR 审查读取权限——请联系租户管理员调整角色。" />
      ) : phase === 'error' ? (
        <Alert type="error" showIcon
          message="用量统计加载失败"
          description={errMsg === 'network' ? '网络异常，请稍后重试。' : `请求失败（${errMsg}）`}
          action={<Button size="small" icon={<ReloadOutlined />} onClick={() => setAttempt((n) => n + 1)}>重试</Button>} />
      ) : emptyData ? (
        <Empty>当前时间窗内无调用记录（统计仅覆盖 v2 管线运行）</Empty>
      ) : s ? (
        <>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 10, marginBottom: 12 }}>
            <div className="kv"><div className="kv-label">Skill 调用总数</div>
              <div className="kv-value">{s.skill.total}<span className="muted" style={{ fontSize: 12 }}>（成功 {s.skill.succeeded} · 失败 {s.skill.failed}）</span></div></div>
            <div className="kv"><div className="kv-label">Skill 成功率</div><div className="kv-value">{fmtRate(s.skill.success_rate)}</div></div>
            <div className="kv"><div className="kv-label">Skill 耗时 p50 / p95</div><div className="kv-value">{fmtMs(s.skill.latency_p50_ms)} / {fmtMs(s.skill.latency_p95_ms)}</div></div>
            <div className="kv"><div className="kv-label">RAG 检索次数</div><div className="kv-value">{s.rag.total}</div></div>
            <div className="kv"><div className="kv-label">Skill 最近调用</div><div className="kv-value" style={{ fontSize: 13 }}>{fmtTime(s.skill.last_called_at)}</div></div>
          </div>

          <h4 style={{ margin: '4px 0 6px' }}>按 Skill 聚合</h4>
          {skillRows.length === 0 ? (
            <Empty>当前时间窗内无 Skill 调用记录</Empty>
          ) : (
            <div className="table-scroll">
              <table className="runs-table">
                <thead><tr>
                  <th>技能</th><th>次数</th><th>成功</th><th>失败</th><th>成功率</th><th>p50 耗时</th><th>最近调用</th>
                </tr></thead>
                <tbody>
                  {skillRows.map((r) => (
                    <tr key={r.key}>
                      <td><code>{r.skill_key}</code></td>
                      <td>{r.total}</td>
                      <td>{r.succeeded}</td>
                      <td>{r.failed}</td>
                      <td>{fmtRate(r.success_rate)}</td>
                      <td>{fmtMs(r.latency_p50_ms)}</td>
                      <td style={{ fontSize: 12 }}>{fmtTime(r.last_called_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {Number(data?.bySkill?.total_groups) > skillRows.length ? (
            <p className="section-note">共 {data.bySkill.total_groups} 个技能，当前显示前 {skillRows.length} 个（按调用次数排序）。</p>
          ) : null}

          <h4 style={{ margin: '12px 0 6px' }}>按日分布（UTC）</h4>
          {periodRows.length === 0 ? (
            <Empty>当前时间窗内无调用记录</Empty>
          ) : (
            <div className="table-scroll">
              <table className="runs-table">
                <thead><tr><th>日期（UTC）</th><th>Skill 调用</th><th>Skill 成功</th><th>RAG 检索</th></tr></thead>
                <tbody>
                  {periodRows.map((r) => (
                    <tr key={r.key}>
                      <td>{r.day}</td><td>{r.skill_total}</td><td>{r.skill_succeeded}</td><td>{r.rag_total}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="section-note">无数据日期在表中真实缺失（不补零），反映实际调用分布。</p>
        </>
      ) : null}

      {/* 两条固定声明（设计红线：恒显示，不依赖接口返回）+ 覆盖口径声明 */}
      <p className="section-note">
        当前未提供 token 计量；未配置价目表，不显示金额（不估算）。<br />
        统计仅覆盖 v2 管线运行——此前历史运行无调用留痕，不计入统计（不可用≠零用量）。
      </p>
    </div>
  );
}

// 知识库：治理面（技能版本）+ 用量只读统计已上线；RAG 检索数据面未接入并如实标注，
// 不谎报数据面。历史证据在对应 run 详情内。
export default function KnowledgePage() {
  return (
    <div>
      <div className="page-head">
        <div>
          <h1>知识库</h1>
          <p className="page-sub">
            RAG / Skill / 用量集中在此：技能版本治理与用量只读统计已上线（见下方入口与面板），
            RAG 检索数据面未接入实时数据；历史证据在对应 run 详情内。
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
      <UsagePanel />
      <RagModelInstallPanel />
      <p className="section-note">
        逐 run 的历史证据（RAG 调用 / Skill 审计 / 用量窗口）见<Link to="/runs">运行历史</Link>。
      </p>
    </div>
  );
}
