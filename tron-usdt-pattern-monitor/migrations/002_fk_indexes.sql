-- Migration 0002 (applied automatically on start; for manual psql use only).
BEGIN;

-- Running upgrade 0001 -> 0002

CREATE INDEX ix_test_events_transaction_id ON test_events (transaction_id);

CREATE INDEX ix_followup_large_transaction_id ON followup_events (large_transaction_id);

UPDATE alembic_version SET version_num='0002' WHERE alembic_version.version_num = '0001';

COMMIT;

