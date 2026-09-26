// fxv/exec/exec.mjs — 把独立进程 workers 接成 orchestrator handlers + 评审回执→立案接入。
// 真实性：handlers 每步 spawn 真实子进程（fixer/verifier 均为独立进程边界）；
// Verifier 只收 artifact（repo/head/patch/digest/test_cmd），绝不收 fixer 工作区或理由。
import { spawn } from 'node:child_process';
import path from 'node:path';
import fs from 'node:fs';
import os from 'node:os';
import crypto from 'node:crypto';
import { recordArtifact } from '../archive.mjs';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FIXER = path.join(HERE, 'worker-fixer.mjs');
const VERIFIER = path.join(HERE, 'worker-verifier.mjs');

function runWorker(script, payload, timeoutMs = 120_000) {
  return new Promise((resolve, reject) => {
    const p = spawn(process.execPath, ['--disable-warning=ExperimentalWarning', script], { stdio: ['pipe', 'pipe', 'pipe'] });
    let out = '', err = '';
    const t = setTimeout(() => { p.kill('SIGKILL'); reject(Object.assign(new Error('worker timeout'), { code: 'STEP_TIMEOUT' })); }, timeoutMs);
    p.stdout.on('data', (d) => { out += d; });
    p.stderr.on('data', (d) => { err += d; });
    p.on('close', (code) => {
      clearTimeout(t);
      let parsed = null;
      try { parsed = JSON.parse(out.trim().split('\n').pop()); } catch { /* keep null */ }
      if (parsed) return resolve({ exit_code: code, ...parsed });
      reject(new Error(`worker unparsable (exit ${code}): ${err.slice(0, 300)}`));
    });
    p.stdin.write(JSON.stringify(payload));
    p.stdin.end();
  });
}

const wsRoot = (root) => path.join(root, 'fxv-ws-' + crypto.randomBytes(6).toString('hex'));

export function makeExecHandlers({ cfg, repoUrl, workspaceRoot = os.tmpdir(), testCmd, store = null, artifactStore = null }) {
  fs.mkdirSync(workspaceRoot, { recursive: true });
  let lastPatch = null;
  let lastVerifier = null;
  let lastAttemptId = null;
  return {
    generatePatch: async (cur) => {
      const r = await runWorker(FIXER, {
        repo_url: repoUrl(cur.repo), base_head_sha: cur.base_head_sha,
        finding: { file: cur.rule_file ?? cur.state_detail?.rule_file, pattern: cur.rule_pattern ?? cur.state_detail?.rule_pattern, replacement: cur.rule_replacement ?? cur.state_detail?.rule_replacement },
        workspace: wsRoot(workspaceRoot),
      });
      if (!r.ok) throw Object.assign(new Error(`fixer: ${r.reason}`), { code: r.reason === 'EMPTY_PATCH' ? 'PATCH_EMPTY' : 'FIXER_FAILED' });
      lastPatch = r.patch_text;
      lastAttemptId = cur.attempt_id;
      if (store && artifactStore?.configured) {
        await recordArtifact(store, cur.attempt_id, 'patch', () => artifactStore.putContent('patch',
          JSON.stringify({ attempt_id: cur.attempt_id, ticket_id: cur.ticket_id, repo: cur.repo,
            pr: cur.state_detail?.pr ?? null, base_head_sha: cur.base_head_sha,
            patch_digest: r.patch_digest, patch_text: r.patch_text }, null, 2)));
      }
      return { patch_text: r.patch_text, patch_digest: r.patch_digest };
    },
    dryRunApply: async (cur) => {
      const r = await runWorker(VERIFIER, {
        repo_url: repoUrl(cur.repo), base_head_sha: cur.base_head_sha,
        patch_text: lastPatch, patch_digest: cur.patch_digest,
        test_cmd: testCmd, workspace: wsRoot(workspaceRoot),
      });
      lastVerifier = r;
      if (store && artifactStore?.configured) {
        await recordArtifact(store, cur.attempt_id, 'verifier_verdict', () => artifactStore.putContent('verifier',
          JSON.stringify({ attempt_id: cur.attempt_id, verdict: r.verdict, reason: r.reason,
            evidence: r.evidence, independently_derived: true }, null, 2)));
      }
      if (r.evidence?.applied !== true) {
        throw Object.assign(new Error(`verifier: ${r.reason} ${JSON.stringify(r.evidence||{}).slice(0,300)}`), { code: 'PATCH_NOT_APPLICABLE' });
      }
      return { applied: true, verdict: r.verdict };
    },
    runTests: async (cur) => {
      const result = lastVerifier
        ? { passed: lastVerifier.verdict === 'VERIFIED', output: `${lastVerifier.reason}\n${lastVerifier.evidence?.output ?? ''}` }
        : { passed: false, output: 'verifier result missing' };
      const aid = lastAttemptId || cur?.attempt_id;
      if (store && artifactStore?.configured && aid) {
        await recordArtifact(store, aid, 'test_results', () => artifactStore.putContent('tests',
          JSON.stringify({ attempt_id: aid, harness: 'verifier-inprocess', ...result }, null, 2)));
      }
      return result;
    },
  };
}

