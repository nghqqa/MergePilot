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
import {
  deriveAnomalies, protectionUnknownKind, groupRowsByPr, pendingReasonOf, bucketOf,
  selectCurrentProtection, REVIEWING_STAGES,
} from '../anomalies.js';
import { ProtectionUnknownCard, ProtectionUnknownSummary } from '../components/ProtectionUnknownPanel.jsx';

// 审查工作台（/overview）。
// 数据口径统一（数据可信度修复 2026-10-05）：全部统计卡、筛选器、异常摘要与表格
// 使用同一套 PR 实体（groupRowsByPr 按 PR 去重，current=最新 head，history 收进抽屉）——
// 统计卡数字 = 点击后列表行数，逐卡相等；统计单位标注在卡上。
// 保护未知是 PR 级集合：并集进异常桶，点击「异常 PR」必能看到全部被计入的 PR
// （含 branch_protection_status=unknown）。focus=reviewing 匹配真实进行中阶段
// （REVIEWING/REMEDIATING/VERIFYING），不再恒空。
// 数据契约：GET /api/overview 权威；MU 模式并读 /api/mu/approvals?status=PENDING（风险列）
// 与 /api/mu/prs（保护未知按 PR 去重）。401/403/网络失败附恢复按钮（RecoveryBox）。
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

// 筛选桶：统计卡/筛选 chips/表格三方同源（PR 实体桶）
const FOCUS_FILTERS = [
  { key: 'attention', label: '待处理' },
  { key: 'blocked', label: '已阻断' },
  { key: 'anomaly', label: '异常' },
  { key: 'reviewing', label: '进行中' },
  { key: 'all', label: '全部' },
];
const BUCKET_RANK = { attention: 0, blocked: 1, anomaly: 2, reviewing: 3, normal: 4 };

