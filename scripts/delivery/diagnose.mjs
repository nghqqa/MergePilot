#!/usr/bin/env node
// scripts/delivery/diagnose.mjs — MergePilot 交付诊断（只读，零第三方依赖，node >= 20）。
//
// 用途：新用户按 docs/delivery/DEPLOY-GUIDE.md 部署后，一键核查各能力面的实际就绪
// 状态，并对每个失败项给出具体处理步骤（指向 docs/delivery/TROUBLESHOOTING.md 条目）。
//
// 检查面（对应能力矩阵 docs/delivery/CAPABILITY-MATRIX.md）：
//   运行环境 / .env 必填项 / GitHub App / OAuth / LLM 模型 / 修复测试命令
//   (MU_FIX_TEST_CMDS) / 执行器 / Gitee / job consumer / cchain 三键 /
//   HTTP health 与 schema / 登录配置端点 / 数据库 schema 版本。
//
// 铁律（docs/delivery/TROUBLESHOOTING.md 同款纪律）：
//   * 只读——不改任何配置、不写远端、不调用任何需要凭据的接口；
//   * 凭据零输出——secret 类键只显示"已设置 + 长度/掩码"，绝不回显值；
//   * 人可读 + --json 双输出；退出码 0=无失败项（warn 不影响）/ 1=有失败项 / 2=用法错误。
//
// 用法：
//   node scripts/delivery/diagnose.mjs [--env <.env 路径>] [--base-url <console 地址>]
//        [--pg-container <postgres 容器名>] [--json] [--no-net]
//   --no-net   跳过全部 HTTP 探测（仅静态配置检查）
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

export const DIAGNOSE_VERSION = '1.0.0';
export const EXPECTED_SCHEMA_VERSION = 29; // 与 console/backend/lib/multiuser/schema.mjs MU_SCHEMA_LATEST 对齐（升级时同步改；v29=fix_apply_operation 应用/合并操作记录）

// ── 键分类：secret 类键任何情况下不回显值 ──
const SECRET_KEY_RE = /PASSWORD|SECRET|API_KEY|TOKEN|PAT\b|PRIVATE_KEY|_PASSWORD$/i;

/** 解析 .env 文本为键值表（与 deploy/selfhost/preflight.mjs 同解析口径）。 */
export function parseEnvFile(text) {
  const env = {};
  for (const line of String(text ?? '').split(/\r?\n/)) {
    const m = line.match(/^([A-Z][A-Z0-9_]*)=(.*)$/);
    if (m) env[m[1]] = m[2].trim().replace(/^"(.*)"$/, '$1');
  }
  return env;
}

/** 脱敏显示：secret → 「已设置(N 字符)」；非 secret 原样；空 → (未设置)。 */
export function displayValue(key, value) {
  if (value === undefined || value === null || String(value).trim() === '') return '(未设置)';
  if (SECRET_KEY_RE.test(key)) {
    const s = String(value);
    return `已设置(${s.length} 字符)`;
  }
  return String(value);
}

/** host 提取（URL 只展示 host，不展示 path/query——path 可能带敏感参数形态）。 */
export function hostOf(url) {
  try { return new URL(String(url)).host; } catch { return null; }
}

/** callback 地址适用范围判定（与 deploy/selfhost/README.md「callback 未配置」节同语义）。 */
export function callbackScope(url) {
  const u = String(url ?? '').trim();
  if (!u) return { level: 'fail', note: '未设置' };
  let parsed;
  try { parsed = new URL(u); } catch { return { level: 'fail', note: '不是合法 URL' }; }
  if (parsed.protocol === 'https:') return { level: 'pass', note: 'HTTPS 非回环——可共享' };
  const h = parsed.hostname;
  if (h === '127.0.0.1' || h === 'localhost' || h === '::1') {
    return { level: 'warn', note: '回环地址——仅本机可用（跨机器登录/公网 webhook 不可用）' };
  }
  if (parsed.protocol === 'http:') {
    return { level: 'warn', note: 'HTTP 非回环——不符合推荐 HTTPS 配置（内网适用）' };
  }
  return { level: 'pass', note: '格式可解析' };
}

