// console/backend/test/ragtrial-unit.test.mjs — RAG trial 纯函数单测（无 PG）。
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  LOCAL_MODEL_ID, localModelSpec, modelDigest, tokenize, embedLocal,
  vectorLiteral, canonicalJson,
} from '../lib/ragtrial/embed.mjs';
import { cleanText, chunkDocument, dedupChunks, prepareDocument } from '../lib/ragtrial/ingest.mjs';

test('model spec digest 稳定且规格敏感', () => {
  const s1 = localModelSpec();
  const s2 = localModelSpec();
  assert.equal(modelDigest(s1), modelDigest(s2), '同规格同 digest');
  const mutated = { ...s2, dims: 128 };
  assert.notEqual(modelDigest(mutated), modelDigest(s1), 'dims 变化必须改变 digest');
  const mutated2 = { ...s2, tokenization: { ...s2.tokenization, latin: 'word_lower_min3' } };
  assert.notEqual(modelDigest(mutated2), modelDigest(s1), '分词参数变化必须改变 digest');
});

test('canonicalJson 键序无关', () => {
  assert.equal(canonicalJson({ a: 1, b: 2 }), canonicalJson({ b: 2, a: 1 }));
});

test('tokenize：拉丁词 + CJK 连续段二元组（孤立单字才出单字）', () => {
  const t = tokenize('Hello world 你好世界');
  assert.ok(t.includes('hello'));
  assert.ok(t.includes('world'));
  assert.ok(t.includes('你好'));
  assert.ok(t.includes('好世'));
  assert.ok(t.includes('世界'));
  assert.ok(!t.includes('你'), '连续段不出孤立单字');
  assert.ok(!t.some((x) => x.length === 1 && /^[a-z]$/.test(x)), '单字母拉丁 token 剔除');
  const iso = tokenize('好 吗');
  assert.ok(iso.includes('好') && iso.includes('吗'), '孤立 CJK 单字保留');
  const t2 = tokenize('探测 与 测试');
  assert.ok(!t2.includes('测'), '探测/测试 不产生共享单字 token');
});

test('embedLocal 确定性 + 归一 + 无 NaN', () => {
  const a = embedLocal('检索审计证据链');
  const b = embedLocal('检索审计证据链');
  assert.deepEqual(a, b, '同文本同向量');
  const norm = Math.sqrt(a.reduce((s, v) => s + v * v, 0));
  assert.ok(Math.abs(norm - 1) < 1e-4, `L2 归一（got ${norm}）`);
  assert.ok(a.every(Number.isFinite), '无 NaN/Infinity');
  assert.equal(a.length, 256);
});

test('embedLocal 空输入走 epsilon 占位（防 pgvector 零向量 NaN）', () => {
  const v = embedLocal('!!!???');
  assert.ok(v.every(Number.isFinite));
  const norm = Math.sqrt(v.reduce((s, x) => s + x * x, 0));
  assert.ok(norm > 0, '零向量被 epsilon 替代');
});

test('vectorLiteral 拒绝 NaN', () => {
  assert.throws(() => vectorLiteral([0, NaN, 1]));
  assert.equal(vectorLiteral([0.5, -0.5]), '[0.5,-0.5]');
});

test('cleanText：CRLF 归一 + 空行折叠，不删正文', () => {
  const r = cleanText('line1\r\nline2\r\n\r\n\r\n\r\nline3  \n');
  assert.equal(r.text, 'line1\nline2\n\nline3');
  assert.ok(r.notes.includes('blank_lines_folded'));
});

test('chunkDocument：行号闭区间正确回溯原文', () => {
  const text = ['para one line A', 'para one line B', '', 'para two line C', '', 'para three line D'].join('\n');
  const chunks = chunkDocument(text, { target: 30, max: 80, min: 10 });
  assert.ok(chunks.length >= 2);
  for (const c of chunks) {
    const lines = text.split('\n').slice(c.line_start - 1, c.line_end);
    assert.equal(c.text, lines.join('\n'),
      `chunk ${c.chunk_index} 行号 ${c.line_start}-${c.line_end} 必须精确对应原文`);
  }
});

test('chunkDocument：超长段落二次切分行号仍精确', () => {
  const longPara = Array.from({ length: 60 }, (_, i) => `sentence ${i} with some words`).join(' ');
  const text = longPara + '\n\ntail';
  const chunks = chunkDocument(text, { target: 200, max: 300, min: 40 });
  assert.ok(chunks.length >= 2, '超长段必须被切分');
  for (const c of chunks) {
    const lines = text.split('\n').slice(c.line_start - 1, c.line_end);
    assert.equal(c.text, lines.join('\n'));
  }
});

test('dedupChunks：文档内精确去重并重排 index', () => {
  const dupText = ['same content', '', 'same content', '', 'other content'].join('\n');
  const chunks = chunkDocument(dupText, { target: 20, max: 100, min: 5 });
  const { unique, duplicates } = dedupChunks(chunks);
  assert.equal(duplicates.length, 1, '检测到 1 个重复块');
  assert.equal(unique.length, chunks.length - 1);
  assert.deepEqual(unique.map((c) => c.chunk_index), unique.map((_, i) => i), 'index 重排连续');
});

test('prepareDocument：全绑定字段齐备（引用链完整）', async () => {
  const doc = await prepareDocument(
    { repo: 'mergepilot', branch: 'feat/local-rag-trial', doc_path: 'docs/a.md', text: '内容一\n\n内容二\n\n内容三' },
    (texts) => texts.map((t) => embedLocal(t)),
  );
  assert.equal(doc.repo, 'mergepilot');
  assert.equal(doc.doc_sha256.length, 64);
  assert.ok(doc.chunks.length >= 1);
  for (const c of doc.chunks) {
    for (const f of ['chunk_index', 'para_index', 'line_start', 'line_end', 'chunk_sha256', 'text', 'embedding']) {
      assert.ok(c[f] !== undefined && c[f] !== null, `chunk 缺字段 ${f}`);
    }
    assert.equal(c.embedding.length, 256);
  }
});

test('prepareDocument：空文档（清洗后无内容）产出 0 chunk 不崩', async () => {
  const doc = await prepareDocument(
    { repo: 'r', branch: 'b', doc_path: 'empty.md', text: '\n\n\n' },
    (ts) => ts.map(embedLocal),
  );
  assert.equal(doc.chunks.length, 0);
  assert.equal(doc.stats.chunks, 0);
});
