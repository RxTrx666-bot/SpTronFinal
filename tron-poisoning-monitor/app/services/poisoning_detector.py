"""Poisoning detection.

Primary event: a monitored wallet SENDS funds to a recipient it has never (or
rarely) used, and that recipient strongly resembles one of the wallet's
established historical recipients.  This is detected from the victim's own
payment - no dust / poisoning transfer has to be observed first.

Event classes (``domain.EventType``):

* ``POISONING_ATTEMPT``          - look-alike activity, victim did NOT pay
                                   (incoming dust, zero-value transferFrom)
* ``POISONING_CANDIDATE``        - victim paid a look-alike; evidence below
                                   ``CONFIDENCE_THRESHOLD`` (investigated further)
* ``SUCCESSFUL_POISONING_EVENT`` - victim paid a look-alike and the combined
                                   evidence crosses the threshold -> immediate alert

Exactly-once processing: a transaction row is "claimed" with a conditional
UPDATE (``analysis_status = 'PENDING'``) inside the same DB transaction that
writes the incident, the outbox alert, the follow-up jobs and the recipient
aggregates.  A crash rolls everything back and the recovery worker retries;
a replay finds the row already DONE and does nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository as repo
from app.domain import AnalysisStatus, EventType, HistoryStatus, Tri, WalletStatus
from app.models import (
    AddressLabelCache,
    AddressSimilarityMatch,
    PoisoningEvent,
    PoisoningEvidence,
    Transaction,
    WatchedWallet,
)
from app.services.notifier import Notifier
from app.services.risk_engine import RecipientStats, RiskAssessment, RiskConfig, RiskContext, RiskEngine
from app.services.similarity import SimilarityConfig, SimilarityEngine, SimilarityResult, candidate_keys
from app.utils.amounts import format_amount
from app.utils.clock import Clock, as_utc
from app.utils.logging import get_logger

log = get_logger(__name__)


# --------------------------------------------------------------------------- registry / locks
class WalletRegistry:
    """In-memory set of monitored wallets (address -> status) for O(1) filtering of every block."""

    def __init__(self) -> None:
        self.wallets: dict[str, str] = {}

    def is_monitored(self, address: str) -> bool:
        return address in self.wallets

    def is_active(self, address: str) -> bool:
        return self.wallets.get(address) == WalletStatus.ACTIVE.value

    async def refresh(self, session_factory) -> None:
        async with session_factory() as s:
            self.wallets = await repo.monitored_wallets(s)

    def set(self, address: str, status: str | None) -> None:
        if status is None or status == WalletStatus.REMOVED.value:
            self.wallets.pop(address, None)
        else:
            self.wallets[address] = status


class LockManager:
    """Per-wallet asyncio locks; acquired in sorted order to avoid deadlocks."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @contextlib.asynccontextmanager
    async def hold(self, addresses):
        keys = sorted(set(a for a in addresses if a))
        async with contextlib.AsyncExitStack() as stack:
            for k in keys:
                await stack.enter_async_context(self._locks[k])
            yield


# --------------------------------------------------------------------------- pure decision logic
@dataclass
class Candidate:
    address: str
    stats: RecipientStats
    flagged: bool = False


@dataclass
class LocalEvidence:
    dust: list[Transaction] = field(default_factory=list)
    history_complete: bool = False
    other_victims: list[str] = field(default_factory=list)
    label: str | None = None
    label_category: str | None = None
    label_source: str | None = None

    @property
    def dust_tri(self) -> Tri:
        if self.dust:
            return Tri.YES
        return Tri.NO if self.history_complete else Tri.UNKNOWN


@dataclass
class Decision:
    legit: Candidate
    similarity: SimilarityResult
    assessment: RiskAssessment
    context: RiskContext
    matches: list[tuple[Candidate, SimilarityResult]]

    @property
    def event_type(self) -> EventType | None:
        return self.assessment.event_type


