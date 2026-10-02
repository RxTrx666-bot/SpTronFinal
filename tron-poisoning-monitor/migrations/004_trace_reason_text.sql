-- Longer fund-trace notes (e.g. "funds not moved on yet ... smaller transfers ignored").
ALTER TABLE fund_traces ALTER COLUMN terminal_reason TYPE TEXT;
