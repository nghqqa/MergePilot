// console/backend/test/distribution-ports.test.mjs — PHASE0A 发行配置校验（B 项）：
// 全部发行/部署 compose 的宿主端口映射必须显式绑定回环（127.0.0.1），
// 防止 console（及未来任何发布端口的服务）回退到 Docker 默认的 0.0.0.0 全网卡发布。
// 背景：distribution compose 曾以 "${CONSOLE_PORT:-4730}:4730" 默认发布全网卡，
// 与其自身注释及 docs/DOCKER-DEPLOY.md 宣称的 loopback 相悖（复核报告 M-17）。
// 解析器为无依赖定向扫描（三份 compose 的 ports 均为扁平字符串映射形式）。
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..', '..');

const FILES = [
  'distribution/docker/docker-compose.yml', // PHASE0A 修复对象（console 默认回环）
  'distribution/docker/fxv-pilot/docker-compose.yml', // 受控试点栈（回归保护）
  'deploy/local-rag-trial/docker-compose.yml', // 本地试验栈（回归保护）
];

// 提取 YAML 中全部 ports 映射项（支持块状列表与单行数组两种既有形式）
function extractPortMappings(yaml) {
  const out = [];
  const lines = yaml.split(/\r?\n/);
  let inPorts = false;
  let portsIndent = 0;
  for (const raw of lines) {
    const line = raw.replace(/#.*$/, ''); // 去注释（ports 项均为简单字符串，无 # 字面量）
    if (!line.trim()) continue;
    const indent = line.length - line.trimStart().length;
    if (inPorts && indent <= portsIndent) inPorts = false; // 块结束（回到同级或更浅）
    const m = line.match(/^(\s*)ports:\s*(.*)$/);
    if (m) {
      portsIndent = m[1].length;
      const inline = m[2].trim();
      if (inline.startsWith('[')) {
        // 单行数组形式：ports: ["127.0.0.1:48440:4730"]
        for (const e of inline.replace(/^\[|\]$/g, '').split(',').map((s) => s.trim()).filter(Boolean)) {
          out.push(e.replace(/^["']|["']$/g, ''));
        }
        inPorts = false;
        continue;
      }
      inPorts = true;
      continue;
    }
    if (inPorts) {
      const item = line.trim();
      if (item.startsWith('- ')) out.push(item.slice(2).trim().replace(/^["']|["']$/g, ''));
    }
  }
  return out;
}

test('发行/部署 compose 宿主端口映射全部显式绑定回环（不得回退 0.0.0.0）', () => {
  for (const rel of FILES) {
    const file = path.join(ROOT, rel);
    assert.ok(fs.existsSync(file), `${rel} 必须存在（发行配置校验对象缺失=校验失效）`);
    const mappings = extractPortMappings(fs.readFileSync(file, 'utf8'));
    assert.ok(mappings.length > 0, `${rel} 未解析到任何 ports 映射（解析器与文件形态漂移，须修测试而非放过）`);
    for (const mp of mappings) {
      assert.match(
        mp,
        /^(127\.0\.0\.1|localhost):/,
        `${rel} 端口映射 "${mp}" 必须显式绑定回环（PHASE0A：禁止 0.0.0.0/全网卡默认发布）`,
      );
    }
  }
});
