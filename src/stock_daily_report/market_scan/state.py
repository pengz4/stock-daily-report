"""Pure market-state calculations from one quote snapshot and index histories."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from typing import Literal

from stock_daily_report.market_scan.models import (
    MarketBreadth,
    MarketIndexState,
    MarketState,
)
from stock_daily_report.models import DailyBar
from stock_daily_report.providers.universe import UniverseQuote

IndexDirection = Literal["bullish", "bearish", "neutral", "insufficient"]
MarketClassification = Literal["aligned", "divergent", "insufficient"]
INDEX_DEFINITIONS = (
    ("上证指数", "000001"),
    ("深证成指", "399001"),
    ("创业板指", "399006"),
    ("沪深300", "000300"),
    ("中证1000", "000852"),
)


def calculate_index_state(
    *,
    name: str,
    code: str,
    bars: Sequence[DailyBar],
    report_date: date,
    generated_at: datetime | None = None,
) -> MarketIndexState:
    """Calculate one index's close, moving-average relations, and trend."""

    if not bars:
        return MarketIndexState(
            name=name,
            code=code,
            status="unavailable",
            provider=None,
            error_code="insufficient_history",
            error_message="no index bars were supplied",
        )

    ordered = tuple(sorted(bars, key=lambda bar: bar.trade_date))
    provider = ordered[-1].provider_name
    latest = ordered[-1]
    if latest.trade_date > report_date:
        return MarketIndexState(
            name=name,
            code=code,
            status="unavailable",
            provider=provider,
            error_code="future_data",
            error_message="latest index bar exceeds report date",
        )

    close = latest.close
    change_pct = (
        (close / ordered[-2].close - 1.0) * 100.0
        if len(ordered) >= 2
        else None
    )
    ma20 = _mean([bar.close for bar in ordered[-20:]]) if len(ordered) >= 20 else None
    ma60 = _mean([bar.close for bar in ordered[-60:]]) if len(ordered) >= 60 else None
    relation20 = _relation(close, ma20)
    relation60 = _relation(close, ma60)
    trend = _classify_index_trend(close, change_pct, ma20, ma60)
    complete = all(
        value is not None
        for value in (change_pct, relation20, relation60, trend)
    )
    return MarketIndexState(
        name=name,
        code=code,
        status="available" if complete else "partial",
        latest_trade_date=latest.trade_date,
        close=close,
        change_pct=change_pct,
        close_vs_ma20=relation20,
        close_vs_ma60=relation60,
        trend=trend,
        provider=provider,
        error_code=None if complete else "insufficient_history",
        error_message=None if complete else "requires 60 index bars",
    )


def calculate_breadth(
    quotes: Iterable[UniverseQuote],
    *,
    provider: str = "universe",
) -> MarketBreadth:
    """Count advancing, declining, and unchanged quotes with valid changes."""

    snapshot = tuple(quotes)
    valid = tuple(
        quote for quote in snapshot if quote.change_pct is not None
    )
    if not valid:
        return MarketBreadth(
            status="unavailable",
            provider=provider,
            error_code="missing_change_pct",
            error_message="quote snapshot contains no valid change_pct values",
        )

    advancing = sum(quote.change_pct > 0 for quote in valid)
    declining = sum(quote.change_pct < 0 for quote in valid)
    unchanged = len(valid) - advancing - declining
    advance_decline_ratio = advancing / declining if declining else None
    status = "available" if advance_decline_ratio is not None else "partial"
    return MarketBreadth(
        status=status,
        advancing_count=advancing,
        declining_count=declining,
        unchanged_count=unchanged,
        advancing_ratio=advancing / len(valid),
        declining_ratio=declining / len(valid),
        advance_decline_ratio=advance_decline_ratio,
        limit_up_count=None,
        limit_down_count=None,
        valid_count=len(valid),
        total_count=len(snapshot),
        provider=provider,
    )


def classify_market_state(
    indices: Sequence[MarketIndexState],
    breadth: MarketBreadth,
) -> MarketClassification:
    """Classify agreement between index direction and quote breadth."""

    index_directions = [
        _direction(index.trend)
        for index in indices
        if index.status == "available"
    ]
    index_directions = [
        direction for direction in index_directions if direction != "insufficient"
    ]
    if not index_directions or breadth.status == "unavailable":
        return "insufficient"
    if breadth.advancing_count is None or breadth.declining_count is None:
        return "insufficient"
    if breadth.advancing_count == breadth.declining_count:
        return "insufficient"

    index_direction = (
        "bullish"
        if index_directions.count("bullish") > index_directions.count("bearish")
        else "bearish"
        if index_directions.count("bearish") > index_directions.count("bullish")
        else "neutral"
    )
    breadth_direction = (
        "bullish"
        if breadth.advancing_count > breadth.declining_count
        else "bearish"
    )
    if index_direction == "neutral":
        return "insufficient"
    return "aligned" if index_direction == breadth_direction else "divergent"


