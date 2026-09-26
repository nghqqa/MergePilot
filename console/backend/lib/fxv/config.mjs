// fxv/config.mjs — FXV（Finding→Ticket→Fixer→Verifier）生产配置。
// 原则：fail-closed——配置缺失/非法时拒绝运行，绝不静默放宽。
// 2026-09-26 核心能力闭环轮引入。

export const FXV_CONFIG_ERRORS = Object.freeze({
  ALLOWLIST_REQUIRED: 'FXV_REPO_ALLOWLIST_REQUIRED',
  BAD_DRY_RUN: 'FXV_DRY_RUN_MUST_BE_BOOLEAN_LIKE',
  BAD_GITHUB_WRITE: 'FXV_GITHUB_WRITE_MUST_BE_disabled_OR_authorized',
  BAD_TIMEOUT: 'FXV_TIMEOUTS_MUST_BE_POSITIVE_MS',
  BAD_RETRIES: 'FXV_MAX_RETRIES_MUST_BE_NON_NEGATIVE',
});

export function loadFxvConfig(env = process.env) {
  const repoAllowlist = String(env.FXV_REPO_ALLOWLIST || '')
    .split(',').map((s) => s.trim()).filter(Boolean);
  if (repoAllowlist.length === 0) {
    return { ok: false, reason: FXV_CONFIG_ERRORS.ALLOWLIST_REQUIRED,
      detail: '仓库白名单为空——FXV 拒绝运行（fail-closed：无白名单=禁止一切修复动作）' };
  }
  const branchAllowlist = String(env.FXV_BRANCH_ALLOWLIST || '*')
    .split(',').map((s) => s.trim()).filter(Boolean);

  const dryRunRaw = env.FXV_DRY_RUN;
  if (dryRunRaw !== undefined && !['0', '1', 'false', 'true'].includes(String(dryRunRaw))) {
    return { ok: false, reason: FXV_CONFIG_ERRORS.BAD_DRY_RUN };
  }
  const dryRun = dryRunRaw === undefined || !['0', 'false'].includes(String(dryRunRaw)); // 默认开启

  const githubWriteRaw = String(env.FXV_GITHUB_WRITE || 'disabled');
  if (!['disabled', 'authorized'].includes(githubWriteRaw)) {
    return { ok: false, reason: FXV_CONFIG_ERRORS.BAD_GITHUB_WRITE };
  }

  const num = (k, d) => {
    const v = Number(env[k]);
    return Number.isFinite(v) && v > 0 ? v : d;
  };
  const timeouts = {
    step_ms: num('FXV_STEP_TIMEOUT_MS', 60_000),
    approval_ttl_ms: num('FXV_APPROVAL_TTL_MS', 24 * 3600_000),
    grant_ttl_ms: num('FXV_GRANT_TTL_MS', 3600_000),
  };
  const maxRetries = Math.max(0, Math.floor(Number(env.FXV_MAX_RETRIES ?? 2) || 0));

  return { ok: true, config: {
    repoAllowlist, branchAllowlist, dryRun, githubWrite: githubWriteRaw,
    timeouts, maxRetries, raw: { ...env },
  } };
}

export function repoAllowed(cfg, repo) {
  return cfg.repoAllowlist.includes(repo);
}
export function branchAllowed(cfg, branch) {
  return cfg.branchAllowlist.includes('*') || cfg.branchAllowlist.includes(branch);
}
export function assertTargetAllowed(cfg, repo, branch) {
  if (!repoAllowed(cfg, repo)) {
    return { ok: false, reason: 'REPO_NOT_IN_FXV_ALLOWLIST', repo, allowlist: cfg.repoAllowlist };
  }
  if (!branchAllowed(cfg, branch)) {
    return { ok: false, reason: 'BRANCH_NOT_IN_FXV_ALLOWLIST', branch, allowlist: cfg.branchAllowlist };
  }
  return { ok: true };
}
