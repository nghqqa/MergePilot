// patch-applicability.test.mjs — 真实可应用性检查服务（本地仓库，无网络）。
// V5/V6 覆盖缺尾换行的确定性修正闭环（服务不做静默归一化——修正=调用方的新版本产物）。
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass += 1; console.log('  PASS ', name); }
  else { fail += 1; console.log('  FAIL ', name, JSON.stringify(detail ?? '').slice(0, 200)); }
};

const { verifyPatchApplies } = await import('../lib/multiuser/patch-applicability.mjs');
const { deterministicNormalize, sha256Hex, applicabilityVerdict } = await import('../lib/multiuser/artifacts.mjs');

const runGit = (cwd, args) => execFileSync('git', args, { cwd, encoding: 'utf8' });

const mkRepo = () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'mp-apply-repo-'));
  runGit(dir, ['init']);
  runGit(dir, ['config', 'user.email', 'test@example.com']);
  runGit(dir, ['config', 'user.name', 'test']);
  fs.writeFileSync(path.join(dir, 'src.js'), 'safe line one\nconst token = "ghp_abcdefABCDEF1234567890abcdefABCDEF4041";\nconst note = "v16 fixture";\n');
  runGit(dir, ['add', '.']);
  runGit(dir, ['commit', '-m', 'base']);
  return dir;
};

const repoDir = mkRepo();
const headSha = runGit(repoDir, ['rev-parse', 'HEAD']).trim();

// 可应用的补丁：移除硬编码 token 行（与生产 f56e3137 修复同形；补丁以换行结尾=合法形态）
const GOOD_LINES = [
  '--- a/src.js', '+++ b/src.js', '@@ -1,3 +1,3 @@',
  ' safe line one', '-const token = "ghp_abcdefABCDEF1234567890abcdefABCDEF4041";',
  '+const token = process.env.GITHUB_TOKEN;', ' const note = "v16 fixture";',
];
const GOOD_DIFF = GOOD_LINES.join('\n') + '\n';

const r1 = await verifyPatchApplies({ repoUrl: repoDir, headSha, diffText: GOOD_DIFF,
  patchDigest: sha256Hex(GOOD_DIFF) });
ok('V1 真实 git apply --check 通过（干净应用）',
  r1.verified === true && r1.ok === true && r1.method === 'git apply --check'
  && r1.head_sha === headSha && r1.duration_ms >= 0, r1);
ok('V1b 证据绑定 patch_digest', r1.patch_digest === sha256Hex(GOOD_DIFF), r1.patch_digest);

// 与目标 head 冲突的补丁（旧基线上下文不匹配）
const STALE_DIFF = [
  '--- a/src.js', '+++ b/src.js', '@@ -1,2 +1,2 @@',
  '-totally different context line', '+new line',
].join('\n') + '\n';
const r2 = await verifyPatchApplies({ repoUrl: repoDir, headSha, diffText: STALE_DIFF });
ok('V2 上下文不匹配的补丁 → git apply --check 失败（ok=false，不误报通过）',
  r2.verified === true && r2.ok === false && /git apply --check 失败/.test(r2.output ?? ''), r2);
ok('V2b 上下文不匹配无确定性线索（不得标 missing_trailing_newline）',
  r2.diagnosis == null, r2.diagnosis);

// 坏 sha → checkout 失败，skipped 如实
const r3 = await verifyPatchApplies({ repoUrl: repoDir, headSha: 'f'.repeat(40), diffText: GOOD_DIFF });
ok('V3 head 不可检出 → skipped 如实（不误报）', r3.verified === false && r3.skipped === true, r3);

// 缺参 → skipped
const r4 = await verifyPatchApplies({ repoUrl: repoDir, headSha, diffText: '' });
ok('V4 空 diff → skipped（missing）', r4.verified === false && r4.skipped === true, r4);

// V5：缺尾换行（生产 f56e3137 实证成因）→ 服务如实失败+成因线索；
// 确定性修正产出新版本补丁 → 重检通过（检查内容=修正后的存储内容）
const NO_NL = GOOD_LINES.join('\n');
const r5 = await verifyPatchApplies({ repoUrl: repoDir, headSha, diffText: NO_NL });
ok('V5a 缺尾换行 → git apply --check 失败（不静默归一化）',
  r5.verified === true && r5.ok === false, r5);
ok('V5b 失败诊断标记 missing_trailing_newline', r5.diagnosis === 'missing_trailing_newline', r5.diagnosis);
const nd = deterministicNormalize(NO_NL);
ok('V5c deterministicNormalize 产出尾换行修正', nd?.kind === 'add_trailing_newline'
  && nd.diff === GOOD_DIFF, nd);
const r5b = await verifyPatchApplies({ repoUrl: repoDir, headSha, diffText: nd.diff,
  patchDigest: sha256Hex(nd.diff) });
ok('V5d 修正版本重检通过（版本化闭环）', r5b.verified === true && r5b.ok === true, r5b);

// V6：deterministicNormalize 边界——已带尾换行/非 diff/缺 hunk → 一律 null（不猜测）
ok('V6a 已带尾换行 → null', deterministicNormalize(GOOD_DIFF) === null, null);
ok('V6b 非 diff 文本缺尾换行 → null', deterministicNormalize('random text no newline') === null, null);
ok('V6c 空 → null', deterministicNormalize('') === null, null);

// V7：权威判定五态（页面/报告/评论共用口径）
const vPass = applicabilityVerdict({ ok: true, issues: [], git_apply: r1 }, sha256Hex(GOOD_DIFF));
ok('V7a pass', vPass.level === 'pass' && /通过/.test(vPass.text), vPass);
const vFail = applicabilityVerdict({ ok: true, issues: [], git_apply: r2 }, sha256Hex(STALE_DIFF));
ok('V7b fail（结构 ok 不覆盖真实失败）', vFail.level === 'fail' && /不可应用/.test(vFail.text), vFail);
const vNone = applicabilityVerdict({ ok: true, issues: [] }, null);
ok('V7c not_executed（未执行≠通过）', vNone.level === 'not_executed' && /未执行/.test(vNone.text), vNone);
const vEnv = applicabilityVerdict({ ok: true, issues: [], git_apply: r3 }, null);
ok('V7d env_unavailable（skipped 如实）', vEnv.level === 'env_unavailable' && /未验证/.test(vEnv.text), vEnv);
const vStale = applicabilityVerdict({ ok: true, git_apply: { verified: true, ok: true, patch_digest: 'a'.repeat(64) } },
  'b'.repeat(64));
ok('V7e stale（digest 不匹配→旧证据失效）', vStale.level === 'stale' && /失效/.test(vStale.text), vStale);

// 清理
fs.rmSync(repoDir, { recursive: true, force: true });

console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
