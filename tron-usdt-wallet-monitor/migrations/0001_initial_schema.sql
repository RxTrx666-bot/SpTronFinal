-- Generated from the Alembic migration (app/db/migrations). For reference / manual setup:
--   psql "$DATABASE_URL" -f migrations/0001_initial_schema.sql
-- The application applies migrations automatically at startup (DB_AUTO_MIGRATE=true).

BEGIN;

CREATE TABLE alembic_version (
    version_num VARCHAR(32) NOT NULL, 
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

-- Running upgrade  -> 0001

CREATE TABLE wallets (
    id BIGSERIAL NOT NULL, 
    address VARCHAR(34) NOT NULL, 
    wallet_type VARCHAR(16) NOT NULL, 
    root_wallet VARCHAR(34) NOT NULL, 
    hop SMALLINT NOT NULL, 
    discovered_from VARCHAR(34), 
    discovered_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    first_seen_tx VARCHAR(64), 
    first_seen_amount_base_units BIGINT, 
    active BOOLEAN NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT wallets_address_key UNIQUE (address)
);

CREATE INDEX ix_wallets_type_active ON wallets (wallet_type, active);

CREATE INDEX ix_wallets_discovered_at ON wallets (discovered_at);

CREATE INDEX ix_wallets_root ON wallets (root_wallet);

CREATE TABLE transfers (
    id BIGSERIAL NOT NULL, 
    tx_hash VARCHAR(64) NOT NULL, 
    event_index INTEGER NOT NULL, 
    block_number BIGINT, 
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL, 
    from_address VARCHAR(34) NOT NULL, 
    to_address VARCHAR(34) NOT NULL, 
    amount_base_units BIGINT NOT NULL, 
    amount_usdt NUMERIC(30, 6) NOT NULL, 
    contract_address VARCHAR(34) NOT NULL, 
    kind VARCHAR(16) NOT NULL, 
    confirmed BOOLEAN NOT NULL, 
    source VARCHAR(16) NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT uq_transfers_tx_event UNIQUE (tx_hash, event_index)
);

CREATE INDEX ix_transfers_to ON transfers (to_address);

CREATE INDEX ix_transfers_from ON transfers (from_address);

CREATE INDEX ix_transfers_tx ON transfers (tx_hash);

CREATE INDEX ix_transfers_block ON transfers (block_number);

CREATE INDEX ix_transfers_ts ON transfers (timestamp);

CREATE INDEX ix_transfers_amount ON transfers (amount_base_units);

CREATE TABLE alerts (
    id BIGSERIAL NOT NULL, 
    tx_hash VARCHAR(64) NOT NULL, 
    event_index INTEGER NOT NULL, 
    alert_type VARCHAR(24) NOT NULL, 
    discovered_wallet VARCHAR(34) NOT NULL, 
    sender VARCHAR(34) NOT NULL, 
    root_wallet VARCHAR(34) NOT NULL, 
    amount_base_units BIGINT NOT NULL, 
    amount_usdt NUMERIC(30, 6) NOT NULL, 
    block_number BIGINT, 
    transfer_timestamp TIMESTAMP WITH TIME ZONE NOT NULL, 
    confirmed BOOLEAN NOT NULL, 
    discovery_tx VARCHAR(64), 
    discovered_at TIMESTAMP WITH TIME ZONE, 
    status VARCHAR(16) NOT NULL, 
    attempts INTEGER NOT NULL, 
    last_error TEXT, 
    telegram_message_id VARCHAR(32), 
    detected_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    sent_at TIMESTAMP WITH TIME ZONE, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT uq_alerts_tx_event_type UNIQUE (tx_hash, event_index, alert_type)
);

CREATE INDEX ix_alerts_status ON alerts (status, id);

CREATE INDEX ix_alerts_created ON alerts (created_at);

CREATE INDEX ix_alerts_wallet ON alerts (discovered_wallet);

CREATE INDEX ix_alerts_amount ON alerts (amount_base_units);

CREATE TABLE checkpoints (
    wallet_address VARCHAR(64) NOT NULL, 
    last_block BIGINT, 
    last_timestamp TIMESTAMP WITH TIME ZONE NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (wallet_address)
);

CREATE TABLE system_state (
    key VARCHAR(64) NOT NULL, 
    value TEXT NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (key)
);

INSERT INTO alembic_version (version_num) VALUES ('0001') RETURNING alembic_version.version_num;

COMMIT;

