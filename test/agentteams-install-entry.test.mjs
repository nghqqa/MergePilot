// test/agentteams-install-entry.test.mjs — 安装入口行为回归（#519 续波，CI 门内）。
// 覆盖：冷启动顺序（apply 先于任何 token 读取）/ 已弃用静态 token-file 假设不得回归 /
// 失败退出码语义（缺必填=非零且不打印安装完成）。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.join(HERE, '..', 'deploy', 'agentteams', 'qwenpaw-v124', 'install-agentteams-full.sh');
const src = fs.readFileSync(SRC, 'utf8');

test('冷启动顺序：apply（ctrl 创建/就绪）先于任何 cli-token 读取', () => {
  const applyAt = src.indexOf('bash "$HERE/apply-qwenpaw-v124.sh"');
  const tokAt = src.indexOf('/var/run/agentteams/cli-token');
  assert.ok(applyAt > 0, 'apply 调用未找到');
  assert.ok(tokAt > applyAt, `冷启动顺序错误：token 读取(offset ${tokAt})早于 apply(offset ${applyAt})——无 ctrl 时不得先取 token`);
});

test('已弃用假设：不得回归静态 token-file 注入/误重建守卫', () => {
  assert.ok(!src.includes('AGENTTEAMS_AUTH_TOKEN_FILE=/data/api-token'),
    '静态 token-file 注入已弃用（ctrl 不以此为准）——不得回归');
  assert.ok(!/grep -c '\^AGENTTEAMS_AUTH_TOKEN_FILE'/.test(src), '不得以 token-file env 触发误重建');
});

test('认证契约：发布端口 cli-token + 错误/无凭据负样本（不关认证/不延有效期）', () => {
  assert.ok(src.includes('Authorization: Bearer $TOK'), 'api 助手必须携带 Bearer 认证头');
  assert.ok(src.includes('$QP_PORT_CTRL'), '必须走发布端口（网络面——loopback 鉴权分叉实证）');
  assert.ok(src.includes('at-iso-wrong-credential'), '错误凭据负样本缺失');
  assert.ok(!src.includes('AUTH_TOKEN_EXPIRATION'), '不得延长 token 有效期');
});

test('失败退出码：缺必填 env → 非零退出且不打印安装完成', () => {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), 'at-entry-test-'));
  // stub docker：一切调用成功退出（digest 核验通过）——脚本应死于缺 QP_LLM_API_KEY
  fs.writeFileSync(path.join(sandbox, 'docker'), '#!/bin/bash\nexit 0\n', { mode: 0o755 });
  // stub python3/openssl：本地 Windows 无 python3 命令——只为通过前置 command -v 检查
  fs.writeFileSync(path.join(sandbox, 'python3'), '#!/bin/bash\nexit 0\n', { mode: 0o755 });
  fs.writeFileSync(path.join(sandbox, 'openssl'), '#!/bin/bash\nexit 0\n', { mode: 0o755 });
  const outPath = path.join(sandbox, 'out.txt');
  const outFd = fs.openSync(outPath, 'w');
  let code = 0;
  try {
    execFileSync('bash', [SRC, '--environment', 'production'], {
      encoding: 'utf8',
      env: {
        PATH: `${sandbox}:${process.env.PATH}`,
        HOME: sandbox, // 隔离秘密目录
        QP_LLM_BASE_URL: 'https://api.deepseek.example',
        QP_LLM_MODEL: 'deepseek-chat',
        // QP_LLM_API_KEY 故意缺失
      },
      stdio: ['ignore', outFd, outFd],
    });
  } catch (e) {
    code = e.status ?? 1;
  } finally {
    fs.closeSync(outFd);
  }
  const out = fs.readFileSync(outPath, 'utf8');
  assert.ok(code !== 0, '缺必填 env 必须非零退出');
  assert.ok(!out.includes('安装完成'), '失败路径不得打印安装完成');
  assert.ok(out.includes('QP_LLM_API_KEY'), '失败原因应指明缺失键');
});

test('worker 定义锁步：入口四角色与 executor AGENTTEAMS_WORKERS 一致', () => {
  const exSrc = fs.readFileSync(path.join(HERE, '..', 'console', 'backend', 'lib', 'multiuser', 'agents', 'agentteams-executor.mjs'), 'utf8');
  for (const n of ['mergepilot-leader', 'mergepilot-reviewer', 'mergepilot-fixer', 'mergepilot-verifier']) {
    assert.ok(src.includes(`'${n}|`), `安装入口缺 worker 定义: ${n}`);
    assert.ok(exSrc.includes(`name: '${n}'`), `executor 缺 worker 定义: ${n}`);
  }
});
