-- M4: ordering. 'pending' already allows kind 'scope' (see 001), so it is not rebuilt.

-- The Telegram message holding an order's bill card, so the card can be edited later.
ALTER TABLE orders ADD COLUMN card_message_id INTEGER;

-- Short id of the lane event that caused a row, to follow one turn through logs and the audit trail.
ALTER TABLE events ADD COLUMN turn_id TEXT;
