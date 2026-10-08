// console/backend/test/agentteams-auth-proxy-config.test.mjs —
// deploy/agentteams-beta/auth-proxy/deploy-auth-proxy.cjs 配置渲染回归（零 Docker）。
//
// 锁定（2026-10-08 auth-proxy 落地）：模板渲染 fail-closed（凭据缺失/过短/占位符残留
// 一律抛错）；渲染产物保留入口拒绝与上游改写两条安全语义行；模板文件本身不含任何
// 疑似真实凭据的字面值。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const proxyDir = fileURLToPath(new URL('../../../deploy/agentteams-beta/auth-proxy/', import.meta.url));
const { renderProxyConfig } = require(`${proxyDir}deploy-auth-proxy.cjs`);

const ENTRY = 'fake-entry-credential-0123456789abcdef'; // 合成 fixture（secret-scan 豁免词 fake）
const UPSTREAM = 'fake-upstream-jwt-value-0123456789abcdef0123456789abcdef012345';
const TEMPLATE = fs.readFileSync(`${proxyDir}nginx-auth-proxy.conf.template`, 'utf8');

test('render: 三占位符全替换，无残留', () => {
  const out = renderProxyConfig(TEMPLATE, { entryCredential: ENTRY, upstreamToken: UPSTREAM, ctrlHost: 'ctrl-x' });
  assert.ok(!out.includes('@AT_PROXY_'), 'entry/upstream placeholder residue');
  assert.ok(!out.includes('@AT_CTRL_HOST@'), 'ctrl-host placeholder residue');
  assert.ok(out.includes(`Bearer ${ENTRY}`));
  assert.ok(out.includes(`Bearer ${UPSTREAM}`));
  assert.ok(out.includes('proxy_pass http://ctrl-x:8090'));
});

test('render: 安全语义行必须保留（入口 401 拒绝 + Authorization 改写）', () => {
  const out = renderProxyConfig(TEMPLATE, { entryCredential: ENTRY, upstreamToken: UPSTREAM });
  assert.match(out, /if \(\$http_authorization != \$expected_cred\) \{ return 401; \}/);
  assert.match(out, /proxy_set_header Authorization \$upstream_token;/);
});

test('render: fail-closed——凭据缺失或过短（<16）抛错', () => {
  assert.throws(() => renderProxyConfig(TEMPLATE, { upstreamToken: UPSTREAM }), /entryCredential/);
  assert.throws(() => renderProxyConfig(TEMPLATE, { entryCredential: ENTRY }), /upstreamToken/);
  assert.throws(() => renderProxyConfig(TEMPLATE, { entryCredential: 'short', upstreamToken: UPSTREAM }), /entryCredential/);
  assert.throws(() => renderProxyConfig(TEMPLATE, { entryCredential: ENTRY, upstreamToken: 'x'.repeat(15) }), /upstreamToken/);
});

test('render: 模板自身不含疑似真实凭据字面值', () => {
  assert.ok(TEMPLATE.includes('@AT_PROXY_ENTRY_CREDENTIAL@'));
  assert.ok(!/[0-9a-f]{32,}/i.test(TEMPLATE), 'template must not embed hex-credential-like literals');
  assert.ok(!/eyJ[A-Za-z0-9_-]{10,}/.test(TEMPLATE), 'template must not embed JWT-like literals');
});
