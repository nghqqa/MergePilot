// CVE-2026-111497（brotli 解码器越界写）双面修复断言器。
//
// 用法：node scripts/verify-image-security-fixes.cjs <image-ref>
// 输出：JSON 证据 + 每项检查 PASS/FAIL；全部通过退出码 0，否则 1。
//
// 纪律（2026-10-26 波，对应任务要求）：
//  - 判定只看 root（--user 0:0）探针的显式输出；任一 root 探针失败或输出缺失
//    → FAIL，绝不静默跳过。
//  - 非 root 探测（USER node 下 apk 无日志权限会静默失败回退旧 index）的结果
//    仅作记录，绝不作为修复或未修复的证据。
//  - trivy 不覆盖 node 二进制内的内置 brotli，因此 node 版本断言必须保留本
//    脚本的 root 探针，不能只依据扫描结果宣称修复。

'use strict';
const { execFileSync } = require('node:child_process');

// node:22-alpine 修复基线（CVE-2026-111497 官方修复版本）
const MIN_NODE = '22.23.4';
const MIN_BROTLI = '1.2.1-r0';

// "v22.23.4" → [22,23,4]；解析失败返回 null
function parseNodeVersion(v) {
  const m = /^v?(\d+)\.(\d+)\.(\d+)$/.exec(String(v || '').trim());
  return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
}

// "1.2.1-r0" → { seg:[1,2,1], rev:0 }；解析失败返回 null
function parseApkVersion(v) {
  const m = /^(\d+(?:\.\d+)*)(?:-r(\d+))?$/.exec(String(v || '').trim());
  return m ? { seg: m[1].split('.').map(Number), rev: m[2] === undefined ? 0 : Number(m[2]) } : null;
}

function compareParsed(a, b) {
  const n = Math.max(a.length, b.length);
  for (let i = 0; i < n; i++) {
    const x = a[i] || 0, y = b[i] || 0;
    if (x !== y) return x < y ? -1 : 1;
  }
  return 0;
}

// -1/0/1；任一无法解析返回 null（调用方按 FAIL 处理）
function compareApkVersion(a, b) {
  const pa = parseApkVersion(a), pb = parseApkVersion(b);
  if (!pa || !pb) return null;
  const c = compareParsed(pa.seg, pb.seg);
  return c !== 0 ? c : (pa.rev < pb.rev ? -1 : pa.rev > pb.rev ? 1 : 0);
}

function compareNodeVersion(a, b) {
  const pa = parseNodeVersion(a), pb = parseNodeVersion(b);
  if (!pa || !pb) return null;
  return compareParsed(pa, pb);
}

// 纯判定函数（可单测）。probes:
//   rootProbeOk     root 探针命令是否成功执行（true/false）
//   nodeVersion     root 探针解析出的 node 版本（如 "v22.23.4"）或 null
//   installedBrotli root 探针解析出的已安装 brotli-libs 版本或 null
//   candidateBrotli root 探针 apk fetch --simulate 解析的仓库候选版本或 null
//   nonRootProbeOk  非 root 探针结果（true/false/null）——仅记录，绝不参与判定
function evaluateProbes(probes) {
  const checks = [];
  const add = (name, pass, detail) => checks.push({ name, pass: pass === true, detail });

  // 检查 0：root 探针必须真实成功——失败即整体 FAIL（不误判为已修复）
  add('root-probe-ran', probes.rootProbeOk === true,
    probes.rootProbeOk === true ? 'root 探针执行成功' : 'root 探针未成功执行——按纪律不判定为修复');

  const nv = parseNodeVersion(probes.nodeVersion);
  const nvMin = parseNodeVersion(MIN_NODE);
  add('node-builtin-brotli-fixed', nv !== null && compareParsed(nv, nvMin) >= 0,
    `node=${probes.nodeVersion || '(缺失)'} 需 >= ${MIN_NODE}`);

  const ib = probes.installedBrotli;
  const ibCmp = ib ? compareApkVersion(ib, MIN_BROTLI) : null;
  add('brotli-libs-installed-fixed', ibCmp !== null && ibCmp >= 0,
    `installed=${ib || '(缺失)'} 需 >= ${MIN_BROTLI}`);

  // 索引新鲜度：最新索引候选必须 >= 已安装（证明构建期拉到含修复的 APKINDEX，
  // 且非陈旧索引误装）；候选缺失或低于已安装 → FAIL
  const cb = probes.candidateBrotli;
  const cbVsMin = cb ? compareApkVersion(cb, MIN_BROTLI) : null;
  const cbVsInst = cb && ib ? compareApkVersion(cb, ib) : null;
  add('apk-index-fresh', cbVsMin !== null && cbVsMin >= 0 && cbVsInst !== null && cbVsInst >= 0,
    `candidate=${cb || '(缺失)'} 需 >= max(${MIN_BROTLI}, installed=${ib || '?'})`);

  return {
    ok: checks.every((c) => c.pass),
    nonRootProbe: { ok: probes.nonRootProbeOk === true, note: '仅记录——非 root 探测失败不作为修复证据，成功也不作为判定依据' },
    minVersions: { node: MIN_NODE, brotliLibs: MIN_BROTLI },
    checks,
  };
}

