"""Minimal ABI decoding for constant-call results and the ERC/TRC-20 Transfer event."""

from __future__ import annotations

from dataclasses import dataclass

from outflow_alert.address import InvalidAddress, hex_to_base58, topic_to_address

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class AbiError(ValueError):
    pass


def _clean(hex_str: str) -> str:
    h = (hex_str or "").strip().lower()
    if h.startswith("0x"):
        h = h[2:]
    if len(h) % 2:
        raise AbiError("odd-length hex")
    try:
        bytes.fromhex(h)
    except ValueError as exc:
        raise AbiError("not hex") from exc
    return h


def decode_uint256(hex_str: str) -> int:
    h = _clean(hex_str)
    if len(h) != 64:
        raise AbiError(f"uint256 must be 32 bytes, got {len(h) // 2}")
    return int(h, 16)


def decode_string(hex_str: str) -> str:
    """Decode an ABI ``string`` return value (also tolerates legacy ``bytes32`` symbols)."""
    h = _clean(hex_str)
    if len(h) == 64:  # bytes32-style return
        return bytes.fromhex(h).rstrip(b"\0").decode("utf-8", errors="strict")
    if len(h) < 128:
        raise AbiError("string result too short")
    offset = int(h[:64], 16) * 2
    if offset + 64 > len(h):
        raise AbiError("string offset out of range")
    length = int(h[offset : offset + 64], 16) * 2
    start = offset + 64
    if start + length > len(h):
        raise AbiError("string length out of range")
    try:
        return bytes.fromhex(h[start : start + length]).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AbiError("string is not utf-8") from exc


@dataclass(frozen=True)
class TransferLog:
    log_index: int
    contract: str  # Base58Check address of the emitting contract
    from_address: str
    to_address: str
    amount_raw: int


def decode_transfer_log(log: dict, log_index: int) -> TransferLog | None:
    """Decode a ``gettransactioninfobyid`` log entry; ``None`` if it is not a Transfer event."""
    topics = log.get("topics") or []
    if len(topics) != 3 or _clean(topics[0]) != TRANSFER_TOPIC:
        return None
    try:
        contract = hex_to_base58(str(log.get("address", "")))
        from_address = topic_to_address(topics[1])
        to_address = topic_to_address(topics[2])
        amount = decode_uint256(log.get("data", ""))
    except (InvalidAddress, AbiError):
        return None
    return TransferLog(log_index, contract, from_address, to_address, amount)
