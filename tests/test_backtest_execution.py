from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from stock_daily_report.backtest.execution import (
    Costs,
    execute_signal,
    is_limit_down,
)
from stock_daily_report.backtest.replay import ReplayEvent
from stock_daily_report.models import DailyBar


def _bars(opens: list[float], *, volume: float = 100) -> list[DailyBar]:
    return [
        DailyBar(
            trade_date=date(2026, 1, 1) + timedelta(days=index),
            open=open_price,
            high=open_price,
            low=open_price,
            close=open_price,
            volume=volume,
            amount=open_price * volume,
            turnover_rate=0.1,
            adjustment_mode="qfq",
            provider_name="fixture",
            source_timestamp=datetime(2026, 1, 1, tzinfo=UTC)
            + timedelta(days=index),
        )
        for index, open_price in enumerate(opens)
    ]


def _signal(*, direction: str = "up") -> ReplayEvent:
    return ReplayEvent(
        analyzer="strict-v1",
        event_id="signal-1",
        kind="signal",
        observed_at=date(2026, 1, 2),
        max_input_date=date(2026, 1, 2),
        formed_at=date(2026, 1, 1),
        confirmed_at=date(2026, 1, 2),
        tradable_at=date(2026, 1, 2),
        status="confirmed",
        reason_code=f"strict_breakout_{direction}_candidate",
        price=None,
        low=None,
        high=None,
        revision=0,
        first_observed_at=date(2026, 1, 2),
    )


def test_execute_signal_uses_next_eligible_bar_and_costs():
    trade = execute_signal(
        _signal(),
        _bars([100, 101, 105]),
        costs=Costs(commission_bps=3, slippage_bps=5),
    )

    assert trade is not None
    assert trade.entry_date == date(2026, 1, 3)
    assert trade.raw_entry_price == 105
    assert trade.entry_price == 105.0525


def test_execute_signal_skips_suspended_and_limit_up_bars():
    bars = _bars([100, 100, 110, 101])
    bars[1] = bars[1].model_copy(update={"volume": 0, "amount": 0})

    trade = execute_signal(_signal(), bars, costs=Costs())

    assert trade is not None
    assert trade.entry_date == date(2026, 1, 4)


def test_execute_signal_does_not_execute_downward_signal():
    trade = execute_signal(_signal(direction="down"), _bars([100, 101, 102]), costs=Costs())

    assert trade is None


def test_execute_signal_never_precedes_observation_time():
    signal = replace(
        _signal(),
        observed_at=date(2026, 1, 4),
        max_input_date=date(2026, 1, 4),
        tradable_at=date(2026, 1, 2),
    )

    trade = execute_signal(signal, _bars([100, 101, 102, 103, 104]), costs=Costs())

    assert trade is not None
    assert trade.entry_date == date(2026, 1, 5)


def test_execute_signal_skips_tick_rounded_limit_up_bar():
    trade = execute_signal(
        _signal(),
        _bars([10.04, 10.04, 11.04, 11.05]),
        costs=Costs(),
    )

    assert trade is not None
    assert trade.entry_date == date(2026, 1, 4)


def test_limit_down_uses_half_up_tick_rounding():
    bars = _bars([1.15, 1.04])

    assert is_limit_down(bars[1], bars[0], 0.10)


def test_costs_reject_impossible_rates():
    with pytest.raises(ValueError, match="below 100%"):
        Costs(commission_bps=10_000)
