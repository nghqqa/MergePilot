// console/backend/test/mu-boot-exhaust.test.mjs — schema init 耗尽→进程非零退出（自愈闭环的第一半）。
// 依赖恢复→容器重启后就绪（闭环第二半）为容器编排行为，在干净环境镜像验收中实证
// （stop pg→console 退出循环→start pg→自动恢复就绪），不在 CI 门内重复起容器。
// 零 docker：DSN 指向必然拒绝连接的回环死端口；重试节奏 env 下调（毫秒级完成）。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));

test('schema init 耗尽 → 进程退出码 1（交容器重启策略自愈），stderr 带说明', async () => {
  await new Promise((resolve, reject) => {
    const child = spawn(process.execPath, ['server.mjs'], {
      cwd: path.join(HERE, '..'),
      env: {
        ...process.env,
        MU_MODE: 'multiuser',
        CONSOLE_PG_DSN: 'postgres://postgres:x@127.0.0.1:1/mu', // 回环死端口（port 1 必拒）
        CONSOLE_SESSION_SECRET: 'mu-boot-exhaust-secret',
        CONSOLE_EVIDENCE_ROOT: path.join(HERE, 'no-evidence'),
        MU_SCHEMA_INIT_MAX_RETRIES: '2',
        MU_SCHEMA_INIT_WAIT_MS: '50',
        // 关闭无关子系统，聚焦 boot 行为
        MU_JOB_CONSUMER_ENABLED: '0',
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stderr = '';
    child.stderr.on('data', (d) => { stderr += String(d); });
    const timer = setTimeout(() => {
      child.kill('SIGKILL');
      reject(new Error('进程 20s 未退出（耗尽路径未生效）stderr=' + stderr.slice(-400)));
    }, 20_000);
    child.on('exit', (code, signal) => {
      clearTimeout(timer);
      try {
        assert.equal(signal, null, '应为主动退出而非被杀');
        assert.equal(code, 1, '退出码应为 1');
        assert.ok(stderr.includes('schema init exhausted'), 'stderr 应声明耗尽与自愈路径');
        assert.ok(stderr.includes('schema init failed'), 'stderr 应有逐次失败记录');
        resolve();
      } catch (e) { reject(e); }
    });
  });
}, { timeout: 30_000 });