def calculate_market_state(
    *,
    report_date: date,
    generated_at: datetime,
    indices: Sequence[MarketIndexState] | None = None,
    breadth: MarketBreadth | None = None,
    quotes: Iterable[UniverseQuote] | None = None,
    index_bars: dict[str, Sequence[DailyBar] | BaseException] | None = None,
    index_definitions: Sequence[tuple[str, str]] = INDEX_DEFINITIONS,
) -> MarketState:
    """Build the validated immutable market-state model from pure components."""

    if breadth is None:
        if quotes is None:
            raise ValueError("quotes or breadth must be supplied")
        breadth = calculate_breadth(quotes)
    if indices is None:
        if index_bars is None:
            raise ValueError("index_bars or indices must be supplied")
        indices = tuple(
            _index_result(
                name=name,
                code=code,
                bars=index_bars.get(code),
                report_date=report_date,
                generated_at=generated_at,
            )
            for name, code in index_definitions
        )
    classification = classify_market_state(indices, breadth)
    if classification == "insufficient":
        conclusion = "市场状态数据不足"
    elif classification == "aligned":
        conclusion = "指数与市场广度方向一致"
    else:
        conclusion = "指数与市场广度方向分歧"
    statuses = [index.status for index in indices]
    statuses.append(breadth.status)
    if all(status == "available" for status in statuses):
        status = "available"
    elif all(status == "unavailable" for status in statuses):
        status = "unavailable"
    else:
        status = "partial"
    return MarketState(
        report_date=report_date,
        generated_at=generated_at.astimezone(UTC),
        rule_version="market-state-v1",
        indices=tuple(indices),
        breadth=breadth,
        status=status,
        conclusion=conclusion,
    )


def classify_index_trend(
    close: float,
    change_pct: float | None,
    ma20: float | None,
    ma60: float | None,
) -> str:
    """Classify an index from its close, daily change, and moving averages."""

    return _classify_index_trend(close, change_pct, ma20, ma60)


def _index_result(
    *,
    name: str,
    code: str,
    bars: Sequence[DailyBar] | BaseException | None,
    report_date: date,
    generated_at: datetime,
) -> MarketIndexState:
    if isinstance(bars, BaseException):
        provider = getattr(bars, "provider", None)
        error_code = getattr(bars, "code", "provider_error")
        detail = getattr(bars, "detail", str(bars))
        return MarketIndexState(
            name=name,
            code=code,
            status="unavailable",
            provider=provider,
            error_code=error_code,
            error_message=detail,
        )
    if bars is None:
        return MarketIndexState(
            name=name,
            code=code,
            status="unavailable",
            error_code="missing_index_history",
            error_message="no index history was supplied",
        )
    return calculate_index_state(
        name=name,
        code=code,
        bars=bars,
        report_date=report_date,
        generated_at=generated_at,
    )


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _relation(close: float, moving_average: float | None) -> str | None:
    if moving_average is None:
        return None
    if math.isclose(close, moving_average, rel_tol=0.0, abs_tol=1e-12):
        return "equal"
    return "above" if close > moving_average else "below"


def _classify_index_trend(
    close: float,
    change_pct: float | None,
    ma20: float | None,
    ma60: float | None,
) -> str:
    if ma20 is None or ma60 is None or change_pct is None:
        return "insufficient"
    if close > ma20 > ma60 and change_pct >= 0:
        return "bullish"
    if close < ma20 < ma60 and change_pct <= 0:
        return "bearish"
    return "neutral"


def _direction(trend: str | None) -> IndexDirection:
    if trend in {"bullish", "bearish"}:
        return trend
    return "insufficient"


calculate_market_breadth = calculate_breadth
build_market_state = calculate_market_state


__all__ = [
    "INDEX_DEFINITIONS",
    "build_market_state",
    "calculate_breadth",
    "calculate_index_state",
    "calculate_market_breadth",
    "calculate_market_state",
    "classify_index_trend",
    "classify_market_state",
]
