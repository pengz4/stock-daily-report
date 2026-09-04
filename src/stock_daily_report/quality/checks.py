"""Quality checks run before bars are allowed into analysis.

Staleness uses weekdays as a minimal trading calendar. It deliberately does
not model exchange holidays, so callers needing holiday-aware validation must
provide a trading-calendar implementation before using this as a final gate.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from stock_daily_report.models import DailyBar

_OHLC_FIELDS = ("open", "high", "low", "close")


@dataclass(frozen=True)
class DataQualitySettings:
    """Configurable pre-analysis thresholds."""

    minimum_history_bars: int = 60
    max_completed_trading_day_lag: int = 1

    def __post_init__(self) -> None:
        if self.minimum_history_bars < 1:
            raise ValueError("minimum_history_bars must be at least 1")
        if self.max_completed_trading_day_lag < 0:
            raise ValueError("max_completed_trading_day_lag must not be negative")


@dataclass(frozen=True)
class DataQualityIssue:
    """One quality problem, including enough context for diagnostics."""

    code: str
    message: str
    record_index: int | None = None


@dataclass(frozen=True)
class DataQualityResult:
    """The complete pre-analysis decision for one security's history."""

    code: str
    as_of: date
    issues: tuple[DataQualityIssue, ...]
    bar_count: int
    analysis_allowed: bool

    @property
    def is_valid(self) -> bool:
        """Compatibility alias for callers phrasing validity as a predicate."""

        return self.analysis_allowed

    @property
    def issue_codes(self) -> tuple[str, ...]:
        """Stable, de-duplicated issue codes in discovery order."""

        return tuple(dict.fromkeys(issue.code for issue in self.issues))

    def with_issue(self, code: str, message: str) -> "DataQualityResult":
        """Return a failed result augmented by a service-level quality issue."""

        return DataQualityResult(
            code=self.code,
            as_of=self.as_of,
            issues=(*self.issues, DataQualityIssue(code, message)),
            bar_count=self.bar_count,
            analysis_allowed=False,
        )


BarInput = DailyBar | Mapping[str, object]


def validate_bars(
    code: str,
    bars: Sequence[BarInput],
    *,
    as_of: date,
    settings: DataQualitySettings | None = None,
) -> DataQualityResult:
    """Collect all known data problems without constructing invalid DailyBars."""

    if isinstance(as_of, datetime) or not isinstance(as_of, date):
        raise TypeError("as_of must be a date, not a datetime")
    active_settings = settings or DataQualitySettings()
    issues: list[DataQualityIssue] = []
    trade_dates: list[tuple[int, date]] = []

    for index, bar in enumerate(bars):
        record = _as_record(bar)
        trade_date = _parse_trade_date(record.get("trade_date"))
        if trade_date is None:
            issues.append(
                DataQualityIssue(
                    "invalid_trade_date",
                    "trade_date must be an ISO date",
                    index,
                )
            )
        else:
            trade_dates.append((index, trade_date))

        prices = {
            field: _parse_price(record.get(field))
            for field in _OHLC_FIELDS
        }
        for field, value in prices.items():
            if value is None:
                issues.append(
                    DataQualityIssue(
                        "missing_ohlc",
                        f"{field} is missing or not numeric",
                        index,
                    )
                )
            elif value <= 0 or not math.isfinite(value):
                issues.append(
                    DataQualityIssue(
                        "negative_ohlc",
                        f"{field} must be finite and positive",
                        index,
                    )
                )

        high, low, close = prices["high"], prices["low"], prices["close"]
        if _all_valid(high, low) and high < low:
            issues.append(
                DataQualityIssue("high_lt_low", "high must not be lower than low", index)
            )
        if _all_valid(low, high, close) and not low <= close <= high:
            issues.append(
                DataQualityIssue(
                    "close_outside_low_high",
                    "close must be within the low/high range",
                    index,
                )
            )

    _validate_date_sequence(trade_dates, issues)
    for index, trade_date in trade_dates:
        if trade_date > as_of:
            issues.append(
                DataQualityIssue(
                    "future_trade_date",
                    f"trade_date {trade_date.isoformat()} is after as_of",
                    index,
                )
            )
    if len(bars) < active_settings.minimum_history_bars:
        issues.append(
            DataQualityIssue(
                "insufficient_history",
                "history has fewer bars than the configured minimum",
            )
        )
    if trade_dates and _is_stale(
        max(value for _, value in trade_dates),
        as_of,
        active_settings.max_completed_trading_day_lag,
    ):
        issues.append(
            DataQualityIssue(
                "stale_last_trade_date",
                "last trade date exceeds the configured completed trading-day lag",
            )
        )

    return DataQualityResult(
        code=code,
        as_of=as_of,
        issues=tuple(issues),
        bar_count=len(bars),
        analysis_allowed=not issues,
    )


def _as_record(bar: BarInput) -> Mapping[str, Any]:
    if isinstance(bar, DailyBar):
        return bar.model_dump()
    if isinstance(bar, Mapping):
        return bar
    return {}


def _parse_trade_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _parse_price(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _all_valid(*values: float | None) -> bool:
    return all(value is not None and math.isfinite(value) for value in values)


def _validate_date_sequence(
    trade_dates: Sequence[tuple[int, date]], issues: list[DataQualityIssue]
) -> None:
    seen_dates: set[date] = set()
    previous: date | None = None
    for index, trade_date in trade_dates:
        if trade_date in seen_dates:
            issues.append(
                DataQualityIssue(
                    "duplicate_trade_date",
                    f"duplicate trade_date {trade_date.isoformat()}",
                    index,
                )
            )
        if previous is not None and trade_date <= previous:
            issues.append(
                DataQualityIssue(
                    "non_chronological_trade_dates",
                    "trade_date values must be strictly chronological",
                    index,
                )
            )
        seen_dates.add(trade_date)
        previous = trade_date


def _is_stale(last_trade_date: date, as_of: date, maximum_lag: int) -> bool:
    expected_date = _most_recent_weekday(as_of)
    if last_trade_date >= expected_date:
        return False
    return _weekday_distance(last_trade_date, expected_date) > maximum_lag


def _most_recent_weekday(value: date) -> date:
    while value.weekday() >= 5:
        value -= timedelta(days=1)
    return value


def _weekday_distance(start: date, end: date) -> int:
    distance = 0
    current = start
    while current < end:
        current += timedelta(days=1)
        if current.weekday() < 5:
            distance += 1
    return distance
