"""Configuration loaded from environment variables / .env. Secrets are never hardcoded."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.filters import Direction, format_token_amount, parse_token_amount
from app.tron_address import is_valid_address

OFFICIAL_USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
DEFAULT_WALLET = "TWkvffFDMsqbmTLkMHMABmw452Hyq98cdn"
USDT_DECIMALS = 6
MONITOR_MODES = ("account", "blocks")


class ConfigError(Exception):
    """Invalid or missing configuration."""


class _Env:
    def __init__(self, env: Mapping[str, str]) -> None:
        self.env = env

    def str(self, name: str, default: str | None = None, required: bool = False) -> str:
        value = self.env.get(name)
        value = value.strip() if isinstance(value, str) else None
        if not value:
            if required:
                raise ConfigError(f"{name} is required")
            return default if default is not None else ""
        return value

    def bool(self, name: str, default: bool) -> bool:
        value = self.str(name)
        if not value:
            return default
        lowered = value.lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise ConfigError(f"{name} must be true/false, got {value!r}")

    def int(self, name: str, default: int, minimum: int | None = None) -> int:
        value = self.str(name)
        try:
            number = int(value) if value else default
        except ValueError as exc:
            raise ConfigError(f"{name} must be an integer") from exc
        if minimum is not None and number < minimum:
            raise ConfigError(f"{name} must be >= {minimum}")
        return number

    def float(self, name: str, default: float, minimum: float | None = None) -> float:
        value = self.str(name)
        try:
            number = float(value) if value else default
        except ValueError as exc:
            raise ConfigError(f"{name} must be a number") from exc
        if minimum is not None and number < minimum:
            raise ConfigError(f"{name} must be >= {minimum}")
        return number


@dataclass(frozen=True)
class Settings:
    # TRON API
    tron_api_url: str
    tron_api_key: str
    tron_api_key_header: str
    tron_request_timeout: float
    tron_max_retries: int
    # Telegram
    telegram_bot_token: str
    telegram_admin_chat_ids: tuple[int, ...]
    # What to monitor
    wallet_address: str
    usdt_contract: str
    token_symbol: str
    token_decimals: int
    min_raw: int
    max_raw: int
    alert_directions: frozenset[Direction]
    tx_limit_threshold: int
    pause_on_limit: bool
    start_paused: bool
    allow_telegram_start: bool
    wallet_created_notice: bool
    gpu_type: str
    # Monitoring behaviour
    monitor_mode: str
    poll_interval_seconds: float
    confirmed_only: bool
    verify_event_log: bool
    verify_timeout_seconds: int
    lookback_seconds: int
    max_pages_per_poll: int
    max_blocks_per_poll: int
    block_fetch_concurrency: int
    verify_contract_on_startup: bool
    # Backfill
    backfill_enabled: bool
    backfill_limit: int
    backfill_max_pages: int
    # Misc
    timezone_name: str
    database_url: str
    log_level: str
    log_format: str
    notify_on_startup: bool
    heartbeat_file: str
    telegram_api_url: str = "https://api.telegram.org"
    tronscan_tx_url: str = "https://tronscan.org/#/transaction/"
    timezone: ZoneInfo = field(default=ZoneInfo("UTC"), compare=False)

    @property
    def min_usdt(self) -> str:
        return format_token_amount(self.min_raw, self.token_decimals)

    @property
    def max_usdt(self) -> str:
        return format_token_amount(self.max_raw, self.token_decimals)

    @property
    def telegram_admin_chat_id(self) -> int:
        """First admin chat (kept for backwards compatibility)."""
        return self.telegram_admin_chat_ids[0]

    @property
    def outgoing_only(self) -> bool:
        return Direction.INCOMING not in self.alert_directions

    @property
    def directions_label(self) -> str:
        if self.outgoing_only:
            return "OUTGOING only ⬆️"
        if Direction.OUTGOING not in self.alert_directions:
            return "INCOMING only ⬇️"
        return "INCOMING ⬇️ + OUTGOING ⬆️"

    @property
    def tron_api_host(self) -> str:
        return urlparse(self.tron_api_url).netloc

    def secrets(self) -> list[str]:
        """Values that must never appear in logs or Telegram messages."""
        values = [self.telegram_bot_token, self.tron_api_key]
        # Some providers (QuickNode, GetBlock, ...) embed the key in the URL path.
        path = urlparse(self.tron_api_url).path.strip("/")
        if path:
            values.extend(part for part in path.split("/") if len(part) >= 16)
            values.append(self.tron_api_url.rstrip("/"))
        query = urlparse(self.tron_api_url).query
        if query:
            values.append(query)
        return [v for v in values if v and len(v) >= 6]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        e = _Env(os.environ if env is None else env)

        wallet = e.str("WALLET_ADDRESS", DEFAULT_WALLET)
        if not is_valid_address(wallet):
            raise ConfigError("WALLET_ADDRESS is not a valid TRON mainnet address")
        contract = e.str("USDT_CONTRACT", OFFICIAL_USDT_CONTRACT)
        if not is_valid_address(contract):
            raise ConfigError("USDT_CONTRACT is not a valid TRON mainnet address")

        decimals = USDT_DECIMALS
        try:
            min_raw = parse_token_amount(e.str("MIN_USDT", "1.000000"), decimals)
            max_raw = parse_token_amount(e.str("MAX_USDT", "1.000100"), decimals)
        except ValueError as exc:
            raise ConfigError(f"MIN_USDT/MAX_USDT: {exc}") from exc
        if min_raw > max_raw:
            raise ConfigError("MIN_USDT must be <= MAX_USDT")

        # One or more chat ids, comma-separated: every id receives all alerts and may use commands.
        chat_ids: list[int] = []
        for part in e.str("TELEGRAM_ADMIN_CHAT_ID", required=True).split(","):
            part = part.strip()
            if not part:
                continue
            try:
                chat_id = int(part)
            except ValueError as exc:
                raise ConfigError("TELEGRAM_ADMIN_CHAT_ID must be integer chat id(s), comma-separated") from exc
            if chat_id not in chat_ids:
                chat_ids.append(chat_id)
        if not chat_ids:
            raise ConfigError("TELEGRAM_ADMIN_CHAT_ID is required")

        directions: set[Direction] = set()
        for part in e.str("ALERT_DIRECTIONS", "OUTGOING").upper().replace(" ", "").split(","):
            if part not in ("INCOMING", "OUTGOING"):
                raise ConfigError("ALERT_DIRECTIONS must be OUTGOING, INCOMING or OUTGOING,INCOMING")
            directions.add(Direction(part))

        mode = e.str("MONITOR_MODE", "account").lower()
        if mode not in MONITOR_MODES:
            raise ConfigError(f"MONITOR_MODE must be one of {MONITOR_MODES}")

        tz_name = e.str("TIMEZONE", "UTC")
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(f"TIMEZONE {tz_name!r} is not a valid IANA timezone") from exc

        api_url = e.str("TRON_API_URL", "https://api.trongrid.io").rstrip("/")
        if urlparse(api_url).scheme not in ("http", "https"):
            raise ConfigError("TRON_API_URL must start with http:// or https://")

        log_format = e.str("LOG_FORMAT", "text").lower()
        if log_format not in ("text", "json"):
            raise ConfigError("LOG_FORMAT must be text or json")

        return cls(
            tron_api_url=api_url,
            tron_api_key=e.str("TRON_API_KEY", ""),
            tron_api_key_header=e.str("TRON_API_KEY_HEADER", "TRON-PRO-API-KEY"),
            tron_request_timeout=e.float("TRON_REQUEST_TIMEOUT", 10.0, 1.0),
            tron_max_retries=e.int("TRON_MAX_RETRIES", 4, 0),
            telegram_bot_token=e.str("TELEGRAM_BOT_TOKEN", required=True),
            telegram_admin_chat_ids=tuple(chat_ids),
            wallet_address=wallet,
            usdt_contract=contract,
            token_symbol="USDT",
            token_decimals=decimals,
            min_raw=min_raw,
            max_raw=max_raw,
            alert_directions=frozenset(directions),
            tx_limit_threshold=e.int("TX_LIMIT_THRESHOLD", 150, 0),
            pause_on_limit=e.bool("PAUSE_ON_LIMIT", True),
            start_paused=e.bool("START_PAUSED", True),
            allow_telegram_start=e.bool("ALLOW_TELEGRAM_START", False),
            wallet_created_notice=e.bool("WALLET_CREATED_NOTICE", True),
            gpu_type=e.env.get("GPU_TYPE", "RTX 4090").strip(),
            monitor_mode=mode,
            poll_interval_seconds=e.float("POLL_INTERVAL_SECONDS", 2.0, 0.2),
            confirmed_only=e.bool("CONFIRMED_ONLY", False),
            verify_event_log=e.bool("VERIFY_EVENT_LOG", True),
            verify_timeout_seconds=e.int("VERIFY_TIMEOUT_SECONDS", 60, 1),
            lookback_seconds=e.int("LOOKBACK_SECONDS", 120, 0),
            max_pages_per_poll=e.int("MAX_PAGES_PER_POLL", 20, 1),
            max_blocks_per_poll=e.int("MAX_BLOCKS_PER_POLL", 60, 1),
            block_fetch_concurrency=e.int("BLOCK_FETCH_CONCURRENCY", 3, 1),
            verify_contract_on_startup=e.bool("VERIFY_CONTRACT_ON_STARTUP", True),
            backfill_enabled=e.bool("BACKFILL_ENABLED", False),
            backfill_limit=e.int("BACKFILL_LIMIT", 100, 1),
            backfill_max_pages=e.int("BACKFILL_MAX_PAGES", 50, 1),
            timezone_name=tz_name,
            timezone=tz,
            database_url=e.str("DATABASE_URL", "sqlite:///data/monitor.db"),
            log_level=e.str("LOG_LEVEL", "INFO").upper(),
            log_format=log_format,
            notify_on_startup=e.bool("NOTIFY_ON_STARTUP", False),
            heartbeat_file=e.str("HEARTBEAT_FILE", "data/heartbeat"),
            telegram_api_url=e.str("TELEGRAM_API_URL", "https://api.telegram.org").rstrip("/"),
        )
