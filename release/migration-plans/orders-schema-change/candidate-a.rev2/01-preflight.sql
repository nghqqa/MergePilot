-- 01-preflight.sql — compatibility checks BEFORE running the migration (read-only).
-- Abort the rollout if any value differs from the verified baseline profile.
SELECT 'null_customer_id' AS check, count(*) AS value, 137 AS verified_baseline FROM orders WHERE customer_id IS NULL
UNION ALL SELECT 'duplicate_payment_rows', count(*) - count(DISTINCT order_id), 2 FROM payments
UNION ALL SELECT 'legacy_owner_coverage', count(*), 120 FROM legacy_order_owner l JOIN orders o USING (order_id) WHERE o.customer_id IS NULL
UNION ALL SELECT 'sentinel_customer_absent', count(*), 0 FROM customers WHERE customer_id = 0
UNION ALL SELECT 'uq_payments_order_id_absent', count(*), 0 FROM pg_constraint WHERE conname = 'uq_payments_order_id';
