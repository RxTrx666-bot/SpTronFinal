"""Startup verification of the network and the token contract, done against the chain itself."""

from __future__ import annotations

import logging

from app.config import OFFICIAL_USDT_CONTRACT, Settings
from app.logger import kv
from app.tron_client import MAINNET_GENESIS_BLOCK_ID, TronApiError, TronClient

log = logging.getLogger(__name__)


class StartupCheckError(Exception):
    """Configuration contradicts on-chain data; monitoring would be meaningless."""


async def run_startup_checks(settings: Settings, client: TronClient) -> list[str]:
    """Returns non-fatal warnings. Raises StartupCheckError on a definitive mismatch.

    API outages are *not* fatal: they are logged and monitoring starts anyway
    (the monitor itself keeps retrying).
    """
    warnings: list[str] = []

    try:
        genesis = await client.get_block_id(0)
        if genesis.lower() != MAINNET_GENESIS_BLOCK_ID:
            warnings.append("TRON_API_URL does not look like TRON mainnet (genesis block mismatch)")
            log.error("network_check_failed_not_mainnet", extra=kv(genesis=genesis))
        else:
            log.info("network_check_ok", extra=kv(network="TRON mainnet", api_host=settings.tron_api_host))
    except TronApiError as exc:
        log.warning("network_check_skipped", extra=kv(error=str(exc)))

    if settings.usdt_contract != OFFICIAL_USDT_CONTRACT:
        msg = (
            f"USDT_CONTRACT {settings.usdt_contract} is not the official Tether USDT contract "
            f"({OFFICIAL_USDT_CONTRACT})"
        )
        warnings.append(msg)
        log.warning("non_official_usdt_contract", extra=kv(configured=settings.usdt_contract))

    if settings.verify_contract_on_startup:
        try:
            symbol, decimals = await client.get_token_metadata(settings.usdt_contract, settings.wallet_address)
        except TronApiError as exc:
            if exc.retryable:
                log.warning("contract_check_skipped_api_unavailable", extra=kv(error=str(exc)))
                return warnings
            raise StartupCheckError(
                f"USDT_CONTRACT {settings.usdt_contract} is not a callable TRC-20 contract on this network: {exc}"
            ) from exc
        if symbol != settings.token_symbol or decimals != settings.token_decimals:
            raise StartupCheckError(
                f"USDT_CONTRACT {settings.usdt_contract} reports symbol={symbol!r} decimals={decimals}; "
                f"expected {settings.token_symbol!r} with {settings.token_decimals} decimals"
            )
        log.info("contract_check_ok", extra=kv(contract=settings.usdt_contract, symbol=symbol, decimals=decimals))
    return warnings
