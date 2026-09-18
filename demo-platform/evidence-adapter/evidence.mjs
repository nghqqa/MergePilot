// evidence-adapter/evidence.mjs
// Read-only loader over the locked evidence directories. Never writes.
// Computes SHA256SUMS integrity at load time so the UI can show live verification.

import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export const EVIDENCE_ROOT =
  process.env.DEMO_EVIDENCE_ROOT || path.resolve(__dirname, '..', '..', 'evidence');

export const EVIDENCE_DIRS = {
  replayMaterials: 'PHASE14-WINDOWS-REPLAY-MATERIALS-20260829-195223',
  fixAudit: 'PHASE14-WINDOWS-COPAW-HIGH-RISK-FIX-AUDIT-20260829-165813',
  fixVerify: 'PHASE14-WINDOWS-COPAW-HIGH-RISK-FIX-VERIFY-20260829-180548',
  finalLock: 'PHASE14-WINDOWS-AGENTTEAMS-COPAW-FINAL-20260829-130500',
  agentloopOtel: 'PHASE14-WINDOWS-AGENTLOOP-OTEL-20260829-200307',
  credRotation: 'PHASE14-WINDOWS-CREDENTIAL-ROTATION-20260829-130500',
  e2eFull: 'PHASE14-WINDOWS-COPAW-E2E-FULL-20260829-122526',
  promotionFinal: 'PHASE14-WINDOWS-COPAW-PROMOTION-A-FINAL-20260829-124400',
  matrixSyncAudit: 'PHASE14-WINDOWS-COPAW-MATRIX-SYNC-AUDIT-20260829-164840',
  highRiskGateBlocked: 'PHASE14-WINDOWS-COPAW-HIGH-RISK-HUMAN-GATE-20260829-144234',
  rejectDemo: 'PHASE14-WINDOWS-COPAW-HIGH-RISK-REJECT-20260830-091913',
  // Finals (2026-09-14) evidence. Deliberately NOT part of INTEGRITY_KEYS: the
  // historical "final package VERIFIED" split stays exactly what it was; these
  // dirs are verified separately by lib/finals_evidence.mjs and carry their own
  // evidence tier (real SQL loop / controller mechanism / offline RAG loop).
  finalsDbLoop: 'FINALS-DB-MIGRATION-LOOP-20260914',
  finalsReworkLoop: 'FINALS-REWORK-LOOP-20260914',
  finalsRagLoop: 'FINALS-RAG-LOOP-20260914',
  // Finals live AgentTeams run (2026-09-16): real PR #2 case executed on the
  // elemiso isolated stack with CoPaw Leader orchestration. Has its own
  // SHA256SUMS; verified separately like the other FINALS dirs.
  finalsPr2Live: 'FINALS-ELEM-PR2-LIVE-20260916',
  // Finals live AgentTeams run (2026-09-16): real PR #3 human-REJECT case on
  // the same stack/team; second run of the day.
  finalsPr3Live: 'FINALS-ELEM-PR3-LIVE-20260916',
  // R2 clean re-run of the PR #2 case (same day, post-audit corrections).
  finalsPr2R2Live: 'FINALS-ELEM-PR2-LIVE-20260916-R2',
  // 2026-09-17 RAG-TRACED round (v2.2 instrumentation). Retired from the demo
  // lineup by the V3 round below; kept resolvable as prior-round evidence.
  finalsPr2RagTraced: 'FINALS-ELEM-PR2-RAG-TRACED',
  finalsPr3RagTraced: 'FINALS-ELEM-PR3-RAG-TRACED',
  // 2026-09-17 V3-TRACED round: AgentLoop instrumentation v3 (single-layer
  // genai.llm.call, loongsuite ExecuteTool semantics, gen_ai.conversation.id,
  // agentteams.delegation.link cross-agent linking) + RAG MCP hook, image
  // copaw-worker:223ddc2-agentloop-v3rag (8e17c3667c3c). These are the two demo
  // cases (pr1 retired); both dirs are SHA256SUMS-locked.
  finalsPr2Sk3Traced: 'FINALS-ELEM-PR2-SK3-TRACED',
  finalsPr3Sk3Traced: 'FINALS-ELEM-PR3-SK3-TRACED',
};

export function dirPath(key) {
  return path.join(EVIDENCE_ROOT, EVIDENCE_DIRS[key] ?? key);
}

export function exists(relKey, file = '') {
  try {
    fs.accessSync(path.join(dirPath(relKey), file));
    return true;
  } catch {
    return false;
  }
}

export function readText(relKey, file) {
  return fs.readFileSync(path.join(dirPath(relKey), file), 'utf8');
}

export function readJson(relKey, file) {
  return JSON.parse(readText(relKey, file));
}

export function sha256File(absPath) {
  const buf = fs.readFileSync(absPath);
  return crypto.createHash('sha256').update(buf).digest('hex');
}

