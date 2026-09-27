import pytest

from app.config import ConfigError
from app.filters import Direction, TransferFilter, format_token_amount, parse_token_amount
from app.transaction_parser import TokenTransfer
from tests.helpers import BLOCK_TS, FAKE_USDT, OTHER, THIRD, USDT, WALLET, make_settings, tx_hash


def flt() -> TransferFilter:
    s = make_settings()
    return TransferFilter(s.wallet_address, s.usdt_contract, s.min_raw, s.max_raw)


def transfer(amount_raw: int, sender=OTHER, recipient=WALLET, contract=USDT, symbol="USDT",
             decimals=6, event_type="Transfer") -> TokenTransfer:
    return TokenTransfer(tx_hash=tx_hash(1), block_timestamp_ms=BLOCK_TS, contract_address=contract,
                         sender=sender, recipient=recipient, amount_raw=amount_raw, token_symbol=symbol,
                         token_decimals=decimals, event_type=event_type)


@pytest.mark.parametrize(
    "amount,expected",
    [
        ("1.000000", True), ("1.010000", True), ("1.050000", True), ("1.100000", True),
        ("1.199999", True), ("1.200000", True), ("1", True), ("1.2", True),
        ("0.999999", False), ("1.200001", False), ("2.000000", False), ("2", False),
        ("10", False), ("100", False), ("0", False), ("0.000001", False),
    ],
)
def test_amount_range(amount, expected):
    raw = parse_token_amount(amount)
    decision = flt().evaluate(transfer(raw))
    assert decision.matched is expected, (amount, decision.reason)


def test_range_boundaries_are_integer_exact():
    f = flt()
    assert f.min_raw == 1_000_000 and f.max_raw == 1_200_000
    assert f.amount_in_range(1_000_000) and f.amount_in_range(1_200_000)
    assert not f.amount_in_range(999_999) and not f.amount_in_range(1_200_001)


def test_parse_amount_is_exact_and_rejects_floats_and_excess_precision():
    assert parse_token_amount("1.2") == 1_200_000
    assert parse_token_amount("0.1") + parse_token_amount("0.2") == parse_token_amount("0.3")
    with pytest.raises(ValueError):
        parse_token_amount(1.2)  # float refused
    with pytest.raises(ValueError):
        parse_token_amount("1.0000001")
    with pytest.raises(ValueError):
        parse_token_amount("-1")
    with pytest.raises(ValueError):
        parse_token_amount("abc")


def test_format_amount():
    assert format_token_amount(1_100_000) == "1.100000"
    assert format_token_amount(1_199_999) == "1.199999"
    assert format_token_amount(5) == "0.000005"


def test_incoming_direction():
    d = flt().evaluate(transfer(1_050_000, sender=OTHER, recipient=WALLET))
    assert d.matched and d.direction is Direction.INCOMING


def test_outgoing_direction():
    d = flt().evaluate(transfer(1_100_000, sender=WALLET, recipient=OTHER))
    assert d.matched and d.direction is Direction.OUTGOING


def test_self_transfer_direction():
    d = flt().evaluate(transfer(1_100_000, sender=WALLET, recipient=WALLET))
    assert d.matched and d.direction is Direction.SELF


def test_unrelated_wallet_ignored():
    assert not flt().evaluate(transfer(1_100_000, sender=OTHER, recipient=THIRD)).matched


def test_wrong_contract_ignored_even_if_symbol_is_usdt():
    d = flt().evaluate(transfer(1_100_000, contract=FAKE_USDT, symbol="USDT"))
    assert not d.matched and "contract" in d.reason


def test_wrong_token_symbol_ignored():
    assert not flt().evaluate(transfer(1_100_000, symbol="USDC")).matched


def test_wrong_decimals_ignored():
    assert not flt().evaluate(transfer(1_100_000, decimals=18)).matched


def test_non_transfer_event_ignored():
    assert not flt().evaluate(transfer(1_100_000, event_type="Approval")).matched


def test_config_rejects_bad_range():
    with pytest.raises(ConfigError):
        make_settings(MIN_USDT="1.3", MAX_USDT="1.2")
    with pytest.raises(ConfigError):
        make_settings(MIN_USDT="1.0000001")
    with pytest.raises(ConfigError):
        make_settings(WALLET_ADDRESS="TWkvffFDMsqbmTLkMHMABmw452Hyq98cdX")  # bad checksum


def test_config_defaults():
    s = make_settings()
    assert s.min_usdt == "1.000000" and s.max_usdt == "1.200000"
    assert s.wallet_address == WALLET
    assert s.monitor_mode == "account"
    assert s.poll_interval_seconds == 2.0
    assert s.backfill_enabled is False and s.backfill_limit == 100
