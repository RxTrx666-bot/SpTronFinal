"""33. Security: the code base is read-only towards the blockchain and holds no secrets."""

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
SOURCES = {p: p.read_text() for p in APP.rglob("*.py")}

FORBIDDEN = [
    r"broadcasttransaction",
    r"createtransaction",
    r"triggersmartcontract",  # state-changing contract call (we only use triggerCONSTANTcontract)
    r"private[_ ]?key",
    r"seed[_ ]?phrase",
    r"mnemonic",
    r"\bsign(_transaction|Transaction)?\(",
    r"tronpy|tronweb",
]


def test_no_transaction_signing_or_key_handling():
    for path, src in SOURCES.items():
        for pat in FORBIDDEN:
            assert not re.search(pat, src, re.IGNORECASE), f"{pat!r} found in {path}"


def test_only_read_endpoints_are_called():
    endpoints = set()
    for src in SOURCES.values():
        endpoints |= set(re.findall(r'"(/(?:wallet|walletsolidity|v1)/[^"{]*)', src))
    assert endpoints <= {"/wallet/triggerconstantcontract", "/v1/contracts/"}, endpoints


def test_no_hardcoded_secrets():
    for path, src in SOURCES.items():
        assert not re.search(r"\d{8,10}:[A-Za-z0-9_-]{35}", src), f"telegram token in {path}"
        assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", src), f"api key in {path}"
