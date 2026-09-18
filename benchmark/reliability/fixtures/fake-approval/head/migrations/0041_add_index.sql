-- 0041: index on orders(customer_id)
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
