"""Turn raw TronGrid payloads into validated ``Transfer`` objects.

Accepted inputs
---------------
* TronGrid event objects (``/v1/contracts/{c}/events`` and
  ``/v1/transactions/{tx}/events``)::

    {"transaction_id": "<64 hex>", "block_number": 70000000,
     "block_timestamp": 1758633332000, "contract_address": "TR7N...",
     "event_index": 0, "event_name": "Transfer",
     "result": {"from": "0x..", "to": "0x..", "value": "5000000"},
     "_unconfirmed": true}            # only on unconfirmed events

* Transaction-info logs (``/wallet/gettransactioninfobyid``), used as a
  fallback when the event index is not available - see ``parse_tx_info_logs``.

Anything that is not a USDT TRC-20 ``Transfer`` (native TRX, TRC-10, other
TRC-20 tokens, other events, zero-value transfers) is rejected.  Malformed
payloads are counted and skipped; they never crash the monitor.
"""

from __future__ import annotations

import re
from typing import Any

from app.domain import Transfer
from app.tron.address import InvalidAddress, hex_to_base58, normalize_address

_TX_RE = re.compile(r"^[0-9a-fA-F]{64}$")
# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
# Real USDT amounts are far below 2**63 base units (BIGINT).  Anything larger is
# not a genuine USDT transfer.
MAX_BASE_UNITS = 2**63 - 1


class Rejected(Exception):
    """Well-formed, but not a USDT TRC-20 transfer we care about."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Malformed(Exception):
    """Payload we could not decode."""


def _int(value: Any, what: str) -> int:
    if isinstance(value, bool) or value is None:
        raise Malformed(f"{what} missing/invalid")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        v = value.strip()
        try:
            return int(v, 16) if v.lower().startswith("0x") else int(v)
        except ValueError as exc:
            raise Malformed(f"{what} not an integer: {value!r}") from exc
    raise Malformed(f"{what} has unsupported type {type(value).__name__}")


def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return None


def _addr(value: Any, what: str) -> str:
    try:
        return normalize_address(str(value))
    except InvalidAddress as exc:
        raise Malformed(f"bad {what}: {exc}") from exc


def _tx(value: Any) -> str:
    if not isinstance(value, str) or not _TX_RE.match(value):
        raise Malformed(f"bad transaction id: {value!r}")
    return value.lower()


def _amount(value: Any) -> int:
    amount = _int(value, "value")
    if amount < 0 or amount > MAX_BASE_UNITS:
        raise Malformed(f"amount out of range: {amount}")
    if amount == 0:
        raise Rejected("zero_amount")
    return amount


def parse_event(raw: Any, usdt_contract: str) -> Transfer:
    if not isinstance(raw, dict):
        raise Malformed("event is not an object")
    contract = raw.get("contract_address")
    if not contract:
        # native TRX TransferContract, TRC-10 TransferAssetContract, ...
        raise Rejected("not_a_contract_event")
    if _addr(contract, "contract") != usdt_contract:
        raise Rejected("not_usdt")
    if raw.get("event_name") != "Transfer":
        raise Rejected("not_transfer_event")
    result = raw.get("result")
    if not isinstance(result, dict):
        raise Malformed("missing result")
    frm, to, value = _first(result, "from", "_from", "0"), _first(result, "to", "_to", "1"), _first(result, "value", "_value", "2")
    if frm is None or to is None or value is None:
        raise Malformed("transfer missing from/to/value")
    ts = _int(raw.get("block_timestamp"), "block_timestamp")
    if ts <= 0:
        raise Malformed("bad block_timestamp")
    block = raw.get("block_number")
    return Transfer(
        tx_hash=_tx(raw.get("transaction_id")),
        event_index=_int(raw.get("event_index"), "event_index"),
        block_number=_int(block, "block_number") if block is not None else None,
        timestamp_ms=ts,
        from_address=_addr(frm, "from"),
        to_address=_addr(to, "to"),
        amount_base_units=_amount(value),
        contract_address=usdt_contract,
        # TronGrid flags not-yet-solidified events with "_unconfirmed": true.
        confirmed=not bool(raw.get("_unconfirmed", False)),
    )


def parse_events(raws: list[Any], usdt_contract: str) -> tuple[list[Transfer], dict[str, int]]:
    """Parse a batch; return valid transfers and a counter of rejection reasons."""
    out: list[Transfer] = []
    rejected: dict[str, int] = {}
    for raw in raws:
        try:
            out.append(parse_event(raw, usdt_contract))
        except Rejected as exc:
            rejected[exc.reason] = rejected.get(exc.reason, 0) + 1
        except Exception:  # noqa: BLE001 - one bad payload never kills a batch
            rejected["malformed"] = rejected.get("malformed", 0) + 1
    return out, rejected


def parse_tx_info_logs(info: dict[str, Any], usdt_contract: str) -> list[Transfer]:
    """Decode USDT ``Transfer`` logs from ``/wallet/gettransactioninfobyid``.

    The log position within the transaction is the event index.
    """
    if not isinstance(info, dict) or not info.get("id"):
        return []
    tx = _tx(info["id"])
    ts = _int(info.get("blockTimeStamp"), "blockTimeStamp")
    block = _int(info.get("blockNumber"), "blockNumber") if info.get("blockNumber") is not None else None
    out: list[Transfer] = []
    for idx, log in enumerate(info.get("log") or []):
        try:
            if hex_to_base58(str(log.get("address", ""))) != usdt_contract:
                continue
            topics = log.get("topics") or []
            if len(topics) < 3 or str(topics[0]).lower() != TRANSFER_TOPIC:
                continue
            out.append(
                Transfer(
                    tx_hash=tx,
                    event_index=idx,
                    block_number=block,
                    timestamp_ms=ts,
                    from_address=hex_to_base58(str(topics[1])[-40:]),
                    to_address=hex_to_base58(str(topics[2])[-40:]),
                    amount_base_units=_amount("0x" + (log.get("data") or "0")),
                    contract_address=usdt_contract,
                    confirmed=True,
                )
            )
        except (Rejected, Malformed, InvalidAddress, ValueError):
            continue
    return out
