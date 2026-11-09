-- OpenTelemetry trace id (32 hex chars) of the lane event that wrote the row; NULL when tracing was off.
ALTER TABLE events ADD COLUMN trace_id TEXT;
