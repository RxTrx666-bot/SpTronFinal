"""Application configuration.

Every value can be set through an environment variable (case-insensitive) or a
``.env`` file.  Secrets are never hard-coded and never logged.

Thresholds and risk weights are intentionally configuration, not code, so the
detection behaviour can be tuned without touching the source.
"""

from __future__ import annotations

import json
from functools import cached_property
from typing import Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.utils.address import InvalidAddress, normalize_address
from app.utils.amounts import parse_token_amount

# Official Tether USDT TRC-20 contract on TRON mainnet.
OFFICIAL_USDT_TRC20 = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

# Default risk weights (points).  Override any subset with RISK_WEIGHTS='{"new_recipient": 25}'.
DEFAULT_RISK_WEIGHTS: dict[str, int] = {
    # --- positive signals --------------------------------------------------
    "legit_used_2plus": 10,  # legitimate recipient used >= 2 times
    "legit_used_5plus": 4,  # additional, >= 5 times
    "legit_used_10plus": 3,  # additional, >= 10 times
    "legit_substantial_volume": 5,  # victim -> legit total >= LEGIT_SUBSTANTIAL_TOTAL_USDT
    "legit_recent": 5,  # legit used within LEGIT_RECENT_DAYS
    "recipient_new": 20,  # victim never paid the suspicious address before
    "suspicious_previously_flagged": 25,  # repeat payment to an address already flagged as possible poisoning
    "similarity_both_edges": 30,  # prefix AND suffix match the configured minimums
    "similarity_single_edge": 10,  # only one edge matches (needs SINGLE_EDGE_MIN_MATCH)
    "similarity_very_high": 10,  # similarity_score >= VERY_HIGH_SIMILARITY
    "amount_significant": 5,  # amount >= SIGNIFICANT_AMOUNT_USDT
    "amount_large": 3,  # amount >= LARGE_AMOUNT_USDT (additional)
    "amount_consistent_with_legit": 5,  # amount within the victim's historical range for the legit recipient
    "prior_dust_to_victim": 15,  # suspicious address sent dust / zero-value transfer involving victim
    "suspicious_multi_victim": 10,  # suspicious address dusted many different wallets
    "suspicious_paid_by_other_victims": 10,  # other watched wallets also paid the suspicious address
    "suspicious_fresh_address": 5,  # little or no history before the victim's payment
    "suspicious_many_unrelated_senders": 5,  # received from many unrelated wallets
    "suspicious_fast_forwarding": 5,  # forwarded the victim's funds quickly
    # --- negative signals (applied as subtractions) -----------------------
    "suspicious_previously_used": 40,  # victim already paid the suspicious address before
    "suspicious_established_counterparty": 25,  # victim paid it >= 3 times / large volume
    "suspicious_labeled_service": 25,  # public label says exchange/service
    "third_party_initiated": 30,  # transfer not signed by the victim (transferFrom by a spender)
    "legit_weak_relationship": 10,  # legit used only once with a small total
    "suspicious_reused_later": 30,  # (history only) victim kept paying the address repeatedly afterwards
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False)

    # ------------------------------------------------------------------ general
    simulation_mode: bool = False
    log_level: str = "INFO"
    log_format: str = Field("text", pattern="^(text|json)$")
    heartbeat_file: str = "/tmp/tron-poisoning-monitor.heartbeat"
    output_dir: str = "output"  # reports / simulation output

    # ------------------------------------------------------------------ TRON API
    tron_api_url: str = "https://api.trongrid.io"
    tron_api_key: str = ""
    tron_api_key_header: str = "TRON-PRO-API-KEY"
    tron_request_timeout_seconds: float = 15.0
    tron_max_retries: int = 5
    tron_retry_base_seconds: float = 0.5
    tron_retry_max_seconds: float = 30.0
    tron_rate_limit_rps: float = 10.0  # shared by all API calls (live calls get priority)
    tron_max_connections: int = 20
    tron_page_limit: int = Field(200, ge=1, le=200)

    # Optional public address labels (TronScan) for exchange/service attribution.
    labels_enabled: bool = True
    tronscan_api_url: str = "https://apilist.tronscanapi.com"
    tronscan_api_key: str = ""
    labels_file: str = "data/address_labels.json"
    label_cache_hours: int = 168

    # ------------------------------------------------------------------ tokens
    # Comma-separated list of SYMBOL:CONTRACT:DECIMALS.  Only USDT is monitored by default.
    tokens: str = f"USDT:{OFFICIAL_USDT_TRC20}:6"

    # ------------------------------------------------------------------ monitor
    monitor_mode: str = Field("block", pattern="^(block|account)$")
    block_poll_interval_seconds: float = 0.5  # retry interval while the next block is late
    block_arrival_margin_ms: int = 600  # first poll this long after a block is due (API propagation)
    block_prefetch: int = Field(3, ge=1, le=20)
    max_block_catchup: int = 1200  # larger gaps are filled through per-wallet history queries
    start_block_lag: int = 0  # start this many blocks behind head on first start
    account_poll_interval_seconds: float = 3.0
    account_poll_concurrency: int = 5
    account_poll_overlap_seconds: int = 60
    confirmation_check_interval_seconds: float = 10.0
    drop_unconfirmed_after_minutes: int = 10
    pending_recovery_interval_seconds: float = 30.0

    # ------------------------------------------------------------------ network-wide detection
    # Detect poisoning on ANY TRON wallet, not only wallets added with /add (block mode only).
    network_wide: bool = True
    network_memory_days: int = Field(7, ge=1, le=90)  # how long sender->recipient payments are remembered
    network_min_alert_usdt: str = "100"  # network-wide alerts only for payments of at least this amount
    network_prune_interval_minutes: int = 60
    network_trx_dust_max: str = "1"  # TRX transfers up to this many TRX count as poisoning contacts
    network_history_lookup_pages: int = Field(2, ge=1, le=10)  # x200 transfers fetched on a contact hit

    # ------------------------------------------------------------------ history
    history_days: int = 0  # 0 = as far back as the API allows
    history_max_transfers: int = 100_000  # per wallet
    history_concurrency: int = 2
    retrospective_analysis: bool = True  # analyse past transfers once the history scan completes
    notify_historical_events: bool = True  # send one summary message per wallet scan

    # ------------------------------------------------------------------ database
    database_url: str = "postgresql+asyncpg://tron:tron@localhost:5432/tron_poison"
    database_pool_size: int = 10
    auto_migrate: bool = True

    # ------------------------------------------------------------------ telegram
    telegram_bot_token: str = ""
    telegram_admin_chat_id: str = ""  # one id or comma-separated ids (user ids or chat ids)
    telegram_alert_chat_id: str = ""  # optional; defaults to the admin chat ids
    telegram_timeout_seconds: float = 15.0
    telegram_poll_timeout_seconds: int = 25
    alert_max_attempts: int = 200
    alert_retry_base_seconds: float = 1.0
    alert_retry_max_seconds: float = 120.0
    send_startup_message: bool = True
    notify_candidates: bool = False
    notify_attempts: bool = False

    # ------------------------------------------------------------------ similarity
    min_prefix_match: int = 3  # characters after the mandatory leading "T" (3 = first 4 shown incl. T)
    min_suffix_match: int = 4
    single_edge_min_match: int = 7  # prefix-only / suffix-only match must be at least this long
    min_similarity_score: float = 0.50
    very_high_similarity: float = 0.85
    similarity_prefix_window: int = 5
    similarity_suffix_window: int = 5
    similarity_weight_prefix: float = 0.40
    similarity_weight_suffix: float = 0.40
    similarity_weight_overall: float = 0.10
    similarity_weight_positional: float = 0.10
    confusable_matching: bool = True
    candidate_key_length: int = 3

    # ------------------------------------------------------------------ detection / risk
    confidence_threshold: int = Field(80, ge=1, le=100)
    candidate_threshold: int = Field(50, ge=1, le=100)
    min_legit_tx_count: int = 2  # legitimate recipient must be used this often ...
    min_legit_total_usdt: str = "1000"  # ... or have received at least this much
    legit_substantial_total_usdt: str = "10000"
    legit_recent_days: int = 180
    max_candidate_recipients: int = 2000
    min_victim_amount_usdt: str = "1"  # victim payments below this are never SUCCESSFUL
    significant_amount_usdt: str = "1000"
    large_amount_usdt: str = "10000"
    dust_max_amount_usdt: str = "1"  # incoming transfers <= this are treated as dust
    multi_victim_min: int = 3
    many_senders_min: int = 5
    fresh_address_max_prior_transfers: int = 3
    forward_window_minutes: int = 60
    forward_min_ratio_pct: int = 50
    risk_weights: str = ""  # JSON object merged over DEFAULT_RISK_WEIGHTS
    investigation_max_transfers: int = 1000

    # ------------------------------------------------------------------ tracing
    trace_enabled: bool = True
    trace_hops: int = Field(5, ge=1, le=20)
    trace_max_branches: int = Field(3, ge=1, le=20)
    trace_max_nodes: int = 60
    trace_min_amount_usdt: str = "1"
    trace_window_days: int = 30
    trace_retrace_minutes: int = 30  # periodic re-trace of recent incidents (0 = off)

    # ------------------------------------------------------------------ X
    x_enabled: bool = False
    x_api_key: str = ""
    x_api_secret: str = ""
    x_access_token: str = ""
    x_access_token_secret: str = ""
    x_api_url: str = "https://api.twitter.com"

    # ----------------------------------------------------------------- validation
    @field_validator("tokens")
    @classmethod
    def _check_tokens(cls, v: str) -> str:
        _parse_tokens(v)
        return v

    @field_validator("risk_weights")
    @classmethod
    def _check_weights(cls, v: str) -> str:
        if v.strip():
            data = json.loads(v)
            if not isinstance(data, dict) or not all(isinstance(x, int) for x in data.values()):
                raise ValueError("RISK_WEIGHTS must be a JSON object of integers")
            unknown = set(data) - set(DEFAULT_RISK_WEIGHTS)
            if unknown:
                raise ValueError(f"unknown risk weights: {sorted(unknown)}")
        return v

    @model_validator(mode="after")
    def _check_thresholds(self) -> Settings:
        if self.candidate_threshold > self.confidence_threshold:
            raise ValueError("CANDIDATE_THRESHOLD must be <= CONFIDENCE_THRESHOLD")
        for name in (
            "min_legit_total_usdt",
            "legit_substantial_total_usdt",
            "min_victim_amount_usdt",
            "significant_amount_usdt",
            "large_amount_usdt",
            "dust_max_amount_usdt",
            "trace_min_amount_usdt",
            "network_min_alert_usdt",
            "network_trx_dust_max",
        ):
            parse_token_amount(getattr(self, name), 6)
        return self

    # ----------------------------------------------------------------- derived
    @cached_property
    def token_list(self) -> list[TokenConfig]:
        return _parse_tokens(self.tokens)

    @cached_property
    def tokens_by_contract(self) -> dict[str, TokenConfig]:
        return {t.contract: t for t in self.token_list}

    @property
    def primary_token(self) -> TokenConfig:
        return self.token_list[0]

    @cached_property
    def weights(self) -> dict[str, int]:
        w = dict(DEFAULT_RISK_WEIGHTS)
        if self.risk_weights.strip():
            w.update(json.loads(self.risk_weights))
        return w

    @cached_property
    def admin_ids(self) -> set[int]:
        return _parse_ids(self.telegram_admin_chat_id)

    @cached_property
    def alert_chat_ids(self) -> list[int]:
        ids = _parse_ids(self.telegram_alert_chat_id) or self.admin_ids
        return sorted(ids)

    def units(self, name: str, decimals: int = 6) -> int:
        """Return a *_usdt setting converted to integer base units."""
        return parse_token_amount(getattr(self, name), decimals)

    def secrets(self) -> list[str]:
        return [
            s
            for s in (
                self.tron_api_key,
                self.telegram_bot_token,
                self.tronscan_api_key,
                self.x_api_key,
                self.x_api_secret,
                self.x_access_token,
                self.x_access_token_secret,
            )
            if s and len(s) >= 6
        ]


