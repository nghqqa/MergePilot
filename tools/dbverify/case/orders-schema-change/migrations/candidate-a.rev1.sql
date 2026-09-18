-- candidate-a · revision 1 — "直接加约束"
-- The PR's first migration: enforce the two invariants the new code assumes.
-- Passes on an EMPTY database; fails on the historical baseline (137 NULL
-- customer_id rows, 2 duplicate payment rows).
BEGIN;
ALTER TABLE orders ALTER COLUMN customer_id SET NOT NULL;
ALTER TABLE payments ADD CONSTRAINT uq_payments_order_id UNIQUE (order_id);
COMMIT;
