// fxv/archive.mjs — attempt artifact 归档与校验（管线旁路，不改变状态机语义）。
// 归档失败/校验失败 → attempt 置 artifact_status=FAILED（MANUAL_WAIT 可人工恢复重归档），
// 绝不让 artifact 异常静默。脱敏：内容含真实凭据形状即拒存（AZ 红线）。
import { attemptManifestKey, verifyAttemptArtifacts } from './artifacts.mjs';
import { runPipeline } from './orchestrator.mjs';


// 在管线各关键点登记 artifact 条目（state_detail.artifacts）
export async function recordArtifact(store, attemptId, name, put) {
  const t0 = Date.now();
  const r = await put();
  const latency_ms = Date.now() - t0;
  if (!r.ok) {
    await store.compareAndSetState(attemptId, (await store.getAttempt(attemptId)).state, (await store.getAttempt(attemptId)).state, {
      artifact_status: 'FAILED', artifact_problem: `${name}: ${r.reason}` });
    await store.recordEvent({ attempt_id: attemptId, kind: 'ARTIFACT_FAILED', reason: `${name}: ${r.reason}` });
    throw new Error(`artifact ${name}: ${r.reason}`);
  }
  const cur = await store.getAttempt(attemptId);
  const arts = { ...(cur.state_detail?.artifacts ?? {}), [name]: { key: r.key, sha256: r.sha256, bytes: r.bytes ?? null, at: new Date().toISOString() } };
  await store.compareAndSetState(attemptId, cur.state, cur.state, { artifacts: arts, artifact_status: 'OK' });
  await store.recordEvent({ attempt_id: attemptId, kind: 'ARTIFACT_WRITTEN', actor: 'artifact-pipeline', reason: name, meta: { key: r.key, bytes: r.bytes ?? null, latency_ms } });
  return r;
}

// 业务成功(DRY_RUN_COMPLETE/VERIFIED)必须 artifact 全量校验通过 -> COMPLETE；
// 校验/归档失败 -> 翻 ERROR_FATAL（archive 失败绝不产生成功状态）。
// 业务失败场景同样归档失败证据（证据完备=COMPLETE，业务状态另有区分）。
export async function runPipelineArchived(store, cfg, handlers, attemptId, artifactStore, { actor = 'fxv-runner' } = {}) {
  const state = await runPipeline(store, cfg, handlers, attemptId, actor);
  const cur = await store.getAttempt(attemptId);
  const bizSuccess = state === 'DRY_RUN_COMPLETE' || state === 'VERIFIED';
  if (!artifactStore?.configured) {
    if (bizSuccess) {
      await store.compareAndSetState(attemptId, cur.state, cur.state, { artifact_status: 'FAILED', artifact_problem: 'ARTIFACT_STORE_NOT_CONFIGURED' });
      await store.recordEvent({ attempt_id: attemptId, kind: 'ARTIFACT_FAILED', reason: 'ARTIFACT_STORE_NOT_CONFIGURED' });
      await store.compareAndSetState(attemptId, cur.state, 'ERROR_FATAL', { last_reason: 'artifact archive unavailable - success not claimable' });
      return 'ERROR_FATAL';
    }
    return state;
  }
  const arch = await archiveAttempt(store, artifactStore, attemptId);
  const after = await store.getAttempt(attemptId);
  if (!arch.ok) {
    if (bizSuccess) {
      await store.compareAndSetState(attemptId, after.state, 'ERROR_FATAL', { last_reason: 'archive failed: ' + arch.reason });
      await store.recordEvent({ attempt_id: attemptId, kind: 'ARTIFACT_FAILED', reason: 'success->ERROR_FATAL: ' + arch.reason });
      return 'ERROR_FATAL';
    }
    return state;
  }
  await store.compareAndSetState(attemptId, after.state, after.state, { artifact_status: 'COMPLETE' });
  await store.recordEvent({ attempt_id: attemptId, kind: 'ARTIFACT_COMPLETE', actor: 'artifact-pipeline', meta: { state } });
  return state;
}

// 终态归档：审计事件快照 + manifest + 校验回读
export async function archiveAttempt(store, artifactStore, attemptId) {
  const cur = await store.getAttempt(attemptId);
  if (!cur) return { ok: false, reason: 'attempt_not_found' };
  if (!artifactStore?.configured) return { ok: false, reason: 'ARTIFACT_STORE_NOT_CONFIGURED' };
  const events = await store.listEvents(attemptId);
  const entries = { ...(cur.state_detail?.artifacts ?? {}) };
  // 审计快照（内容寻址）
  const auditPut = await artifactStore.putContent('audit', JSON.stringify(events, null, 2));
  if (!auditPut.ok) return failArtifact(store, cur, `audit: ${auditPut.reason}`);
  entries.audit = { key: auditPut.key, sha256: auditPut.sha256, at: new Date().toISOString() };
  // manifest（固定 key）
  const manifest = { ...cur, state_detail: { ...cur.state_detail, artifacts: entries } };
  const mPut = await artifactStore.putRaw(attemptManifestKey(attemptId), Buffer.from(JSON.stringify(manifest, null, 2)));
  if (!mPut.ok) return failArtifact(store, cur, `manifest: ${mPut.reason}`);
  // 全量校验
  const v = await verifyAttemptArtifacts(artifactStore, cur, entries);
  if (!v.ok) return failArtifact(store, cur, v.problems.join(';'));
  await store.compareAndSetState(attemptId, cur.state, cur.state, { artifacts: entries, artifact_status: 'OK' });
  await store.recordEvent({ attempt_id: attemptId, kind: 'ARTIFACT_ARCHIVED', actor: 'artifact-pipeline', meta: { keys: Object.keys(entries) } });
  return { ok: true, entries };
}

async function failArtifact(store, cur, problem) {
  await store.compareAndSetState(cur.attempt_id, cur.state, cur.state, { artifact_status: 'FAILED', artifact_problem: problem });
  await store.recordEvent({ attempt_id: cur.attempt_id, kind: 'ARTIFACT_FAILED', actor: 'artifact-pipeline', reason: problem });
  return { ok: false, reason: problem };
}

// 读侧校验（API 用）：artifact 缺失/损坏/digest 不一致 → FAILED 状态
export async function artifactStatusOf(store, artifactStore, attempt) {
  const entries = attempt.state_detail?.artifacts;
  if (!entries || Object.keys(entries).length === 0) return { artifact_status: 'NONE' };
  if (attempt.state_detail?.artifact_status === 'FAILED') {
    return { artifact_status: 'FAILED', problem: attempt.state_detail.artifact_problem };
  }
  if (!artifactStore?.configured) return { artifact_status: 'UNKNOWN', reason: 'ARTIFACT_STORE_NOT_CONFIGURED' };
  const v = await verifyAttemptArtifacts(artifactStore, attempt, entries);
  return v.ok ? { artifact_status: 'OK', keys: Object.keys(entries) }
              : { artifact_status: 'FAILED', problems: v.problems };
}