class TokenConfig:
    __slots__ = ("symbol", "contract", "decimals")

    def __init__(self, symbol: str, contract: str, decimals: int) -> None:
        self.symbol = symbol
        self.contract = contract
        self.decimals = decimals

    def __repr__(self) -> str:
        return f"TokenConfig({self.symbol}, {self.contract}, {self.decimals})"


def _parse_tokens(v: str) -> list[TokenConfig]:
    out: list[TokenConfig] = []
    for part in v.split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split(":")
        if len(bits) != 3:
            raise ValueError(f"TOKENS entry must be SYMBOL:CONTRACT:DECIMALS, got {part!r}")
        symbol, contract, decimals = bits
        try:
            contract = normalize_address(contract)
        except InvalidAddress as exc:
            raise ValueError(f"invalid token contract {contract!r}") from exc
        out.append(TokenConfig(symbol.strip().upper(), contract, int(decimals)))
    if not out:
        raise ValueError("TOKENS must contain at least one token")
    return out


def _parse_ids(v: str) -> set[int]:
    out: set[int] = set()
    for part in (v or "").replace(";", ",").split(","):
        part = part.strip()
        if part:
            out.add(int(part))
    return out


def describe(settings: Settings) -> dict[str, Any]:
    """Non-secret configuration summary for logs / /status."""
    return {
        "mode": "SIMULATION" if settings.simulation_mode else settings.monitor_mode,
        "tokens": [f"{t.symbol}:{t.contract}" for t in settings.token_list],
        "api": settings.tron_api_url,
        "api_key": "set" if settings.tron_api_key else "not set",
        "confidence_threshold": settings.confidence_threshold,
        "min_prefix_match": settings.min_prefix_match,
        "min_suffix_match": settings.min_suffix_match,
        "min_similarity_score": settings.min_similarity_score,
        "trace_hops": settings.trace_hops,
        "network_wide": settings.network_wide and settings.monitor_mode == "block",
        "x_enabled": settings.x_enabled,
    }
