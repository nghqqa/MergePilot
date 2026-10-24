// test/agentteams-install-helpers.test.mjs — 安装器请求助手行为回归（CI 门内）。
// 防回归目标（#520 波事故）：sh -c 参数契约——token 必须进 Authorization 头、
// 不进 URL、不进输出。方法：从 install-agentteams-full.sh 原样提取 api() 的
// 内层 sh -c 脚本，用非秘密占位值按生产 argv 约定（$0=占位名 $1=token $2+=URL）
// 执行，stub curl 捕获 argv 断言。执行的是脚本内原文（非复制品）。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.join(HERE, '..', 'deploy', 'agentteams', 'qwenpaw-v124', 'install-agentteams-full.sh');

function extractInnerSh() {
  const src = fs.readFileSync(SRC, 'utf8');
  const line = src.match(/^api\(\) \{.*$/m);
  assert.ok(line, 'api() 定义未找到');
  assert.ok(line[0].includes('TOK=$1; shift;'), '必须 $1 读 token 并 shift（防 $0 事故回归）');
  const afterShift = line[0].split('TOK=$1; shift;')[1] ?? '';
  assert.ok(!afterShift.includes('$0'), 'shift 后不得再用 $0 传 token');
  // 内层脚本 = sh -c '<single-quoted>' 的字面内容
  const m = line[0].match(/sh -c '([^']*)'/);
  assert.ok(m, 'api() 内层 sh -c 脚本未提取到');
  return m[1];
}

test('api() 内层脚本：占位 token 进 Authorization 头、不进 URL/输出', () => {
  const inner = extractInnerSh();
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), 'at-helper-test-'));
  const PLACEHOLDER = 'PLACEHOLDER-NOT-A-SECRET-0123456789abcdef';
  fs.writeFileSync(path.join(sandbox, 'curl'), `#!/bin/bash
  printf 'ARG:%s\\n' "\$@" >> "\$CAPTURE"
  echo -n 200
  `, { mode: 0o755 });
  const capture = path.join(sandbox, 'captured.txt');
  const url = 'http://127.0.0.1:8090/api/v1/workers';
  // 生产 argv 约定：sh -c "$inner" _ "$TOK" "$url"（_=$0 占位）
  const out = execFileSync('sh', ['-c', inner, '_', PLACEHOLDER, url], {
    encoding: 'utf8',
    env: { ...process.env, PATH: `${sandbox}:${process.env.PATH}`, CAPTURE: capture },
  });
  assert.equal(out.trim(), '200', 'curl stub 应返回 200');
  const captured = fs.readFileSync(capture, 'utf8');
  const args = captured.split('\n').filter((l) => l.startsWith('ARG:')).map((l) => l.slice(4));
  // 1) Authorization 头=正确占位值
  assert.ok(args.includes(`Authorization: Bearer ${PLACEHOLDER}`),
    'Authorization 头必须携带占位 token，实际 argv：' + JSON.stringify(args));
  // 2) URL 参数只有目标地址（token 不以 URL 形态出现）
  const urlArgs = args.filter((a) => a.startsWith('http'));
  assert.deepEqual(urlArgs, [url], 'curl 只应收到目标 URL：' + JSON.stringify(args));
  // 3) 除 Authorization 头外无任何参数携带 token
  const leaked = args.filter((a) => a !== `Authorization: Bearer ${PLACEHOLDER}` && a.includes(PLACEHOLDER));
  assert.deepEqual(leaked, [], 'token 不得出现在 Authorization 之外的任何参数');
  // 4) token 不进 stdout 输出
  assert.ok(!out.includes(PLACEHOLDER), 'token 不得出现在输出');
});

test('api() 内层脚本：无/错 token 时授权头形状仍然确定（不为空串拼接）', () => {
  const inner = extractInnerSh();
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), 'at-helper-test2-'));
  fs.writeFileSync(path.join(sandbox, 'curl'), `#!/bin/bash
  printf 'ARG:%s\\n' "\$@" >> "\$CAPTURE"
  echo -n 401
  `, { mode: 0o755 });
  const capture = path.join(sandbox, 'captured.txt');
  // 空 token：头仍应为 "Authorization: Bearer "（形状确定，由服务端拒绝——客户端不静默吞）
  const out = execFileSync('sh', ['-c', inner, '_', '', 'http://127.0.0.1:8090/api/v1/workers'], {
    encoding: 'utf8',
    env: { ...process.env, PATH: `${sandbox}:${process.env.PATH}`, CAPTURE: capture },
  });
  assert.equal(out.trim(), '401');
  const captured = fs.readFileSync(capture, 'utf8');
  assert.ok(captured.includes('ARG:Authorization: Bearer '), '空 token 时仍发送 Bearer 形状头（由服务端 401）');
});
