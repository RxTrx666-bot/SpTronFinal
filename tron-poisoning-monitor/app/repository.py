"""Data-access helpers.  All writes are idempotent (ON CONFLICT DO NOTHING /
unique keys), so replays, polling overlap and crash recovery never duplicate
transactions, incidents, evidence or alerts."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import AlertStatus, AnalysisStatus, Confirmation, TaskStatus, TokenTransfer, WalletStatus
from app.models import (
    Alert,
    HistoricalRecipient,
    Job,
    MonitorState,
    PoisoningEvent,
    Transaction,
    WatchedWallet,
)
from app.services.risk_engine import RecipientStats
from app.services.similarity import candidate_keys
from app.utils.clock import as_utc, from_ms


def _insert(session: AsyncSession, model):
    return (postgresql.insert if session.bind.dialect.name == "postgresql" else sqlite.insert)(model)


def is_pg(session: AsyncSession) -> bool:
    return session.bind.dialect.name == "postgresql"


# ---------------------------------------------------------------- state
async def get_state(session: AsyncSession, key: str) -> str | None:
    row = await session.get(MonitorState, key)
    return row.value if row else None


async def set_state(session: AsyncSession, key: str, value: str, now: datetime) -> None:
    stmt = _insert(session, MonitorState).values(key=key, value=value, updated_at=now)
    stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value, "updated_at": now})
    await session.execute(stmt)


# ---------------------------------------------------------------- wallets
async def get_wallet(session: AsyncSession, address: str) -> WatchedWallet | None:
    return (await session.execute(select(WatchedWallet).where(WatchedWallet.address == address))).scalar_one_or_none()


async def monitored_wallets(session: AsyncSession) -> dict[str, str]:
    """address -> status for every wallet that is collected (ACTIVE or PAUSED)."""
    rows = await session.execute(select(WatchedWallet.address, WatchedWallet.status).where(WatchedWallet.status != WalletStatus.REMOVED.value))
    return {a: s for a, s in rows.all()}


# ---------------------------------------------------------------- transactions
async def insert_transfers(
    session: AsyncSession,
    transfers: list[TokenTransfer],
    *,
    source: str,
    detected_at: datetime,
    now: datetime,
    analysis_status: str = AnalysisStatus.PENDING.value,
) -> list[tuple[int, TokenTransfer]]:
    """Insert transfers; return (id, transfer) for rows that were NEW (idempotent on transfer_key)."""
    new_ids: list[tuple[int, TokenTransfer]] = []
    for t in transfers:
        stmt = (
            _insert(session, Transaction)
            .values(
                transfer_key=t.transfer_key,
                tx_hash=t.tx_hash,
                seq=t.seq,
                log_index=t.log_index,
                token_contract=t.token_contract,
                from_address=t.from_address,
                to_address=t.to_address,
                amount=t.amount,
                block_number=t.block_number,
                block_timestamp=from_ms(t.block_timestamp_ms),
                confirmation_status=(Confirmation.CONFIRMED if t.confirmed else Confirmation.UNCONFIRMED).value,
                initiator_address=t.initiator,
                source=source,
                analysis_status=analysis_status,
                analysis_attempts=0,
                detected_at=detected_at,
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=["transfer_key"])
            .returning(Transaction.id)
        )
        res = (await session.execute(stmt)).scalar_one_or_none()
        if res is not None:
            new_ids.append((res, t))
        else:
            # Already known: enrich missing details (block number / initiator / confirmation).
            values: dict[str, Any] = {}
            if t.block_number is not None:
                values["block_number"] = t.block_number
            if t.initiator:
                values["initiator_address"] = t.initiator
            if values:
                await session.execute(
                    update(Transaction)
                    .where(Transaction.transfer_key == t.transfer_key)
                    .where(or_(Transaction.block_number.is_(None), Transaction.initiator_address.is_(None)))
                    .values(**values)
                )
            if t.confirmed:
                await session.execute(
                    update(Transaction)
                    .where(Transaction.transfer_key == t.transfer_key, Transaction.confirmation_status == Confirmation.UNCONFIRMED.value)
                    .values(confirmation_status=Confirmation.CONFIRMED.value)
                )
    return new_ids


async def pending_transaction_ids(session: AsyncSession, older_than: datetime, limit: int = 500) -> list[int]:
    rows = await session.execute(
        select(Transaction.id)
        .where(Transaction.analysis_status == AnalysisStatus.PENDING.value, Transaction.created_at <= older_than)
        .order_by(Transaction.block_timestamp, Transaction.id)
        .limit(limit)
    )
    return [r[0] for r in rows.all()]


# ---------------------------------------------------------------- recipients
def stats_from_row(row: HistoricalRecipient | None) -> RecipientStats:
    if row is None:
        return RecipientStats()
    return RecipientStats(
        transaction_count=row.transaction_count,
        total_amount=row.total_amount,
        first_seen=as_utc(row.first_seen),
        last_seen=as_utc(row.last_seen),
        largest_amount=row.largest_amount,
        smallest_amount=row.smallest_amount,
        average_amount=row.average_amount,
    )


async def get_recipient(session: AsyncSession, victim: str, recipient: str, token: str, *, lock: bool = False) -> HistoricalRecipient | None:
    q = select(HistoricalRecipient).where(
        HistoricalRecipient.victim_wallet == victim,
        HistoricalRecipient.recipient_wallet == recipient,
        HistoricalRecipient.token_contract == token,
    )
    if lock and is_pg(session):
        q = q.with_for_update()
    return (await session.execute(q)).scalar_one_or_none()


async def apply_payment(session: AsyncSession, victim: str, recipient: str, token: str, amount: int, ts: datetime, now: datetime, key_len: int = 3) -> None:
    """Add one victim -> recipient payment to the aggregate (call exactly once per transaction)."""
    row = await get_recipient(session, victim, recipient, token, lock=True)
    if row is None:
        pk, sk = candidate_keys(recipient, key_len)
        stmt = (
            _insert(session, HistoricalRecipient)
            .values(
                victim_wallet=victim,
                recipient_wallet=recipient,
                token_contract=token,
                transaction_count=1,
                total_amount=amount,
                first_seen=ts,
                last_seen=ts,
                largest_amount=amount,
                smallest_amount=amount,
                average_amount=amount,
                prefix_key=pk,
                suffix_key=sk,
                flagged_suspicious=False,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["victim_wallet", "recipient_wallet", "token_contract"])
            .returning(HistoricalRecipient.id)
        )
        if (await session.execute(stmt)).scalar_one_or_none() is not None:
            return
        row = await get_recipient(session, victim, recipient, token, lock=True)
        assert row is not None
    row.transaction_count += 1
    row.total_amount += amount
    row.first_seen = min(as_utc(row.first_seen), ts)
    row.last_seen = max(as_utc(row.last_seen), ts)
    row.largest_amount = max(row.largest_amount, amount)
    row.smallest_amount = min(row.smallest_amount, amount)
    row.average_amount = row.total_amount // row.transaction_count
    row.updated_at = now


async def flag_recipient(session: AsyncSession, victim: str, recipient: str, token: str) -> None:
    await session.execute(
        update(HistoricalRecipient)
        .where(
            HistoricalRecipient.victim_wallet == victim,
            HistoricalRecipient.recipient_wallet == recipient,
            HistoricalRecipient.token_contract == token,
        )
        .values(flagged_suspicious=True)
    )


async def similarity_candidates(session: AsyncSession, victim: str, token: str, address: str, key_len: int, limit: int) -> list[HistoricalRecipient]:
    """Victim's recipients sharing a folded prefix or suffix key with ``address`` (indexed lookup)."""
    pk, sk = candidate_keys(address, key_len)
    q = (
        select(HistoricalRecipient)
        .where(
            HistoricalRecipient.victim_wallet == victim,
            HistoricalRecipient.token_contract == token,
            HistoricalRecipient.recipient_wallet != address,
            or_(HistoricalRecipient.prefix_key == pk, HistoricalRecipient.suffix_key == sk),
        )
        .order_by(HistoricalRecipient.transaction_count.desc())
        .limit(limit)
    )
    return list((await session.execute(q)).scalars().all())


