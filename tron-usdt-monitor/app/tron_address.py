"""TRON address helpers (Base58Check <-> hex), implemented with the stdlib only.

A TRON address is 21 bytes: the 0x41 mainnet prefix followed by the 20-byte
account id. Its Base58Check form (starting with "T") appends a 4-byte
double-SHA256 checksum.
"""

from __future__ import annotations

import hashlib

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(_ALPHABET)}
ADDRESS_PREFIX = b"\x41"


class InvalidAddressError(ValueError):
    """Raised when a value is not a valid TRON address."""


def _checksum(payload: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = []
    while n:
        n, rem = divmod(n, 58)
        out.append(_ALPHABET[rem])
    pad = len(data) - len(data.lstrip(b"\x00"))
    return "1" * pad + "".join(reversed(out))


def b58decode(value: str) -> bytes:
    n = 0
    for char in value:
        if char not in _INDEX:
            raise InvalidAddressError(f"invalid base58 character {char!r}")
        n = n * 58 + _INDEX[char]
    pad = len(value) - len(value.lstrip("1"))
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * pad + body


def base58_to_hex(address: str) -> str:
    """Return the 42-char lowercase hex form ("41" + 40 hex) of a T-address."""
    if not isinstance(address, str) or not address:
        raise InvalidAddressError("address must be a non-empty string")
    raw = b58decode(address.strip())
    if len(raw) != 25:
        raise InvalidAddressError("decoded address must be 25 bytes")
    payload, checksum = raw[:21], raw[21:]
    if _checksum(payload) != checksum:
        raise InvalidAddressError("address checksum mismatch")
    if payload[:1] != ADDRESS_PREFIX:
        raise InvalidAddressError("address is not a TRON mainnet address (prefix 0x41)")
    return payload.hex()


def hex_to_base58(value: str) -> str:
    """Convert a hex TRON address to Base58Check.

    Accepts "41"+40 hex, "0x"+40 hex, bare 40 hex (EVM-style, as used in event
    logs) or a 64-hex ABI-encoded topic word (last 20 bytes are the address).
    """
    if not isinstance(value, str):
        raise InvalidAddressError("hex address must be a string")
    h = value.strip().lower()
    if h.startswith("0x"):
        h = h[2:]
    if len(h) == 64:
        if h[:24].strip("0"):
            raise InvalidAddressError("topic word has non-zero high bytes")
        h = h[24:]
    if len(h) == 42 and h.startswith("41"):
        h = h[2:]
    if len(h) != 40:
        raise InvalidAddressError(f"unexpected hex address length {len(h)}")
    try:
        body = bytes.fromhex(h)
    except ValueError as exc:
        raise InvalidAddressError("invalid hex address") from exc
    payload = ADDRESS_PREFIX + body
    return b58encode(payload + _checksum(payload))


def is_valid_address(address: str) -> bool:
    try:
        base58_to_hex(address)
    except InvalidAddressError:
        return False
    return True
