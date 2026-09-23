-- Initial schema for the TRON USDT pattern monitor (PostgreSQL).
-- Generated from the Alembic migration with:  alembic upgrade head --sql
-- The application applies migrations automatically (AUTO_MIGRATE=true) or via:
--   python -m app.main migrate
-- Use this file only if you prefer to create the schema manually with psql.

BEGIN;

CREATE TABLE alembic_version (
    version_num VARCHAR(32) NOT NULL, 
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

-- Running upgrade  -> 0001

CREATE TABLE alerts (
    id BIGSERIAL NOT NULL, 
    dedup_key VARCHAR(200) NOT NULL, 
    transaction_hash VARCHAR(64), 
    alert_type VARCHAR(32) NOT NULL, 
    priority INTEGER NOT NULL, 
    sender VARCHAR(34), 
    recipient VARCHAR(34), 
    amount_raw BIGINT, 
    message_text TEXT NOT NULL, 
    status VARCHAR(16) NOT NULL, 
    attempts INTEGER NOT NULL, 
    next_attempt_at TIMESTAMP WITH TIME ZONE, 
    last_error TEXT, 
    sent_at TIMESTAMP WITH TIME ZONE, 
    telegram_message_id VARCHAR(64), 
    blockchain_event_time TIMESTAMP WITH TIME ZONE, 
    blockchain_detection_time TIMESTAMP WITH TIME ZONE, 
    processing_start_time TIMESTAMP WITH TIME ZONE, 
    processing_end_time TIMESTAMP WITH TIME ZONE, 
    telegram_send_start_time TIMESTAMP WITH TIME ZONE, 
    telegram_send_end_time TIMESTAMP WITH TIME ZONE, 
    total_detection_latency_ms BIGINT, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT uq_alerts_dedup_key UNIQUE (dedup_key)
);

CREATE INDEX ix_alerts_hash ON alerts (transaction_hash);

CREATE INDEX ix_alerts_pair ON alerts (sender, recipient);

CREATE INDEX ix_alerts_queue ON alerts (status, priority, id);

CREATE TABLE collector_state (
    key VARCHAR(64) NOT NULL, 
    value TEXT NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (key)
);

CREATE TABLE transactions (
    id BIGSERIAL NOT NULL, 
    transaction_hash VARCHAR(64) NOT NULL, 
    event_index INTEGER NOT NULL, 
    block_number BIGINT, 
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL, 
    sender VARCHAR(34) NOT NULL, 
    recipient VARCHAR(34) NOT NULL, 
    amount_raw BIGINT NOT NULL, 
    amount_usdt NUMERIC(38, 6) NOT NULL, 
    token_contract VARCHAR(34) NOT NULL, 
    status VARCHAR(16) NOT NULL, 
    detected_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    confirmed_at TIMESTAMP WITH TIME ZONE, 
    processed BOOLEAN NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT uq_transactions_hash_event UNIQUE (transaction_hash, event_index)
);

CREATE INDEX ix_transactions_hash ON transactions (transaction_hash);

CREATE INDEX ix_transactions_pair_ts ON transactions (sender, recipient, timestamp);

CREATE INDEX ix_transactions_recipient ON transactions (recipient);

CREATE INDEX ix_transactions_sender ON transactions (sender);

CREATE INDEX ix_transactions_status_ts ON transactions (status, timestamp);

CREATE INDEX ix_transactions_timestamp ON transactions (timestamp);

CREATE INDEX ix_transactions_unprocessed ON transactions (processed, detected_at);

CREATE TABLE wallet_pairs (
    id BIGSERIAL NOT NULL, 
    sender VARCHAR(34) NOT NULL, 
    recipient VARCHAR(34) NOT NULL, 
    total_transfers BIGINT NOT NULL, 
    total_volume_raw NUMERIC(40, 0) NOT NULL, 
    average_amount_raw NUMERIC(40, 0) NOT NULL, 
    median_amount_raw BIGINT, 
    smallest_amount_raw BIGINT NOT NULL, 
    largest_amount_raw BIGINT NOT NULL, 
    first_seen_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    sequence_count INTEGER NOT NULL, 
    needs_analysis BOOLEAN NOT NULL, 
    last_analyzed_at TIMESTAMP WITH TIME ZONE, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT uq_wallet_pairs_pair UNIQUE (sender, recipient)
);

CREATE INDEX ix_wallet_pairs_needs_analysis ON wallet_pairs (needs_analysis);

CREATE INDEX ix_wallet_pairs_recipient ON wallet_pairs (recipient);

CREATE INDEX ix_wallet_pairs_sender ON wallet_pairs (sender);

CREATE TABLE watchlist (
    id BIGSERIAL NOT NULL, 
    sender VARCHAR(34) NOT NULL, 
    recipient VARCHAR(34) NOT NULL, 
    status VARCHAR(16) NOT NULL, 
    status_reason TEXT, 
    confidence VARCHAR(8) NOT NULL, 
    confidence_score FLOAT NOT NULL, 
    pattern_strength FLOAT NOT NULL, 
    typical_test_amount_raw BIGINT NOT NULL, 
    median_test_amount_raw BIGINT NOT NULL, 
    test_amount_min_raw BIGINT NOT NULL, 
    test_amount_max_raw BIGINT NOT NULL, 
    match_low_raw BIGINT NOT NULL, 
    match_high_raw BIGINT NOT NULL, 
    typical_large_amount_raw BIGINT NOT NULL, 
    median_large_amount_raw BIGINT NOT NULL, 
    large_amount_min_raw BIGINT NOT NULL, 
    large_amount_max_raw BIGINT NOT NULL, 
    typical_test_to_large_ratio NUMERIC(30, 6) NOT NULL, 
    typical_followup_seconds BIGINT NOT NULL, 
    followup_window_seconds BIGINT NOT NULL, 
    successful_sequences INTEGER NOT NULL, 
    total_sequences INTEGER NOT NULL, 
    success_rate FLOAT NOT NULL, 
    last_test_transfer TIMESTAMP WITH TIME ZONE, 
    last_large_transfer TIMESTAMP WITH TIME ZONE, 
    last_sequence_at TIMESTAMP WITH TIME ZONE, 
    activated_at TIMESTAMP WITH TIME ZONE, 
    activation_count INTEGER NOT NULL, 
    pause_until TIMESTAMP WITH TIME ZONE, 
    manual_pause BOOLEAN NOT NULL, 
    model_json TEXT, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT uq_watchlist_pair UNIQUE (sender, recipient)
);

CREATE INDEX ix_watchlist_status ON watchlist (status);

CREATE TABLE pattern_sequences (
    id BIGSERIAL NOT NULL, 
    sender VARCHAR(34) NOT NULL, 
    recipient VARCHAR(34) NOT NULL, 
    test_transaction_id BIGINT, 
    large_transaction_id BIGINT, 
    test_tx_hash VARCHAR(64) NOT NULL, 
    large_tx_hash VARCHAR(64) NOT NULL, 
    test_amount_raw BIGINT NOT NULL, 
    large_amount_raw BIGINT NOT NULL, 
    amount_ratio NUMERIC(30, 6) NOT NULL, 
    test_timestamp TIMESTAMP WITH TIME ZONE NOT NULL, 
    large_timestamp TIMESTAMP WITH TIME ZONE NOT NULL, 
    time_difference_seconds BIGINT NOT NULL, 
    is_inlier BOOLEAN NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(large_transaction_id) REFERENCES transactions (id) ON DELETE SET NULL, 
    FOREIGN KEY(test_transaction_id) REFERENCES transactions (id) ON DELETE SET NULL, 
    CONSTRAINT uq_sequences_large_tx UNIQUE (large_transaction_id), 
    CONSTRAINT uq_sequences_test_tx UNIQUE (test_transaction_id)
);

CREATE INDEX ix_sequences_pair_ts ON pattern_sequences (sender, recipient, test_timestamp);

CREATE TABLE test_events (
    id BIGSERIAL NOT NULL, 
    watchlist_id BIGINT NOT NULL, 
    sender VARCHAR(34) NOT NULL, 
    recipient VARCHAR(34) NOT NULL, 
    transaction_id BIGINT, 
    transaction_hash VARCHAR(64) NOT NULL, 
    event_index INTEGER NOT NULL, 
    amount_raw BIGINT NOT NULL, 
    tx_timestamp TIMESTAMP WITH TIME ZONE NOT NULL, 
    detected_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    status VARCHAR(16) NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(transaction_id) REFERENCES transactions (id) ON DELETE SET NULL, 
    FOREIGN KEY(watchlist_id) REFERENCES watchlist (id) ON DELETE CASCADE, 
    CONSTRAINT uq_test_events_tx UNIQUE (transaction_hash, event_index)
);

CREATE INDEX ix_test_events_pair_status ON test_events (sender, recipient, status);

CREATE INDEX ix_test_events_status_expires ON test_events (status, expires_at);

CREATE TABLE followup_events (
    id BIGSERIAL NOT NULL, 
    test_event_id BIGINT NOT NULL, 
    sender VARCHAR(34) NOT NULL, 
    recipient VARCHAR(34) NOT NULL, 
    large_transaction_id BIGINT, 
    large_transaction_hash VARCHAR(64) NOT NULL, 
    large_event_index INTEGER NOT NULL, 
    test_amount_raw BIGINT NOT NULL, 
    large_amount_raw BIGINT NOT NULL, 
    amount_ratio NUMERIC(30, 6) NOT NULL, 
    time_difference_seconds BIGINT NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(large_transaction_id) REFERENCES transactions (id) ON DELETE SET NULL, 
    FOREIGN KEY(test_event_id) REFERENCES test_events (id) ON DELETE CASCADE, 
    CONSTRAINT uq_followup_large_tx UNIQUE (large_transaction_hash, large_event_index), 
    CONSTRAINT uq_followup_test_event UNIQUE (test_event_id)
);

CREATE INDEX ix_followup_pair ON followup_events (sender, recipient);

INSERT INTO alembic_version (version_num) VALUES ('0001') RETURNING alembic_version.version_num;

COMMIT;

