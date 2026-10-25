// test/agentteams-install-helpers.test.mjs — 安装器请求助手行为回归（v3 契约：宿主 curl→发布端口）。
// 被测对象=install-agentteams-full.sh 原样提取的 api() 定义；
// stub curl 捕获 argv：断言 path 强制前导斜杠、Authorization 头携带 token、
// token 不进 URL/输出。CI 门内（node --test test/*.test.mjs）。
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

test('api() 契约：path 首参+强制前导斜杠+宿主 curl（无 docker exec/loopback）', () => {
  const m = src.match(/^api\(\) \{.*$/m);
  assert.ok(m, 'api() 定义未找到');
  assert.ok(m[0].includes('local p="$1"; shift;'), 'path 必须是首参并 shift');
  assert.ok(m[0].includes("case \"$p\" in"), '必须有前导斜杠守卫（防 URL 拼接畸形 curl exit 3）');
  assert.ok(!m[0].includes('docker exec'), '不得再走容器内 loopback（鉴权分叉实证 401）');
  assert.ok(m[0].includes('$QP_PORT_CTRL'), '必须走发布端口');
});

test('api() 行为：占位 token 进 Authorization 头、不进 URL/输出（POSIX；Windows 下由 CI ubuntu 覆盖）', { skip: process.platform === 'win32' ? 'stub-exec 依赖 POSIX 执行位——CI ubuntu 执行' : false }, () => {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), 'at-helper-test-'));
  const P = 'PLACEHOLDER-NOT-A-SECRET-0123456789abcdef';
  const curlLines = [
    '#!/bin/bash',
    'printf \'ARG:%s\\n\' "$@" >> "$CAPTURE"',
    'echo -n 200',
  ];
  fs.writeFileSync(path.join(sandbox, 'curl'), curlLines.join('\n'), { mode: 0o755 });
  const cap = path.join(sandbox, 'captured.txt');
  const apiDef = src.match(/^api\(\) \{.*$/m)[0];
  const script = [
    'export PATH=' + JSON.stringify(sandbox) + ':$PATH',
    'export CAPTURE=' + JSON.stringify(cap),
    'TOK=' + JSON.stringify(P),
    apiDef,
    'api /api/v1/workers',
  ].join('\n');
  let code = 0;
  try {
    execFileSync('bash', ['-c', script], { encoding: 'utf8', env: { ...process.env, CAPTURE: cap } });
  } catch (e) { code = e.status ?? 1; }
  const captured = fs.readFileSync(cap, 'utf8');
  const args = captured.split('\n').filter((x) => x.startsWith('ARG:')).map((x) => x.slice(4));
  assert.ok(args.includes('Authorization: Bearer ' + P),
    'Authorization 头必须携带占位 token，实际 argv：' + JSON.stringify(args));
  const urlArgs = args.filter((x) => x.startsWith('http'));
  assert.deepEqual(urlArgs, ['http://127.0.0.1:18290/api/v1/workers'],
    'curl 只应收到目标 URL：' + JSON.stringify(args));
  assert.ok(code === 0, 'api() 应成功退出');
});

test('worker 定义锁步（继承）：入口四角色与 executor AGENTTEAMS_WORKERS 一致', () => {
  for (const n of ['mergepilot-leader', 'mergepilot-reviewer', 'mergepilot-fixer', 'mergepilot-verifier']) {
    assert.ok(src.includes(`'${n}|`), `安装入口缺 worker 定义: ${n}`);
  }
});
