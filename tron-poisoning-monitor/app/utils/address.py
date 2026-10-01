"""TRON address handling (Base58Check <-> hex).  Pure functions, no network.

A TRON mainnet address is 21 bytes: the network byte ``0x41`` followed by a
20-byte account id.  It is shown to users as Base58Check (34 characters,
always starting with ``T``) and returned by node APIs as hex
(``41`` + 40 hex chars, or occasionally ``0x`` + 40 hex chars, or a 32-byte
ABI-padded topic).

All comparisons in this application use the canonical Base58Check form
produced by :func:`normalize_address`, so ``41ab..``/``0xab..``/``Tx..`` forms
of the same account always compare equal and malformed strings can never
"match" anything.  Stored addresses are always canonical; we never rewrite a
user-supplied address into a different account.
"""

from __future__ import annotations

import hashlib

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(ALPHABET)}
TRON_PREFIX = 0x41


class InvalidAddress(ValueError):
    pass


def _b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = []
    while n:
        n, rem = divmod(n, 58)
        out.append(ALPHABET[rem])
    pad = len(data) - len(data.lstrip(b"\0"))
    return "1" * pad + "".join(reversed(out))


def _b58decode(text: str) -> bytes:
    n = 0
    for ch in text:
        idx = _INDEX.get(ch)
        if idx is None:
            raise InvalidAddress(f"invalid base58 character {ch!r}")
        n = n * 58 + idx
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(text) - len(text.lstrip("1"))
    return b"\0" * pad + body


def _checksum(payload: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]


def hex_to_base58(value: str) -> str:
    """Convert a hex TRON address (``41``+40, ``0x``+40, bare 40 or 64-char ABI topic) to Base58Check."""
    h = value.strip().lower()
    if h.startswith("0x"):
        h = h[2:]
    if len(h) == 64:  # ABI-encoded (topic / log data): last 20 bytes
        if h[:24].strip("0"):
            raise InvalidAddress(f"not an address topic: {value!r}")
        h = h[24:]
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
    """Return the ``41``-prefixed lowercase hex form of a Base58Check address."""
    raw = _b58decode(address)
    if len(raw) != 25:
        raise InvalidAddress(f"bad address length: {address!r}")
    payload, check = raw[:21], raw[21:]
    if _checksum(payload) != check:
        raise InvalidAddress(f"bad checksum: {address!r}")
    if payload[0] != TRON_PREFIX:
        raise InvalidAddress(f"not a TRON mainnet address: {address!r}")
    return payload.hex()


def is_valid_tron_address(address: object) -> bool:
    if not isinstance(address, str) or len(address) != 34 or not address.startswith("T"):
        return False
    try:
        base58_to_hex(address)
    except InvalidAddress:
        return False
    return True


def normalize_address(value: object) -> str:
    """Return the canonical Base58Check form of a TRON address given as Base58 or hex.

    Raises :class:`InvalidAddress` for anything that is not a valid mainnet address.
    Base58 is case-sensitive, so the case of a Base58 input is never changed.
    """
    if not isinstance(value, str):
        raise InvalidAddress(f"address must be a string: {value!r}")
    v = value.strip()
    if not v:
        raise InvalidAddress("empty address")
    if v.startswith("T"):
        if not is_valid_tron_address(v):
            raise InvalidAddress(f"invalid TRON address: {value!r}")
        return v
    return hex_to_base58(v)


def try_normalize(value: object) -> str | None:
    try:
        return normalize_address(value)
    except InvalidAddress:
        return None


def address_from_seed(seed: str) -> str:
    """Deterministic, checksum-valid address for simulations/tests (no key exists for it)."""
    body = hashlib.sha256(seed.encode()).digest()[:20]
    return hex_to_base58("41" + body.hex())


def short(address: str, head: int = 6, tail: int = 4) -> str:
    if not address or len(address) <= head + tail + 1:
        return address or ""
    return f"{address[:head]}…{address[-tail:]}"


def tronscan_tx_url(tx_hash: str) -> str:
    h = tx_hash.strip().lower()
    if h.startswith("0x"):
        h = h[2:]
    if len(h) != 64 or any(c not in "0123456789abcdef" for c in h):
        raise ValueError(f"invalid transaction hash: {tx_hash!r}")
    return f"https://tronscan.org/#/transaction/{h}"


def tronscan_address_url(address: str) -> str:
    return f"https://tronscan.org/#/address/{normalize_address(address)}"


def is_tx_hash(value: str) -> bool:
    h = value.strip().lower()
    return len(h) == 64 and all(c in "0123456789abcdef" for c in h)
