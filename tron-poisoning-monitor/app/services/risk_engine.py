"""Risk / confidence engine.

Turns many independent signals into a 0-100 confidence score.  Similarity on
its own is never enough to reach the default threshold: a look-alike address
contributes at most ``similarity_both_edges + similarity_very_high`` (40)
points; the rest must come from the victim's payment history (a well-used
legitimate recipient, a brand-new recipient, a meaningful amount) and
optional on-chain evidence (dust / zero-value poisoning transfers,
multi-victim activity, fast forwarding).

Weights live in ``config.DEFAULT_RISK_WEIGHTS`` and can be overridden with
``RISK_WEIGHTS`` (JSON).  Every contribution is recorded in the breakdown so
an investigator can see exactly why a score was produced.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from app.domain import EventType, Tri
from app.services.similarity import SimilarityResult
from app.utils.amounts import format_amount


@dataclass
class RiskConfig:
    weights: dict[str, int]
    confidence_threshold: int = 80
    candidate_threshold: int = 50
    min_legit_tx_count: int = 2
    min_legit_total: int = 1_000_000_000
    legit_substantial_total: int = 10_000_000_000
    legit_recent_days: int = 180
    min_victim_amount: int = 1_000_000
    significant_amount: int = 1_000_000_000
    large_amount: int = 10_000_000_000
    multi_victim_min: int = 3
    many_senders_min: int = 5
    fresh_address_max_prior_transfers: int = 3
    forward_min_ratio_pct: int = 50
    rapid_dust_minutes: int = 120
    rapid_payment_hours: int = 48

    @classmethod
    def from_settings(cls, s) -> RiskConfig:
        d = s.primary_token.decimals
        return cls(
            weights=s.weights,
            confidence_threshold=s.confidence_threshold,
            candidate_threshold=s.candidate_threshold,
            min_legit_tx_count=s.min_legit_tx_count,
            min_legit_total=s.units("min_legit_total_usdt", d),
            legit_substantial_total=s.units("legit_substantial_total_usdt", d),
            legit_recent_days=s.legit_recent_days,
            min_victim_amount=s.units("min_victim_amount_usdt", d),
            significant_amount=s.units("significant_amount_usdt", d),
            large_amount=s.units("large_amount_usdt", d),
            multi_victim_min=s.multi_victim_min,
            many_senders_min=s.many_senders_min,
            fresh_address_max_prior_transfers=s.fresh_address_max_prior_transfers,
            forward_min_ratio_pct=s.forward_min_ratio_pct,
            rapid_dust_minutes=s.rapid_dust_minutes,
            rapid_payment_hours=s.rapid_payment_hours,
        )


@dataclass
class RecipientStats:
    transaction_count: int = 0
    total_amount: int = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    largest_amount: int = 0
    smallest_amount: int = 0
    average_amount: int = 0


@dataclass
class RiskContext:
    similarity: SimilarityResult
    legit: RecipientStats
    suspicious_prior: RecipientStats  # victim -> suspicious BEFORE this transfer
    amount: int
    decimals: int
    tx_time: datetime
    initiator_is_victim: bool | None = None
    symbol: str = "USDT"
    suspicious_flagged_before: bool = False  # earlier payments to it were themselves flagged
    # --- optional evidence (None = not investigated / unknown) ---
    prior_dust_to_victim: Tri = Tri.UNKNOWN
    dust_recipients_count: int | None = None
    other_victims_paid: int = 0
    suspicious_prior_activity: int | None = None
    distinct_senders: int | None = None
    forwarded_pct: int | None = None
    label_category: str | None = None
    subsequent_payments: int | None = None  # retrospective only: later payments to the same address
    dust_times: list[datetime] = field(default_factory=list)  # when the look-alike touched the victim (dust / contacts)


@dataclass
class Signal:
    key: str
    points: int
    description: str
    kind: str = "ANALYSIS"  # FACT (directly observed) / ANALYSIS (inference)


@dataclass
class RiskAssessment:
    score: int
    event_type: EventType | None
    signals: list[Signal] = field(default_factory=list)
    capped_reason: str | None = None

    def breakdown(self) -> list[dict]:
        return [asdict(s) for s in self.signals]


class RiskEngine:
    def __init__(self, config: RiskConfig) -> None:
        self.cfg = config

    def w(self, key: str) -> int:
        return int(self.cfg.weights.get(key, 0))

    def rapid_sequence(self, ctx: RiskContext) -> tuple[int, int] | None:
        """(seconds legit payment -> first look-alike contact, seconds legit payment -> victim's payment) or None."""
        last = ctx.legit.last_seen
        if last is None or not ctx.dust_times:
            return None
        pay_s = (ctx.tx_time - last).total_seconds()
        if pay_s < 0 or pay_s > self.cfg.rapid_payment_hours * 3600:
            return None
        after = [(t - last).total_seconds() for t in ctx.dust_times if last <= t <= ctx.tx_time]
        if not after or min(after) > self.cfg.rapid_dust_minutes * 60:
            return None
        return int(min(after)), int(pay_s)

    def legit_is_established(self, legit: RecipientStats) -> bool:
        return legit.transaction_count >= self.cfg.min_legit_tx_count or legit.total_amount >= self.cfg.min_legit_total

    def assess(self, ctx: RiskContext) -> RiskAssessment:
        sig: list[Signal] = []
        add = lambda key, desc, kind="ANALYSIS", sign=1: sig.append(Signal(key, sign * self.w(key), desc, kind))  # noqa: E731
        d = ctx.decimals
        fmt = lambda units: f"{format_amount(units, d)} {ctx.symbol}"  # noqa: E731
        lg, sp, sim = ctx.legit, ctx.suspicious_prior, ctx.similarity
        rapid = self.rapid_sequence(ctx)

        # -- historical relationship with the legitimate recipient --------------
        if lg.transaction_count >= 2:
            add("legit_used_2plus", f"Victim paid the legitimate recipient {lg.transaction_count} times before", "FACT")
        if lg.transaction_count >= 5:
            add("legit_used_5plus", "Legitimate recipient is a repeat counterparty (>= 5 payments)")
        if lg.transaction_count >= 10:
            add("legit_used_10plus", "Legitimate recipient is a frequent counterparty (>= 10 payments)")
        if lg.total_amount >= self.cfg.legit_substantial_total:
            add("legit_substantial_volume", f"Victim previously sent {fmt(lg.total_amount)} in total to the legitimate recipient", "FACT")
        if lg.last_seen and ctx.tx_time - lg.last_seen <= timedelta(days=self.cfg.legit_recent_days):
            add("legit_recent", f"Legitimate recipient was used within the last {self.cfg.legit_recent_days} days")
        if lg.transaction_count <= 1 and lg.total_amount < self.cfg.min_legit_total and not rapid:
            add("legit_weak_relationship", "Relationship with the 'legitimate' recipient is weak (single small payment)", sign=-1)

        # -- timing: the classic "test payment -> dust -> main payment" sequence ----------------
        if rapid:
            dust_s, pay_s = rapid
            add(
                "rapid_poisoning_sequence",
                f"Look-alike touched the victim {_dur(dust_s)} after the victim paid the real address; "
                f"the victim then paid the look-alike {_dur(pay_s)} after paying the real address",
                "FACT",
            )

        # -- novelty of the suspicious recipient -----------------------------------
        if sp.transaction_count == 0:
            add("recipient_new", "Victim had never paid the suspicious recipient before", "FACT")
        elif ctx.suspicious_flagged_before:
            add("suspicious_previously_flagged", "Victim's earlier payment(s) to this address were already flagged as possible poisoning (repeat loss)", "FACT")
        else:
            add(
                "suspicious_previously_used",
                f"Victim had already paid the suspicious recipient {sp.transaction_count} time(s) before",
                "FACT",
                sign=-1,
            )
            if sp.transaction_count >= 3 or sp.total_amount >= self.cfg.legit_substantial_total:
                add("suspicious_established_counterparty", "Suspicious recipient is an established counterparty of the victim", sign=-1)

        # -- similarity ------------------------------------------------------------
        if sim.edge_rule == "both_edges":
            add(
                "similarity_both_edges",
                f"Suspicious address matches the legitimate address at both ends ({sim.prefix_match_length} leading + {sim.suffix_match_length} trailing chars)",
                "FACT",
            )
        elif sim.edge_rule == "single_edge":
            add(
                "similarity_single_edge",
                f"Suspicious address matches one end of the legitimate address ({max(sim.prefix_match_length, sim.suffix_match_length)} chars)",
                "FACT",
            )
        if sim.very_high:
            add("similarity_very_high", f"Very high overall similarity ({sim.similarity_pct}%)")

        # -- amount ---------------------------------------------------------------------
        if ctx.amount >= self.cfg.significant_amount:
            add("amount_significant", f"Significant amount ({fmt(ctx.amount)})")
        if ctx.amount >= self.cfg.large_amount:
            add("amount_large", f"Large amount ({fmt(ctx.amount)})")
        if lg.transaction_count >= 2 and lg.smallest_amount // 2 <= ctx.amount <= lg.largest_amount * 2:
            add("amount_consistent_with_legit", "Amount is consistent with the victim's earlier payments to the legitimate recipient")

        # -- transaction initiator ------------------------------------------------------
        if ctx.initiator_is_victim is False:
            add("third_party_initiated", "Transfer was not signed by the victim (spender / transferFrom)", "FACT", sign=-1)

        # -- poisoning evidence ---------------------------------------------------------
        if ctx.prior_dust_to_victim == Tri.YES:
            add("prior_dust_to_victim", "Suspicious address previously sent a dust / zero-value transfer involving the victim", "FACT")
        if ctx.dust_recipients_count is not None and ctx.dust_recipients_count >= self.cfg.multi_victim_min:
            add("suspicious_multi_victim", f"Suspicious address sent dust transfers to {ctx.dust_recipients_count} different wallets", "FACT")
        if ctx.other_victims_paid > 0:
            add("suspicious_paid_by_other_victims", f"{ctx.other_victims_paid} other wallet(s) were also observed paying the suspicious address", "FACT")
        if ctx.suspicious_prior_activity is not None and ctx.suspicious_prior_activity <= self.cfg.fresh_address_max_prior_transfers:
            add("suspicious_fresh_address", f"Suspicious address had little prior activity ({ctx.suspicious_prior_activity} transfers)")
        if ctx.distinct_senders is not None and ctx.distinct_senders >= self.cfg.many_senders_min:
            add("suspicious_many_unrelated_senders", f"Suspicious address received funds from {ctx.distinct_senders} different wallets")
        if ctx.forwarded_pct is not None and ctx.forwarded_pct >= self.cfg.forward_min_ratio_pct:
            add("suspicious_fast_forwarding", f"Suspicious address forwarded {ctx.forwarded_pct}% of the received amount shortly afterwards", "FACT")
        if ctx.subsequent_payments is not None and ctx.subsequent_payments >= 2:
            add(
                "suspicious_reused_later",
                f"Victim kept paying this address afterwards ({ctx.subsequent_payments} later payments) - consistent with a legitimate counterparty",
                "FACT",
                sign=-1,
            )
        if ctx.label_category in ("exchange", "service"):
            add("suspicious_labeled_service", "Suspicious address carries a public exchange/service label", sign=-1)

        score = max(0, min(100, sum(s.points for s in sig)))
        capped = None
        event_type: EventType | None
        if score >= self.cfg.confidence_threshold:
            event_type = EventType.SUCCESSFUL_POISONING_EVENT
        elif score >= self.cfg.candidate_threshold:
            event_type = EventType.POISONING_CANDIDATE
        else:
            event_type = None

        if event_type == EventType.SUCCESSFUL_POISONING_EVENT:
            if ctx.amount < self.cfg.min_victim_amount:
                event_type, capped = EventType.POISONING_CANDIDATE, "amount below MIN_VICTIM_AMOUNT_USDT"
            elif not self.legit_is_established(lg) and not rapid:
                event_type, capped = EventType.POISONING_CANDIDATE, "legitimate recipient not established"
            elif ctx.initiator_is_victim is False:
                event_type, capped = EventType.POISONING_CANDIDATE, "transfer not initiated by victim"
        return RiskAssessment(score=score, event_type=event_type, signals=sig, capped_reason=capped)


def _dur(seconds: int) -> str:
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 7200:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h"
