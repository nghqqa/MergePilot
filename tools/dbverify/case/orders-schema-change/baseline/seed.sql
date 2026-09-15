-- Deterministic SYNTHETIC seed (no RNG, no clock): 500 customers, 10,000 orders,
-- 10,002 payments. Historical defects planted on purpose:
--   * orders 1..137 have customer_id NULL            (137 rows — blocks SET NOT NULL)
--   * payments 10001/10002 duplicate orders 500/777  (2 rows  — blocks UNIQUE(order_id))
--   * legacy_order_owner can recover owners for orders 1..120 only (17 stay unknown)
INSERT INTO customers(customer_id, name)
SELECT g, 'customer-' || g FROM generate_series(1, 500) g;

INSERT INTO orders(order_id, customer_id, amount_cents, status, created_at)
SELECT g,
       CASE WHEN g <= 137 THEN NULL ELSE ((g * 7) % 500) + 1 END,
       100 + (g % 900),
       (ARRAY['new','paid','shipped','cancelled'])[(g % 4) + 1],
       TIMESTAMPTZ '2025-01-01 00:00:00+00' + (g || ' minutes')::interval
FROM generate_series(1, 10000) g;

INSERT INTO payments(payment_id, order_id, amount_cents, paid_at)
SELECT g, g, 100 + (g % 900), TIMESTAMPTZ '2025-01-01 00:10:00+00' + (g || ' minutes')::interval
FROM generate_series(1, 10000) g;
INSERT INTO payments(payment_id, order_id, amount_cents, paid_at) VALUES
  (10001, 500, 600, TIMESTAMPTZ '2025-01-01 08:30:00+00'),
  (10002, 777, 877, TIMESTAMPTZ '2025-01-01 13:07:00+00');

INSERT INTO legacy_order_owner(order_id, customer_id)
SELECT g, ((g * 3) % 500) + 1 FROM generate_series(1, 120) g;
