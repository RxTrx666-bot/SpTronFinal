"""Configuration loaded from environment variables (and an optional ``.env`` file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from outflow_alert.address import InvalidAddress, base58_to_hex, normalize_address
from outflow_alert.amounts import USDT_DECIMALS, format_units, parse_amount_list, to_base_units

DEFAULT_WALLET = "TWkvffFDMsqbmTLkMHMABmw452Hyq98cdn"
DEFAULT_USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    tron_api_key: str
    telegram_bot_token: str
    telegram_chat_ids: tuple[str, ...]
    wallet: str = DEFAULT_WALLET
    usdt_contract: str = DEFAULT_USDT_CONTRACT
    token_decimals: int = USDT_DECIMALS
    amounts_raw: tuple[int, ...] = (1_010_000, 10_000_000, 20_000_000, 30_000_000)
    tolerance_raw: int = 100
    poll_interval_seconds: float = 2.0
    lookback_seconds: int = 120
    initial_backfill_seconds: int = 0
    page_limit: int = 200
    max_pages_per_poll: int = 25
    verify_max_age_seconds: int = 3600
    confirmation_mode: str = "fast"  # "fast" (/wallet) or "solidified" (/walletsolidity)
    trongrid_base_url: str = "https://api.trongrid.io"
    http_timeout_seconds: float = 10.0
    db_path: str = "data/outflow_alert.db"
    heartbeat_file: str = ""
    log_level: str = "INFO"
    explorer_tx_url: str = "https://tronscan.org/#/transaction/{txid}"
    explorer_address_url: str = "https://tronscan.org/#/address/{address}"

    @property
    def wallet_hex(self) -> str:
        return base58_to_hex(self.wallet)

    def describe(self) -> str:
        amounts = ", ".join(format_units(a, self.token_decimals) for a in self.amounts_raw)
        return (
            f"wallet={self.wallet} contract={self.usdt_contract} amounts=[{amounts}] "
            f"tolerance={format_units(self.tolerance_raw, self.token_decimals)} "
            f"poll={self.poll_interval_seconds}s lookback={self.lookback_seconds}s "
            f"mode={self.confirmation_mode} chats={len(self.telegram_chat_ids)} db={self.db_path}"
        )


def _get(env: dict[str, str], key: str, default: str | None = None) -> str | None:
    value = env.get(key)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _int(env: dict[str, str], key: str, default: int, minimum: int = 0) -> int:
    raw = _get(env, key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}")
    return value


def _seconds(env: dict[str, str], key: str, default: float, minimum: float) -> float:
    # Time intervals only - token amounts never go through float.
    raw = _get(env, key)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}")
    return value


def load_settings(env: dict[str, str] | None = None, env_file: str | Path | None = ".env") -> Settings:
    if env is None:
        if env_file and Path(env_file).is_file():
            from dotenv import load_dotenv

            load_dotenv(env_file, override=False)
        env = dict(os.environ)

    errors: list[str] = []

    api_key = _get(env, "TRON_API_KEY")
    if not api_key:
        errors.append("TRON_API_KEY is required")
    token = _get(env, "TELEGRAM_BOT_TOKEN")
    if not token:
        errors.append("TELEGRAM_BOT_TOKEN is required")
    chats = tuple(c.strip() for c in (_get(env, "TELEGRAM_CHAT_ID") or "").split(",") if c.strip())
    if not chats:
        errors.append("TELEGRAM_CHAT_ID is required (comma-separated for several chats)")

    try:
        wallet = normalize_address(_get(env, "WALLET_ADDRESS", DEFAULT_WALLET))
    except InvalidAddress as exc:
        errors.append(f"WALLET_ADDRESS: {exc}")
        wallet = DEFAULT_WALLET
    try:
        contract = normalize_address(_get(env, "USDT_CONTRACT", DEFAULT_USDT_CONTRACT))
    except InvalidAddress as exc:
        errors.append(f"USDT_CONTRACT: {exc}")
        contract = DEFAULT_USDT_CONTRACT

    amounts_raw: tuple[int, ...] = ()
    tolerance_raw = 0
    try:
        amounts_raw = parse_amount_list(_get(env, "AMOUNTS", "1.01,10,20,30"), USDT_DECIMALS)
    except ValueError as exc:
        errors.append(f"AMOUNTS: {exc}")
    try:
        tolerance_raw = to_base_units(_get(env, "AMOUNT_TOLERANCE", "0.000100"), USDT_DECIMALS)
    except ValueError as exc:
        errors.append(f"AMOUNT_TOLERANCE: {exc}")

    mode = (_get(env, "CONFIRMATION_MODE", "fast") or "fast").lower()
    if mode not in ("fast", "solidified"):
        errors.append("CONFIRMATION_MODE must be 'fast' or 'solidified'")

    try:
        settings = Settings(
            tron_api_key=api_key or "",
            telegram_bot_token=token or "",
            telegram_chat_ids=chats,
            wallet=wallet,
            usdt_contract=contract,
            amounts_raw=amounts_raw,
            tolerance_raw=tolerance_raw,
            poll_interval_seconds=_seconds(env, "POLL_INTERVAL_SECONDS", 2.0, 0.5),
            lookback_seconds=_int(env, "LOOKBACK_SECONDS", 120, 0),
            initial_backfill_seconds=_int(env, "INITIAL_BACKFILL_SECONDS", 0, 0),
            page_limit=min(_int(env, "PAGE_LIMIT", 200, 1), 200),
            max_pages_per_poll=_int(env, "MAX_PAGES_PER_POLL", 25, 1),
            verify_max_age_seconds=_int(env, "VERIFY_MAX_AGE_SECONDS", 3600, 60),
            confirmation_mode=mode,
            trongrid_base_url=(_get(env, "TRONGRID_BASE_URL", "https://api.trongrid.io") or "").rstrip("/"),
            http_timeout_seconds=_seconds(env, "HTTP_TIMEOUT_SECONDS", 10.0, 1.0),
            db_path=_get(env, "DB_PATH", "data/outflow_alert.db"),
            heartbeat_file=_get(env, "HEARTBEAT_FILE", "") or "",
            log_level=(_get(env, "LOG_LEVEL", "INFO") or "INFO").upper(),
        )
    except ConfigError as exc:
        errors.append(str(exc))
        settings = None  # type: ignore[assignment]

    if errors:
        raise ConfigError("; ".join(errors))
    return settings
