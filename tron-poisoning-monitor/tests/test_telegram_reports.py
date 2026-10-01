"""Telegram commands/authorization, buttons, reports, X integration, read-only guarantees, schema."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx
import pytest
from sqlalchemy import inspect

from app.domain import EventType
from app.models import Base, TelegramUser, WatchedWallet, XPostDraft
from app.services import report_service as rs
from app.services.x_service import XError, XService, oauth1_header, validate_post
from app.simulation import addresses as A
from tests.conftest import ADMIN, USDT, make_settings

SUCCESS = EventType.SUCCESSFUL_POISONING_EVENT.value
OUTSIDER = 999


def msg(text: str, user: int = ADMIN, chat: int | None = None) -> dict:
    return {"update_id": 1, "message": {"text": text, "chat": {"id": chat or user}, "from": {"id": user, "username": f"u{user}"}}}


def cb(data: str, user: int = ADMIN) -> dict:
    return {"callback_query": {"id": "c1", "from": {"id": user}, "message": {"chat": {"id": user}}, "data": data}}


async def test_unauthorized_users_are_rejected(make_app):
    h = await make_app()
    await h.app.bot.handle_update(msg(f"/add {A.VICTIM}", user=OUTSIDER))
    assert await h.app.admin.list_wallets() == []
    assert "not authorized" in h.t.messages[-1]["text"]
    await h.app.bot.handle_update(msg("/status", user=OUTSIDER))  # rate-limited notice: no second reply
    assert len(h.t.messages) == 1
    await h.app.bot.handle_update(cb("report:1", user=OUTSIDER))
    assert h.t.callbacks[-1][1] == "Not authorized" and h.t.documents == []
    async with h.app.sf() as s:
        u = (await s.execute(TelegramUser.__table__.select().where(TelegramUser.user_id == OUTSIDER))).first()
    assert u.unauthorized_attempts == 3 and not u.is_admin


async def test_admin_wallet_commands(make_app):
    h = await make_app()
    bot = h.app.bot
    assert "Invalid TRON address" in await bot.command("/add TNotAnAddress")
    assert "Usage" in await bot.command("/add")
    assert "Now monitoring" in await bot.command(f"/add {A.VICTIM} Treasury <b>wallet</b>", user_id=ADMIN)
    assert "Already monitored" in await bot.command(f"/add {A.VICTIM}")
    # hex input is accepted and normalized to the canonical Base58 address
    from app.utils.address import base58_to_hex

    assert "Now monitoring" in await bot.command(f"/add@MyBot {base58_to_hex(A.VICTIM_2)}")
    wallets = await h.app.admin.list_wallets()
    assert [w.address for w in wallets] == [A.VICTIM, A.VICTIM_2]
    assert wallets[0].label == "Treasury bwallet/b"  # markup stripped
    listing = await bot.command("/list")
    assert A.VICTIM in listing and A.VICTIM_2 in listing
    assert "paused" in (await bot.command(f"/pause {A.VICTIM}")).lower()
    assert h.app.registry.wallets[A.VICTIM] == "PAUSED"
    assert "resumed" in (await bot.command(f"/resume {A.VICTIM}")).lower()
    assert "paused" in (await bot.command("/pause")).lower() and h.app.admin.alerts_paused
    assert "resumed" in (await bot.command("/resume")).lower() and not h.app.admin.alerts_paused
    assert "STATUS" in await bot.command("/status")
    assert "/add" in await bot.command("/help") and "/add" in await bot.command("/start")
    assert "Stopped monitoring" in await bot.command(f"/remove {A.VICTIM_2}")
    assert not h.app.registry.is_monitored(A.VICTIM_2)
    assert "not monitored" in await bot.command(f"/remove {A.VICTIM_2}")
    assert "Unknown command" in await bot.command("/rm -rf")


async def _incident(make_app, **kw):
    h = await make_app(**kw)
    h.history(payments=8)
    await h.add()
    await h.go_live()
    await h.send_live(A.VICTIM, A.POISON, 25_000 * USDT)
    await h.settle()
    [ev] = await h.events(event_type=SUCCESS)
    return h, ev


async def test_buttons_copy_report_trace_and_x_draft(make_app, tmp_path):
    h, ev = await _incident(make_app, output_dir=str(tmp_path))
    await h.app.bot.handle_update(cb(f"copy:{ev.id}"))
    assert "CASE ID:" in h.t.messages[-1]["text"] and ev.case_id in h.t.messages[-1]["text"]
    await h.app.bot.handle_update(cb(f"report:{ev.id}"))
    names = [d["filename"] for d in h.t.documents]
    assert names == [f"{ev.case_id}-report.md", f"{ev.case_id}.json"]
    data = json.loads(h.t.documents[1]["content"])
    for key in (
        "case_id",
        "network",
        "token",
        "victim",
        "legitimate_recipient",
        "suspicious_recipient",
        "amount",
        "transaction_hash",
        "confidence",
        "evidence",
        "fund_trace",
    ):
        assert key in data
    assert data["amount"] == "25000" and data["confidence"] == ev.confidence
    assert (tmp_path / "cases" / ev.case_id / "report.md").exists()

    await h.app.bot.handle_update(cb(f"trace:{ev.id}"))
    assert "Tracing funds" in h.t.messages[-1]["text"]
    await h.settle()
    assert "FUND TRACE" in h.t.messages[-1]["text"]

    await h.app.bot.handle_update(cb(f"xprep:{ev.id}"))
    m = h.t.messages[-1]
    assert "X post draft" in m["text"]
    buttons = [b["text"] for row in m["reply_markup"]["inline_keyboard"] for b in row]
    assert buttons == ["CANCEL"]  # X disabled -> no POST button
    async with h.app.sf() as s:
        draft = (await s.execute(XPostDraft.__table__.select())).first()
    await h.app.bot.handle_update(cb(f"xpost:{draft.id}"))
    assert "Not posted" in h.t.messages[-1]["text"]  # never posts while disabled


async def test_x_post_requires_manual_approval_when_enabled(make_app):
    h, ev = await _incident(make_app, x_enabled=True, x_api_key="ck", x_api_secret="cs", x_access_token="at", x_access_token_secret="as")
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        assert request.headers["Authorization"].startswith("OAuth ")
        return httpx.Response(201, json={"data": {"id": "1850000000000000000"}})

    h.app.x._transport = httpx.MockTransport(handler)
    h.app.bot.x = h.app.x
    assert posted == []  # nothing posted automatically by the incident
    await h.app.bot.handle_update(cb(f"xprep:{ev.id}"))
    buttons = [b for row in h.t.messages[-1]["reply_markup"]["inline_keyboard"] for b in row]
    assert [b["text"] for b in buttons] == ["📤 POST TO X", "CANCEL"]
    draft_id = int(buttons[0]["callback_data"].split(":")[1])
    await h.app.bot.handle_update(cb(f"xpost:{draft_id}"))
    assert len(posted) == 1 and "Posted to X" in h.t.messages[-1]["text"]
    await h.app.bot.handle_update(cb(f"xpost:{draft_id}"))  # double press
    assert len(posted) == 1
    # cancel flow
    await h.app.bot.handle_update(cb(f"xprep:{ev.id}"))
    d2 = int(h.t.messages[-1]["reply_markup"]["inline_keyboard"][0][1]["callback_data"].split(":")[1])
    await h.app.bot.handle_update(cb(f"xcancel:{d2}"))
    await h.app.bot.handle_update(cb(f"xpost:{d2}"))
    assert len(posted) == 1


def test_x_validation_and_oauth_signature_shape():
    with pytest.raises(XError):
        validate_post("This is a confirmed scam!")
    with pytest.raises(XError):
        validate_post("x" * 300)
    hdr = oauth1_header(
        "POST", "https://api.twitter.com/2/tweets", consumer_key="ck", consumer_secret="cs", token="t", token_secret="ts", nonce="n", timestamp="1"
    )
    assert hdr == oauth1_header(
        "POST", "https://api.twitter.com/2/tweets", consumer_key="ck", consumer_secret="cs", token="t", token_secret="ts", nonce="n", timestamp="1"
    )
    assert re.search(r'oauth_signature="[A-Za-z0-9%]+"', hdr)
    assert not XService(make_settings("sqlite+aiosqlite://", x_enabled=True)).enabled  # credentials missing


FORBIDDEN = re.compile(r"confirmed scam|100% (?:confirmed|certain)|scammer|thief|stolen|criminal(?! responsibility)", re.I)


async def test_reports_are_factual_and_complete(make_app):
    h, ev = await _incident(make_app)
    async with h.app.sf() as s:
        b = await rs.load_bundle(s, ev.id)
    alert = rs.telegram_alert(b)
    packet = rs.evidence_packet(b)
    report = rs.investigator_report(b)
    post = rs.x_post(b)
    for text in (alert, packet, report, post):
        assert not FORBIDDEN.search(text), text
    for section in ("Observed blockchain facts", "Address similarity measurements", "Poisoning evidence", "Downstream fund tracing",
                    "Analytical assessment", "All relevant transaction hashes", "All relevant addresses", "Methodology", "Limitations"):  # fmt: skip
        assert section in report
    for field in ("CASE ID:", "NETWORK:", "TOKEN:", "VICTIM:", "LEGITIMATE RECIPIENT:", "SUSPICIOUS RECIPIENT:", "AMOUNT:", "TRANSACTION HASH:",
                  "BLOCK:", "TIMESTAMP:", "SIMILARITY:", "HISTORICAL TRANSACTIONS WITH LEGITIMATE RECIPIENT:", "PREVIOUS VICTIM → LEGITIMATE TOTAL:",
                  "SUSPICIOUS RECIPIENT PREVIOUS ACTIVITY:", "POISONING TRANSACTION OBSERVED:", "FORWARDING ACTIVITY:", "TRACE:", "TRONSCAN LINKS:", "CONFIDENCE:"):  # fmt: skip
        assert field in packet
    assert "does not by itself prove" in report and "does not by itself prove" in packet
    assert rs.tweet_length(post) <= 280
    assert "appears to" in post
    js = rs.report_json(b)
    assert js["transaction_hash"] == ev.tx_hash and js["amount_base_units"] == str(25_000 * USDT)


def test_codebase_is_read_only():
    """No signing, broadcasting, key handling or write endpoints anywhere in the application."""
    root = Path(__file__).resolve().parent.parent / "app"
    forbidden = [
        "broadcasttransaction", "broadcasthex", "createtransaction", "/wallet/triggersmartcontract", "transferasset",
        "freezebalance", "private_key", "privatekey", "signtransaction", "gettransactionsign", "mnemonic", "seed_phrase",
    ]  # fmt: skip
    for path in root.rglob("*.py"):
        text = path.read_text().lower()
        for f in forbidden:
            assert f not in text, f"{f} found in {path}"


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL schema check needs TEST_DATABASE_URL")
async def test_postgres_migrations_match_models(make_app):
    h = await make_app()  # schema created by migrations/*.sql
    async with h.app.engine.connect() as conn:

        def describe(sync_conn):
            insp = inspect(sync_conn)
            return {t: {c["name"] for c in insp.get_columns(t)} for t in insp.get_table_names()}

        actual = await conn.run_sync(describe)
    for table in Base.metadata.sorted_tables:
        assert table.name in actual, table.name
        assert {c.name for c in table.columns} == actual[table.name], table.name
    assert WatchedWallet.__tablename__ in actual and "schema_migrations" in actual