// ── MU 域附加数据（风险列 + 保护未知汇总）：失败如实降级，不阻塞主数据 ──
// 数据可信度加固：先按 PR 定当前 head，再读该 head 的保护状态，最后筛 unknown
// （禁止「先筛 unknown 再取最新」——旧 head unknown 不得污染新 head known）；
// 单仓库请求失败计入 repoFailures（部分数据显式呈现，不静默跳过）。
async function loadMuExtras(source) {
  const out = { tickets: [], protectionUnknown: [], repoFailures: [], ticketsErr: null, protectionErr: null };
  try {
    out.tickets = await source.listPending() ?? [];
  } catch (e) { out.ticketsErr = e; }
  try {
    const repos = (await source.listRepos() ?? []).filter((r) => r.binding_state === 'active' || r.binding_id);
    // mu/prs 每 head 一行（upsert 键含 head_sha）→ 按 PR 分组取当前 head 行的保护状态
    const unknownByPr = new Map();
    for (const r of repos) {
      let res = null;
      try {
        res = await fetch(`/api/mu/prs?repo_id=${encodeURIComponent(r.repo_id)}`, { credentials: 'same-origin' });
      } catch { /* 网络失败 → 按失败计 */ }
      if (!res || !res.ok) {
        out.repoFailures.push({ repo: r.repo, status: res ? res.status : null });
        continue;
      }
      const body = await res.json().catch(() => null);
      const prRows = body?.pull_requests ?? [];
      const byPr = new Map();
      for (const pr of prRows) {
        const key = String(pr.provider_pr_number);
        if (!byPr.has(key)) byPr.set(key, []);
        byPr.get(key).push(pr);
      }
      for (const [num, rowsOfPr] of byPr) {
        const { status, inReview } = selectCurrentProtection(rowsOfPr);
        if (status !== 'unknown') continue; // 只计当前 head 的 unknown——known 的当前 head 不被旧 head 污染
        const cur = rowsOfPr.find((x) => String(x.provider_pr_number) === num);
        const key = `${r.repo}#${num}`;
        unknownByPr.set(key, { repo: r.repo, owner: r.owner, name: r.name,
          pr: Number(num), updated_at: cur?.updated_at ?? null, inReview });
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
  const [drawerEntity, setDrawerEntity] = useState(null);
  const [page, setPage] = useState(1);
  const listRef = useRef(null);
  const [searchParams, setSearchParams] = useSearchParams();
  const focus = searchParams.get('focus') ?? 'all';

  // 刷新失败保留旧数据（数据可信度加固）：已有数据时刷新失败 → 保留旧集合 +
  // refreshError 横幅（如实标注数据时点）；仅首载失败才进入整页错误态。
  const [refreshError, setRefreshError] = useState(null);
  const hasDataRef = useRef(false);
  useEffect(() => { hasDataRef.current = data != null; }, [data]);
  useEffect(() => {
    if (auth.status !== 'authed') return;
    let dead = false;
    const refreshOnce = async () => {
      try {
        const fresh = await apiGet('/api/overview');
        if (dead) return;
        setData(fresh); setRefreshError(null);
        setLastRefresh(new Date().toLocaleTimeString());
      } catch (e) {
        if (dead) return;
        if (hasDataRef.current) setRefreshError(e); // 保留旧数据（stale，时点见 lastRefresh）
        else setError(e); // 首载失败 → 整页错误态（RecoveryBox）
      }
    };
    refreshOnce();
    const t = setInterval(refreshOnce, REFRESH_MS);
    return () => { dead = true; clearInterval(t); };
  }, [auth.status, attempt]);

  // MU 域附加数据（风险列/保护未知）：随主数据刷新；失败如实降级
  useEffect(() => {
    if (auth.status !== 'authed' || !isMu || !source) return;
    let dead = false;
    loadMuExtras(source).then((out) => { if (!dead) setExtras(out); });
    return () => { dead = true; };
  }, [auth.status, isMu, source, attempt, data]);

  const refreshPageData = useCallback(() => setAttempt((n) => n + 1), []);

  const setFocus = useCallback((f) => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.set('focus', f);
      return next;
    }, { replace: false });
    setPage(1);
    // 键盘/读屏反馈：焦点移动到列表区域（可聚焦 region），滚动跟随
    requestAnimationFrame(() => {
      listRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      listRef.current?.focus({ preventScroll: true });
    });
  }, [setSearchParams]);

  // ── Hooks 纪律：全部派生计算在任何早返回之前（data=null 安全退化空集）──

  // 保护未知 PR 集合（MU 域，已按 PR 去重）
  const protectionSet = useMemo(
    () => new Set((extras?.protectionUnknown ?? []).map((x) => `${x.repo}#${x.pr}`)),
    [extras]);

  // PR 实体（按 PR 去重；current head 由 selectCurrentHead 权威化选择，
  // 后端无 is_current 标记 → headConfirmed=false → UI 显示「当前 head 未确认」）。
  // 保护未知但不在 overview 投影内的 PR（如超出 LIMIT 50）合成占位实体——
  // 阶段显示「阶段未获取」（不伪造 PENDING），保证「异常 PR」计数与列表实体恒等。
  const entities = useMemo(() => {
    const base = groupRowsByPr(data?.prs ?? [], { isMu });
    const have = new Set(base.map((e) => e.key));
    const synth = (extras?.protectionUnknown ?? [])
      .filter((x) => !have.has(`${x.repo}#${x.pr}`))
      .map((x) => {
        const [so, sn] = String(x.repo ?? '').split('/');
        const owner = x.owner ?? so ?? null;
        const name = x.name ?? sn ?? null;
        const row = { repo: x.repo, pr: x.pr, head_sha: null, run_id: null,
          stage: null, stage_source: 'not_in_overview_projection',
          updated_at: x.updated_at ?? null, placeholder: true };
        return {
          ...row,
          key: `${x.repo}#${x.pr}`,
          n: x.pr,
          owner,
          name,
          detailTo: (x.pr != null && owner && name)
            ? (isMu
              ? `/mu/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/pr/${x.pr}`
              : `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/pr/${x.pr}`)
            : null,
          current: row,
          headConfirmed: false,
          headBasis: 'protection_only',
          headTotal: 1,
          history: [],
          updated_at: x.updated_at ?? null,
          reason: pendingReasonOf(row),
          bucket: bucketOf(row),
        };
      });
    return [...base, ...synth];
  }, [data, isMu, extras]);

  // 保护未知子态 + 注记：摘要与详情共享同一推导。
  // 红线：审查 run 在途 ≠ 探测在途——无探测证据一律 undetermined；
  // 当前 head 审查在途（overview 实体的阶段）仅作「审查进行中」注记
  // （不宣称检查中/不承诺恢复时点）。mu/prs 投影行无 stage——注记取 overview 实体。
  const puStateByPr = useMemo(() => {
    const m = new Map();
    if (!extras) return m;
    const entityByPr = new Map(entities.map((e) => [e.key, e]));
    for (const it of extras.protectionUnknown ?? []) {
      const key = `${it.repo}#${it.pr}`;
      const e = entityByPr.get(key);
      const stage = String(e?.current?.stage ?? '').toUpperCase();
      m.set(key, {
        stateKey: protectionUnknownKind(e?.current, null), // 无探测证据 → undetermined
        note: REVIEWING_STAGES.includes(stage) ? '审查进行中' : null,
      });
    }
    return m;
  }, [extras, entities]);

  // 异常 = 阶段维异常（head 过期/决策缺失/失败 run）∪ 保护未知 PR 集合
  const enriched = useMemo(() => entities.map((e) => {
    const isProtectionUnknown = protectionSet.has(e.key);
    const pu = puStateByPr.get(e.key) ?? null;
    return {
      ...e,
      isProtectionUnknown,
      anomaly: e.bucket === 'anomaly' || isProtectionUnknown,
      stateKey: pu?.stateKey ?? null,
      puNote: pu?.note ?? null,
    };
  }), [entities, protectionSet, puStateByPr]);

  const byFocus = useMemo(() => ({
    attention: enriched.filter((e) => e.bucket === 'attention'),
    blocked: enriched.filter((e) => e.bucket === 'blocked'),
    reviewing: enriched.filter((e) => e.bucket === 'reviewing'),
    anomaly: enriched.filter((e) => e.anomaly),
    all: enriched,
  }), [enriched]);

  const counts = useMemo(() => Object.fromEntries(
    FOCUS_FILTERS.map((f) => [f.key, byFocus[f.key].length])), [byFocus]);

  // 默认"全部"按工作台优先级排序（首屏优先展示需要人的行）
  const sortedAll = useMemo(() => [...enriched].sort((a, b) =>
    (BUCKET_RANK[a.bucket] - BUCKET_RANK[b.bucket])
    || String(b.updated_at ?? '').localeCompare(String(a.updated_at ?? ''))), [enriched]);
  const filtered = focus === 'all'
    ? sortedAll
    : byFocus[focus] ?? [];
  const filterMeta = FOCUS_FILTERS.find((f) => f.key === focus) ?? FOCUS_FILTERS[0];

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

  const ticketCount = isMu ? (extras?.tickets?.length ?? null) : (data?.pending_summary?.count ?? 0);
  const extrasLoaded = extras != null;
  const protectionCount = byFocus.anomaly.filter((e) => e.isProtectionUnknown).length;
  const placeholderCount = entities.filter((e) => e.placeholder === true).length;
  const stalePrCount = entities.filter((e) => String(e.current?.stage ?? '').toUpperCase() === 'STALE').length;
  // 回执级计数（legacy 口径，无法逐 PR 映射——如实标注单位）
  const incidents = data?.incidents ?? {};
  const incidentAnomalies = deriveAnomalies(data, 0)
    .filter((a) => a.key === 'failed_receipt' || a.key === 'integrity');

  // ── 早返回（全部 hook 之后；data=null 安全退化已就绪）──
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
  if (error) {
    return (
      <div>
        <Typography.Title level={1} style={{ fontSize: 24 }}>审查工作台</Typography.Title>
        <RecoveryBox error={error} mode={config?.mode} subject="工作台数据"
          onRetry={refreshPageData} />
      </div>
    );
  }

  const notWired = data.source === 'BACKEND_NOT_WIRED';
  const isErr = data.source === 'BACKEND_ERROR';

  // ── 图表数据（语义色 + 文本摘要；按 PR 实体口径）──
  const stageData = STAGE_ORDER
    .map((s) => ({ stage: stageMap(s).label, key: s, count: entities.filter((e) => String(e.current?.stage ?? '').toUpperCase() === s).length }))
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
    { title: 'PR', ellipsis: true, render: (_, e) => {
      const label = e.n != null ? `${e.repo} #${e.n}` : `${e.repo}（PR 号缺失）`;
      const headShort = String(e.current?.head_sha ?? '').slice(0, 8);
      return (
        <span className="wb-pr-cell">
          {e.detailTo ? <Link to={e.detailTo}>{label}</Link> : label}
          {headShort ? (
            <code className="mono wb-head-chip"
              title={`当前 head 未确认：按最近事件排序（${e.headBasis}），后端无权威 is_current 标记`}>
              {headShort}
            </code>
          ) : (
            <span className="muted wb-head-chip" title="概览投影未包含该 PR 的 head——阶段未获取">无 head 记录</span>
          )}
        </span>
      );
    } },
    { title: '风险', width: 100, render: (_, e) => {
      const t = ticketByPr.get(`${e.repo}#${e.n}`);
      if (t) return sevBadge(t.severity);
      return <span className="muted" title="无未决高危审批票（P0/P1 产生审批票；P2/P3 裁量入口在 PR 详情）">—</span>;
    } },
    { title: '阶段', width: 120, render: (_, e) => {
      if (e.current?.stage == null) {
        return <Tag title="概览投影未包含该 PR——不猜测阶段">阶段未获取</Tag>;
      }
      const m = stageMap(e.current?.stage);
      return <Tag color={toneToColor(m.tone)} title={m.note}>{m.label}</Tag>;
    } },
    { title: '待处理原因', ellipsis: true, render: (_, e) => (
      <span className="wb-reason" title={e.current?.stage_source || e.reason.detail}>
        {e.reason.text}
        {e.isProtectionUnknown ? <Tag className="wb-reason-tag" color="warning">保护未知</Tag> : null}
        {e.history.length > 0
          ? <span className="muted wb-hist-note">{`（另有 ${e.history.length} 个历史 head）`}</span>
          : null}
      </span>
    ) },
    { title: '更新时间', width: 140, render: (_, e) => (e.updated_at
      ? <span className="mono" title={new Date(e.updated_at).toLocaleString()}>{fmtTime(e.updated_at)}</span>
      : '—') },
    { title: '操作', width: 170, fixed: 'right', render: (_, e) => {
      const t = ticketByPr.get(`${e.repo}#${e.n}`);
      const prLabel = `${e.repo}${e.n != null ? ` #${e.n}` : ''}`;
      return (
        <span className="row-actions">
          <Button size="small" onClick={() => setDrawerEntity(e)}
            aria-label={`查看详情：${prLabel}，head ${String(e.current?.head_sha ?? '').slice(0, 8) || '未知'}`}>查看详情</Button>
          {t ? <Link className="btn btn-sm" to="/approvals"
            aria-label={`处理审批：${prLabel} 有待批准的高危修复票`}>处理审批</Link> : null}
        </span>
      );
    } },
  ];

  const puItems = (extras?.protectionUnknown ?? []).map((it) => {
    const pu = puStateByPr.get(`${it.repo}#${it.pr}`) ?? {};
    return { ...it, stateKey: pu.stateKey ?? 'undetermined', note: pu.note ?? null };
  });
  const repoFailures = extras?.repoFailures ?? [];

  return (
    <div>
      <Typography.Title level={1} style={{ fontSize: 24, marginBottom: 4 }}>审查工作台</Typography.Title>
      <Typography.Paragraph type="secondary">
        需要我处理的 PR、阻断与异常——统计卡即入口，点击直接过滤下方列表（数字=列表行数，按 PR 去重）。
        {lastRefresh ? `刷新于 ${lastRefresh} · 每 ${REFRESH_MS / 1000}s` : ''}
      </Typography.Paragraph>
      <SourceDetail source={data.source} error={data.error} generatedAt={data.generated_at} />

      {notWired ? (
        <Alert type="warning" showIcon message="后端未接线"
          description={<>CONSOLE_PG_DSN 未配置——以下为诚实零值，不是真实数据。配置见 <Link to="/core">系统状态</Link>。</>} />
      ) : isErr ? (
        <Alert type="error" showIcon message="后端错误"
          description={<>连接失败：<span className="mono">{data.error}</span>。
            <Button size="small" style={{ marginInlineStart: 8 }} onClick={refreshPageData}>重试</Button>
            {' '}或检查 <Link to="/datasources">数据源</Link>。</>} />
      ) : null}

      {/* 刷新失败保留旧数据：横幅如实标注数据时点，不静默冒充新鲜 */}
      {refreshError ? (
        <Alert type="warning" showIcon style={{ marginTop: 8 }} message="刷新失败——当前显示上次成功数据"
          description={<>刷新失败（{String(refreshError.message ?? refreshError)}）：以下数据为 {lastRefresh || '上次成功'} 时点的快照，可能过期。
            <Button size="small" style={{ marginInlineStart: 8 }} onClick={refreshPageData}>刷新状态</Button></>} />
      ) : null}
      {/* 投影覆盖范围：触顶时统计为下限（数据可信度加固——不静默漏计） */}
      {data.prs_truncated ? (
        <Alert type="info" showIcon style={{ marginTop: 8 }} message="概览投影达到上限——统计为下限"
          description={<>概览投影最多返回 {data.prs_projection_limit ?? 50} 个 head 行，超出部分未计入本页统计与列表；
            单个 PR 的完整 head/run 历史在其详情页查看。</>} />
      ) : null}

      {/* ── 首屏统计卡：单位标注 + 数字=点击后列表 PR 数（同源联动） ── */}
      <div className="stat-row" role="group" aria-label="工作台统计（按 PR 去重；点击过滤下方列表）">
        <StatCard label="待处理 PR" count={counts.attention} tone={counts.attention ? 'warn' : 'neutral'}
          active={focus === 'attention'} onClick={() => setFocus('attention')}
          title="等待人工审批或裁定的 PR 数（按 PR 去重）——点击在下方列表查看" />
        <StatCard label="待审批票（张）" count={ticketCount ?? 0} tone={ticketCount ? 'warn' : 'neutral'}
          to="/approvals" title="未决的高危修复审批票张数（P0/P1 逐条开票）——前往审批页处理"
          loading={isMu && ticketCount == null} />
        <StatCard label="已阻断 PR" count={counts.blocked} tone={counts.blocked ? 'bad' : 'neutral'}
          active={focus === 'blocked'} onClick={() => setFocus('blocked')}
          title="受控停止待人工裁定的 PR 数——点击在下方列表查看" />
        <StatCard label="异常 PR" count={counts.anomaly} tone={counts.anomaly ? 'warn' : 'neutral'}
          active={focus === 'anomaly'} onClick={() => setFocus('anomaly')}
          title="Head 过期 / 决策缺失 / 失败运行 / 保护状态未知的 PR 数（并集去重）——点击在下方列表查看" />
        <StatCard label="进行中 PR" count={counts.reviewing} tone="info"
          active={focus === 'reviewing'} onClick={() => setFocus('reviewing')}
          title="审查/修复预演/验证进行中的 PR 数——点击在下方列表查看" />
        <StatCard label="全部 PR" count={counts.all} tone="neutral"
          active={focus === 'all'} onClick={() => setFocus('all')}
          title="当前 allowlist 内全部 PR 数（按 PR 去重，每个 PR 显示最新 head）" />
      </div>

      {/* ── PR 工作列表（前置；每 PR 一行当前/latest head，历史收进抽屉） ── */}
      <section ref={listRef} aria-label="PR 工作列表" tabIndex={-1} className="wb-list-section">
        <div className="wb-table-head">
          <div>
            <Typography.Title level={3} style={{ marginBottom: 0 }}>PR 列表（{filterMeta.label}）</Typography.Title>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              每个 PR 一行（当前/latest head，按最近事件排序——当前 head 未确认，后端无权威标记）；
              历史 head 与 run 在「查看详情」抽屉展开。
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
        {/* 联动反馈：数字与统计卡同源，读屏与键盘 focus 均可感知（单文本节点，插值完整） */}
        <p className="wb-focus-status" role="status">
          {`已显示「${filterMeta.label}」${filtered.length} 个${focus === 'all' ? 'PR' : ''}——与「${filterMeta.label}」统计卡数字一致。`}
        </p>
        <Table
          size="small" rowKey={(e) => e.key}
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

      {/* ── 异常摘要（收敛：摘要+数量+一个主动作；原因/影响在 tooltip 与抽屉详情） ── */}
      <section className="anomaly-panel" aria-label="异常状态摘要">
        <div className="anomaly-bar">
          <Typography.Title level={4} style={{ marginBottom: 0 }}>异常状态</Typography.Title>
          {counts.anomaly > 0 ? (
            <Button type="primary" size="small" onClick={() => setFocus('anomaly')}
              aria-label={`查看全部异常 PR（${counts.anomaly} 个）`}>
              查看全部异常 PR（{counts.anomaly}）
            </Button>
          ) : null}
        </div>
        {counts.anomaly === 0 && Number(incidents.failed_receipts ?? 0) === 0 && Number(incidents.integrity_conflicts ?? 0) === 0 ? (
          <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
            当前无异常（保护状态未知 {extrasLoaded ? protectionCount : '获取中'} · Head 过期 {stalePrCount} ·
            失败回执 0 · 完整性冲突 0——诚实零值，不虚构）。
          </Typography.Paragraph>
        ) : (
          <>
            <ul className="anomaly-chips">
              <li className={`anomaly-chip anomaly-tone-warn${protectionCount > 0 ? ' is-nonzero' : ''}`}
                title="GitHub 分支保护状态未能确认为受保护（按当前 head 判定；探测未完成或不可达）——合并资格 fail-closed 恒为未知，不承诺等待后一定恢复。">
                <span className="anomaly-chip-label">保护状态未知</span>
                <span className="anomaly-chip-unit">PR</span>
                <span className="anomaly-chip-count">
                  {extrasLoaded ? protectionCount : '…'}{repoFailures.length > 0 ? '+' : ''}
                </span>
              </li>
              <li className={`anomaly-chip anomaly-tone-neutral${stalePrCount > 0 ? ' is-nonzero' : ''}`}
                title="同一 PR 推进了更新的 head，既有审查运行仍绑定旧 head——旧结论不再代表当前代码。">
                <span className="anomaly-chip-label">Head 过期</span>
                <span className="anomaly-chip-unit">PR</span>
                <span className="anomaly-chip-count">{stalePrCount}</span>
              </li>
              {incidentAnomalies.map((a) => (
                <li key={a.key} className={`anomaly-chip anomaly-tone-${a.tone}${a.count > 0 ? ' is-nonzero' : ''}`}
                  title={`${a.cause}（${a.impact}）`}>
                  <span className="anomaly-chip-label">{a.short}</span>
                  <span className="anomaly-chip-unit">回执</span>
                  <span className="anomaly-chip-count">{a.count}</span>
                </li>
              ))}
            </ul>
            {protectionCount > 0 ? (
              <ProtectionUnknownSummary items={puItems} limit={5}
                onOpenPr={(it) => {
                  const e = enriched.find((x) => x.key === `${it.repo}#${it.pr}`);
                  if (e?.detailTo) navigate(e.detailTo);
                  else setFocus('anomaly');
                }} />
            ) : null}
          </>
        )}
        {extras?.protectionErr || repoFailures.length > 0 ? (
          <Alert type="warning" showIcon style={{ marginTop: 8 }}
            message={repoFailures.length > 0 ? '保护状态为部分数据' : '保护状态汇总不可得'}
            description={repoFailures.length > 0 ? (
              <>{repoFailures.length} 个仓库的 PR 保护状态读取失败（{repoFailures.map((f) => f.repo).join('、')}）——
                这些仓库的保护未知项未计入「保护状态未知」数量（当前计数为下限，不猜测）。
                <Button size="small" style={{ marginInlineStart: 8 }} onClick={refreshPageData}>刷新状态</Button></>
            ) : (
              <>PR 保护状态读取失败（{String(extras.protectionErr.message ?? extras.protectionErr)}）——不猜测数量。
                <Button size="small" style={{ marginInlineStart: 8 }} onClick={refreshPageData}>刷新状态</Button></>
            )} />
        ) : null}
      </section>

      {/* ── 详情抽屉：当前 run + 历史 head/run 展开 + 保护状态 ── */}
      <Drawer open={drawerEntity != null} onClose={() => setDrawerEntity(null)} width={480}
        title={drawerEntity ? `${drawerEntity.repo}${drawerEntity.n != null ? ` #${drawerEntity.n}` : ''} · 运行详情` : '运行详情'}
        destroyOnClose>
        {drawerEntity ? (
          <div className="wb-drawer">
            {drawerEntity.detailTo ? (
              <Typography.Paragraph>
                <Link className="btn" to={drawerEntity.detailTo}>打开 PR 详情页（审查管线/风险项/操作）</Link>
              </Typography.Paragraph>
            ) : null}
            <dl className="kv-grid">
              <div className="kv"><div className="kv-label">Head（当前）</div>
                <div className="kv-value">
                  <span className="sha mono" title={drawerEntity.current?.head_sha ?? ''}>{drawerEntity.current?.head_sha?.slice(0, 12) || '—'}</span>
                  {!drawerEntity.headConfirmed ? (
                    <Tag className="wb-reason-tag" color="default"
                      title="后端无权威 is_current 标记——当前 head 按最近事件排序推导（head_basis=event_order），未与 GitHub 实时对照。">当前 head 未确认</Tag>
                  ) : null}
                  <div className="muted" style={{ fontSize: 12 }}>同一 head 的更多 run 在 PR 详情页「审查管线」中。</div>
                </div></div>
              <div className="kv"><div className="kv-label">Run（当前）</div>
                <div className="kv-value"><span className="mono">{drawerEntity.current?.run_id || '—'}</span></div></div>
              <div className="kv"><div className="kv-label">阶段</div>
                <div className="kv-value">
                  {drawerEntity.current?.stage == null
                    ? <Tag title="概览投影未包含该 PR——不猜测阶段">阶段未获取</Tag>
                    : (() => { const m = stageMap(drawerEntity.current?.stage);
                      return <Tag color={toneToColor(m.tone)}>{m.label}</Tag>; })()}
                </div></div>
              <div className="kv"><div className="kv-label">阶段来源</div>
                <div className="kv-value"><span className="mono" style={{ fontSize: 12 }}>{drawerEntity.current?.stage_source || '—'}</span></div></div>
              <div className="kv"><div className="kv-label">待处理原因</div>
                <div className="kv-value">{drawerEntity.reason.text}
                  {drawerEntity.isProtectionUnknown ? <Tag className="wb-reason-tag" color="warning">保护状态未知</Tag> : null}
                  <div className="muted" style={{ fontSize: 12 }}>{drawerEntity.reason.detail}</div></div></div>
              <div className="kv"><div className="kv-label">更新时间</div>
                <div className="kv-value">{drawerEntity.updated_at ? new Date(drawerEntity.updated_at).toLocaleString() : '—'}</div></div>
            </dl>

            {drawerEntity.history.length > 0 ? (
              <details className="wb-history">
                <summary>历史 head / run（{drawerEntity.history.length}）</summary>
                <ul className="wb-history-list">
                  {drawerEntity.history.map((h, i) => {
                    const m = stageMap(h.stage);
                    return (
                      <li key={`${h.run_id ?? 'norun'}-${i}`} className="wb-history-item">
                        <Tag className="wb-hist-tag">历史</Tag>
                        <code className="mono" title={h.head_sha ?? ''}>{String(h.head_sha ?? '').slice(0, 8) || '—'}</code>
                        <span className="mono wb-hist-run" title={h.run_id ?? ''}>{String(h.run_id ?? '').slice(0, 8) || '—'}</span>
                        <Tag color={toneToColor(m.tone)} title={m.note}>{m.label}</Tag>
                        <span className="muted" style={{ fontSize: 12 }}>{h.updated_at ? fmtTime(h.updated_at) : '—'}</span>
                      </li>
                    );
                  })}
                </ul>
              </details>
            ) : null}

            {drawerEntity.isProtectionUnknown ? (
              <div style={{ marginTop: 12 }}>
                <Typography.Title level={5}>保护状态未知</Typography.Title>
                <ProtectionUnknownCard
                  stateKey={drawerEntity.stateKey ?? 'undetermined'}
                  note={drawerEntity.puNote}
                  repo={drawerEntity.owner && drawerEntity.name ? { owner: drawerEntity.owner, name: drawerEntity.name } : null}
                  onRefresh={refreshPageData}
                  onOpenDetail={drawerEntity.detailTo
                    ? () => { setDrawerEntity(null); navigate(drawerEntity.detailTo); }
                    : undefined} />
              </div>
            ) : null}
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
                      const f = key === 'ACTION_REQUIRED' ? 'attention'
                        : key === 'BLOCKED' ? 'blocked'
                          : REVIEWING_STAGES.includes(String(key)) ? 'reviewing'
                            : ['STALE', 'UNKNOWN'].includes(String(key)) ? 'anomaly' : 'all';
                      setFocus(f);
                    }
                  }}
                />
                <p className="ov-chart-summary">
                  {`共 ${entities.length} 个 PR（按最新 head 阶段计）：待处理 ${counts.attention} · 已阻断 ${counts.blocked} · 异常 ${counts.anomaly} · 进行中 ${counts.reviewing}${placeholderCount > 0 ? `；另有 ${placeholderCount} 个阶段未获取（保护未知占位，未计入阶段分布）` : ''}。点击柱体可在上方列表过滤对应分类。`}
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
                    ? `，峰值 ${trendPeak.date}（${trendPeak.runs} 次）` : ''}（单位：run）。
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
                  {repoCounts.length} 个仓库 · 共 {repoCounts.reduce((n, r) => n + (Number(r.runs) || 0), 0)} 项运行记录（单位：run）
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
