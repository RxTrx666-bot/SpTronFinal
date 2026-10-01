"""Human- and machine-readable outputs for an incident.

Everything here is generated strictly from stored blockchain observations and
the analysis results.  Wording rules (enforced by tests):

* never claim certainty ("confirmed scam", "scammer", "stolen", "thief");
* use "possible" / "high-confidence address-poisoning event";
* keep OBSERVED BLOCKCHAIN FACTS separate from ANALYTICAL ASSESSMENT;
* exchange/service names are "possible exchange/service attribution" only.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import EventType
from app.models import (
    AddressSimilarityMatch,
    FundTrace,
    HistoricalRecipient,
    PoisoningEvent,
    PoisoningEvidence,
    Transaction,
)
from app.utils.address import short, tronscan_address_url, tronscan_tx_url
from app.utils.amounts import format_amount
from app.utils.clock import as_utc, iso

DISCLAIMER = (
    "Blockchain data shows addresses, transactions, amounts, timestamps and fund movements. "
    "It does not by itself prove who controls an address, anyone's identity, intent or criminal responsibility."
)

STATUS_LINE = {
    EventType.SUCCESSFUL_POISONING_EVENT.value: "⚠️ POSSIBLE SUCCESSFUL ADDRESS-POISONING ATTACK",
    EventType.POISONING_CANDIDATE.value: "🟠 POSSIBLE ADDRESS POISONING (below confidence threshold)",
    EventType.POISONING_ATTEMPT.value: "🟡 POISONING ATTEMPT (victim has not sent funds)",
}

TITLE = {
    EventType.SUCCESSFUL_POISONING_EVENT.value: "🚨 SUCCESSFUL ADDRESS POISONING DETECTED",
    EventType.POISONING_CANDIDATE.value: "🟠 POSSIBLE ADDRESS POISONING - CANDIDATE",
    EventType.POISONING_ATTEMPT.value: "🟡 ADDRESS-POISONING ATTEMPT",
}


@dataclass
class CaseBundle:
    event: PoisoningEvent
    evidence: list[PoisoningEvidence] = field(default_factory=list)
    similarity: dict[str, Any] = field(default_factory=dict)
    traces: list[FundTrace] = field(default_factory=list)
    legit_row: HistoricalRecipient | None = None
    legit_payments: list[Transaction] = field(default_factory=list)
    suspicious_payments: list[Transaction] = field(default_factory=list)
    network: str = "TRON"

    # ---------------------------------------------------------------- helpers
    def amt(self, units: int | None) -> str:
        if units is None:
            return "unknown"
        return f"{format_amount(units, self.event.token_decimals)} {self.event.token_symbol}"

    @property
    def token_name(self) -> str:
        return f"{self.event.token_symbol} TRC-20"

    @property
    def is_success(self) -> bool:
        return self.event.event_type == EventType.SUCCESSFUL_POISONING_EVENT.value

    def facts(self) -> list[PoisoningEvidence]:
        return [e for e in self.evidence if e.kind == "FACT"]

    def analysis(self) -> list[PoisoningEvidence]:
        return [e for e in self.evidence if e.kind != "FACT"]

    def ev(self, evidence_type: str) -> list[PoisoningEvidence]:
        return [e for e in self.evidence if e.evidence_type == evidence_type]

    def trace_hops(self) -> list[FundTrace]:
        return sorted(self.traces, key=lambda t: (t.hop, as_utc(t.block_timestamp), t.id or 0))


async def load_bundle(session: AsyncSession, event_id: int) -> CaseBundle:
    ev = await session.get(PoisoningEvent, event_id)
    if ev is None:
        raise KeyError(event_id)
    evidence = list((await session.execute(select(PoisoningEvidence).where(PoisoningEvidence.event_id == event_id).order_by(PoisoningEvidence.id))).scalars())
    sim_row = (
        await session.execute(
            select(AddressSimilarityMatch).where(
                AddressSimilarityMatch.transaction_id == ev.transaction_id,
                AddressSimilarityMatch.victim_wallet == ev.victim_wallet,
                AddressSimilarityMatch.legitimate_recipient == ev.legitimate_recipient,
            )
        )
    ).scalar_one_or_none()
    last_run = (
        await session.execute(select(FundTrace.trace_run).where(FundTrace.event_id == event_id).order_by(FundTrace.trace_run.desc()).limit(1))
    ).scalar_one_or_none()
    traces: list[FundTrace] = []
    if last_run is not None:
        traces = list((await session.execute(select(FundTrace).where(FundTrace.event_id == event_id, FundTrace.trace_run == last_run))).scalars())
    legit_row = (
        await session.execute(
            select(HistoricalRecipient).where(
                HistoricalRecipient.victim_wallet == ev.victim_wallet,
                HistoricalRecipient.recipient_wallet == ev.legitimate_recipient,
                HistoricalRecipient.token_contract == ev.token_contract,
            )
        )
    ).scalar_one_or_none()
    legit_payments = list(
        (
            await session.execute(
                select(Transaction)
                .where(
                    Transaction.from_address == ev.victim_wallet,
                    Transaction.to_address == ev.legitimate_recipient,
                    Transaction.token_contract == ev.token_contract,
                )
                .order_by(Transaction.block_timestamp.desc())
                .limit(10)
            )
        ).scalars()
    )
    suspicious_payments = list(
        (
            await session.execute(
                select(Transaction)
                .where(
                    Transaction.token_contract == ev.token_contract,
                    or_(
                        and_(Transaction.from_address == ev.victim_wallet, Transaction.to_address == ev.suspicious_recipient),
                        and_(Transaction.from_address == ev.suspicious_recipient, Transaction.to_address == ev.victim_wallet),
                    ),
                )
                .order_by(Transaction.block_timestamp)
                .limit(20)
            )
        ).scalars()
    )
    sim = dict(sim_row.metrics) if sim_row else {}
    return CaseBundle(
        event=ev,
        evidence=evidence,
        similarity=sim,
        traces=traces,
        legit_row=legit_row,
        legit_payments=legit_payments,
        suspicious_payments=suspicious_payments,
    )


def _h(s: Any) -> str:
    return html.escape(str(s), quote=False)


def _link(url: str, label: str) -> str:
    return f'<a href="{html.escape(url)}">{_h(label)}</a>'


def latency_text(ev: PoisoningEvent) -> str:
    parts = []
    if ev.chain_latency_ms is not None:
        parts.append(f"block→detected {ev.chain_latency_ms} ms")
    if ev.detection_latency_ms is not None:
        parts.append(f"detected→analysed {ev.detection_latency_ms} ms")
    if ev.alert_latency_ms is not None:
        parts.append(f"detected→alert {ev.alert_latency_ms} ms")
    return ", ".join(parts) or "n/a"


def evidence_bullets(b: CaseBundle) -> list[str]:
    ev = b.event
    out = []
    if ev.event_type != EventType.POISONING_ATTEMPT.value:
        out.append(
            f"Victim repeatedly used legitimate recipient ({ev.legit_tx_count} previous payments)"
            if ev.legit_tx_count >= 2
            else f"Victim used legitimate recipient ({ev.legit_tx_count} previous payment)"
        )
        out.append(
            "New recipient strongly resembles legitimate recipient"
            if ev.suspicious_prior_tx_count == 0
            else f"Recipient resembles legitimate recipient (victim paid it {ev.suspicious_prior_tx_count} time(s) before)"
        )
        out.append("Victim sent funds to the look-alike recipient")
    else:
        out.append("Look-alike address interacted with the victim (no payment by the victim)")
    out.append(f"Suspicious recipient had prior dust interaction: {ev.poisoning_tx_observed}")
    if b.ev("OTHER_VICTIMS"):
        out.append(b.ev("OTHER_VICTIMS")[0].description)
    if b.ev("MULTI_VICTIM_DUST"):
        out.append(b.ev("MULTI_VICTIM_DUST")[0].description)
    if ev.forwarding_summary:
        out.append(f"Forwarding: {ev.forwarding_summary}")
    if not ev.history_complete:
        out.append("Note: victim history scan was still in progress")
    return out


# ------------------------------------------------------------------ telegram
def telegram_alert(b: CaseBundle) -> str:
    ev = b.event
    sim_pct = int(round(ev.similarity_score * 100))
    lines = [
        f"<b>{_h(TITLE[ev.event_type])}</b>",
        "",
        f"<b>Case:</b> <code>{_h(ev.case_id)}</code>",
        f"<b>Network:</b> {b.network}",
        f"<b>Token:</b> {_h(b.token_name)}",
        "",
        "<b>Victim:</b>",
        f"<code>{ev.victim_wallet}</code>",
        "",
        "<b>Victim sent:</b>" if ev.event_type != EventType.POISONING_ATTEMPT.value else "<b>Transfer amount:</b>",
        f"<b>{_h(b.amt(ev.amount))}</b>",
        "",
        "<b>Legitimate historical recipient:</b>",
        f"<code>{ev.legitimate_recipient}</code>",
        "",
        "<b>Suspicious recipient:</b>" if ev.event_type != EventType.POISONING_ATTEMPT.value else "<b>Look-alike address:</b>",
        f"<code>{ev.suspicious_recipient}</code>",
        "",
        f"<b>Recipient similarity:</b> {sim_pct}%  "
        f"(prefix {b.similarity.get('prefix_match_length', '?')} / suffix {b.similarity.get('suffix_match_length', '?')} chars after T)",
        f"<b>Historical legitimate recipient:</b> {ev.legit_tx_count} previous transactions ({_h(b.amt(ev.legit_total_amount))})",
        f"<b>Suspicious recipient history:</b> {ev.suspicious_prior_tx_count} previous transaction(s) from victim",
        f"<b>Confidence:</b> {ev.confidence}/100",
        f"<b>Block:</b> {ev.block_number if ev.block_number is not None else 'pending'} ({_h(ev.confirmation_status.lower())})",
        f"<b>Block time:</b> {_h(iso(ev.block_timestamp))}",
        "",
        f"<b>Transaction:</b> {_link(tronscan_tx_url(ev.tx_hash), ev.tx_hash[:16] + '…')}",
        f"<b>Victim:</b> {_link(tronscan_address_url(ev.victim_wallet), short(ev.victim_wallet, 8, 6))}",
        f"<b>Legitimate recipient:</b> {_link(tronscan_address_url(ev.legitimate_recipient), short(ev.legitimate_recipient, 8, 6))}",
        f"<b>Suspicious recipient:</b> {_link(tronscan_address_url(ev.suspicious_recipient), short(ev.suspicious_recipient, 8, 6))}",
        "",
        f"<b>Detection time:</b> {_h(iso(ev.analysis_completed_at, ms=True))}",
        f"<b>Latency:</b> {_h(latency_text(ev))}",
        "",
        "<b>Evidence:</b>",
    ]
    lines += [f"• {_h(x)}" for x in evidence_bullets(b)]
    lines += ["", f"<b>Status:</b>\n{_h(STATUS_LINE[ev.event_type])}"]
    if ev.is_historical:
        lines.insert(1, "<i>(historical event found during the initial history scan)</i>")
    return "\n".join(lines)


def alert_keyboard(event_id: int, *, x_enabled: bool, include_x: bool = True) -> dict:
    rows = [
        [{"text": "📋 COPY CASE", "callback_data": f"copy:{event_id}"}, {"text": "🔎 TRACE FUNDS", "callback_data": f"trace:{event_id}"}],
        [{"text": "📄 FULL REPORT", "callback_data": f"report:{event_id}"}],
    ]
    if include_x:
        rows[1].append({"text": "🐦 PREPARE X POST", "callback_data": f"xprep:{event_id}"})
    return {"inline_keyboard": rows}


def trace_message(b: CaseBundle) -> str:
    ev = b.event
    hops = b.trace_hops()
    lines = [f"<b>🔎 FUND TRACE</b> — <code>{_h(ev.case_id)}</code>", ""]
    if not hops:
        lines.append(f"No outgoing {ev.token_symbol} transfers from the suspicious address were found yet.")
        lines.append(f"<code>{ev.suspicious_recipient}</code>")
        return "\n".join(lines)
    lines.append(f"Victim <code>{short(ev.victim_wallet, 8, 6)}</code>")
    lines.append(f"  ↓ {_h(b.amt(ev.amount))}  {_link(tronscan_tx_url(ev.tx_hash), ev.tx_hash[:10] + '…')}")
    lines.append(f"Suspicious <code>{short(ev.suspicious_recipient, 8, 6)}</code>")
    for t in hops:
        label = f" — <i>{_h(t.to_label)} (possible exchange/service attribution)</i>" if t.to_label else ""
        term = f" [{_h(t.terminal_reason)}]" if t.terminal_reason else ""
        lines.append(
            f"{'  ' * t.hop}↳ hop {t.hop}: {_h(b.amt(t.amount))} → <code>{short(t.to_address, 8, 6)}</code>{label}{term}\n"
            f"{'  ' * t.hop}   {_link(tronscan_tx_url(t.tx_hash), t.tx_hash[:10] + '…')} · block {t.block_number or '?'} · {_h(iso(t.block_timestamp))} · {_h(t.confirmation_status.lower())}"
        )
    lines += ["", "<i>Trace follows on-chain transfers only; it does not establish who controls any address.</i>"]
    return "\n".join(lines)


# ------------------------------------------------------------------ evidence packet
def evidence_packet(b: CaseBundle) -> str:
    ev = b.event
    fwd = ev.forwarding_summary or "No forwarding observed at the time of analysis"
    prior_activity = "; ".join(e.description for e in b.ev("SUSPICIOUS_ACTIVITY")) or (
        f"{ev.suspicious_prior_tx_count} previous transfer(s) from the victim" if ev.suspicious_prior_tx_count else "No previous transfers from the victim"
    )
    trace_lines = ["Victim", f"→ Suspicious Address ({ev.suspicious_recipient})"]
    for t in b.trace_hops():
        lbl = f" [possible exchange/service attribution: {t.to_label}]" if t.to_label else ""
        trace_lines.append(f"{'  ' * (t.hop - 1)}→ Address {t.hop + 1} (hop {t.hop}): {t.to_address} ({b.amt(t.amount)}, tx {t.tx_hash}){lbl}")
    if not b.traces:
        trace_lines.append("→ (no downstream transfers recorded yet)")
    links = [
        f"Transaction: {tronscan_tx_url(ev.tx_hash)}",
        f"Victim: {tronscan_address_url(ev.victim_wallet)}",
        f"Legitimate recipient: {tronscan_address_url(ev.legitimate_recipient)}",
        f"Suspicious recipient: {tronscan_address_url(ev.suspicious_recipient)}",
    ]
    for e in b.ev("PRIOR_DUST"):
        if e.tx_hash:
            links.append(f"Poisoning/dust transaction: {tronscan_tx_url(e.tx_hash)}")
    sections = [
        ("CASE ID", ev.case_id),
        ("EVENT TYPE", ev.event_type),
        ("NETWORK", b.network),
        ("TOKEN", f"{b.token_name} ({ev.token_contract})"),
        ("VICTIM", ev.victim_wallet),
        ("LEGITIMATE RECIPIENT", ev.legitimate_recipient),
        ("SUSPICIOUS RECIPIENT", ev.suspicious_recipient),
        ("AMOUNT", b.amt(ev.amount)),
        ("TRANSACTION HASH", ev.tx_hash),
        ("BLOCK", f"{ev.block_number if ev.block_number is not None else 'unknown'} ({ev.confirmation_status})"),
        ("TIMESTAMP", iso(ev.block_timestamp)),
        (
            "SIMILARITY",
            f"{int(round(ev.similarity_score * 100))}% (prefix {b.similarity.get('prefix_match_length', '?')} chars, "
            f"suffix {b.similarity.get('suffix_match_length', '?')} chars after leading T)",
        ),
        ("HISTORICAL TRANSACTIONS WITH LEGITIMATE RECIPIENT", str(ev.legit_tx_count)),
        ("PREVIOUS VICTIM → LEGITIMATE TOTAL", b.amt(ev.legit_total_amount)),
        ("SUSPICIOUS RECIPIENT PREVIOUS ACTIVITY", prior_activity),
        ("POISONING TRANSACTION OBSERVED", ev.poisoning_tx_observed),
        ("FORWARDING ACTIVITY", fwd),
        ("TRACE", "\n".join(trace_lines)),
        ("TRONSCAN LINKS", "\n".join(links)),
        ("CONFIDENCE", f"{ev.confidence}/100"),
        ("ASSESSMENT", STATUS_LINE[ev.event_type]),
        ("NOTE", DISCLAIMER),
    ]
    return "\n\n".join(f"{k}:\n{v}" for k, v in sections)


# ------------------------------------------------------------------ X post
_X_WEIGHT_1 = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))


def _char_weight(c: str) -> int:
    o = ord(c)
    if o in (0xFE0F, 0x200D):  # emoji variation selector / zero-width joiner
        return 0
    return 1 if any(lo <= o <= hi for lo, hi in _X_WEIGHT_1) else 2


def tweet_length(text: str) -> int:
    """X weighted length (twitter-text rules, simplified): URLs count 23, CJK/emoji count 2."""
    n = 0
    for i, word in enumerate(text.replace("\n", " \n ").split(" ")):
        if i:
            n += 1
        if word.startswith(("http://", "https://")):
            n += 23
        else:
            n += sum(_char_weight(c) for c in word)
    return n - 2 * text.count("\n")  # undo the spaces added around newlines


def x_post(b: CaseBundle) -> str:
    """Short, factual, copy-ready post (<= 280 weighted chars).  Never claims certainty."""
    ev = b.event
    amount = format_amount(ev.amount, ev.token_decimals)
    if ev.event_type == EventType.SUCCESSFUL_POISONING_EVENT.value:
        headline = "🚨 TRON ADDRESS-POISONING ALERT"
        summary = f"A wallet appears to have sent {amount} {ev.token_symbol} to a look-alike address previously unused by the victim."
    else:
        headline = "⚠️ POSSIBLE TRON ADDRESS POISONING"
        summary = f"A wallet sent {amount} {ev.token_symbol} to an address closely resembling one it had used before."
    legit = f"Legitimate recipient: {short(ev.legitimate_recipient, 7, 5)}"
    sus = f"Suspicious recipient: {short(ev.suspicious_recipient, 7, 5)}"
    resemble = "The suspicious address closely resembles a recipient the victim had previously used."
    url = tronscan_tx_url(ev.tx_hash)
    warn = "⚠️ Verify recipient addresses carefully before sending funds."
    warn_short = "⚠️ Verify addresses before sending."
    variants = [
        f"{headline}\n\n{summary}\n\n{legit}\n{sus}\n\n{resemble}\n\nTransaction: {url}\n\n{warn}",
        f"{headline}\n\n{summary}\n\n{legit}\n{sus}\n\nTransaction: {url}\n\n{warn}",
        f"{headline}\n\n{summary}\n\n{legit}\n{sus}\n\n{url}\n\n{warn_short}",
        f"{headline}\n{summary}\n{legit}\n{sus}\n{url}\n{warn_short}",
        f"{headline}\n{summary}\n{legit}\n{sus}\n{url}",
    ]
    for v in variants:
        if tweet_length(v) <= 280:
            return v
    return variants[-1]


# ------------------------------------------------------------------ investigator report
def _row(cells: list[Any]) -> str:
    return "| " + " | ".join(str(c).replace("|", "/") for c in cells) + " |"


def investigator_report(b: CaseBundle) -> str:
    ev = b.event
    sim = b.similarity
    L = []
    L.append(f"# Address-Poisoning Incident Report — {ev.case_id}")
    L.append("")
    L.append(f"**Assessment:** {STATUS_LINE[ev.event_type]}  ")
    L.append(f"**Confidence:** {ev.confidence}/100 (threshold-based analytical score, not a certainty)  ")
    L.append(f"**Generated:** {iso(ev.updated_at)}")
    L.append("")
    L.append("## 1. Summary")
    L.append("")
    if ev.event_type == EventType.POISONING_ATTEMPT.value:
        L.append(
            f"The look-alike address `{ev.suspicious_recipient}` interacted with the monitored wallet `{ev.victim_wallet}`. "
            "The monitored wallet has not been observed sending funds to it."
        )
    else:
        L.append(
            f"The monitored wallet `{ev.victim_wallet}` sent **{b.amt(ev.amount)}** to `{ev.suspicious_recipient}`, "
            f"an address whose beginning and end closely resemble `{ev.legitimate_recipient}`, a recipient the wallet had "
            f"paid {ev.legit_tx_count} time(s) before (total {b.amt(ev.legit_total_amount)}). "
            f"Before this transfer the wallet had made {ev.suspicious_prior_tx_count} payment(s) to the look-alike address."
        )
    L.append("")
    L.append("## 2. Observed blockchain facts")
    L.append("")
    L.append(_row(["Field", "Value"]))
    L.append(_row(["---", "---"]))
    for k, v in [
        ("Case ID", ev.case_id),
        ("Network", b.network),
        ("Token", f"{b.token_name} `{ev.token_contract}`"),
        ("Victim address", f"`{ev.victim_wallet}`"),
        ("Legitimate historical recipient", f"`{ev.legitimate_recipient}`"),
        ("Suspicious recipient", f"`{ev.suspicious_recipient}`"),
        ("Amount", b.amt(ev.amount)),
        ("Transaction hash", f"`{ev.tx_hash}`"),
        ("Block", ev.block_number if ev.block_number is not None else "unknown"),
        ("Block timestamp", iso(ev.block_timestamp)),
        ("Confirmation status", ev.confirmation_status),
        ("Transaction signer", f"`{ev.initiator_address}`" if ev.initiator_address else "not resolved"),
        ("Poisoning (dust / zero-value) transaction observed", ev.poisoning_tx_observed),
    ]:
        L.append(_row([k, v]))
    L.append("")
    for e in b.facts():
        extra = f" (tx `{e.tx_hash}`)" if e.tx_hash else ""
        L.append(f"- {e.description}{extra}")
    L.append("")
    L.append("### 2.1 Victim → legitimate recipient (most recent payments)")
    L.append("")
    if b.legit_row:
        r = b.legit_row
        L.append(
            f"Aggregate (all recorded payments): {r.transaction_count} transfers, total {b.amt(r.total_amount)}, "
            f"first {iso(r.first_seen)}, last {iso(r.last_seen)}, largest {b.amt(r.largest_amount)}, "
            f"smallest {b.amt(r.smallest_amount)}, average {b.amt(r.average_amount)}."
        )
        L.append("")
    if b.legit_payments:
        L.append(_row(["Time", "Amount", "Transaction"]))
        L.append(_row(["---", "---", "---"]))
        for t in b.legit_payments:
            L.append(_row([iso(t.block_timestamp), b.amt(t.amount), f"[{t.tx_hash[:16]}…]({tronscan_tx_url(t.tx_hash)})"]))
        L.append("")
    L.append("### 2.2 Transfers between victim and suspicious address")
    L.append("")
    if b.suspicious_payments:
        L.append(_row(["Time", "Direction", "Amount", "Transaction"]))
        L.append(_row(["---", "---", "---", "---"]))
        for t in b.suspicious_payments:
            direction = "victim → suspicious" if t.from_address == ev.victim_wallet else "suspicious → victim"
            L.append(_row([iso(t.block_timestamp), direction, b.amt(t.amount), f"[{t.tx_hash[:16]}…]({tronscan_tx_url(t.tx_hash)})"]))
    else:
        L.append("None recorded.")
    L.append("")
    L.append("## 3. Address similarity measurements")
    L.append("")
    L.append(f"Legitimate: `{ev.legitimate_recipient}`  ")
    L.append(f"Suspicious: `{ev.suspicious_recipient}`")
    L.append("")
    L.append(_row(["Metric", "Value"]))
    L.append(_row(["---", "---"]))
    for key, label in [
        ("prefix_match_length", "Prefix match length (chars after leading T)"),
        ("suffix_match_length", "Suffix match length"),
        ("fuzzy_prefix_match_length", "Prefix match incl. case/confusable characters"),
        ("fuzzy_suffix_match_length", "Suffix match incl. case/confusable characters"),
        ("prefix_similarity", "Prefix similarity"),
        ("suffix_similarity", "Suffix similarity"),
        ("overall_similarity", "Overall similarity (normalised Levenshtein)"),
        ("positional_similarity", "Positional similarity"),
        ("similarity_score", "Weighted similarity score"),
        ("coincidence_log10", "log10 probability a random address shares these edges"),
        ("edge_rule", "Match rule"),
    ]:
        if key in sim:
            L.append(_row([label, sim[key]]))
    L.append("")
    L.append("## 4. Poisoning evidence")
    L.append("")
    poison = [
        e
        for e in b.evidence
        if e.evidence_type in ("PRIOR_DUST", "MULTI_VICTIM_DUST", "OTHER_VICTIMS", "DUST_FUNDING", "SUSPICIOUS_ACTIVITY", "MANY_SENDERS", "ACCOUNT_AGE")
    ]
    if poison:
        for e in poison:
            L.append(f"- [{e.kind}] {e.description}" + (f" — tx `{e.tx_hash}`" if e.tx_hash else ""))
    else:
        L.append(
            f"No dust or zero-value poisoning transfer was found (status: {ev.poisoning_tx_observed}). "
            "A poisoning transfer is supporting evidence only and is not required for detection."
        )
    L.append("")
    L.append("## 5. Downstream fund tracing")
    L.append("")
    hops = b.trace_hops()
    if hops:
        L.append(_row(["Hop", "From", "To", "Amount", "Transaction", "Block", "Time", "Status", "Attribution"]))
        L.append(_row(["---"] * 9))
        for t in hops:
            attr = f"{t.to_label} (possible exchange/service attribution, source: {t.to_label_source})" if t.to_label else ""
            if t.terminal_reason:
                attr = (attr + "; " if attr else "") + t.terminal_reason
            L.append(
                _row(
                    [
                        t.hop,
                        f"`{t.from_address}`",
                        f"`{t.to_address}`",
                        b.amt(t.amount),
                        f"[{t.tx_hash[:12]}…]({tronscan_tx_url(t.tx_hash)})",
                        t.block_number or "?",
                        iso(t.block_timestamp),
                        t.confirmation_status,
                        attr,
                    ]
                )  # fmt: skip
            )
    else:
        L.append("No downstream transfers recorded (trace not yet run, or funds not yet moved).")
    if ev.forwarding_summary:
        L.append("")
        L.append(f"Forwarding: {ev.forwarding_summary}")
    L.append("")
    L.append("## 6. Analytical assessment")
    L.append("")
    L.append(_row(["Signal", "Points", "Basis"]))
    L.append(_row(["---", "---", "---"]))
    for s in ev.score_breakdown or []:
        L.append(_row([s.get("description"), s.get("points"), s.get("kind")]))
    L.append("")
    for e in b.analysis():
        if e.evidence_type != "SIGNAL":  # signals are already listed in the table above
            L.append(f"- {e.description}")
    L.append("")
    L.append(f"Resulting confidence: **{ev.confidence}/100** → {STATUS_LINE[ev.event_type]}")
    L.append("")
    L.append("## 7. All relevant transaction hashes")
    L.append("")
    for h in all_tx_hashes(b):
        L.append(f"- `{h}` — {tronscan_tx_url(h)}")
    L.append("")
    L.append("## 8. All relevant addresses")
    L.append("")
    for role, a in all_addresses(b):
        L.append(f"- {role}: `{a}` — {tronscan_address_url(a)}")
    L.append("")
    L.append("## 9. Methodology")
    L.append("")
    L.append(
        "1. Every transfer of the monitored token sent by the wallet is compared with the wallet's historical recipient "
        "database (built from the full available transfer history).\n"
        "2. Recipients the wallet never (or rarely) used are compared against its established recipients using several "
        "independent similarity measures (prefix/suffix match excluding the constant leading 'T', case/confusable-aware "
        "edge matching, Levenshtein and positional similarity).\n"
        "3. A configurable, additive risk model combines relationship history, recipient novelty, similarity, amount, "
        "transaction signer, and optional on-chain poisoning evidence (dust/zero-value transfers, multi-victim activity, "
        "forwarding) into a 0–100 confidence score. The full breakdown is listed in section 6.\n"
        "4. A successful-poisoning event is raised only when the victim actually sends funds and the score crosses the "
        "configured threshold. A dust transfer is supporting evidence, never a requirement.\n"
        "5. Funds are followed for a configurable number of hops along the largest outgoing transfers after receipt."
    )
    L.append("")
    L.append("## 10. Limitations")
    L.append("")
    L.append(
        f"- {DISCLAIMER}\n"
        "- The confidence score is an analytical estimate; a legitimate new address that happens to resemble an old one "
        "would produce the same on-chain pattern.\n"
        "- History completeness depends on the data provider (pagination/retention limits).\n"
        "- Only the configured token contract is analysed; dust sent with other tokens (including counterfeit tokens) "
        "and TRX/swap activity is not followed by the tracer.\n"
        "- Fund tracing follows the largest outgoing transfers (FIFO approximation); mixing, swaps and exchange internal "
        "transfers can break or blur the trail.\n"
        "- Exchange/service names are public labels and are shown as possible attribution only."
    )
    return "\n".join(L) + "\n"


def all_tx_hashes(b: CaseBundle) -> list[str]:
    seen: list[str] = []
    for h in (
        [b.event.tx_hash] + [e.tx_hash for e in b.evidence if e.tx_hash] + [t.tx_hash for t in b.trace_hops()] + [t.tx_hash for t in b.suspicious_payments]
    ):
        if h and h not in seen:
            seen.append(h)
    return seen


def all_addresses(b: CaseBundle) -> list[tuple[str, str]]:
    ev = b.event
    out = [("Victim", ev.victim_wallet), ("Legitimate recipient", ev.legitimate_recipient), ("Suspicious recipient", ev.suspicious_recipient)]
    seen = {a for _, a in out}
    for t in b.trace_hops():
        if t.to_address not in seen:
            seen.add(t.to_address)
            out.append((f"Trace address {t.hop + 1} (hop {t.hop})", t.to_address))
    for e in b.evidence:
        if e.address and e.address not in seen:
            seen.add(e.address)
            out.append(("Evidence", e.address))
    return out


def report_json(b: CaseBundle) -> dict[str, Any]:
    ev = b.event

    def ts(dt: datetime | None) -> str | None:
        return as_utc(dt).isoformat() if dt else None

    return {
        "case_id": ev.case_id,
        "event_type": ev.event_type,
        "assessment": STATUS_LINE[ev.event_type],
        "network": b.network,
        "token": ev.token_symbol,
        "token_contract": ev.token_contract,
        "token_decimals": ev.token_decimals,
        "victim": ev.victim_wallet,
        "legitimate_recipient": ev.legitimate_recipient,
        "suspicious_recipient": ev.suspicious_recipient,
        "amount": format_amount(ev.amount, ev.token_decimals, grouping=False),
        "amount_base_units": str(ev.amount),
        "transaction_hash": ev.tx_hash,
        "block_number": ev.block_number,
        "block_timestamp": ts(ev.block_timestamp),
        "confirmation_status": ev.confirmation_status,
        "transaction_signer": ev.initiator_address,
        "confidence": ev.confidence,
        "similarity": b.similarity,
        "legitimate_recipient_history": {
            "transaction_count": ev.legit_tx_count,
            "total_amount_base_units": str(ev.legit_total_amount),
            "first_seen": ts(b.legit_row.first_seen) if b.legit_row else None,
            "last_seen": ts(b.legit_row.last_seen) if b.legit_row else None,
        },
        "suspicious_prior_tx_count": ev.suspicious_prior_tx_count,
        "poisoning_transaction_observed": ev.poisoning_tx_observed,
        "forwarding_summary": ev.forwarding_summary,
        "score_breakdown": ev.score_breakdown,
        "evidence": [
            {
                "type": e.evidence_type,
                "basis": e.kind,
                "supports_poisoning": e.supports,
                "description": e.description,
                "tx_hash": e.tx_hash,
                "address": e.address,
                "amount_base_units": str(e.amount) if e.amount is not None else None,
                "observed_at": ts(e.observed_at),
            }
            for e in b.evidence
        ],
        "fund_trace": [
            {
                "hop": t.hop,
                "from": t.from_address,
                "to": t.to_address,
                "amount": format_amount(t.amount, ev.token_decimals, grouping=False),
                "token": t.token_symbol,
                "transaction_hash": t.tx_hash,
                "block_number": t.block_number,
                "timestamp": ts(t.block_timestamp),
                "confirmation_status": t.confirmation_status,
                "possible_attribution": t.to_label,
                "attribution_source": t.to_label_source,
                "note": t.terminal_reason,
            }
            for t in b.trace_hops()
        ],
        "tronscan": {
            "transaction": tronscan_tx_url(ev.tx_hash),
            "victim": tronscan_address_url(ev.victim_wallet),
            "legitimate_recipient": tronscan_address_url(ev.legitimate_recipient),
            "suspicious_recipient": tronscan_address_url(ev.suspicious_recipient),
        },
        "latency_ms": {
            "block_to_detected": ev.chain_latency_ms,
            "detected_to_analysed": ev.detection_latency_ms,
            "detected_to_alert": ev.alert_latency_ms,
        },
        "is_historical": ev.is_historical,
        "detected_at": ts(ev.analysis_completed_at),
        "disclaimer": DISCLAIMER,
    }


def report_json_text(b: CaseBundle) -> str:
    return json.dumps(report_json(b), indent=2, ensure_ascii=False)