class DecisionEngine:
    def __init__(self, sim: SimilarityEngine, risk: RiskEngine) -> None:
        self.sim = sim
        self.risk = risk

    def matches(self, address: str, candidates: list[Candidate]) -> list[tuple[Candidate, SimilarityResult]]:
        out = []
        for c in candidates:
            if c.address == address or c.flagged or c.stats.transaction_count == 0:
                continue
            r = self.sim.compare(c.address, address)
            if r.is_match:
                out.append((c, r))
        return out

    def evaluate_payment(
        self,
        *,
        recipient: str,
        amount: int,
        decimals: int,
        ts: datetime,
        initiator_is_victim: bool | None,
        symbol: str = "USDT",
        prior: RecipientStats,
        prior_flagged: bool,
        candidates: list[Candidate],
        local: LocalEvidence,
        subsequent_payments: int | None = None,
    ) -> Decision | None:
        matches = self.matches(recipient, candidates)
        if not matches:
            return None
        best: Decision | None = None
        for c, r in matches:
            ctx = RiskContext(
                similarity=r,
                legit=c.stats,
                suspicious_prior=prior,
                amount=amount,
                decimals=decimals,
                symbol=symbol,
                tx_time=ts,
                initiator_is_victim=initiator_is_victim,
                suspicious_flagged_before=prior_flagged,
                prior_dust_to_victim=local.dust_tri,
                other_victims_paid=len(local.other_victims),
                label_category=local.label_category,
                subsequent_payments=subsequent_payments,
            )
            a = self.risk.assess(ctx)
            d = Decision(legit=c, similarity=r, assessment=a, context=ctx, matches=matches)
            if best is None or (a.score, r.similarity_score) > (best.assessment.score, best.similarity.similarity_score):
                best = d
        return best


def attempt_confidence(r: SimilarityResult) -> int:
    base = r.similarity_pct
    return base if r.edge_rule == "both_edges" else base // 2


