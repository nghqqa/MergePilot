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
  return snapshotSource(fetchImpl);
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
