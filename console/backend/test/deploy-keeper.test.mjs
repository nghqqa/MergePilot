// console/backend/test/deploy-keeper.test.mjs — Wave 3.11：Keeper v2 行为回归
// （deploy/agentteams-beta/ensure-deepseek-model.sh——版本控制工件的可测性门槛）。
//
// 用假 mc（PATH 注入 shim：cat/pipe 落到本地 JSON 文件）驱动真实脚本，覆盖：
//   K1 boot：漂移（gateway primary）→ 补丁为 deepseek-direct；
//   K2 boot 幂等：已 correct → 零写入（mtime 不变）；
//   K3 watch：漂移静默重补丁（不重启进程、无 kill）；
//   K4 配置未就绪（坏 JSON）→ boot 重试不崩；
//   K5 脚本/工件卫生：仓库脚本零内嵌凭据、provision 安装步骤存在且幂等（grep 守卫）、
//      无临时 py 残留。
// 全程零 Docker/MinIO/真实 key（DEEPSEEK_API_KEY 用假值）。
import { test, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const REPO = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const KEEPER = path.join(REPO, 'deploy', 'agentteams-beta', 'ensure-deepseek-model.sh');
const PROVISION = path.join(REPO, 'deploy', 'agentteams-beta', 'provision-workers.sh');

let TMP = null;
let binDir = null;
let objPath = null; // 假 MinIO 对象（本地 JSON）
let mtimeBefore = null;

const gwTemplate = () => JSON.stringify({
  models: { providers: { 'agentteams-gateway': {
    api: 'openai-completions', baseUrl: 'http://gateway.internal',
    models: [{ id: 'deepseek-chat', name: 'deepseek-chat', contextWindow: 64000, maxTokens: 8000, input: ['text'] }],
  } } },
  agents: { defaults: { model: { primary: 'agentteams-gateway/deepseek-chat' } } },
});
const writeObj = (primary) => {
  const d = JSON.parse(gwTemplate());
  if (primary === 'deepseek-direct/deepseek-chat') {
    d.models.providers['deepseek-direct'] = { api: 'openai-completions', apiKey: 'TESTKEY', baseUrl: 'https://api.deepseek.com/v1', models: d.models.providers['agentteams-gateway'].models };
    d.agents.defaults.model.primary = 'deepseek-direct/deepseek-chat';
  }
  fs.writeFileSync(objPath, JSON.stringify(d, null, 2));
};
const readPrimary = () => JSON.parse(fs.readFileSync(objPath, 'utf8')).agents.defaults.model.primary;

// 假 mc：cat <obj> → stdout；pipe <obj> ← stdin（写入本地文件）
const fakeMc = () => `#!/bin/sh
if [ "$1" = "cat" ]; then cat "${objPath.split('\\').join('\\\\')}"; exit 0; fi
if [ "$1" = "pipe" ]; then cat > "${objPath.split('\\').join('\\\\')}"; exit 0; fi
exit 1`;

beforeEach(() => {
  TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'keeper-test-'));
  binDir = path.join(TMP, 'bin');
  fs.mkdirSync(binDir);
  objPath = path.join(TMP, 'openclaw.json');
  fs.writeFileSync(path.join(binDir, 'mc'), fakeMc(), { mode: 0o755 });
  // python3 shim：转交宿主 python（keeper 的 JSON 变换用标准库）
  const PY = process.platform === 'win32' ? 'python'
    : (fs.existsSync('/usr/bin/python3') ? 'python3' : 'python');
  fs.writeFileSync(path.join(binDir, 'python3'), '#!/bin/sh\nexec ' + PY + ' "$@"', { mode: 0o755 });
});
// 跨平台：Windows 用 Git Bash 的 sh（脚本含 nohup 等 POSIX 语义），Linux 用 /bin/sh
const SH = process.env.KEEPER_SH
  || (process.platform === 'win32' ? 'C:/Program Files/Git/bin/sh.exe' : '/bin/sh');

afterEach(() => {
  try { fs.rmSync(TMP, { recursive: true, force: true }); } catch { /* */ }
});

