// benchmarks/review-quality/lib/validate-samples.mjs — 执行前样本校验器。
//
// 校验四件事（任务书 §2，任何一项不过都不进入评测，绝不静默吞掉）：
//   1. manifest 漂移核对：全部钉死的 sha256 与磁盘实际一致（固定基线不漂移）；
//   2. 规格完备：case.json 登记了预期行为与评价规则（目标路径/缺陷标记/期望严重度）；
//   3. 真实 diff：从固定干净基线构建临时 git 仓库，缺陷版/正确版 diff 非空且目标文件在 diff 中；
//   4. 独立测试判别力：同一测试在缺陷版失败、正确版通过。
//
// 失败分类（显式枚举，不互相掩盖）：
//   manifest_drift / spec_incomplete / no_diff / target_not_in_diff /
//   defect_test_not_failing / fixed_test_not_passing / build_error
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

export const FAILURE_CLASSES = Object.freeze([
  'manifest_drift', 'spec_incomplete', 'no_diff', 'target_not_in_diff',
  'defect_test_not_failing', 'fixed_test_not_passing', 'build_error',
]);

export function sha256File(absPath) {
  return crypto.createHash('sha256').update(fs.readFileSync(absPath)).digest('hex');
}

/** 规格完备性检查（纯函数，供单测复用）。返回缺失项数组；空数组=完备。 */
export function checkSpecCompleteness(spec) {
  const missing = [];
  if (!spec || typeof spec !== 'object') return ['spec_not_object'];
  if (!spec.case_id) missing.push('case_id');
  if (!Array.isArray(spec.expected_behaviors) || spec.expected_behaviors.length === 0) {
    missing.push('expected_behaviors_empty');
  }
  const rules = spec.evaluation_rules;
  if (!rules || typeof rules !== 'object') { missing.push('evaluation_rules_missing'); return missing; }
  if (!rules.target_path) missing.push('target_path');
  if (!Array.isArray(rules.defect_marker_patterns) || rules.defect_marker_patterns.length === 0) {
    missing.push('defect_marker_patterns_empty');
  }
  const sev = rules.expected_severity?.value;
  if (!['P0', 'P1', 'P2', 'P3'].includes(sev)) missing.push('expected_severity_invalid');
  if (!rules.expected_severity?.provenance) missing.push('expected_severity_provenance');
  if (!Number.isFinite(rules.severity_tolerance) || rules.severity_tolerance < 0) {
    missing.push('severity_tolerance_invalid');
  }
  return missing;
}

function git(args, cwd) {
  const r = spawnSync('git', ['-c', 'user.email=bench@mergepilot.local',
    '-c', 'user.name=review-quality-bench', ...args],
  { cwd, encoding: 'utf8', timeout: 30_000 });
  return { code: r.status, out: (r.stdout ?? '') + (r.stderr ?? '') };
}

function runNode(testFile, cwd) {
  const r = spawnSync(process.execPath, [testFile], { cwd, encoding: 'utf8', timeout: 20_000 });
  return { code: r.status, output: ((r.stdout ?? '') + (r.stderr ?? '')).slice(-300) };
}

/**
 * 构建临时仓库并产出变体 diff（校验与后续评测共用同一 diff 来源）。
 * 返回 { ok, error?, baseCommit?, diffs?: { defect, fixed }, testRuns? }
 */
export function buildCaseWorktree({ caseDir, baselineDir, targetFile, testFile, workDir }) {
  try {
    fs.mkdirSync(workDir, { recursive: true });
    // 基线文件 → base commit
    for (const f of fs.readdirSync(baselineDir)) {
      fs.copyFileSync(path.join(baselineDir, f), path.join(workDir, f));
    }
    git(['init', '-q'], workDir);
    git(['add', '-A'], workDir);
    git(['commit', '-q', '-m', 'base: 固定干净基线'], workDir);
    const baseCommit = git(['rev-parse', 'HEAD'], workDir).out.trim();
    if (!/^[0-9a-f]{40}$/.test(baseCommit)) {
      return { ok: false, error: `base commit 解析失败: ${baseCommit.slice(0, 80)}` };
    }
    const diffs = {};
    const testRuns = {};
    for (const variant of ['defect', 'fixed']) {
      // 从 base 重建工作区 → 写入变体文件（目标文件统一命名为 targetFile）
      git(['checkout', '-f', baseCommit], workDir);
      const variantSrc = path.join(caseDir, 'files', 'src', `${path.basename(targetFile, '.js')}.${variant}.js`);
      if (!fs.existsSync(variantSrc)) return { ok: false, error: `变体文件缺失: ${variantSrc}` };
      fs.mkdirSync(path.dirname(path.join(workDir, targetFile)), { recursive: true });
      fs.copyFileSync(variantSrc, path.join(workDir, targetFile));
      fs.mkdirSync(path.dirname(path.join(workDir, testFile)), { recursive: true });
      fs.copyFileSync(path.join(caseDir, 'files', testFile), path.join(workDir, testFile));
      git(['add', '-A'], workDir);
      git(['commit', '-q', '-m', `${variant} variant`], workDir);
      const d = git(['diff', baseCommit, 'HEAD', '--'], workDir);
      if (d.code !== 0) return { ok: false, error: `git diff 失败: ${d.out.slice(0, 120)}` };
      diffs[variant] = d.out;
      testRuns[variant] = runNode(testFile, workDir);
    }
    return { ok: true, baseCommit, diffs, testRuns };
  } catch (e) {
    return { ok: false, error: String(e?.message ?? e).slice(0, 200) };
  }
}

