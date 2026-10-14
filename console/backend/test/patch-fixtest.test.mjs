// patch-fixtest.test.mjs — 实际修复测试服务（本地仓库+真实 node 测试命令）。
// 覆盖：修复前败→应用→修复后过；修复前即通过=如实 not-proving；缺参 skipped；
//       预批准命令解析（env 映射/deps 注入/坏 JSON）。
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass += 1; console.log('  PASS ', name); }
  else { fail += 1; console.log('  FAIL ', name, JSON.stringify(detail ?? '').slice(0, 240)); }
};

const { verifyFixTest } = await import('../lib/multiuser/patch-test.mjs');
const { sha256Hex } = await import('../lib/multiuser/artifacts.mjs');

const runGit = (cwd, args) => execFileSync('git', args, { cwd, encoding: 'utf8' });

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'mp-fixtest-repo-'));
runGit(dir, ['init']);
runGit(dir, ['config', 'user.email', 'test@example.com']);
runGit(dir, ['config', 'user.name', 'test']);
// 功能缺陷：off-by-one（与生产 f56e3137 同形）——测试命令检查其行为
fs.writeFileSync(path.join(dir, 'src.js'),
  'function sumDiscounts(p){let s=0;for(let i=0;i<=p.length;i++)s+=p[i];return s}\nmodule.exports={sumDiscounts};\n');
fs.writeFileSync(path.join(dir, 'check.js'),
  'const {sumDiscounts}=require("./src.js");\nif(sumDiscounts([1,2,3])!==6){console.error("BUG: off-by-one");process.exit(1);}\nconsole.log("ok");\n');
runGit(dir, ['add', '.']);
runGit(dir, ['commit', '-m', 'base']);
const headSha = runGit(dir, ['rev-parse', 'HEAD']).trim();

// 修复补丁：<= 越界 → <（修复前 check.js exit 1 → 修复后 exit 0）
const FIX_DIFF = [
  '--- a/src.js', '+++ b/src.js', '@@ -1,2 +1,2 @@',
  '-function sumDiscounts(p){let s=0;for(let i=0;i<=p.length;i++)s+=p[i];return s}',
  '+function sumDiscounts(p){let s=0;for(let i=0;i<p.length;i++)s+=p[i];return s}',
  ' module.exports={sumDiscounts};',
].join('\n') + '\n';

const t1 = await verifyFixTest({ repoUrl: dir, headSha, diffText: FIX_DIFF,
  testCmd: 'node check.js', patchDigest: sha256Hex(FIX_DIFF) });
ok('T1 修复前失败→应用→修复后通过', t1.verified === true && t1.ok === true
  && t1.pre?.code !== 0 && t1.post?.code === 0 && t1.test_cmd === 'node check.js', t1);
ok('T1b 证据绑定 head/digest', t1.head_sha === headSha && t1.patch_digest === sha256Hex(FIX_DIFF), null);

// 修复前即通过（测试没覆盖缺陷）→ 如实 ok=false + pre_test_passed
const t2 = await verifyFixTest({ repoUrl: dir, headSha, diffText: FIX_DIFF,
  testCmd: 'node -e "process.exit(0)"' });
ok('T2 修复前即通过 → ok=false（不能证明修复）', t2.verified === true && t2.ok === false
  && t2.reason === 'pre_test_passed', t2);

// 坏补丁 → apply_failed 如实
const t3 = await verifyFixTest({ repoUrl: dir, headSha, diffText: '--- a/src.js\n+++ b/src.js\n@@ -9,9 +9,9 @@\n-x\n+y\n',
  testCmd: 'node check.js' });
ok('T3 补丁应用失败 → apply_failed 如实', t3.verified === true && t3.ok === false
  && t3.reason === 'apply_failed', t3);

// 缺参 → skipped
const t4 = await verifyFixTest({ repoUrl: dir, headSha, diffText: FIX_DIFF, testCmd: '' });
ok('T4 缺 testCmd → skipped', t4.verified === false && t4.skipped === true, t4);

// 预批准命令解析（部署 env 唯一权威——deps.testCmd 后门默认关闭）
const envMap = JSON.stringify({ 'gitee:nghqqa/mergepilot-test': 'node check.js', 'x/y': 'node y.js' });
const { resolvePreApprovedTestCmd } = await import('../lib/multiuser/agents/patch-test-cmd.mjs');
ok('T5 provider 键命中', resolvePreApprovedTestCmd({ MU_FIX_TEST_CMDS: envMap },
  { provider: 'gitee', owner: 'nghqqa', repoName: 'mergepilot-test' }) === 'node check.js', null);
ok('T6 owner/repo 键回退命中', resolvePreApprovedTestCmd({ MU_FIX_TEST_CMDS: envMap },
  { owner: 'x', repoName: 'y' }) === 'node y.js', null);
ok('T7 未命中 → null', resolvePreApprovedTestCmd({ MU_FIX_TEST_CMDS: envMap },
  { owner: 'a', repoName: 'b' }) === null, null);
ok('T8 坏 JSON → null', resolvePreApprovedTestCmd({ MU_FIX_TEST_CMDS: '{bad' }, {}) === null, null);
ok('T9 无 env → null（含 deps 平凡命令被忽略）',
  resolvePreApprovedTestCmd({}, { owner: 'x', repoName: 'y', testCmd: 'node -e process.exit(0)' }) === null, null);
ok('T10 deps.testCmd 不再遮蔽部署配置（b10e3f4f 实证缺陷回归锁）',
  resolvePreApprovedTestCmd({ MU_FIX_TEST_CMDS: envMap },
    { owner: 'nghqqa', repoName: 'mergepilot-test', testCmd: 'node -e process.exit(0)' }) === 'node check.js', null);
ok('T11 deps 后门默认关闭', resolvePreApprovedTestCmd({},
  { owner: 'x', repoName: 'y', testCmd: 'node real.js' }) === null, null);
ok('T12 deps 后门显式开启（E2E 隔离）才采纳',
  resolvePreApprovedTestCmd({ MU_FIX_TEST_ALLOW_DEPS_CMD: '1' },
    { owner: 'x', repoName: 'y', testCmd: 'node real.js' }) === 'node real.js', null);

fs.rmSync(dir, { recursive: true, force: true });
console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
