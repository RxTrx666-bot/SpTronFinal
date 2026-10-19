"""The bot must never be able to create, sign or broadcast a transaction."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from outflow_alert.tron_client import TronClient

SRC = Path(__file__).resolve().parents[1] / "outflow_alert"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["/wallet/broadcasttransaction", "/wallet/createtransaction", "/wallet/triggersmartcontract",
     "/wallet/gettransactionsign", "/wallet/easytransferbyprivate", "/wallet/freezebalancev2"],
)
async def test_client_refuses_write_endpoints(path):
    client = TronClient("https://api.trongrid.io", "k")
    with pytest.raises(PermissionError):
        await client._request("POST", path, json={})
    await client.close()


def test_no_signing_material_in_source():
    pattern = re.compile(r"private_?key|privkey|mnemonic|broadcasttransaction|broadcasthex|tronpy|ecdsa|secp256k1", re.I)
    for f in SRC.rglob("*.py"):
        for line in f.read_text().splitlines():
            assert not pattern.search(line), f"{f.name}: {line.strip()}"
