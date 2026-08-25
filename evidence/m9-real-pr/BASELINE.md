# M9-A Baseline (dev machine)

- Release baseline: v0.1.0-preview.3 (tag -> 379744d, main)
- Verdict input: SAME_MACHINE_ACCEPTED / EXTERNAL_BLOCKED
- Tests at baseline: 2246 passed / 20 skipped
- Upstream review inputs: fix/m9-pgvector-pin-and-checksums (adopted after review),
  m9/evidence-external-realcase @ c20e693
- Truth boundaries (unchanged): application_integration_verified=false,
  database_verified=false, production_verified=false,
  revision_producer_contract=NOT_VERIFIED, audit_producer_contract=NOT_VERIFIED,
  direct_routing_verified=false
- This round: dev machine only. No external physical machine acceptance claimed.
