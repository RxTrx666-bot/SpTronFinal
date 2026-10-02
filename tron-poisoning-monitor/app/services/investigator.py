"""Optional poisoning-evidence discovery for a suspicious recipient.

Runs *after* the incident has been created (and, for high-confidence events,
after the alert has been queued), so API latency never delays the primary
alert.  It looks at the suspicious address's own token history and records:

* dust / zero-value transfers between the suspicious address and the victim
* dust sent to many different wallets (multi-victim poisoning campaign)
* dust received from other addresses
* number of different wallets that funded it
* how much activity it had before the victim's payment (fresh address?)
* forwarding of the victim's funds shortly after receipt
* account creation time and public label

Each finding is stored as evidence (FACT = directly observed on-chain,
ANALYSIS = inference) and the confidence is re-scored.  A candidate can be
upgraded to a SUCCESSFUL_POISONING_EVENT, which then triggers the alert.
"""

from __future__ import annotations

from sqlalchemy import func, select

from app import repository as repo
from app.domain import EventType, TokenTransfer, Tri
from app.models import PoisoningEvent, PoisoningEvidence, Transaction
from app.services.notifier import Notifier
from app.services.poisoning_detector import PoisoningDetector
from app.services.risk_engine import RecipientStats, RiskContext
from app.utils.amounts import format_amount, ratio_pct
from app.utils.clock import Clock, as_utc, from_ms, to_ms
from app.utils.logging import get_logger
from app.utils.ratelimit import PRIORITY_INVESTIGATION

log = get_logger(__name__)


