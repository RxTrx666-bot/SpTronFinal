"""Initial schema: wallets, transfers, alerts, checkpoints, system_state.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "wallets",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("address", sa.String(34), nullable=False),
        sa.Column("wallet_type", sa.String(16), nullable=False),
        sa.Column("root_wallet", sa.String(34), nullable=False),
        sa.Column("hop", sa.SmallInteger(), nullable=False),
        sa.Column("discovered_from", sa.String(34)),
        sa.Column("discovered_at", TS, nullable=False),
        sa.Column("first_seen_tx", sa.String(64)),
        sa.Column("first_seen_amount_base_units", sa.BigInteger()),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("address", name="wallets_address_key"),
    )
    op.create_index("ix_wallets_type_active", "wallets", ["wallet_type", "active"])
    op.create_index("ix_wallets_discovered_at", "wallets", ["discovered_at"])
    op.create_index("ix_wallets_root", "wallets", ["root_wallet"])

    op.create_table(
        "transfers",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tx_hash", sa.String(64), nullable=False),
        sa.Column("event_index", sa.Integer(), nullable=False),
        sa.Column("block_number", sa.BigInteger()),
        sa.Column("timestamp", TS, nullable=False),
        sa.Column("from_address", sa.String(34), nullable=False),
        sa.Column("to_address", sa.String(34), nullable=False),
        sa.Column("amount_base_units", sa.BigInteger(), nullable=False),
        sa.Column("amount_usdt", sa.Numeric(30, 6), nullable=False),
        sa.Column("contract_address", sa.String(34), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("confirmed", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tx_hash", "event_index", name="uq_transfers_tx_event"),
    )
    op.create_index("ix_transfers_to", "transfers", ["to_address"])
    op.create_index("ix_transfers_from", "transfers", ["from_address"])
    op.create_index("ix_transfers_tx", "transfers", ["tx_hash"])
    op.create_index("ix_transfers_block", "transfers", ["block_number"])
    op.create_index("ix_transfers_ts", "transfers", ["timestamp"])
    op.create_index("ix_transfers_amount", "transfers", ["amount_base_units"])

    op.create_table(
        "alerts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tx_hash", sa.String(64), nullable=False),
        sa.Column("event_index", sa.Integer(), nullable=False),
        sa.Column("alert_type", sa.String(24), nullable=False),
        sa.Column("discovered_wallet", sa.String(34), nullable=False),
        sa.Column("sender", sa.String(34), nullable=False),
        sa.Column("root_wallet", sa.String(34), nullable=False),
        sa.Column("amount_base_units", sa.BigInteger(), nullable=False),
        sa.Column("amount_usdt", sa.Numeric(30, 6), nullable=False),
        sa.Column("block_number", sa.BigInteger()),
        sa.Column("transfer_timestamp", TS, nullable=False),
        sa.Column("confirmed", sa.Boolean(), nullable=False),
        sa.Column("discovery_tx", sa.String(64)),
        sa.Column("discovered_at", TS),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column("telegram_message_id", sa.String(32)),
        sa.Column("detected_at", TS, nullable=False),
        sa.Column("sent_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tx_hash", "event_index", "alert_type", name="uq_alerts_tx_event_type"),
    )
    op.create_index("ix_alerts_status", "alerts", ["status", "id"])
    op.create_index("ix_alerts_created", "alerts", ["created_at"])
    op.create_index("ix_alerts_wallet", "alerts", ["discovered_wallet"])
    op.create_index("ix_alerts_amount", "alerts", ["amount_base_units"])

    op.create_table(
        "checkpoints",
        sa.Column("wallet_address", sa.String(64), primary_key=True),
        sa.Column("last_block", sa.BigInteger()),
        sa.Column("last_timestamp", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "system_state",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    for table in ("system_state", "checkpoints", "alerts", "transfers", "wallets"):
        op.drop_table(table)
