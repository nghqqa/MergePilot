// data/config.js — 运行时数据源配置（可信服务配置）。
//
// 规则：数据源模式只由"为页面提供服务的后端"经 /api/health 声明；
// sessionStorage、URL 参数、localStorage 一律无权改变模式或授予 live/contract 权限。
// 声明缺失/解析失败 → 保守回退 snapshot（不回退到任何私有 live 数据）。

let cached = null;

export function normalizeConfig(healthBody) {
  const sources = healthBody?.sources ?? null;
  const primary = sources?.primary;
  const contract = sources?.contract_v2 ?? {};
  // 仅当服务明确声明 contract 可用时才启用契约数据源；其余一律 snapshot。
  const mode = primary === 'contract' && contract.available === true ? 'contract' : 'snapshot';
  return {
    mode,
    dataMode: healthBody?.data_mode ?? (mode === 'contract' ? 'fixture' : 'snapshot'),
    contractAvailable: contract.available === true,
    contractReason: contract.reason ?? null,
    // 契约模式下的仓库列表由可信配置声明（未来来自 installation 映射），不由前端猜测
    declaredRepos: Array.isArray(healthBody?.declared_repos) ? healthBody.declared_repos : [],
    service: healthBody?.service ?? null,
    raw: healthBody ?? null,
  };
}

export async function loadRuntimeConfig(fetchImpl = (typeof fetch !== 'undefined' ? fetch : null), force = false) {
  if (cached && !force) return cached;
  if (!fetchImpl) {
    cached = normalizeConfig(null);
    return cached;
  }
  let body = null;
  try {
    const res = await fetchImpl('/api/health', { credentials: 'same-origin' });
    body = await res.json();
  } catch {
    body = null; // 配置不可得 → 保守 snapshot
  }
  cached = normalizeConfig(body);
  return cached;
}

// 测试与模式切换辅助：清除模块级缓存（配置本身非私有数据，会话过期无需清除）
export function clearRuntimeConfigCache() {
  cached = null;
}
