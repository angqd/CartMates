-- /address: the address an order was proposed for, so confirm can refuse a stale one.
ALTER TABLE orders ADD COLUMN address_id TEXT;
