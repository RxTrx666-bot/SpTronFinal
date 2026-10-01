"""PostgreSQL schema (mirrored by the Alembic migration in ``migrations/versions``)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


ADDR = String(34)
TXH = String(64)
USDT_NUM = Numeric(30, 6)


class Wallet(Base):
    __tablename__ = "wallets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    address: Mapped[str] = mapped_column(ADDR, nullable=False, unique=True)
    wallet_type: Mapped[str] = mapped_column(String(16), nullable=False)  # root | discovered
    root_wallet: Mapped[str] = mapped_column(ADDR, nullable=False)
    hop: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)  # root=0, direct recipients=1
    discovered_from: Mapped[str | None] = mapped_column(ADDR)  # parent wallet (Wallet A at hop 1)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # on-chain time
    first_seen_tx: Mapped[str | None] = mapped_column(TXH)
    first_seen_amount_base_units: Mapped[int | None] = mapped_column(BigInteger)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_wallets_type_active", "wallet_type", "active"),
        Index("ix_wallets_discovered_at", "discovered_at"),
        Index("ix_wallets_root", "root_wallet"),
    )


class TransferRow(Base):
    __tablename__ = "transfers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tx_hash: Mapped[str] = mapped_column(TXH, nullable=False)
    event_index: Mapped[int] = mapped_column(Integer, nullable=False)
    block_number: Mapped[int | None] = mapped_column(BigInteger)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    from_address: Mapped[str] = mapped_column(ADDR, nullable=False)
    to_address: Mapped[str] = mapped_column(ADDR, nullable=False)
    amount_base_units: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount_usdt: Mapped[Decimal] = mapped_column(USDT_NUM, nullable=False)
    contract_address: Mapped[str] = mapped_column(ADDR, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # discovery | incoming | both
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # stream | reconcile | backfill
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tx_hash", "event_index", name="uq_transfers_tx_event"),
        Index("ix_transfers_to", "to_address"),
        Index("ix_transfers_from", "from_address"),
        Index("ix_transfers_tx", "tx_hash"),
        Index("ix_transfers_block", "block_number"),
        Index("ix_transfers_ts", "timestamp"),
        Index("ix_transfers_amount", "amount_base_units"),
    )


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tx_hash: Mapped[str] = mapped_column(TXH, nullable=False)
    event_index: Mapped[int] = mapped_column(Integer, nullable=False)
    alert_type: Mapped[str] = mapped_column(String(24), nullable=False)  # large_transfer | discovery
    discovered_wallet: Mapped[str] = mapped_column(ADDR, nullable=False)
    sender: Mapped[str] = mapped_column(ADDR, nullable=False)
    root_wallet: Mapped[str] = mapped_column(ADDR, nullable=False)
    amount_base_units: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount_usdt: Mapped[Decimal] = mapped_column(USDT_NUM, nullable=False)
    block_number: Mapped[int | None] = mapped_column(BigInteger)
    transfer_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    discovery_tx: Mapped[str | None] = mapped_column(TXH)
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # delivery (transactional outbox)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # pending | sending | sent | suppressed
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    telegram_message_id: Mapped[str | None] = mapped_column(String(32))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # first seen by us
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tx_hash", "event_index", "alert_type", name="uq_alerts_tx_event_type"),
        Index("ix_alerts_status", "status", "id"),
        Index("ix_alerts_created", "created_at"),
        Index("ix_alerts_wallet", "discovered_wallet"),
        Index("ix_alerts_amount", "amount_base_units"),
    )


class Checkpoint(Base):
    """Progress per monitored wallet; ``stream:<contract>`` holds the live-stream cursor."""

    __tablename__ = "checkpoints"

    wallet_address: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_block: Mapped[int | None] = mapped_column(BigInteger)
    last_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SystemState(Base):
    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