// Parse a SHA256SUMS file (format: "<hex> *./<file>")
function parseSums(relKey) {
  const sumsFile = path.join(dirPath(relKey), 'SHA256SUMS');
  if (!fs.existsSync(sumsFile)) return null;
  const entries = [];
  for (const line of fs.readFileSync(sumsFile, 'utf8').split(/\r?\n/)) {
    const m = line.match(/^([0-9a-f]{64})\s+\*?\.?\/?(.+?)\s*$/i);
    if (m) entries.push({ sha256: m[1].toLowerCase(), file: m[2].replace(/\\/g, '/') });
  }
  return entries;
}

// Recompute hashes for one evidence dir; returns drift report.
export function verifyDirIntegrity(relKey) {
  const entries = parseSums(relKey);
  if (!entries) {
    return { dir: EVIDENCE_DIRS[relKey] ?? relKey, sha256sums: false, files: 0, ok: 0, mismatched: [], missing: [] };
  }
  let ok = 0;
  const mismatched = [];
  const missing = [];
  for (const e of entries) {
    const abs = path.join(dirPath(relKey), e.file);
    try {
      const actual = sha256File(abs);
      if (actual === e.sha256) ok += 1;
      else mismatched.push(e.file);
    } catch {
      missing.push(e.file);
    }
  }
  return {
    dir: EVIDENCE_DIRS[relKey] ?? relKey,
    sha256sums: true,
    files: entries.length,
    ok,
    mismatched,
    missing,
    verified: ok === entries.length && entries.length > 0,
  };
}

const INTEGRITY_KEYS = [
  'replayMaterials',
  'fixAudit',
  'fixVerify',
  'finalLock',
  'agentloopOtel',
  'credRotation',
  'e2eFull',
  'promotionFinal',
  'rejectDemo',
];

// TRUTHFULNESS-FIX: distinguish the historical materials pack from the final
// locked submission package so the UI can show both honestly.
const HISTORICAL_PACKAGE_KEYS = ['replayMaterials'];
const FINAL_PACKAGE_KEYS = ['finalLock', 'fixAudit', 'fixVerify', 'agentloopOtel', 'credRotation', 'e2eFull', 'promotionFinal', 'rejectDemo'];

let integrityCache = null;
export function integrityReport({ refresh = false } = {}) {
  if (integrityCache && !refresh) return integrityCache;
  integrityCache = { checked_at: new Date().toISOString(), dirs: {} };
  let total = 0;
  let ok = 0;
  for (const key of INTEGRITY_KEYS) {
    try {
      const r = verifyDirIntegrity(key);
      integrityCache.dirs[key] = r;
      if (r.sha256sums) {
        total += r.files;
        ok += r.ok;
      }
    } catch (e) {
      integrityCache.dirs[key] = { dir: EVIDENCE_DIRS[key], error: String(e && e.message) };
    }
  }
  integrityCache.total_files = total;
  integrityCache.ok_files = ok;
  integrityCache.all_ok = ok === total && total > 0;
  return integrityCache;
}

// Split integrity report: historical materials vs final locked submission package.
export function integritySplit() {
  const full = integrityReport();
  const summarize = (keys) => {
    const dirs = {};
    let files = 0;
    let ok = 0;
    const drift = [];
    const missing = [];
    for (const key of keys) {
      const d = full.dirs[key];
      if (!d || d.error) continue;
      dirs[key] = d;
      if (d.sha256sums) {
        files += d.files;
        ok += d.ok;
        for (const f of d.mismatched ?? []) drift.push({ dir: d.dir, file: f });
        for (const f of d.missing ?? []) missing.push({ dir: d.dir, file: f });
      }
    }
    return {
      files,
      ok,
      all_ok: ok === files && files > 0 && missing.length === 0,
      drift,
      missing,
      dirs,
      status: missing.length > 0 ? 'BROKEN' : drift.length > 0 ? 'DRIFT_DOCUMENTED' : 'VERIFIED',
    };
  };
  const historical = summarize(HISTORICAL_PACKAGE_KEYS);
  const final = summarize(FINAL_PACKAGE_KEYS);
  return {
    checked_at: full.checked_at,
    historical_source_integrity: {
      ...historical,
      label: '历史材料目录（replay materials pack）',
      note: historical.drift.length ? 'SHA256SUMS 锁定后文件被外部更新；平台只读，不修改历史目录，SUMS 待证据包维护方重生成' : '',
    },
    final_submission_integrity: {
      ...final,
      label: '最终提交包（final locked submission package）',
    },
  };
}

