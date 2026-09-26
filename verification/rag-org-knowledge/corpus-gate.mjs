// corpus-gate.mjs — A 链语料受控门禁 + 内容寻址。
// snapshot_id = 内容寻址（canonical 语料 sha256 前 32 hex）；
// 禁含内容 screening：仓库事实（repo 名/run id/SHA）、历史案例引用、
// 凭证/敏感数据形态。任一命中即拒绝装载（fail-closed）。
import { createHash } from 'node:crypto';
import fs from 'node:fs';

export const RETRIEVAL_VERSION = 'lexical-zh-en-v1';

export function canonicalCorpus(corpus) {
  // 稳定规范化：文档按 doc_id 排序、chunks 原序、键序固定
  const norm = {
    corpus_name: corpus.corpus_name,
    corpus_version: corpus.corpus_version,
    data_mode: corpus.data_mode,
    publication_status: corpus.publication_status,
    documents: [...corpus.documents]
      .sort((a, b) => a.doc_id.localeCompare(b.doc_id))
      .map((d) => ({ doc_id: d.doc_id, title: d.title, source_ref: d.source_ref, chunks: [...d.chunks] })),
  };
  return JSON.stringify(norm);
}

export function corpusDigest(corpus) {
  return createHash('sha256').update(canonicalCorpus(corpus), 'utf8').digest('hex');
}

export function snapshotId(corpus) {
  return `snap-${corpusDigest(corpus).slice(0, 32)}`;
}

// screening：语料不得包含仓库事实 / 历史案例 / 企业敏感数据
const FORBIDDEN = [
  { name: 'repo-fact', re: /\b(nghqqa|wookat|fastapi-boilerplate-demo|tizhou|speaktype)\b|run-canary\d*|gh-bridge|mergepilot\/|\b[0-9a-f]{40}\b/i },
  { name: 'secret-like', re: /(ghp_[A-Za-z0-9]{20,}|github_pat_|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|password\s*[:=]\s*\S{6,})/i },
  { name: 'personal-data', re: /\b[\w.+-]+@[\w-]+\.[a-z]{2,}\b|\b\d{17}[\dXx]\b|\b(身份证|手机号|工资)\b/ },
  { name: 'historical-case-claim', re: /历史案例|PR #\d+|案例 \d+|CVE-\d{4}-\d+/i },
];

export function screenCorpus(corpus) {
  const findings = [];
  const texts = [];
  for (const d of corpus.documents) {
    texts.push(d.title, d.source_ref, ...d.chunks);
  }
  for (const t of texts) {
    for (const f of FORBIDDEN) {
      if (f.re.test(t)) findings.push({ rule: f.name, sample: t.slice(0, 40) });
    }
  }
  return { ok: findings.length === 0, findings };
}

export function validateCorpusShape(corpus) {
  const problems = [];
  for (const k of ['corpus_name', 'corpus_version', 'data_mode', 'publication_status', 'documents']) {
    if (!(k in corpus)) problems.push(`missing field ${k}`);
  }
  for (const d of corpus.documents || []) {
    if (!d.doc_id || !d.title || !d.source_ref) problems.push(`document missing id/title/source_ref: ${d.doc_id}`);
    if (!Array.isArray(d.chunks) || !d.chunks.length) problems.push(`document has no chunks: ${d.doc_id}`);
    for (const c of d.chunks || []) {
      if (typeof c !== 'string' || c.length < 8) problems.push(`bad chunk in ${d.doc_id}`);
    }
  }
  return { ok: problems.length === 0, problems };
}

export function loadCorpus(path) {
  const raw = fs.readFileSync(path, 'utf8');
  const corpus = JSON.parse(raw);
  const shape = validateCorpusShape(corpus);
  const screen = screenCorpus(corpus);
  if (!shape.ok || !screen.ok) {
    const err = new Error(`CORPUS_REJECTED shape=${JSON.stringify(shape.problems)} screen=${JSON.stringify(screen.findings)}`);
    err.code = 'CORPUS_REJECTED';
    throw err;
  }
  return { corpus, digest: corpusDigest(corpus), snapshot_id: snapshotId(corpus),
           retrieval_version: RETRIEVAL_VERSION };
}
