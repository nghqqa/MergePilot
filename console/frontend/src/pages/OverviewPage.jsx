import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { Alert, Button, Drawer, Spin, Table, Tag, Typography } from 'antd';
import { Column, Line } from '@ant-design/plots';
import { useAuth } from '../auth.jsx';
import { useAppConfig } from '../App.jsx';
import { useDataSource } from '../hooks.js';
import { STAGE_ORDER, stageMap, toneToColor, SEVERITY, unknownEntry } from '../status-map.js';
import { fmtTime } from '../format.js';
import { RecoveryBox, StatCard } from '../ui.jsx';
import { deriveAnomalies, pendingReasonOf, bucketOf, isRunChecking } from '../anomalies.js';
import { ProtectionUnknownCard, ProtectionUnknownSummary } from '../components/ProtectionUnknownPanel.jsx';

// 审查工作台（/overview，UX 收敛重构 2026-10-05）。
// 5 秒原则：进入页面即回答四个问题——
//   ① 哪些 PR 需要我处理（首屏统计卡+待处理列表）
//   ② 哪些 PR 被阻断（已阻断=可点击主入口，点击即过滤下方列表）
//   ③ 阻断原因是什么（异常区四分建模：原因/影响/下一步）
//   ④ 下一步点击什么（统计卡/异常区/行操作全部可执行去向）
// 数据契约：全部来自 GET /api/overview 后端权威推导；MU 模式额外并读
// /api/mu/approvals?status=PENDING（风险列）与 /api/mu/prs（保护未知汇总）。
// 无数据/未接线/错误/401 诚实显示并附恢复按钮（RecoveryBox）；图表不用 fixture 填充。
const REFRESH_MS = 30_000;

