"""Typed configuration and normalized market-data models for the pipeline."""

import math
import re
from datetime import UTC, date, datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
A_SHARE_CODE_PATTERN = re.compile(
    r"^(?:00[0-3]\d{3}|30[01]\d{3}|60[0135]\d{3}|688\d{3}|920\d{3})$"
)


class WatchlistStock(BaseModel):
    """A tracked mainland A-share security.

    Supported code classes are Shenzhen main-board (000/001/002/003), ChiNext
    (300/301), Shanghai main-board (600/601/603/605), STAR Market (688), and
    Beijing Stock Exchange (920). Legacy BSE aliases are not accepted because
    this current-date-only report supports post-2025 BSE codes only.
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    name: NonEmptyString
    group: str = "default"
    # Tags are stripped of surrounding whitespace while retaining their case.
    tags: list[str] = Field(default_factory=list)
    news_enabled: bool = True

    @field_validator("code")
    @classmethod
    def require_supported_a_share_code(cls, value: str) -> str:
        if not A_SHARE_CODE_PATTERN.fullmatch(value):
            raise ValueError(
                "stock code must use a supported mainland A-share code prefix "
                "(SZ 000/001/002/003, ChiNext 300/301, SH 600/601/603/605, "
                "STAR 688, BSE 920; legacy BSE aliases are not accepted)"
            )
        return value

    @field_validator("tags")
    @classmethod
    def normalize_and_require_unique_tags(cls, value: list[str]) -> list[str]:
        normalized_tags = [tag.strip() for tag in value]
        if any(not tag for tag in normalized_tags):
            raise ValueError("tag must not be blank")
        if len(normalized_tags) != len(set(normalized_tags)):
            raise ValueError("duplicate tag(s) are not allowed")
        return normalized_tags


class Watchlist(BaseModel):
    """The version-controlled set of securities to report on."""

    model_config = ConfigDict(extra="forbid")

    stocks: list[WatchlistStock]

    @field_validator("stocks")
    @classmethod
    def require_stocks(cls, value: list[WatchlistStock]) -> list[WatchlistStock]:
        if not value:
            raise ValueError("watchlist must contain at least one stock")
        return value

    @model_validator(mode="after")
    def ensure_unique_codes(self) -> "Watchlist":
        codes = [stock.code for stock in self.stocks]
        duplicate_codes = sorted({code for code in codes if codes.count(code) > 1})
        if duplicate_codes:
            raise ValueError(f"duplicate stock code(s): {', '.join(duplicate_codes)}")
        return self


class RuleVersion(BaseModel):
    """The named version of the active analysis rule set."""

    model_config = ConfigDict(extra="forbid")

    name: NonEmptyString
    version: NonEmptyString


class NotificationSettings(BaseModel):
    """Non-secret settings for optional report summary delivery.

    Webhook URLs are deliberately not model fields. They are supplied through
    process environment variables at delivery time.
    """

    model_config = ConfigDict(extra="forbid")

    enabled_channels: set[Literal["wecom", "feishu"]] = Field(default_factory=set)
    timeout_seconds: int = Field(default=10, gt=0)
    max_attempts: int = Field(default=3, ge=1, le=5)

    @field_validator("enabled_channels", mode="before")
    @classmethod
    def reject_duplicate_enabled_channels(cls, value: object) -> object:
        if isinstance(value, list):
            seen_channels: set[str] = set()
            duplicates: set[str] = set()
            for channel in value:
                if isinstance(channel, str):
                    if channel in seen_channels:
                        duplicates.add(channel)
                    seen_channels.add(channel)
            if duplicates:
                raise ValueError(
                    "duplicate enabled channel(s) are not allowed: "
                    f"{', '.join(sorted(duplicates))}"
                )
        return value

class MarketDataSettings(BaseModel):
    """Provider order, cache path, and weekday-only pre-analysis gate settings.

    ``cache_directory`` is intentionally a local ignored directory. Weekday
    staleness does not account for Chinese exchange holidays.
    """

    model_config = ConfigDict(extra="forbid")

    primary_provider: NonEmptyString = "akshare"
    fallback_provider: NonEmptyString = "fixture"
    cache_directory: NonEmptyString = ".cache/stock-daily-report"
    cache_ttl_seconds: int = Field(default=3600, ge=0)
    minimum_history_bars: int = Field(default=60, ge=1)
    max_completed_trading_day_lag: int = Field(default=1, ge=0)

    @model_validator(mode="after")
    def require_distinct_provider_order(self) -> "MarketDataSettings":
        if self.primary_provider == self.fallback_provider:
            raise ValueError("primary_provider and fallback_provider must differ")
        return self


class RiskRulesSettings(BaseModel):
    """Versioned thresholds consumed by the risk-first decision layer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_version: NonEmptyString
    high_realized_volatility20: float = Field(gt=0.0)
    overextension_ma20_distance: float = Field(gt=0.0)
    large_drawdown60: float = Field(gt=-1.0, lt=0.0)
    adverse_volume_ratio20: float = Field(ge=0.0)
    minimum_history_bars: int = Field(ge=1)

    @field_validator(
        "high_realized_volatility20",
        "overextension_ma20_distance",
        "large_drawdown60",
        "adverse_volume_ratio20",
        "minimum_history_bars",
        mode="before",
    )
    @classmethod
    def reject_non_finite_thresholds(cls, value: object) -> object:
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            return value
        if not math.isfinite(numeric_value):
            raise ValueError("risk thresholds must be finite")
        return value


