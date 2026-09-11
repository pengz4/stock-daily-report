import math
import sys
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta

import pytest

from stock_daily_report.models import DailyBar


def make_bars(
    closes: list[float],
    *,
    volumes: list[float] | None = None,
    adjustment_mode: str = "qfq",
) -> list[DailyBar]:
    volumes = volumes or [100.0] * len(closes)
    start = date(2026, 1, 1)
    return [
        DailyBar(
            trade_date=start + timedelta(days=index),
            open=close,
            high=close + 1.0,
            low=close - 0.5 if close <= 1.0 else close - 1.0,
            close=close,
            volume=volumes[index],
            amount=volumes[index] * close,
            turnover_rate=0.1,
            adjustment_mode=adjustment_mode,
            provider_name="test",
            source_timestamp=datetime(2026, 1, 1, tzinfo=UTC)
            + timedelta(days=index),
        )
        for index, close in enumerate(closes)
    ]


def test_technical_metrics_calculates_exact_moving_averages_and_returns():
    from stock_daily_report.indicators.technical import calculate_technical_metrics

    metrics = calculate_technical_metrics(make_bars([float(value) for value in range(1, 122)]))

    assert metrics.ma5 == pytest.approx(119.0)
    assert metrics.ma10 == pytest.approx(116.5)
    assert metrics.ma20 == pytest.approx(111.5)
    assert metrics.ma60 == pytest.approx(91.5)
    assert metrics.ma120 == pytest.approx(61.5)
    assert metrics.return20 == pytest.approx(121.0 / 101.0 - 1.0)
    assert metrics.return60 == pytest.approx(121.0 / 61.0 - 1.0)
    assert metrics.return120 == pytest.approx(120.0)


def test_technical_metrics_calculates_trailing_ma_slopes_without_future_bars():
    from stock_daily_report.indicators.technical import calculate_technical_metrics

    prefix = make_bars([float(value) for value in range(1, 131)])
    extended = make_bars(
        [float(value) for value in range(1, 131)] + [1_000.0, 2.0]
    )

    prefix_metrics = calculate_technical_metrics(prefix)
    extended_at_prefix = calculate_technical_metrics(extended[: len(prefix)])

    assert prefix_metrics.ma20_slope5 == pytest.approx(120.5 / 115.5 - 1.0)
    assert prefix_metrics.ma60_slope5 == pytest.approx(100.5 / 95.5 - 1.0)
    assert extended_at_prefix.ma20_slope5 == pytest.approx(prefix_metrics.ma20_slope5)
    assert extended_at_prefix.ma60_slope5 == pytest.approx(prefix_metrics.ma60_slope5)


def test_technical_metrics_keeps_subnormal_moving_average_slopes_finite():
    from stock_daily_report.indicators.technical import calculate_technical_metrics

    smallest_positive = math.ulp(0.0)
    bars = [
        DailyBar(
            trade_date=date(2026, 1, 1) + timedelta(days=index),
            open=smallest_positive,
            high=smallest_positive,
            low=smallest_positive,
            close=smallest_positive,
            volume=smallest_positive,
            amount=smallest_positive,
            turnover_rate=smallest_positive,
            adjustment_mode="qfq",
            provider_name="test",
            source_timestamp=datetime(2026, 1, 1, tzinfo=UTC)
            + timedelta(days=index),
        )
        for index in range(65)
    ]

    metrics = calculate_technical_metrics(bars)

    assert metrics.ma20_slope5 == pytest.approx(0.0)
    assert metrics.ma60_slope5 == pytest.approx(0.0)
    assert math.isfinite(metrics.ma20_slope5)
    assert math.isfinite(metrics.ma60_slope5)


def test_technical_metrics_averages_maximum_finite_prices_without_overflow():
    from stock_daily_report.indicators.technical import calculate_technical_metrics

    maximum = sys.float_info.max
    bars = [
        DailyBar(
            trade_date=date(2026, 1, 1) + timedelta(days=index),
            open=maximum,
            high=maximum,
            low=maximum,
            close=maximum,
            volume=0.0,
            amount=0.0,
            turnover_rate=0.0,
            adjustment_mode="qfq",
            provider_name="test",
            source_timestamp=datetime(2026, 1, 1, tzinfo=UTC)
            + timedelta(days=index),
        )
        for index in range(125)
    ]

    metrics = calculate_technical_metrics(bars)

    assert metrics.ma120 == maximum
    assert metrics.ma20_slope5 == pytest.approx(0.0)
    assert metrics.ma60_slope5 == pytest.approx(0.0)


@pytest.mark.parametrize(("current", "expected"), [(0.0, 0.0), (1.0, None)])
def test_percentage_return_defines_zero_denominator(current, expected):
    from stock_daily_report.indicators.technical import _percentage_return

    assert _percentage_return(current, 0.0) == expected


