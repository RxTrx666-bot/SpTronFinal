"""Decoder / filtering tests: USDT-only, TRX & other-token rejection, sender/recipient, precision."""

from decimal import Decimal

import pytest

from app.amounts import fmt_usdt, raw_to_usdt, usdt_to_raw
from app.collector.address import base58_to_hex, hex_to_base58, is_valid_tron_address
from app.collector.event_parser import EventRejected, MalformedEvent, parse_events, parse_transfer_event
from app.config.settings import OFFICIAL_USDT_TRC20_CONTRACT as USDT
from app.simulation.source import make_event
from tests.helpers import addr

OTHER = addr("other-token-contract")


def usdt_event(amount="5", **kw):
    return make_event(sender=addr("A"), recipient=addr("B"), amount_usdt=amount, ts_ms=1_758_600_000_000, contract=USDT, **kw)


# 1. USDT-only filtering
def test_usdt_transfer_is_accepted():
    ev = parse_transfer_event(usdt_event(), USDT, confirmed=True)
    assert ev.token_contract == USDT
    assert ev.amount_raw == 5_000_000
    assert ev.confirmed is True


# 2. TRX rejection
def test_trx_transfer_is_rejected():
    trx = {
        "transaction_id": "ab" * 32,
        "block_timestamp": 1_758_600_000_000,
        "type": "TransferContract",
        "amount": 5_000_000,
        "owner_address": addr("A"),
        "to_address": addr("B"),
    }
    with pytest.raises(EventRejected) as e:
        parse_transfer_event(trx, USDT, confirmed=True)
    assert e.value.reason == "no_contract_address"


# 3. Other-token rejection (other TRC-20, e.g. USDC/USDD, or a TRC-10 payload)
def test_other_trc20_token_is_rejected():
    raw = make_event(sender=addr("A"), recipient=addr("B"), amount_usdt=5, ts_ms=1_758_600_000_000, contract=OTHER)
    with pytest.raises(EventRejected) as e:
        parse_transfer_event(raw, USDT, confirmed=True)
    assert e.value.reason == "not_usdt_contract"


def test_non_transfer_usdt_event_is_rejected():
    raw = usdt_event()
    raw["event_name"] = "Approval"
    with pytest.raises(EventRejected):
        parse_transfer_event(raw, USDT, confirmed=True)


def test_trc10_style_payload_rejected_in_batch():
    events, rej = parse_events(
        [usdt_event(), {"asset_name": "1002000", "amount": 5}, make_event(
            sender=addr("A"), recipient=addr("B"), amount_usdt=1, ts_ms=1, contract=OTHER)],
        USDT,
        confirmed=True,
    )
    assert len(events) == 1
    assert rej == {"no_contract_address": 1, "not_usdt_contract": 1}


# 4 + 5. Correct sender / recipient detection (hex -> base58)
def test_sender_and_recipient_decoded_from_hex():
    ev = parse_transfer_event(usdt_event(), USDT, confirmed=True)
    assert ev.sender == addr("A")
    assert ev.recipient == addr("B")
    assert ev.sender != ev.recipient
    raw = usdt_event()
    assert raw["result"]["from"].startswith("0x")  # really decoded, not copied


def test_positional_result_keys_supported():
    raw = usdt_event()
    r = raw["result"]
    raw["result"] = {"0": r["from"], "1": r["to"], "2": r["value"]}
    ev = parse_transfer_event(raw, USDT, confirmed=True)
    assert (ev.sender, ev.recipient, ev.amount_raw) == (addr("A"), addr("B"), 5_000_000)


def test_address_round_trip_and_official_contract():
    assert is_valid_tron_address(USDT)
    h = base58_to_hex(USDT)
    assert h == "41a614f803b6fd780986a42c78ec9c7f77e6ded13c"
    assert hex_to_base58("0x" + h[2:]) == USDT
    assert not is_valid_tron_address("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6X")  # bad checksum


# malformed payloads never crash the batch
@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(result=None),
        lambda r: r["result"].update(value="not-a-number"),
        lambda r: r.update(transaction_id="xyz"),
        lambda r: r["result"].update({"from": "0x1234"}),
        lambda r: r.update(block_timestamp=None),
        lambda r: r["result"].update(value=str(2**200)),
    ],
)
def test_malformed_events_are_skipped(mutate):
    raw = usdt_event()
    mutate(raw)
    with pytest.raises(MalformedEvent):
        parse_transfer_event(raw, USDT, confirmed=True)
    good, rej = parse_events([raw, usdt_event(), "garbage", None], USDT, confirmed=True)
    assert len(good) == 1 and rej["malformed"] == 3


def test_zero_value_transfer_rejected():
    with pytest.raises(EventRejected):
        parse_transfer_event(usdt_event("0"), USDT, confirmed=True)


def test_unconfirmed_flag_wins():
    ev = parse_transfer_event(usdt_event(unconfirmed=True), USDT, confirmed=True)
    assert ev.confirmed is False


# 26. Decimal precision
def test_decimal_precision_is_exact():
    ev = parse_transfer_event(usdt_event("0.000001"), USDT, confirmed=True)
    assert ev.amount_raw == 1
    assert raw_to_usdt(1) == Decimal("0.000001")
    big = usdt_to_raw("123456789.123456")
    assert big == 123_456_789_123_456
    assert raw_to_usdt(big) == Decimal("123456789.123456")
    # values that would lose precision as float64
    v = usdt_to_raw("9007199254.740993")
    assert v == 9_007_199_254_740_993
    assert str(raw_to_usdt(v)) == "9007199254.740993"
    assert fmt_usdt(20_000_000_000) == "20,000"
    assert fmt_usdt(1) == "0.000001"
    with pytest.raises(ValueError):
        usdt_to_raw("0.0000001")
