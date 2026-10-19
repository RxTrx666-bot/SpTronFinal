from __future__ import annotations

import pytest

from outflow_alert.abi import TRANSFER_TOPIC, decode_string, decode_transfer_log, decode_uint256
from outflow_alert.address import base58_to_hex, hex_to_base58, is_valid_tron_address, topic_to_address
from outflow_alert.amounts import format_units, match_target, parse_amount_list, parse_raw_value, to_base_units
from outflow_alert.config import ConfigError, load_settings
from tests.fakes import OTHER, USDT, WALLET, abi_string, topic, transfer_log

BASE_ENV = {"TRON_API_KEY": "k", "TELEGRAM_BOT_TOKEN": "1:a", "TELEGRAM_CHAT_ID": "-100, 42"}


def test_addresses_roundtrip():
    assert base58_to_hex(USDT) == "41a614f803b6fd780986a42c78ec9c7f77e6ded13c"
    assert hex_to_base58("a614f803b6fd780986a42c78ec9c7f77e6ded13c") == USDT
    assert is_valid_tron_address(WALLET)
    assert not is_valid_tron_address(WALLET[:-1] + ("a" if WALLET[-1] != "a" else "b"))
    assert topic_to_address(topic(WALLET)) == WALLET


def test_amounts_are_integer_base_units():
    assert to_base_units("1.01") == 1_010_000
    assert to_base_units("0.000100") == 100
    assert parse_amount_list("1.01,10,20,30") == (1_010_000, 10_000_000, 20_000_000, 30_000_000)
    with pytest.raises(ValueError):
        to_base_units("0.0000001")
    with pytest.raises(TypeError):
        to_base_units(1.01)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        parse_raw_value(1.5)
    assert parse_raw_value("1010000") == 1_010_000
    assert format_units(1_010_000) == "1.01"
    assert format_units(1_234_567_890) == "1,234.56789"


@pytest.mark.parametrize(
    "amount,expected",
    [
        (1_010_000, 1_010_000),
        (1_010_100, 1_010_000),  # +0.0001 inclusive
        (1_009_900, 1_010_000),  # -0.0001 inclusive
        (1_010_101, None),
        (1_009_899, None),
        (10_000_000, 10_000_000),
        (19_999_950, 20_000_000),
        (30_000_100, 30_000_000),
        (30_000_101, None),
        (5_000_000, None),
    ],
)
def test_match_target_tolerance(amount, expected):
    targets = parse_amount_list("1.01,10,20,30")
    assert match_target(amount, targets, 100) == expected


def test_abi_decoding():
    assert decode_string(abi_string("USDT")) == "USDT"
    assert decode_string("55534454" + "0" * 56) == "USDT"  # bytes32 style
    assert decode_uint256(f"{6:064x}") == 6
    log = decode_transfer_log(transfer_log(USDT, WALLET, OTHER, 10_000_000), 3)
    assert log is not None
    assert (log.contract, log.from_address, log.to_address, log.amount_raw, log.log_index) == (
        USDT, WALLET, OTHER, 10_000_000, 3)
    approval = transfer_log(USDT, WALLET, OTHER, 1)
    approval["topics"][0] = "8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"
    assert decode_transfer_log(approval, 0) is None
    assert TRANSFER_TOPIC.startswith("ddf252ad")


def test_config_defaults_and_validation():
    s = load_settings(dict(BASE_ENV), env_file=None)
    assert s.wallet == WALLET and s.usdt_contract == USDT
    assert s.amounts_raw == (1_010_000, 10_000_000, 20_000_000, 30_000_000)
    assert s.tolerance_raw == 100
    assert s.poll_interval_seconds == 2.0
    assert s.telegram_chat_ids == ("-100", "42")
    with pytest.raises(ConfigError, match="TRON_API_KEY"):
        load_settings({k: v for k, v in BASE_ENV.items() if k != "TRON_API_KEY"}, env_file=None)
    with pytest.raises(ConfigError, match="AMOUNT_TOLERANCE"):
        load_settings({**BASE_ENV, "AMOUNT_TOLERANCE": "0.0000001"}, env_file=None)
    with pytest.raises(ConfigError, match="WALLET_ADDRESS"):
        load_settings({**BASE_ENV, "WALLET_ADDRESS": "Tnotanaddress"}, env_file=None)
