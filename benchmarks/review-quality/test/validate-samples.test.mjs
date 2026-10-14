// benchmarks/review-quality/test/validate-samples.test.mjs — 样本校验器单测。
// 覆盖：真实六组样本全绿（git+node 真实执行，零网络）+ 合成坏样本的失败分类。
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { validateSamples, checkSpecCompleteness, sha256File } from '../lib/validate-samples.mjs';

const BENCH_DIR = path.resolve(path.dirname(fileURLToPathSafe()), '..');
function fileURLToPathSafe() {
  // node:test 下 import.meta.url 可用（.mjs）——直接取本文件位置
  return new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1');
}

function tmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'rq-val-test-'));
}

/** 构建合成 bench 目录（一个可用的 CASE-XX + baseline + manifest）。 */
function makeSyntheticBench({ defectCode, fixedCode, testCode, targetFile = 'src/t1.js', specOverrides = {} }) {
  const dir = tmpDir();
  fs.mkdirSync(path.join(dir, 'samples', 'baseline'), { recursive: true });
  fs.writeFileSync(path.join(dir, 'samples', 'baseline', 'README.md'), '# base\n');
  const caseDir = path.join(dir, 'samples', 'CASE-X1');
  fs.mkdirSync(path.join(caseDir, 'files', 'src'), { recursive: true });
  fs.mkdirSync(path.join(caseDir, 'files', 'tests'), { recursive: true });
  const base = path.basename(targetFile, '.js');
  fs.writeFileSync(path.join(caseDir, 'files', 'src', `${base}.defect.js`), defectCode);
  fs.writeFileSync(path.join(caseDir, 'files', 'src', `${base}.fixed.js`), fixedCode);
  fs.writeFileSync(path.join(caseDir, 'files', 'tests', `${base}.test.js`), testCode);
  const spec = {
    case_id: 'CASE-X1', name: '合成样本', target_file: targetFile,
    test_file: `tests/${base}.test.js`,
    expected_behaviors: [{ call: 'f(1)', want: '1' }],
    evaluation_rules: {
      target_path: targetFile,
      defect_marker_patterns: ['i\\s*>\\s*0'],
      expected_severity: { value: 'P2', provenance: 'registered' },
      severity_tolerance: 1,
    },
    ...specOverrides,
  };
  fs.writeFileSync(path.join(caseDir, 'case.json'), JSON.stringify(spec));
  const manifest = {
    schema_version: 'rq-manifest-v1',
    baseline_files: { 'samples/baseline/README.md': sha256File(path.join(dir, 'samples', 'baseline', 'README.md')) },
    cases: { 'CASE-X1': { files: {
      'samples/CASE-X1/case.json': sha256File(path.join(caseDir, 'case.json')),
      [`samples/CASE-X1/files/src/${base}.defect.js`]: sha256File(path.join(caseDir, 'files', 'src', `${base}.defect.js`)),
      [`samples/CASE-X1/files/src/${base}.fixed.js`]: sha256File(path.join(caseDir, 'files', 'src', `${base}.fixed.js`)),
      [`samples/CASE-X1/files/tests/${base}.test.js`]: sha256File(path.join(caseDir, 'files', 'tests', `${base}.test.js`)),
    } } },
  };
  fs.writeFileSync(path.join(dir, 'manifest.json'), JSON.stringify(manifest));
  return dir;
}

const GOOD_DEFECT = "function f(a){let s='';for(let i=a.length-1;i>0;i--)s+=a[i];return s;}\nmodule.exports={f};\n";
const GOOD_FIXED = "function f(a){let s='';for(let i=a.length-1;i>=0;i--)s+=a[i];return s;}\nmodule.exports={f};\n";
const GOOD_TEST = "const {f}=require('../src/t1.js');const assert=require('node:assert');assert.strictEqual(f(['x','y']),'yx');console.log('ok');\n";

test('checkSpecCompleteness：缺项逐项报出', () => {
  assert.deepEqual(checkSpecCompleteness({
    case_id: 'C', expected_behaviors: [{}],
    evaluation_rules: { target_path: 'a.js', defect_marker_patterns: ['x'],
      expected_severity: { value: 'P2', provenance: 'registered' }, severity_tolerance: 1 },
  }), []);
  assert.ok(checkSpecCompleteness({}).includes('expected_behaviors_empty'));
  assert.ok(checkSpecCompleteness({
    case_id: 'C', expected_behaviors: [{}],
    evaluation_rules: { target_path: 'a.js', defect_marker_patterns: [],
      expected_severity: { value: 'P9' }, severity_tolerance: -1 },
  }).length >= 3, '标记空/严重度非法/容差非法/provenance 缺失都应报');
});

