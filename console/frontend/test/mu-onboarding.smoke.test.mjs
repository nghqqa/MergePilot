// mu-onboarding.smoke.test.mjs — Beta onboarding 流程冒烟。
import test from 'node:test';
import assert from 'node:assert/strict';
import { after } from 'node:test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import React, { act } from 'react';
import TestRenderer from 'react-test-renderer';
import { MemoryRouter } from 'react-router-dom';
import { Window } from 'happy-dom';

const dirname_ = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND = path.resolve(dirname_, '..');
const toFwd = (p) => p.split(path.sep).join('/');

const domWindow = new Window();
if (!globalThis.window) globalThis.window = domWindow;
for (const k of ['HTMLElement', 'SVGElement', 'ShadowRoot', 'Element', 'Node', 'Document',
  'MouseEvent', 'KeyboardEvent', 'Event', 'CustomEvent', 'DOMRect', 'ResizeObserver', 'MutationObserver']) {
  if (domWindow[k] && !globalThis[k]) globalThis[k] = domWindow[k];
}
globalThis.document = domWindow.document;
globalThis.navigator ??= domWindow.navigator;
globalThis.getComputedStyle ??= domWindow.getComputedStyle.bind(domWindow);
globalThis.matchMedia ??= domWindow.matchMedia.bind(domWindow);
globalThis.requestAnimationFrame ??= domWindow.requestAnimationFrame.bind(domWindow);

const SESSION = { user: { name: 'smoke' }, repos: [] };
let ROUTES = {};
globalThis.fetch = async (input) => {
  const u = new URL(typeof input === 'string' ? input : input.url, 'http://smoke.local');
  const route = ROUTES[u.pathname];
  if (!route) return new Response(JSON.stringify({ error: { reason: 'unexpected:' + u.pathname } }), { status: 404 });
  const [status, body] = route(u);
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
};

async function loadApp() {
  const outDir = path.join(FRONTEND, 'node_modules', '.mu-onb-smoke');
  fs.mkdirSync(outDir, { recursive: true });
  const entry = path.join(outDir, 'entry.mjs');
  const bundle = path.join(outDir, 'bundle.cjs');
  fs.writeFileSync(entry, 'import App from ' + JSON.stringify(toFwd(path.join(FRONTEND, 'src/App.jsx'))) + ';\nexport { App };');
  await build({ entryPoints: [entry], bundle: true, format: 'cjs', platform: 'node', outfile: bundle,
    jsx: 'automatic', external: ['react', 'react-dom', 'scheduler', 'react-router-dom'],
    define: { 'process.env.NODE_ENV': '"test"' }, logLevel: 'silent' });
  const mod = await import(pathToFileURL(bundle).href);
  return mod.App ?? mod.default?.App ?? mod.default ?? mod;
}

const COMMON = {
  '/api/auth/session': () => [200, SESSION],
  '/api/health': () => [200, { service: 'console', data_mode: 'fixture',
    sources: { primary: 'console-pg', console_pg: { available: true, base: '/pg' } } }],
};

async function renderRoute(route) {
  const App = await loadApp();
  let renderer;
  await act(async () => {
    renderer = TestRenderer.create(React.createElement(MemoryRouter, { initialEntries: [route] }, React.createElement(App)));
  });
  for (let i = 0; i < 20; i++) await act(async () => { await Promise.resolve(); });
  return { renderer, json: () => JSON.stringify(renderer.toJSON()) };
}

after(() => { try { domWindow.close(); } catch {} setTimeout(() => process.exit(0), 300); });

test('Onboarding Steps + GitHub App unconfigured guide', async () => {
  ROUTES = {
    ...COMMON,
    '/api/mu/session': () => [200, {
      user: { user_id: 'u-1', login: 'alice' }, tenant: { tenant_id: 't-1', slug: 'default', display_name: 'Default' },
      role: 'maintainer', actions: ['read_repository', 'manage_repository_binding'], memberships: [],
    }],
    '/api/mu/auth/providers': () => [200, { github: { configured: false } }],
    '/api/mu/members': () => [200, { members: [] }],
    '/api/mu/repositories': () => [200, { repositories: [] }],
    '/api/mu/github/app/status': () => [200, { configured: false, reason: 'github_app_not_configured' }],
    '/api/mu/github/installations': () => [200, { installations: [] }],
  };
  const { renderer, json } = await renderRoute('/multiuser');
  try {
    const text = json();
    assert.ok(text.includes('GitHub App') || text.includes('not_configured'), 'App unconfigured visible');
    assert.ok(text.includes('BETA-GUIDE'), 'Guide link visible');
    assert.ok(!text.includes('unexpected:'), 'No unexpected API paths');
  } finally { await act(async () => { renderer.unmount(); }); }
});

test('Onboarding complete: bound repos + PR review available', async () => {
  ROUTES = {
    ...COMMON,
    '/api/mu/session': () => [200, {
      user: { user_id: 'u-1', login: 'alice' }, tenant: { tenant_id: 't-1', slug: 'default', display_name: 'Default' },
      role: 'maintainer', actions: ['read_repository', 'read_pull_request', 'request_review', 'decide_review', 'manage_repository_binding'],
      memberships: [],
    }],
    '/api/mu/auth/providers': () => [200, { github: { configured: true } }],
    '/api/mu/members': () => [200, { members: [{ membership_id: 'm-1', login: 'alice', role: 'maintainer', state: 'active', created_at: '2026-09-28T10:00:00Z' }] }],
    '/api/mu/repositories': () => [200, { repositories: [
      { repo_id: 'r-1', owner: 'acme', name: 'app', provider_repo_id: '9001', binding_id: 'b-1', installation_state: 'active' },
    ] }],
    '/api/mu/github/app/status': () => [200, { configured: true, app_id: 999, permissions: ['contents:read'], events: ['pull_request'] }],
    '/api/mu/github/installations': () => [200, { installations: [{ installation_id: 7001, account_login: 'acme', suspended: false, revoked: false }] }],
  };
  const { renderer, json } = await renderRoute('/multiuser');
  try {
    const text = json();
    assert.ok(text.includes('acme'), 'Bound repo visible');
    assert.ok(text.includes('PR') || text.includes('review'), 'PR review panel visible');
    assert.ok(!text.includes('unexpected:'), 'No unexpected API paths');
  } finally { await act(async () => { renderer.unmount(); }); }
});
