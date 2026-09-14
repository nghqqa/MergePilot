-- candidate-a · revision 2 — "先回填、归档重复、再加约束，并保留旧程序兼容"
-- Same candidate, revised after revision 1 failed on historical data.
--   1. recover owners from legacy_order_owner; unresolvable rows get the
--      sentinel customer 0 (UNKNOWN_LEGACY) and are written to an audit table
--      so the business can follow up — no silent data invention.
--   2. duplicate payments are archived (not deleted) before UNIQUE is added.
--   3. NOT NULL is added together with DEFAULT 0 so workers still running the
--      old code (which omits customer_id) keep working during the rollout
--      window; the new code always supplies a real customer_id.
BEGIN;

INSERT INTO customers(customer_id, name) VALUES (0, 'UNKNOWN_LEGACY')
ON CONFLICT (customer_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS orders_backfill_audit (
  order_id        BIGINT PRIMARY KEY,
  old_customer_id BIGINT,
  new_customer_id BIGINT NOT NULL,
  source          TEXT NOT NULL CHECK (source IN ('legacy_order_owner','sentinel')),
  applied_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO orders_backfill_audit(order_id, old_customer_id, new_customer_id, source)
SELECT o.order_id, o.customer_id, COALESCE(l.customer_id, 0),
       CASE WHEN l.customer_id IS NULL THEN 'sentinel' ELSE 'legacy_order_owner' END
FROM orders o
LEFT JOIN legacy_order_owner l ON l.order_id = o.order_id
WHERE o.customer_id IS NULL
ON CONFLICT (order_id) DO NOTHING;

UPDATE orders o
   SET customer_id = a.new_customer_id
  FROM orders_backfill_audit a
 WHERE a.order_id = o.order_id AND o.customer_id IS NULL;

CREATE TABLE IF NOT EXISTS payments_dedup_archive (LIKE payments INCLUDING ALL);

INSERT INTO payments_dedup_archive
SELECT p.* FROM payments p
WHERE p.payment_id IN (
  SELECT payment_id FROM (
    SELECT payment_id, row_number() OVER (PARTITION BY order_id ORDER BY payment_id) AS rn
    FROM payments) t
  WHERE t.rn > 1)
ON CONFLICT (payment_id) DO NOTHING;

DELETE FROM payments WHERE payment_id IN (SELECT payment_id FROM payments_dedup_archive);

ALTER TABLE orders ALTER COLUMN customer_id SET DEFAULT 0;
ALTER TABLE orders ALTER COLUMN customer_id SET NOT NULL;
ALTER TABLE payments ADD CONSTRAINT uq_payments_order_id UNIQUE (order_id);

COMMIT;
