-- Network-wide detection: retention pruning of the payment memory.
CREATE INDEX IF NOT EXISTS ix_hist_last_seen ON historical_recipients (last_seen);
CREATE INDEX IF NOT EXISTS ix_tx_source_ts ON transactions (source, block_timestamp);
