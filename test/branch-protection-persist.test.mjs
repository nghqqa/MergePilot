// test/branch-protection-persist.test.mjs — 保护探测结果回写 PR 行（rc8.1 修复回归）。
// 缺陷史：webhook 消费时 branch_protection_status 硬编码 'unknown' 且探测结果从不落库，
// UI 永远显示「保护状态未知」；本修复在 executeEventSync 两处消费点把探测结果回写 PR 行。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, '..');
const providerSrc = fs.readFileSync(path.join(ROOT, 'console/backend/lib/multiuser/ghprovider.mjs'), 'utf8');
const apiSrc = fs.readFileSync(path.join(ROOT, 'console/backend/lib/multiuser/api.mjs'), 'utf8');

test('纯函数映射：confirmed→known_clean，其余如实 unknown（fail-closed）', async () => {
  const mod = await import(pathToFileURL(path.join(ROOT, 'console/backend/lib/multiuser/ghprovider.mjs')).href);
  const f = mod.branchProtectionStatusFromProbe;
  assert.equal(f({ configured: true, required_checks: 1 }), 'known_clean');
  assert.equal(f({ configured: false }), 'unknown');
  assert.equal(f({}), 'unknown');
  assert.equal(f(null), 'unknown');
  assert.equal(f(undefined), 'unknown');
});

test('消费点契约：executeEventSync 两处消费点都在审查成功后回写（UPDATE+helper）', () => {
  const sites = [...apiSrc.matchAll(/if \(rv\.ok && rv\.protection != null\) \{/g)];
  assert.equal(sites.length, 2, 'gitee 与 github 两个消费点都必须回写');
  for (const m of sites) {
    const seg = apiSrc.slice(m.index, m.index + 700);
    assert.ok(seg.includes('branchProtectionStatusFromProbe(rv.protection)'), '必须经纯函数映射（不得内联三元）');
    assert.ok(seg.includes('UPDATE mu.pull_request SET branch_protection_status=$1'), '必须落 PR 行');
    assert.ok(seg.includes('provider_pr_number=$4 AND head_sha=$5'), '必须钉 pr+head（防串写）');
  }
});

test('值域一致：helper 输出 ⊆ api 白名单（unknown/known_clean/blocked，零迁移）', async () => {
  const mod = await import(pathToFileURL(path.join(ROOT, 'console/backend/lib/multiuser/ghprovider.mjs')).href);
  const f = mod.branchProtectionStatusFromProbe;
  const whitelist = new Set(['unknown', 'known_clean', 'blocked']);
  for (const input of [{ configured: true }, { configured: false }, {}, null, undefined, { configured: 'yes' }]) {
    assert.ok(whitelist.has(f(input)), `输出必须在白名单内：${String(f(input))}`);
  }
  assert.ok(apiSrc.includes("['unknown', 'known_clean', 'blocked']"), 'api 白名单必须在场');
});
