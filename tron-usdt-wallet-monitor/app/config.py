"""Configuration from environment / ``.env``.

Secrets are held as ``SecretStr`` so they never show up in reprs, and are
registered with the log redactor at startup.
"""

from __future__ import annotations

from decimal import Decimal
from functools import cached_property

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.amounts import usdt_to_base
from app.tron.address import is_valid_tron_address

# Official Tether USDT TRC-20 contract on TRON mainnet.
OFFICIAL_USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ------------------------------------------------------------- TRON API
    tron_api_url: str = "https://api.trongrid.io"
    tron_api_key: SecretStr = SecretStr("")
    tron_request_timeout_seconds: float = 15.0
    tron_max_retries: int = 5
    tron_retry_base_seconds: float = 0.5
    tron_retry_max_seconds: float = 30.0
    # Global limits shared by every API caller (stream, scheduler, backfill).
    max_concurrent_api_requests: int = Field(default=5, ge=1)
    max_requests_per_second: float = Field(default=10.0, gt=0)

    # ------------------------------------------------------------- Target
    root_wallet: str
    usdt_contract: str = OFFICIAL_USDT_CONTRACT
    usdt_decimals: int = Field(default=6, ge=0, le=30)

    # ------------------------------------------------------------- Rules
    alert_min_amount_usdt: Decimal = Decimal("500")
    max_hops: int = Field(default=1, ge=1)
    alert_on_discovery: bool = False
    alert_on_self_transfer: bool = False
    # Alert on transfers that happened before monitoring started (backfill).
    alert_on_historical: bool = False

    # ------------------------------------------------------------- Live stream
    poll_interval_seconds: float = Field(default=2.0, gt=0)
    # false = fastest: process events as soon as TronGrid sees them (unconfirmed).
    # true  = only solidified (irreversible) events; ~1 minute slower.
    require_confirmed: bool = False
    stream_overlap_seconds: int = Field(default=30, ge=0)
    stream_page_limit: int = Field(default=200, ge=1, le=200)
    stream_max_pages_per_poll: int = Field(default=50, ge=1)

    # ------------------------------------------------------------- Scheduler
    reconcile_enabled: bool = True
    # Minimum gap between two full sweeps over all monitored wallets.
    reconcile_interval_seconds: int = Field(default=300, ge=10)
    # Workers used by the sweep (each holds at most one API request).
    reconcile_workers: int = Field(default=2, ge=1)
    reconcile_overlap_seconds: int = Field(default=120, ge=0)

    # ------------------------------------------------------------- Backfill
    backfill_enabled: bool = False
    backfill_lookback_days: int = Field(default=30, ge=0)
    backfill_window_hours: int = Field(default=24, ge=1)

    # ------------------------------------------------------------- Telegram
    telegram_bot_token: SecretStr
    telegram_admin_chat_id: str
    telegram_commands_enabled: bool = True
    telegram_api_url: str = "https://api.telegram.org"
    telegram_min_send_interval_seconds: float = 1.1
    send_startup_message: bool = True
    wallets_page_size: int = Field(default=20, ge=1, le=50)

    # ------------------------------------------------------------- Database
    database_url: str
    db_auto_migrate: bool = True
    db_pool_size: int = 10

    # ------------------------------------------------------------- Ops
    health_host: str = "0.0.0.0"
    health_port: int = 8080
    health_max_stall_seconds: int = 120
    log_level: str = "INFO"
    log_format: str = "text"

    # ------------------------------------------------------------- validation
    @field_validator("root_wallet", "usdt_contract")
    @classmethod
    def _valid_address(cls, v: str) -> str:
        v = v.strip()
        if not is_valid_tron_address(v):
            raise ValueError(f"not a valid TRON address: {v!r}")
        return v

    @field_validator("telegram_admin_chat_id")
    @classmethod
    def _chat_id(cls, v: str) -> str:
        v = str(v).strip()
        if not v.lstrip("-").isdigit():
            raise ValueError("TELEGRAM_ADMIN_CHAT_ID must be a numeric chat id")
        return v

    @field_validator("database_url")
    @classmethod
    def _asyncpg(cls, v: str) -> str:
        v = v.strip()
        for prefix in ("postgresql://", "postgres://"):
            if v.startswith(prefix):
                return "postgresql+asyncpg://" + v[len(prefix) :]
        return v

    @field_validator("alert_min_amount_usdt", mode="before")
    @classmethod
    def _no_float(cls, v):
        # Keep the exact decimal text the user wrote (never via float).
        return Decimal(str(v).strip()) if not isinstance(v, Decimal) else v

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        usdt_to_base(self.alert_min_amount_usdt, self.usdt_decimals)  # raises if too precise
        if self.alert_min_amount_usdt <= 0:
            raise ValueError("ALERT_MIN_AMOUNT_USDT must be positive")
        return self

    # ------------------------------------------------------------- derived
    @cached_property
    def alert_min_base_units(self) -> int:
        return usdt_to_base(self.alert_min_amount_usdt, self.usdt_decimals)

    @property
    def is_official_usdt(self) -> bool:
        return self.usdt_contract == OFFICIAL_USDT_CONTRACT

    def secrets(self) -> list[str]:
        out = [self.tron_api_key.get_secret_value(), self.telegram_bot_token.get_secret_value()]
        from urllib.parse import urlsplit

        try:
            pw = urlsplit(self.database_url).password
        except ValueError:
            pw = None
        if pw:
            out.append(pw)
        return [s for s in out if s]