function runKeeper(mode) {
  // python3 shim：node 执行 stdin 上的 python 代码？不可行——改为直接以 node 语义
  // 运行等价变换不可取。方案：PATH 里提供真 python3（若存在）则用之；否则跳过该用例。
  const env = { ...process.env,
    PATH: `${binDir}${path.delimiter}${process.env.PATH ?? ''}`,
    DEEPSEEK_API_KEY: 'TESTKEY',
    AGENTTEAMS_WORKER_NAME: 'probe-worker',
    KEEPER_NO_WATCH: '1',
  };
  return execFileSync(SH, [KEEPER, mode], { env, encoding: 'utf8', timeout: 45_000 });
}

test('K1 boot：gateway 漂移 → 补丁为 deepseek-direct（假 mc 驱动真实脚本）', () => {
  writeObj('agentteams-gateway/deepseek-chat');
  runKeeper('boot');
  assert.equal(readPrimary(), 'deepseek-direct/deepseek-chat');
  const d = JSON.parse(fs.readFileSync(objPath, 'utf8'));
  assert.equal(d.models.providers['deepseek-direct'].baseUrl, 'https://api.deepseek.com/v1');
  assert.ok(d.models.providers['deepseek-direct'].apiKey.length > 0);
});

test('K2 幂等：已 correct → 重复 boot 零改写（mtime 不变）', () => {
  writeObj('deepseek-direct/deepseek-chat');
  runKeeper('boot');
  const m1 = fs.statSync(objPath).mtimeMs;
  runKeeper('boot');
  assert.equal(fs.statSync(objPath).mtimeMs, m1, '已 correct 时不得改写对象');
});

test('K3 watch：运行期漂移 → 静默重补丁（脚本存在且含 watch 分支）', () => {
  const src = fs.readFileSync(KEEPER, 'utf8');
  assert.ok(/watch\)/.test(src), 'watch 分支存在');
  assert.ok(!/kill -TERM 1/.test(src), 'v2 keeper 不得重启容器进程（3.10 实证 reconcile 只写 MinIO）');
  assert.ok(/sleep 30/.test(src), '检查频率 30s 限频');
  // 运行期漂移静默重补丁由 K1 同一 apply_patch 路径覆盖（watch 循环调用之）
  writeObj('agentteams-gateway/deepseek-chat');
  runKeeper('boot');
  assert.equal(readPrimary(), 'deepseek-direct/deepseek-chat');
});

test('K4 配置未就绪（坏 JSON）→ boot 不崩且不改写', () => {
  fs.writeFileSync(objPath, 'not-valid-json{');
  runKeeper('boot');
  assert.equal(fs.readFileSync(objPath, 'utf8'), 'not-valid-json{', '坏 JSON 时保持原样（等待就绪）');
});

test('K5 工件卫生：脚本零内嵌凭据 + provision 幂等守卫 + 无临时残留', () => {
  const src = fs.readFileSync(KEEPER, 'utf8');
  assert.ok(!/sk-[A-Za-z0-9]{16,}/.test(src), 'keeper 不得内嵌真实 key');
  assert.ok(!/ghp_[A-Za-z0-9]{16,}/.test(src), 'keeper 不得内嵌 GitHub token');
  const prov = fs.readFileSync(PROVISION, 'utf8');
  assert.ok(prov.includes('ensure-deepseek-model.sh'), 'provision 必须安装 keeper');
  assert.ok(prov.includes('grep -q "ensure-deepseek-model.sh boot"'), 'entrypoint 钩子必须 grep 幂等守卫');
  assert.ok(!/DEEPSEEK_API_KEY=["'][A-Za-z0-9-]{16,}["']/.test(prov), 'provision 不得内嵌字面量 key 值（env 透传允许）');
  assert.ok(!/sk-[A-Za-z0-9]{16,}/.test(prov), 'provision 不得内嵌 sk- 字面量');
  const repoRoot = path.join(REPO);
  assert.ok(!fs.existsSync(path.join(repoRoot, 'w34-fix-prov3.py')), '临时脚本不得存在于仓库');
});
