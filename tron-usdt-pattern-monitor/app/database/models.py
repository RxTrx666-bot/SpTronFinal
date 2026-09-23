"""SQLAlchemy ORM models (PostgreSQL in production; SQLite for tests/simulation).

Amounts are stored as exact integers in the token's smallest unit
(``*_raw`` columns, 6 decimals for USDT).  ``transactions.amount_usdt`` is an
exact NUMERIC(38, 6) copy for human-friendly SQL queries.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on every backend (SQLite drops tzinfo)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone(timezone.utc)
        if dialect.name == "sqlite":
            return value.replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class DecimalText(TypeDecorator):
    """Exact decimal storage for SQLite (which would otherwise use REAL)."""

    impl = String(80)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else str(Decimal(value))

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


PK = BigInteger().with_variant(Integer, "sqlite")
RawAmount = BigInteger  # exact integer (USDT supply << 2**63 raw units)
BigVolume = Numeric(40, 0).with_variant(BigInteger, "sqlite")
UsdtAmount = Numeric(38, 6).with_variant(DecimalText(), "sqlite")
Ratio = Numeric(30, 6).with_variant(DecimalText(), "sqlite")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    transaction_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # A TRON transaction can emit several USDT Transfer events (batch
    # contracts).  (transaction_hash, event_index) identifies one transfer; for
    # ordinary wallet transfers event_index is 0, so the hash alone is unique.
    event_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    block_number: Mapped[int | None] = mapped_column(BigInteger)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    sender: Mapped[str] = mapped_column(String(34), nullable=False)
    recipient: Mapped[str] = mapped_column(String(34), nullable=False)
    amount_raw: Mapped[int] = mapped_column(RawAmount, nullable=False)
    amount_usdt: Mapped[Decimal] = mapped_column(UsdtAmount, nullable=False)
    token_contract: Mapped[str] = mapped_column(String(34), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # False while the transfer still has to go through watchlist matching.
    processed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("transaction_hash", "event_index", name="uq_transactions_hash_event"),
        Index("ix_transactions_hash", "transaction_hash"),
        Index("ix_transactions_sender", "sender"),
        Index("ix_transactions_recipient", "recipient"),
        Index("ix_transactions_pair_ts", "sender", "recipient", "timestamp"),
        Index("ix_transactions_timestamp", "timestamp"),
        Index("ix_transactions_status_ts", "status", "timestamp"),
        Index("ix_transactions_unprocessed", "processed", "detected_at"),
    )


class WalletPair(Base):
    __tablename__ = "wallet_pairs"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    sender: Mapped[str] = mapped_column(String(34), nullable=False)
    recipient: Mapped[str] = mapped_column(String(34), nullable=False)
    total_transfers: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_volume_raw: Mapped[int] = mapped_column(BigVolume, nullable=False, default=0)
    average_amount_raw: Mapped[int] = mapped_column(BigVolume, nullable=False, default=0)
    median_amount_raw: Mapped[int | None] = mapped_column(RawAmount)
    smallest_amount_raw: Mapped[int] = mapped_column(RawAmount, nullable=False)
    largest_amount_raw: Mapped[int] = mapped_column(RawAmount, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    sequence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    needs_analysis: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_analyzed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=_now)

    __table_args__ = (
        UniqueConstraint("sender", "recipient", name="uq_wallet_pairs_pair"),
        Index("ix_wallet_pairs_sender", "sender"),
        Index("ix_wallet_pairs_recipient", "recipient"),
        Index("ix_wallet_pairs_needs_analysis", "needs_analysis"),
    )


class PatternSequence(Base):
    __tablename__ = "pattern_sequences"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    sender: Mapped[str] = mapped_column(String(34), nullable=False)
    recipient: Mapped[str] = mapped_column(String(34), nullable=False)
    test_transaction_id: Mapped[int | None] = mapped_column(
        PK, ForeignKey("transactions.id", ondelete="SET NULL")
    )
    large_transaction_id: Mapped[int | None] = mapped_column(
        PK, ForeignKey("transactions.id", ondelete="SET NULL")
    )
    test_tx_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    large_tx_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    test_amount_raw: Mapped[int] = mapped_column(RawAmount, nullable=False)
    large_amount_raw: Mapped[int] = mapped_column(RawAmount, nullable=False)
    amount_ratio: Mapped[Decimal] = mapped_column(Ratio, nullable=False)
    test_timestamp: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    large_timestamp: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    time_difference_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    is_inlier: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=_now)

    __table_args__ = (
        UniqueConstraint("test_transaction_id", name="uq_sequences_test_tx"),
        UniqueConstraint("large_transaction_id", name="uq_sequences_large_tx"),
        Index("ix_sequences_pair_ts", "sender", "recipient", "test_timestamp"),
    )


class WatchlistEntry(Base):
    __tablename__ = "watchlist"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    sender: Mapped[str] = mapped_column(String(34), nullable=False)
    recipient: Mapped[str] = mapped_column(String(34), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    status_reason: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[str] = mapped_column(String(8), nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    pattern_strength: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    typical_test_amount_raw: Mapped[int] = mapped_column(RawAmount, nullable=False)
    median_test_amount_raw: Mapped[int] = mapped_column(RawAmount, nullable=False)
    test_amount_min_raw: Mapped[int] = mapped_column(RawAmount, nullable=False)
    test_amount_max_raw: Mapped[int] = mapped_column(RawAmount, nullable=False)
    match_low_raw: Mapped[int] = mapped_column(RawAmount, nullable=False)
    match_high_raw: Mapped[int] = mapped_column(RawAmount, nullable=False)
    typical_large_amount_raw: Mapped[int] = mapped_column(RawAmount, nullable=False)
    median_large_amount_raw: Mapped[int] = mapped_column(RawAmount, nullable=False)
    large_amount_min_raw: Mapped[int] = mapped_column(RawAmount, nullable=False)
    large_amount_max_raw: Mapped[int] = mapped_column(RawAmount, nullable=False)
    typical_test_to_large_ratio: Mapped[Decimal] = mapped_column(Ratio, nullable=False)
    typical_followup_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    followup_window_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    successful_sequences: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_sequences: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    last_test_transfer: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_large_transfer: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_sequence_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    activated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    activation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pause_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    manual_pause: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    model_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=_now)

    __table_args__ = (
        UniqueConstraint("sender", "recipient", name="uq_watchlist_pair"),
        Index("ix_watchlist_status", "status"),
    )


class TestEvent(Base):
    __tablename__ = "test_events"
    __test__ = False  # not a pytest test class

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    watchlist_id: Mapped[int] = mapped_column(PK, ForeignKey("watchlist.id", ondelete="CASCADE"), nullable=False)
    sender: Mapped[str] = mapped_column(String(34), nullable=False)
    recipient: Mapped[str] = mapped_column(String(34), nullable=False)
    transaction_id: Mapped[int | None] = mapped_column(PK, ForeignKey("transactions.id", ondelete="SET NULL"))
    transaction_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    amount_raw: Mapped[int] = mapped_column(RawAmount, nullable=False)
    tx_timestamp: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=_now)

    __table_args__ = (
        UniqueConstraint("transaction_hash", "event_index", name="uq_test_events_tx"),
        Index("ix_test_events_pair_status", "sender", "recipient", "status"),
        Index("ix_test_events_status_expires", "status", "expires_at"),
    )


class FollowupEvent(Base):
    __tablename__ = "followup_events"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    test_event_id: Mapped[int] = mapped_column(PK, ForeignKey("test_events.id", ondelete="CASCADE"), nullable=False)
    sender: Mapped[str] = mapped_column(String(34), nullable=False)
    recipient: Mapped[str] = mapped_column(String(34), nullable=False)
    large_transaction_id: Mapped[int | None] = mapped_column(PK, ForeignKey("transactions.id", ondelete="SET NULL"))
    large_transaction_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    large_event_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    test_amount_raw: Mapped[int] = mapped_column(RawAmount, nullable=False)
    large_amount_raw: Mapped[int] = mapped_column(RawAmount, nullable=False)
    amount_ratio: Mapped[Decimal] = mapped_column(Ratio, nullable=False)
    time_difference_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=_now)

    __table_args__ = (
        UniqueConstraint("test_event_id", name="uq_followup_test_event"),
        UniqueConstraint("large_transaction_hash", "large_event_index", name="uq_followup_large_tx"),
        Index("ix_followup_pair", "sender", "recipient"),
    )


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    dedup_key: Mapped[str] = mapped_column(String(200), nullable=False)
    transaction_hash: Mapped[str | None] = mapped_column(String(64))
    alert_type: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    sender: Mapped[str | None] = mapped_column(String(34))
    recipient: Mapped[str | None] = mapped_column(String(34))
    amount_raw: Mapped[int | None] = mapped_column(RawAmount)
    message_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    telegram_message_id: Mapped[str | None] = mapped_column(String(64))
    # Latency measurement (all UTC).
    blockchain_event_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    blockchain_detection_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    processing_start_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    processing_end_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    telegram_send_start_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    telegram_send_end_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    total_detection_latency_ms: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=_now)

    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_alerts_dedup_key"),
        Index("ix_alerts_hash", "transaction_hash"),
        Index("ix_alerts_queue", "status", "priority", "id"),
        Index("ix_alerts_pair", "sender", "recipient"),
    )


class CollectorState(Base):
    __tablename__ = "collector_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=_now)
