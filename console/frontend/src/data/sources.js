// data/sources.js — 数据源注入机制（页面只消费 createDataSource 产出的统一接口）。
//
// 三种来源严格分离，页面不做来源判断以外的环境分支：
//   snapshot —— console 后端 /api/runs（锁定证据包，客户端聚合；现状默认）
//   contract —— 正式契约 v2（/api/pulls 等；由可信配置声明 available 后启用）
//   harness  —— 同 contract，但由开发/测试 fixture harness 供数（data_mode='fixture'，
//               UI 全程可见 fixture 标识）。harness 不是业务后端：无存储/授权/审批状态机。
//
// 红线：
// - 模式只由 loadRuntimeConfig 的可信服务声明决定；sessionStorage/URL 无权授予 contract/live；
// - 契约来源失败 → 显示错误，绝不回退私有 snapshot 数据；
// - 两个来源即使路径同名（/api/runs）也互不调用对方的响应结构。

import { pullsUrl, pullUrl } from '../api-live.js';
import { groupRunsByPr } from '../pr-model.js';
import { prViewFromContract, prViewFromSnapshot } from './pr-view.js';

// ---- snapshot 源（现状默认） ----

function snapshotSource(fetchImpl) {
  const getRuns = async () => {
    const res = await fetchImpl('/api/runs?limit=200', { credentials: 'same-origin' });
    if (!res.ok) {
      const body = await res.json().catch(() => null);
      const err = new Error(body?.error?.message ?? `HTTP ${res.status}`);
      err.status = res.status;
      throw err;
    }
    return res.json();
  };
  return {
    kind: 'snapshot',
    dataMode: 'snapshot',
    async listRepos() {
      const data = await getRuns();
      const prs = groupRunsByPr(data?.items ?? []);
      const map = new Map();
      for (const pr of prs) {
        if (!map.has(pr.repo)) map.set(pr.repo, { repo: pr.repo, owner: pr.owner, name: pr.name, prCount: 0, runCount: 0, activityAt: null, kind: 'snapshot' });
        const e = map.get(pr.repo);
        e.prCount += 1;
        e.runCount += pr.runs.length;
        if (pr.activityAt && (!e.activityAt || pr.activityAt > e.activityAt)) e.activityAt = pr.activityAt;
      }
      return [...map.values()].sort((a, b) => a.repo.localeCompare(b.repo));
    },
    async listPrs(repo) {
      const data = await getRuns();
      return groupRunsByPr(data?.items ?? [])
        .filter((p) => p.repo === repo)
        .map(prViewFromSnapshot);
    },
    async getPr(repo, prNumber) {
      const views = await this.listPrs(repo);
      const view = views.find((p) => p.prNumber === prNumber) ?? null;
      return view ? { view, detail: null } : null; // snapshot 详情复用列表聚合（run 详情另有端点）
    },
  };
}

// ---- contract 源（契约 v2；harness 与未来正式后端共用此实现） ----

function contractSource(fetchImpl, config) {
  const get = async (url) => {
    const res = await fetchImpl(url, { credentials: 'same-origin' });
    const body = await res.json().catch(() => null);
    if (!res.ok) {
      const err = new Error(body?.error?.message ?? `HTTP ${res.status}`);
      err.status = res.status;
      err.reason = body?.error?.reason ?? null;
      throw err;
    }
    return body;
  };
  return {
    kind: 'contract',
    dataMode: config.dataMode, // 'fixture'（harness）或未来正式 'live'
    async listRepos() {
      // 契约 v2 未定义仓库总览端点（installation 映射属后续）——仓库清单由可信配置声明
      return (config.declaredRepos ?? []).map((repo) => ({
        repo, owner: repo.split('/')[0], name: repo.split('/').slice(1).join('/'),
        prCount: null, runCount: null, activityAt: null, kind: 'contract',
      }));
    },
    async listPrs(repo) {
      const body = await get(pullsUrl(repo, { state: 'open', limit: 50 }));
      return (body?.items ?? []).map(prViewFromContract);
    },
    async getPr(repo, prNumber) {
      const detail = await get(pullUrl(repo, prNumber));
      return { view: prViewFromContract({ ...detail, has_pending_tickets: detail.has_pending_tickets ?? null }), detail };
    },
  };
}

export function createDataSource(config, fetchImpl = (typeof fetch !== 'undefined' ? fetch : null)) {
  if (config?.mode === 'contract') return contractSource(fetchImpl, config);
  if (config?.mode === 'console-pg') return consolePgSource(fetchImpl, config);
  return snapshotSource(fetchImpl);
}

