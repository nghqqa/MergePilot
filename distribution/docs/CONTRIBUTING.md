# Contributing

## Development
```bash
# Backend (zero-framework Node.js)
cd console/backend && node server.mjs

# Frontend (Vite)
cd console/frontend && npm install && npm run dev

# Tests
node --test console/backend/test/*.test.mjs
```

## Code Standards
- Backend: ESM, zero third-party deps (except pg)
- Frontend: React 18 + antd 5
- CSS: BEM naming + CSS variables

## Commit Convention
- Prefix: feat / fix / docs / chore / test
- Must include verification notes

## Security Requirements
- New APIs must pass allowlist
- New side-effect methods must validate CSRF
- No secrets in code
- No bypass of fail-closed paths
