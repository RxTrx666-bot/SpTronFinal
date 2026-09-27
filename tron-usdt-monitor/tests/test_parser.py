import pytest

from app.tron_address import base58_to_hex, hex_to_base58, is_valid_address
from app.transaction_parser import (
    MalformedTransactionError,
    parse_transfer_logs,
    parse_trongrid_trc20_record,
)
from tests.helpers import (
    BLOCK_TS, FAKE_USDT, OTHER, USDT, WALLET, trongrid_record, transfer_log, trx_transfer_info,
    tx_hash, tx_info,
)


def test_address_roundtrip_known_values():
    assert base58_to_hex(USDT) == "41a614f803b6fd780986a42c78ec9c7f77e6ded13c"
    assert hex_to_base58("a614f803b6fd780986a42c78ec9c7f77e6ded13c") == USDT
    assert hex_to_base58("0x" + "0" * 24 + "a614f803b6fd780986a42c78ec9c7f77e6ded13c") == USDT
    assert is_valid_address(WALLET)
    assert not is_valid_address("0xa614f803b6fd780986a42c78ec9c7f77e6ded13c")


def test_parse_trongrid_incoming_record():
    t = parse_trongrid_trc20_record(trongrid_record(1, sender=OTHER, recipient=WALLET, value="1000050"))
    assert t.tx_hash == tx_hash(1)
    assert t.sender == OTHER and t.recipient == WALLET
    assert t.amount_raw == 1_000_050
    assert t.contract_address == USDT and t.token_symbol == "USDT" and t.token_decimals == 6
    assert t.block_timestamp_ms == BLOCK_TS


def test_parse_trongrid_outgoing_record():
    t = parse_trongrid_trc20_record(trongrid_record(2, sender=WALLET, recipient=OTHER))
    assert t.sender == WALLET and t.recipient == OTHER


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.pop("transaction_id"),
        lambda r: r.update(transaction_id="xyz"),
        lambda r: r.update(value="1.1"),          # value must be integer base units
        lambda r: r.update(value="-1100000"),
        lambda r: r.update(value=None),
        lambda r: r.update(**{"from": "not-an-address"}),
        lambda r: r.update(to=None),
        lambda r: r.update(token_info=None),
        lambda r: r.update(block_timestamp="abc"),
        lambda r: r.update(block_timestamp=0),
        lambda r: r["token_info"].update(address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6X"),
    ],
)
def test_malformed_trongrid_record_rejected(mutate):
    record = trongrid_record(3)
    mutate(record)
    with pytest.raises(MalformedTransactionError):
        parse_trongrid_trc20_record(record)


def test_malformed_non_dict():
    with pytest.raises(MalformedTransactionError):
        parse_trongrid_trc20_record(["nope"])


def test_decode_transfer_event_log():
    info = tx_info(4, [transfer_log(WALLET, OTHER, 1_000_100)], block=71_234_567)
    [t] = parse_transfer_logs(info)
    assert t.sender == WALLET and t.recipient == OTHER
    assert t.amount_raw == 1_000_100
    assert t.contract_address == USDT
    assert t.block_number == 71_234_567
    assert t.block_timestamp_ms == BLOCK_TS  # blockchain time, not detection time
    assert t.source == "event_log" and t.log_index == 0


def test_multiple_logs_and_contract_prefilter():
    info = tx_info(5, [transfer_log(OTHER, WALLET, 1_000_087, contract=FAKE_USDT),
                       transfer_log(OTHER, WALLET, 1_000_087)])
    all_transfers = parse_transfer_logs(info)
    assert {t.contract_address for t in all_transfers} == {USDT, FAKE_USDT}
    only_usdt = parse_transfer_logs(info, contract=USDT)
    assert len(only_usdt) == 1 and only_usdt[0].contract_address == USDT


def test_wallet_prefilter():
    info = tx_info(6, [transfer_log(OTHER, hex_to_base58("41" + "44" * 20), 1_000_087)])
    assert parse_transfer_logs(info, contract=USDT, wallet=WALLET) == []


def test_trx_transfer_yields_nothing():
    assert parse_transfer_logs(trx_transfer_info(7)) == []


def test_failed_transaction_yields_nothing():
    info = tx_info(8, [transfer_log(OTHER, WALLET, 1_000_087)], result="REVERT")
    assert parse_transfer_logs(info) == []


def test_non_transfer_event_ignored():
    log = transfer_log(OTHER, WALLET, 1_000_087)
    log["topics"][0] = "8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"  # Approval
    assert parse_transfer_logs(tx_info(9, [log])) == []


def test_trc721_transfer_with_four_topics_ignored():
    log = transfer_log(OTHER, WALLET, 1)
    log["topics"].append("0" * 63 + "1")
    assert parse_transfer_logs(tx_info(10, [log])) == []


def test_malformed_log_is_skipped_but_valid_log_kept():
    bad = transfer_log(OTHER, WALLET, 1_000_087)
    bad["data"] = "zz"
    good = transfer_log(WALLET, OTHER, 1_000_000)
    transfers = parse_transfer_logs(tx_info(11, [bad, good]))
    assert len(transfers) == 1 and transfers[0].amount_raw == 1_000_000


def test_malformed_tx_info_envelope():
    with pytest.raises(MalformedTransactionError):
        parse_transfer_logs({"id": "nope", "log": []})
    with pytest.raises(MalformedTransactionError):
        parse_transfer_logs({"id": tx_hash(1), "log": "x"})
    with pytest.raises(MalformedTransactionError):
        parse_transfer_logs({"id": tx_hash(1), "log": [transfer_log(OTHER, WALLET, 1)]})  # no timestamp
