-- Network-wide: poisoning "contacts" (dust, zero-value spoofs, fake tokens, tiny TRX).
CREATE TABLE IF NOT EXISTS network_contacts (
    toucher VARCHAR(34) NOT NULL,
    touched VARCHAR(34) NOT NULL,
    kind VARCHAR(16) NOT NULL,
    token_contract VARCHAR(34),
    amount NUMERIC(78,0) NOT NULL,
    tx_hash VARCHAR(64) NOT NULL,
    first_seen TIMESTAMPTZ NOT NULL,
    last_seen TIMESTAMPTZ NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (toucher, touched)
);
CREATE INDEX IF NOT EXISTS ix_contacts_last_seen ON network_contacts (last_seen);