// Artifact registry with hashes, built from SHA256SUMS of the replay-materials pack.
export function artifactRegistry() {
  const out = [];
  for (const key of ['replayMaterials', 'fixVerify', 'finalLock']) {
    const entries = parseSums(key);
    if (!entries) continue;
    for (const e of entries) {
      out.push({
        dir: EVIDENCE_DIRS[key],
        file: e.file,
        sha256: e.sha256,
        abs_path: path.join(dirPath(key), e.file),
        exists: fs.existsSync(path.join(dirPath(key), e.file)),
      });
    }
  }
  return out;
}

// Extract probe result pairs from the REAL raw probe outputs in evidence.
export function loadProbeComparison() {
  const before = readText('fixVerify', 'artifacts/verify-1.before_probe_raw.txt');
  const after = readText('fixVerify', 'artifacts/verify-1.after_probe_raw.txt');
  const parseProbe = (txt) => {
    const statuses = txt
      .split(/\r?\n/)
      .filter((l) => /status:\s*(\d{3})/i.test(l))
      .map((l) => Number(l.match(/status:\s*(\d{3})/i)[1]));
    return {
      traversal_status: statuses[0] ?? null,
      legit_status: statuses[1] ?? null,
      leaked_outside_secret: /LEAKED_OUTSIDE_SECRET:\s*True/i.test(txt),
      legit_served: /legit[\s\S]{0,200}?status:\s*200/i.test(txt) || statuses[1] === 200,
    };
  };
  return {
    source: {
      before: `${EVIDENCE_DIRS.fixVerify}/artifacts/verify-1.before_probe_raw.txt`,
      after: `${EVIDENCE_DIRS.fixVerify}/artifacts/verify-1.after_probe_raw.txt`,
      runner: 'p14h2-copaw-worker-verifier（独立自设计探针）',
    },
    request_traversal: 'GET /demo/download?name=../outside/outside-secret.txt',
    request_legit: 'GET /demo/download?name=welcome.txt',
    before: parseProbe(before),
    after: parseProbe(after),
    raw: { before, after },
  };
}

// Every replay event references source files; validate they all exist (replay integrity).
export function assertSourcesExist(refs) {
  const missing = [];
  for (const ref of refs) {
    // ref head: path up to first space/#/（ annotation; dir part may be a key or full dir name
    const head = ref.split(/[\s#（]/)[0];
    if (!head.includes('/')) continue;
    const first = head.split('/')[0];
    const rest = head.slice(first.length + 1);
    const dirName = EVIDENCE_DIRS[first] ?? first;
    const abs = path.join(EVIDENCE_ROOT, dirName, rest);
    if (!fs.existsSync(abs)) missing.push(ref);
  }
  return missing;
}

// Resolve a source_ref ("DIRKEY/path…" or "FULLDIRNAME/path…") to a real evidence
// file and verify it against that directory's SHA256SUMS (live recomputation).
export function resolveSourceRef(ref) {
  if (!ref) return null;
  const head = ref.split(/[\s#（]/)[0];
  if (!head.includes('/')) {
    return { ref, dir: null, file: head, exists: false, note: 'ref 不含文件路径（描述性引用）' };
  }
  const first = head.split('/')[0];
  const rest = head.slice(first.length + 1);
  const dirName = EVIDENCE_DIRS[first] ?? first;
  const dirKey = EVIDENCE_DIRS[first] ? first : null;
  const abs = path.join(EVIDENCE_ROOT, dirName, rest);
  const out = {
    ref,
    dir: dirName,
    dir_key: dirKey,
    file: rest,
    abs_path: abs,
    exists: fs.existsSync(abs) && fs.statSync(abs).isFile(),
    evidence_root_readonly: true,
  };
  if (out.exists) {
    const actual = sha256File(abs);
    out.sha256_actual = actual;
    // check against the dir's SHA256SUMS when present
    const entries = parseSums(dirKey ?? dirName);
    const hit = entries ? entries.find((e) => e.file.replace(/\\/g, '/') === rest.replace(/\\/g, '/')) : null;
    out.sha256_expected = hit ? hit.sha256 : null;
    out.hash_verified = hit ? hit.sha256 === actual : null; // null = no SUMS entry for this file
    out.sums_source = `${dirName}/SHA256SUMS`;
  } else {
    out.hash_verified = null;
  }
  return out;
}

// Redaction status for a payload (used by the evidence drawer).
export function redactionStatusOf(text, { scanForSecrets }) {
  const hits = scanForSecrets(typeof text === 'string' ? text : JSON.stringify(text));
  return {
    redaction_layer: 'applied (evidence-adapter/redact.mjs)',
    scan_hits: hits,
    status: hits.length === 0 ? 'CLEAN — redaction applied; PR#2 demo placeholders allow-listed' : 'REVIEW REQUIRED',
    forbidden_fields_excluded: ['Authorization', 'Cookie', 'API key', 'Matrix token', 'password', '完整工具参数', '完整 prompt/completion'],
  };
}
