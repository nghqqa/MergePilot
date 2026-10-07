// preflight-ref.test.mjs — 镜像引用解析/本地精确匹配 单元测试（纯函数，零依赖）。
// 场景锁定 PR 修复：rc.17 digest + 本机残留 rc.16 tag 不误报；多版本共存只取精确引用；
// 缺失引用 / 不可解析引用 不猜测版本。
import test from 'node:test';
import assert from 'node:assert/strict';
import { parseImageRef, pickLocalImage } from './preflight.mjs';

const RC17 = 'ghcr.io/nghqqa/mergepilot-console@sha256:112284f104001ac5c94b7d6c64ed2b6c854b52ec1c96dee275268bd7ee0e380a';
const RC16_TAG = 'ghcr.io/nghqqa/mergepilot-console:v0.2.0-beta.6-rc.16';
const RC17_TAG = 'ghcr.io/nghqqa/mergepilot-console:v0.2.0-beta.6-rc.17';

const ROW = (id, repo, tag, digest) => ({ id, repo, tag, digest });

test('parseImageRef：digest 引用', () => {
  const r = parseImageRef(RC17);
  assert.equal(r.kind, 'digest');
  assert.equal(r.repo, 'ghcr.io/nghqqa/mergepilot-console');
  assert.equal(r.digest, 'sha256:112284f104001ac5c94b7d6c64ed2b6c854b52ec1c96dee275268bd7ee0e380a');
});

test('parseImageRef：精确 tag 引用', () => {
  const r = parseImageRef(RC17_TAG);
  assert.equal(r.kind, 'tag');
  assert.equal(r.tag, 'v0.2.0-beta.6-rc.17');
});

test('parseImageRef：缺失引用 → none', () => {
  assert.equal(parseImageRef('').kind, 'none');
  assert.equal(parseImageRef(null).kind, 'none');
});

test('parseImageRef：非法 digest → invalid（不猜测）', () => {
  const r = parseImageRef('ghcr.io/nghqqa/mergepilot-console@sha256:zzzz');
  assert.equal(r.kind, 'invalid');
});

test('parseImageRef：repo-only（无 tag 无 digest）→ ambiguous（不猜测）', () => {
  const r = parseImageRef('mergepilot-console');
  assert.equal(r.kind, 'ambiguous');
});

test('pickLocalImage：rc.17 digest + 本机残留 rc.16 tag → 只取 digest 行', () => {
  const rows = [
    ROW('b1c95275750b', 'ghcr.io/nghqqa/mergepilot-console', 'v0.2.0-beta.6-rc.16', 'sha256:b1c95275750b3b7f6c836235e09659bff6e575aa93d5f041350b9aebe4bfbb0a'),
    ROW('112284f10400', 'ghcr.io/nghqqa/mergepilot-console', '<none>', 'sha256:112284f104001ac5c94b7d6c64ed2b6c854b52ec1c96dee275268bd7ee0e380a'),
  ];
  const hit = pickLocalImage(rows, RC17);
  assert.ok(hit, '应命中 digest 行');
  assert.equal(hit.id, '112284f10400');
});

test('pickLocalImage：rc.17 tag 无残留旧 tag → 精确命中', () => {
  const rows = [ROW('112284f10400', 'ghcr.io/nghqqa/mergepilot-console', 'v0.2.0-beta.6-rc.17', 'sha256:112284f104001ac5c94b7d6c64ed2b6c854b52ec1c96dee275268bd7ee0e380a')];
  const hit = pickLocalImage(rows, RC17_TAG);
  assert.ok(hit);
  assert.equal(hit.id, '112284f10400');
});

test('pickLocalImage：本地无该引用 → null（不猜版本）', () => {
  const rows = [ROW('b1c95275750b', 'ghcr.io/nghqqa/mergepilot-console', 'v0.2.0-beta.6-rc.16', 'sha256:b1c95275750b3b7f6c836235e09659bff6e575aa93d5f041350b9aebe4bfbb0a')];
  assert.equal(pickLocalImage(rows, RC17_TAG), null);
});

test('pickLocalImage：多版本共存 → 只取精确引用（rc.17 tag ≠ rc.16 tag）', () => {
  const rows = [
    ROW('aaa', 'ghcr.io/nghqqa/mergepilot-console', 'v0.2.0-beta.6-rc.16', 'sha256:aaa'),
    ROW('bbb', 'ghcr.io/nghqqa/mergepilot-console', 'v0.2.0-beta.6-rc.17', 'sha256:bbb'),
  ];
  const hit = pickLocalImage(rows, RC17_TAG);
  assert.ok(hit);
  assert.equal(hit.id, 'bbb');
});

test('pickLocalImage：引用缺失 → null（compose 未声明时跳过探测）', () => {
  assert.equal(pickLocalImage([{ id: 'x', repo: 'ghcr.io/nghqqa/mergepilot-console', tag: 'v0.2.0-beta.6-rc.17', digest: null }], ''), null);
});
