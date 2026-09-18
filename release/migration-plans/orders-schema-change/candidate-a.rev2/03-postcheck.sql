-- 03-postcheck.sql — the verified assertions (same queries the trial run evaluated).
-- no_null_customer_id (expect 0)
SELECT count(*) FROM orders WHERE customer_id IS NULL;
-- payments_order_id_unique (expect 0)
SELECT count(*) - count(DISTINCT order_id) FROM payments;
-- not_null_constraint_present (expect NO)
SELECT is_nullable FROM information_schema.columns WHERE table_name = 'orders' AND column_name = 'customer_id';
-- unique_constraint_present (expect 1)
SELECT count(*) FROM pg_constraint WHERE conname = 'uq_payments_order_id';
-- orders_row_count_preserved (expect 10000)
SELECT count(*) FROM orders;
-- payments_preserved_incl_archive (expect 10002)
SELECT (SELECT count(*) FROM payments) + (SELECT count(*) FROM payments_dedup_archive);
-- backfill_fully_audited (expect 137)
SELECT count(*) FROM orders_backfill_audit;
-- sentinel_rows_flagged_for_follow_up (expect 17)
SELECT count(*) FROM orders_backfill_audit WHERE source = 'sentinel';
