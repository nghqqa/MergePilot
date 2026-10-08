#!/usr/bin/env node
// deploy/agentteams-beta/auth-proxy/recover-agentteams.mjs — ctrl【重建】后的注册表恢复工具。
//
// 背景（2026-10-08 生产实证，勿混淆两种操作）：
//   * docker restart agentteams-beta-ctrl —— 保留容器文件系统：cli-token 与注册表
//     （workers/teams/humans）都存活，走 RUNBOOK §4 / recover-runtime.sh 即可；
//   * 容器【重建】（rm + run，含 compose up 重建、换镜像、换端口重发布）—— 容器 FS
//     重建带来两个必然副作用：
//       1) /var/run/agentteams/cli-token（容器 FS，非卷）再生成 → **全部旧 Bearer
//          token 立即失效（401）**。这正是泄露 token 的撤销配方；
//       2) 驻留容器 FS 的注册表清空 → worker 端 `agt get` 404、循环
//          "Worker config not ready"。/data 卷不承载注册表，重建卷无效。
//
// 本工具只做【注册表恢复】：复用 Console 自带的 ensureFourAgents（生产契约路径，
// fix-orchestrator 每次 run 都会调用同一函数），不手写 API 注入、不旁路业务契约。
// worker 容器本体由 controller 在 ensure 过程中经其自身逻辑接管/重建。
//
// 纪律：
//  * 幂等——ensureFourAgents 为 GET 判存在→缺者 POST/在者 PUT（409/404 感知）→
//    完整性复查；重复运行不产生重复注册（有测试锁定）；
//  * fail-closed——健康检查未过不做任何写操作；ensure 不完整即非零退出；
//  * 凭据零回显——输出仅为结构化摘要（worker 名/数量/reason），绝无 token；
//  * 恢复完成的判定=四 worker 就绪（ctrl 列表 4/4），不是"token 换成功"。
//
// 用法（宿主机，自动复制进容器执行）：
//   node recover-agentteams.mjs --container beta-mp-console
// 容器内直接执行（等价）：
//   docker exec beta-mp-console node /tmp/recover-agentteams.mjs --in-container
// 测试：
//   import { recoverWorkerRegistry } from ...（fetchImpl 注入 mock，见
//   console/backend/test/agentteams-recovery.test.mjs）

import { pathToFileURL, fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const EXECUTOR_IN_CONTAINER = '/app/backend/lib/multiuser/agents/agentteams-executor.mjs';
const EXECUTOR_IN_REPO = '../../../console/backend/lib/multiuser/agents/agentteams-executor.mjs';

/**
 * 恢复核心：健康检查 → ensureFourAgents（幂等注册）→ 四 worker 就绪核验。
 * 返回结构化摘要（无任何凭据字段）。失败时 {ok:false, stage, reason}。
 */
export async function recoverWorkerRegistry({ env = process.env, fetchImpl = fetch } = {}) {
  const inContainer = process.argv.includes('--in-container');
  const override = env.AT_EXECUTOR_MODULE || '';
  const moduleUrl = override
    ? pathToFileURL(override).href
    : new URL(inContainer ? EXECUTOR_IN_CONTAINER : EXECUTOR_IN_REPO, import.meta.url).href;
  const mod = await import(moduleUrl);

  const cfg = mod.resolveAgentTeamsConfig(env);
  const healthy = await mod.agentTeamsHealthy(cfg, fetchImpl);
  if (!healthy.ok) return { ok: false, stage: 'healthy', reason: healthy.reason ?? 'AT_UNHEALTHY' };

  const ensure = await mod.ensureFourAgents(cfg, { fetchImpl });
  if (!ensure.ok) {
    return { ok: false, stage: 'ensure', reason: ensure.reason ?? 'AT_ENSURE_FAILED', missing: ensure.missing ?? null };
  }

  const detail = await mod.listWorkersDetail(cfg, { fetchImpl });
  if (!detail.ok) return { ok: false, stage: 'verify', reason: detail.reason ?? 'AT_WORKERS_LIST_HTTP' };
  const workers = [...detail.workers.keys()].sort();
  return {
    ok: true,
    created: ensure.created ?? [],
    workers,
    workerCount: workers.length,
    note: workers.length === 4 ? 'registry-restored-4/4' : `unexpected-worker-count=${workers.length}`,
  };
}

function isMainModule() {
  if (!process.argv[1]) return false;
  try { return pathToFileURL(process.argv[1]).href === import.meta.url; } catch { return false; }
}

async function runInContainer() {
  try {
    const result = await recoverWorkerRegistry({ env: process.env, fetchImpl: (...a) => fetch(...a) });
    console.log(JSON.stringify(result));
    process.exit(result.ok ? 0 : 1);
  } catch (e) {
    console.log(JSON.stringify({ ok: false, stage: 'bootstrap', reason: String(e?.message ?? e).slice(0, 200) }));
    process.exit(1);
  }
}

function hostMain(argv) {
  const i = argv.indexOf('--container');
  const container = i >= 0 ? argv[i + 1] : 'beta-mp-console';
  const self = fileURLToPath(import.meta.url);
  const step = (args, input) => {
    const r = spawnSync('docker', args, { encoding: 'utf8', input, maxBuffer: 16 * 1024 * 1024 });
    if (r.status !== 0) {
      console.error(`recover: docker step failed: ${(r.stderr || r.stdout || '').trim().slice(0, 200)}`);
      process.exit(1);
    }
    return (r.stdout || '').trim();
  };
  step(['cp', self, `${container}:/tmp/recover-agentteams.mjs`]);
  const out = step(['exec', container, 'node', '/tmp/recover-agentteams.mjs', '--in-container']);
  console.log(out);
  try {
    const parsed = JSON.parse(out.split('\n').filter((l) => l.startsWith('{')).pop() ?? '');
    process.exit(parsed.ok ? 0 : 1);
  } catch {
    console.error('recover: unreadable summary from container');
    process.exit(1);
  }
}

const argv = process.argv.slice(2);
if (isMainModule()) {
  if (argv.includes('--in-container')) await runInContainer();
  else hostMain(argv);
}
