"""Network-wide address-poisoning detection (``NETWORK_WIDE=true``, block mode).

Every USDT transfer on TRON passes through here, not only transfers of wallets
added with /add.  Per block the scanner:

1. Looks up, with ONE indexed query, the recent recipients of every sender in
   the block that share a folded prefix/suffix key with the address being paid
   (and, for dust transfers, with the address sending the dust).
2. Runs the similarity engine on those few candidates in memory.
3. Sends the rare look-alike payments through the full detector (incident,
   confidence, Telegram alert, investigation, fund trace) as source NETWORK.
4. Stores dust / zero-value transfers from look-alikes as evidence rows, so a
   later payment to that look-alike is scored with "poisoning transfer observed".
5. Bulk-upserts all other payments into ``historical_recipients`` - the
   network-wide "who paid whom" memory - in the same DB transaction that
   advances the block cursor, so a crash/replay never double counts.

Memory is kept for ``NETWORK_MEMORY_DAYS`` (default 7) and pruned hourly;
wallets added with /add keep their full history forever.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, select, tuple_

from app import repository as repo
from app.database import with_db_retry
from app.domain import AnalysisStatus, BlockData, TokenTransfer
from app.models import HistoricalRecipient, PoisoningEvent, Transaction, WatchedWallet
from app.services.poisoning_detector import Candidate, PoisoningDetector, WalletRegistry
from app.services.similarity import candidate_keys
from app.utils.clock import Clock, from_ms
from app.utils.logging import get_logger

log = get_logger(__name__)

STATE_BLOCK_CURSOR = "block_cursor"
STATE_BLOCK_CURSOR_TS = "block_cursor_ts"


class NetworkScanner:
    def __init__(self, settings, session_factory, clock: Clock, registry: WalletRegistry, detector: PoisoningDetector) -> None:
        self.s = settings
        self.sf = session_factory
        self.clock = clock
        self.registry = registry
        self.detector = detector
        self.token = settings.primary_token
        self.key_len = settings.candidate_key_length
        self.stats = {"blocks": 0, "transfers": 0, "pairs_upserted": 0, "lookalike_payments": 0, "dust_evidence": 0, "events": 0, "pruned": 0}

    # ------------------------------------------------------------------ per block
    async def process(self, blk: BlockData, detected: datetime) -> list[int]:
        """Analyse one block network-wide and advance the block cursor atomically."""
        rest = [
            t for t in blk.transfers
            if t.token_contract == self.token.contract and t.from_address != t.to_address and not self.registry.is_monitored(t.from_address)
        ]  # fmt: skip
        self.stats["blocks"] += 1
        self.stats["transfers"] += len(rest)
        dust_max = self.detector.dust_max

        lookups: set[tuple[str, str]] = set()  # (owner, folded key) prefix lookups
        lookups_sfx: set[tuple[str, str]] = set()
        for t in rest:
            if t.amount > 0:  # payment / zero-value spoof: owner = sender, compare the recipient
                pk, sk = candidate_keys(t.to_address, self.key_len)
                lookups.add((t.from_address, pk))
                lookups_sfx.add((t.from_address, sk))
            if t.amount <= dust_max:  # dust: owner = receiver, compare the dust sender
                pk, sk = candidate_keys(t.from_address, self.key_len)
                lookups.add((t.to_address, pk))
                lookups_sfx.add((t.to_address, sk))
            if t.amount == 0:
                pk, sk = candidate_keys(t.to_address, self.key_len)
                lookups.add((t.from_address, pk))
                lookups_sfx.add((t.from_address, sk))

        by_owner = await with_db_retry(lambda: self._candidates(lookups, lookups_sfx), what="network_candidates") if rest else {}

        lookalike: list[TokenTransfer] = []
        evidence: list[TokenTransfer] = []
        plain: list[TokenTransfer] = []
        for t in rest:
            if t.amount > 0 and self._matches(by_owner, t.from_address, t.to_address):
                lookalike.append(t)
                continue
            if t.amount <= dust_max and (
                self._matches(by_owner, t.to_address, t.from_address) or (t.amount == 0 and self._matches(by_owner, t.from_address, t.to_address))
            ):
                evidence.append(t)
            if t.amount > 0:
                plain.append(t)

        events: list[int] = []
        if evidence:
            await with_db_retry(lambda: self._store(evidence, "NETWORK_DUST", AnalysisStatus.DONE.value, detected), what="network_dust")
            self.stats["dust_evidence"] += len(evidence)
        if lookalike:
            self.stats["lookalike_payments"] += len(lookalike)
            new = await with_db_retry(lambda: self._store(lookalike, "NETWORK", AnalysisStatus.PENDING.value, detected), what="network_lookalike")
            for tx_id, t in new:
                try:
                    events += await with_db_retry(
                        lambda i=tx_id, x=t: self.detector.analyze(i, from_address=x.from_address, to_address=x.to_address), what="analyze"
                    )
                except Exception:  # noqa: BLE001 - stays PENDING; the recovery worker retries it
                    log.exception("NETWORK_ANALYSIS_FAILED", tx=t.tx_hash)
        self.stats["events"] += len(events)
        await with_db_retry(lambda: self._upsert_and_advance(plain, blk), what="network_upsert")
        return events

    def _matches(self, by_owner: dict[str, list[Candidate]], owner: str, address: str) -> bool:
        cands = by_owner.get(owner)
        return bool(cands) and bool(self.detector.engine.matches(address, cands))

    async def _candidates(self, lookups, lookups_sfx) -> dict[str, list[Candidate]]:
        HR = HistoricalRecipient
        out: dict[str, list[Candidate]] = {}
        async with self.sf() as s:
            lp, ls = list(lookups), list(lookups_sfx)
            for i in range(0, max(len(lp), len(ls)), 400):
                q = select(HR).where(
                    HR.token_contract == self.token.contract,
                    or_(tuple_(HR.victim_wallet, HR.prefix_key).in_(lp[i : i + 400] or [("", "")]),
                        tuple_(HR.victim_wallet, HR.suffix_key).in_(ls[i : i + 400] or [("", "")])),
                )  # fmt: skip
                for r in (await s.execute(q)).scalars():
                    out.setdefault(r.victim_wallet, []).append(Candidate(r.recipient_wallet, repo.stats_from_row(r), r.flagged_suspicious))
        return out

    async def _store(self, transfers, source: str, status: str, detected: datetime):
        async with self.sf() as s, s.begin():
            return await repo.insert_transfers(s, transfers, source=source, detected_at=detected, now=self.clock.now(), analysis_status=status)

    async def _upsert_and_advance(self, payments: list[TokenTransfer], blk: BlockData) -> None:
        now = self.clock.now()
        agg: dict[tuple[str, str], list] = {}
        for t in payments:
            ts = from_ms(t.block_timestamp_ms)
            a = agg.get((t.from_address, t.to_address))
            if a is None:
                agg[(t.from_address, t.to_address)] = [1, t.amount, ts, ts, t.amount, t.amount]
            else:
                a[0] += 1
                a[1] += t.amount
                a[2], a[3] = min(a[2], ts), max(a[3], ts)
                a[4], a[5] = max(a[4], t.amount), min(a[5], t.amount)
        async with self.sf() as s, s.begin():
            pg = repo.is_pg(s)
            least, greatest = (func.least, func.greatest) if pg else (func.min, func.max)
            HR = HistoricalRecipient
            rows = []
            for (sender, rcpt), (n, total, first, last, mx, mn) in agg.items():
                pk, sk = candidate_keys(rcpt, self.key_len)
                rows.append(dict(
                    victim_wallet=sender, recipient_wallet=rcpt, token_contract=self.token.contract, transaction_count=n,
                    total_amount=total, first_seen=first, last_seen=last, largest_amount=mx, smallest_amount=mn,
                    average_amount=total // n, prefix_key=pk, suffix_key=sk, flagged_suspicious=False, updated_at=now,
                ))  # fmt: skip
            for i in range(0, len(rows), 500):
                stmt = repo._insert(s, HR).values(rows[i : i + 500])
                ex = stmt.excluded
                total_expr = HR.total_amount + ex.total_amount
                count_expr = HR.transaction_count + ex.transaction_count
                stmt = stmt.on_conflict_do_update(
                    index_elements=["victim_wallet", "recipient_wallet", "token_contract"],
                    set_={
                        "transaction_count": count_expr,
                        "total_amount": total_expr,
                        "first_seen": least(HR.first_seen, ex.first_seen),
                        "last_seen": greatest(HR.last_seen, ex.last_seen),
                        "largest_amount": greatest(HR.largest_amount, ex.largest_amount),
                        "smallest_amount": least(HR.smallest_amount, ex.smallest_amount),
                        "average_amount": func.div(total_expr, count_expr) if pg else total_expr / count_expr,
                        "updated_at": ex.updated_at,
                    },
                )
                await s.execute(stmt)
            await repo.set_state(s, STATE_BLOCK_CURSOR, str(blk.number), now)
            await repo.set_state(s, STATE_BLOCK_CURSOR_TS, str(blk.timestamp_ms), now)
        self.stats["pairs_upserted"] += len(agg)

    # ------------------------------------------------------------------ retention
    async def prune(self, batch: int = 20_000) -> int:
        """Forget network-wide payment memory older than NETWORK_MEMORY_DAYS (watched wallets are kept)."""
        cutoff = self.clock.now() - timedelta(days=self.s.network_memory_days)
        HR = HistoricalRecipient
        watched = select(WatchedWallet.address)
        removed = 0
        while True:
            async with self.sf() as s, s.begin():
                ids = select(HR.id).where(HR.last_seen < cutoff, HR.flagged_suspicious.is_(False), HR.victim_wallet.not_in(watched)).limit(batch)
                n = (await s.execute(delete(HR).where(HR.id.in_(ids)))).rowcount or 0
            removed += n
            if n < batch:
                break
        while True:
            async with self.sf() as s, s.begin():
                ids = (
                    select(Transaction.id)
                    .where(
                        Transaction.source.in_(["NETWORK", "NETWORK_DUST"]),
                        Transaction.block_timestamp < cutoff,
                        Transaction.analysis_status != AnalysisStatus.PENDING.value,
                        Transaction.id.not_in(select(PoisoningEvent.transaction_id)),
                    )
                    .limit(batch)
                )
                n = (await s.execute(delete(Transaction).where(Transaction.id.in_(ids)))).rowcount or 0
            removed += n
            if n < batch:
                break
        if removed:
            log.info("NETWORK_MEMORY_PRUNED", rows=removed, older_than_days=self.s.network_memory_days)
        self.stats["pruned"] += removed
        return removed

    async def remembered_pairs(self) -> int:
        async with self.sf() as s:
            return (await s.execute(select(func.count()).select_from(HistoricalRecipient))).scalar_one()