// ── 评审回执 → finding → 立案（真实契约：读 skill_receipt_outbox 形状的回执）──
export const FINDINGS_SCHEMA_SQL = [
  `CREATE TABLE IF NOT EXISTS fxv.findings (
     finding_id   TEXT PRIMARY KEY,
     repo         TEXT NOT NULL,
     branch       TEXT NOT NULL,
     head_sha     TEXT NOT NULL,
     file         TEXT NOT NULL,
     pattern      TEXT NOT NULL,
     replacement  TEXT NOT NULL,
     source_receipt JSONB,
     created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
   )`,
];

export function ruleDigest(head, file, pattern, replacement) {
  return crypto.createHash('sha256').update(`${head}|${file}|${pattern}|${replacement}`).digest('hex');
}

// 从评审回执（skill_receipt_outbox 载荷契约）落地 finding；幂等
export async function ingestReceipt(pool, receipt) {
  // receipt 契约：{repo, pr, head_sha, run_id, finding:{file, pattern, replacement}, integrity}
  for (const k of ['repo', 'head_sha']) if (!receipt?.[k]) throw new Error(`ingestReceipt: missing ${k}`);
  const f = receipt.finding ?? {};
  if (!f.file || !f.pattern || !f.replacement) throw new Error('ingestReceipt: finding.{file,pattern,replacement} required');
  const finding_id = 'fn-' + crypto.createHash('sha256')
    .update(`${receipt.repo}|${receipt.head_sha}|${f.file}|${f.pattern}`).digest('hex').slice(0, 16);
  await pool.query(
    `INSERT INTO fxv.findings (finding_id, repo, branch, head_sha, file, pattern, replacement, source_receipt)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (finding_id) DO NOTHING`,
    [finding_id, receipt.repo, receipt.branch || 'main', receipt.head_sha, f.file, f.pattern, f.replacement,
     JSON.stringify({ run_id: receipt.run_id ?? null, integrity: receipt.integrity ?? null })]);
  return { finding_id, branch: receipt.branch || 'main', file: f.file, pattern: f.pattern, replacement: f.replacement };
}

// finding → FXV 立案（绑定 rule@head 摘要；审批前状态）
export async function fileAttemptFromFinding(store, cfg, findingRow, { actor = 'fxv-ingest' } = {}) {
  const attempt_id = 'att-' + crypto.randomBytes(8).toString('hex');
  const ticket_id = 'tkt-' + crypto.createHash('sha256').update(findingRow.finding_id).digest('hex').slice(0, 16);
  const r = await store.fileAttempt({
    attempt_id, ticket_id, finding_id: findingRow.finding_id,
    repo: findingRow.repo, branch: findingRow.branch, base_head_sha: findingRow.head_sha,
    patch_digest: ruleDigest(findingRow.head_sha, findingRow.file, findingRow.pattern, findingRow.replacement),
    receipt_id: findingRow.source_receipt?.run_id ?? null,
    approval_ttl_ms: cfg.timeouts.approval_ttl_ms, actor,
    reason: 'filed from review receipt',
  });
  // 立案行带修复规则（exec handlers 需要）——存 state_detail
  await store.compareAndSetState(attempt_id, 'FILED', 'FILED', {
    rule_file: findingRow.file, rule_pattern: findingRow.pattern, rule_replacement: findingRow.replacement,
  });
  return r.attempt;
}
