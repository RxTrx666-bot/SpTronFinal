"""SQLAlchemy ORM models.

PostgreSQL is the production database; the schema is created by the SQL
migrations in ``migrations/``.  The same models are used with SQLite for unit
tests and simulation (``create_all``).  ``tests/test_postgres_schema.py``
verifies that the SQL migrations and these models agree.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
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
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

# BIGSERIAL on PostgreSQL, INTEGER PRIMARY KEY (rowid alias) on SQLite.
BigId = BigInteger().with_variant(Integer(), "sqlite")


class Amount(TypeDecorator):
    """Exact integer token amount: NUMERIC(78,0) on PostgreSQL (fits uint256), BIGINT on SQLite."""

    impl = Numeric(78, 0)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(BigInteger())
        return dialect.type_descriptor(Numeric(78, 0, asdecimal=True))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"amounts must be int base units, got {type(value).__name__}")
        return value

    def process_result_value(self, value, dialect):
        return None if value is None else int(value)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


TS = DateTime(timezone=True)


class WatchedWallet(Base):
    __tablename__ = "watched_wallets"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    address: Mapped[str] = mapped_column(String(34), unique=True, nullable=False)
    address_hex: Mapped[str] = mapped_column(String(42), nullable=False)
    label: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    added_by: Mapped[int | None] = mapped_column(BigInteger)
    added_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    history_status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    history_cursor_ms: Mapped[int | None] = mapped_column(BigInteger)
    history_fingerprint: Mapped[str | None] = mapped_column(Text)
    history_transfers_scanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    history_started_at: Mapped[datetime | None] = mapped_column(TS)
    history_completed_at: Mapped[datetime | None] = mapped_column(TS)
    history_error: Mapped[str | None] = mapped_column(Text)
    history_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    poll_cursor_ms: Mapped[int | None] = mapped_column(BigInteger)  # account mode / gap fill
    last_activity_at: Mapped[datetime | None] = mapped_column(TS)

    __table_args__ = (Index("ix_watched_wallets_status", "status"),)


class HistoricalRecipient(Base):
    __tablename__ = "historical_recipients"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    victim_wallet: Mapped[str] = mapped_column(String(34), nullable=False)
    recipient_wallet: Mapped[str] = mapped_column(String(34), nullable=False)
    token_contract: Mapped[str] = mapped_column(String(34), nullable=False)
    transaction_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_amount: Mapped[int] = mapped_column(Amount, nullable=False, default=0)
    first_seen: Mapped[datetime] = mapped_column(TS, nullable=False)
    last_seen: Mapped[datetime] = mapped_column(TS, nullable=False)
    largest_amount: Mapped[int] = mapped_column(Amount, nullable=False, default=0)
    smallest_amount: Mapped[int] = mapped_column(Amount, nullable=False, default=0)
    average_amount: Mapped[int] = mapped_column(Amount, nullable=False, default=0)
    prefix_key: Mapped[str] = mapped_column(String(8), nullable=False)
    suffix_key: Mapped[str] = mapped_column(String(8), nullable=False)
    flagged_suspicious: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False)

    __table_args__ = (
        UniqueConstraint("victim_wallet", "recipient_wallet", "token_contract", name="uq_hist_recipient"),
        Index("ix_hist_victim_prefix", "victim_wallet", "token_contract", "prefix_key"),
        Index("ix_hist_victim_suffix", "victim_wallet", "token_contract", "suffix_key"),
        Index("ix_hist_victim_count", "victim_wallet", "token_contract", "transaction_count"),
        Index("ix_hist_recipient", "recipient_wallet"),
        Index("ix_hist_last_seen", "last_seen"),
    )


class Transaction(Base):
    __tablename__ = "transactions"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    transfer_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    tx_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    log_index: Mapped[int | None] = mapped_column(Integer)
    token_contract: Mapped[str] = mapped_column(String(34), nullable=False)
    from_address: Mapped[str] = mapped_column(String(34), nullable=False)
    to_address: Mapped[str] = mapped_column(String(34), nullable=False)
    amount: Mapped[int] = mapped_column(Amount, nullable=False)
    block_number: Mapped[int | None] = mapped_column(BigInteger)
    block_timestamp: Mapped[datetime] = mapped_column(TS, nullable=False)
    confirmation_status: Mapped[str] = mapped_column(String(16), nullable=False)
    initiator_address: Mapped[str | None] = mapped_column(String(34))
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    analysis_status: Mapped[str] = mapped_column(String(16), nullable=False)
    analysis_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    analysis_error: Mapped[str | None] = mapped_column(Text)
    detected_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False)

    __table_args__ = (
        Index("ix_tx_hash", "tx_hash"),
        Index("ix_tx_from_ts", "from_address", "block_timestamp"),
        Index("ix_tx_to_ts", "to_address", "block_timestamp"),
        Index("ix_tx_analysis", "analysis_status", "id"),
        Index("ix_tx_confirmation", "confirmation_status", "block_number"),
        Index("ix_tx_source_ts", "source", "block_timestamp"),
    )


class AddressSimilarityMatch(Base):
    __tablename__ = "address_similarity_matches"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id", ondelete="CASCADE"))
    victim_wallet: Mapped[str] = mapped_column(String(34), nullable=False)
    legitimate_recipient: Mapped[str] = mapped_column(String(34), nullable=False)
    suspicious_recipient: Mapped[str] = mapped_column(String(34), nullable=False)
    prefix_match_length: Mapped[int] = mapped_column(Integer, nullable=False)
    suffix_match_length: Mapped[int] = mapped_column(Integer, nullable=False)
    prefix_similarity: Mapped[float] = mapped_column(Float, nullable=False)
    suffix_similarity: Mapped[float] = mapped_column(Float, nullable=False)
    overall_similarity: Mapped[float] = mapped_column(Float, nullable=False)
    positional_similarity: Mapped[float] = mapped_column(Float, nullable=False)
    similarity_score: Mapped[float] = mapped_column(Float, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False)

    __table_args__ = (
        UniqueConstraint("transaction_id", "victim_wallet", "legitimate_recipient", name="uq_similarity_tx_legit"),
        Index("ix_similarity_suspicious", "suspicious_recipient"),
    )


class PoisoningEvent(Base):
    __tablename__ = "poisoning_events"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    case_id: Mapped[str | None] = mapped_column(String(40), unique=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False)
    tx_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    victim_wallet: Mapped[str] = mapped_column(String(34), nullable=False)
    legitimate_recipient: Mapped[str] = mapped_column(String(34), nullable=False)
    suspicious_recipient: Mapped[str] = mapped_column(String(34), nullable=False)
    token_contract: Mapped[str] = mapped_column(String(34), nullable=False)
    token_symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    token_decimals: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[int] = mapped_column(Amount, nullable=False)
    block_number: Mapped[int | None] = mapped_column(BigInteger)
    block_timestamp: Mapped[datetime] = mapped_column(TS, nullable=False)
    confirmation_status: Mapped[str] = mapped_column(String(16), nullable=False)
    similarity_score: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False)
    fast_confidence: Mapped[int] = mapped_column(Integer, nullable=False)
    score_breakdown: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    legit_tx_count: Mapped[int] = mapped_column(Integer, nullable=False)
    legit_total_amount: Mapped[int] = mapped_column(Amount, nullable=False)
    suspicious_prior_tx_count: Mapped[int] = mapped_column(Integer, nullable=False)
    poisoning_tx_observed: Mapped[str] = mapped_column(String(8), nullable=False)
    forwarding_summary: Mapped[str | None] = mapped_column(Text)
    initiator_address: Mapped[str | None] = mapped_column(String(34))
    is_historical: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    history_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    investigation_status: Mapped[str] = mapped_column(String(16), nullable=False)
    trace_status: Mapped[str] = mapped_column(String(16), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    analysis_started_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    analysis_completed_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    alert_sent_at: Mapped[datetime | None] = mapped_column(TS)
    detection_latency_ms: Mapped[int | None] = mapped_column(Integer)  # detected -> analysis complete
    alert_latency_ms: Mapped[int | None] = mapped_column(Integer)  # detected -> telegram accepted
    chain_latency_ms: Mapped[int | None] = mapped_column(BigInteger)  # block timestamp -> detected
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False)

    __table_args__ = (
        UniqueConstraint("transaction_id", "victim_wallet", name="uq_event_tx_victim"),
        Index("ix_events_victim", "victim_wallet", "created_at"),
        Index("ix_events_suspicious", "suspicious_recipient"),
        Index("ix_events_type", "event_type", "created_at"),
        Index("ix_events_tx_hash", "tx_hash"),
    )


class PoisoningEvidence(Base):
    __tablename__ = "poisoning_evidence"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("poisoning_events.id", ondelete="CASCADE"), nullable=False)
    evidence_key: Mapped[str] = mapped_column(String(160), nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(48), nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)  # FACT / ANALYSIS
    supports: Mapped[bool] = mapped_column(Boolean, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    tx_hash: Mapped[str | None] = mapped_column(String(64))
    address: Mapped[str | None] = mapped_column(String(34))
    amount: Mapped[int | None] = mapped_column(Amount)
    observed_at: Mapped[datetime | None] = mapped_column(TS)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False)

    __table_args__ = (UniqueConstraint("event_id", "evidence_key", name="uq_evidence_key"),)


class FundTrace(Base):
    __tablename__ = "fund_traces"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("poisoning_events.id", ondelete="CASCADE"), nullable=False)
    trace_run: Mapped[int] = mapped_column(Integer, nullable=False)
    hop: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_address: Mapped[str] = mapped_column(String(34), nullable=False)
    from_address: Mapped[str] = mapped_column(String(34), nullable=False)
    to_address: Mapped[str] = mapped_column(String(34), nullable=False)
    amount: Mapped[int] = mapped_column(Amount, nullable=False)
    token_contract: Mapped[str] = mapped_column(String(34), nullable=False)
    token_symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    tx_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    transfer_key: Mapped[str] = mapped_column(String(64), nullable=False)
    block_number: Mapped[int | None] = mapped_column(BigInteger)
    block_timestamp: Mapped[datetime] = mapped_column(TS, nullable=False)
    confirmation_status: Mapped[str] = mapped_column(String(16), nullable=False)
    to_label: Mapped[str | None] = mapped_column(String(200))
    to_label_category: Mapped[str | None] = mapped_column(String(32))
    to_label_source: Mapped[str | None] = mapped_column(String(64))
    terminal_reason: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False)

    __table_args__ = (
        UniqueConstraint("event_id", "trace_run", "transfer_key", name="uq_trace_transfer"),
        Index("ix_trace_event", "event_id", "trace_run", "hop"),
    )


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    dedup_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("poisoning_events.id", ondelete="CASCADE"))
    alert_type: Mapped[str] = mapped_column(String(32), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(TS)

    __table_args__ = (Index("ix_alerts_pending", "status", "next_attempt_at"),)


class Job(Base):
    """Durable background work (investigation, fund trace, retrospective scan)."""

    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    ref: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    next_run_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False)

    __table_args__ = (
        UniqueConstraint("job_type", "ref", name="uq_job_ref"),
        Index("ix_jobs_due", "status", "next_run_at"),
    )


class TelegramUser(Base):
    __tablename__ = "telegram_users"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    command_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unauthorized_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_seen: Mapped[datetime] = mapped_column(TS, nullable=False)
    last_seen: Mapped[datetime] = mapped_column(TS, nullable=False)


class SystemLog(Base):
    __tablename__ = "system_logs"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(TS, nullable=False)
    level: Mapped[str] = mapped_column(String(10), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    wallet: Mapped[str | None] = mapped_column(String(34))
    tx_hash: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (Index("ix_system_logs_ts", "ts"),)


class MonitorState(Base):
    __tablename__ = "monitor_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False)


class XPostDraft(Base):
    __tablename__ = "x_post_drafts"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("poisoning_events.id", ondelete="CASCADE"), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # DRAFT / POSTED / CANCELLED / FAILED
    tweet_id: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False)


class AddressLabelCache(Base):
    __tablename__ = "address_labels"
    address: Mapped[str] = mapped_column(String(34), primary_key=True)
    label: Mapped[str | None] = mapped_column(String(200))
    category: Mapped[str | None] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TS, nullable=False)
