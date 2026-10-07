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
  const consolePg = sources?.console_pg ?? {};
  const multiuser = sources?.multiuser ?? {};
  // 仅当服务明确声明对应源可用时才启用；其余一律 snapshot。
  // 'console-pg' 为 DEV/联调适配源（后端 tools/console_pg 只读服务；形状差异见
  // INTEGRATION-REQUESTS C-10 备注），生产 console 后端不会声明该值。
  // R4（OVERVIEW_REMEDIATION 二）：服务端 primary='contract_v2'（live DSN 已配置时）
  // → 全站页面统一走服务端 allowlist 过滤的实时契约端点，不再回退 snapshot 默认。
  // Wave 3.7 数据源纯化：MU 部署（primary='multiuser'）→ 全站页面走 /api/mu/* canonical
  // 源（会话内 tenant 收窄）；仓库清单经认证后获取，health 不携带租户仓库名。
  let mode = 'snapshot';
  if (primary === 'multiuser' && multiuser.available === true) mode = 'multiuser';
  if (primary === 'contract' && contract.available === true) mode = 'contract';
  if (primary === 'contract_v2' && contract.available === true) mode = 'contract';
  if (primary === 'console-pg' && consolePg.available === true) mode = 'console-pg';
  return {
    mode,
    // primary 是否被服务显式声明——演示放行依据之一：模式未声明（未知）时
    // 残留演示标记不放行（不能假定未知=可演示的 legacy 环境）。
    modeDeclared: sources?.primary != null,
    dataMode: healthBody?.data_mode ?? (mode === 'snapshot' ? 'snapshot' : 'fixture'),
    contractAvailable: contract.available === true,
    contractReason: contract.reason ?? null,
    consolePgAvailable: consolePg.available === true,
    pgBase: consolePg.base ?? '/pg',
    muAvailable: multiuser.available === true,
    // 契约模式下的仓库列表由可信配置声明（服务端 allowlist 权威下发），不由前端猜测；
    // multiuser 模式下 health 按租户隔离原则不下发仓库清单（恒空），清单走 /api/mu/repositories。
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