/**
 * 校验全部样本。返回结构化校验记录（含每样本 ok / failure_class / 明细），
 * 以及构建产物（diff/测试运行结果），供执行器复用（校验与评测同一 diff）。
 */
export async function validateSamples({ benchDir, manifest, caseIds, workRoot }) {
  // 1) manifest 漂移核对（基线 + 全部样本文件）
  const drift = [];
  const checkTree = (tree) => {
    for (const [rel, want] of Object.entries(tree ?? {})) {
      const abs = path.join(benchDir, rel);
      if (!fs.existsSync(abs)) { drift.push({ file: rel, reason: 'missing' }); continue; }
      const got = sha256File(abs);
      if (got !== want) drift.push({ file: rel, want, got });
    }
  };
  checkTree(manifest.baseline_files);
  for (const id of caseIds) checkTree(manifest.cases?.[id]?.files);

  const cases = [];
  for (const caseId of caseIds) {
    const rec = {
      case_id: caseId, ok: true, failure_class: null, detail: '',
      spec: null, target_file: null, test_file: null,
      diffs: null, test_runs: null, base_commit: null,
    };
    try {
      const caseDir = path.join(benchDir, 'samples', caseId);
      const spec = JSON.parse(fs.readFileSync(path.join(caseDir, 'case.json'), 'utf8'));
      rec.spec = spec;
      rec.target_file = spec.target_file;
      rec.test_file = spec.test_file;
      if (drift.some((d) => d.file.startsWith(`samples/${caseId}/`))) {
        rec.ok = false; rec.failure_class = 'manifest_drift';
        rec.detail = '样本文件 sha256 与 manifest 不符（基线不得漂移）';
        cases.push(rec); continue;
      }
      const missing = checkSpecCompleteness(spec);
      if (missing.length > 0) {
        rec.ok = false; rec.failure_class = 'spec_incomplete'; rec.detail = `缺少: ${missing.join(',')}`;
        cases.push(rec); continue;
      }
      const built = buildCaseWorktree({
        caseDir,
        baselineDir: path.join(benchDir, 'samples', 'baseline'),
        targetFile: spec.target_file,
        testFile: spec.test_file,
        workDir: path.join(workRoot, caseId),
      });
      if (!built.ok) {
        rec.ok = false; rec.failure_class = 'build_error'; rec.detail = built.error ?? 'unknown';
        cases.push(rec); continue;
      }
      rec.base_commit = built.baseCommit;
      rec.diffs = built.diffs;
      rec.test_runs = built.testRuns;
      for (const variant of ['defect', 'fixed']) {
        if (!built.diffs[variant] || built.diffs[variant].trim() === '') {
          rec.ok = false; rec.failure_class = 'no_diff';
          rec.detail = `${variant} 版相对基线无 diff`; break;
        }
        if (!built.diffs[variant].includes(spec.target_file)) {
          rec.ok = false; rec.failure_class = 'target_not_in_diff';
          rec.detail = `${variant} 版 diff 未包含目标文件 ${spec.target_file}`; break;
        }
      }
      if (!rec.ok) { cases.push(rec); continue; }
      if (built.testRuns.defect.code === 0) {
        rec.ok = false; rec.failure_class = 'defect_test_not_failing';
        rec.detail = `独立测试在缺陷版意外通过: ${built.testRuns.defect.output}`;
        cases.push(rec); continue;
      }
      if (built.testRuns.fixed.code !== 0) {
        rec.ok = false; rec.failure_class = 'fixed_test_not_passing';
        rec.detail = `独立测试在正确版意外失败: ${built.testRuns.fixed.output}`;
        cases.push(rec); continue;
      }
      rec.detail = 'defect 测试失败 / fixed 测试通过 / 目标文件在 diff 中 / 规格完备';
    } catch (e) {
      rec.ok = false; rec.failure_class = 'build_error'; rec.detail = String(e?.message ?? e).slice(0, 200);
    }
    cases.push(rec);
  }
  return { manifest_drift: drift, cases, all_ok: drift.length === 0 && cases.every((c) => c.ok) };
}
