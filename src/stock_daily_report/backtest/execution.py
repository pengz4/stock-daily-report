"""Execution constraints for long-only A-share research backtests."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise

from stock_daily_report.models import DailyBar

from .replay import ReplayEvent


@dataclass(frozen=True)
class Costs:
    """Explicit transaction-cost and daily price-limit assumptions."""

    commission_bps: float = 0.0
    slippage_bps: float = 0.0
    price_limit_pct: float = 0.10
    price_tick: float = 0.01

    def __post_init__(self) -> None:
        for name, value in (
            ("commission_bps", self.commission_bps),
            ("slippage_bps", self.slippage_bps),
            ("price_limit_pct", self.price_limit_pct),
            ("price_tick", self.price_tick),
        ):
            if isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.commission_bps >= 10_000 or self.slippage_bps >= 10_000:
            raise ValueError("commission and slippage must be below 100%")
        if self.price_limit_pct <= 0 or self.price_limit_pct >= 1:
            raise ValueError("price_limit_pct must be between 0 and 1")
        if self.price_tick <= 0:
            raise ValueError("price_tick must be positive")

    @property
    def commission_rate(self) -> float:
        return self.commission_bps / 10_000

    @property
    def slippage_rate(self) -> float:
        return self.slippage_bps / 10_000


@dataclass(frozen=True)
class Trade:
    """One executable long trade with its entry cost assumptions."""

    signal_id: str
    direction: str
    entry_date: date
    raw_entry_price: float
    entry_price: float
    commission_rate: float
    slippage_rate: float


def execute_signal(
    signal: ReplayEvent,
    bars: list[DailyBar],
    *,
    costs: Costs,
) -> Trade | None:
    """Execute a confirmed upward signal at the next eligible daily bar."""

    _validate_bars(bars)
    if (
        signal.kind != "signal"
        or signal.status != "confirmed"
        or signal.tradable_at is None
        or "breakout_up" not in signal.reason_code
    ):
        return None
    earliest_entry = max(
        signal.tradable_at,
        signal.observed_at,
        signal.max_input_date,
    )
    for index, bar in enumerate(bars):
        if bar.trade_date <= earliest_entry:
            continue
        if is_suspended(bar) or index == 0:
            continue
        if _is_limit_up(
            bar,
            bars[index - 1],
            costs.price_limit_pct,
            costs.price_tick,
        ):
            continue
        raw_price = bar.open
        return Trade(
            signal_id=signal.event_id,
            direction="long",
            entry_date=bar.trade_date,
            raw_entry_price=raw_price,
            entry_price=raw_price * (1 + costs.slippage_rate),
            commission_rate=costs.commission_rate,
            slippage_rate=costs.slippage_rate,
        )
    return None


def _validate_bars(bars: list[DailyBar]) -> None:
    if any(earlier.trade_date >= later.trade_date for earlier, later in pairwise(bars)):
        raise ValueError("DailyBar trade_date values must be strictly increasing")


def is_suspended(bar: DailyBar) -> bool:
    return bar.volume <= 0 or bar.amount <= 0


def _is_limit_up(
    bar: DailyBar,
    previous: DailyBar,
    limit_pct: float,
    price_tick: float,
) -> bool:
    return (
        bar.open + 1e-9
        >= _rounded_limit(previous.close * (1 + limit_pct), price_tick)
        and bar.high == bar.low == bar.open
    )


def is_limit_down(
    bar: DailyBar,
    previous: DailyBar,
    limit_pct: float,
    price_tick: float = 0.01,
) -> bool:
    """Return whether a bar is locked at its daily down limit."""

    return (
        bar.open - 1e-9
        <= _rounded_limit(previous.close * (1 - limit_pct), price_tick)
        and bar.high == bar.low == bar.open
    )


def _rounded_limit(price: float, price_tick: float) -> float:
    value = (Decimal(str(price)) / Decimal(str(price_tick))).quantize(
        Decimal(1),
        rounding=ROUND_HALF_UP,
    )
    return float(value * Decimal(str(price_tick)))


__all__ = ["Costs", "Trade", "execute_signal", "is_limit_down", "is_suspended"]
