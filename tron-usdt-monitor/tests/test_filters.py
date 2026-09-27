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
        # spec: 1.000000 <= amount <= 1.000100 (inclusive)
        ("1.000000", True), ("1.000001", True), ("1.000010", True), ("1.000050", True),
        ("1.000087", True), ("1.000099", True), ("1.000100", True), ("1", True), ("1.0001", True),
        ("1.000101", False), ("1.001000", False), ("1.010000", False), ("1.100000", False),
        ("1.200000", False), ("0.999999", False), ("2", False), ("10", False), ("100", False),
        ("0", False),
    ],
)
def test_amount_range(amount, expected):
    raw = parse_token_amount(amount)
    decision = flt().evaluate(transfer(raw))
    assert decision.matched is expected, (amount, decision.reason)


def test_range_boundaries_are_integer_exact():
    f = flt()
    assert f.min_raw == 1_000_000 and f.max_raw == 1_000_100  # MIN/MAX_USDT_BASE_UNITS
    assert f.amount_in_range(1_000_000) and f.amount_in_range(1_000_099) and f.amount_in_range(1_000_100)
    assert not f.amount_in_range(999_999) and not f.amount_in_range(1_000_101)


def test_parse_amount_is_exact_and_rejects_floats_and_excess_precision():
    assert parse_token_amount("1.2") == 1_200_000
    assert parse_token_amount("1.0001") == 1_000_100
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
    assert format_token_amount(1_000_087) == "1.000087"
    assert format_token_amount(1_199_999) == "1.199999"
    assert format_token_amount(5) == "0.000005"


def test_incoming_direction():
    d = flt().evaluate(transfer(1_000_050, sender=OTHER, recipient=WALLET))
    assert d.matched and d.direction is Direction.INCOMING


def test_outgoing_direction():
    d = flt().evaluate(transfer(1_000_087, sender=WALLET, recipient=OTHER))
    assert d.matched and d.direction is Direction.OUTGOING


def test_self_transfer_direction():
    d = flt().evaluate(transfer(1_000_087, sender=WALLET, recipient=WALLET))
    assert d.matched and d.direction is Direction.SELF


def test_unrelated_wallet_ignored():
    assert not flt().evaluate(transfer(1_000_087, sender=OTHER, recipient=THIRD)).matched


def test_wrong_contract_ignored_even_if_symbol_is_usdt():
    d = flt().evaluate(transfer(1_000_087, contract=FAKE_USDT, symbol="USDT"))
    assert not d.matched and "contract" in d.reason


def test_wrong_token_symbol_ignored():
    assert not flt().evaluate(transfer(1_000_087, symbol="USDC")).matched


def test_wrong_decimals_ignored():
    assert not flt().evaluate(transfer(1_000_087, decimals=18)).matched


def test_non_transfer_event_ignored():
    assert not flt().evaluate(transfer(1_000_087, event_type="Approval")).matched


def test_config_rejects_bad_range():
    with pytest.raises(ConfigError):
        make_settings(MIN_USDT="1.3", MAX_USDT="1.2")
    with pytest.raises(ConfigError):
        make_settings(MIN_USDT="1.0000001")
    with pytest.raises(ConfigError):
        make_settings(WALLET_ADDRESS="TWkvffFDMsqbmTLkMHMABmw452Hyq98cdX")  # bad checksum


def test_default_is_outgoing_only():
    from app.config import Settings
    s = Settings.from_env({"TELEGRAM_BOT_TOKEN": "x:y", "TELEGRAM_ADMIN_CHAT_ID": "1"})
    assert s.alert_directions == frozenset({Direction.OUTGOING}) and s.outgoing_only
    f = TransferFilter(s.wallet_address, s.usdt_contract, s.min_raw, s.max_raw, directions=s.alert_directions)
    incoming = f.evaluate(transfer(1_000_087, sender=OTHER, recipient=WALLET))
    assert not incoming.matched and "INCOMING not monitored" in incoming.reason
    assert f.evaluate(transfer(1_000_087, sender=WALLET, recipient=OTHER)).direction is Direction.OUTGOING
    assert f.evaluate(transfer(1_000_087, sender=WALLET, recipient=WALLET)).matched  # self-send = outgoing
    # outgoing amount range still enforced
    assert not f.evaluate(transfer(1_000_101, sender=WALLET, recipient=OTHER)).matched
    assert not f.evaluate(transfer(999_999, sender=WALLET, recipient=OTHER)).matched


def test_alert_directions_config():
    both = make_settings(ALERT_DIRECTIONS="outgoing, incoming")
    assert both.alert_directions == frozenset({Direction.OUTGOING, Direction.INCOMING})
    with pytest.raises(ConfigError):
        make_settings(ALERT_DIRECTIONS="SIDEWAYS")


def test_config_defaults():
    s = make_settings()
    assert s.min_usdt == "1.000000" and s.max_usdt == "1.000100"
    assert s.wallet_address == WALLET
    assert s.monitor_mode == "account"
    assert s.poll_interval_seconds == 2.0
    assert s.backfill_enabled is False and s.backfill_limit == 100