# --------------------------------------------------------------------------- detector
class PoisoningDetector:
    def __init__(self, settings, session_factory, clock: Clock, registry: WalletRegistry, notifier: Notifier, locks: LockManager) -> None:
        self.s = settings
        self.sf = session_factory
        self.clock = clock
        self.registry = registry
        self.notifier = notifier
        self.locks = locks
        self.engine = DecisionEngine(SimilarityEngine(SimilarityConfig.from_settings(settings)), RiskEngine(RiskConfig.from_settings(settings)))
        self.jobs_wakeup = asyncio.Event()
        self.dust_max = settings.units("dust_max_amount_usdt", settings.primary_token.decimals)
        self.network_min_alert = settings.units("network_min_alert_usdt", settings.primary_token.decimals)
        self.significant = settings.units("significant_amount_usdt", settings.primary_token.decimals)
        self.stats = {"analysed": 0, "events": 0, "successful": 0, "candidates": 0, "attempts": 0}

    # ----------------------------------------------------------------- entry point
    async def analyze(self, tx_id: int, *, from_address: str | None = None, to_address: str | None = None) -> list[int]:
        """Analyse one stored transfer exactly once.  Returns ids of created incidents."""
        if from_address is None or to_address is None:
            async with self.sf() as s:
                row = (await s.execute(select(Transaction.from_address, Transaction.to_address).where(Transaction.id == tx_id))).first()
            if row is None:
                return []
            from_address, to_address = row
        async with self.locks.hold([a for a in (from_address, to_address) if self.registry.is_monitored(a)]):
            created, alerts = await self._analyze_locked(tx_id)
        if alerts:
            self.notifier.wake()
        if created:
            self.jobs_wakeup.set()
        return created

    async def _analyze_locked(self, tx_id: int) -> tuple[list[int], bool]:
        started = self.clock.now()
        created: list[int] = []
        alerts = False
        async with self.sf() as s, s.begin():
            claim = await s.execute(
                update(Transaction)
                .where(Transaction.id == tx_id, Transaction.analysis_status == AnalysisStatus.PENDING.value)
                .values(analysis_attempts=Transaction.analysis_attempts + 1)
            )
            if not claim.rowcount:
                return [], False
            tx = await s.get(Transaction, tx_id)
            token = self.s.tokens_by_contract.get(tx.token_contract)
            if token is not None and tx.from_address != tx.to_address:
                if self.registry.is_monitored(tx.from_address) or tx.source == "NETWORK":
                    c, a = await self._outgoing(s, tx, token, started)
                    created += c
                    alerts |= a
                if self.registry.is_monitored(tx.to_address):
                    c, a = await self._incoming(s, tx, token, started)
                    created += c
                    alerts |= a
            tx.analysis_status = AnalysisStatus.DONE.value
            tx.analysis_error = None
        self.stats["analysed"] += 1
        return created, alerts

    # ----------------------------------------------------------------- helpers
    async def _candidates(self, s: AsyncSession, victim: str, token: str, address: str) -> list[Candidate]:
        rows = await repo.similarity_candidates(s, victim, token, address, self.s.candidate_key_length, self.s.max_candidate_recipients)
        return [Candidate(r.recipient_wallet, repo.stats_from_row(r), r.flagged_suspicious) for r in rows]

    async def local_evidence(self, s: AsyncSession, wallet: WatchedWallet | None, victim: str, suspicious: str, token: str, ts: datetime) -> LocalEvidence:
        dust = await repo.local_dust_evidence(s, victim, suspicious, token, ts, self.dust_max)
        others = set(await repo.other_victims_paying(s, suspicious, victim, token))
        for ev in await repo.events_for_suspicious(s, suspicious):
            if ev.victim_wallet != victim and ev.event_type != EventType.POISONING_ATTEMPT.value:
                others.add(ev.victim_wallet)
        label = await s.get(AddressLabelCache, suspicious)
        complete = bool(wallet and wallet.history_status == HistoryStatus.COMPLETE.value and not wallet.history_truncated)
        return LocalEvidence(
            dust=dust,
            history_complete=complete,
            other_victims=sorted(others),
            label=label.label if label else None,
            label_category=label.category if label else None,
            label_source=label.source if label else None,
        )

    async def _outgoing(self, s: AsyncSession, tx: Transaction, token, started: datetime, *, historical: bool = False) -> tuple[list[int], bool]:
        victim, recipient = tx.from_address, tx.to_address
        ts = as_utc(tx.block_timestamp)
        wallet = await repo.get_wallet(s, victim)
        prior_row = await repo.get_recipient(s, victim, recipient, token.contract)
        prior = repo.stats_from_row(prior_row)
        prior_flagged = bool(prior_row and prior_row.flagged_suspicious)
        created: list[int] = []
        alerts = False

        watched = bool(wallet and wallet.status != WalletStatus.REMOVED.value)
        if tx.amount == 0:
            if not watched:
                return created, alerts  # network-wide: zero-value spoofs are kept only as evidence rows
            # Zero-value transfer "from" the victim: almost always a transferFrom(victim, lookalike, 0)
            # spoof created by a third party. Not a payment -> never aggregated, at most an ATTEMPT.
            if prior.transaction_count == 0:
                c, a = await self._attempt(
                    s, tx, token, victim=victim, suspicious=recipient, wallet=wallet, started=started, historical=historical, kind="zero_value"
                )
                created += c
                alerts |= a
            return created, alerts

        if prior.transaction_count < 3:  # established counterparties (>= 3 payments) are not re-analysed
            cands = await self._candidates(s, victim, token.contract, recipient)
            if cands:
                local = await self.local_evidence(s, wallet, victim, recipient, token.contract, ts)
                initiator_is_victim = None if tx.initiator_address is None else tx.initiator_address == victim
                decision = self.engine.evaluate_payment(
                    recipient=recipient,
                    amount=tx.amount,
                    decimals=token.decimals,
                    symbol=token.symbol,
                    ts=ts,
                    initiator_is_victim=initiator_is_victim,
                    prior=prior,
                    prior_flagged=prior_flagged,
                    candidates=cands,
                    local=local,
                )
                if decision is not None:
                    await self._store_matches(s, tx, victim, decision.matches)
                    if decision.event_type is not None:
                        eid, a = await self._create_event(s, tx, token, wallet, decision, local, started, historical=historical)
                        if eid:
                            created.append(eid)
                            alerts |= a
                    else:
                        log.info("SIMILAR_RECIPIENT_BELOW_THRESHOLD", victim=victim, tx=tx.tx_hash, recipient=recipient,
                                 similarity=decision.similarity.similarity_pct, confidence=decision.assessment.score)  # fmt: skip

        await repo.apply_payment(s, victim, recipient, token.contract, tx.amount, ts, self.clock.now(), self.s.candidate_key_length)
        if created:
            await repo.flag_recipient(s, victim, recipient, token.contract)
        if wallet:
            wallet.last_activity_at = ts
        return created, alerts

    async def _incoming(self, s: AsyncSession, tx: Transaction, token, started: datetime, *, historical: bool = False) -> tuple[list[int], bool]:
        if tx.amount > self.dust_max:
            return [], False
        victim, sender = tx.to_address, tx.from_address
        prior_row = await repo.get_recipient(s, victim, sender, token.contract)
        if prior_row is not None and not prior_row.flagged_suspicious:
            return [], False  # the victim has paid this address before: an ordinary small incoming transfer
        wallet = await repo.get_wallet(s, victim)
        return await self._attempt(s, tx, token, victim=victim, suspicious=sender, wallet=wallet, started=started, historical=historical, kind="dust")

    async def _attempt(self, s, tx, token, *, victim, suspicious, wallet, started, historical, kind) -> tuple[list[int], bool]:
        cands = await self._candidates(s, victim, token.contract, suspicious)
        matches = self.engine.matches(suspicious, cands)
        if not matches:
            return [], False
        legit, sim = max(matches, key=lambda m: (m[1].similarity_score, m[0].stats.transaction_count))
        await self._store_matches(s, tx, victim, matches)
        if await self._existing_event(s, tx.id, victim):
            return [], False
        now = self.clock.now()
        if kind == "dust":
            desc = f"Look-alike address sent {format_amount(tx.amount, token.decimals)} {token.symbol} to the victim (dust / poisoning transfer)"
        else:
            desc = "Zero-value transfer recorded from the victim to the look-alike address (typically created by a third party via transferFrom)"
        conf = attempt_confidence(sim)
        ev = self._new_event(
            tx, token, victim=victim, legit=legit, sim=sim, event_type=EventType.POISONING_ATTEMPT, confidence=conf,
            breakdown=[{"key": "attempt_similarity", "points": conf, "description": f"Look-alike of a recipient used {legit.stats.transaction_count} time(s)", "kind": "ANALYSIS"}],
            suspicious=suspicious, prior_count=0, dust_tri=Tri.YES, wallet=wallet, started=started, historical=historical, now=now,
        )  # fmt: skip
        ev.investigation_status = "SKIPPED"
        s.add(ev)
        await s.flush()
        ev.case_id = repo.make_case_id(ev.id, now)
        self._add_evidence(
            s,
            ev.id,
            "attempt",
            "PRIOR_DUST",
            "FACT",
            True,
            desc,
            now,
            tx_hash=tx.tx_hash,
            address=suspicious,
            amount=tx.amount,
            observed_at=as_utc(tx.block_timestamp),
        )
        self._add_evidence(
            s, ev.id, "similarity", "SIMILARITY", "FACT", True,
            f"Address resembles legitimate recipient {legit.address}: {sim.describe()}", now, address=legit.address, data=sim.as_dict(),
        )  # fmt: skip
        self.stats["attempts"] += 1
        self.stats["events"] += 1
        log.info(
            "POISONING_ATTEMPT",
            victim=victim,
            tx=tx.tx_hash,
            suspicious=suspicious,
            legit=legit.address,
            similarity=sim.similarity_pct,
            kind=kind,
            case=ev.case_id,
        )
        alerts = False
        if self.s.notify_attempts and not historical and wallet and wallet.status == WalletStatus.ACTIVE.value:
            alerts = bool(await self.notifier.event_alert(s, ev.id, "ATTEMPT"))
        return [ev.id], alerts

    async def _existing_event(self, s: AsyncSession, tx_id: int, victim: str) -> bool:
        q = select(PoisoningEvent.id).where(PoisoningEvent.transaction_id == tx_id, PoisoningEvent.victim_wallet == victim)
        return (await s.execute(q)).first() is not None

    async def _store_matches(self, s: AsyncSession, tx: Transaction, victim: str, matches) -> None:
        now = self.clock.now()
        for c, r in matches:
            stmt = (
                repo._insert(s, AddressSimilarityMatch)
                .values(
                    transaction_id=tx.id,
                    victim_wallet=victim,
                    legitimate_recipient=c.address,
                    suspicious_recipient=r.candidate,
                    prefix_match_length=r.prefix_match_length,
                    suffix_match_length=r.suffix_match_length,
                    prefix_similarity=r.prefix_similarity,
                    suffix_similarity=r.suffix_similarity,
                    overall_similarity=r.overall_similarity,
                    positional_similarity=r.positional_similarity,
                    similarity_score=r.similarity_score,
                    metrics=r.as_dict(),
                    created_at=now,
                )
                .on_conflict_do_nothing(index_elements=["transaction_id", "victim_wallet", "legitimate_recipient"])
            )
            await s.execute(stmt)

    def _new_event(
        self, tx, token, *, victim, legit, sim, event_type, confidence, breakdown, suspicious, prior_count, dust_tri, wallet, started, historical, now
    ) -> PoisoningEvent:
        detected = as_utc(tx.detected_at)
        block_ts = as_utc(tx.block_timestamp)
        return PoisoningEvent(
            case_id=None,
            event_type=event_type.value,
            transaction_id=tx.id,
            tx_hash=tx.tx_hash,
            victim_wallet=victim,
            legitimate_recipient=legit.address,
            suspicious_recipient=suspicious,
            token_contract=token.contract,
            token_symbol=token.symbol,
            token_decimals=token.decimals,
            amount=tx.amount,
            block_number=tx.block_number,
            block_timestamp=block_ts,
            confirmation_status=tx.confirmation_status,
            similarity_score=sim.similarity_score,
            confidence=confidence,
            fast_confidence=confidence,
            score_breakdown=breakdown,
            legit_tx_count=legit.stats.transaction_count,
            legit_total_amount=legit.stats.total_amount,
            suspicious_prior_tx_count=prior_count,
            poisoning_tx_observed=dust_tri.value,
            initiator_address=tx.initiator_address,
            is_historical=historical,
            history_complete=bool(wallet and wallet.history_status == HistoryStatus.COMPLETE.value),
            investigation_status="PENDING",
            trace_status="PENDING",
            detected_at=detected,
            analysis_started_at=started,
            analysis_completed_at=now,
            detection_latency_ms=None if historical else max(0, int((now - detected).total_seconds() * 1000)),
            chain_latency_ms=None if historical else max(0, int((detected - block_ts).total_seconds() * 1000)),
            created_at=now,
            updated_at=now,
        )

    def _add_evidence(
        self, s, event_id, key, etype, kind, supports, description, now, *, tx_hash=None, address=None, amount=None, observed_at=None, data=None
    ) -> None:
        s.add(
            PoisoningEvidence(
                event_id=event_id,
                evidence_key=key[:160],
                evidence_type=etype,
                kind=kind,
                supports=supports,
                description=description,
                tx_hash=tx_hash,
                address=address,
                amount=amount,
                observed_at=observed_at,
                data=data or {},
                created_at=now,
            )
        )

    async def _create_event(self, s, tx, token, wallet, decision: Decision, local: LocalEvidence, started, *, historical: bool) -> tuple[int | None, bool]:
        victim = tx.from_address
        if await self._existing_event(s, tx.id, victim):
            return None, False
        now = self.clock.now()
        a = decision.assessment
        event_type = decision.event_type
        ev = self._new_event(
            tx, token, victim=victim, legit=decision.legit, sim=decision.similarity, event_type=event_type, confidence=a.score,
            breakdown=a.breakdown(), suspicious=tx.to_address, prior_count=decision.context.suspicious_prior.transaction_count,
            dust_tri=local.dust_tri, wallet=wallet, started=started, historical=historical, now=now,
        )  # fmt: skip
        if event_type != EventType.SUCCESSFUL_POISONING_EVENT:
            ev.trace_status = "SKIPPED"
        s.add(ev)
        await s.flush()
        ev.case_id = repo.make_case_id(ev.id, now)
        await self.write_signal_evidence(s, ev.id, a, now)
        sim = decision.similarity
        self._add_evidence(
            s, ev.id, "similarity", "SIMILARITY", "FACT", True,
            f"Suspicious recipient resembles legitimate recipient: {sim.describe()}", now, address=decision.legit.address, data=sim.as_dict(),
        )  # fmt: skip
        if a.capped_reason:
            self._add_evidence(s, ev.id, "capped", "THRESHOLD_CAP", "ANALYSIS", False, f"Not classified as successful: {a.capped_reason}", now)
        for d in local.dust:
            direction = "zero-value transfer victim → suspicious" if d.from_address == victim else "dust transfer suspicious → victim"
            self._add_evidence(
                s, ev.id, f"dust:{d.transfer_key}", "PRIOR_DUST", "FACT", True,
                f"Prior {direction} of {format_amount(d.amount, token.decimals)} {token.symbol}", now,
                tx_hash=d.tx_hash, address=tx.to_address, amount=d.amount, observed_at=as_utc(d.block_timestamp),
            )  # fmt: skip
        if local.other_victims:
            self._add_evidence(
                s, ev.id, "other_victims", "OTHER_VICTIMS", "FACT", True,
                f"{len(local.other_victims)} other wallet(s) were also observed sending funds to the suspicious address", now,
                data={"victims": local.other_victims},
            )  # fmt: skip
        if local.label:
            self._add_evidence(
                s, ev.id, "label", "LABEL", "ANALYSIS", False,
                f"Public label for suspicious address: {local.label} (possible exchange/service attribution, source: {local.label_source})", now,
            )  # fmt: skip

        alerts = False
        if not historical:
            success = event_type == EventType.SUCCESSFUL_POISONING_EVENT
            if wallet is not None and wallet.status != WalletStatus.REMOVED.value:
                active = wallet.status == WalletStatus.ACTIVE.value  # watched wallet (/add)
                investigate = True
            else:  # network-wide detection on a wallet nobody added
                active = self.s.network_wide and tx.amount >= self.network_min_alert
                investigate = success or tx.amount >= self.significant
            if investigate:
                await repo.enqueue_job(s, "INVESTIGATE", str(ev.id), now)
            else:
                ev.investigation_status = "SKIPPED"
            if success and self.s.trace_enabled:
                await repo.enqueue_job(s, "TRACE", f"{ev.id}:1", now, {"event_id": ev.id, "run": 1})
            if active and (success or self.s.notify_candidates):
                alerts = bool(await self.notifier.event_alert(s, ev.id, Notifier.kind_for(event_type.value)))
        else:
            ev.investigation_status = "SKIPPED"
            ev.trace_status = "SKIPPED"

        self.stats["events"] += 1
        self.stats["successful" if event_type == EventType.SUCCESSFUL_POISONING_EVENT else "candidates"] += 1
        log.warning(
            "SUCCESSFUL_POISONING_EVENT" if event_type == EventType.SUCCESSFUL_POISONING_EVENT else "POISONING_CANDIDATE",
            case=ev.case_id, victim=victim, tx=tx.tx_hash, suspicious=tx.to_address, legit=decision.legit.address,
            amount=format_amount(tx.amount, token.decimals), similarity=sim.similarity_pct, confidence=a.score,
            historical=historical, latency_ms=ev.detection_latency_ms,
        )  # fmt: skip
        return ev.id, alerts

    async def write_signal_evidence(self, s, event_id: int, a: RiskAssessment, now: datetime) -> None:
        existing = {
            k
            for (k,) in (
                await s.execute(
                    select(PoisoningEvidence.evidence_key).where(PoisoningEvidence.event_id == event_id, PoisoningEvidence.evidence_key.like("signal:%"))
                )
            ).all()
        }
        for sig in a.signals:
            key = f"signal:{sig.key}"
            if key in existing:
                continue
            self._add_evidence(s, event_id, key, "SIGNAL", sig.kind, sig.points > 0, sig.description, now, data={"points": sig.points})

    # ----------------------------------------------------------------- retrospective (history)
    async def retrospective(self, wallet_address: str) -> dict:
        """Chronologically re-play a freshly scanned history to find past poisoning events.

        Uses an in-memory running aggregate, so "was this recipient new at the
        time?" is answered correctly even though history pages were ingested
        out of order relative to live monitoring.
        """
        token = self.s.primary_token
        summary = {"outgoing": 0, "successful": [], "candidates": [], "attempts": []}
        async with self.locks.hold([wallet_address]):
            async with self.sf() as s, s.begin():
                wallet = await repo.get_wallet(s, wallet_address)
                if wallet is None:
                    return summary
                live_cutoff = as_utc(wallet.added_at) - timedelta(minutes=10)
                completed = as_utc(wallet.history_completed_at) if wallet.history_completed_at else None

                def needs_review(t: Transaction) -> bool:
                    # History rows, and live rows analysed before the history scan finished
                    # (their first analysis only saw a partial recipient history).
                    return t.source == "HISTORY" or completed is None or as_utc(t.created_at) <= completed

                rows = (
                    await s.execute(
                        select(Transaction)
                        .where(
                            Transaction.token_contract == token.contract,
                            or_(
                                Transaction.from_address == wallet_address,
                                (Transaction.to_address == wallet_address) & (Transaction.amount <= self.dust_max),
                            ),
                        )
                        .order_by(Transaction.block_timestamp, Transaction.id)
                    )
                ).scalars()
                rows = list(rows)
                total_payments: dict[str, int] = defaultdict(int)  # hindsight: all payments per recipient
                for t in rows:
                    if t.from_address == wallet_address and t.to_address != wallet_address and t.amount > 0:
                        total_payments[t.to_address] += 1
                running: dict[str, RecipientStats] = {}
                flagged: set[str] = set()
                index: dict[str, set[str]] = defaultdict(set)

                def cands_for(addr: str) -> list[Candidate]:
                    pk, sk = candidate_keys(addr, self.s.candidate_key_length)
                    names = (index.get("p" + pk, set()) | index.get("s" + sk, set())) - {addr}
                    return [Candidate(n, replace(running[n]), n in flagged) for n in names]

                dust_seen: list[Transaction] = []
                for tx in rows:
                    ts = as_utc(tx.block_timestamp)
                    historical = ts < live_cutoff
                    if tx.to_address == wallet_address and tx.from_address != wallet_address:
                        dust_seen.append(tx)
                        if needs_review(tx) and tx.from_address not in running:
                            matches = self.engine.matches(tx.from_address, cands_for(tx.from_address))
                            if matches and not await self._existing_event(s, tx.id, wallet_address):
                                c, _ = await self._attempt(
                                    s,
                                    tx,
                                    token,
                                    victim=wallet_address,
                                    suspicious=tx.from_address,
                                    wallet=wallet,
                                    started=self.clock.now(),
                                    historical=historical,
                                    kind="dust",
                                )
                                summary["attempts"] += c
                        continue
                    if tx.to_address == wallet_address:
                        continue
                    summary["outgoing"] += 1
                    recipient = tx.to_address
                    if tx.amount == 0:
                        dust_seen.append(tx)
                        continue
                    prior = running.get(recipient, RecipientStats())
                    if needs_review(tx) and prior.transaction_count < 3:
                        cands = cands_for(recipient)
                        if cands:
                            dust = [
                                d for d in dust_seen
                                if (d.from_address == recipient and d.to_address == wallet_address) or (d.to_address == recipient and d.amount == 0)
                            ]  # fmt: skip
                            local = LocalEvidence(dust=dust, history_complete=True)
                            if any(self.engine.matches(recipient, cands)):
                                others = await repo.other_victims_paying(s, recipient, wallet_address, token.contract)
                                local.other_victims = others
                            decision = self.engine.evaluate_payment(
                                recipient=recipient, amount=tx.amount, decimals=token.decimals, symbol=token.symbol, ts=ts,
                                initiator_is_victim=None if tx.initiator_address is None else tx.initiator_address == wallet_address,
                                prior=replace(prior), prior_flagged=recipient in flagged, candidates=cands, local=local,
                                subsequent_payments=total_payments[recipient] - prior.transaction_count - 1,
                            )  # fmt: skip
                            if decision is not None:
                                await self._store_matches(s, tx, wallet_address, decision.matches)
                                if decision.event_type is not None:
                                    eid, _ = await self._create_event(s, tx, token, wallet, decision, local, self.clock.now(), historical=historical)
                                    if eid:
                                        flagged.add(recipient)
                                        await repo.flag_recipient(s, wallet_address, recipient, token.contract)
                                        key = "successful" if decision.event_type == EventType.SUCCESSFUL_POISONING_EVENT else "candidates"
                                        summary[key].append(eid)
                    # running aggregate update
                    st = running.get(recipient)
                    if st is None:
                        st = RecipientStats(0, 0, ts, ts, tx.amount, tx.amount, 0)
                        running[recipient] = st
                        pk, sk = candidate_keys(recipient, self.s.candidate_key_length)
                        index["p" + pk].add(recipient)
                        index["s" + sk].add(recipient)
                    st.transaction_count += 1
                    st.total_amount += tx.amount
                    st.first_seen = min(st.first_seen, ts)
                    st.last_seen = max(st.last_seen, ts)
                    st.largest_amount = max(st.largest_amount, tx.amount)
                    st.smallest_amount = min(st.smallest_amount, tx.amount)
                    st.average_amount = st.total_amount // st.transaction_count
        if summary["successful"] or summary["candidates"]:
            self.jobs_wakeup.set()
            self.notifier.wake()
        return summary
