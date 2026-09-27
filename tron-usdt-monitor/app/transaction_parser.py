"""Parsing of TRON API payloads into validated ``TokenTransfer`` objects.

Two sources are supported:

* ``parse_transfer_logs`` decodes the raw TRC-20 ``Transfer(address,address,uint256)``
  event logs contained in a full-node ``TransactionInfo`` (``/wallet/gettransactioninfobyid``
  or ``/wallet/gettransactioninfobyblocknum``). This is the authoritative on-chain data.
* ``parse_trongrid_trc20_record`` parses a record of TronGrid's indexed
  ``/v1/accounts/{address}/transactions/trc20`` endpoint.

Anything that does not look exactly like the expected structure raises
``MalformedTransactionError`` (or is skipped with a warning when scanning a batch),
so malformed data can never be accepted as a match.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import Any, Mapping

from app.tron_address import InvalidAddressError, base58_to_hex, hex_to_base58

log = logging.getLogger(__name__)

# keccak256("Transfer(address,address,uint256)")
TRANSFER_EVENT_TOPIC = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

_TX_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]*$")
_MAX_UINT256 = 2**256 - 1


class MalformedTransactionError(ValueError):
    """Raised when API data does not have the expected structure."""


@dataclass(frozen=True)
class TokenTransfer:
    tx_hash: str
    block_timestamp_ms: int
    contract_address: str
    sender: str
    recipient: str
    amount_raw: int
    block_number: int | None = None
    token_symbol: str | None = None
    token_decimals: int | None = None
    event_type: str = "Transfer"
    log_index: int | None = None
    source: str = "unknown"  # "event_log" | "trongrid_index"

    def with_metadata(self, **changes: Any) -> "TokenTransfer":
        return replace(self, **changes)


def normalize_tx_hash(value: Any) -> str:
    if not isinstance(value, str):
        raise MalformedTransactionError("transaction id missing or not a string")
    h = value.strip().lower()
    if h.startswith("0x"):
        h = h[2:]
    if not _TX_HASH_RE.match(h):
        raise MalformedTransactionError(f"invalid transaction id {value!r}")
    return h


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise MalformedTransactionError(f"{field} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise MalformedTransactionError(f"{field} must be an integer") from exc
    if number <= 0:
        raise MalformedTransactionError(f"{field} must be positive")
    return number


def _base58(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise MalformedTransactionError(f"{field} missing")
    try:
        base58_to_hex(value)
    except InvalidAddressError as exc:
        raise MalformedTransactionError(f"{field} is not a valid TRON address: {exc}") from exc
    return value


def _log_address_to_base58(value: Any) -> str:
    if not isinstance(value, str):
        raise MalformedTransactionError("log address missing")
    try:
        return hex_to_base58(value)
    except InvalidAddressError as exc:
        raise MalformedTransactionError(f"invalid log address: {exc}") from exc


def parse_trongrid_trc20_record(record: Mapping[str, Any]) -> TokenTransfer:
    """Parse one item of TronGrid ``/v1/accounts/{addr}/transactions/trc20``."""
    if not isinstance(record, Mapping):
        raise MalformedTransactionError("record is not an object")
    tx_hash = normalize_tx_hash(record.get("transaction_id"))
    token_info = record.get("token_info")
    if not isinstance(token_info, Mapping):
        raise MalformedTransactionError("token_info missing")
    contract = _base58(token_info.get("address"), "token_info.address")
    sender = _base58(record.get("from"), "from")
    recipient = _base58(record.get("to"), "to")
    value = record.get("value")
    if not isinstance(value, str) or not value.isdigit():
        raise MalformedTransactionError(f"value must be a decimal integer string, got {value!r}")
    amount = int(value)
    if amount > _MAX_UINT256:
        raise MalformedTransactionError("value exceeds uint256")
    decimals = token_info.get("decimals")
    if decimals is not None:
        try:
            decimals = int(decimals)
        except (TypeError, ValueError) as exc:
            raise MalformedTransactionError("token_info.decimals invalid") from exc
    symbol = token_info.get("symbol")
    event_type = record.get("type")
    if not isinstance(event_type, str):
        raise MalformedTransactionError("type missing")
    return TokenTransfer(
        tx_hash=tx_hash,
        block_timestamp_ms=_positive_int(record.get("block_timestamp"), "block_timestamp"),
        contract_address=contract,
        sender=sender,
        recipient=recipient,
        amount_raw=amount,
        token_symbol=symbol if isinstance(symbol, str) else None,
        token_decimals=decimals,
        event_type=event_type,
        source="trongrid_index",
    )


def tx_info_succeeded(tx_info: Mapping[str, Any]) -> bool:
    """False for reverted / failed transactions (their logs must never be trusted)."""
    if tx_info.get("result") == "FAILED":
        return False
    receipt = tx_info.get("receipt")
    if isinstance(receipt, Mapping):
        result = receipt.get("result")
        if result is not None and result != "SUCCESS":
            return False
    return True


def decode_transfer_log(
    log_entry: Mapping[str, Any],
    *,
    tx_hash: str,
    block_number: int | None,
    block_timestamp_ms: int,
    log_index: int,
) -> TokenTransfer | None:
    """Decode one event log. Returns None when it is not a TRC-20 Transfer event."""
    if not isinstance(log_entry, Mapping):
        raise MalformedTransactionError("log entry is not an object")
    topics = log_entry.get("topics")
    if not isinstance(topics, list) or not topics:
        return None
    topic0 = str(topics[0]).lower().removeprefix("0x")
    if topic0 != TRANSFER_EVENT_TOPIC:
        return None
    # TRC-20 Transfer has exactly 3 topics (sig, from, to) + 32-byte data (value).
    # TRC-721 Transfer has 4 topics (tokenId indexed) and is ignored.
    if len(topics) != 3:
        return None
    data = log_entry.get("data")
    if not isinstance(data, str):
        raise MalformedTransactionError("Transfer log data missing")
    data = data.removeprefix("0x")
    if len(data) != 64 or not _HEX_RE.match(data):
        raise MalformedTransactionError(f"Transfer log data must be 32 bytes, got {len(data) // 2}")
    for topic in topics[1:]:
        if not isinstance(topic, str) or not _HEX_RE.match(topic.removeprefix("0x")):
            raise MalformedTransactionError("invalid topic")
    try:
        sender = hex_to_base58(topics[1])
        recipient = hex_to_base58(topics[2])
    except InvalidAddressError as exc:
        raise MalformedTransactionError(f"invalid address topic: {exc}") from exc
    return TokenTransfer(
        tx_hash=tx_hash,
        block_number=block_number,
        block_timestamp_ms=block_timestamp_ms,
        contract_address=_log_address_to_base58(log_entry.get("address")),
        sender=sender,
        recipient=recipient,
        amount_raw=int(data, 16),
        event_type="Transfer",
        log_index=log_index,
        source="event_log",
    )


def _log_matches_prefilter(log_entry: Any, contract_hex40: str | None, wallet_hex40: str | None) -> bool:
    """Cheap string checks so block scans only fully decode relevant logs."""
    if not isinstance(log_entry, Mapping):
        return True  # let the decoder report it
    if contract_hex40 is not None:
        address = str(log_entry.get("address", "")).lower().removeprefix("0x")
        if address[-40:] != contract_hex40 or len(address) not in (40, 42):
            return False
    if wallet_hex40 is not None:
        topics = log_entry.get("topics")
        if not isinstance(topics, list) or len(topics) < 3:
            return False
        if not any(str(t).lower().endswith(wallet_hex40) for t in topics[1:3]):
            return False
    return True


def parse_transfer_logs(
    tx_info: Mapping[str, Any],
    *,
    contract: str | None = None,
    wallet: str | None = None,
) -> list[TokenTransfer]:
    """Decode all TRC-20 Transfer events of a ``TransactionInfo`` object.

    ``contract`` / ``wallet`` (Base58) optionally pre-filter logs by emitting
    contract and by address topics. Failed transactions yield no transfers.
    Raises MalformedTransactionError if the transaction envelope itself is
    malformed; individual malformed logs are skipped with a warning.
    """
    if not isinstance(tx_info, Mapping):
        raise MalformedTransactionError("transaction info is not an object")
    tx_hash = normalize_tx_hash(tx_info.get("id"))
    logs = tx_info.get("log") or []
    if not isinstance(logs, list):
        raise MalformedTransactionError("log field is not a list")
    if not logs or not tx_info_succeeded(tx_info):
        return []
    block_ts = _positive_int(tx_info.get("blockTimeStamp"), "blockTimeStamp")
    block_number_raw = tx_info.get("blockNumber")
    block_number = int(block_number_raw) if isinstance(block_number_raw, int) else None
    contract_hex40 = base58_to_hex(contract)[2:] if contract else None
    wallet_hex40 = base58_to_hex(wallet)[2:] if wallet else None

    transfers: list[TokenTransfer] = []
    for index, entry in enumerate(logs):
        if not _log_matches_prefilter(entry, contract_hex40, wallet_hex40):
            continue
        try:
            transfer = decode_transfer_log(
                entry,
                tx_hash=tx_hash,
                block_number=block_number,
                block_timestamp_ms=block_ts,
                log_index=index,
            )
        except MalformedTransactionError as exc:
            log.warning(
                "malformed_log_skipped",
                extra={"ctx": {"tx_hash": tx_hash, "log_index": index, "error": str(exc)}},
            )
            continue
        if transfer is not None:
            transfers.append(transfer)
    return transfers
