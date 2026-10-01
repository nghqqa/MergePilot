// Wave 3.13：Copaw bridge 消费探针（recover-runtime.sh 第 8 步调用）。
// 在 ctrl netns 内运行：向 reviewer 发一次最小委托并等待回复——
// 证明桥"真正可消费任务"，而非仅 service/heartbeat 存活。
// 零正文泄漏：输出只含 probe_reply 布尔与 reason code。
const mt = await import('file:///be/console/backend/lib/multiuser/agents/matrix-transport.mjs');

const mtCfg = {
  kind: 'matrix',
  baseUrl: 'http://127.0.0.1:6167',
  user: 'agentteams-beta-admin',
  password: process.env.ATB_ADMIN_PASSWORD ?? '',
};

const list = await mt.listWorkersDetail(
  { baseUrl: 'http://127.0.0.1:8090', token: process.env.AT_TOKEN ?? '' }, { fetchImpl: fetch });
if (!list.ok) { console.log(JSON.stringify({ probe_reply: false, reason: 'AT_WORKERS_DETAIL_FAILED' })); process.exit(0); }
const w = list.workers.get('mergepilot-reviewer');
if (!w?.roomID || !w?.matrixUserID) { console.log(JSON.stringify({ probe_reply: false, reason: 'AT_WORKERS_INCOMPLETE' })); process.exit(0); }

const sub = `probe-${Date.now()}`;
const sent = await mt.sendTaskDelegation(mtCfg, {
  room: w.roomID, workerMatrixId: w.matrixUserID, taskId: 't-probe', correlationId: sub,
  submissionId: sub, role: 'reviewer',
  brief: 'readiness probe: reply with {"findings":[]} only.', fetchImpl: fetch,
});
if (!sent.ok) { console.log(JSON.stringify({ probe_reply: false, reason: sent.reason })); process.exit(0); }

const got = await mt.collectReply(mtCfg, {
  room: w.roomID, expectedSender: w.matrixUserID, marker: `[mp:${sub}]`,
  sinceTs: sent.ts - (mt.MT_LIMITS?.markerGuardMs ?? 5000), timeoutMs: 120_000, fetchImpl: fetch,
});
console.log(JSON.stringify({ probe_reply: got.ok === true, reason: got.ok ? 'AT_BRIDGE_OK' : (got.reason ?? 'MT_REPLY_TIMEOUT') }));
process.exit(0);
