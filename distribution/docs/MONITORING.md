# Monitoring

## Health Check
```bash
curl http://127.0.0.1:4730/api/health
# {"ok":true,"service":"mergepilot-console",...}
```

## Docker Health
All services have healthchecks (30s interval, 3 failures = unhealthy).
```bash
docker compose ps
```

## Key Metrics
| Metric | Check | Alert |
|---|---|---|
| Health | HTTP 200 | Non-200 |
| PG | POSTGRESQL_LIVE | BACKEND_ERROR |
| Allowlist | 403 on foreign | Non-403 |
| A-chain | a_chain_disabled | Unexpected enable |

## Logs
```bash
docker compose logs -f console
docker compose logs -f pg
```