function runProbe(image, user) {
  // 已安装版本一律读 /lib/apk/db/installed（权威）：apk info 会同时吐出旧版本的
  // 残留 info 块（candidate.2 实测：DB=1.2.1-r0 而 apk info 首行为 1.2.0-r1），
  // 按首行正则取值会冤枉好镜像。apk policy 仅作人读证据附在输出里，不参与解析。
  const cmd = [
    'node --version',
    'echo ---BROTLI---',
    "awk '/^P:brotli-libs$/{getline; sub(/^V:/,\"\"); print; exit}' /lib/apk/db/installed",
    'echo ---POLICY---',
    'apk policy brotli-libs',
    'echo ---UPDATE---',
    'apk update',
    'echo ---CAND---',
    'apk fetch --simulate brotli-libs',
  ].join('; ');
  try {
    const out = execFileSync('docker',
      ['run', '--rm', '--user', user, '--entrypoint', '/bin/sh', image, '-c', cmd],
      { encoding: 'utf8', timeout: 180000 });
    return { ok: true, out };
  } catch (e) {
    return { ok: false, out: String((e && e.stdout) || '') + String((e && e.stderr) || e) };
  }
}

function parseProbeOutput(out) {
  const grab = (marker, next) => {
    const i = out.indexOf(marker);
    if (i < 0) return null;
    const j = next ? out.indexOf(next, i) : out.length;
    if (j < 0) return null;
    return out.slice(i + marker.length, j);
  };
  const nodeVersion = (() => {
    const seg = grab('', '---BROTLI---') || '';
    const m = /v?\d+\.\d+\.\d+/.exec(seg);
    return m ? m[0] : null;
  })();
  const installedBrotli = (() => {
    const seg = grab('---BROTLI---', '---POLICY---') || '';
    const m = /(\d+(?:\.\d+)*-r\d+)/.exec(seg);
    return m ? m[1] : null;
  })();
  const candidateBrotli = (() => {
    const seg = grab('---CAND---', null) || '';
    const m = /brotli-libs-(\d+(?:\.\d+)*-r\d+)/.exec(seg);
    return m ? m[1] : null;
  })();
  return { nodeVersion, installedBrotli, candidateBrotli };
}

function main() {
  const image = process.argv[2];
  if (!image || image === '-h' || image === '--help') {
    console.error('usage: node scripts/verify-image-security-fixes.cjs <image-ref>');
    process.exit(2);
  }
  const root = runProbe(image, '0:0');
  const parsed = root.ok ? parseProbeOutput(root.out) : { nodeVersion: null, installedBrotli: null, candidateBrotli: null };
  const nonRoot = runProbe(image, '1:1');
  const verdict = evaluateProbes({
    rootProbeOk: root.ok,
    nodeVersion: parsed.nodeVersion,
    installedBrotli: parsed.installedBrotli,
    candidateBrotli: parsed.candidateBrotli,
    nonRootProbeOk: nonRoot.ok ? parseProbeOutput(nonRoot.out).installedBrotli !== null : false,
  });
  console.log(JSON.stringify({ image, verdict }, null, 2));
  process.exit(verdict.ok ? 0 : 1);
}

if (require.main === module) main();

module.exports = {
  MIN_NODE, MIN_BROTLI,
  parseNodeVersion, parseApkVersion,
  compareApkVersion, compareNodeVersion,
  evaluateProbes, parseProbeOutput,
};