/**
 * 构造全部检查项。deps 全部可注入（单测用）：
 *   env          解析后的环境变量对象（.env 与进程 env 合并，进程 env 优先）
 *   fetchImpl    HTTP 探测函数（默认全局 fetch；--no-net 时传 null）
 *   dockerExec   (container, args[]) => {status, stdout, stderr}（psql 探测；null=跳过）
 *   pgContainer  postgres 容器名
 *   nodeVersion  进程版本（默认 process.versions.node）
 * 返回检查项数组：{ id, title, status: pass|fail|warn|skip, detail, fix?, ref? }
 *   fix  = 失败时的处理步骤摘要；ref = TROUBLESHOOTING.md 条目锚点。
 */
export async function runChecks(deps = {}) {
  const env = deps.env ?? {};
  const fetchImpl = deps.fetchImpl === undefined ? globalThis.fetch : deps.fetchImpl;
  const dockerExec = deps.dockerExec ?? null;
  const pgContainer = deps.pgContainer ?? 'mergepilot-postgres-1';
  const baseUrl = String(deps.baseUrl ?? '').replace(/\/+$/, '');
  const checks = [];
  const add = (c) => checks.push(c);

  // ── 1. 运行环境 ──
  const nv = deps.nodeVersion ?? process.versions.node;
  const nvOk = Number(nv.split('.')[0]) >= 20;
  add({ id: 'node_version', title: 'Node 版本 >= 20',
    status: nvOk ? 'pass' : 'fail', detail: `当前 ${nv}`,
    fix: nvOk ? undefined : '安装 Node.js 20 或更高版本后重跑', ref: 'T-ENV' });

  // ── 2. .env 文件 ──
  const envFile = deps.envFile ?? null;
  add({ id: 'env_file', title: '.env 文件存在且可读',
    status: envFile ? 'pass' : 'skip',
    detail: envFile ? envFile : '未提供 --env（仅进程环境变量参与检查）' });

  // ── 3. 必填：数据库与会话 ──
  checkKeys(add, env, 'env_core', '数据库与会话密钥', [
    ['POSTGRES_PASSWORD', '数据库密码（compose 拼接 DSN）'],
    ['CONSOLE_SESSION_SECRET', '会话签名密钥'],
  ], 'T-ENV');

  // ── 4. GitHub App（webhook + 安装 + 机器人评论发布）──
  checkKeys(add, env, 'env_github_app', 'GitHub App 五项', [
    ['MU_GITHUB_APP_ID', 'App ID'],
    ['MU_GITHUB_APP_SLUG', 'App slug'],
    ['MU_GITHUB_APP_PRIVATE_KEY', 'App 私钥 PEM'],
    ['MU_GITHUB_WEBHOOK_SECRET', 'webhook 签名密钥'],
    ['MU_GITHUB_APP_INSTALL_CALLBACK_URL', '安装回调地址'],
  ], 'T-WEBHOOK');

  // ── 5. GitHub OAuth（用户登录）──
  const hasOauth = ['MU_GITHUB_OAUTH_CLIENT_ID', 'MU_GITHUB_OAUTH_CLIENT_SECRET', 'MU_GITHUB_OAUTH_CALLBACK_URL']
    .every((k) => String(env[k] ?? '').trim() !== '');
  add({ id: 'env_oauth', title: 'GitHub OAuth 登录三项',
    status: hasOauth ? 'pass' : 'fail',
    detail: hasOauth ? '三项齐全' : '缺失：' + ['MU_GITHUB_OAUTH_CLIENT_ID', 'MU_GITHUB_OAUTH_CLIENT_SECRET', 'MU_GITHUB_OAUTH_CALLBACK_URL']
      .filter((k) => String(env[k] ?? '').trim() === '').join(', '),
    fix: hasOauth ? undefined : '创建 GitHub OAuth App（与 GitHub App 是两个应用）并填入三项；回调地址=<console 公开地址>/api/mu/auth/oauth/github/callback', ref: 'T-OAUTH' });
  add({ id: 'oauth_callback_scope', title: 'OAuth 回调地址适用范围',
    status: callbackScope(env.MU_GITHUB_OAUTH_CALLBACK_URL).level,
    detail: `${displayValue('MU_GITHUB_OAUTH_CALLBACK_URL', env.MU_GITHUB_OAUTH_CALLBACK_URL)} —— ${callbackScope(env.MU_GITHUB_OAUTH_CALLBACK_URL).note}`,
    fix: callbackScope(env.MU_GITHUB_OAUTH_CALLBACK_URL).level === 'fail'
      ? '设置 MU_GITHUB_OAUTH_CALLBACK_URL 为绝对地址（https://<公开地址>/api/mu/auth/oauth/github/callback）' : undefined,
    ref: 'T-OAUTH' });

  // ── 6. LLM 模型配置（第二阶段模型审查）──
  checkKeys(add, env, 'env_llm', 'LLM 出站四项', [
    ['MU_LLM_PROVIDER', 'provider（须 openai_compatible）'],
    ['MU_LLM_BASE_URL', 'openai 兼容端点'],
    ['MU_LLM_MODEL', '模型名'],
    ['MU_LLM_API_KEY', '模型 API key'],
  ], 'T-MODEL');
  if (String(env.MU_LLM_BASE_URL ?? '').trim()) {
    add({ id: 'llm_base_url_host', title: 'LLM 端点（仅显示 host）',
      status: hostOf(env.MU_LLM_BASE_URL) ? 'pass' : 'warn',
      detail: hostOf(env.MU_LLM_BASE_URL) ?? '无法解析 host' });
  }
  add({ id: 'llm_allowed_models', title: '模型白名单 MU_LLM_ALLOWED_MODELS',
    status: String(env.MU_LLM_ALLOWED_MODELS ?? '').trim() ? 'pass' : 'warn',
    detail: String(env.MU_LLM_ALLOWED_MODELS ?? '').trim()
      ? String(env.MU_LLM_ALLOWED_MODELS).trim() : '未设置——默认白名单 deepseek-flash/deepseek-chat；策略里的模型必须 ∈ 白名单，否则 LLM 阶段 fail-closed 关闭（LLM_POLICY_MODEL_NOT_ALLOWED）',
    fix: String(env.MU_LLM_ALLOWED_MODELS ?? '').trim() ? undefined
      : '如需用非默认模型：设置 MU_LLM_ALLOWED_MODELS=<逗号分隔模型列表>，并保证控制台「运行策略」里的模型在其中', ref: 'T-MODEL' });

  // ── 7. 实际修复测试命令（验证第三分）──
  const rawCmds = String(env.MU_FIX_TEST_CMDS ?? '').trim();
  if (!rawCmds) {
    add({ id: 'fix_test_cmds', title: '修复测试命令 MU_FIX_TEST_CMDS',
      status: 'warn', detail: '未设置——补丁的「实际修复测试」将如实登记为未执行（其余验证不受影响）',
      fix: '按仓库配置：MU_FIX_TEST_CMDS={"<provider>:<owner>/<repo>":"<测试命令>"}（JSON 映射，命令来自部署者预批准，绝不取自模型输出）', ref: 'T-FIXTEST' });
  } else {
    let parsed = null;
    try { parsed = JSON.parse(rawCmds); } catch { /* 坏 JSON */ }
    const entries = parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? Object.entries(parsed).filter(([, v]) => typeof v === 'string' && v.trim()) : null;
    add({ id: 'fix_test_cmds', title: '修复测试命令 MU_FIX_TEST_CMDS',
      status: entries && entries.length ? 'pass' : 'fail',
      detail: entries && entries.length
        ? `已配置 ${entries.length} 个仓库键：${entries.map(([k]) => k).join(', ')}（命令内容不显示——可含路径/参数）`
        : '已设置但不是合法 JSON 映射（key=字符串、value=非空命令字符串）——解析失败等同未配置',
      fix: entries && entries.length ? undefined
        : '改为合法 JSON，例如 {"gitee:owner/repo":"npm test"}；坏 JSON 会被静默视为未配置', ref: 'T-FIXTEST' });
  }
  if (String(env.MU_FIX_TEST_ALLOW_DEPS_CMD ?? '').trim() === '1') {
    add({ id: 'fix_test_deps_backdoor', title: 'MU_FIX_TEST_ALLOW_DEPS_CMD=1（deps 命令后门）',
      status: 'warn', detail: '该开关仅供隔离 E2E 显式开启；生产部署应保持未设置',
      fix: '生产环境从 .env 移除 MU_FIX_TEST_ALLOW_DEPS_CMD', ref: 'T-FIXTEST' });
  }

  // ── 8. 执行器（修复轮运行面）──
  const execMode = String(env.MU_EXECUTOR ?? '').trim();
  if (!execMode) {
    add({ id: 'executor', title: '执行器 MU_EXECUTOR',
      status: 'warn', detail: '未设置——修复轮（fixer/verifier）执行面 fail-closed（EXECUTOR_MODE_UNSET），审查主链不受影响',
      fix: '需要修复验证能力时：MU_EXECUTOR=agentteams 并配齐 AgentTeams 五项 + Matrix 三项（见 DEPLOY-GUIDE「启用修复验证」）', ref: 'T-EXECUTOR' });
  } else if (execMode === 'agentteams') {
    const need = ['MU_AGENTTEAMS_BASE_URL', 'MU_AGENTTEAMS_TOKEN', 'MU_AGENTTEAMS_RUNTIME', 'MU_AGENTTEAMS_MODEL',
      'MU_AGENTTEAMS_MATRIX_URL', 'MU_AGENTTEAMS_MATRIX_USER', 'MU_AGENTTEAMS_MATRIX_PASSWORD'];
    const missing = need.filter((k) => String(env[k] ?? '').trim() === '');
    add({ id: 'executor', title: '执行器 MU_EXECUTOR=agentteams（七项配置）',
      status: missing.length ? 'fail' : 'pass',
      detail: missing.length ? `缺失：${missing.join(', ')}` : '七项齐全（Matrix 传输层含在内）',
      fix: missing.length ? '补齐缺失项；Matrix 三项缺失时任务传输 fail-closed（MT_NOT_CONFIGURED）' : undefined, ref: 'T-EXECUTOR' });
  } else if (execMode === 'internal') {
    const allow = String(env.MU_EXECUTOR_INTERNAL_ALLOW ?? '').trim();
    add({ id: 'executor', title: '执行器 MU_EXECUTOR=internal（降级路径）',
      status: ['development', 'test', 'emergency'].includes(allow) ? 'warn' : 'fail',
      detail: allow ? `MU_EXECUTOR_INTERNAL_ALLOW=${allow}（非生产路径）` : 'internal 未声明 MU_EXECUTOR_INTERNAL_ALLOW——执行器门拒绝（EXECUTOR_INTERNAL_NOT_ALLOWED）',
      fix: allow ? undefined : '设置 MU_EXECUTOR_INTERNAL_ALLOW=development|test|emergency 之一（仅开发/测试/应急）；或改用 MU_EXECUTOR=agentteams', ref: 'T-EXECUTOR' });
  } else {
    add({ id: 'executor', title: '执行器 MU_EXECUTOR',
      status: 'fail', detail: `非法值：${execMode}——执行器门拒绝（EXECUTOR_MODE_INVALID）`,
      fix: '改为 agentteams（生产唯一选项）或 internal + MU_EXECUTOR_INTERNAL_ALLOW（降级）', ref: 'T-EXECUTOR' });
  }

  // ── 9. Gitee（PAT + OAuth，可选）──
  const giteePat = String(env.MU_GITEE_PAT ?? '').trim() !== '';
  const giteeOauth = ['MU_GITEE_OAUTH_CLIENT_ID', 'MU_GITEE_OAUTH_CLIENT_SECRET', 'MU_GITEE_OAUTH_CALLBACK_URL']
    .every((k) => String(env[k] ?? '').trim() !== '');
  if (giteePat || giteeOauth) {
    add({ id: 'gitee', title: 'Gitee 接入（PAT / OAuth）',
      status: 'pass',
      detail: [`部署级 PAT（Gitee 仓库连接/评论发布）: ${giteePat ? '已设置' : '未设置'}`,
        `Gitee OAuth 登录三项: ${giteeOauth ? '齐全' : '未配齐（登录页不渲染 Gitee 入口，fail-closed）'}`].join('；') });
  } else {
    add({ id: 'gitee', title: 'Gitee 接入（PAT / OAuth）',
      status: 'warn', detail: '未配置任何 Gitee 项——仅 GitHub 接入可用（属预期可选形态）',
      fix: '需要 Gitee 仓库/发布时：设置 MU_GITEE_PAT（仓库连接与评论）与 MU_GITEE_OAUTH_* 三项（Gitee 登录）', ref: 'T-GITEE' });
  }

  // ── 10. job consumer（webhook 自动消费）──
  const jcRaw = String(env.MU_JOB_CONSUMER_ENABLED ?? '1').toLowerCase();
  const jcEnabled = !['0', 'off', 'false'].includes(jcRaw);
  const jcInterval = env.MU_JOB_CONSUMER_INTERVAL_MS === undefined
    ? 45000 : Number(env.MU_JOB_CONSUMER_INTERVAL_MS);
  const jcIntervalOk = env.MU_JOB_CONSUMER_INTERVAL_MS === undefined
    || (Number.isFinite(jcInterval) && jcInterval >= 250 && jcInterval <= 600000);
  add({ id: 'job_consumer', title: 'webhook job 自动消费循环',
    status: jcEnabled && jcIntervalOk ? 'pass' : 'fail',
    detail: jcEnabled
      ? (jcIntervalOk ? `开启（间隔 ${jcInterval}ms）` : `间隔非法：${env.MU_JOB_CONSUMER_INTERVAL_MS}（允许 250–600000ms）`)
      : 'MU_JOB_CONSUMER_ENABLED 已关闭——webhook 事件将滞留队列，需人工 POST /api/mu/jobs/tick 才消费',
    fix: jcEnabled ? (jcIntervalOk ? undefined : '把 MU_JOB_CONSUMER_INTERVAL_MS 设置为 250–600000 之间的数值') : '从 .env 移除 MU_JOB_CONSUMER_ENABLED=0（或改回 1）', ref: 'T-WEBHOOK' });

  // ── 11. fixture 登录（生产应关闭）──
  if (String(env.MU_ALLOW_FIXTURE_LOGIN ?? '').trim() === '1') {
    add({ id: 'fixture_login', title: 'MU_ALLOW_FIXTURE_LOGIN=1',
      status: 'warn', detail: 'fixture 引导操作员登录已开启——仅用于首次建管/开发测试，生产保持关闭',
      fix: '首个平台管理员就绪后从 .env 移除该项并重启', ref: 'T-OAUTH' });
  }

  // ── 12. cchain 三键（可选组件）──
  const cc3 = ['MERGEPILOT_MODEL_CACHE_DIR', 'MERGEPILOT_PROVIDER_ATTEST_URL', 'MERGEPILOT_RUN_BINDING_KEYSTORE'];
  const ccHave = cc3.filter((k) => String(env[k] ?? '').trim() !== '').length;
  if (ccHave > 0 && ccHave < 3) {
    add({ id: 'cchain', title: 'cchain 三键（运行绑定信任链，可选）',
      status: 'warn', detail: `${ccHave}/3——三项不全则 cchain=BLOCKED（fail-closed 如实呈现，不影响审查主链）`,
      fix: '三键齐备见 deploy/selfhost/README.md「provider attestation」节；不需要 cchain 可全部留空', ref: 'T-CCHAIN' });
  } else {
    add({ id: 'cchain', title: 'cchain 三键（运行绑定信任链，可选）',
      status: ccHave === 3 ? 'pass' : 'skip',
      detail: ccHave === 3 ? '三键齐全（最终以控制台 /api/cchain/status 为准）' : '全部未设置——cchain=BLOCKED（可选组件，不影响审查主链）' });
  }

  // ── 13. HTTP：/api/health ──
  if (fetchImpl && baseUrl) {
    const health = await fetchJson(fetchImpl, `${baseUrl}/api/health`);
    if (!health.ok) {
      add({ id: 'http_health', title: 'GET /api/health',
        status: 'fail', detail: health.detail,
        fix: '确认栈已启动（docker compose ps 全 healthy）且 base-url 正确；容器日志 docker compose logs console', ref: 'T-HTTP' });
    } else {
      const h = health.json ?? {};
      add({ id: 'http_health', title: 'GET /api/health',
        status: h.ok === true ? 'pass' : 'fail',
        detail: `ok=${h.ok} version=${h.version ?? '?'}${h.git_commit ? ` commit=${String(h.git_commit).slice(0, 12)}` : ''}`,
        fix: h.ok === true ? undefined : 'health ok!=true——查 docker compose logs console', ref: 'T-HTTP' });
      add({ id: 'http_schema_ready', title: 'schema 就绪（mu_schema_ready）',
        status: h.mu_schema_ready === true ? 'pass' : 'fail',
        detail: h.mu_schema_ready === true ? 'schema 已初始化' : `未就绪${h.mu_schema_note ? `（${h.mu_schema_note}）` : ''}`,
        fix: h.mu_schema_ready === true ? undefined : '等 console 首次启动迁移完成；持续失败查 postgres 容器日志与 pgdata 卷', ref: 'T-DB' });
    }

    // ── 14. HTTP：/api/mu/auth/providers（公开端点）──
    const prov = await fetchJson(fetchImpl, `${baseUrl}/api/mu/auth/providers`);
    if (!prov.ok) {
      add({ id: 'http_providers', title: 'GET /api/mu/auth/providers',
        status: 'fail', detail: prov.detail, fix: '同 http_health 处理', ref: 'T-HTTP' });
    } else {
      const p = prov.json ?? {};
      const lines = ['github', 'gitee'].map((k) => `${k}: ${p[k]?.configured ? '已配置' : `未配置${p[k]?.reason ? `（${p[k].reason}）` : ''}`}`);
      lines.push(`fixture 登录: ${p.fixture?.configured ? '开启（生产应关闭）' : '关闭'}`);
      add({ id: 'http_providers', title: '登录方式配置（服务端视角）',
        status: p.github?.configured ? 'pass' : 'fail', detail: lines.join('；'),
        fix: p.github?.configured ? undefined : '服务端报告 GitHub OAuth 未配置——核对 .env 三项是否进入了容器环境（改 .env 后须重启）', ref: 'T-OAUTH' });
    }
  } else {
    add({ id: 'http_health', title: 'GET /api/health', status: 'skip',
      detail: fetchImpl ? '未提供 --base-url' : '--no-net 跳过' });
    add({ id: 'http_providers', title: '登录方式配置（服务端视角）', status: 'skip',
      detail: fetchImpl ? '未提供 --base-url' : '--no-net 跳过' });
  }

  // ── 15. 数据库 schema 版本（docker exec psql，只读查询）──
  if (dockerExec) {
    const r = dockerExec(pgContainer,
      ['psql', '-U', 'postgres', '-d', 'mu', '-tAc', 'SELECT COALESCE(MAX(version),0) FROM mu.schema_migrations']);
    if (r.status !== 0) {
      add({ id: 'db_schema', title: `数据库 schema 版本（容器 ${pgContainer}）`,
        status: 'fail', detail: `查询失败：${String(r.stderr ?? '').trim().slice(0, 120) || `退出码 ${r.status}`}`,
        fix: '确认容器名（docker ps）与 -d mu 数据库；容器未起则先 docker compose up -d', ref: 'T-DB' });
    } else {
      const v = Number(String(r.stdout ?? '').trim());
      add({ id: 'db_schema', title: `数据库 schema 版本（容器 ${pgContainer}）`,
        status: v >= EXPECTED_SCHEMA_VERSION ? 'pass' : 'fail',
        detail: `当前 v${v}（期望 >= v${EXPECTED_SCHEMA_VERSION}；console 启动时自动迁移）`,
        fix: v >= EXPECTED_SCHEMA_VERSION ? undefined
          : `schema 落后：升级 console 镜像并重启（迁移自动执行、幂等、只前进）；不要手工改 mu schema`, ref: 'T-DB' });
    }
  } else {
    add({ id: 'db_schema', title: '数据库 schema 版本', status: 'skip',
      detail: '未提供 --pg-container（或宿主无 docker）——可用 --pg-container <容器名> 启用' });
  }

  return checks;
}

