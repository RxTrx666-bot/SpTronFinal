"""Follow-the-money tracing from the suspicious recipient.

Algorithm (per hop, up to ``TRACE_HOPS``):

1. For each address on the frontier, fetch its outgoing token transfers after
   it received the traced funds (within ``TRACE_WINDOW_DAYS``).
2. FIFO approximation: walk the outflows chronologically until they cover the
   amount received; from those, follow the ``TRACE_MAX_BRANCHES`` largest.
3. Resolve block number / confirmation of every followed transfer and look up
   a public label for the destination.  A destination labelled as an
   exchange/service ends that branch ("possible exchange/service
   attribution"); already-visited addresses end it to avoid loops.

Every run is stored under a new ``trace_run`` number, so re-traces keep the
earlier results.  Tracing is read-only and runs in the background - it never
delays the primary alert.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select

from app.domain import TokenTransfer
from app.models import FundTrace, PoisoningEvent
from app.repository import _insert
from app.utils.amounts import format_amount, ratio_pct
from app.utils.clock import Clock, as_utc, from_ms, to_ms
from app.utils.logging import get_logger
from app.utils.ratelimit import PRIORITY_INVESTIGATION

log = get_logger(__name__)


@dataclass
class TraceHop:
    hop: int
    parent: str
    transfer: TokenTransfer
    block_number: int | None
    confirmed: bool
    label: tuple[str, str, str] | None
    terminal: str | None


@dataclass
class TraceResult:
    event_id: int
    run: int
    hops: list[TraceHop] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def max_depth(self) -> int:
        return max((h.hop for h in self.hops), default=0)


def select_outflows(outs: list[TokenTransfer], amount_in: int, max_branches: int, min_share_pct: int = 0) -> list[TokenTransfer]:
    """Follow the outflows that plausibly carry the traced funds.

    Outflows smaller than ``min_share_pct`` % of the received amount are ignored: attacker hubs send
    many small transfers (e.g. 10 USDT to fund new look-alike addresses) that are not the stolen money.
    """
    floor = amount_in * min_share_pct // 100
    outs = [o for o in outs if o.amount >= floor]
    covered = 0
    window: list[TokenTransfer] = []
    for o in sorted(outs, key=lambda t: (t.block_timestamp_ms, t.tx_hash)):
        window.append(o)
        covered += o.amount
        if covered >= amount_in:
            break
    return sorted(window, key=lambda t: t.amount, reverse=True)[:max_branches]


class FundTracer:
    def __init__(self, settings, session_factory, source, clock: Clock, labels) -> None:
        self.s = settings
        self.sf = session_factory
        self.source = source
        self.clock = clock
        self.labels = labels

    async def _outflows(self, address: str, contract: str, since_ms: int, min_amount: int) -> list[TokenTransfer]:
        out: list[TokenTransfer] = []
        fp = None
        until = since_ms + self.s.trace_window_days * 86_400_000
        for _ in range(5):  # at most 1000 transfers per address
            page, fp = await self.source.get_trc20_transfers(
                address, contract, min_timestamp_ms=since_ms, max_timestamp_ms=until, order="asc", fingerprint=fp, priority=PRIORITY_INVESTIGATION
            )
            out.extend(t for t in page if t.from_address == address and t.to_address != address and t.amount >= min_amount)
            if not fp or not page:
                break
        return out

    async def trace(self, event_id: int, run: int) -> TraceResult:
        async with self.sf() as s:
            ev = await s.get(PoisoningEvent, event_id)
        if ev is None:
            raise KeyError(event_id)
        token = self.s.tokens_by_contract[ev.token_contract]
        min_amount = self.s.units("trace_min_amount_usdt", token.decimals)
        result = TraceResult(event_id=event_id, run=run)
        visited = {ev.victim_wallet, ev.suspicious_recipient}
        frontier: list[tuple[str, int, int, TraceHop | None]] = [(ev.suspicious_recipient, ev.amount, to_ms(as_utc(ev.block_timestamp)), None)]
        share = self.s.trace_min_share_pct
        nodes = 1
        for hop in range(1, self.s.trace_hops + 1):
            nxt: list[tuple[str, int, int, TraceHop | None]] = []
            for addr, amount_in, since, came_from in frontier:
                outs = await self._outflows(addr, token.contract, since, min_amount)
                chosen = select_outflows(outs, amount_in, self.s.trace_max_branches, share)
                if not chosen:
                    small = len(outs)
                    why = (
                        f"funds not moved on yet - no outgoing {token.symbol} transfer of at least {share}% of the "
                        f"{format_amount(amount_in, token.decimals)} received"
                        + (f" ({small} smaller transfer(s) ignored, e.g. funding of new look-alike addresses)" if small else "")
                    )
                    result.notes.append(f"{addr}: {why}")
                    if came_from is not None and came_from.terminal is None:
                        came_from.terminal = why
                    continue
                for o in chosen:
                    if nodes >= self.s.trace_max_nodes:
                        result.notes.append("trace node limit reached")
                        break
                    nodes += 1
                    details = None
                    try:
                        details = await self.source.get_transaction(o.tx_hash, priority=PRIORITY_INVESTIGATION)
                    except Exception as exc:  # noqa: BLE001 - details are optional
                        log.warning("TRACE_TX_LOOKUP_FAILED", tx=o.tx_hash, error=type(exc).__name__)
                    label = await self.labels.get(o.to_address)
                    terminal = None
                    if label and label[1] in ("exchange", "service"):
                        terminal = "possible exchange/service attribution - branch ends"
                    elif o.to_address in visited:
                        terminal = "address already in trace"
                    elif hop == self.s.trace_hops:
                        terminal = "max hops reached"
                    th = TraceHop(
                        hop=hop,
                        parent=addr,
                        transfer=o,
                        block_number=(details.block_number if details else None) or o.block_number,
                        confirmed=bool(details and details.confirmed) or o.confirmed,
                        label=label,
                        terminal=terminal,
                    )
                    result.hops.append(th)
                    if terminal is None:
                        visited.add(o.to_address)
                        nxt.append((o.to_address, o.amount, o.block_timestamp_ms, th))
            frontier = nxt
            if not frontier or nodes >= self.s.trace_max_nodes:
                break
        await self._store(ev, token, result)
        log.info("FUND_TRACE_COMPLETE", case=ev.case_id, run=run, hops=result.max_depth, transfers=len(result.hops))
        return result

    async def _store(self, ev: PoisoningEvent, token, result: TraceResult) -> None:
        now = self.clock.now()
        async with self.sf() as s, s.begin():
            for h in result.hops:
                t = h.transfer
                stmt = _insert(s, FundTrace).values(
                    event_id=ev.id,
                    trace_run=result.run,
                    hop=h.hop,
                    parent_address=h.parent,
                    from_address=t.from_address,
                    to_address=t.to_address,
                    amount=t.amount,
                    token_contract=t.token_contract,
                    token_symbol=token.symbol,
                    tx_hash=t.tx_hash,
                    transfer_key=t.transfer_key,
                    block_number=h.block_number,
                    block_timestamp=from_ms(t.block_timestamp_ms),
                    confirmation_status="CONFIRMED" if h.confirmed else "UNCONFIRMED",
                    to_label=h.label[0] if h.label else None,
                    to_label_category=h.label[1] if h.label else None,
                    to_label_source=h.label[2] if h.label else None,
                    terminal_reason=h.terminal,
                    created_at=now,
                )
                await s.execute(stmt.on_conflict_do_nothing(index_elements=["event_id", "trace_run", "transfer_key"]))
            row = await s.get(PoisoningEvent, ev.id)
            row.trace_status = "DONE"
            row.updated_at = now

    def forwarding_facts(self, ev: PoisoningEvent, result: TraceResult) -> tuple[list, str | None]:
        """Forwarding evidence derived from hop-1 transfers within FORWARD_WINDOW_MINUTES."""
        token = self.s.tokens_by_contract[ev.token_contract]
        t0 = to_ms(as_utc(ev.block_timestamp))
        window = self.s.forward_window_minutes * 60_000
        first_hop = [h.transfer for h in result.hops if h.hop == 1 and h.transfer.block_timestamp_ms - t0 <= window]
        if not first_hop:
            return [], None
        total = sum(t.amount for t in first_hop)
        pct = min(100, ratio_pct(total, ev.amount))
        delay = (min(t.block_timestamp_ms for t in first_hop) - t0) // 1000
        summary = (
            f"{format_amount(total, token.decimals)} {token.symbol} ({pct}% of the received amount) forwarded in "
            f"{len(first_hop)} transfer(s) within {self.s.forward_window_minutes} min; first after {delay} s"
        )
        fact = ("forwarding", "FORWARDING", "FACT", pct >= self.s.forward_min_ratio_pct, summary, {"pct": pct}, first_hop[0])
        return [fact], summary

    async def previous_signature(self, event_id: int, run: int) -> set[str]:
        async with self.sf() as s:
            rows = await s.execute(select(FundTrace.transfer_key).where(FundTrace.event_id == event_id, FundTrace.trace_run == run))
            return {r[0] for r in rows.all()}
