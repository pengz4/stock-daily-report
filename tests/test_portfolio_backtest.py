from datetime import UTC, date, datetime, timedelta

import pytest

from stock_daily_report.backtest.portfolio import simulate_portfolio
from stock_daily_report.models import DailyBar, PortfolioBacktestSettings


def _bar(day: date, price: float, *, volume: float = 100_000) -> DailyBar:
    return DailyBar(
        trade_date=day,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=volume,
        amount=price * volume,
        turnover_rate=1,
        adjustment_mode="qfq",
        provider_name="fixture",
        source_timestamp=datetime.combine(day, datetime.min.time(), UTC),
    )


def _settings(**overrides) -> PortfolioBacktestSettings:
    values = {
        "rule_version": "portfolio-backtest-v1",
        "profile": "trend",
        "max_positions": 1,
        "rebalance_every_sessions": 1,
        "initial_cash": 100_000,
        "minimum_sample_count": 1,
        "out_of_sample_start": date(2026, 1, 5),
        "costs": {
            "commission_bps": 0,
            "slippage_bps": 0,
            "price_limit_pct": 0.1,
            "price_tick": 0.01,
        },
    }
    values.update(overrides)
    return PortfolioBacktestSettings.model_validate(values)


def test_portfolio_executes_signal_at_next_session_open_and_tracks_baseline():
    days = [date(2026, 1, 5) + timedelta(days=index) for index in range(3)]
    result = simulate_portfolio(
        {"600519": [_bar(day, price) for day, price in zip(days, (10, 10, 11))]},
        {days[0]: ("600519",), days[1]: ("600519",)},
        settings=_settings(),
    )

    assert [(trade.trade_date, trade.side, trade.shares) for trade in result.trades] == [
        (days[1], "buy", 10_000)
    ]
    assert result.trades[0].signal_date == days[0]
    assert result.trades[0].trade_date > result.trades[0].signal_date
    assert result.portfolio_curve[0].equity == 100_000
    assert result.portfolio_curve[-1].equity == 110_000
    assert result.baseline_curve[0].holdings_count == 0
    assert result.baseline_curve[-1].equity == 110_000
    assert result.metrics["evidence_status"] == "enough_evidence"
    assert result.metrics["turnover"] == 1.0
    assert result.baseline_metrics["turnover"] == 1.0


def test_portfolio_skips_locked_limit_up_and_suspended_entries():
    days = [date(2026, 1, 5) + timedelta(days=index) for index in range(4)]
    bars = [
        _bar(days[0], 10),
        _bar(days[1], 11),
        _bar(days[2], 11, volume=0),
        _bar(days[3], 10.9),
    ]
    result = simulate_portfolio(
        {"600519": bars},
        {day: ("600519",) for day in days},
        settings=_settings(),
    )

    assert [trade.trade_date for trade in result.trades] == [days[3]]
    assert result.blocked_orders == 2


def test_portfolio_holds_locked_limit_down_position_until_sellable():
    days = [date(2026, 1, 5) + timedelta(days=index) for index in range(4)]
    result = simulate_portfolio(
        {
            "600519": [
                _bar(days[0], 10),
                _bar(days[1], 10),
                _bar(days[2], 9),
                _bar(days[3], 9),
            ],
            "000001": [_bar(day, 10) for day in days],
        },
        {
            days[0]: ("600519",),
            days[1]: ("000001",),
            days[2]: ("000001",),
        },
        settings=_settings(),
    )

    assert [
        (trade.trade_date, trade.code, trade.side)
        for trade in result.trades
    ] == [
        (days[1], "600519", "buy"),
        (days[3], "600519", "sell"),
        (days[3], "000001", "buy"),
    ]
    assert result.blocked_orders == 1


def test_baseline_return_includes_initial_transaction_costs():
    days = [date(2026, 1, 5), date(2026, 1, 6)]
    result = simulate_portfolio(
        {"600519": [_bar(day, 10) for day in days]},
        {},
        settings=_settings(
            costs={
                "commission_bps": 10,
                "slippage_bps": 10,
                "price_limit_pct": 0.1,
                "price_tick": 0.01,
            }
        ),
    )

    assert result.baseline_metrics["total_return"] < 0


def test_baseline_does_not_buy_locked_limit_up_bar():
    days = [date(2026, 1, 5), date(2026, 1, 6)]
    bars = [_bar(days[0], 10), _bar(days[1], 11)]
    result = simulate_portfolio(
        {"600519": bars},
        {},
        settings=_settings(),
    )

    assert result.baseline_curve[-1].holdings_count == 0
    assert result.baseline_metrics["evidence_status"] == "no_baseline_positions"


def test_baseline_retries_locked_entry_on_later_sessions():
    days = [date(2026, 1, 5) + timedelta(days=index) for index in range(3)]
    result = simulate_portfolio(
        {"600519": [_bar(days[0], 10), _bar(days[1], 11), _bar(days[2], 10.9)]},
        {},
        settings=_settings(),
    )

    assert [point.holdings_count for point in result.baseline_curve] == [0, 0, 1]


def test_annualized_metrics_are_suppressed_without_a_holdout_period():
    days = [date(2026, 1, 5) + timedelta(days=index) for index in range(4)]
    result = simulate_portfolio(
        {"600519": [_bar(day, 10 + index) for index, day in enumerate(days)]},
        {},
        settings=_settings(out_of_sample_start=None),
    )

    assert result.baseline_metrics["evidence_status"] == "holdout_not_configured"
    assert result.baseline_metrics["annualized_return"] is None
    assert result.baseline_metrics["sharpe"] is None


def test_rebalance_cadence_starts_at_first_out_of_sample_session():
    days = [date(2026, 1, 5) + timedelta(days=index) for index in range(4)]
    result = simulate_portfolio(
        {"600519": [_bar(day, 10) for day in days]},
        {day: ("600519",) for day in days},
        settings=_settings(
            rebalance_every_sessions=2,
            out_of_sample_start=days[1],
        ),
    )

    assert [trade.trade_date for trade in result.trades] == [days[2]]


def test_empty_out_of_sample_window_has_no_annualized_metrics():
    days = [date(2026, 1, 5), date(2026, 1, 6)]
    result = simulate_portfolio(
        {"600519": [_bar(day, 10) for day in days]},
        {},
        settings=_settings(out_of_sample_start=date(2026, 2, 1)),
    )

    assert result.metrics["evidence_status"] == "no_evaluation_data"
    assert result.metrics["annualized_return"] is None


def test_baseline_with_no_executable_positions_is_not_enough_evidence():
    days = [date(2026, 1, 5), date(2026, 1, 6)]
    result = simulate_portfolio(
        {"600519": [_bar(day, 10, volume=0) for day in days]},
        {},
        settings=_settings(),
    )

    assert result.baseline_metrics["evidence_status"] == "no_baseline_positions"
    assert result.baseline_metrics["annualized_return"] is None


def test_portfolio_rejects_future_or_unknown_targets():
    day = date(2026, 1, 5)

    with pytest.raises(ValueError, match="unknown symbols"):
        simulate_portfolio(
            {"600519": [_bar(day, 10)]},
            {day: ("000001",)},
            settings=_settings(),
        )
