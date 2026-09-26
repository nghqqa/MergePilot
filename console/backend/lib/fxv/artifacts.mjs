// fxv/artifacts.mjs — MinIO(S3) 内容寻址 artifact 持久化。2026-09-26 promote 轮。
// 原则：key=sha256(内容)（content-addressed）；写入前本地哈希、写后读回再哈希双校验；
// 任何 artifact 含真实凭据形状 → 拒存（AZ refuses secrets at rest）；
// 缺失/损坏/摘要不一致 → 调用方必须把 attempt 置失败态。
// 无第三方依赖：AWS SigV4 以 node:crypto 自实现（PUT/GET/HEAD/DELETE，path-style）。
import crypto from 'node:crypto';

const SECRET_SHAPED_RE = /(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,40}|github_pat_[A-Za-z0-9_]{60,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|(password|secret|token|api_key)["']?\s*[:=]\s*["'][^"']{8,}["']/gi;
const SECRET_EXEMPT_RE = /EXAMPLE|placeholder|changeme|dummy|sample|fake|your[-_]|xxx+|abcdef|ABCDEFGH|0{8,}|REDACTED|SUPERSECRET|process\.env/i;

export function artifactSecretShaped(text) {
  const m = String(text).match(SECRET_SHAPED_RE);
  if (!m) return null;
  return m.find((x) => !SECRET_EXEMPT_RE.test(x)) ?? null;
}
export const sha256hex = (buf) => crypto.createHash('sha256').update(buf).digest('hex');

function hmac(key, s) { return crypto.createHmac('sha256', key).update(s).digest(); }

export function createArtifactStore({ endpoint, bucket, accessKey, secretKey, region = 'us-east-1', fetchImpl = fetch }) {
  if (!endpoint || !bucket || !accessKey || !secretKey) {
    return { configured: false, blocked_condition: '需要 FXV_S3_{ENDPOINT,BUCKET,ACCESS_KEY,SECRET_KEY}' };
  }
  const u = new URL(endpoint);
  const hostHeader = u.host;
  const creds = { ak: accessKey, sk: secretKey, region };

  async function s3(method, key, { body = null, headers = {} } = {}) {
    const path = `/${bucket}/${key.split('/').map(encodeURIComponent).join('/')}`;
    const url = `${u.protocol}//${u.host}${path}`;
    const amzDate = new Date().toISOString().replace(/[-:]/g, '').slice(0, 15) + 'Z';
    const date = amzDate.slice(0, 8);
    const payloadHash = body ? sha256hex(body) : sha256hex('');
    const h = { host: hostHeader, 'x-amz-content-sha256': payloadHash, 'x-amz-date': amzDate, ...headers };
    const signedHeaders = Object.keys(h).map((k) => k.toLowerCase()).sort().join(';');
    const canonicalHeaders = Object.keys(h).map((k) => `${k.toLowerCase()}:${String(h[k]).trim()}\n`).sort().join('');
    const canonical = [method, path, '', canonicalHeaders, signedHeaders, payloadHash].join('\n');
    const scope = `${date}/${creds.region}/s3/aws4_request`;
    const sts = ['AWS4-HMAC-SHA256', amzDate, scope,
      crypto.createHash('sha256').update(canonical).digest('hex')].join('\n');
    const signing = hmac(hmac(hmac(hmac(`AWS4${creds.sk}`, date), creds.region), 's3'), 'aws4_request');
    const sig = crypto.createHmac('sha256', signing).update(sts).digest('hex');
    h.authorization = `AWS4-HMAC-SHA256 Credential=${creds.ak}/${scope}, SignedHeaders=${signedHeaders}, Signature=${sig}`;
    let res;
    try { res = await fetchImpl(url, { method, headers: h, body }); }
    catch (e) { return { ok: false, status: 0, _network: String(e?.cause?.message || e?.message || e) }; }
    return res;
  }

  async function ensureBucket() {
    const r = await s3('PUT', '', { body: '' });
    if (!(r.ok || r.status === 409)) throw new Error(`ensureBucket HTTP ${r.status}`);
    return true;
  }

  // 内容寻址写入：返回 {key, sha256, verified}；写后读回校验；含凭据形状拒存
  async function putContent(name, text) {
    const buf = Buffer.from(String(text), 'utf8');
    const leak = artifactSecretShaped(buf.toString('utf8'));
    if (leak) return { ok: false, reason: 'ARTIFACT_CONTAINS_SECRET_SHAPED_CONTENT', sample: leak.slice(0, 12) + '…' };
    const digest = sha256hex(buf);
    const key = `fxv/artifacts/sha256/${digest}`;
    const r = await s3('PUT', key, { body: buf, headers: { 'content-type': 'application/json; charset=utf-8' } });
    if (!r.ok) return { ok: false, reason: `S3_PUT_HTTP_${r.status}` };
    const back = await s3('GET', key);
    if (!back.ok) return { ok: false, reason: `S3_READBACK_HTTP_${back.status}` };
    const body2 = Buffer.from(await back.arrayBuffer());
    if (sha256hex(body2) !== digest) return { ok: false, reason: 'READBACK_DIGEST_MISMATCH' };
    return { ok: true, key, sha256: digest, bytes: buf.length, verified: true };
  }

  async function verifyKey(key, expectedSha) {
    const r = await s3('HEAD', key);
    if (r.status === 404) return { status: 'MISSING' };
    if (!r.ok) return { status: 'ERROR', http: r.status };
    const back = await s3('GET', key);
    if (!back.ok) return { status: 'ERROR', http: back.status };
    const got = sha256hex(Buffer.from(await back.arrayBuffer()));
    if (expectedSha && got !== expectedSha) return { status: 'DIGEST_MISMATCH', expected: expectedSha, got };
    return { status: 'OK', sha256: got };
  }

  async function deleteKey(key) { const r = await s3('DELETE', key); return r.ok || r.status === 204; }

  // 固定 key 原始写入（attempt manifest 等可变对象）；同样写后读回校验+凭据拒存
  async function putRaw(key, buf) {
    const text = buf.toString('utf8');
    const leak = artifactSecretShaped(text);
    if (leak) return { ok: false, reason: 'ARTIFACT_CONTAINS_SECRET_SHAPED_CONTENT' };
    const digest = sha256hex(buf);
    const r = await s3('PUT', key, { body: buf, headers: { 'content-type': 'application/json; charset=utf-8' } });
    if (!r.ok) return { ok: false, reason: `S3_PUT_HTTP_${r.status}` };
    const back = await s3('GET', key);
    if (!back.ok) return { ok: false, reason: `S3_READBACK_HTTP_${back.status}` };
    if (sha256hex(Buffer.from(await back.arrayBuffer())) !== digest) return { ok: false, reason: 'READBACK_DIGEST_MISMATCH' };
    return { ok: true, key, sha256: digest, verified: true };
  }

  return { configured: true, ensureBucket, putContent, verifyKey, deleteKey, putRaw, s3PutRaw: putRaw, endpoint: u.host, bucket };
}

// attempt artifact 清单（固定 key，内容含各 content-addressed 条目）
export function attemptManifestKey(attemptId) { return `fxv/attempts/${attemptId}.manifest.json`; }

export async function writeAttemptManifest(store, attempt, entries) {
  const manifest = { attempt_id: attempt.attempt_id, ticket_id: attempt.ticket_id, finding_id: attempt.finding_id,
    repo: attempt.repo, branch: attempt.branch, base_head_sha: attempt.base_head_sha, patch_digest: attempt.patch_digest,
    pr: (attempt.state_detail && attempt.state_detail.pr) || null, run_id: (attempt.state_detail && attempt.state_detail.run_id) || attempt.receipt_id || null,
    state: attempt.state, entries, updated_at: new Date().toISOString() };
  const buf = Buffer.from(JSON.stringify(manifest, null, 2));
  const leak = artifactSecretShaped(buf.toString());
  if (leak) return { ok: false, reason: 'MANIFEST_CONTAINS_SECRET_SHAPED_CONTENT' };
  const r = await store.s3PutRaw(attemptManifestKey(attempt.attempt_id), buf);
  return r;
}

// 校验 attempt 全部 artifact：任一缺失/损坏/不一致 → {ok:false, problems}
export async function verifyAttemptArtifacts(store, attempt, entries) {
  const problems = [];
  for (const [name, e] of Object.entries(entries ?? {})) {
    const v = await store.verifyKey(e.key, e.sha256);
    if (v.status !== 'OK') problems.push(`${name}: ${v.status}`);
  }
  return { ok: problems.length === 0, problems };
}
