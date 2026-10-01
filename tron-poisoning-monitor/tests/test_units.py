"""Unit tests: addresses, amounts, similarity, risk engine, block parsing, rate limiting, config, logging."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from app.config import DEFAULT_RISK_WEIGHTS, Settings
from app.domain import EventType, TokenTransfer, Tri
from app.services.risk_engine import RecipientStats, RiskConfig, RiskContext, RiskEngine
from app.services.similarity import SimilarityConfig, SimilarityEngine, candidate_keys, levenshtein
from app.services.tron_service import TRANSFER_TOPIC, parse_block_transfers, parse_trc20_history
from app.simulation import addresses as A
from app.utils.address import (
    InvalidAddress,
    base58_to_hex,
    hex_to_base58,
    is_valid_tron_address,
    normalize_address,
    tronscan_address_url,
    tronscan_tx_url,
)
from app.utils.amounts import format_amount, parse_token_amount
from app.utils.logging import redact, register_secrets, setup_logging
from app.utils.ratelimit import PriorityRateLimiter

USDT_C = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


# ----------------------------------------------------------------- addresses
def test_address_normalization_hex_and_base58_forms_agree():
    h = base58_to_hex(A.LEGIT)
    assert h.startswith("41") and len(h) == 42
    assert normalize_address(h) == A.LEGIT
    assert normalize_address("0x" + h[2:]) == A.LEGIT
    assert normalize_address(h[2:]) == A.LEGIT
    assert normalize_address("0" * 24 + h[2:]) == A.LEGIT  # ABI topic
    assert normalize_address(f"  {A.LEGIT}  ") == A.LEGIT
    assert hex_to_base58(base58_to_hex(USDT_C)) == USDT_C


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "T",
        "TLegit9dwtq5H8YqVXiRsE7Y2zvRTSWr2X",
        "TLegit9dwtq5H8YqVXiRsE7Y2zvRTSWr2",
        "0x1234",
        "TOIl0" * 7,
        None,
        123,
        "tlegit9dwtq5h8yqvxirse7y2zvrtswr2c",
    ],
)
def test_invalid_addresses_rejected(bad):
    assert not is_valid_tron_address(bad)
    with pytest.raises(InvalidAddress):
        normalize_address(bad)


def test_base58_case_is_never_changed():
    # Base58 is case-sensitive: a case-flipped address is a different (here: invalid) string.
    flipped = A.LEGIT[0] + A.LEGIT[1:].swapcase()
    assert not is_valid_tron_address(flipped)


def test_tronscan_links():
    tx = "ab" * 32
    assert tronscan_tx_url(tx) == f"https://tronscan.org/#/transaction/{tx}"
    assert tronscan_tx_url("0x" + tx.upper()) == f"https://tronscan.org/#/transaction/{tx}"
    assert tronscan_address_url(base58_to_hex(A.LEGIT)) == f"https://tronscan.org/#/address/{A.LEGIT}"
    with pytest.raises(ValueError):
        tronscan_tx_url("not-a-hash")
    with pytest.raises(InvalidAddress):
        tronscan_address_url("Tbad")


# ----------------------------------------------------------------- amounts
def test_amounts_are_integer_exact():
    assert parse_token_amount("0.000001", 6) == 1
    assert parse_token_amount("25,000", 6) == 25_000_000_000
    assert parse_token_amount("1234567890123.123456", 6) == 1234567890123123456
    with pytest.raises(ValueError):
        parse_token_amount("0.0000001", 6)
    with pytest.raises(ValueError):
        parse_token_amount("-1", 6)
    with pytest.raises(ValueError):
        parse_token_amount("nan", 6)
    assert format_amount(25_000_000_000) == "25,000"
    assert format_amount(1) == "0.000001"
    assert format_amount(10**30 + 5) == "1,000,000,000,000,000,000,000,000.000005"
    assert format_amount(1_500_000, grouping=False, trim=False) == "1.500000"


# ----------------------------------------------------------------- similarity
ENGINE = SimilarityEngine()


def test_strong_prefix_and_suffix_match():
    r = ENGINE.compare(A.LEGIT, A.POISON)
    assert (r.prefix_match_length, r.suffix_match_length) == (5, 4)
    assert r.edge_rule == "both_edges" and r.is_match
    assert r.prefix_similarity == 1.0 and r.suffix_similarity == 0.8
    assert 0.6 <= r.similarity_score <= 1
    assert r.coincidence_log10 < -15  # matching 9 chars by chance ~ 1e-16
    assert r.positional_similarity > 0.2 and r.overall_similarity > 0.2


def test_prefix_only_and_suffix_only_do_not_match():
    p = ENGINE.compare(A.LEGIT, A.PREFIX_ONLY)
    assert p.prefix_match_length >= 4 and p.suffix_match_length < 4 and not p.is_match
    s = ENGINE.compare(A.LEGIT, A.SUFFIX_ONLY)
    assert s.suffix_match_length == 4 and s.prefix_match_length == 0 and not s.is_match


def test_unrelated_and_identical_do_not_match():
    assert not ENGINE.compare(A.LEGIT, A.VICTIM).is_match
    same = ENGINE.compare(A.LEGIT, base58_to_hex(A.LEGIT))  # hex form of the same account
    assert same.identical and not same.is_match


def test_leading_t_is_not_counted():
    r = ENGINE.compare(A.LEGIT, A.VICTIM)  # both start with 'T' only
    assert r.prefix_match_length == 0


def test_thresholds_are_configurable():
    strict = SimilarityEngine(SimilarityConfig(min_prefix_match=6, min_suffix_match=4))
    assert not strict.compare(A.LEGIT, A.POISON).is_match
    loose_single = SimilarityEngine(SimilarityConfig(single_edge_min_match=5, min_similarity_score=0.3))
    r = loose_single.compare(A.LEGIT, A.PREFIX_ONLY)
    assert r.edge_rule == "single_edge" and r.is_match
    high_score = SimilarityEngine(SimilarityConfig(min_similarity_score=0.95))
    assert not high_score.compare(A.LEGIT, A.POISON).is_match


def test_best_matches_and_candidate_keys():
    best = ENGINE.best_matches(A.POISON, [A.VICTIM, A.LEGIT, A.LEGIT_B, "garbage"])
    assert [r.legitimate for r in best] == [A.LEGIT]
    pk, sk = candidate_keys(A.POISON)
    assert (pk, sk) == candidate_keys(A.LEGIT)
    assert levenshtein("kitten", "sitting") == 3


def test_similarity_from_settings():
    s = Settings(_env_file=None, min_prefix_match=3, min_suffix_match=3, min_similarity_score=0.5)
    cfg = SimilarityConfig.from_settings(s)
    assert (cfg.min_prefix_match, cfg.min_suffix_match, cfg.min_similarity_score) == (3, 3, 0.5)


# ----------------------------------------------------------------- risk engine
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _ctx(**kw) -> RiskContext:
    base = dict(
        similarity=ENGINE.compare(A.LEGIT, A.POISON),
        legit=RecipientStats(87, 2_430_000 * 10**6, NOW - timedelta(days=300), NOW - timedelta(days=3), 90_000 * 10**6, 5_000 * 10**6, 27_931 * 10**6),
        suspicious_prior=RecipientStats(),
        amount=25_000 * 10**6,
        decimals=6,
        tx_time=NOW,
        initiator_is_victim=True,
    )
    base.update(kw)
    return RiskContext(**base)


ENG = RiskEngine(RiskConfig(weights=dict(DEFAULT_RISK_WEIGHTS)))


def test_risk_high_confidence_without_dust():
    a = ENG.assess(_ctx())
    assert a.event_type == EventType.SUCCESSFUL_POISONING_EVENT and a.score >= 80
    keys = {s.key for s in a.signals}
    assert {"recipient_new", "similarity_both_edges", "legit_used_10plus"} <= keys


def test_risk_dust_and_campaign_raise_confidence():
    base = ENG.assess(_ctx(legit=RecipientStats(2, 200 * 10**6, NOW, NOW, 100 * 10**6, 100 * 10**6, 100 * 10**6), amount=150 * 10**6))
    more = ENG.assess(
        _ctx(
            legit=RecipientStats(2, 200 * 10**6, NOW, NOW, 100 * 10**6, 100 * 10**6, 100 * 10**6),
            amount=150 * 10**6,
            prior_dust_to_victim=Tri.YES,
            dust_recipients_count=50,
            forwarded_pct=99,
        )
    )
    assert more.score > base.score


def test_risk_negative_signals():
    used = ENG.assess(_ctx(suspicious_prior=RecipientStats(5, 50_000 * 10**6)))
    assert used.event_type is None or used.event_type != EventType.SUCCESSFUL_POISONING_EVENT
    labeled = ENG.assess(_ctx(label_category="exchange"))
    assert labeled.score < ENG.assess(_ctx()).score


def test_risk_caps_successful_classification():
    small = ENG.assess(_ctx(amount=500_000, prior_dust_to_victim=Tri.YES))
    assert small.event_type == EventType.POISONING_CANDIDATE and "MIN_VICTIM_AMOUNT" in small.capped_reason
    third = ENG.assess(_ctx(initiator_is_victim=False, prior_dust_to_victim=Tri.YES, dust_recipients_count=10))
    assert third.event_type != EventType.SUCCESSFUL_POISONING_EVENT


def test_risk_threshold_and_weights_configurable():
    s = Settings(_env_file=None, confidence_threshold=99, risk_weights=json.dumps({"recipient_new": 0}))
    eng = RiskEngine(RiskConfig.from_settings(s))
    a = eng.assess(_ctx())
    assert a.event_type == EventType.POISONING_CANDIDATE
    assert all(sig.key != "recipient_new" or sig.points == 0 for sig in a.signals)
    with pytest.raises(ValueError):
        Settings(_env_file=None, risk_weights='{"nonexistent": 5}')
    with pytest.raises(ValueError):
        Settings(_env_file=None, candidate_threshold=90, confidence_threshold=80)


# ----------------------------------------------------------------- parsing
def _topic(addr: str) -> str:
    return "0" * 24 + base58_to_hex(addr)[2:]


def test_parse_block_transfers_filters_and_decodes():
    usdt_hex = base58_to_hex(USDT_C)[2:]
    other_hex = base58_to_hex(A.LEGIT_B)[2:]
    block = {
        "block_header": {"raw_data": {"number": 70000000, "timestamp": 1_790_000_000_000}},
        "transactions": [
            {"txID": "aa" * 32, "raw_data": {"contract": [{"parameter": {"value": {"owner_address": base58_to_hex(A.VICTIM)}}}]}},
        ],
    }
    infos = [
        {
            "id": "aa" * 32,
            "blockTimeStamp": 1_790_000_000_000,
            "receipt": {"result": "SUCCESS"},
            "log": [
                {"address": usdt_hex, "topics": [TRANSFER_TOPIC, _topic(A.VICTIM), _topic(A.POISON)], "data": f"{25_000 * 10**6:064x}"},
                {"address": other_hex, "topics": [TRANSFER_TOPIC, _topic(A.VICTIM), _topic(A.POISON)], "data": f"{1:064x}"},
                {
                    "address": usdt_hex,
                    "topics": ["8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925", _topic(A.VICTIM), _topic(A.POISON)],
                    "data": "00",
                },
            ],
        },
        {
            "id": "bb" * 32,
            "result": "FAILED",
            "receipt": {"result": "REVERT"},
            "log": [{"address": usdt_hex, "topics": [TRANSFER_TOPIC, _topic(A.VICTIM), _topic(A.LEGIT)], "data": "01"}],
        },
    ]
    blk = parse_block_transfers(70000000, block, infos, {USDT_C: 6})
    assert len(blk.transfers) == 1
    t = blk.transfers[0]
    assert (t.from_address, t.to_address, t.amount, t.initiator, t.block_number) == (A.VICTIM, A.POISON, 25_000 * 10**6, A.VICTIM, 70000000)


def test_parse_trc20_history_and_idempotency_key():
    items = [
        {
            "transaction_id": "cc" * 32,
            "token_info": {"address": USDT_C, "decimals": 6},
            "block_timestamp": 1,
            "from": A.VICTIM,
            "to": A.LEGIT,
            "type": "Transfer",
            "value": "5",
        },
        {
            "transaction_id": "cc" * 32,
            "token_info": {"address": USDT_C},
            "block_timestamp": 1,
            "from": A.VICTIM,
            "to": A.LEGIT,
            "type": "Transfer",
            "value": "5",
        },
        {
            "transaction_id": "dd" * 32,
            "token_info": {"address": USDT_C},
            "block_timestamp": 1,
            "from": A.VICTIM,
            "to": A.LEGIT,
            "type": "Approval",
            "value": "5",
        },
        {
            "transaction_id": "ee" * 32,
            "token_info": {"address": A.LEGIT_B},
            "block_timestamp": 1,
            "from": A.VICTIM,
            "to": A.LEGIT,
            "type": "Transfer",
            "value": "5",
        },
    ]
    out = parse_trc20_history(items, USDT_C, confirmed=True)
    assert len(out) == 2 and out[0].seq == 0 and out[1].seq == 1
    assert out[0].transfer_key != out[1].transfer_key
    # the same transfer seen in a block (with log index/initiator) has the same key
    same = TokenTransfer("cc" * 32, USDT_C, A.VICTIM, A.LEGIT, 5, 1, block_number=9, initiator=A.VICTIM, log_index=3)
    assert same.transfer_key == out[0].transfer_key


# ----------------------------------------------------------------- rate limiter
async def test_rate_limiter_priority_and_rate():
    lim = PriorityRateLimiter(50, burst=1)
    order = []
    await lim.acquire(0)

    async def take(p, name):
        await lim.acquire(p)
        order.append(name)

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await asyncio.gather(take(3, "history-1"), take(3, "history-2"), take(0, "live"))
    assert order[0] == "live"
    assert loop.time() - t0 >= 0.04  # 3 tokens at 50/s after the burst


# ----------------------------------------------------------------- logging
def test_secrets_are_redacted(capsys):
    setup_logging("INFO", "text")
    register_secrets(["super-secret-api-key", "123456:ABC-telegram-token"])
    from app.utils.logging import get_logger

    get_logger("t").info("REQUEST", url="https://api.telegram.org/bot123456:ABC-telegram-token/sendMessage", key="super-secret-api-key")
    logging.getLogger("httpx").warning("HTTP Request: POST https://api.telegram.org/bot999:OTHER/getUpdates")
    out = capsys.readouterr().out
    assert "super-secret-api-key" not in out and "ABC-telegram-token" not in out and "999:OTHER" not in out
    assert "***" in out
    assert redact("api.telegram.org/botXYZ/send") == "api.telegram.org/bot***/send"


def test_settings_tokens_and_admins():
    s = Settings(_env_file=None, telegram_admin_chat_id="11, 22", tokens=f"USDT:{USDT_C}:6,USDC:{A.LEGIT_B}:6")
    assert s.admin_ids == {11, 22} and s.alert_chat_ids == [11, 22]
    assert [t.symbol for t in s.token_list] == ["USDT", "USDC"]
    assert s.units("dust_max_amount_usdt") == 1_000_000
    with pytest.raises(ValueError):
        Settings(_env_file=None, tokens="USDT:Tinvalid:6")
