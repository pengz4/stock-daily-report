"""Deterministic, trailing technical indicators for validated adjusted daily bars.

All windows end at the final supplied bar; this module neither sorts input nor
uses centered windows, so it cannot consume future prices. Moving averages are
simple trailing close averages. Returns are close-to-close percentage returns.
Realized volatility is the annualized (252 sessions), population standard
deviation of the last 20 close-to-close percentage returns. ``drawdown60`` is
the largest peak-to-trough percentage drawdown in the trailing 60 closes.
``recent_high20`` and ``recent_low20`` are the highest high and lowest low in
the trailing 20 bars.

MACD uses recursive EMAs (``adjust=False``) with spans 12, 26, and 9. The
line is emitted after 26 closes and the signal/histogram after 34 closes.
RSI uses Wilder smoothing with an initial 14-change average. ``volume_ratio20``
is the latest volume divided by the preceding 20-bar mean volume; a zero
denominator returns ``None`` unless both latest and preceding volume are zero,
which returns ``0.0``.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise

from stock_daily_report.models import DailyBar

MA_WINDOWS = (5, 10, 20, 60, 120)
RETURN_WINDOWS = (20, 60, 120)
RSI_PERIOD = 14
VOLATILITY_WINDOW = 20
DRAWDOWN_WINDOW = 60
VOLUME_RATIO_WINDOW = 20
EXTREMA_WINDOW = 20
MACD_FAST_PERIOD = 12
MACD_SLOW_PERIOD = 26
MACD_SIGNAL_PERIOD = 9
TRADING_DAYS_PER_YEAR = 252
_ADJUSTED_MODES = frozenset({"qfq", "hfq"})


@dataclass(frozen=True)
class TechnicalMetrics:
    """Trailing indicator values at ``as_of``; unavailable values are ``None``."""

    as_of: date | None
    bar_count: int
    close: float | None
    ma5: float | None
    ma10: float | None
    ma20: float | None
    ma60: float | None
    ma120: float | None
    macd_line: float | None
    macd_signal: float | None
    macd_histogram: float | None
    rsi14: float | None
    return20: float | None
    return60: float | None
    return120: float | None
    realized_volatility20: float | None
    drawdown60: float | None
    volume_ratio20: float | None
    recent_high20: float | None
    recent_low20: float | None


def calculate_technical_metrics(bars: Sequence[DailyBar]) -> TechnicalMetrics:
    """Calculate trailing metrics without altering the validated input sequence.

    ``bars`` must be strictly chronological, duplicate-free, and consistently
    adjusted using ``qfq`` or ``hfq`` prices. Invalid ordering is rejected here
    rather than silently sorted so upstream data-quality failures remain visible.
    """

    normalized_bars = _validate_bars(bars)
    closes = [bar.close for bar in normalized_bars]
    volumes = [bar.volume for bar in normalized_bars]
    highs = [bar.high for bar in normalized_bars]
    lows = [bar.low for bar in normalized_bars]

    macd_line, macd_signal, macd_histogram = _macd(closes)
    return TechnicalMetrics(
        as_of=normalized_bars[-1].trade_date if normalized_bars else None,
        bar_count=len(normalized_bars),
        close=closes[-1] if closes else None,
        ma5=_simple_moving_average(closes, 5),
        ma10=_simple_moving_average(closes, 10),
        ma20=_simple_moving_average(closes, 20),
        ma60=_simple_moving_average(closes, 60),
        ma120=_simple_moving_average(closes, 120),
        macd_line=macd_line,
        macd_signal=macd_signal,
        macd_histogram=macd_histogram,
        rsi14=_rsi(closes),
        return20=_close_return(closes, 20),
        return60=_close_return(closes, 60),
        return120=_close_return(closes, 120),
        realized_volatility20=_realized_volatility(closes),
        drawdown60=_maximum_drawdown(closes),
        volume_ratio20=_volume_ratio(volumes),
        recent_high20=_trailing_extreme(highs, max),
        recent_low20=_trailing_extreme(lows, min),
    )


def _validate_bars(bars: Sequence[DailyBar]) -> tuple[DailyBar, ...]:
    normalized_bars = tuple(bars)
    if not all(isinstance(bar, DailyBar) for bar in normalized_bars):
        raise TypeError("bars must contain only DailyBar values")

    modes = {bar.adjustment_mode for bar in normalized_bars}
    if modes and not modes <= _ADJUSTED_MODES:
        raise ValueError("bars must use an adjusted qfq or hfq adjustment_mode")
    if len(modes) > 1:
        raise ValueError("bars must use the same adjustment_mode")

    for previous, current in pairwise(normalized_bars):
        if current.trade_date <= previous.trade_date:
            raise ValueError("bar trade_date values must be strictly increasing")
    return normalized_bars


def _simple_moving_average(values: Sequence[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return _mean(values[-window:])


def _close_return(values: Sequence[float], window: int) -> float | None:
    if len(values) <= window:
        return None
    return _percentage_return(values[-1], values[-window - 1])


def _macd(values: Sequence[float]) -> tuple[float | None, float | None, float | None]:
    if len(values) < MACD_SLOW_PERIOD:
        return None, None, None

    fast = _ema(values, MACD_FAST_PERIOD)
    slow = _ema(values, MACD_SLOW_PERIOD)
    macd_values = [fast_value - slow_value for fast_value, slow_value in zip(fast, slow)]
    line = _finite_or_none(macd_values[-1])
    if len(values) < MACD_SLOW_PERIOD + MACD_SIGNAL_PERIOD - 1:
        return line, None, None
    signal = _finite_or_none(_ema(macd_values, MACD_SIGNAL_PERIOD)[-1])
    if line is None or signal is None:
        return line, signal, None
    return line, signal, _finite_or_none(line - signal)


def _ema(values: Sequence[float], period: int) -> list[float]:
    alpha = 2.0 / (period + 1)
    output = [values[0]]
    for value in values[1:]:
        output.append(alpha * value + (1.0 - alpha) * output[-1])
    return output


def _rsi(values: Sequence[float]) -> float | None:
    if len(values) <= RSI_PERIOD:
        return None
    changes = [current - previous for previous, current in pairwise(values)]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    average_gain = _mean(gains[:RSI_PERIOD])
    average_loss = _mean(losses[:RSI_PERIOD])
    if average_gain is None or average_loss is None:
        return None
    for gain, loss in zip(gains[RSI_PERIOD:], losses[RSI_PERIOD:]):
        average_gain = _finite_or_none(
            average_gain * (RSI_PERIOD - 1) / RSI_PERIOD + gain / RSI_PERIOD
        )
        average_loss = _finite_or_none(
            average_loss * (RSI_PERIOD - 1) / RSI_PERIOD + loss / RSI_PERIOD
        )
        if average_gain is None or average_loss is None:
            return None
    if average_loss == 0.0:
        return 100.0 if average_gain > 0.0 else 50.0
    if average_gain == 0.0:
        return 0.0
    try:
        relative_strength = average_gain / average_loss
    except OverflowError:
        return 100.0
    return _finite_or_none(100.0 - 100.0 / (1.0 + relative_strength))


def _realized_volatility(values: Sequence[float]) -> float | None:
    if len(values) <= VOLATILITY_WINDOW:
        return None
    returns = tuple(
        _percentage_return(current, previous)
        for previous, current in pairwise(values[-VOLATILITY_WINDOW - 1 :])
    )
    if any(value is None for value in returns):
        return None
    try:
        volatility = statistics.pstdev(returns) * math.sqrt(TRADING_DAYS_PER_YEAR)
    except OverflowError:
        return None
    return _finite_or_none(volatility)


def _maximum_drawdown(values: Sequence[float]) -> float | None:
    if len(values) < DRAWDOWN_WINDOW:
        return None
    peak = values[-DRAWDOWN_WINDOW]
    maximum_drawdown = 0.0
    for value in values[-DRAWDOWN_WINDOW:]:
        peak = max(peak, value)
        maximum_drawdown = min(maximum_drawdown, value / peak - 1.0)
    return maximum_drawdown


def _volume_ratio(values: Sequence[float]) -> float | None:
    if len(values) <= VOLUME_RATIO_WINDOW:
        return None
    preceding_average = _mean(values[-VOLUME_RATIO_WINDOW - 1 : -1])
    if preceding_average is None:
        return None
    latest_volume = values[-1]
    if preceding_average == 0.0:
        return 0.0 if latest_volume == 0.0 else None
    try:
        ratio = latest_volume / preceding_average
    except OverflowError:
        return None
    return _finite_or_none(ratio)


def _trailing_extreme(
    values: Sequence[float], operation: Callable[[Sequence[float]], float]
) -> float | None:
    if len(values) < EXTREMA_WINDOW:
        return None
    return operation(values[-EXTREMA_WINDOW:])


def _mean(values: Sequence[float]) -> float | None:
    try:
        return _finite_or_none(math.fsum(value / len(values) for value in values))
    except OverflowError:
        return None


def _percentage_return(current: float, previous: float) -> float | None:
    try:
        return _finite_or_none(current / previous - 1.0)
    except OverflowError:
        return None


def _finite_or_none(value: float) -> float | None:
    return value if math.isfinite(value) else None


__all__ = [
    "TechnicalMetrics",
    "calculate_technical_metrics",
]
