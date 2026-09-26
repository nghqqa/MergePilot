# Rollback

## Image Rollback
```bash
docker compose down
docker tag mp-canonical-console:rollback-prev mp-canonical-console:candidate
docker compose up -d
curl http://127.0.0.1:4730/api/health  # verify 200
```

## A-Chain Disable (without image rollback)
Remove MERGEPILOT_ORG_RAG_A_CHAIN from .env, then:
```bash
docker compose up -d --force-recreate console
# verify: /api/rag/org-search -> a_chain_disabled
```

## Verification Checklist
- [ ] health = 200
- [ ] login works
- [ ] allowlist 403 effective
- [ ] two PRs data consistent
- [ ] A-chain state correct
- [ ] audit readable

## Versions
- Current: sha256:1056df76...
- Rollback: sha256:41bd3029... (mp-canonical-console:rollback-prev)
