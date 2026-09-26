// console/backend/test/api.test.mjs — HTTP 层测试（临时端口真实起服）

import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createConsole } from '../server.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES = path.join(__dirname, 'fixtures');
const REPO_ROOT = path.resolve(__dirname, '..', '..', '..');
const REAL_EVIDENCE = path.join(REPO_ROOT, 'evidence');

async function withServer(evidenceRoot, fn) {
  const { server } = createConsole({ evidenceRoot, distDir: path.join(__dirname, 'no-dist') });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const { port } = server.address();
  const base = `http://127.0.0.1:${port}`;
  try {
    await fn(base);
  } finally {
    await new Promise((r) => server.close(r));
  }
}

test('GET /api/health：数据模式 snapshot、live 未接入如实标注', async () => {
  await withServer(FIXTURES, async (base) => {
    const res = await fetch(`${base}/api/health`);
    assert.equal(res.status, 200);
    const j = await res.json();
    assert.equal(j.ok, true);
    assert.equal(j.data_mode, 'snapshot');
    assert.equal(j.live.configured, false);
    assert.equal(j.runs, 2);
  });
});

test('GET /api/runs：列表含 fixture 两条，可按 repo 过滤', async () => {
  await withServer(FIXTURES, async (base) => {
    const res = await fetch(`${base}/api/runs`);
    assert.equal(res.status, 200);
    const j = await res.json();
    assert.equal(j.total, 2);
    assert.ok(j.items.every((r) => r.data_mode !== undefined || r.pack_id));

    const filtered = await (await fetch(`${base}/api/runs?repo=fastapi-boilerplate`)).json();
    assert.equal(filtered.total, 1);
    assert.equal(filtered.items[0].pack_id, 'SAMPLE-RUN-WH');
  });
});

test('GET /api/runs/:id：detail 返回 timeline 与归属字段', async () => {
  await withServer(FIXTURES, async (base) => {
    const res = await fetch(`${base}/api/runs/SAMPLE-RUN-WH`);
    assert.equal(res.status, 200);
    const j = await res.json();
    assert.equal(j.pack_id, 'SAMPLE-RUN-WH');
    assert.ok(Array.isArray(j.timeline) && j.timeline.length > 0);
    assert.ok(Array.isArray(j.tasks));
    assert.ok(j.head_sha && j.run_id);
  });
  await withServer(FIXTURES, async (base) => {
    const res = await fetch(`${base}/api/runs/NO-SUCH-PACK`);
    assert.equal(res.status, 404);
    const j = await res.json();
    assert.ok(j.error);
  });
});

test('evidence 列表/内容/下载：合法路径可读，非法路径 400，未知文件 404', async () => {
  await withServer(FIXTURES, async (base) => {
    const list = await (await fetch(`${base}/api/runs/SAMPLE-RUN-WH/evidence`)).json();
    const paths = list.items.map((f) => f.path);
    assert.ok(paths.includes('project/result.md'));
    const listed = list.items.find((f) => f.path === 'project/result.md');
    assert.equal(listed.sums_status, 'listed');
    const unlisted = list.items.find((f) => f.path === 'extra-unlisted.txt');
    assert.equal(unlisted.sums_status, 'unlisted');

    const content = await (await fetch(`${base}/api/runs/SAMPLE-RUN-WH/evidence/content?path=project/result.md`)).json();
    assert.equal(content.encoding, 'utf-8');
    assert.match(content.text, /Project Result/);
    assert.equal(content.sums_status, 'listed');

    const traversal = await fetch(`${base}/api/runs/SAMPLE-RUN-WH/evidence/content?path=../SAMPLE-RUN-MATRIX/kickoff.json`);
    assert.equal(traversal.status, 400);

    const encoded = await fetch(`${base}/api/runs/SAMPLE-RUN-WH/evidence/content?path=${encodeURIComponent('..\\..\\..\\secrets')}`);
    assert.equal(encoded.status, 400);

    const missing = await fetch(`${base}/api/runs/SAMPLE-RUN-WH/evidence/content?path=no-such-file.md`);
    assert.equal(missing.status, 404);

    const dl = await fetch(`${base}/api/runs/SAMPLE-RUN-WH/evidence/download?path=project/result.md`);
    assert.equal(dl.status, 200);
    assert.match(dl.headers.get('content-disposition'), /attachment/);
    assert.equal(dl.headers.get('content-type'), 'application/octet-stream');
    const body = await dl.text();
    assert.match(body, /Project Result/);
  });
});

test('integrity 端点：fixture 校验通过', async () => {
  await withServer(FIXTURES, async (base) => {
    const res = await fetch(`${base}/api/runs/SAMPLE-RUN-WH/integrity`);
    assert.equal(res.status, 200);
    const j = await res.json();
    assert.equal(j.status, 'verified');
    assert.equal(j.mismatched.length, 0);
  });
});

test('真实证据根冒烟：/api/runs ≥15 条且含 WH 轮（空根显式 SKIP，不冒充通过）', { skip: !fs.existsSync(REAL_EVIDENCE) }, async (t) => {
  await withServer(REAL_EVIDENCE, async (base) => {
    const j = await (await fetch(`${base}/api/runs?limit=200`)).json();
    if (!j.total) {
      // 证据根存在但为空（如 worktree 中 evidence 被迁移清理）：显式跳过而非失败/伪造
      t.skip(`evidence root present but empty: ${REAL_EVIDENCE}`);
      return;
    }
    assert.ok(j.total >= 15, `expected >=15 runs, got ${j.total}`);
    const wh = j.items.find((r) => r.pack_id === 'FINALS-ELEM-PR1-WH-20260919');
    assert.ok(wh, 'WH run present');
    assert.equal(wh.publish.status, 'published');
    const detail = await (await fetch(`${base}/api/runs/FINALS-ELEM-PR1-WH-20260919`)).json();
    assert.equal(detail.run_id, 'run-gh-pr1-575aa8e1-093022');
    assert.ok(detail.timeline.length >= 8);
  });
});
