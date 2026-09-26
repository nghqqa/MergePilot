// fxv/archive.mjs — attempt artifact 归档与校验（管线旁路，不改变状态机语义）。
// 归档失败/校验失败 → attempt 置 artifact_status=FAILED（MANUAL_WAIT 可人工恢复重归档），
// 绝不让 artifact 异常静默。脱敏：内容含真实凭据形状即拒存（AZ 红线）。
import { attemptManifestKey, verifyAttemptArtifacts } from './artifacts.mjs';


// 在管线各关键点登记 artifact 条目（state_detail.artifacts）
export async function recordArtifact(store, attemptId, name, put) {
  const r = await put();
  if (!r.ok) {
    await store.compareAndSetState(attemptId, (await store.getAttempt(attemptId)).state, (await store.getAttempt(attemptId)).state, {
      artifact_status: 'FAILED', artifact_problem: `${name}: ${r.reason}` });
    await store.recordEvent({ attempt_id: attemptId, kind: 'ARTIFACT_FAILED', reason: `${name}: ${r.reason}` });
    throw new Error(`artifact ${name}: ${r.reason}`);
  }
  const cur = await store.getAttempt(attemptId);
  const arts = { ...(cur.state_detail?.artifacts ?? {}), [name]: { key: r.key, sha256: r.sha256, at: new Date().toISOString() } };
  await store.compareAndSetState(attemptId, cur.state, cur.state, { artifacts: arts, artifact_status: 'OK' });
  return r;
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
  await store.recordEvent({ attempt_id: attemptId, kind: 'ARTIFACT_ARCHIVED', meta: { keys: Object.keys(entries) } });
  return { ok: true, entries };
}

async function failArtifact(store, cur, problem) {
  await store.compareAndSetState(cur.attempt_id, cur.state, cur.state, { artifact_status: 'FAILED', artifact_problem: problem });
  await store.recordEvent({ attempt_id: cur.attempt_id, kind: 'ARTIFACT_FAILED', reason: problem });
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
