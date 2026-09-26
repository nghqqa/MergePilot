// fxv-artifact.test.mjs — 真 MinIO(S3 sigv4) 内容寻址归档回路。需 env FXV_S3_*；
// 缺省时整套 SKIP（显式原因）。运行：node --test test/fxv-artifact.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createArtifactStore, artifactSecretShaped, sha256hex, attemptManifestKey } from '../lib/fxv/artifacts.mjs';
import { archiveAttempt, artifactStatusOf } from '../lib/fxv/archive.mjs';

const envOk = process.env.FXV_S3_ENDPOINT && process.env.FXV_S3_ACCESS_KEY && process.env.FXV_S3_SECRET_KEY;
const store = envOk ? createArtifactStore({
  endpoint: process.env.FXV_S3_ENDPOINT, bucket: process.env.FXV_S3_BUCKET || 'fxv-artifacts-test',
  accessKey: process.env.FXV_S3_ACCESS_KEY, secretKey: process.env.FXV_S3_SECRET_KEY }) : null;

test('artifact 全链（真 MinIO）', { skip: !envOk && 'FXV_S3_* 未配置（需真 MinIO，禁 mock）' }, async () => {
  await store.ensureBucket();
  const put = await store.putContent('patch', 'diff --git + int(x)');
  assert.equal(put.ok, true, JSON.stringify(put));
  assert.equal(put.key, `fxv/artifacts/sha256/${sha256hex('diff --git + int(x)')}`);
  assert.equal(put.verified, true);
  // 读回校验 OK；篡改（删后写同名 key 他容）→ DIGEST_MISMATCH
  assert.equal((await store.verifyKey(put.key, put.sha256)).status, 'OK');
  assert.equal((await store.verifyKey('fxv/artifacts/sha256/' + '0'.repeat(64))).status, 'MISSING');
  await store.deleteKey(put.key);
  await store.putRaw(put.key, Buffer.from('tampered'));
  assert.equal((await store.verifyKey(put.key, put.sha256)).status, 'DIGEST_MISMATCH');
  // 凭据形状拒存
  const bad = await store.putContent('x', 'token = "ghp_' + 'Zz9Xx8Yy'.repeat(5) + '"');
  assert.equal(bad.ok, false);
  assert.equal(bad.reason, 'ARTIFACT_CONTAINS_SECRET_SHAPED_CONTENT');
});

test('archiveAttempt + artifactStatusOf（memStore + 真 S3）', { skip: !envOk && 'FXV_S3_* 未配置' }, async () => {
  await store.ensureBucket();
  const mem = {
    m: new Map(),
    async getAttempt(i){return this.m.get(i)??null},
    async listEvents(){return [{kind:'FILED'}]},
    async compareAndSetState(i,e,n,d){const r=this.m.get(i);if(!r||r.state!==e)return null;r.state=n;r.state_detail={...(r.state_detail??{}),...(d??{})};return{...r}},
    async recordEvent(){},
  };
  mem.m.set('att-a', { attempt_id:'att-a', ticket_id:'t1', finding_id:'f1', repo:'acme/app', branch:'main', base_head_sha:'h', patch_digest:'d', state:'DRY_RUN_COMPLETE', state_detail:{ artifacts:{ patch:{ key:'fxv/artifacts/sha256/'+sha256hex('p'), sha256:sha256hex('p') } } } });
  const put = await store.putContent('patch', 'p'); assert.equal(put.ok, true);
  const r = await archiveAttempt(mem, store, 'att-a');
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.ok(r.entries.audit && r.entries.audit.key);
  const st = await artifactStatusOf(mem, store, mem.m.get('att-a'));
  assert.equal(st.artifact_status, 'OK');
  // 删一个对象 → FAILED
  await store.deleteKey(r.entries.audit.key);
  const st2 = await artifactStatusOf(mem, store, mem.m.get('att-a'));
  assert.equal(st2.artifact_status, 'FAILED');
  assert.match(st2.problems.join(';'), /audit: MISSING/);
});

test('secretShape 基本豁免', () => {
  assert.equal(artifactSecretShaped('AKIAIOSFODNN7EXAMPLE'), null);
  assert.match(artifactSecretShaped('-----BEGIN ' + 'RSA PRIVATE KEY-----'), /PRIVATE/);
});
