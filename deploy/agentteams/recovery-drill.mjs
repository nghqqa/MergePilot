// recovery-drill.mjs — AgentTeams 恢复能力隔离验证（四场景；recover-workers.sh 的验收器）
//
// 前置：itest 栈已 up（itest-stack-compose.yaml）+ recover-workers.sh 已跑过至少一次。
// 每场景验证：①四 worker 运行时模型配置=deepseek-direct ②带关联标识（submissionId marker）
// 的 Matrix 任务发送→worker 真实 LLM 调用→回复（JSON schema 校验）。
// 场景：
//   S1 初次部署后（baseline，调用于栈首次 recover 后）
//   S2 重复 provision/reconcile（再跑 recover-workers.sh——验证配置不漂移）
//   S3 worker 重启（docker restart reviewer——验证配置存活/工具恢复）
//   S4 worker 重建（docker rm reviewer——工具接管重建）
// 用法：node recovery-drill.mjs <scenario:1|2|3|4> [role]
//   场景 2/3/4 的破坏动作+恢复动作由 --with-recovery 包装执行，本脚本只做验证轮询。
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const BE = path.resolve(here, '../../console/backend');
const STACK = process.env.DRILL_STACK || 'atitest';
const CTRL = `${STACK}-ctrl`;
const API = process.env.DRILL_API || 'http://127.0.0.1:28562';
const MATRIX = process.env.DRILL_MATRIX || 'http://127.0.0.1:29768';
const ADMIN_USER = process.env.DRILL_ADMIN_USER || 'atitest-admin';
const ADMIN_PW = process.env.ATB_ADMIN_PASSWORD;
const AT_TOKEN = execFileSync('docker', ['exec', CTRL, 'sh', '-c',
  'tr -d "\\n\\r" < /var/run/agentteams/cli-token'], { encoding: 'utf8' }).trim();

if (!ADMIN_PW) { console.error('ATB_ADMIN_PASSWORD required'); process.exit(2); }
process.env.MU_EXECUTOR = 'agentteams';
process.env.MU_AGENTTEAMS_BASE_URL = API;
process.env.MU_AGENTTEAMS_TOKEN = AT_TOKEN;
process.env.MU_AGENTTEAMS_RUNTIME = 'copaw';
process.env.MU_AGENTTEAMS_MODEL = 'deepseek-chat';
process.env.MU_AGENTTEAMS_MATRIX_URL = MATRIX;
process.env.MU_AGENTTEAMS_MATRIX_USER = ADMIN_USER;
process.env.MU_AGENTTEAMS_MATRIX_PASSWORD = ADMIN_PW;

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail ? ' — ' + JSON.stringify(detail).slice(0, 200) : ''}`); }
};

const { pathToFileURL } = await import('node:url');
const at = await import(pathToFileURL(path.join(BE, 'lib/multiuser/agents/agentteams-executor.mjs')).href);
const mt = await import(pathToFileURL(path.join(BE, 'lib/multiuser/agents/matrix-transport.mjs')).href);

const cfg = at.resolveAgentTeamsConfig(process.env);
const mtCfg = mt.resolveMatrixConfig(process.env);

/** 配置验证：四 worker 运行时 primary + phase/绑定。 */
async function verifyConfig(tag) {
  const detail = await at.listWorkersDetail(cfg, {});
  ok(`${tag} workers detail ok`, detail.ok);
  if (!detail.ok) return false;
  let allOk = true;
  for (const [role, def] of Object.entries(at.AGENTTEAMS_WORKERS)) {
    const w = detail.workers.get(def.name);
    const phaseOk = w && String(w.phase).toLowerCase() === 'running';
    const bindOk = w && w.roomID && w.matrixUserID;
    ok(`${tag} ${role}: phase=Running + room/matrixID`, phaseOk && bindOk,
      { phase: w?.phase, room: Boolean(w?.roomID), mid: Boolean(w?.matrixUserID) });
    allOk = allOk && phaseOk && bindOk;
  }
  // 运行时模型配置（worker 容器本地 openclaw.json）
  for (const role of ['leader', 'reviewer', 'fixer', 'verifier']) {
    const p = execFileSync('docker', ['exec', `${STACK}-worker-mergepilot-${role}`, 'python3', '-c',
      `import json;d=json.load(open('/root/.copaw-worker/'+__import__('os').environ['AGENTTEAMS_WORKER_NAME']+'/openclaw.json'));print(d.get('models',{}).get('providers',{}).get('agentteams-gateway',{}).get('baseUrl','?'))`],
      { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim();
    ok(`${tag} ${role}: runtime gateway→deepseek 直连`, p === 'https://api.deepseek.com/v1', p);
    allOk = allOk && p === 'https://api.deepseek.com/v1';
  }
  return allOk;
}

/** Matrix 往返验证：sendTaskDelegation → worker LLM → collectReply（marker=关联标识）。 */
async function matrixRoundtrip(tag, role, taskId) {
  const detail = await at.listWorkersDetail(cfg, {});
  const def = at.AGENTTEAMS_WORKERS[role];
  const w = detail.workers.get(def.name);
  if (!w?.roomID) { ok(`${tag} ${role} roundtrip: binding missing`, false); return false; }
  const submissionId = `${tag}-${crypto.randomUUID().slice(0, 8)}`;
  const brief = `recovery drill ${tag}: report one JSON finding with severity P3, path "drill/${tag}.txt", summary "connectivity+llm ok". Dry-run only.`;
  const t0 = Date.now();
  const sent = await mt.sendTaskDelegation(mtCfg, {
    room: w.roomID, workerMatrixId: w.matrixUserID, taskId,
    correlationId: `drill-${tag}`, submissionId, role, brief });
  ok(`${tag} ${role} delegation sent (submissionId=${submissionId.slice(0, 14)}…)`, sent.ok, sent.reason);
  if (!sent.ok) return false;
  const got = await mt.collectReply(mtCfg, {
    room: w.roomID, expectedSender: w.matrixUserID, marker: `[mp:${submissionId}]`,
    sinceTs: sent.ts - mt.MT_LIMITS.markerGuardMs, timeoutMs: 120_000 });
  const latency = ((Date.now() - t0) / 1000).toFixed(1);
  if (!got.ok) { ok(`${tag} ${role} reply (${latency}s)`, false, got.reason); return false; }
  // 回复 JSON 校验（reviewer/fixer schema；非空 LLM 内容证明真实模型调用）
  const v = at.validateAgentTeamsOutput(role === 'fixer' ? 'fixer' : 'reviewer', got.json);
  const hasContent = JSON.stringify(got.json).length > 20;
  ok(`${tag} ${role} reply ${latency}s: marker 关联+schema+真实 LLM 内容`, v && hasContent,
    { schema_ok: v, bytes: JSON.stringify(got.json).length });
  return v && hasContent;
}

const scenario = process.argv[2] || '1';
const tag = `S${scenario}`;
try {
  console.log(`\n# recovery-drill ${tag}（栈=${STACK}）`);
  const cfgOk = await verifyConfig(tag);
  const rt1 = await matrixRoundtrip(tag, 'reviewer', `t-${tag.toLowerCase()}-rev`);
  const rt2 = await matrixRoundtrip(tag, 'fixer', `t-${tag.toLowerCase()}-fix`);
  console.log(`\n${tag}: ${pass} pass, ${fail} fail`);
  process.exit(cfgOk && rt1 && rt2 && fail === 0 ? 0 : 1);
} catch (e) {
  console.error('DRILL-ERROR', e?.stack ?? e);
  process.exit(1);
}
