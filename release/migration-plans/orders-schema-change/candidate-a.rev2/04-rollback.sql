-- 04-rollback.sql — reverse candidate-a rev2 (only while no post-migration writes depend on the constraints).
BEGIN;
ALTER TABLE payments DROP CONSTRAINT IF EXISTS uq_payments_order_id;
ALTER TABLE orders ALTER COLUMN customer_id DROP NOT NULL;
ALTER TABLE orders ALTER COLUMN customer_id DROP DEFAULT;
-- restore archived duplicate payments
INSERT INTO payments SELECT * FROM payments_dedup_archive ON CONFLICT (payment_id) DO NOTHING;
-- restore the pre-backfill customer_id values (NULL) recorded in the audit table
UPDATE orders o SET customer_id = a.old_customer_id FROM orders_backfill_audit a WHERE a.order_id = o.order_id;
COMMIT;
-- keep orders_backfill_audit / payments_dedup_archive until the rollback is confirmed, then drop them explicitly.