async def top_recipients(session: AsyncSession, victim: str, token: str, limit: int = 10) -> list[HistoricalRecipient]:
    q = (
        select(HistoricalRecipient)
        .where(HistoricalRecipient.victim_wallet == victim, HistoricalRecipient.token_contract == token)
        .order_by(HistoricalRecipient.transaction_count.desc(), HistoricalRecipient.total_amount.desc())
        .limit(limit)
    )
    return list((await session.execute(q)).scalars().all())


async def count_recipients(session: AsyncSession, victim: str) -> int:
    return (await session.execute(select(func.count()).select_from(HistoricalRecipient).where(HistoricalRecipient.victim_wallet == victim))).scalar_one()


async def other_victims_paying(session: AsyncSession, suspicious: str, victim: str, token: str) -> list[str]:
    rows = await session.execute(
        select(HistoricalRecipient.victim_wallet).where(
            HistoricalRecipient.recipient_wallet == suspicious,
            HistoricalRecipient.token_contract == token,
            HistoricalRecipient.victim_wallet != victim,
        )
    )
    return sorted({r[0] for r in rows.all()})


async def local_dust_evidence(session: AsyncSession, victim: str, suspicious: str, token: str, before: datetime, dust_max: int) -> list[Transaction]:
    """Recorded dust from suspicious -> victim, or zero-value transfers victim -> suspicious, before ``before``."""
    q = (
        select(Transaction)
        .where(
            Transaction.token_contract == token,
            Transaction.block_timestamp <= before,
            or_(
                and_(Transaction.from_address == suspicious, Transaction.to_address == victim, Transaction.amount <= dust_max),
                and_(Transaction.from_address == victim, Transaction.to_address == suspicious, Transaction.amount == 0),
            ),
        )
        .order_by(Transaction.block_timestamp)
        .limit(20)
    )
    return list((await session.execute(q)).scalars().all())


