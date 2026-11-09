-- UPI payment (DESIGN 7.3): how the order was paid for and Swiggy's payment tracking ids.
ALTER TABLE orders ADD COLUMN payment_method TEXT;
ALTER TABLE orders ADD COLUMN payment_json TEXT;
