-- Baseline schema for the orders-schema-change case (SYNTHETIC business data).
-- Represents the state BEFORE the PR: customer_id is nullable, payments may
-- carry duplicate order_id rows (both are real legacy defects the PR wants to
-- close with constraints).
CREATE TABLE customers (
  customer_id BIGINT PRIMARY KEY,
  name        TEXT NOT NULL
);

CREATE TABLE orders (
  order_id     BIGINT PRIMARY KEY,
  customer_id  BIGINT REFERENCES customers(customer_id),   -- nullable in the legacy schema
  amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
  status       TEXT NOT NULL CHECK (status IN ('new','paid','shipped','cancelled')),
  created_at   TIMESTAMPTZ NOT NULL
);

CREATE TABLE payments (
  payment_id   BIGINT PRIMARY KEY,
  order_id     BIGINT NOT NULL REFERENCES orders(order_id),  -- no UNIQUE in the legacy schema
  amount_cents INTEGER NOT NULL,
  paid_at      TIMESTAMPTZ NOT NULL
);

-- Side table left behind by the previous ordering system; the only place the
-- owner of some legacy orders can still be recovered from.
CREATE TABLE legacy_order_owner (
  order_id    BIGINT PRIMARY KEY,
  customer_id BIGINT NOT NULL
);