function checkKeys(add, env, id, title, keys, ref) {
  const missing = keys.filter(([k]) => String(env[k] ?? '').trim() === '');
  add({
    id, title,
    status: missing.length === 0 ? 'pass' : 'fail',
    detail: missing.length === 0
      ? keys.map(([k, label]) => `${label}（${k}）: ${displayValue(k, env[k])}`).join('；')
      : `缺失：${missing.map(([k]) => k).join(', ')}`,
    fix: missing.length === 0 ? undefined
      : `在 .env 补齐并重启栈：${missing.map(([k, label]) => `${k}（${label}）`).join('、')}`,
    ref,
  });
}

async function fetchJson(fetchImpl, url) {
  try {
    const res = await fetchImpl(url, { signal: AbortSignal.timeout(8000), headers: { accept: 'application/json' } });
    const text = await res.text();
    let json = null;
    try { json = JSON.parse(text); } catch { /* 非 JSON */ }
    if (res.status !== 200) return { ok: false, detail: `HTTP ${res.status}${json?.error?.reason ? `（${json.error.reason}）` : ''}` };
    return { ok: true, json };
  } catch (e) {
    return { ok: false, detail: `不可达：${String(e?.message ?? e).slice(0, 120)}` };
  }
}

function summarize(checks) {
  const s = { pass: 0, fail: 0, warn: 0, skip: 0 };
  for (const c of checks) s[c.status] = (s[c.status] ?? 0) + 1;
  return s;
}

