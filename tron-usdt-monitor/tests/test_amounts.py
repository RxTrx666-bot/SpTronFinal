"""AMOUNTS=1.01,10,20,30: each amount matches [amount, amount + 0.000100]; everything else ignored."""

import pytest

from app.config import ConfigError
from app.filters import TransferFilter, parse_token_amount
from app.formatting import build_wallet_message
from app.stats import MonitorStats
from app.transaction_parser import TokenTransfer
from tests.helpers import BLOCK_TS, OTHER, USDT, WALLET, make_settings, tx_hash

AMOUNTS = "1.01,10,20,30"


def flt(settings):
    return TransferFilter(settings.wallet_address, settings.usdt_contract, settings.min_raw, settings.max_raw,
                          directions=settings.alert_directions, ranges=settings.amount_ranges)


def outgoing(amount: str) -> TokenTransfer:
    return TokenTransfer(tx_hash=tx_hash(1), block_timestamp_ms=BLOCK_TS, contract_address=USDT, sender=WALLET,
                         recipient=OTHER, amount_raw=parse_token_amount(amount), token_symbol="USDT",
                         token_decimals=6)


@pytest.mark.parametrize(
    "amount,expected",
    [
        ("1.010000", True), ("1.010050", True), ("1.010100", True),
        ("10", True), ("10.000001", True), ("10.000100", True),
        ("20", True), ("20.000057", True), ("20.000100", True),
        ("30", True), ("30.000099", True), ("30.000100", True),
        # everything else is ignored
        ("1.000000", False), ("1.000050", False), ("1.000100", False), ("1.009999", False),
        ("1.010101", False), ("9.999999", False), ("10.000101", False), ("15", False),
        ("19.999999", False), ("20.5", False), ("25", False), ("29.999999", False),
        ("30.000101", False), ("31", False), ("100", False),
    ],
)
def test_amount_list(amount, expected):
    s = make_settings(AMOUNTS=AMOUNTS, ALERT_DIRECTIONS="OUTGOING")
    assert flt(s).evaluate(outgoing(amount)).matched is expected, amount


def test_exact_amounts_with_zero_tolerance_and_explicit_ranges():
    s = make_settings(AMOUNTS="10,20", AMOUNT_TOLERANCE="0", ALERT_DIRECTIONS="OUTGOING")
    assert s.amount_ranges == ((10_000_000, 10_000_000), (20_000_000, 20_000_000))
    f = flt(s)
    assert f.evaluate(outgoing("10")).matched and not f.evaluate(outgoing("10.000001")).matched
    s2 = make_settings(AMOUNTS="10-30", ALERT_DIRECTIONS="OUTGOING")
    assert flt(s2).evaluate(outgoing("17.25")).matched


def test_amounts_override_min_max_and_old_config_still_works():
    s = make_settings(AMOUNTS=AMOUNTS, MIN_USDT="1.000000", MAX_USDT="1.000100")
    assert s.amount_ranges[0] == (1_010_000, 1_010_100) and len(s.amount_ranges) == 4
    old = make_settings()
    assert old.amount_ranges == ((1_000_000, 1_000_100),)


@pytest.mark.parametrize("bad", ["1,01", "abc", "10.0000001", "30-20"])
def test_bad_amounts_rejected(bad):
    # "1,01" would be read as 1 and 01; it is accepted only because both parse. Guard real typos:
    if bad == "1,01":
        assert make_settings(AMOUNTS=bad).amount_ranges == ((1_000_000, 1_000_100), (1_000_000, 1_000_100))
        return
    with pytest.raises(ConfigError):
        make_settings(AMOUNTS=bad)


def test_wallet_message_lists_all_amounts():
    s = make_settings(AMOUNTS=AMOUNTS, ALERT_DIRECTIONS="OUTGOING")
    text = build_wallet_message(s, MonitorStats())
    for line in ("Amounts:", "1.010000 – 1.010100 USDT", "10.000000 – 10.000100 USDT",
                 "20.000000 – 20.000100 USDT", "30.000000 – 30.000100 USDT"):
        assert line in text