async function apiGet(path) {
  const res = await fetch(path, { credentials: 'same-origin' });
  let body = null;
  try { body = await res.json(); } catch { /* non-json */ }
  if (!res.ok) {
    const err = new Error(body?.error?.reason || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return body;
}

function SourceDetail({ source, error, generatedAt }) {
  const hasGenerated = generatedAt != null && generatedAt !== '';
  return (
    <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
      <details>
        <summary style={{ cursor: 'pointer' }}>数据来源详情</summary>
        source: <span className="mono">{source || '—'}</span>
        {error ? <> · 错误: <span className="mono">{error}</span></> : null}
        {hasGenerated ? <> · 生成于 <span className="mono">{new Date(generatedAt).toLocaleString()}</span></> : null}
        · 阶段由后端权威状态推导（票据 / gate 审计 / 回执完整性 / head 排序）
      </details>
    </Typography.Paragraph>
  );
}

// stage tone → 图表语义色（与 CSS 变量同源的状态色，不允许装饰性随机配色）
const TONE_HEX = { ok: '#17b26a', info: '#2e90fa', warn: '#f79009', bad: '#f04438', neutral: '#98a2b3' };

// 统计卡点击 → 列表筛选（联动口径：统计与表格都从同一 rows 推导，数字=列表行数）
const FOCUS_FILTERS = [
  { key: 'attention', label: '待处理' },
  { key: 'blocked', label: '已阻断' },
  { key: 'anomaly', label: '异常' },
  { key: 'reviewing', label: '进行中' },
  { key: 'all', label: '全部' },
];

// ── MU 域附加数据（风险列 + 保护未知汇总）：失败如实降级，不阻塞主数据 ──
async function loadMuExtras(source) {
  const out = { tickets: [], protectionUnknown: [], ticketsErr: null, protectionErr: null };
  try {
    out.tickets = await source.listPending() ?? [];
  } catch (e) { out.ticketsErr = e; }
  try {
    const repos = (await source.listRepos() ?? []).filter((r) => r.binding_state === 'active' || r.binding_id);
    // /api/mu/prs 每个 head 一行（upsert 键含 head_sha）——保护未知按 PR 去重（取最新 head），
    // 否则同一 PR 重复计数、异常数虚高于真实 PR 数
    const unknownByPr = new Map();
    for (const r of repos) {
      const res = await fetch(`/api/mu/prs?repo_id=${encodeURIComponent(r.repo_id)}`, { credentials: 'same-origin' });
      if (!res.ok) continue;
      const body = await res.json().catch(() => null);
      for (const pr of body?.pull_requests ?? []) {
        if (String(pr.branch_protection_status ?? 'unknown') !== 'unknown') continue;
        const key = `${r.repo}#${pr.provider_pr_number}`;
        const prev = unknownByPr.get(key);
        if (!prev || String(pr.updated_at ?? '') > String(prev.updated_at ?? '')) {
          unknownByPr.set(key, { repo: r.repo, pr: pr.provider_pr_number,
            updated_at: pr.updated_at ?? null, hasActiveRun: false });
        }
      }
    }
    out.protectionUnknown = [...unknownByPr.values()];
  } catch (e) { out.protectionErr = e; }
  return out;
}

export default function OverviewPage() {
  const auth = useAuth();
  const config = useAppConfig();
  const navigate = useNavigate();
  const { source } = useDataSource(config);
  const isMu = source?.kind === 'multiuser';
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [lastRefresh, setLastRefresh] = useState(null);
  const [extras, setExtras] = useState(null); // MU 域附加数据（tickets/protectionUnknown）
  const [attempt, setAttempt] = useState(0);
  const [drawerRow, setDrawerRow] = useState(null);
  const [page, setPage] = useState(1);
  const tableRef = useRef(null);
  const [searchParams, setSearchParams] = useSearchParams();
  const focus = searchParams.get('focus') ?? 'all';

  const load = useCallback(async () => {
    setError(null);
    try {
      setData(await apiGet('/api/overview'));
      setLastRefresh(new Date().toLocaleTimeString());
    } catch (e) { setError(e); }
  }, []);

  useEffect(() => {
    if (auth.status !== 'authed') return;
    load();
    const t = setInterval(load, REFRESH_MS);
    return () => clearInterval(t);
  }, [auth.status, load, attempt]);

  // MU 域附加数据（风险列/保护未知）：随主数据刷新；失败如实降级
  useEffect(() => {
    if (auth.status !== 'authed' || !isMu || !source) return;
    let dead = false;
    loadMuExtras(source).then((out) => { if (!dead) setExtras(out); });
    return () => { dead = true; };
  }, [auth.status, isMu, source, attempt, data]);

  const setFocus = useCallback((f) => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.set('focus', f);
      return next;
    }, { replace: false });
    setPage(1);
    // 联动：统计卡点击后滚动到列表（视觉焦点立即落在"数字对应的行"上）
    requestAnimationFrame(() => tableRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
  }, [setSearchParams]);

  // ── 行归一：legacy pr_number 与 MU 投影 pr 双形状；缺值不出链接（绝无 /pr/undefined 死链）──
  //（Hooks 纪律：全部派生计算位于任何早返回之前——data=null 时安全退化空集）
  const rows = useMemo(() => {
    const list = ((data?.prs) ?? []).map((r) => {
      const n = r.pr_number ?? r.pr;
      const [owner, name] = String(r.repo ?? '').split('/');
      const detailTo = (n != null && owner && name)
        ? (isMu
          ? `/mu/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/pr/${n}`
          : `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/pr/${n}`)
        : null;
      return { ...r, n, owner, name, detailTo, bucket: bucketOf(r), reason: pendingReasonOf(r) };
    });
    return list;
  }, [data, isMu]);

  // ── 风险列：MU 域 PENDING 审批票按 repo#pr 关联（P0/P1=高危待批）──
  const ticketByPr = useMemo(() => {
    const m = new Map();
    for (const t of extras?.tickets ?? []) {
      const key = `${t.repo}#${t.prNumber}`;
      const prev = m.get(key);
      const rank = (s) => (['P0', 'CRITICAL'].includes(String(s)) ? 2 : ['P1', 'HIGH'].includes(String(s)) ? 1 : 0);
      if (!prev || rank(t.severity) > rank(prev.severity)) m.set(key, { severity: t.severity, count: (prev?.count ?? 0) + 1 });
      else m.set(key, { ...prev, count: prev.count + 1 });
    }
    return m;
  }, [extras]);

  // ── 联动统计（与表格同一 rows 推导——数字=点击后看到的行数）──
  const attentionRows = rows.filter((r) => r.bucket === 'attention');
  const blockedRows = rows.filter((r) => r.bucket === 'blocked');
  const anomalyRows = rows.filter((r) => r.bucket === 'anomaly');
  const protectionUnknown = extras?.protectionUnknown ?? [];
  const protectionCount = protectionUnknown.length;
  const ticketCount = isMu ? (extras?.tickets?.length ?? null) : (data?.pending_summary?.count ?? 0);
  const totalPrs = new Set(rows.map((r) => `${r.repo}#${r.n}`)).size;

  // 异常四分（原因/影响/下一步建模见 anomalies.js）
  const anomalies = deriveAnomalies(data, protectionCount);
  const anomalyTotal = anomalies.reduce((n, a) => n + a.count, 0);
  const extrasLoaded = extras != null;

  // ── 早返回（全部 hook 之后；data=null 安全退化已就绪）──
  // 未认证
  if (auth.status !== 'authed') {
    return (
      <div>
        <Typography.Title level={1} style={{ fontSize: 24 }}>审查工作台</Typography.Title>
        <Alert type="warning" showIcon message="需要登录"
          description="工作台数据受服务端会话与仓库 allowlist 保护（未认证返回 401）。"
          action={<Button type="primary" size="small" onClick={() => navigate('/multiuser')}>前往登录</Button>} />
      </div>
    );
  }
  // 加载中
  if (!data && !error) {
    return (
      <div>
        <Typography.Title level={1} style={{ fontSize: 24 }}>审查工作台</Typography.Title>
        <div className="state-box" role="status" style={{ padding: 48, justifyContent: 'center' }}>
          <Spin tip="正在读取工作台数据…" />
        </div>
      </div>
    );
  }
  // 错误：按状态给可执行恢复按钮（401/403/网络），不允许只有文字
  if (error) {
    return (
      <div>
        <Typography.Title level={1} style={{ fontSize: 24 }}>审查工作台</Typography.Title>
        <RecoveryBox error={error} mode={config?.mode} subject="工作台数据"
          onRetry={() => setAttempt((n) => n + 1)} />
      </div>
    );
  }

  const notWired = data.source === 'BACKEND_NOT_WIRED';
  const isErr = data.source === 'BACKEND_ERROR';

  // 保护未知行的 checking 推导：同名 PR 在 overview 行里处于进行中阶段
  const activePrKeys = new Set(rows
    .filter((r) => ['REVIEWING', 'PENDING'].includes(String(r.stage).toUpperCase()))
    .map((r) => `${r.repo}#${r.n}`));

  // 默认"全部"但按工作台优先级排序（待处理 → 已阻断 → 异常 → 其余）——
  // 首屏优先展示需要人的行，同时不隐藏其余行；统计卡点击仍可精确过滤。
  const BUCKET_RANK = { attention: 0, blocked: 1, anomaly: 2, normal: 3 };
  const sortedRows = [...rows].sort((a, b) =>
    (BUCKET_RANK[a.bucket] - BUCKET_RANK[b.bucket])
    || String(b.updated_at ?? '').localeCompare(String(a.updated_at ?? '')));
  const filtered = focus === 'all' ? sortedRows : rows.filter((r) => r.bucket === focus);
  const filterMeta = FOCUS_FILTERS.find((f) => f.key === focus) ?? FOCUS_FILTERS[0];

  // ── 图表数据（语义色 + 文本摘要）──
  const stageData = STAGE_ORDER
    .map((s) => ({ stage: stageMap(s).label, key: s, count: rows.filter((r) => String(r.stage).toUpperCase() === s).length }))
    .filter((d) => d.count > 0 || ['REVIEWING', 'ACTION_REQUIRED', 'BLOCKED', 'PASSED'].includes(d.key));
  const stageRange = stageData.map((d) => TONE_HEX[stageMap(d.key).tone] ?? TONE_HEX.neutral);

  const trend = data.trend ?? [];
  const trendTotal = trend.reduce((n, d) => n + (Number(d.runs) || 0), 0);
  const trendPeak = trend.reduce((best, d) => ((Number(d.runs) || 0) > (Number(best?.runs) || 0) ? d : best), null);

  const repoCounts = data.repository_counts ?? [];
  const repoTop = [...repoCounts].sort((a, b) => (Number(b.runs) || 0) - (Number(a.runs) || 0))[0] ?? null;

  const baseCol = {
    height: 220,
    xAxis: { label: { style: { fontSize: 11 } } },
    yAxis: { label: { style: { fontSize: 11 } } },
  };
  const snapshotSource = source?.kind === 'snapshot';

  const sevBadge = (severity) => {
    const m = SEVERITY[String(severity ?? '').toUpperCase()] ?? unknownEntry(severity);
    return <Tag color={toneToColor(m.tone)} title={`${m.note}（来自未决审批票——批准入口见审批页）`}>{m.label}</Tag>;
  };

  const columns = [
    { title: 'PR', ellipsis: true, render: (_, r) => {
      const label = r.n != null ? `${r.repo} #${r.n}` : `${r.repo}（PR 号缺失）`;
      return r.detailTo ? <Link to={r.detailTo}>{label}</Link> : label;
    } },
    { title: '风险', width: 110, render: (_, r) => {
      const t = ticketByPr.get(`${r.repo}#${r.n}`);
      if (t) return sevBadge(t.severity);
      return <span className="muted" title="无未决高危审批票（P0/P1 产生审批票；P2/P3 裁量入口在 PR 详情）">—</span>;
    } },
    { title: '阶段', width: 130, render: (_, r) => {
      const m = stageMap(r.stage);
      return <Tag color={toneToColor(m.tone)} title={m.note}>{m.label}</Tag>;
    } },
    { title: '待处理原因', ellipsis: true, render: (_, r) => (
      <span title={r.reason.detail}>{r.reason.text}</span>
    ) },
    { title: '更新时间', width: 150, render: (_, r) => (r.updated_at
      ? <span className="mono" title={new Date(r.updated_at).toLocaleString()}>{fmtTime(r.updated_at)}</span>
      : '—') },
    { title: '操作', width: 190, fixed: 'right', render: (_, r) => {
      const t = ticketByPr.get(`${r.repo}#${r.n}`);
      return (
        <span className="row-actions">
          <Button size="small" onClick={() => setDrawerRow(r)}
            aria-label={`查看详情：${r.repo}${r.n != null ? ` #${r.n}` : ''}`}>查看详情</Button>
          {t ? <Link className="btn btn-sm" to="/approvals"
            aria-label={`处理审批：${r.repo} #${r.n} 有待批准的高危修复票`}>处理审批</Link> : null}
        </span>
      );
    } },
  ];

  return (
    <div>
      <Typography.Title level={1} style={{ fontSize: 24, marginBottom: 4 }}>审查工作台</Typography.Title>
      <Typography.Paragraph type="secondary">
        需要我处理的 PR、阻断与异常——统计卡即入口，点击直接过滤下方列表。
        {lastRefresh ? `刷新于 ${lastRefresh} · 每 ${REFRESH_MS / 1000}s` : ''}
      </Typography.Paragraph>
      <SourceDetail source={data.source} error={data.error} generatedAt={data.generated_at} />

      {notWired ? (
        <Alert type="warning" showIcon message="后端未接线"
          description={<>CONSOLE_PG_DSN 未配置——以下为诚实零值，不是真实数据。配置见 <Link to="/core">系统状态</Link>。</>} />
      ) : isErr ? (
        <Alert type="error" showIcon message="后端错误"
          description={<>连接失败：<span className="mono">{data.error}</span>。
            <Button size="small" style={{ marginInlineStart: 8 }} onClick={() => setAttempt((n) => n + 1)}>重试</Button>
            {' '}或检查 <Link to="/datasources">数据源</Link>。</>} />
      ) : null}

      {/* ── 首屏统计卡：数字=点击后列表行数（直接联动） ── */}
      <div className="stat-row" role="group" aria-label="工作台统计（点击过滤下方列表）">
        <StatCard label="待处理（需我处理）" count={attentionRows.length} tone="warn"
          active={focus === 'attention'} onClick={() => setFocus('attention')}
          title="等待人工审批或裁定的 PR——点击在下方列表查看" />
        <StatCard label="待审批票" count={ticketCount ?? 0} tone={ticketCount ? 'warn' : 'neutral'}
          to="/approvals" title="未决的高危修复审批票（P0/P1）——前往审批页处理"
          loading={isMu && ticketCount == null} />
        <StatCard label="已阻断" count={blockedRows.length} tone={blockedRows.length ? 'bad' : 'neutral'}
          active={focus === 'blocked'} onClick={() => setFocus('blocked')}
          title="受控停止待人工裁定的 PR——点击在下方列表查看" />
        <StatCard label="异常" count={anomalyTotal} tone={anomalyTotal ? 'warn' : 'neutral'}
          active={focus === 'anomaly'} onClick={() => setFocus('anomaly')}
          title="Head 过期 / 失败回执 / 完整性冲突 / 保护状态未知——点击在下方列表查看" />
        <StatCard label="全部 PR" count={totalPrs} tone="neutral"
          active={focus === 'all'} onClick={() => setFocus('all')}
          title="当前 allowlist 内的全部 PR（按最新 run）" />
      </div>

      {/* ── 异常区：四分建模，每类给原因/影响/下一步 ── */}
      <section className="anomaly-panel" aria-label="异常状态（原因/影响/下一步）">
        <Typography.Title level={4} style={{ marginBottom: 8 }}>异常状态</Typography.Title>
        {anomalyTotal === 0 ? (
          <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
            当前无异常（Head 过期 0 · 失败回执 0 · 完整性冲突 0 · 保护状态未知
            {isMu && !extrasLoaded ? ' 检查中' : ` ${protectionCount}`}——诚实零值，不虚构）。
          </Typography.Paragraph>
        ) : (
          <div className="anomaly-grid">
            {anomalies.filter((a) => a.count > 0).map((a) => (
              <div key={a.key} className={`anomaly-card anomaly-tone-${a.tone}`}>
                <div className="anomaly-head">
                  <strong>{a.label}</strong>
                  <span className="anomaly-count">{a.count}</span>
                </div>
                <dl className="anomaly-facts">
                  <div><dt>原因</dt><dd>{a.cause}</dd></div>
                  <div><dt>影响</dt><dd>{a.impact}</dd></div>
                  <div><dt>下一步</dt><dd>
                    <button type="button" className="btn btn-sm" onClick={() => setFocus(a.next.filter)}>
                      {a.next.label}
                    </button>
                    <div className="muted" style={{ fontSize: 12 }}>{a.next.hint}</div>
                  </dd></div>
                </dl>
                {a.key === 'protection_unknown' && protectionCount > 0 ? (
                  <ProtectionUnknownSummary items={protectionUnknown} onOpenPr={(it) => {
                    const row = rows.find((r) => r.repo === it.repo && String(r.n) === String(it.pr));
                    if (row?.detailTo) navigate(row.detailTo);
                    else setFocus('anomaly');
                  }} />
                ) : null}
              </div>
            ))}
          </div>
        )}
        {extras?.protectionErr ? (
          <Alert type="warning" showIcon style={{ marginTop: 8 }}
            message="保护状态汇总不可得"
            description={<>PR 保护状态读取失败（{String(extras.protectionErr.message ?? extras.protectionErr)}）——不猜测数量。
              <Button size="small" style={{ marginInlineStart: 8 }} onClick={() => setAttempt((n) => n + 1)}>重试</Button></>} />
        ) : null}
      </section>

      {/* ── 工作台列表（默认待处理优先；统计卡/异常区点击即过滤） ── */}
      <section ref={tableRef} aria-label="PR 工作列表">
        <div className="wb-table-head">
          <div>
            <Typography.Title level={3} style={{ marginBottom: 0 }}>PR 列表（{filterMeta.label}）</Typography.Title>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              按 run 一行（同一 PR 的多个 head 各占一行，旧 head 标记为过期）；统计卡口径按 PR 去重。
            </Typography.Text>
          </div>
          <div className="wb-filter-chips" role="group" aria-label="列表筛选">
            {FOCUS_FILTERS.map((f) => (
              <button key={f.key} type="button"
                className={`qf-chip${focus === f.key ? ' qf-active' : ''}`}
                aria-pressed={focus === f.key}
                onClick={() => setFocus(f.key)}>{f.label}</button>
            ))}
          </div>
        </div>
        <Table
          size="small" rowKey={(r) => `${r.repo}#${r.n}#${r.run_id ?? ''}`}
          pagination={{
            current: page, pageSize: 10, onChange: (p) => setPage(p), hideOnSinglePage: false,
            showTotal: (total, range) => `第 ${range[0]}-${range[1]} 项，共 ${total} 项`,
            itemRender: (pg, type, originalElement) => {
              const label = type === 'page' ? `第 ${pg} 页${pg === page ? '（当前页）' : ''}`
                : type === 'prev' ? '上一页' : type === 'next' ? '下一页'
                  : type === 'prev5' ? '向前 5 页' : type === 'next5' ? '向后 5 页' : undefined;
              if (label && React.isValidElement(originalElement)) {
                return React.cloneElement(originalElement, { 'aria-label': label, 'aria-current': type === 'page' && pg === page ? 'page' : undefined });
              }
              return originalElement;
            },
          }}
          scroll={{ x: 'max-content' }}
          dataSource={filtered}
          locale={{ emptyText: focus === 'all' ? '没有 PR 记录（诚实零值）' : `没有「${filterMeta.label}」分类下的 PR——可切回"全部"查看` }}
          columns={columns}
        />
      </section>

      {/* ── 详情抽屉：Head / Run / 阶段来源 / 保护状态（首屏表格瘦身移入此处） ── */}
      <Drawer open={drawerRow != null} onClose={() => setDrawerRow(null)} width={480}
        title={drawerRow ? `${drawerRow.repo}${drawerRow.n != null ? ` #${drawerRow.n}` : ''} · 运行详情` : '运行详情'}
        destroyOnClose>
        {drawerRow ? (
          <div className="wb-drawer">
            {drawerRow.detailTo ? (
              <Typography.Paragraph>
                <Link className="btn" to={drawerRow.detailTo}>打开 PR 详情页（审查管线/风险项/操作）</Link>
              </Typography.Paragraph>
            ) : null}
            <dl className="kv-grid">
              <div className="kv"><div className="kv-label">Head</div>
                <div className="kv-value"><span className="sha mono" title={drawerRow.head_sha ?? ''}>{drawerRow.head_sha?.slice(0, 12) || '—'}</span></div></div>
              <div className="kv"><div className="kv-label">Run</div>
                <div className="kv-value"><span className="mono">{drawerRow.run_id || '—'}</span></div></div>
              <div className="kv"><div className="kv-label">阶段</div>
                <div className="kv-value">
                  {(() => { const m = stageMap(drawerRow.stage);
                    return <Tag color={toneToColor(m.tone)}>{m.label}</Tag>; })()}
                </div></div>
              <div className="kv"><div className="kv-label">阶段来源</div>
                <div className="kv-value"><span className="mono" style={{ fontSize: 12 }}>{drawerRow.stage_source || '—'}</span></div></div>
              <div className="kv"><div className="kv-label">待处理原因</div>
                <div className="kv-value">{drawerRow.reason.text}
                  <div className="muted" style={{ fontSize: 12 }}>{drawerRow.reason.detail}</div></div></div>
              <div className="kv"><div className="kv-label">更新时间</div>
                <div className="kv-value">{drawerRow.updated_at ? new Date(drawerRow.updated_at).toLocaleString() : '—'}</div></div>
            </dl>
            {(() => {
              const pu = protectionUnknown.find((x) => x.repo === drawerRow.repo && String(x.pr) === String(drawerRow.n));
              if (!pu) return null;
              const hasActiveRun = activePrKeys.has(`${drawerRow.repo}#${drawerRow.n}`) || isRunChecking(drawerRow.stage);
              return (
                <div style={{ marginTop: 12 }}>
                  <Typography.Title level={5}>保护状态未知</Typography.Title>
                  <ProtectionUnknownCard pr={drawerRow} hasActiveRun={hasActiveRun}
                    onRetry={() => setAttempt((n) => n + 1)} />
                </div>
              );
            })()}
            <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 12 }}>
              gate 审计与票据明细见 <Link to="/core">系统状态</Link>。
            </Typography.Paragraph>
          </div>
        ) : null}
      </Drawer>

      {/* ── 图表区（降级到下方：可视化摘要；语义色+点击联动+文本摘要） ── */}
      <div className="ov-charts" role="region" aria-label="运营图表（可视化摘要；点击可跳转对应列表，明细见文字摘要与表格）">
        <section aria-label="各阶段 PR 数量">
          <Typography.Title level={3}>各阶段 PR</Typography.Title>
          <div className="ov-chart-box">
            {stageData.length === 0 ? (
              <div className="ov-chart-empty" role="status">没有阶段数据（{data.source}——不渲染空轴冒充）</div>
            ) : (
              <>
                <Column
                  {...baseCol}
                  data={stageData}
                  xField="stage" yField="count" colorField="key"
                  scale={{ color: { range: stageRange } }}
                  legend={false}
                  onEvent={(_, event) => {
                    if (event.type === 'element:click') {
                      const key = event?.data?.data?.key;
                      const f = ['ACTION_REQUIRED'].includes(key) ? 'attention'
                        : key === 'BLOCKED' ? 'blocked'
                          : ['STALE', 'UNKNOWN'].includes(key) ? 'anomaly' : 'all';
                      setFocus(f);
                    }
                  }}
                />
                <p className="ov-chart-summary">
                  共 {rows.length} 项：待处理 {attentionRows.length} · 已阻断 {blockedRows.length} ·
                  异常（过期/未知）{anomalyRows.length} · 点击柱体可在上方列表过滤对应分类。
                </p>
              </>
            )}
          </div>
        </section>
        <section aria-label="最近运行趋势（14 天）">
          <Typography.Title level={3}>最近运行趋势（14 天）</Typography.Title>
          <div className="ov-chart-box">
            {trend.length === 0 ? (
              <div className="ov-chart-empty" role="status">没有趋势数据（{data.source}——不渲染空轴冒充）</div>
            ) : (
              <>
                <Line
                  {...baseCol}
                  data={trend} xField="date" yField="runs"
                  point={{ size: 3 }}
                  onEvent={snapshotSource ? (_, event) => {
                    if (event.type === 'element:click') navigate('/runs');
                  } : undefined}
                  style={{ cursor: snapshotSource ? 'pointer' : 'default' }}
                />
                <p className="ov-chart-summary">
                  近 14 天共 {trendTotal} 次运行{trendPeak && (Number(trendPeak.runs) || 0) > 0
                    ? `，峰值 ${trendPeak.date}（${trendPeak.runs} 次）` : ''}。
                  {snapshotSource ? '点击数据点打开运行记录。' : '当前数据源不提供 run 级全量历史——运行记录见各 PR 详情。'}
                </p>
              </>
            )}
          </div>
        </section>
        <section aria-label="各仓库分布">
          <Typography.Title level={3}>各仓库分布</Typography.Title>
          <div className="ov-chart-box">
            {repoCounts.length === 0 ? (
              <div className="ov-chart-empty" role="status">没有仓库分布数据（{data.source}——不渲染空轴冒充）</div>
            ) : (
              <>
                <Column
                  {...baseCol}
                  data={repoCounts}
                  xField="repo" yField="runs" colorField="repo"
                  legend={false}
                  onEvent={(_, event) => {
                    if (event.type === 'element:click') {
                      const repo = event?.data?.data?.repo;
                      if (repo && String(repo).includes('/')) {
                        const [o, n] = String(repo).split('/');
                        navigate(`/repos/${encodeURIComponent(o)}/${encodeURIComponent(n)}`);
                      }
                    }
                  }}
                />
                <p className="ov-chart-summary">
                  {repoCounts.length} 个仓库 · 共 {repoCounts.reduce((n, r) => n + (Number(r.runs) || 0), 0)} 项运行记录
                  {repoTop ? `，最多 ${repoTop.repo}（${repoTop.runs} 项）` : ''}。
                  点击柱体打开对应仓库的 PR 列表。
                </p>
              </>
            )}
          </div>
        </section>
      </div>

      {/* ── 健康与审计（次要信息，沉底） ── */}
      <Typography.Paragraph style={{ marginTop: 16 }}>
        健康：
        <Tag color={data.health.postgres === 'LIVE' ? 'success' : data.health.postgres === 'ERROR' ? 'error' : 'warning'}>
          {data.health.postgres === 'LIVE' ? 'PG 实时' : data.health.postgres === 'ERROR' ? 'PG 错误' : 'PG 未接线'}
        </Tag>
        <Tag color={data.health.minio.state === 'AGENTTEAMS_MANAGED' ? 'success' : 'default'} title={data.health.minio.note}>MinIO {data.health.minio.state === 'AGENTTEAMS_MANAGED' ? 'AgentTeams 管理' : data.health.minio.state === 'NOT_WIRED' ? '未接线' : data.health.minio.state}</Tag>
        <Tag color="success">后端只读 OK</Tag>
      </Typography.Paragraph>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
        <details><summary style={{ cursor: 'pointer' }}>审计入口</summary>
          gate 审计与票据明细见 <Link to="/core">系统状态</Link>；
          审批票处理见 <Link to="/approvals">审批</Link>；队列视图见 <Link to="/pending">待处理</Link>。
          REMEDIATING / VERIFYING 需要 Fixer/Verifier（本部署禁用）——计数恒 0，不虚构。
        </details>
      </Typography.Paragraph>
    </div>
  );
}