function renderHuman(checks, meta) {
  const lines = [];
  lines.push('== MergePilot 交付诊断（只读）==');
  lines.push(`时间 ${meta.checked_at}  base-url ${meta.base_url || '(未指定)'}  env ${meta.env_file || '(未指定)'}`);
  lines.push('');
  for (const c of checks) {
    const tag = { pass: 'PASS', fail: 'FAIL', warn: 'WARN', skip: 'SKIP' }[c.status] ?? c.status;
    lines.push(`  ${tag}  ${c.title}`);
    if (c.detail) for (const d of String(c.detail).split('；')) lines.push(`        ${d}`);
    if (c.status === 'fail' && c.fix) {
      lines.push(`        → 处理：${c.fix}`);
      if (c.ref) lines.push(`        → 详见 docs/delivery/TROUBLESHOOTING.md「${c.ref}」`);
    } else if (c.status === 'warn' && c.fix) {
      lines.push(`        → 建议：${c.fix}`);
      if (c.ref) lines.push(`        → 详见 docs/delivery/TROUBLESHOOTING.md「${c.ref}」`);
    }
  }
  const s = summarize(checks);
  lines.push('');
  lines.push(`${s.pass} pass, ${s.fail} fail, ${s.warn} warn, ${s.skip} skip${s.fail ? ' —— 有失败项，按 FAIL 行的处理步骤修复后重跑' : ' —— 无失败项'}`);
  return lines.join('\n');
}

