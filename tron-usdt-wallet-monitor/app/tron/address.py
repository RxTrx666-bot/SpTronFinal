"""TRON address helpers (Base58Check <-> hex).  Pure functions, no network."""

from __future__ import annotations

import hashlib

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(_ALPHABET)}
TRON_PREFIX = 0x41


class InvalidAddress(ValueError):
    pass


def _b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = []
    while n:
        n, rem = divmod(n, 58)
        out.append(_ALPHABET[rem])
    pad = len(data) - len(data.lstrip(b"\0"))
    return "1" * pad + "".join(reversed(out))


def _b58decode(text: str) -> bytes:
    n = 0
    for ch in text:
        if ch not in _INDEX:
            raise InvalidAddress(f"invalid base58 character {ch!r}")
        n = n * 58 + _INDEX[ch]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(text) - len(text.lstrip("1"))
    return b"\0" * pad + body


def _checksum(payload: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]


def hex_to_base58(value: str) -> str:
    """Convert ``0x``-prefixed 20-byte hex or ``41``-prefixed 21-byte hex to Base58Check."""
    h = value.lower()
    if h.startswith("0x"):
        h = h[2:]
    if len(h) == 40:
        h = "41" + h
    if len(h) != 42 or not h.startswith("41"):
        raise InvalidAddress(f"not a TRON hex address: {value!r}")
    try:
        payload = bytes.fromhex(h)
    except ValueError as exc:
        raise InvalidAddress(f"not hex: {value!r}") from exc
    return _b58encode(payload + _checksum(payload))


def base58_to_hex(address: str) -> str:
    raw = _b58decode(address)
    if len(raw) != 25:
        raise InvalidAddress(f"bad address length: {address!r}")
    payload, check = raw[:21], raw[21:]
    if _checksum(payload) != check:
        raise InvalidAddress(f"bad checksum: {address!r}")
    if payload[0] != TRON_PREFIX:
        raise InvalidAddress(f"not a TRON mainnet address: {address!r}")
    return payload.hex()


def is_valid_tron_address(address: str) -> bool:
    if not isinstance(address, str) or len(address) != 34 or not address.startswith("T"):
        return False
    try:
        base58_to_hex(address)
    except InvalidAddress:
        return False
    return True


def normalize_address(value: str) -> str:
    """Return the canonical Base58Check form of a TRON address given as base58 or hex."""
    if not isinstance(value, str) or not value:
        raise InvalidAddress(f"empty address: {value!r}")
    value = value.strip()
    if value.startswith("T"):
        if not is_valid_tron_address(value):
            raise InvalidAddress(f"invalid base58 TRON address: {value!r}")
        return value
    return hex_to_base58(value)


def address_from_seed(seed: str) -> str:
    """Deterministic valid-looking address for simulations/tests (never a real key)."""
    body = hashlib.sha256(seed.encode()).digest()[:20]
    return hex_to_base58("41" + body.hex())


def short(address: str, head: int = 6, tail: int = 4) -> str:
    if len(address) <= head + tail + 1:
        return address
    return f"{address[:head]}…{address[-tail:]}"