test('真实六组样本：校验全绿（manifest 无漂移+测试判别力+目标在 diff）', async () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(BENCH_DIR, 'manifest.json'), 'utf8'));
  const v = await validateSamples({ benchDir: BENCH_DIR, manifest,
    caseIds: Object.keys(manifest.cases), workRoot: tmpDir() });
  assert.deepEqual(v.manifest_drift, [], `manifest 漂移: ${JSON.stringify(v.manifest_drift)}`);
  for (const c of v.cases) {
    assert.ok(c.ok, `${c.case_id} 校验失败: ${c.failure_class} ${c.detail}`);
    assert.equal(c.failure_class, null);
    assert.notEqual(c.test_runs.defect.code, 0, `${c.case_id} 缺陷版测试应失败`);
    assert.equal(c.test_runs.fixed.code, 0, `${c.case_id} 正确版测试应通过`);
    assert.ok(c.diffs.defect.includes(c.spec.target_file));
    assert.ok(c.diffs.fixed.includes(c.spec.target_file));
    assert.ok(c.diffs.defect.trim().length > 0);
  }
  assert.ok(v.all_ok);
});

test('manifest 漂移：文件内容被改 → manifest_drift，不进入评测', async () => {
  const dir = makeSyntheticBench({ defectCode: GOOD_DEFECT, fixedCode: GOOD_FIXED, testCode: GOOD_TEST });
  const caseDir = path.join(dir, 'samples', 'CASE-X1');
  // 篡改 defect 文件（模拟基线漂移）但不改 manifest
  fs.writeFileSync(path.join(caseDir, 'files', 'src', 't1.defect.js'), '/* tampered */\n');
  const manifest = JSON.parse(fs.readFileSync(path.join(dir, 'manifest.json'), 'utf8'));
  const v = await validateSamples({ benchDir: dir, manifest, caseIds: ['CASE-X1'], workRoot: tmpDir() });
  assert.equal(v.all_ok, false);
  assert.equal(v.cases[0].failure_class, 'manifest_drift');
  assert.equal(v.manifest_drift.length, 1);
});

test('规格不完备：缺 expected_behaviors → spec_incomplete', async () => {
  const dir = makeSyntheticBench({ defectCode: GOOD_DEFECT, fixedCode: GOOD_FIXED, testCode: GOOD_TEST,
    specOverrides: { expected_behaviors: [] } });
  const manifest = JSON.parse(fs.readFileSync(path.join(dir, 'manifest.json'), 'utf8'));
  const v = await validateSamples({ benchDir: dir, manifest, caseIds: ['CASE-X1'], workRoot: tmpDir() });
  assert.equal(v.cases[0].failure_class, 'spec_incomplete');
});

test('缺陷版测试意外通过 → defect_test_not_failing（测试无判别力即拒绝评测）', async () => {
  const dir = makeSyntheticBench({ defectCode: GOOD_DEFECT, fixedCode: GOOD_FIXED,
    testCode: "console.log('always pass');\n" });
  const manifest = JSON.parse(fs.readFileSync(path.join(dir, 'manifest.json'), 'utf8'));
  const v = await validateSamples({ benchDir: dir, manifest, caseIds: ['CASE-X1'], workRoot: tmpDir() });
  assert.equal(v.cases[0].failure_class, 'defect_test_not_failing');
});

test('正确版测试意外失败 → fixed_test_not_passing', async () => {
  const dir = makeSyntheticBench({ defectCode: GOOD_DEFECT, fixedCode: GOOD_FIXED,
    testCode: "process.exit(1);\n" });
  const manifest = JSON.parse(fs.readFileSync(path.join(dir, 'manifest.json'), 'utf8'));
  const v = await validateSamples({ benchDir: dir, manifest, caseIds: ['CASE-X1'], workRoot: tmpDir() });
  assert.equal(v.cases[0].failure_class, 'fixed_test_not_passing');
});

test('变体文件缺失 → build_error（不静默吞掉；manifest 同步移除该文件钉值以隔离漂移检查）', async () => {
  const dir = makeSyntheticBench({ defectCode: GOOD_DEFECT, fixedCode: GOOD_FIXED, testCode: GOOD_TEST });
  fs.rmSync(path.join(dir, 'samples', 'CASE-X1', 'files', 'src', 't1.fixed.js'));
  const manifest = JSON.parse(fs.readFileSync(path.join(dir, 'manifest.json'), 'utf8'));
  delete manifest.cases['CASE-X1'].files['samples/CASE-X1/files/src/t1.fixed.js'];
  const v = await validateSamples({ benchDir: dir, manifest, caseIds: ['CASE-X1'], workRoot: tmpDir() });
  assert.equal(v.cases[0].failure_class, 'build_error');
  assert.ok(v.cases[0].detail.includes('缺失'));
});
