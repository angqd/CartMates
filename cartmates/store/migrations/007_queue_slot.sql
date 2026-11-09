-- Short display number for queued lines. NULL once a line is ordered or removed.
ALTER TABLE queue_lines ADD COLUMN slot INTEGER;

UPDATE queue_lines SET slot = (
    SELECT COUNT(*) FROM queue_lines q WHERE q.status = 'queued' AND q.id <= queue_lines.id
) WHERE status = 'queued';

CREATE UNIQUE INDEX idx_queue_lines_slot ON queue_lines (slot) WHERE status = 'queued';
