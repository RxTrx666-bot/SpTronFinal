"""USDT TRC-20 ``Transfer`` event decoder.

Input: one raw event object as returned by TronGrid
``GET /v1/contracts/{contract}/events`` (also used by the simulator)::

    {
      "transaction_id": "a1b2...64 hex",
      "block_number": 70000000,
      "block_timestamp": 1758633332000,
      "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
      "event_index": 0,
      "event_name": "Transfer",
      "result": {"from": "0x...", "to": "0x...", "value": "5000000"},
      "_unconfirmed": true            # only present for unconfirmed events
    }

Anything that is not a USDT TRC-20 ``Transfer`` (TRX transfers, TRC-10 tokens,
other TRC-20 tokens, other events) is rejected.  Malformed payloads raise
``MalformedEvent`` and are skipped by the caller - they never crash the monitor.
"""

from __future__ import annotations

import re
from typing import Any

from app.collector.address import InvalidAddress, normalize_address
from app.domain import TransferEvent

_TX_RE = re.compile(r"^[0-9a-fA-F]{64}$")
# USDT total supply is far below 2**63 raw units; anything larger is not a
# real USDT transfer and would overflow BIGINT storage.
_MAX_RAW = 2**63 - 1


class EventRejected(Exception):
    """A well-formed event that is not a USDT TRC-20 Transfer (filtered out)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class MalformedEvent(Exception):
    """An event we could not decode."""


def _first(result: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in result and result[k] is not None:
            return result[k]
    return None


def _parse_int(value: Any, what: str) -> int:
    if isinstance(value, bool):
        raise MalformedEvent(f"{what} is boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        v = value.strip()
        try:
            return int(v, 16) if v.lower().startswith("0x") else int(v)
        except ValueError as exc:
            raise MalformedEvent(f"{what} not an integer: {value!r}") from exc
    raise MalformedEvent(f"{what} has unsupported type {type(value).__name__}")


def parse_transfer_event(raw: Any, usdt_contract: str, *, confirmed: bool) -> TransferEvent:
    """Decode and validate one raw event.

    ``confirmed`` is the status implied by the query that returned the event; an
    explicit ``_unconfirmed: true`` flag in the payload always wins.
    """
    if not isinstance(raw, dict):
        raise MalformedEvent("event is not an object")

    # --- token filter -----------------------------------------------------
    contract = raw.get("contract_address")
    if not contract:
        # e.g. a native TRX TransferContract, TRC-10 transfer or other payload
        raise EventRejected("no_contract_address")
    try:
        contract = normalize_address(str(contract))
    except InvalidAddress as exc:
        raise MalformedEvent(f"bad contract address: {contract!r}") from exc
    if contract != usdt_contract:
        raise EventRejected("not_usdt_contract")

    if raw.get("event_name") != "Transfer":
        raise EventRejected("not_transfer_event")

    result = raw.get("result")
    if not isinstance(result, dict):
        raise MalformedEvent("missing result")

    frm = _first(result, "from", "_from", "0")
    to = _first(result, "to", "_to", "1")
    value = _first(result, "value", "_value", "2")
    if frm is None or to is None or value is None:
        raise MalformedEvent("transfer result missing from/to/value")

    try:
        sender = normalize_address(str(frm))
        recipient = normalize_address(str(to))
    except InvalidAddress as exc:
        raise MalformedEvent(str(exc)) from exc

    amount_raw = _parse_int(value, "value")
    if amount_raw < 0 or amount_raw > _MAX_RAW:
        raise MalformedEvent(f"amount out of range: {amount_raw}")
    if amount_raw == 0:
        raise EventRejected("zero_amount")

    tx = raw.get("transaction_id")
    if not isinstance(tx, str) or not _TX_RE.match(tx):
        raise MalformedEvent(f"bad transaction_id: {tx!r}")

    ts = _parse_int(raw.get("block_timestamp"), "block_timestamp")
    if ts <= 0:
        raise MalformedEvent("bad block_timestamp")

    block = raw.get("block_number")
    block_number = _parse_int(block, "block_number") if block is not None else None
    event_index = _parse_int(raw.get("event_index", 0), "event_index")

    is_confirmed = confirmed and not bool(raw.get("_unconfirmed", False))

    return TransferEvent(
        transaction_hash=tx.lower(),
        event_index=event_index,
        block_number=block_number,
        block_timestamp_ms=ts,
        sender=sender,
        recipient=recipient,
        amount_raw=amount_raw,
        token_contract=contract,
        confirmed=is_confirmed,
    )


def parse_events(
    raws: list[Any], usdt_contract: str, *, confirmed: bool
) -> tuple[list[TransferEvent], dict[str, int]]:
    """Parse a batch, returning valid events and a counter of rejections."""
    out: list[TransferEvent] = []
    rejected: dict[str, int] = {}
    for raw in raws:
        try:
            out.append(parse_transfer_event(raw, usdt_contract, confirmed=confirmed))
        except EventRejected as exc:
            rejected[exc.reason] = rejected.get(exc.reason, 0) + 1
        except MalformedEvent:
            rejected["malformed"] = rejected.get("malformed", 0) + 1
        except Exception:  # never let one bad payload kill the batch
            rejected["malformed"] = rejected.get("malformed", 0) + 1
    return out, rejected
