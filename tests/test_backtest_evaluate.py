from datetime import UTC, date, datetime, timedelta

import pytest

from stock_daily_report.backtest.evaluate import evaluate_signals
from stock_daily_report.backtest.execution import Costs
from stock_daily_report.backtest.replay import ReplayEvent
from stock_daily_report.models import DailyBar


def _bars(opens: list[float]) -> list[DailyBar]:
    return [
        DailyBar(
            trade_date=date(2026, 1, 1) + timedelta(days=index),
            open=open_price,
            high=open_price,
            low=open_price,
            close=open_price,
            volume=100,
            amount=open_price * 100,
            turnover_rate=0.1,
            adjustment_mode="qfq",
            provider_name="fixture",
            source_timestamp=datetime(2026, 1, 1, tzinfo=UTC)
            + timedelta(days=index),
        )
        for index, open_price in enumerate(opens)
    ]


def test_evaluate_signals_reports_out_of_sample_metrics():
    signals = [
        ReplayEvent(
            analyzer="simplified-v1",
            event_id="signal-1",
            kind="signal",
            observed_at=date(2026, 1, 1),
            max_input_date=date(2026, 1, 1),
            formed_at=date(2026, 1, 1),
            confirmed_at=date(2026, 1, 1),
            tradable_at=date(2026, 1, 1),
            status="confirmed",
            reason_code="strict_breakout_up_candidate",
            price=None,
            low=None,
            high=None,
            revision=0,
            first_observed_at=date(2026, 1, 1),
        )
    ]
    bars = _bars([100, 100, 110, 120])

    result = evaluate_signals(
        signals,
        bars,
        costs=Costs(),
        holding_days=1,
        out_of_sample_start=date(2026, 1, 2),
    )

    assert result.sample_count == 1
    assert result.hit_rate == 1
    assert result.cumulative_return > 0
    assert result.out_of_sample_start == date(2026, 1, 2)


def test_evaluate_signals_uses_calendar_horizon_and_marks_drawdown():
    signal = ReplayEvent(
        analyzer="simplified-v1",
        event_id="signal-1",
        kind="signal",
        observed_at=date(2026, 1, 1),
        max_input_date=date(2026, 1, 1),
        formed_at=date(2026, 1, 1),
        confirmed_at=date(2026, 1, 1),
        tradable_at=date(2026, 1, 1),
        status="confirmed",
        reason_code="strict_breakout_up_candidate",
        price=None,
        low=None,
        high=None,
        revision=0,
        first_observed_at=date(2026, 1, 1),
    )

    result = evaluate_signals(
        [signal],
        _bars([100, 100, 80, 105, 95]),
        costs=Costs(),
        holding_days=2,
    )

    assert result.sample_count == 1
    assert result.cumulative_return == pytest.approx(0.05)
    assert result.maximum_drawdown == pytest.approx(-0.2)


def test_evaluate_signals_suppresses_overlapping_positions():
    signal = ReplayEvent(
        analyzer="simplified-v1",
        event_id="signal-1",
        kind="signal",
        observed_at=date(2026, 1, 1),
        max_input_date=date(2026, 1, 1),
        formed_at=date(2026, 1, 1),
        confirmed_at=date(2026, 1, 1),
        tradable_at=date(2026, 1, 1),
        status="confirmed",
        reason_code="strict_breakout_up_candidate",
        price=None,
        low=None,
        high=None,
        revision=0,
        first_observed_at=date(2026, 1, 1),
    )
    second = signal.__class__(**{**signal.__dict__, "event_id": "signal-2"})

    result = evaluate_signals(
        [signal, second],
        _bars([100, 100, 110, 120]),
        costs=Costs(),
        holding_days=1,
    )

    assert result.sample_count == 1
    assert result.turnover == 1


def test_evaluate_signals_marks_entry_day_cost_and_price_move():
    signal = ReplayEvent(
        analyzer="simplified-v1",
        event_id="signal-1",
        kind="signal",
        observed_at=date(2026, 1, 1),
        max_input_date=date(2026, 1, 1),
        formed_at=date(2026, 1, 1),
        confirmed_at=date(2026, 1, 1),
        tradable_at=date(2026, 1, 1),
        status="confirmed",
        reason_code="strict_breakout_up_candidate",
        price=None,
        low=None,
        high=None,
        revision=0,
        first_observed_at=date(2026, 1, 1),
    )

    bars = _bars([100, 100, 105])
    bars[1] = bars[1].model_copy(update={"close": 50})

    result = evaluate_signals([signal], bars, costs=Costs(), holding_days=1)

    assert result.maximum_drawdown == pytest.approx(-0.5)
def test_evaluate_signals_rolls_exit_only_when_target_bar_is_unavailable():
    signal = ReplayEvent(
        analyzer="simplified-v1",
        event_id="signal-1",
        kind="signal",
        observed_at=date(2026, 1, 1),
        max_input_date=date(2026, 1, 1),
        formed_at=date(2026, 1, 1),
        confirmed_at=date(2026, 1, 1),
        tradable_at=date(2026, 1, 1),
        status="confirmed",
        reason_code="strict_breakout_up_candidate",
        price=None,
        low=None,
        high=None,
        revision=0,
        first_observed_at=date(2026, 1, 1),
    )
    bars = _bars([100, 100, 105, 105, 95])
    bars[2] = bars[2].model_copy(update={"volume": 0, "amount": 0})

    result = evaluate_signals([signal], bars, costs=Costs(), holding_days=2)

    assert result.sample_count == 1
    assert result.cumulative_return == pytest.approx(0.05)
