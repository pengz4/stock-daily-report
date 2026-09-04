"""Execution-aware, long-only out-of-sample evaluation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from statistics import mean, pstdev

from stock_daily_report.models import DailyBar

from .execution import Costs, Trade, execute_signal, is_limit_down, is_suspended
from .replay import ReplayEvent


@dataclass(frozen=True)
class BacktestEvaluation:
    """Aggregate results with explicit sample and split metadata."""

    sample_count: int
    hit_rate: float
    average_return: float
    cumulative_return: float
    maximum_drawdown: float
    sharpe: float
    turnover: float
    out_of_sample_start: date | None
    trades: tuple[Trade, ...]
    evidence_status: str
    minimum_sample_count: int
    average_return_interval: tuple[float, float] | None

    def to_dict(self) -> dict[str, object]:
        return {
            "sample_count": self.sample_count,
            "hit_rate": self.hit_rate,
            "average_return": self.average_return,
            "cumulative_return": self.cumulative_return,
            "maximum_drawdown": self.maximum_drawdown,
            "sharpe": self.sharpe,
            "turnover": self.turnover,
            "out_of_sample_start": (
                self.out_of_sample_start.isoformat()
                if self.out_of_sample_start
                else None
            ),
            "evidence_status": self.evidence_status,
            "minimum_sample_count": self.minimum_sample_count,
            "average_return_interval": (
                list(self.average_return_interval)
                if self.average_return_interval
                else None
            ),
        }


def evaluate_signals(
    signals: list[ReplayEvent] | tuple[ReplayEvent, ...],
    bars: list[DailyBar],
    *,
    costs: Costs,
    holding_days: int = 5,
    out_of_sample_start: date | None = None,
    minimum_sample_count: int = 20,
) -> BacktestEvaluation:
    """Evaluate deduplicated confirmed signals after the configured split."""

    if isinstance(holding_days, bool) or not isinstance(holding_days, int) or holding_days < 1:
        raise ValueError("holding_days must be a positive integer")
    if (
        isinstance(minimum_sample_count, bool)
        or not isinstance(minimum_sample_count, int)
        or minimum_sample_count < 1
    ):
        raise ValueError("minimum_sample_count must be a positive integer")
    confirmed: dict[str, ReplayEvent] = {}
    for signal in signals:
        if signal.status != "confirmed":
            continue
        previous = confirmed.get(signal.event_id)
        if previous is None or (
            previous.tradable_at is None and signal.tradable_at is not None
        ):
            confirmed[signal.event_id] = signal
    trades: list[Trade] = []
    returns: list[float] = []
    equity_curve = [1.0]
    occupied_until = -1
    for signal in sorted(
        confirmed.values(),
        key=lambda item: (
            item.tradable_at or item.observed_at,
            item.observed_at,
            item.event_id,
        ),
    ):
        trade = execute_signal(signal, bars, costs=costs)
        if trade is None:
            continue
        entry_index = _bar_index(bars, trade.entry_date)
        if entry_index is None or entry_index <= occupied_until:
            continue
        if out_of_sample_start is not None and trade.entry_date < out_of_sample_start:
            continue
        exit_result = _exit_result(trade, bars, costs, holding_days)
        if exit_result is None:
            continue
        exit_index, exit_price = exit_result
        occupied_until = exit_index
        returns.append(
            exit_price / (trade.entry_price * (1 + trade.commission_rate)) - 1
        )
        trades.append(trade)
        _append_marked_equity(
            equity_curve,
            trade,
            bars,
            costs,
            exit_index,
        )
    cumulative = math.prod(1 + value for value in returns) - 1 if returns else 0.0
    peak = 1.0
    drawdown = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1)
    average = mean(returns) if returns else 0.0
    deviation = pstdev(returns) if len(returns) > 1 else 0.0
    sharpe = average / deviation if deviation else 0.0
    interval = None
    if len(returns) > 1:
        margin = 1.96 * deviation / math.sqrt(len(returns))
        interval = (average - margin, average + margin)
    return BacktestEvaluation(
        sample_count=len(returns),
        hit_rate=sum(value > 0 for value in returns) / len(returns)
        if returns
        else 0.0,
        average_return=average,
        cumulative_return=cumulative,
        maximum_drawdown=drawdown,
        sharpe=sharpe,
        turnover=float(len(trades)),
        out_of_sample_start=out_of_sample_start,
        trades=tuple(trades),
        evidence_status=(
            "enough_evidence"
            if len(returns) >= minimum_sample_count
            else "not_enough_evidence"
        ),
        minimum_sample_count=minimum_sample_count,
        average_return_interval=interval,
    )


def _exit_result(
    trade: Trade,
    bars: list[DailyBar],
    costs: Costs,
    holding_days: int,
) -> tuple[int, float] | None:
    entry_index = _bar_index(bars, trade.entry_date)
    if entry_index is None:
        return None
    target_index = entry_index + holding_days
    for index in range(target_index, len(bars)):
        bar = bars[index]
        if is_suspended(bar):
            continue
        if index > 0 and is_limit_down(
            bar,
            bars[index - 1],
            costs.price_limit_pct,
            costs.price_tick,
        ):
            continue
        return (
            index,
            bar.close * (1 - costs.slippage_rate) * (1 - trade.commission_rate),
        )
    return None


def _append_marked_equity(
    equity_curve: list[float],
    trade: Trade,
    bars: list[DailyBar],
    costs: Costs,
    exit_index: int,
) -> None:
    entry_index = next(index for index, bar in enumerate(bars) if bar.trade_date == trade.entry_date)
    entry_cost = trade.entry_price * (1 + trade.commission_rate)
    base_equity = equity_curve[-1]
    for index in range(entry_index, exit_index + 1):
        bar = bars[index]
        if is_suspended(bar):
            continue
        marked_value = bar.close * (1 - costs.slippage_rate) * (
            1 - trade.commission_rate
        )
        equity_curve.append(base_equity * marked_value / entry_cost)


def _bar_index(bars: list[DailyBar], target_date: date) -> int | None:
    return next(
        (index for index, bar in enumerate(bars) if bar.trade_date == target_date),
        None,
    )


__all__ = ["BacktestEvaluation", "evaluate_signals"]