async function main() {
  const args = process.argv.slice(2);
  if (args.includes('-h') || args.includes('--help')) {
    console.log('用法: node scripts/delivery/diagnose.mjs [--env <.env>] [--base-url <url>] [--pg-container <name>] [--json] [--no-net]');
    process.exit(2);
  }
  const argOf = (flag) => { const i = args.indexOf(flag); return i >= 0 ? args[i + 1] : null; };
  const noNet = args.includes('--no-net');
  const asJson = args.includes('--json');
  const envPath = argOf('--env') ?? path.join(process.cwd(), '.env');
  const baseUrl = argOf('--base-url') ?? 'http://127.0.0.1:48500';
  const pgContainer = argOf('--pg-container');
  if (args.includes('--env') && !envPath) { console.error('--env 需要路径参数'); process.exit(2); }

  let fileEnv = {};
  if (envPath && fs.existsSync(envPath)) {
    fileEnv = parseEnvFile(fs.readFileSync(envPath, 'utf8'));
  } else if (argOf('--env')) {
    console.error(`--env 指定的文件不存在: ${envPath}`);
    process.exit(2);
  }
  // 进程 env 优先于文件（容器外手工覆盖场景）；secret 类键不落任何输出。
  const merged = { ...fileEnv };
  for (const [k, v] of Object.entries(process.env)) {
    if (/^[A-Z][A-Z0-9_]*$/.test(k) && k in fileEnv) merged[k] = v;
  }

  const dockerExec = pgContainer
    ? (container, a) => spawnSync('docker', ['exec', container, ...a], { encoding: 'utf8' })
    : null;

  const checks = await runChecks({
    env: merged,
    envFile: fs.existsSync(envPath) ? envPath : null,
    fetchImpl: noNet ? null : undefined,
    dockerExec,
    pgContainer,
    baseUrl,
  });
  const meta = {
    tool: 'mergepilot-delivery-diagnose', version: DIAGNOSE_VERSION,
    checked_at: new Date().toISOString(), base_url: baseUrl,
    env_file: fs.existsSync(envPath) ? envPath : null,
    expected_schema_version: EXPECTED_SCHEMA_VERSION,
  };
  if (asJson) {
    console.log(JSON.stringify({ ...meta, summary: summarize(checks), checks }, null, 2));
  } else {
    console.log(renderHuman(checks, meta));
  }
  process.exit(checks.some((c) => c.status === 'fail') ? 1 : 0);
}

const __argv1 = process.argv[1] ? path.resolve(process.argv[1]) : '';
if (__argv1 && import.meta.url === pathToFileURL(__argv1).href) {
  await main();
}
