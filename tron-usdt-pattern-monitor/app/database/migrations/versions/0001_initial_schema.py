"""initial schema

Revision ID: 0001
Revises: 
Create Date: 2026-09-23 14:36:21.898612
"""
from alembic import op
import sqlalchemy as sa


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Initial schema: transactions, wallet_pairs, pattern_sequences, watchlist,
    # test_events, followup_events, alerts, collector_state.
    op.create_table('alerts',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('dedup_key', sa.String(length=200), nullable=False),
    sa.Column('transaction_hash', sa.String(length=64), nullable=True),
    sa.Column('alert_type', sa.String(length=32), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('sender', sa.String(length=34), nullable=True),
    sa.Column('recipient', sa.String(length=34), nullable=True),
    sa.Column('amount_raw', sa.BigInteger(), nullable=True),
    sa.Column('message_text', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('telegram_message_id', sa.String(length=64), nullable=True),
    sa.Column('blockchain_event_time', sa.DateTime(timezone=True), nullable=True),
    sa.Column('blockchain_detection_time', sa.DateTime(timezone=True), nullable=True),
    sa.Column('processing_start_time', sa.DateTime(timezone=True), nullable=True),
    sa.Column('processing_end_time', sa.DateTime(timezone=True), nullable=True),
    sa.Column('telegram_send_start_time', sa.DateTime(timezone=True), nullable=True),
    sa.Column('telegram_send_end_time', sa.DateTime(timezone=True), nullable=True),
    sa.Column('total_detection_latency_ms', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dedup_key', name='uq_alerts_dedup_key')
    )
    op.create_index('ix_alerts_hash', 'alerts', ['transaction_hash'], unique=False)
    op.create_index('ix_alerts_pair', 'alerts', ['sender', 'recipient'], unique=False)
    op.create_index('ix_alerts_queue', 'alerts', ['status', 'priority', 'id'], unique=False)
    op.create_table('collector_state',
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('transactions',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('transaction_hash', sa.String(length=64), nullable=False),
    sa.Column('event_index', sa.Integer(), nullable=False),
    sa.Column('block_number', sa.BigInteger(), nullable=True),
    sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
    sa.Column('sender', sa.String(length=34), nullable=False),
    sa.Column('recipient', sa.String(length=34), nullable=False),
    sa.Column('amount_raw', sa.BigInteger(), nullable=False),
    sa.Column('amount_usdt', sa.Numeric(precision=38, scale=6), nullable=False),
    sa.Column('token_contract', sa.String(length=34), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('detected_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('processed', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('transaction_hash', 'event_index', name='uq_transactions_hash_event')
    )
    op.create_index('ix_transactions_hash', 'transactions', ['transaction_hash'], unique=False)
    op.create_index('ix_transactions_pair_ts', 'transactions', ['sender', 'recipient', 'timestamp'], unique=False)
    op.create_index('ix_transactions_recipient', 'transactions', ['recipient'], unique=False)
    op.create_index('ix_transactions_sender', 'transactions', ['sender'], unique=False)
    op.create_index('ix_transactions_status_ts', 'transactions', ['status', 'timestamp'], unique=False)
    op.create_index('ix_transactions_timestamp', 'transactions', ['timestamp'], unique=False)
    op.create_index('ix_transactions_unprocessed', 'transactions', ['processed', 'detected_at'], unique=False)
    op.create_table('wallet_pairs',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('sender', sa.String(length=34), nullable=False),
    sa.Column('recipient', sa.String(length=34), nullable=False),
    sa.Column('total_transfers', sa.BigInteger(), nullable=False),
    sa.Column('total_volume_raw', sa.Numeric(precision=40, scale=0), nullable=False),
    sa.Column('average_amount_raw', sa.Numeric(precision=40, scale=0), nullable=False),
    sa.Column('median_amount_raw', sa.BigInteger(), nullable=True),
    sa.Column('smallest_amount_raw', sa.BigInteger(), nullable=False),
    sa.Column('largest_amount_raw', sa.BigInteger(), nullable=False),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('sequence_count', sa.Integer(), nullable=False),
    sa.Column('needs_analysis', sa.Boolean(), nullable=False),
    sa.Column('last_analyzed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('sender', 'recipient', name='uq_wallet_pairs_pair')
    )
    op.create_index('ix_wallet_pairs_needs_analysis', 'wallet_pairs', ['needs_analysis'], unique=False)
    op.create_index('ix_wallet_pairs_recipient', 'wallet_pairs', ['recipient'], unique=False)
    op.create_index('ix_wallet_pairs_sender', 'wallet_pairs', ['sender'], unique=False)
    op.create_table('watchlist',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('sender', sa.String(length=34), nullable=False),
    sa.Column('recipient', sa.String(length=34), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('status_reason', sa.Text(), nullable=True),
    sa.Column('confidence', sa.String(length=8), nullable=False),
    sa.Column('confidence_score', sa.Float(), nullable=False),
    sa.Column('pattern_strength', sa.Float(), nullable=False),
    sa.Column('typical_test_amount_raw', sa.BigInteger(), nullable=False),
    sa.Column('median_test_amount_raw', sa.BigInteger(), nullable=False),
    sa.Column('test_amount_min_raw', sa.BigInteger(), nullable=False),
    sa.Column('test_amount_max_raw', sa.BigInteger(), nullable=False),
    sa.Column('match_low_raw', sa.BigInteger(), nullable=False),
    sa.Column('match_high_raw', sa.BigInteger(), nullable=False),
    sa.Column('typical_large_amount_raw', sa.BigInteger(), nullable=False),
    sa.Column('median_large_amount_raw', sa.BigInteger(), nullable=False),
    sa.Column('large_amount_min_raw', sa.BigInteger(), nullable=False),
    sa.Column('large_amount_max_raw', sa.BigInteger(), nullable=False),
    sa.Column('typical_test_to_large_ratio', sa.Numeric(precision=30, scale=6), nullable=False),
    sa.Column('typical_followup_seconds', sa.BigInteger(), nullable=False),
    sa.Column('followup_window_seconds', sa.BigInteger(), nullable=False),
    sa.Column('successful_sequences', sa.Integer(), nullable=False),
    sa.Column('total_sequences', sa.Integer(), nullable=False),
    sa.Column('success_rate', sa.Float(), nullable=False),
    sa.Column('last_test_transfer', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_large_transfer', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_sequence_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('activated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('activation_count', sa.Integer(), nullable=False),
    sa.Column('pause_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('manual_pause', sa.Boolean(), nullable=False),
    sa.Column('model_json', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('sender', 'recipient', name='uq_watchlist_pair')
    )
    op.create_index('ix_watchlist_status', 'watchlist', ['status'], unique=False)
    op.create_table('pattern_sequences',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('sender', sa.String(length=34), nullable=False),
    sa.Column('recipient', sa.String(length=34), nullable=False),
    sa.Column('test_transaction_id', sa.BigInteger(), nullable=True),
    sa.Column('large_transaction_id', sa.BigInteger(), nullable=True),
    sa.Column('test_tx_hash', sa.String(length=64), nullable=False),
    sa.Column('large_tx_hash', sa.String(length=64), nullable=False),
    sa.Column('test_amount_raw', sa.BigInteger(), nullable=False),
    sa.Column('large_amount_raw', sa.BigInteger(), nullable=False),
    sa.Column('amount_ratio', sa.Numeric(precision=30, scale=6), nullable=False),
    sa.Column('test_timestamp', sa.DateTime(timezone=True), nullable=False),
    sa.Column('large_timestamp', sa.DateTime(timezone=True), nullable=False),
    sa.Column('time_difference_seconds', sa.BigInteger(), nullable=False),
    sa.Column('is_inlier', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['large_transaction_id'], ['transactions.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['test_transaction_id'], ['transactions.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('large_transaction_id', name='uq_sequences_large_tx'),
    sa.UniqueConstraint('test_transaction_id', name='uq_sequences_test_tx')
    )
    op.create_index('ix_sequences_pair_ts', 'pattern_sequences', ['sender', 'recipient', 'test_timestamp'], unique=False)
    op.create_table('test_events',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('watchlist_id', sa.BigInteger(), nullable=False),
    sa.Column('sender', sa.String(length=34), nullable=False),
    sa.Column('recipient', sa.String(length=34), nullable=False),
    sa.Column('transaction_id', sa.BigInteger(), nullable=True),
    sa.Column('transaction_hash', sa.String(length=64), nullable=False),
    sa.Column('event_index', sa.Integer(), nullable=False),
    sa.Column('amount_raw', sa.BigInteger(), nullable=False),
    sa.Column('tx_timestamp', sa.DateTime(timezone=True), nullable=False),
    sa.Column('detected_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['transaction_id'], ['transactions.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['watchlist_id'], ['watchlist.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('transaction_hash', 'event_index', name='uq_test_events_tx')
    )
    op.create_index('ix_test_events_pair_status', 'test_events', ['sender', 'recipient', 'status'], unique=False)
    op.create_index('ix_test_events_status_expires', 'test_events', ['status', 'expires_at'], unique=False)
    op.create_table('followup_events',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('test_event_id', sa.BigInteger(), nullable=False),
    sa.Column('sender', sa.String(length=34), nullable=False),
    sa.Column('recipient', sa.String(length=34), nullable=False),
    sa.Column('large_transaction_id', sa.BigInteger(), nullable=True),
    sa.Column('large_transaction_hash', sa.String(length=64), nullable=False),
    sa.Column('large_event_index', sa.Integer(), nullable=False),
    sa.Column('test_amount_raw', sa.BigInteger(), nullable=False),
    sa.Column('large_amount_raw', sa.BigInteger(), nullable=False),
    sa.Column('amount_ratio', sa.Numeric(precision=30, scale=6), nullable=False),
    sa.Column('time_difference_seconds', sa.BigInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['large_transaction_id'], ['transactions.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['test_event_id'], ['test_events.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('large_transaction_hash', 'large_event_index', name='uq_followup_large_tx'),
    sa.UniqueConstraint('test_event_id', name='uq_followup_test_event')
    )
    op.create_index('ix_followup_pair', 'followup_events', ['sender', 'recipient'], unique=False)


def downgrade() -> None:
    # Initial schema: transactions, wallet_pairs, pattern_sequences, watchlist,
    # test_events, followup_events, alerts, collector_state.
    op.drop_index('ix_followup_pair', table_name='followup_events')
    op.drop_table('followup_events')
    op.drop_index('ix_test_events_status_expires', table_name='test_events')
    op.drop_index('ix_test_events_pair_status', table_name='test_events')
    op.drop_table('test_events')
    op.drop_index('ix_sequences_pair_ts', table_name='pattern_sequences')
    op.drop_table('pattern_sequences')
    op.drop_index('ix_watchlist_status', table_name='watchlist')
    op.drop_table('watchlist')
    op.drop_index('ix_wallet_pairs_sender', table_name='wallet_pairs')
    op.drop_index('ix_wallet_pairs_recipient', table_name='wallet_pairs')
    op.drop_index('ix_wallet_pairs_needs_analysis', table_name='wallet_pairs')
    op.drop_table('wallet_pairs')
    op.drop_index('ix_transactions_unprocessed', table_name='transactions')
    op.drop_index('ix_transactions_timestamp', table_name='transactions')
    op.drop_index('ix_transactions_status_ts', table_name='transactions')
    op.drop_index('ix_transactions_sender', table_name='transactions')
    op.drop_index('ix_transactions_recipient', table_name='transactions')
    op.drop_index('ix_transactions_pair_ts', table_name='transactions')
    op.drop_index('ix_transactions_hash', table_name='transactions')
    op.drop_table('transactions')
    op.drop_table('collector_state')
    op.drop_index('ix_alerts_queue', table_name='alerts')
    op.drop_index('ix_alerts_pair', table_name='alerts')
    op.drop_index('ix_alerts_hash', table_name='alerts')
    op.drop_table('alerts')