# ---------------------------------------------------------------- events
async def events_for_suspicious(session: AsyncSession, suspicious: str) -> list[PoisoningEvent]:
    q = select(PoisoningEvent).where(PoisoningEvent.suspicious_recipient == suspicious).order_by(PoisoningEvent.id)
    return list((await session.execute(q)).scalars().all())


async def event_by_case(session: AsyncSession, case: str) -> PoisoningEvent | None:
    case = case.strip()
    if case.isdigit():
        return await session.get(PoisoningEvent, int(case))
    return (await session.execute(select(PoisoningEvent).where(PoisoningEvent.case_id == case.upper()))).scalar_one_or_none()


def make_case_id(event_id: int, ts: datetime) -> str:
    return f"TRON-POISON-{ts:%Y%m%d}-{event_id:06d}"


# ---------------------------------------------------------------- alerts (outbox)
async def enqueue_alert(
    session: AsyncSession,
    *,
    dedup_key: str,
    chat_id: int,
    alert_type: str,
    payload: dict[str, Any],
    now: datetime,
    event_id: int | None = None,
) -> bool:
    stmt = (
        _insert(session, Alert)
        .values(
            dedup_key=dedup_key[:200],
            event_id=event_id,
            alert_type=alert_type,
            chat_id=chat_id,
            payload=payload,
            status=AlertStatus.PENDING.value,
            attempts=0,
            next_attempt_at=now,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=["dedup_key"])
        .returning(Alert.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def due_alerts(session: AsyncSession, now: datetime, limit: int = 20) -> list[Alert]:
    q = select(Alert).where(Alert.status == AlertStatus.PENDING.value, Alert.next_attempt_at <= now).order_by(Alert.id).limit(limit)
    return list((await session.execute(q)).scalars().all())


# ---------------------------------------------------------------- jobs
async def enqueue_job(
    session: AsyncSession, job_type: str, ref: str, now: datetime, payload: dict | None = None, *, reset: bool = False, run_at: datetime | None = None
) -> None:
    stmt = _insert(session, Job).values(
        job_type=job_type,
        ref=ref,
        status=TaskStatus.PENDING.value,
        attempts=0,
        payload=payload or {},
        next_run_at=run_at or now,
        created_at=now,
        updated_at=now,
    )
    if reset:
        stmt = stmt.on_conflict_do_update(
            index_elements=["job_type", "ref"],
            set_={"status": TaskStatus.PENDING.value, "attempts": 0, "next_run_at": now, "updated_at": now, "last_error": None},
        )
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=["job_type", "ref"])
    await session.execute(stmt)


async def claim_due_jobs(session: AsyncSession, now: datetime, job_types: list[str], limit: int) -> list[Job]:
    q = (
        select(Job)
        .where(Job.status == TaskStatus.PENDING.value, Job.next_run_at <= now, Job.job_type.in_(job_types))
        .order_by(Job.next_run_at, Job.id)
        .limit(limit)
    )
    if is_pg(session):
        q = q.with_for_update(skip_locked=True)
    jobs = list((await session.execute(q)).scalars().all())
    for j in jobs:
        j.status = TaskStatus.RUNNING.value
        j.attempts += 1
        j.updated_at = now
    return jobs


async def reset_running_jobs(session: AsyncSession, now: datetime) -> int:
    res = await session.execute(
        update(Job).where(Job.status == TaskStatus.RUNNING.value).values(status=TaskStatus.PENDING.value, next_run_at=now, updated_at=now)
    )
    return res.rowcount or 0


async def finish_job(
    session: AsyncSession, job_id: int, now: datetime, *, error: str | None = None, retry_in: float | None = None, max_attempts: int = 8
) -> None:
    job = await session.get(Job, job_id)
    if job is None:
        return
    job.updated_at = now
    if error is None:
        job.status = TaskStatus.DONE.value
        job.last_error = None
        return
    job.last_error = error[:500]
    if job.attempts >= max_attempts:
        job.status = TaskStatus.FAILED.value
    else:
        job.status = TaskStatus.PENDING.value
        job.next_run_at = now + timedelta(seconds=retry_in or 30)
