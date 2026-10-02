"""Explain why a given transaction was (or was not) detected as address poisoning.

Usage (on the server):
    docker compose exec monitor python -m app.diagnose <TX_HASH>

Read-only: it only queries the TRON API and the bot's database.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import func, select

from app.config import Settings
from app.database import create_engine, create_session_factory
from app.models import HistoricalRecipient, NetworkContact, PoisoningEvent
from app.services.similarity import SimilarityConfig, SimilarityEngine
from app.services.tron_service import TronGridClient, parse_block_transfers
from app.utils.clock import from_ms


async def diagnose(tx_hash: str) -> None:
    s = Settings()
    c = TronGridClient(s)
    eng = SimilarityEngine(SimilarityConfig.from_settings(s))
    e = create_engine(s.database_url)
    sf = create_session_factory(e)
    tok = s.primary_token.contract
    try:
        tx = await c._request("POST", "/wallet/gettransactionbyid", json={"value": tx_hash})
        info = await c._request("POST", "/wallet/gettransactioninfobyid", json={"value": tx_hash})
        if not tx and not info:
            print("RESULT: transaction not found. Check the hash.")
            return
        block = {"block_header": {"raw_data": {"timestamp": info.get("blockTimeStamp", 0)}}, "transactions": [tx] if tx else []}
        blk = parse_block_transfers(info.get("blockNumber", 0), block, [info], {tok: 6})
        if not blk.transfers:
            print(f"RESULT: no transfer of the real USDT contract ({tok}) in this transaction.")
            print("        It is another token - often a FAKE 'USDT'. The bot only analyses real USDT.")
            return

        async with sf() as db:
            mem_start = (await db.execute(select(func.min(HistoricalRecipient.first_seen)))).scalar()
            events = (await db.execute(select(PoisoningEvent).where(PoisoningEvent.tx_hash == tx_hash))).scalars().all()
        print(f"Bot memory starts at: {mem_start}")
        for ev in events:
            print(f"Bot incident for this tx: {ev.case_id} {ev.event_type} confidence {ev.confidence}/100")

        for t in blk.transfers:
            victim, fake = t.from_address, t.to_address
            print(f"\nTRANSFER {victim} -> {fake}")
            print(f"  amount {t.amount / 1e6:,.6f} USDT at {from_ms(t.block_timestamp_ms)} (block {t.block_number})")
            if t.amount < s.units("network_min_alert_usdt"):
                print(f"  NOTE: below NETWORK_MIN_ALERT_USDT ({s.network_min_alert_usdt} USDT) - recorded but not alerted")

            hist, fp = [], None
            for _ in range(5):
                page, fp = await c.get_trc20_transfers(victim, tok, max_timestamp_ms=t.block_timestamp_ms - 1, order="desc", fingerprint=fp)
                hist += page
                if not fp or not page:
                    break
            paid: dict[str, list] = {}
            for h in hist:
                if h.from_address == victim and h.to_address != fake and h.amount > 0:
                    paid.setdefault(h.to_address, []).append(h)
            print(f"  Victim paid {len(paid)} different addresses in its previous {len(hist)} USDT transfers.")
            print("  Most similar to the fake:")
            for r in sorted((eng.compare(a, fake) for a in paid), key=lambda r: -r.similarity_score)[:3]:
                last = max(h.block_timestamp_ms for h in paid[r.legitimate])
                async with sf() as db:
                    mem = (
                        await db.execute(
                            select(HistoricalRecipient).where(HistoricalRecipient.victim_wallet == victim, HistoricalRecipient.recipient_wallet == r.legitimate)
                        )
                    ).scalar_one_or_none()
                print(f"    {r.legitimate}  paid {len(paid[r.legitimate])}x, last {from_ms(last)}")
                print(
                    f"      match: first {r.prefix_match_length} + last {r.suffix_match_length} chars after T, "
                    f"score {r.similarity_pct}%, rule={r.edge_rule}, counts as look-alike={r.is_match}"
                )
                print(f"      in bot memory: {'YES (' + str(mem.transaction_count) + 'x)' if mem else 'NO'}")
            async with sf() as db:
                contact = await db.get(NetworkContact, (fake, victim))
            if contact:
                print(f"  Bot saw the fake touch the victim's history: {contact.kind} at {contact.first_seen} (tx {contact.tx_hash})")
            else:
                print("  Bot has NOT recorded a fake-token / TRX / dust contact from the fake to the victim (or it was before the bot started)")
            dust = [h for h in hist if h.from_address == fake and h.to_address == victim]
            first = f", first {from_ms(min(h.block_timestamp_ms for h in dust))}" if dust else ""
            print(f"  Dust from fake to victim (real USDT): {len(dust)}{first}")
    finally:
        await c.close()
        await e.dispose()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__)
        return 2
    raw = argv[0].strip().lower().rstrip("/")
    tx = raw.rsplit("/", 1)[-1].removeprefix("0x")  # accepts a pasted Tronscan link too
    if len(tx) != 64 or any(ch not in "0123456789abcdef" for ch in tx):
        print(f"'{argv[0]}' is not a transaction hash.")
        print("Use the 64-character hash of the victim's payment to the fake address (or its Tronscan link), e.g.:")
        print("  python -m app.diagnose 4f2a9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7e81b")
        return 2
    asyncio.run(diagnose(tx))
    return 0


if __name__ == "__main__":
    sys.exit(main())
