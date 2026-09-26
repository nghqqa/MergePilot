# Audit and Evidence

## Storage
| Data | Store | Persistence |
|---|---|---|
| Skill receipts | PG skill_receipt_outbox | Persistent |
| Gate decisions | PG skill_gate_audit | Persistent |
| Approval tickets | PG approval.tickets | Persistent |
| A-chain retrievals | JSONL + MinIO | Persistent |
| Fixer/Verifier artifacts | Temp workspace + MinIO | Session-level |

## Queries
```sql
SELECT skill_name, status, payload->>'run_id', payload->>'integrity',
       payload->>'binding_status', created_at
FROM skill_receipt_outbox ORDER BY created_at DESC LIMIT 20;

SELECT run_id, decision, created_at FROM skill_gate_audit ORDER BY created_at DESC;

SELECT ticket_id, run_id, repo_id, action, status, approval_expires_at
FROM approval.tickets ORDER BY created_at DESC;
```

## Integrity
- Receipt integrity: OK / CONFLICT / SINK_WRITE_FAILED / LEDGER_CORRUPT
- Semantic idempotency: same invocation_id + same content = no-op
- Same invocation_id + different content = CONFLICT (original preserved)
- A-chain audit: five fields (snapshot_id / query_hash / source_refs / service_state / run_id)
