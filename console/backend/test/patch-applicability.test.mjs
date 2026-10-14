// patch-applicability.test.mjs — 真实可应用性检查服务（本地仓库，无网络）。
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

// 可应用的补丁：移除硬编码 token 行（与生产 f56e3137 修复同形）
const GOOD_DIFF = [
  '--- a/src.js', '+++ b/src.js', '@@ -1,3 +1,3 @@',
  ' safe line one', '-const token = "ghp_abcdefABCDEF1234567890abcdefABCDEF4041";',
  '+const token = process.env.GITHUB_TOKEN;', ' const note = "v16 fixture";',
].join('\n');

const r1 = await verifyPatchApplies({ repoUrl: repoDir, headSha, diffText: GOOD_DIFF });
ok('V1 真实 git apply --check 通过（干净应用）',
  r1.verified === true && r1.ok === true && r1.method === 'git apply --check'
  && r1.head_sha === headSha && r1.duration_ms >= 0, r1);

// 与目标 head 冲突的补丁（旧基线上下文不匹配）
const STALE_DIFF = [
  '--- a/src.js', '+++ b/src.js', '@@ -1,2 +1,2 @@',
  '-totally different context line', '+new line',
].join('\n');
const r2 = await verifyPatchApplies({ repoUrl: repoDir, headSha, diffText: STALE_DIFF });
ok('V2 上下文不匹配的补丁 → git apply --check 失败（ok=false，不误报通过）',
  r2.verified === true && r2.ok === false && /git apply --check 失败/.test(r2.output ?? ''), r2);

// 坏 sha → checkout 失败，skipped 如实
const r3 = await verifyPatchApplies({ repoUrl: repoDir, headSha: 'f'.repeat(40), diffText: GOOD_DIFF });
ok('V3 head 不可检出 → skipped 如实（不误报）', r3.verified === false && r3.skipped === true, r3);

// 缺参 → skipped
const r4 = await verifyPatchApplies({ repoUrl: repoDir, headSha: headSha, diffText: '' });
ok('V4 空 diff → skipped（missing）', r4.verified === false && r4.skipped === true, r4);

// 清理
fs.rmSync(repoDir, { recursive: true, force: true });

console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
