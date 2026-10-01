-- TRON address-poisoning monitor: initial PostgreSQL schema.
-- Applied automatically at startup (AUTO_MIGRATE=true) or with `python -m app.main migrate`.
-- Amounts are NUMERIC(78,0) integer base units (never floating point).

CREATE TABLE watched_wallets (
    id BIGSERIAL PRIMARY KEY,
    address VARCHAR(34) NOT NULL UNIQUE,
    address_hex VARCHAR(42) NOT NULL,
    label VARCHAR(120),
    status VARCHAR(16) NOT NULL DEFAULT 'ACTIVE',
    added_by BIGINT,
    added_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    history_status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    history_cursor_ms BIGINT,
    history_fingerprint TEXT,
    history_transfers_scanned INTEGER NOT NULL DEFAULT 0,
    history_started_at TIMESTAMPTZ,
    history_completed_at TIMESTAMPTZ,
    history_error TEXT,
    history_truncated BOOLEAN NOT NULL DEFAULT FALSE,
    poll_cursor_ms BIGINT,
    last_activity_at TIMESTAMPTZ
);
CREATE INDEX ix_watched_wallets_status ON watched_wallets (status);

CREATE TABLE historical_recipients (
    id BIGSERIAL PRIMARY KEY,
    victim_wallet VARCHAR(34) NOT NULL,
    recipient_wallet VARCHAR(34) NOT NULL,
    token_contract VARCHAR(34) NOT NULL,
    transaction_count INTEGER NOT NULL DEFAULT 0,
    total_amount NUMERIC(78,0) NOT NULL DEFAULT 0,
    first_seen TIMESTAMPTZ NOT NULL,
    last_seen TIMESTAMPTZ NOT NULL,
    largest_amount NUMERIC(78,0) NOT NULL DEFAULT 0,
    smallest_amount NUMERIC(78,0) NOT NULL DEFAULT 0,
    average_amount NUMERIC(78,0) NOT NULL DEFAULT 0,
    prefix_key VARCHAR(8) NOT NULL,
    suffix_key VARCHAR(8) NOT NULL,
    flagged_suspicious BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_hist_recipient UNIQUE (victim_wallet, recipient_wallet, token_contract)
);
CREATE INDEX ix_hist_victim_prefix ON historical_recipients (victim_wallet, token_contract, prefix_key);
CREATE INDEX ix_hist_victim_suffix ON historical_recipients (victim_wallet, token_contract, suffix_key);
CREATE INDEX ix_hist_victim_count ON historical_recipients (victim_wallet, token_contract, transaction_count);
CREATE INDEX ix_hist_recipient ON historical_recipients (recipient_wallet);

