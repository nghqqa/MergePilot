import io

p = 'console/backend/server.mjs'
s = io.open(p, encoding='utf-8').read()

# 1) Import the MU console adapter
old_imp = "import { muApi, getMuStore } from './lib/multiuser/api.mjs';"
new_imp = "import { muApi, getMuStore } from './lib/multiuser/api.mjs';\nimport { createMuConsoleApi } from './lib/mu-console-api.mjs';"
assert s.count(old_imp) == 1, 'muApi import'
s = s.replace(old_imp, new_imp)

# 2) Create adapter instance inside createConsole (after pool setup)
anchor = 'async function authGate(req) {'
adapter = '''// ── MU Console 数据适配层 ──
let _muApiInstance = null;
async function getMuConsoleApi() {
  if (process.env.MU_MODE !== 'multiuser') return null;
  if (!_muApiInstance) {
    const store = await getMuStore(process.env).catch(() => null);
    if (!store) return null;
    _muApiInstance = createMuConsoleApi({ pool: store.pool ?? store._pool });
  }
  return _muApiInstance;
}

async function authGate(req) {'''
assert s.count(anchor) == 1, 'authGate anchor'
s = s.replace(anchor, adapter)

# 3) Wire MU adapter into /api/overview
old_ov = """    if (p === '/api/overview' && req.method === 'GET') {
      const gate = await authGate(req);
      if (gate.denied) return sendJson(res, gate.denied, anonymousBody());
      const auth = gate.principal;
      const ov = await overviewState(auth.repos);
      return sendJson(res, 200, ov);"""
new_ov = """    if (p === '/api/overview' && req.method === 'GET') {
      const gate = await authGate(req);
      if (gate.denied) return sendJson(res, gate.denied, anonymousBody());
      if (gate.principal.authMode === 'multiuser') {
        const api = await getMuConsoleApi();
        if (api) return sendJson(res, 200, await api.overview(gate.principal.tenantId));
      }
      const auth = gate.principal.legacyAuth ?? gate.principal;
      const ov = await overviewState(auth.repos);
      return sendJson(res, 200, ov);"""
assert s.count(old_ov) == 1, 'overview: %d' % s.count(old_ov)
s = s.replace(old_ov, new_ov)

# 4) Wire MU adapter into /api/pulls (inside the allowlist block)
old_pulls = """      if (p === '/api/pulls') {
        const gate = await authGate(req);
        if (gate.denied) return sendJson(res, gate.denied, anonymousBody());
        const auth = gate.principal;
        const ov = await overviewState(auth.repos);
        return sendJson(res, 200, ov);"""
if s.count(old_pulls) == 0:
    # Try alternative pattern
    old_pulls = """      if (p === '/api/pulls') {
        const gate = await authGate(req);
        if (gate.denied) return sendJson(res, gate.denied, anonymousBody());"""
    # find the overviewState call after this
    idx = s.find(old_pulls)
    if idx > 0:
        # find the overviewState call after this position
        ov_idx = s.find('overviewState(', idx)
        end_idx = s.find('return sendJson(res, 200, ov);', ov_idx)
        if ov_idx > 0 and end_idx > 0:
            old_block = s[idx:end_idx + len('return sendJson(res, 200, ov);')]
            new_block = old_block.replace(
                'await overviewState(auth.repos)',
                '''(async () => {
          if (gate.principal.authMode === 'multiuser') {
            const api = await getMuConsoleApi();
            if (api) return api.overview(gate.principal.tenantId);
          }
          return overviewState(auth.repos);
        })()''')
            s = s.replace(old_block, new_block)
            print('pulls: adapted overviewState call')

# 5) Wire MU adapter into /api/runs
old_runs_gate = """    if (p === '/api/runs') {"""
new_runs_gate = """    if (p === '/api/runs' && process.env.MU_MODE === 'multiuser') {
      const gate = await authGate(req);
      if (gate.denied) return sendJson(res, gate.denied, anonymousBody());
      const api = await getMuConsoleApi();
      const runs = api ? await api.runs(gate.principal.tenantId) : [];
      return sendJson(res, 200, { runs });
    }
    if (p === '/api/runs') {"""
assert s.count(old_runs_gate) >= 1, 'runs gate'
s = s.replace(old_runs_gate, new_runs_gate, 1)

io.open(p, 'w', encoding='utf-8', newline='\n').write(s)

# Verify
remaining = s.count('tokenFromCookieHeader')
has_adapter = 'createMuConsoleApi' in s
has_authGate = 'async function authGate' in s
print(f'Done. remaining tokenFromCookieHeader={remaining}, adapter={has_adapter}, authGate={has_auth_gate}')
