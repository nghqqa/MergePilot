// console/backend/test/pack.test.mjs — pack 访问层安全与完整性测试

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { safeResolve, listPackFiles, parseSha256Sums, verifyPack, listRunPacks, looksTextual } from '../lib/pack.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES = path.join(__dirname, 'fixtures');
const WH = path.join(FIXTURES, 'SAMPLE-RUN-WH');

test('listRunPacks 只索引带 run 锚点的目录', () => {
  const packs = listRunPacks(FIXTURES);
  const ids = packs.map((p) => p.pack_id).sort();
  assert.deepEqual(ids, ['SAMPLE-RUN-MATRIX', 'SAMPLE-RUN-WH']);
});

test('safeResolve 接受包内相对路径', () => {
  const abs = safeResolve(WH, 'project/result.md');
  assert.ok(abs.startsWith(path.resolve(WH)));
  const abs2 = safeResolve(WH, 'tasks/gh-pr9-aabbcc-review-1/meta.json');
  assert.ok(abs2.endsWith(path.join('tasks', 'gh-pr9-aabbcc-review-1', 'meta.json')));
});

test('safeResolve 拒绝路径穿越（.. / 绝对路径 / 盘符 / 反斜杠 / 空值）', () => {
  const bad = ['../other/secret', '..\\..\\secret', '/etc/passwd', 'C:\\Windows\\win.ini', '', null, undefined, 'a/../../b'];
  for (const p of bad) {
    assert.throws(() => safeResolve(WH, p), (e) => e.status === 400, `must reject: ${p}`);
  }
});

test('listPackFiles 列出全部文件且包含 SUMS 标记外的未列文件', () => {
  const files = listPackFiles(WH);
  const paths = files.map((f) => f.path);
  assert.ok(paths.includes('project/result.md'));
  assert.ok(paths.includes('extra-unlisted.txt'));
  assert.ok(files.every((f) => Number.isInteger(f.bytes) && f.bytes >= 0));
});

test('parseSha256Sums 解析锁定清单', () => {
  const sums = parseSha256Sums(WH);
  assert.ok(sums, 'fixture has SHA256SUMS');
  assert.equal(sums.size, 6);
  assert.ok(sums.has('project/result.md'));
  assert.ok(/^[0-9a-f]{64}$/.test(sums.get('project/result.md')));
});

test('verifyPack：完整包 → verified，未列文件计数', async () => {
  const r = await verifyPack('SAMPLE-RUN-WH', WH);
  assert.equal(r.status, 'verified');
  assert.equal(r.listed, 6);
  assert.equal(r.verified, 6);
  assert.equal(r.mismatched.length, 0);
  assert.equal(r.unlisted_count, 2); // SHA256SUMS 自身 + extra-unlisted.txt
});

test('verifyPack：篡改内容 → mismatch（临时副本）', async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'mp-pack-'));
  fs.cpSync(WH, tmp, { recursive: true });
  const target = path.join(tmp, 'project', 'result.md');
  fs.appendFileSync(target, '\ntampered\n');
  const r = await verifyPack('TAMPER-TEST', tmp);
  assert.equal(r.status, 'mismatch');
  assert.ok(r.mismatched.some((m) => m.path === 'project/result.md'));
  fs.rmSync(tmp, { recursive: true, force: true });
});

test('looksTextual 区分文本与二进制', () => {
  assert.equal(looksTextual(Buffer.from('hello 文本')), true);
  assert.equal(looksTextual(Buffer.from([0x00, 0x01, 0x02, 0xff])), false);
});