CREATE TABLE transactions (
    id BIGSERIAL PRIMARY KEY,
    transfer_key VARCHAR(64) NOT NULL UNIQUE,
    tx_hash VARCHAR(64) NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0,
    log_index INTEGER,
    token_contract VARCHAR(34) NOT NULL,
    from_address VARCHAR(34) NOT NULL,
    to_address VARCHAR(34) NOT NULL,
    amount NUMERIC(78,0) NOT NULL,
    block_number BIGINT,
    block_timestamp TIMESTAMPTZ NOT NULL,
    confirmation_status VARCHAR(16) NOT NULL,
    initiator_address VARCHAR(34),
    source VARCHAR(16) NOT NULL,
    analysis_status VARCHAR(16) NOT NULL,
    analysis_attempts INTEGER NOT NULL DEFAULT 0,
    analysis_error TEXT,
    detected_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX ix_tx_hash ON transactions (tx_hash);
CREATE INDEX ix_tx_from_ts ON transactions (from_address, block_timestamp);
CREATE INDEX ix_tx_to_ts ON transactions (to_address, block_timestamp);
CREATE INDEX ix_tx_analysis ON transactions (analysis_status, id);
CREATE INDEX ix_tx_confirmation ON transactions (confirmation_status, block_number);

CREATE TABLE address_similarity_matches (
    id BIGSERIAL PRIMARY KEY,
    transaction_id BIGINT REFERENCES transactions(id) ON DELETE CASCADE,
    victim_wallet VARCHAR(34) NOT NULL,
    legitimate_recipient VARCHAR(34) NOT NULL,
    suspicious_recipient VARCHAR(34) NOT NULL,
    prefix_match_length INTEGER NOT NULL,
    suffix_match_length INTEGER NOT NULL,
    prefix_similarity DOUBLE PRECISION NOT NULL,
    suffix_similarity DOUBLE PRECISION NOT NULL,
    overall_similarity DOUBLE PRECISION NOT NULL,
    positional_similarity DOUBLE PRECISION NOT NULL,
    similarity_score DOUBLE PRECISION NOT NULL,
    metrics JSON NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_similarity_tx_legit UNIQUE (transaction_id, victim_wallet, legitimate_recipient)
);
CREATE INDEX ix_similarity_suspicious ON address_similarity_matches (suspicious_recipient);

CREATE TABLE poisoning_events (
    id BIGSERIAL PRIMARY KEY,
    case_id VARCHAR(40) UNIQUE,
    event_type VARCHAR(32) NOT NULL,
    transaction_id BIGINT NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    tx_hash VARCHAR(64) NOT NULL,
    victim_wallet VARCHAR(34) NOT NULL,
    legitimate_recipient VARCHAR(34) NOT NULL,
    suspicious_recipient VARCHAR(34) NOT NULL,
    token_contract VARCHAR(34) NOT NULL,
    token_symbol VARCHAR(16) NOT NULL,
    token_decimals INTEGER NOT NULL,
    amount NUMERIC(78,0) NOT NULL,
    block_number BIGINT,
    block_timestamp TIMESTAMPTZ NOT NULL,
    confirmation_status VARCHAR(16) NOT NULL,
    similarity_score DOUBLE PRECISION NOT NULL,
    confidence INTEGER NOT NULL,
    fast_confidence INTEGER NOT NULL,
    score_breakdown JSON NOT NULL,
    legit_tx_count INTEGER NOT NULL,
    legit_total_amount NUMERIC(78,0) NOT NULL,
    suspicious_prior_tx_count INTEGER NOT NULL,
    poisoning_tx_observed VARCHAR(8) NOT NULL,
    forwarding_summary TEXT,
    initiator_address VARCHAR(34),
    is_historical BOOLEAN NOT NULL DEFAULT FALSE,
    history_complete BOOLEAN NOT NULL DEFAULT TRUE,
    investigation_status VARCHAR(16) NOT NULL,
    trace_status VARCHAR(16) NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL,
    analysis_started_at TIMESTAMPTZ NOT NULL,
    analysis_completed_at TIMESTAMPTZ NOT NULL,
    alert_sent_at TIMESTAMPTZ,
    detection_latency_ms INTEGER,
    alert_latency_ms INTEGER,
    chain_latency_ms BIGINT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_event_tx_victim UNIQUE (transaction_id, victim_wallet)
);
CREATE INDEX ix_events_victim ON poisoning_events (victim_wallet, created_at);
CREATE INDEX ix_events_suspicious ON poisoning_events (suspicious_recipient);
CREATE INDEX ix_events_type ON poisoning_events (event_type, created_at);
CREATE INDEX ix_events_tx_hash ON poisoning_events (tx_hash);

CREATE TABLE poisoning_evidence (
    id BIGSERIAL PRIMARY KEY,
    event_id BIGINT NOT NULL REFERENCES poisoning_events(id) ON DELETE CASCADE,
    evidence_key VARCHAR(160) NOT NULL,
    evidence_type VARCHAR(48) NOT NULL,
    kind VARCHAR(12) NOT NULL,
    supports BOOLEAN NOT NULL,
    description TEXT NOT NULL,
    tx_hash VARCHAR(64),
    address VARCHAR(34),
    amount NUMERIC(78,0),
    observed_at TIMESTAMPTZ,
    data JSON NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_evidence_key UNIQUE (event_id, evidence_key)
);

CREATE TABLE fund_traces (
    id BIGSERIAL PRIMARY KEY,
    event_id BIGINT NOT NULL REFERENCES poisoning_events(id) ON DELETE CASCADE,
    trace_run INTEGER NOT NULL,
    hop INTEGER NOT NULL,
    parent_address VARCHAR(34) NOT NULL,
    from_address VARCHAR(34) NOT NULL,
    to_address VARCHAR(34) NOT NULL,
    amount NUMERIC(78,0) NOT NULL,
    token_contract VARCHAR(34) NOT NULL,
    token_symbol VARCHAR(16) NOT NULL,
    tx_hash VARCHAR(64) NOT NULL,
    transfer_key VARCHAR(64) NOT NULL,
    block_number BIGINT,
    block_timestamp TIMESTAMPTZ NOT NULL,
    confirmation_status VARCHAR(16) NOT NULL,
    to_label VARCHAR(200),
    to_label_category VARCHAR(32),
    to_label_source VARCHAR(64),
    terminal_reason VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_trace_transfer UNIQUE (event_id, trace_run, transfer_key)
);
CREATE INDEX ix_trace_event ON fund_traces (event_id, trace_run, hop);

CREATE TABLE alerts (
    id BIGSERIAL PRIMARY KEY,
    dedup_key VARCHAR(200) NOT NULL UNIQUE,
    event_id BIGINT REFERENCES poisoning_events(id) ON DELETE CASCADE,
    alert_type VARCHAR(32) NOT NULL,
    chat_id BIGINT NOT NULL,
    payload JSON NOT NULL,
    status VARCHAR(16) NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL,
    last_error TEXT,
    telegram_message_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL,
    sent_at TIMESTAMPTZ
);
CREATE INDEX ix_alerts_pending ON alerts (status, next_attempt_at);

CREATE TABLE jobs (
    id BIGSERIAL PRIMARY KEY,
    job_type VARCHAR(32) NOT NULL,
    ref VARCHAR(100) NOT NULL,
    status VARCHAR(16) NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    payload JSON NOT NULL,
    next_run_at TIMESTAMPTZ NOT NULL,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_job_ref UNIQUE (job_type, ref)
);
CREATE INDEX ix_jobs_due ON jobs (status, next_run_at);

CREATE TABLE telegram_users (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL UNIQUE,
    username VARCHAR(64),
    first_name VARCHAR(128),
    is_admin BOOLEAN NOT NULL DEFAULT FALSE,
    command_count INTEGER NOT NULL DEFAULT 0,
    unauthorized_attempts INTEGER NOT NULL DEFAULT 0,
    first_seen TIMESTAMPTZ NOT NULL,
    last_seen TIMESTAMPTZ NOT NULL
);

CREATE TABLE system_logs (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL,
    level VARCHAR(10) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    wallet VARCHAR(34),
    tx_hash VARCHAR(64),
    message TEXT NOT NULL,
    data JSON NOT NULL
);
CREATE INDEX ix_system_logs_ts ON system_logs (ts);

CREATE TABLE monitor_state (
    key VARCHAR(64) PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE x_post_drafts (
    id BIGSERIAL PRIMARY KEY,
    event_id BIGINT NOT NULL REFERENCES poisoning_events(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    status VARCHAR(16) NOT NULL,
    tweet_id VARCHAR(64),
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE address_labels (
    address VARCHAR(34) PRIMARY KEY,
    label VARCHAR(200),
    category VARCHAR(32),
    source VARCHAR(64) NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL
);
