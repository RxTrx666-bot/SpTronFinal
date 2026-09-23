"""Application configuration.

Every value can be overridden through environment variables or a ``.env`` file.
Secrets (API keys, bot token, database password) are never hard-coded.

IMPORTANT: none of these settings defines a *global test amount*.  Test amounts
are learned independently for every Sender -> Recipient relationship by the
pattern engine.  ``MIN_LARGE_TO_TEST_RATIO`` only decides whether a follow-up
transfer is "substantially larger" than the transfer that preceded it.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain import ConfidenceLevel

# Official Tether USDT TRC-20 contract on TRON mainnet.
OFFICIAL_USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ TRON
    tron_api_url: str = "https://api.trongrid.io"
    tron_api_key: str = ""
    tron_request_timeout_seconds: float = 15.0
    tron_max_retries: int = 6
    tron_retry_base_seconds: float = 1.0
    tron_retry_max_seconds: float = 30.0
    tron_page_limit: int = Field(200, ge=1, le=200)

    usdt_contract_address: str = OFFICIAL_USDT_TRC20_CONTRACT
    usdt_decimals: int = 6

    # ------------------------------------------------------------ Database
    database_url: str = "postgresql+asyncpg://tron:tron@localhost:5432/tron_usdt"
    database_pool_size: int = 10
    auto_migrate: bool = True

    # ------------------------------------------------------------ Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_commands_enabled: bool = True
    telegram_timeout_seconds: float = 15.0
    alert_max_attempts: int = 40
    alert_retry_base_seconds: float = 1.0
    alert_retry_max_seconds: float = 60.0
    send_startup_message: bool = True

    # ----------------------------------------------------------- Collector
    poll_interval_seconds: float = 1.0
    confirmed_overlap_seconds: int = 6
    max_pages_per_poll: int = 25
    enable_unconfirmed: bool = True
    unconfirmed_poll_interval_seconds: float = 1.0
    unconfirmed_lookback_seconds: int = 120
    unconfirmed_drop_after_minutes: int = 10
    send_confirmation_alerts: bool = False
    dedup_cache_size: int = 200_000

    # ------------------------------------------------------------ Backfill
    initial_history_days: int = 30
    backfill_window_seconds: int = 300
    backfill_concurrency: int = 3
    backfill_notify_individual: bool = False

    # ------------------------------------------------ Behavioural analysis
    min_successful_sequences: int = Field(3, ge=1)
    candidate_min_sequences: int = Field(2, ge=1)
    min_large_to_test_ratio: Decimal = Decimal("10")
    max_followup_hours: float = 168.0
    min_pattern_confidence: ConfidenceLevel = ConfidenceLevel.HIGH
    confidence_high_threshold: float = 0.75
    confidence_medium_threshold: float = 0.50
    min_success_rate: float = 0.5
    min_inlier_fraction: float = 0.5
    # Relative clustering: sequences whose test amount is within this factor of
    # the relationship's own learned test centre are treated as the same test
    # behaviour.  This is RELATIVE to each relationship, never absolute.
    test_cluster_band_factor: float = 3.0
    # How far outside the relationship's learned [min, max] test range a new
    # transfer may be and still match (2.0 => from min/2 up to max*2).  Relative
    # to each relationship; the upper bound is also capped at
    # (smallest learned large) / MIN_LARGE_TO_TEST_RATIO.
    test_match_factor: float = Field(2.0, ge=1.0)
    # Pure noise floor for zero-ish dust (address-poisoning style transfers).
    # This is NOT a test-amount range; it only discards sub-cent noise.
    dust_floor_usdt: Decimal = Decimal("0.01")
    pattern_half_life_days: float = 30.0
    pattern_lookback_days: int = 120
    pattern_expiry_days: int = 60
    pair_history_limit: int = 1000
    followup_window_multiplier: float = 3.0
    min_followup_window_minutes: int = 30
    alert_on_weakened: bool = False

    # ------------------------------------------------------------- Alerting
    alert_max_tx_age_minutes: int = 60
    flood_max_tests_per_hour: int = 6
    flood_pause_minutes: int = 60
    notify_new_patterns: bool = True

    # --------------------------------------------------------- Maintenance
    analysis_workers: int = 2
    maintenance_interval_seconds: int = 15
    watchlist_reevaluate_minutes: int = 60
    retention_days: int = 150
    heartbeat_file: str = "/tmp/tron-usdt-monitor.heartbeat"
    heartbeat_log_seconds: int = 60

    # ------------------------------------------------------------ Display
    display_timezone: str = "UTC"
    tronscan_tx_url: str = "https://tronscan.org/#/transaction/"
    log_level: str = "INFO"
    log_format: str = "text"  # text | json

    # ----------------------------------------------------------- validators
    @field_validator("usdt_contract_address")
    @classmethod
    def _validate_contract(cls, value: str) -> str:
        from app.collector.address import is_valid_tron_address

        value = value.strip()
        if not is_valid_tron_address(value):
            raise ValueError(f"USDT_CONTRACT_ADDRESS is not a valid TRON address: {value!r}")
        return value

    @field_validator("min_pattern_confidence", mode="before")
    @classmethod
    def _upper_conf(cls, value):
        return value.upper() if isinstance(value, str) else value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_level(cls, value):
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        if self.min_large_to_test_ratio <= 1:
            raise ValueError("MIN_LARGE_TO_TEST_RATIO must be > 1")
        if self.candidate_min_sequences > self.min_successful_sequences:
            self.candidate_min_sequences = self.min_successful_sequences
        if self.pattern_lookback_days * 24 < self.max_followup_hours:
            raise ValueError("PATTERN_LOOKBACK_DAYS must cover MAX_FOLLOWUP_HOURS")
        return self

    # ------------------------------------------------------------ helpers
    @property
    def ratio_fraction(self) -> Fraction:
        """Exact rational form of MIN_LARGE_TO_TEST_RATIO (no float rounding)."""
        return Fraction(self.min_large_to_test_ratio)

    @property
    def dust_floor_raw(self) -> int:
        return int(self.dust_floor_usdt.scaleb(self.usdt_decimals).to_integral_value())

    @property
    def is_official_usdt_contract(self) -> bool:
        return self.usdt_contract_address == OFFICIAL_USDT_TRC20_CONTRACT

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