def test_technical_metrics_calculates_exact_macd_and_rsi_after_warmup():
    from stock_daily_report.indicators.technical import calculate_technical_metrics

    metrics = calculate_technical_metrics(make_bars([float(value) for value in range(1, 41)]))

    assert metrics.macd_line == pytest.approx(6.386727317589834)
    assert metrics.macd_signal == pytest.approx(6.114555740619211)
    assert metrics.macd_histogram == pytest.approx(0.27217157697062344)
    assert metrics.rsi14 == pytest.approx(100.0)


def test_technical_metrics_calculates_drawdown_volume_ratio_and_extrema():
    from stock_daily_report.indicators.technical import calculate_technical_metrics

    closes = [100.0, 120.0, 96.0] + [110.0] * 57
    metrics = calculate_technical_metrics(
        make_bars(closes, volumes=[100.0] * 59 + [200.0])
    )

    assert metrics.drawdown60 == pytest.approx(-0.2)
    assert metrics.volume_ratio20 == pytest.approx(2.0)
    assert metrics.recent_high20 == pytest.approx(111.0)
    assert metrics.recent_low20 == pytest.approx(109.0)


def test_technical_metrics_uses_population_volatility_and_explicit_zero_volume_rules():
    from stock_daily_report.indicators.technical import calculate_technical_metrics

    steady_growth = make_bars([100.0 * 1.01**index for index in range(21)])
    no_prior_volume = make_bars(
        [100.0] * 21,
        volumes=[0.0] * 20 + [100.0],
    )
    no_volume = make_bars([100.0] * 21, volumes=[0.0] * 21)

    assert calculate_technical_metrics(steady_growth).realized_volatility20 == pytest.approx(
        0.0
    )
    assert calculate_technical_metrics(no_prior_volume).volume_ratio20 is None
    assert calculate_technical_metrics(no_volume).volume_ratio20 == pytest.approx(0.0)


def test_technical_metrics_uses_none_for_insufficient_history_and_flat_rsi():
    from stock_daily_report.indicators.technical import calculate_technical_metrics

    metrics = calculate_technical_metrics(make_bars([100.0] * 20))

    assert metrics.ma20 == pytest.approx(100.0)
    assert metrics.return20 is None
    assert metrics.macd_line is None
    assert metrics.drawdown60 is None
    assert metrics.rsi14 == pytest.approx(50.0)


def test_technical_metrics_rejects_non_chronological_or_duplicate_dates():
    from stock_daily_report.indicators.technical import calculate_technical_metrics

    bars = make_bars([100.0, 101.0, 102.0])
    duplicated = [bars[0], bars[1], bars[1]]
    out_of_order = [bars[1], bars[0], bars[2]]

    with pytest.raises(ValueError, match="strictly increasing"):
        calculate_technical_metrics(duplicated)
    with pytest.raises(ValueError, match="strictly increasing"):
        calculate_technical_metrics(out_of_order)


def test_technical_metrics_rejects_unadjusted_or_mixed_adjustment_modes():
    from stock_daily_report.indicators.technical import calculate_technical_metrics

    unadjusted = make_bars([100.0, 101.0], adjustment_mode="none")
    mixed = make_bars([100.0, 101.0])
    mixed[-1] = mixed[-1].model_copy(update={"adjustment_mode": "hfq"})

    with pytest.raises(ValueError, match="adjusted"):
        calculate_technical_metrics(unadjusted)
    with pytest.raises(ValueError, match="same adjustment_mode"):
        calculate_technical_metrics(mixed)


def test_technical_metrics_output_is_immutable():
    from stock_daily_report.indicators.technical import calculate_technical_metrics

    metrics = calculate_technical_metrics(make_bars([100.0] * 20))

    with pytest.raises(FrozenInstanceError):
        metrics.ma20 = 1.0


def test_technical_metrics_never_emits_non_finite_values_for_finite_bars():
    from stock_daily_report.indicators.technical import calculate_technical_metrics

    bars = [
        DailyBar(
            trade_date=date(2026, 1, 1) + timedelta(days=index),
            open=close,
            high=close,
            low=close,
            close=close,
            volume=close,
            amount=close,
            turnover_rate=0.1,
            adjustment_mode="qfq",
            provider_name="test",
            source_timestamp=datetime(2026, 1, 1, tzinfo=UTC)
            + timedelta(days=index),
        )
        for index, close in enumerate([1e-308, 1e308] * 61)
    ]

    metrics = calculate_technical_metrics(bars)

    assert all(
        value is None or math.isfinite(value)
        for value in vars(metrics).values()
        if isinstance(value, float) or value is None
    )
