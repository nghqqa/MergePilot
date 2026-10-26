// CVE-2026-111497（brotli 双面修复）回归断言：
// 1) 静态：Dockerfile 两个阶段都钉定含 node 22.23.4 的基础 digest，apk 升级行
//    以 --no-cache（每次最新索引）同时升级 zlib brotli-libs——浮动 tag 回退或
//    升级行被删即红。
// 2) 单测：判定函数 evaluateProbes 的语义——root 探针失败/输出缺失不得被判为
//    已修复；非 root 探测结果绝不参与判定；索引候选落后于已安装即 FAIL。
'use strict';
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const require = createRequire(import.meta.url);
const rootDir = join(dirname(fileURLToPath(import.meta.url)), '..'); // 仓库根 = src/（test/ 的上一级）
const dockerfile = readFileSync(join(rootDir, 'docker', 'Dockerfile.canonical-console'), 'utf8');
const verifier = require(join(rootDir, 'scripts', 'verify-image-security-fixes.cjs'));

const PIN = 'sha256:fe4f6d74e20370acb04ad889077d14d0f64a9c8ea6a42bc60cddf1e844699914';

test('BROTLI-1 Dockerfile 两个阶段都钉定含 node 22.23.4 的基础 digest（禁止浮动 tag）', () => {
  const fromLines = dockerfile.split('\n').filter((l) => l.startsWith('FROM '));
  assert.equal(fromLines.length, 2, `应有恰好两个 FROM：${fromLines.length}`);
  for (const line of fromLines) {
    assert.ok(line.includes(`node@${PIN}`), `阶段未钉定修复版基础 digest: ${line}`);
    assert.ok(!/^FROM node:22-alpine\b/m.test(line), '禁止浮动 tag 回退');
  }
});

test('BROTLI-2 apk 升级行以 --no-cache 同时升级 zlib brotli-libs', () => {
  const run = dockerfile.split('\n').filter((l) => l.startsWith('RUN apk '));
  assert.equal(run.length, 1, `应恰有一条 apk RUN：${run.length}`);
  assert.ok(run[0].includes('--no-cache'), '必须 --no-cache（每次拉取最新 APKINDEX）');
  assert.ok(/upgrade\s+zlib\s+brotli-libs/.test(run[0]), `必须同时升级 zlib brotli-libs: ${run[0]}`);
});

test('BROTLI-3 判定：全部达标 → ok=true；非 root 探测失败不影响判定', () => {
  const v = verifier.evaluateProbes({
    rootProbeOk: true,
    nodeVersion: 'v22.23.4',
    installedBrotli: '1.2.1-r0',
    candidateBrotli: '1.2.1-r0',
    nonRootProbeOk: false, // 非 root 探测失败属预期，不得拉低判定
  });
  assert.equal(v.ok, true, JSON.stringify(v.checks));
});

test('BROTLI-4 判定：node 22.23.3（未修复）→ ok=false', () => {
  const v = verifier.evaluateProbes({
    rootProbeOk: true, nodeVersion: 'v22.23.3',
    installedBrotli: '1.2.1-r0', candidateBrotli: '1.2.1-r0', nonRootProbeOk: true,
  });
  assert.equal(v.ok, false);
  assert.equal(v.checks.find((c) => c.name === 'node-builtin-brotli-fixed').pass, false);
});

test('BROTLI-5 判定：brotli-libs 1.2.0-r1（旧基础）→ ok=false', () => {
  const v = verifier.evaluateProbes({
    rootProbeOk: true, nodeVersion: 'v22.23.4',
    installedBrotli: '1.2.0-r1', candidateBrotli: '1.2.1-r0', nonRootProbeOk: true,
  });
  assert.equal(v.ok, false);
  assert.equal(v.checks.find((c) => c.name === 'brotli-libs-installed-fixed').pass, false);
});

test('BROTLI-6 判定：root 探针失败不得误判为已修复（即便非 root 探测“成功”）', () => {
  const v = verifier.evaluateProbes({
    rootProbeOk: false, nodeVersion: null, installedBrotli: null, candidateBrotli: null,
    nonRootProbeOk: true,
  });
  assert.equal(v.ok, false, 'root 探针失败必须整体 FAIL');
  assert.equal(v.checks.find((c) => c.name === 'root-probe-ran').pass, false);
});

test('BROTLI-7 判定：输出缺失（解析为 null）不得误判为已修复', () => {
  const v = verifier.evaluateProbes({
    rootProbeOk: true, nodeVersion: null, installedBrotli: '1.2.1-r0', candidateBrotli: '1.2.1-r0',
    nonRootProbeOk: true,
  });
  assert.equal(v.ok, false);
});

test('BROTLI-8 判定：索引候选落后于已安装（陈旧索引）→ ok=false', () => {
  const v = verifier.evaluateProbes({
    rootProbeOk: true, nodeVersion: 'v22.23.4',
    installedBrotli: '1.2.1-r0', candidateBrotli: '1.2.0-r1', nonRootProbeOk: true,
  });
  assert.equal(v.ok, false);
  assert.equal(v.checks.find((c) => c.name === 'apk-index-fresh').pass, false);
});

test('BROTLI-9 版本比较边界', () => {
  assert.equal(verifier.compareApkVersion('1.2.0-r1', '1.2.1-r0'), -1);
  assert.equal(verifier.compareApkVersion('1.2.1-r0', '1.2.1-r0'), 0);
  assert.equal(verifier.compareApkVersion('1.2.10-r0', '1.2.9-r1'), 1);
  assert.equal(verifier.compareApkVersion('1.2.1-r2', '1.2.1-r1'), 1);
  assert.equal(verifier.compareNodeVersion('v22.23.3', '22.23.4'), -1);
  assert.equal(verifier.compareNodeVersion('v22.23.4', 'v22.23.4'), 0);
  assert.equal(verifier.compareNodeVersion('v22.24.0', 'v22.23.4'), 1);
  assert.equal(verifier.compareApkVersion('garbage', '1.2.1-r0'), null);
});

test('BROTLI-10 探针输出解析（含 apk info 的 description 噪音行）', () => {
  const sample = [
    'v22.23.4',
    '---BROTLI---',
    'brotli-libs-1.2.1-r0 description:',
    'Generic lossless compressor (libraries)',
    '---CAND---',
    'Downloading brotli-libs-1.2.1-r0',
  ].join('\n');
  const p = verifier.parseProbeOutput(sample);
  assert.equal(p.nodeVersion, 'v22.23.4');
  assert.equal(p.installedBrotli, '1.2.1-r0');
  assert.equal(p.candidateBrotli, '1.2.1-r0');
});
