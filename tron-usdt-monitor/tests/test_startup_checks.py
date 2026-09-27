import pytest

from app.startup_checks import StartupCheckError, run_startup_checks
from app.tron_client import MAINNET_GENESIS_BLOCK_ID, TronApiError
from tests.helpers import make_settings, run


class FakeTron:
    def __init__(self, symbol="USDT", decimals=6, genesis=MAINNET_GENESIS_BLOCK_ID, meta_error=None):
        self.symbol, self.decimals, self.genesis, self.meta_error = symbol, decimals, genesis, meta_error

    async def get_block_id(self, n):
        return self.genesis

    async def get_token_metadata(self, contract, owner):
        if self.meta_error:
            raise self.meta_error
        return self.symbol, self.decimals


def test_official_contract_passes():
    assert run(run_startup_checks(make_settings(), FakeTron())) == []


def test_non_usdt_contract_is_fatal():
    settings = make_settings(USDT_CONTRACT="TLa2f6VPqDgRE67v1736s7bJ8Ray5wYjU7")
    with pytest.raises(StartupCheckError):
        run(run_startup_checks(settings, FakeTron(symbol="XYZ", decimals=18)))


def test_missing_contract_is_fatal():
    err = TronApiError("contract call symbol() failed: No contract", retryable=False)
    with pytest.raises(StartupCheckError):
        run(run_startup_checks(make_settings(), FakeTron(meta_error=err)))


def test_api_outage_is_not_fatal():
    warnings = run(run_startup_checks(make_settings(), FakeTron(meta_error=TronApiError("timeout"))))
    assert warnings == []


def test_non_official_contract_and_testnet_warn():
    settings = make_settings(USDT_CONTRACT="TLa2f6VPqDgRE67v1736s7bJ8Ray5wYjU7")
    warnings = run(run_startup_checks(settings, FakeTron(genesis="00" * 32)))
    assert any("mainnet" in w for w in warnings) and any("official" in w for w in warnings)
