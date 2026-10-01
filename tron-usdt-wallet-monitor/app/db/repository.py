"""All SQL lives here.  Functions take an ``AsyncSession``; callers own the transaction."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.amounts import base_to_usdt
from app.db.models import Alert, Checkpoint, SystemState, TransferRow, Wallet
from app.domain import (
    ALERT_LARGE_TRANSFER,
    ALERT_PENDING,
    ALERT_SENDING,
    ALERT_SENT,
    WALLET_DISCOVERED,
    WALLET_ROOT,
    Transfer,
    utcnow,
)

# ------------------------------------------------------------------ state


async def get_state(s: AsyncSession, key: str) -> str | None:
    return await s.scalar(select(SystemState.value).where(SystemState.key == key))


async def set_state(s: AsyncSession, key: str, value: str) -> None:
    stmt = pg_insert(SystemState).values(key=key, value=value, updated_at=utcnow())
    await s.execute(stmt.on_conflict_do_update(index_elements=[SystemState.key], set_={"value": value, "updated_at": utcnow()}))


# ------------------------------------------------------------------ wallets


async def ensure_root_wallet(s: AsyncSession, address: str) -> None:
    stmt = pg_insert(Wallet).values(
        address=address, wallet_type=WALLET_ROOT, root_wallet=address, hop=0, discovered_at=utcnow(), active=True
    )
    await s.execute(stmt.on_conflict_do_nothing(index_elements=[Wallet.address]))


async def insert_discovered_wallet(
    s: AsyncSession, *, address: str, root: str, parent: str, hop: int, t: Transfer
) -> bool:
    """Insert a discovered wallet; returns False if the address already exists."""
    stmt = (
        pg_insert(Wallet)
        .values(
            address=address,
            wallet_type=WALLET_DISCOVERED,
            root_wallet=root,
            hop=hop,
            discovered_from=parent,
            discovered_at=t.timestamp,
            first_seen_tx=t.tx_hash,
            first_seen_amount_base_units=t.amount_base_units,
            active=True,
        )
        .on_conflict_do_nothing(index_elements=[Wallet.address])
        .returning(Wallet.id)
    )
    return (await s.execute(stmt)).scalar_one_or_none() is not None


async def load_wallets(s: AsyncSession) -> list[Wallet]:
    return list((await s.scalars(select(Wallet))).all())


async def wallets_page(s: AsyncSession, page: int, size: int) -> tuple[list[Wallet], int]:
    q = select(Wallet).where(Wallet.wallet_type == WALLET_DISCOVERED)
    total = await s.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = (await s.scalars(q.order_by(Wallet.discovered_at.desc(), Wallet.id.desc()).offset((page - 1) * size).limit(size))).all()
    return list(rows), int(total)


# ------------------------------------------------------------------ transfers


async def insert_transfer(s: AsyncSession, t: Transfer, *, kind: str, source: str) -> bool:
    """Insert once; returns False if ``(tx_hash, event_index)`` was already stored."""
    stmt = (
        pg_insert(TransferRow)
        .values(
            tx_hash=t.tx_hash,
            event_index=t.event_index,
            block_number=t.block_number,
            timestamp=t.timestamp,
            from_address=t.from_address,
            to_address=t.to_address,
            amount_base_units=t.amount_base_units,
            amount_usdt=base_to_usdt(t.amount_base_units),
            contract_address=t.contract_address,
            kind=kind,
            confirmed=t.confirmed,
            source=source,
        )
        .on_conflict_do_nothing(index_elements=[TransferRow.tx_hash, TransferRow.event_index])
        .returning(TransferRow.id)
    )
    return (await s.execute(stmt)).scalar_one_or_none() is not None


async def known_transfers(s: AsyncSession, hashes: Iterable[str]) -> set[tuple[str, str, str, int]]:
    """Stored transfers of these transactions as (tx_hash, from, to, amount) tuples."""
    hashes = list(set(hashes))
    if not hashes:
        return set()
    q = select(TransferRow.tx_hash, TransferRow.from_address, TransferRow.to_address, TransferRow.amount_base_units).where(
        TransferRow.tx_hash.in_(hashes)
    )
    return {(r[0], r[1], r[2], int(r[3])) for r in (await s.execute(q)).all()}


# ------------------------------------------------------------------ alerts


async def insert_alert(s: AsyncSession, **values: Any) -> int | None:
    """Create an alert at most once per (tx_hash, event_index, alert_type)."""
    stmt = (
        pg_insert(Alert)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[Alert.tx_hash, Alert.event_index, Alert.alert_type])
        .returning(Alert.id)
    )
    return (await s.execute(stmt)).scalar_one_or_none()


async def pending_alerts(s: AsyncSession, limit: int = 20) -> list[Alert]:
    q = select(Alert).where(Alert.status == ALERT_PENDING).order_by(Alert.id).limit(limit)
    return list((await s.scalars(q)).all())


async def claim_alert(s: AsyncSession, alert_id: int) -> bool:
    """pending -> sending (atomic; only one claimer wins)."""
    res = await s.execute(
        update(Alert)
        .where(Alert.id == alert_id, Alert.status == ALERT_PENDING)
        .values(status=ALERT_SENDING, attempts=Alert.attempts + 1)
    )
    return res.rowcount == 1


async def mark_sent(s: AsyncSession, alert_id: int, message_id: str | None) -> None:
    await s.execute(
        update(Alert).where(Alert.id == alert_id).values(status=ALERT_SENT, telegram_message_id=message_id, sent_at=utcnow(), last_error=None)
    )


async def mark_failed(s: AsyncSession, alert_id: int, error: str) -> None:
    await s.execute(update(Alert).where(Alert.id == alert_id).values(status=ALERT_PENDING, last_error=error[:500]))


async def requeue_interrupted(s: AsyncSession) -> int:
    """Alerts left in 'sending' by a crash go back to 'pending'."""
    res = await s.execute(update(Alert).where(Alert.status == ALERT_SENDING).values(status=ALERT_PENDING, last_error="interrupted"))
    return res.rowcount or 0


async def recent_alerts(s: AsyncSession, limit: int = 10) -> list[Alert]:
    q = select(Alert).where(Alert.alert_type == ALERT_LARGE_TRANSFER).order_by(Alert.transfer_timestamp.desc(), Alert.id.desc()).limit(limit)
    return list((await s.scalars(q)).all())


# ------------------------------------------------------------------ checkpoints


async def get_checkpoint(s: AsyncSession, key: str) -> Checkpoint | None:
    return await s.get(Checkpoint, key)


async def advance_checkpoint(s: AsyncSession, key: str, ts: datetime, block: int | None = None) -> None:
    """Upsert; a checkpoint only ever moves forward."""
    stmt = pg_insert(Checkpoint).values(wallet_address=key, last_timestamp=ts, last_block=block, updated_at=utcnow())
    stmt = stmt.on_conflict_do_update(
        index_elements=[Checkpoint.wallet_address],
        set_={
            "last_timestamp": func.greatest(Checkpoint.last_timestamp, stmt.excluded.last_timestamp),
            "last_block": func.coalesce(func.greatest(Checkpoint.last_block, stmt.excluded.last_block), Checkpoint.last_block),
            "updated_at": utcnow(),
        },
    )
    await s.execute(stmt)


async def init_checkpoint(s: AsyncSession, key: str, ts: datetime) -> None:
    """Create only if missing (never moves an existing checkpoint)."""
    stmt = pg_insert(Checkpoint).values(wallet_address=key, last_timestamp=ts, updated_at=utcnow())
    await s.execute(stmt.on_conflict_do_nothing(index_elements=[Checkpoint.wallet_address]))


# ------------------------------------------------------------------ stats


async def stats(s: AsyncSession) -> dict[str, Any]:
    day_ago = utcnow() - timedelta(hours=24)
    large = Alert.alert_type == ALERT_LARGE_TRANSFER
    discovered = await s.scalar(select(func.count()).select_from(Wallet).where(Wallet.wallet_type == WALLET_DISCOVERED))
    monitored = await s.scalar(
        select(func.count()).select_from(Wallet).where(Wallet.wallet_type == WALLET_DISCOVERED, Wallet.active.is_(True))
    )
    transfers = await s.scalar(select(func.count()).select_from(TransferRow))
    alerts = await s.scalar(select(func.count()).select_from(Alert).where(large))
    alerts_24h = await s.scalar(select(func.count()).select_from(Alert).where(large, Alert.transfer_timestamp >= day_ago))
    sent = await s.scalar(select(func.count()).select_from(Alert).where(Alert.status == ALERT_SENT))
    pending = await s.scalar(select(func.count()).select_from(Alert).where(Alert.status == ALERT_PENDING))
    largest = (
        await s.execute(select(Alert).where(large).order_by(Alert.amount_base_units.desc(), Alert.id).limit(1))
    ).scalar_one_or_none()
    return {
        "discovered": int(discovered or 0),
        "monitored": int(monitored or 0),
        "transfers": int(transfers or 0),
        "alerts": int(alerts or 0),
        "alerts_24h": int(alerts_24h or 0),
        "sent": int(sent or 0),
        "pending": int(pending or 0),
        "largest": largest,
    }
