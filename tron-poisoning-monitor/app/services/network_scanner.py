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
from app.domain import AnalysisStatus, BlockData, Contact, TokenTransfer
from app.models import HistoricalRecipient, NetworkContact, PoisoningEvent, Transaction, WatchedWallet
from app.services.poisoning_detector import Candidate, PoisoningDetector, WalletRegistry
from app.services.risk_engine import RecipientStats
from app.services.similarity import candidate_keys
from app.utils.clock import Clock, from_ms
from app.utils.logging import get_logger
from app.utils.ratelimit import PRIORITY_DETECTION

log = get_logger(__name__)

STATE_BLOCK_CURSOR = "block_cursor"
STATE_BLOCK_CURSOR_TS = "block_cursor_ts"


class NetworkScanner:
    def __init__(self, settings, session_factory, clock: Clock, registry: WalletRegistry, detector: PoisoningDetector, source=None) -> None:
        self.s = settings
        self.source = source
        self._history_cache: dict[str, tuple[float, list[Candidate]]] = {}
        self.sf = session_factory
        self.clock = clock
        self.registry = registry
        self.detector = detector
        self.token = settings.primary_token
        self.key_len = settings.candidate_key_length
        self.stats = {
            "blocks": 0, "transfers": 0, "pairs_upserted": 0, "lookalike_payments": 0, "dust_evidence": 0,
            "contacts": 0, "contact_hits": 0, "history_lookups": 0, "events": 0, "pruned": 0,
        }  # fmt: skip

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

        # --- poisoning contacts of this block (fake tokens / tiny TRX / USDT dust / zero-value spoofs)
        contacts = list(blk.contacts)
        for t in blk.transfers:
            if t.token_contract == self.token.contract and t.from_address != t.to_address and t.amount <= dust_max:
                kind = "ZERO_VALUE" if t.amount == 0 else "USDT_DUST"
                contacts.append(Contact(t.from_address, t.to_address, kind, t.tx_hash, t.block_timestamp_ms, t.amount, t.token_contract))
                if t.amount == 0:  # transferFrom(victim, fake, 0) puts the fake in the victim's history
                    contacts.append(Contact(t.to_address, t.from_address, kind, t.tx_hash, t.block_timestamp_ms, 0, t.token_contract))
        self.stats["contacts"] += len(contacts)

        # --- payments to an address that earlier touched the payer's history: check against the payer's REAL history
        extra: dict[str, list[Candidate]] = {}
        min_lookup = self.detector.network_min_alert
        probe = [t for t in plain if t.amount >= min_lookup]
        if probe and self.source is not None:
            hits = await with_db_retry(lambda: self._contact_hits({(t.to_address, t.from_address) for t in probe}), what="contact_hits")
            for t in probe:
                if (t.to_address, t.from_address) not in hits:
                    continue
                self.stats["contact_hits"] += 1
                cands = await self._history_candidates(t.from_address, t.to_address, t.block_timestamp_ms)
                if cands and self.detector.engine.matches(t.to_address, cands):
                    lookalike.append(t)
                    plain.remove(t)
                    extra[t.transfer_key] = cands

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
                        lambda i=tx_id, x=t: self.detector.analyze(
                            i, from_address=x.from_address, to_address=x.to_address, extra_candidates=extra.get(x.transfer_key)
                        ),
                        what="analyze",
                    )
                except Exception:  # noqa: BLE001 - stays PENDING; the recovery worker retries it
                    log.exception("NETWORK_ANALYSIS_FAILED", tx=t.tx_hash)
        self.stats["events"] += len(events)
        await with_db_retry(lambda: self._upsert_and_advance(plain, blk, contacts), what="network_upsert")
        return events

    async def _contact_hits(self, pairs: set[tuple[str, str]]) -> set[tuple[str, str]]:
        out: set[tuple[str, str]] = set()
        NC = NetworkContact
        lst = list(pairs)
        async with self.sf() as s:
            for i in range(0, len(lst), 400):
                q = select(NC.toucher, NC.touched).where(tuple_(NC.toucher, NC.touched).in_(lst[i : i + 400]))
                out.update((a, b) for a, b in (await s.execute(q)).all())
        return out

    async def _history_candidates(self, victim: str, fake: str, before_ms: int) -> list[Candidate]:
        """The victim's real payment history before ``before_ms`` (TronGrid), as similarity candidates."""
        now = self.clock.now().timestamp()
        cached = self._history_cache.get(victim)
        if cached and now - cached[0] < 600:
            return [c for c in cached[1] if c.address != fake]
        self.stats["history_lookups"] += 1
        stats: dict[str, RecipientStats] = {}
        fp = None
        try:
            for _ in range(self.s.network_history_lookup_pages):
                page, fp = await self.source.get_trc20_transfers(
                    victim, self.token.contract, max_timestamp_ms=before_ms - 1, order="desc", fingerprint=fp, priority=PRIORITY_DETECTION
                )
                for h in page:
                    if h.from_address != victim or h.amount <= 0 or h.to_address == victim:
                        continue
                    ts = from_ms(h.block_timestamp_ms)
                    st = stats.get(h.to_address)
                    if st is None:
                        stats[h.to_address] = RecipientStats(1, h.amount, ts, ts, h.amount, h.amount, h.amount)
                    else:
                        st.transaction_count += 1
                        st.total_amount += h.amount
                        st.first_seen, st.last_seen = min(st.first_seen, ts), max(st.last_seen, ts)
                        st.largest_amount, st.smallest_amount = max(st.largest_amount, h.amount), min(st.smallest_amount, h.amount)
                        st.average_amount = st.total_amount // st.transaction_count
                if not fp or not page:
                    break
        except Exception as exc:  # noqa: BLE001 - fall back to the in-memory history
            log.warning("HISTORY_LOOKUP_FAILED", victim=victim, error=str(exc)[:120])
            return []
        cands = [Candidate(a, st) for a, st in stats.items()]
        if len(self._history_cache) > 2000:
            self._history_cache.clear()
        self._history_cache[victim] = (now, cands)
        return [c for c in cands if c.address != fake]

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

    async def _upsert_and_advance(self, payments: list[TokenTransfer], blk: BlockData, contacts: list[Contact] = ()) -> None:
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
            await self._upsert_contacts(s, contacts, pg)
            await repo.set_state(s, STATE_BLOCK_CURSOR, str(blk.number), now)
            await repo.set_state(s, STATE_BLOCK_CURSOR_TS, str(blk.timestamp_ms), now)
        self.stats["pairs_upserted"] += len(agg)

    async def _upsert_contacts(self, s, contacts, pg: bool) -> None:
        if not contacts:
            return
        latest: dict[tuple[str, str], Contact] = {}
        for c in contacts:
            if c.toucher != c.touched:
                latest[(c.toucher, c.touched)] = c
        rows = [
            dict(toucher=c.toucher, touched=c.touched, kind=c.kind, token_contract=c.token_contract, amount=c.amount,
                 tx_hash=c.tx_hash, first_seen=from_ms(c.timestamp_ms), last_seen=from_ms(c.timestamp_ms), count=1)
            for c in latest.values()
        ]  # fmt: skip
        NC = NetworkContact
        for i in range(0, len(rows), 500):
            stmt = repo._insert(s, NC).values(rows[i : i + 500])
            ex = stmt.excluded
            stmt = stmt.on_conflict_do_update(
                index_elements=["toucher", "touched"],
                set_={
                    "last_seen": ex.last_seen,
                    "count": NC.count + 1,
                    "kind": ex.kind,
                    "tx_hash": ex.tx_hash,
                    "amount": ex.amount,
                    "token_contract": ex.token_contract,
                },
            )
            await s.execute(stmt)

    async def contacts_count(self) -> int:
        async with self.sf() as s:
            return (await s.execute(select(func.count()).select_from(NetworkContact))).scalar_one()

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
        async with self.sf() as s, s.begin():
            removed += (await s.execute(delete(NetworkContact).where(NetworkContact.last_seen < cutoff))).rowcount or 0
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