class Investigator:
    def __init__(self, settings, session_factory, source, clock: Clock, detector: PoisoningDetector, notifier: Notifier, labels) -> None:
        self.s = settings
        self.sf = session_factory
        self.source = source
        self.clock = clock
        self.detector = detector
        self.notifier = notifier
        self.labels = labels

    async def _history(self, address: str, contract: str, **kw) -> tuple[list[TokenTransfer], bool]:
        out: list[TokenTransfer] = []
        fp = None
        while True:
            page, fp = await self.source.get_trc20_transfers(address, contract, fingerprint=fp, priority=PRIORITY_INVESTIGATION, **kw)
            out.extend(page)
            if not fp or not page:
                return out, False
            if len(out) >= self.s.investigation_max_transfers:
                return out, True

    async def investigate(self, event_id: int) -> None:
        async with self.sf() as s:
            ev = await s.get(PoisoningEvent, event_id)
        if ev is None or ev.event_type == EventType.POISONING_ATTEMPT.value:
            return
        token = self.s.tokens_by_contract[ev.token_contract]
        dust_max = self.detector.dust_max
        victim, sus = ev.victim_wallet, ev.suspicious_recipient
        ts_ms = to_ms(as_utc(ev.block_timestamp))

        before, truncated = await self._history(sus, token.contract, max_timestamp_ms=ts_ms, order="asc")
        before = [t for t in before if t.tx_hash != ev.tx_hash]
        window_end = ts_ms + self.s.forward_window_minutes * 60_000
        after, _ = await self._history(sus, token.contract, min_timestamp_ms=ts_ms, max_timestamp_ms=window_end, order="asc")
        account = None
        try:
            account = await self.source.get_account(sus, priority=PRIORITY_INVESTIGATION)
        except Exception as exc:  # noqa: BLE001 - optional evidence
            log.warning("ACCOUNT_LOOKUP_FAILED", address=sus, error=type(exc).__name__)
        label = await self.labels.get(sus)

        facts: list[tuple[str, str, str, bool, str, dict, TokenTransfer | None]] = []
        dust_out = [t for t in before if t.from_address == sus and t.amount <= dust_max]
        to_victim = [t for t in dust_out if t.to_address == victim]
        zero_from_victim = [t for t in before if t.from_address == victim and t.to_address == sus and t.amount == 0]
        for t in to_victim + zero_from_victim:
            d = "zero-value transfer victim → suspicious" if t.amount == 0 and t.from_address == victim else "dust transfer suspicious → victim"
            facts.append((f"dust:{t.transfer_key}", "PRIOR_DUST", "FACT", True,
                          f"Prior {d} of {format_amount(t.amount, token.decimals)} {token.symbol} at {from_ms(t.block_timestamp_ms):%Y-%m-%d %H:%M UTC}", {}, t))  # fmt: skip
        dust_recipients = {t.to_address for t in dust_out}
        if dust_recipients:
            facts.append(("multi_victim", "MULTI_VICTIM_DUST", "FACT", len(dust_recipients) >= self.s.multi_victim_min,
                          f"Suspicious address sent dust (≤ {format_amount(dust_max, token.decimals)} {token.symbol}) to {len(dust_recipients)} different wallet(s) before the victim's payment",
                          {"count": len(dust_recipients), "sample": sorted(dust_recipients)[:20]}, None))  # fmt: skip
        dust_in = [t for t in before if t.to_address == sus and t.amount <= dust_max and t.from_address != victim]
        if dust_in:
            facts.append(("dust_funding", "DUST_FUNDING", "FACT", True,
                          f"Suspicious address received {len(dust_in)} dust transfer(s) from {len({t.from_address for t in dust_in})} other address(es)",
                          {"count": len(dust_in)}, None))  # fmt: skip
        senders = {t.from_address for t in before if t.to_address == sus and t.amount > dust_max and t.from_address != victim}
        if senders:
            facts.append(("many_senders", "MANY_SENDERS", "FACT", len(senders) >= self.s.many_senders_min,
                          f"Suspicious address received funds from {len(senders)} different wallet(s) before the victim's payment",
                          {"count": len(senders), "sample": sorted(senders)[:20]}, None))  # fmt: skip
        prior_activity = None if truncated else len(before)
        facts.append(("activity", "SUSPICIOUS_ACTIVITY", "FACT", (prior_activity or 0) <= self.s.fresh_address_max_prior_transfers,
                      f"Suspicious address had {'more than ' + str(len(before)) if truncated else len(before)} {token.symbol} transfer(s) before the victim's payment",
                      {"prior_transfers": prior_activity, "truncated": truncated}, None))  # fmt: skip
        if account and account.exists and account.create_time_ms:
            age_h = (ts_ms - account.create_time_ms) / 3_600_000
            facts.append(("account_age", "ACCOUNT_AGE", "FACT", age_h < 24 * 30,
                          f"Suspicious account was activated {from_ms(account.create_time_ms):%Y-%m-%d %H:%M UTC} ({age_h / 24:.1f} days before the payment)",
                          {"create_time_ms": account.create_time_ms}, None))  # fmt: skip
        outflows = [t for t in after if t.from_address == sus and t.block_timestamp_ms >= ts_ms]
        fwd_pct = None
        fwd_summary = None
        if outflows:
            total = sum(t.amount for t in outflows)
            fwd_pct = min(100, ratio_pct(total, ev.amount))
            first = min(t.block_timestamp_ms for t in outflows)
            fwd_summary = (
                f"{format_amount(total, token.decimals)} {token.symbol} ({fwd_pct}% of the received amount) forwarded in "
                f"{len(outflows)} transfer(s) within {self.s.forward_window_minutes} min; first after {(first - ts_ms) // 1000} s"
            )
            facts.append(("forwarding", "FORWARDING", "FACT", fwd_pct >= self.s.forward_min_ratio_pct, fwd_summary, {"pct": fwd_pct}, outflows[0]))
        if label:
            facts.append(("label", "LABEL", "ANALYSIS", False,
                          f"Public label for suspicious address: {label[0]} (possible exchange/service attribution, source: {label[2]})",
                          {"category": label[1]}, None))  # fmt: skip

        dust_tri = Tri.YES if (to_victim or zero_from_victim) else (Tri.NO if not truncated else None)
        await self.apply_findings(event_id, facts, dust_tri=dust_tri, forwarding_summary=fwd_summary, source="investigation")

    async def apply_findings(self, event_id: int, facts, *, dust_tri: Tri | None, forwarding_summary: str | None, source: str) -> None:
        """Store evidence, re-score and send upgrade/update alerts (single DB transaction)."""
        alerts = False
        async with self.sf() as s, s.begin():
            ev = await s.get(PoisoningEvent, event_id)
            if ev is None:
                return
            now = self.clock.now()
            existing = {k for (k,) in (await s.execute(select(PoisoningEvidence.evidence_key).where(PoisoningEvidence.event_id == event_id))).all()}
            new_supporting = []
            for key, etype, kind, supports, desc, data, t in facts:
                if key in existing:
                    if etype in ("FORWARDING", "MULTI_VICTIM_DUST", "MANY_SENDERS", "SUSPICIOUS_ACTIVITY", "OTHER_VICTIMS"):
                        row = (
                            await s.execute(select(PoisoningEvidence).where(PoisoningEvidence.event_id == event_id, PoisoningEvidence.evidence_key == key))
                        ).scalar_one()
                        if row.data != data:
                            row.description, row.data, row.supports = desc, data, supports
                            if supports and etype in ("FORWARDING", "OTHER_VICTIMS"):
                                new_supporting.append(desc)
                    continue
                s.add(PoisoningEvidence(
                    event_id=event_id, evidence_key=key, evidence_type=etype, kind=kind, supports=supports, description=desc,
                    tx_hash=t.tx_hash if t else None, address=(t.to_address if etype == "FORWARDING" else t.from_address) if t else None,
                    amount=t.amount if t else None, observed_at=from_ms(t.block_timestamp_ms) if t else None, data=data, created_at=now,
                ))  # fmt: skip
                if supports and etype in ("PRIOR_DUST", "MULTI_VICTIM_DUST", "OTHER_VICTIMS", "FORWARDING", "MANY_SENDERS"):
                    new_supporting.append(desc)
            await s.flush()
            if dust_tri is not None and not (ev.poisoning_tx_observed == Tri.YES.value and dust_tri != Tri.YES):
                ev.poisoning_tx_observed = dust_tri.value
            if forwarding_summary:
                ev.forwarding_summary = forwarding_summary
            old_type, old_score = ev.event_type, ev.confidence
            assessment = await self.rescore(s, ev)
            ev.confidence = assessment.score
            ev.score_breakdown = assessment.breakdown()
            if assessment.event_type is not None:
                ev.event_type = assessment.event_type.value
            await self.detector.write_signal_evidence(s, ev.id, assessment, now)
            if source == "investigation":
                ev.investigation_status = "DONE"
            ev.updated_at = now
            upgraded = old_type != EventType.SUCCESSFUL_POISONING_EVENT.value and ev.event_type == EventType.SUCCESSFUL_POISONING_EVENT.value
            wallet = await repo.get_wallet(s, ev.victim_wallet)
            active = self.detector.alerts_allowed(wallet, ev.amount) and not ev.is_historical
            if upgraded:
                log.warning("CANDIDATE_UPGRADED", case=ev.case_id, victim=ev.victim_wallet, tx=ev.tx_hash, confidence=ev.confidence, previous=old_score)
                if active:
                    alerts |= bool(await self.notifier.event_alert(s, ev.id, "UPGRADE"))
                if self.s.trace_enabled and ev.trace_status in ("SKIPPED", "PENDING"):
                    ev.trace_status = "PENDING"
                    await repo.enqueue_job(s, "TRACE", f"{ev.id}:1", now, {"event_id": ev.id, "run": 1})
            elif ev.event_type == EventType.SUCCESSFUL_POISONING_EVENT.value or old_type == EventType.SUCCESSFUL_POISONING_EVENT.value:
                if active and (new_supporting or abs(ev.confidence - old_score) >= 5):
                    alerts |= bool(await self.notifier.event_alert(s, ev.id, "UPDATE"))
            log.info("INVESTIGATION_COMPLETE" if source == "investigation" else "EVIDENCE_UPDATED", case=ev.case_id, confidence=ev.confidence,
                     previous=old_score, event_type=ev.event_type, dust=ev.poisoning_tx_observed, new_facts=len(new_supporting))  # fmt: skip
        if alerts:
            self.notifier.wake()

    async def rescore(self, s, ev: PoisoningEvent):
        """Rebuild the risk context from stored facts and re-assess."""
        legit_row = await repo.get_recipient(s, ev.victim_wallet, ev.legitimate_recipient, ev.token_contract)
        legit = repo.stats_from_row(legit_row)
        if legit.transaction_count < ev.legit_tx_count:  # never weaker than at detection time
            legit.transaction_count, legit.total_amount = ev.legit_tx_count, ev.legit_total_amount
        prior_total = (
            await s.execute(
                select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                    Transaction.from_address == ev.victim_wallet,
                    Transaction.to_address == ev.suspicious_recipient,
                    Transaction.token_contract == ev.token_contract,
                    Transaction.block_timestamp < ev.block_timestamp,
                )
            )
        ).scalar_one()
        prior = RecipientStats(transaction_count=ev.suspicious_prior_tx_count, total_amount=int(prior_total or 0))
        evidence = list((await s.execute(select(PoisoningEvidence).where(PoisoningEvidence.event_id == ev.id))).scalars())

        def data(etype: str, key: str):
            for e in evidence:
                if e.evidence_type == etype:
                    return (e.data or {}).get(key)
            return None

        others = data("OTHER_VICTIMS", "victims") or []
        sim = self.detector.engine.sim.compare(ev.legitimate_recipient, ev.suspicious_recipient)
        ctx = RiskContext(
            similarity=sim,
            legit=legit,
            suspicious_prior=prior,
            amount=ev.amount,
            decimals=ev.token_decimals,
            symbol=ev.token_symbol,
            tx_time=as_utc(ev.block_timestamp),
            initiator_is_victim=None if ev.initiator_address is None else ev.initiator_address == ev.victim_wallet,
            suspicious_flagged_before=False,
            prior_dust_to_victim=Tri(ev.poisoning_tx_observed),
            dust_recipients_count=data("MULTI_VICTIM_DUST", "count"),
            other_victims_paid=len(others),
            suspicious_prior_activity=data("SUSPICIOUS_ACTIVITY", "prior_transfers"),
            distinct_senders=data("MANY_SENDERS", "count"),
            forwarded_pct=data("FORWARDING", "pct"),
            label_category=data("LABEL", "category"),
            dust_times=[as_utc(e.observed_at) for e in evidence if e.evidence_type == "PRIOR_DUST" and e.observed_at],
        )
        return self.detector.engine.risk.assess(ctx)

    async def refresh_other_victims(self, event_id: int) -> None:
        """Re-check whether other wallets paid the same suspicious address."""
        async with self.sf() as s:
            ev = await s.get(PoisoningEvent, event_id)
            if ev is None:
                return
            others = set(await repo.other_victims_paying(s, ev.suspicious_recipient, ev.victim_wallet, ev.token_contract))
        if others:
            await self.apply_findings(
                event_id,
                [("other_victims", "OTHER_VICTIMS", "FACT", True, f"{len(others)} other wallet(s) were also observed sending funds to the suspicious address", {"victims": sorted(others)}, None)],
                dust_tri=None, forwarding_summary=None, source="refresh",
            )  # fmt: skip