// ---- console_pg 源（DEV/隔离联调适配 —— 明确标记，非正式契约 /api/pulls） ----
//
// 后端：tools/console_pg/server.py v0.2.0（只读；data_mode 恒 fixture；认证未实现 → 401）。
// 与正式契约的差异（已记录 INTEGRATION-REQUESTS C-10 备注，不默默双轨）：
//   - 端点 /api/prs（非 /api/pulls），列表项无 title/state/current_head_sha/verdict/
//     has_pending_tickets —— PG 读模型暂无结论与待办字段；
//   - head_sha 为该 PR 各 run 的 MIN(head)，不是 GitHub 当前 head 权威 → 本源
//     currentHead 恒 null，页面维持"最近记录"口径，绝不显示当前结论；
//   - 已知后端缺陷（已在交接记录，适配层规避而非掩盖）：/api/runs?repo&pr 组合
//     KeyError → 连接重置；except StorageUnavailable 未定义 → NameError。
// 本源仅用于隔离 PG fixture 联调；正式后端交付契约端点后由配置切回 contract。
function consolePgSource(fetchImpl, config) {
  const base = (config.pgBase ?? '/pg').replace(/\/$/, '');
  const get = async (url) => {
    const res = await fetchImpl(url, { credentials: 'same-origin' });
    const body = await res.json().catch(() => null);
    if (!res.ok) {
      const err = new Error(body?.error?.message ?? `HTTP ${res.status}`);
      err.status = res.status;
      err.reason = body?.error?.reason ?? null;
      throw err;
    }
    return body;
  };
  const pgTs = (v) => (v ? String(v).replace(' ', 'T') : null);

  const runRow = (r) => ({
    run_id: r.run_id,
    head_sha: r.head_sha ?? null,
    created_at: pgTs(r.created_at) ?? pgTs(r.updated_at),
    execution: { status: String(r.status ?? '').toUpperCase() || null },
    review: {}, // 读模型无独立结论字段——绝不伪造 verdict
    mode: r.mode ?? null,
    outcome: r.outcome ?? null,
    runClass: r.run_class ?? null,
    superseded: Boolean(r.superseded_by_run_id) || String(r.status).toUpperCase() === 'SUPERSEDED',
  });

  const prView = (repo, item, runItems) => ({
    kind: 'console-pg',
    key: `${repo}#${item.pr_number}`,
    repo,
    owner: repo.split('/')[0],
    name: repo.split('/').slice(1).join('/'),
    prNumber: item.pr_number,
    title: null,
    prUrl: `https://github.com/${repo}/pull/${item.pr_number}`,
    activityAt: pgTs(item.latest_activity),
    attention: { flag: 'no-data', label: 'PG 读模型未提供结论/待办字段' },
    review: { basis: 'not-in-read-model', review: {}, stale: null },
    currentHead: null, // 后端无 GitHub 当前 head 权威（如实）
    heads: [],
    runs: runItems,
    latest: runItems[0] ?? null,
    latestCompleted: runItems.find((r) => ['SUCCEEDED', 'FAILED'].includes(String(r.execution.status))) ?? null,
    runCountExact: true,
    runCount: item.run_count ?? runItems.length,
  });

  return {
    kind: 'console-pg',
    dataMode: 'fixture',
    async listRepos() {
      const body = await get(`${base}/api/repos`);
      return (body?.items ?? []).map((r) => ({
        repo: r.repo_id,
        owner: String(r.repo_id ?? '').split('/')[0],
        name: String(r.repo_id ?? '').split('/').slice(1).join('/'),
        prCount: r.pr_count ?? null,
        runCount: r.run_count ?? null,
        activityAt: pgTs(r.latest_activity),
        kind: 'console-pg',
      }));
    },
    async listPrs(repo) {
      const body = await get(`${base}/api/prs?repo=${encodeURIComponent(repo)}`);
      const items = [];
      for (const it of body?.items ?? []) {
        // 规避后端 /api/runs?repo&pr 缺陷：repo 级查询 + 客户端按 pr_number 过滤
        const runsBody = await get(`${base}/api/runs?repo=${encodeURIComponent(repo)}`);
        const runItems = (runsBody?.items ?? [])
          .filter((r) => r.pr_number === it.pr_number)
          .map(runRow)
          .sort((a, b) => (a.created_at ?? '').localeCompare(b.created_at ?? ''));
        items.push(prView(repo, it, runItems.reverse()));
      }
      return items;
    },
    async getPr(repo, prNumber) {
      const views = await this.listPrs(repo);
      const found = views.find((v) => v.prNumber === prNumber) ?? null;
      return found ? { view: found, detail: { runs: found.runs } } : null;
    },
    async getRunDetail(runId) {
      return get(`${base}/api/runs/${encodeURIComponent(runId)}`);
    },
  };
}

// ---- 请求竞态守卫：切换仓库后晚到的旧响应不得覆盖当前页面 ----
export function createRaceGuard() {
  let alive = true;
  let gen = 0;
  return {
    next: () => ++gen,
    isCurrent: (token) => alive && token === gen,
    cancel: () => { alive = false; },
  };
}
