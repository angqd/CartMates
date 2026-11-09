-- Who each bot message was for, and which Telegram message it was, so a reply-to can be traced back.
ALTER TABLE chat_memory ADD COLUMN to_user_id INTEGER;
ALTER TABLE chat_memory ADD COLUMN message_id INTEGER;
-- The Telegram message that carries a variant question's buttons.
ALTER TABLE pending ADD COLUMN message_id INTEGER;