class Settings(BaseModel):
    """Top-level deterministic report settings."""

    model_config = ConfigDict(extra="forbid")

    rule_version: RuleVersion
    notifications: NotificationSettings
    market_data: MarketDataSettings = Field(default_factory=MarketDataSettings)
    # Optional here preserves compatibility with Task 1-5 ad-hoc settings
    # documents; decision evaluation requires an explicit risk configuration.
    risk_rules: RiskRulesSettings | None = None


class BacktestCostsSettings(BaseModel):
    """Validated transaction-cost assumptions for research backtests."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    commission_bps: float = Field(ge=0.0, lt=10_000)
    slippage_bps: float = Field(ge=0.0, lt=10_000)
    price_limit_pct: float = Field(gt=0.0, lt=1.0)
    price_tick: float = Field(gt=0.0)

    @field_validator(
        "commission_bps",
        "slippage_bps",
        "price_limit_pct",
        "price_tick",
        mode="before",
    )
    @classmethod
    def require_finite_values(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError(  # noqa: TRY004
                "backtest cost values must be numeric"
            )
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            return value
        if not math.isfinite(numeric_value):
            raise ValueError("backtest cost values must be finite")
        return value


class BacktestSettings(BaseModel):
    """Versioned execution and sample-split assumptions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_version: NonEmptyString
    holding_days: int = Field(ge=1)
    holding_periods: tuple[int, ...] | None = None
    minimum_sample_count: int = Field(ge=1)
    out_of_sample_start: date | None = None
    costs: BacktestCostsSettings

    @field_validator("holding_periods")
    @classmethod
    def require_unique_positive_holding_periods(
        cls, value: tuple[int, ...] | None
    ) -> tuple[int, ...] | None:
        if value is None:
            return None
        if not value or any(
            isinstance(period, bool) or period < 1 for period in value
        ):
            raise ValueError("holding_periods must contain positive integers")
        if len(set(value)) != len(value):
            raise ValueError("holding_periods must not contain duplicates")
        return value


class MarketScanSettings(BaseModel):
    """Versioned limits and eligibility thresholds for full-market scans."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_version: Literal["market-scan-v1"]
    trend_limit: int = Field(gt=0)
    balanced_limit: int = Field(gt=0)
    minimum_history_bars: int = Field(gt=0)
    minimum_latest_amount: float = Field(gt=0.0)
    minimum_coverage_ratio: float = Field(gt=0.0, le=1.0)
    max_workers: int = Field(gt=0)
    max_candidates: int = Field(gt=0)

    @field_validator(
        "minimum_latest_amount",
        "minimum_coverage_ratio",
        mode="before",
    )
    @classmethod
    def require_finite_thresholds(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError(  # noqa: TRY004
                "market scan thresholds must be numeric"
            )
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            return value
        if not math.isfinite(numeric_value):
            raise ValueError("market scan thresholds must be finite")
        return value


class DailyBar(BaseModel):
    """One normalized daily OHLC bar from a named market-data provider.

    Source timestamps must be timezone-aware. They are normalized to UTC so
    serialized snapshots have one unambiguous representation regardless of
    the provider's local exchange timezone.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    turnover_rate: float
    adjustment_mode: NonEmptyString
    provider_name: NonEmptyString
    source_timestamp: datetime

    @field_validator("trade_date", mode="before")
    @classmethod
    def reject_datetime_trade_dates(cls, value: object) -> object:
        if isinstance(value, datetime):
            raise ValueError("trade_date must be a date, not a datetime")  # noqa: TRY004
        return value

    @field_validator("open", "high", "low", "close", mode="before")
    @classmethod
    def require_numeric_prices(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("OHLC values must be numeric")  # noqa: TRY004
        return value

    @field_validator("volume", "amount", "turnover_rate", mode="before")
    @classmethod
    def require_numeric_measures(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError(  # noqa: TRY004
                "volume, amount, and turnover_rate must be numeric"
            )
        return value

    @field_validator("open", "high", "low", "close")
    @classmethod
    def require_positive_finite_prices(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("OHLC values must be finite and strictly positive")
        return value

    @field_validator("volume", "amount", "turnover_rate")
    @classmethod
    def require_non_negative_finite_measures(cls, value: float) -> float:
        if not math.isfinite(value) or value < 0:
            raise ValueError(
                "volume, amount, and turnover_rate must be finite and non-negative"
            )
        return value

    @field_validator("source_timestamp")
    @classmethod
    def require_aware_source_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("source_timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_ohlc_ordering(self) -> "DailyBar":
        if not (
            self.low <= min(self.open, self.close)
            and max(self.open, self.close) <= self.high
        ):
            raise ValueError(
                "OHLC values must satisfy "
                "low <= min(open, close) <= max(open, close) <= high"
            )
        return self
