from datetime import UTC, date, datetime, timedelta

import pytest

from stock_daily_report.models import DailyBar


def make_bars(closes: list[float]) -> list[DailyBar]:
    start = date(2026, 1, 1)
    return [
        DailyBar(
            trade_date=start + timedelta(days=index),
            open=close,
            high=close + 1.0,
            low=close - 0.5 if close <= 1.0 else close - 1.0,
            close=close,
            volume=100.0,
            amount=close * 100.0,
            turnover_rate=0.1,
            adjustment_mode="qfq",
            provider_name="test",
            source_timestamp=datetime(2026, 1, 1, tzinfo=UTC)
            + timedelta(days=index),
        )
        for index, close in enumerate(closes)
    ]


def test_trend_label_is_waiting_when_history_is_insufficient():
    from stock_daily_report.indicators.trend import classify_trend

    result = classify_trend(make_bars([100.0] * 60))

    assert result.label == "等待确认"
    assert result.evidence_codes == ("insufficient_trend_history",)


def test_trend_label_is_bullish_with_explicit_evidence():
    from stock_daily_report.indicators.trend import classify_trend

    result = classify_trend(make_bars([float(value) for value in range(1, 81)]))

    assert result.label == "偏强"
    assert result.evidence_codes == (
        "price_above_rising_moving_averages",
        "positive_20_and_60_day_returns",
        "macd_line_above_signal",
        "rsi_in_constructive_range",
    )


def test_trend_label_is_weak_with_explicit_evidence():
    from stock_daily_report.indicators.trend import classify_trend

    result = classify_trend(make_bars([float(value) for value in range(80, 0, -1)]))

    assert result.label == "偏弱"
    assert result.evidence_codes == (
        "price_below_falling_moving_averages",
        "negative_20_and_60_day_returns",
        "macd_line_below_signal",
    )


def test_trend_label_is_observation_when_rules_do_not_agree():
    from stock_daily_report.indicators.trend import classify_trend

    result = classify_trend(make_bars([100.0] * 61))

    assert result.label == "观察"
    assert result.evidence_codes == ("mixed_or_neutral_trend_signals",)


def test_high_drawdown_overrides_an_otherwise_bullish_trend():
    from stock_daily_report.indicators.trend import classify_trend

    closes = [100.0, 130.0, 95.0] + [float(value) for value in range(96, 154)]
    result = classify_trend(make_bars(closes))

    assert result.label == "风险升高"
    assert result.evidence_codes == (
        "price_above_rising_moving_averages",
        "positive_20_and_60_day_returns",
        "macd_line_above_signal",
        "rsi_in_constructive_range",
        "drawdown60_exceeds_risk_threshold",
    )


def test_high_volatility_overrides_an_otherwise_bullish_trend():
    from stock_daily_report.indicators.trend import classify_trend

    closes = [100.0] * 41 + [100.0, 140.0] * 5 + [200.0] * 10
    result = classify_trend(make_bars(closes))

    assert result.label == "风险升高"
    assert "realized_volatility20_exceeds_risk_threshold" in result.evidence_codes


@pytest.mark.parametrize("closes", ([100.0, 101.0, 100.0] * 21,))
def test_trend_classifier_rejects_malformed_date_ordering(closes):
    from stock_daily_report.indicators.trend import classify_trend

    bars = make_bars(closes)
    bars[20] = bars[20].model_copy(update={"trade_date": bars[19].trade_date})

    with pytest.raises(ValueError, match="strictly increasing"):
        classify_trend(bars)
