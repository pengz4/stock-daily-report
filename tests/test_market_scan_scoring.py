import math
import sys
from datetime import UTC, date, datetime, timedelta

import pytest

from stock_daily_report.models import DailyBar


def make_bars(
    closes: list[float],
    *,
    volumes: list[float] | None = None,
    ranges: list[tuple[float, float]] | None = None,
) -> list[DailyBar]:
    volumes = volumes or [1_000_000.0] * len(closes)
    ranges = ranges or [(close + 0.8, close - 0.8) for close in closes]
    start = date(2026, 1, 1)
    return [
        DailyBar(
            trade_date=start + timedelta(days=index),
            open=close,
            high=high,
            low=low,
            close=close,
            volume=volumes[index],
            amount=volumes[index] * close,
            turnover_rate=0.5,
            adjustment_mode="qfq",
            provider_name="test",
            source_timestamp=datetime(2026, 1, 1, tzinfo=UTC)
            + timedelta(days=index),
        )
        for index, (close, volume, (high, low)) in enumerate(
            zip(closes, volumes, ranges)
        )
    ]


def clean_rising_bars() -> list[DailyBar]:
    closes = [100.0 * 1.003**index for index in range(130)]
    volumes = [1_000_000.0] * 129 + [1_300_000.0]
    return make_bars(closes, volumes=volumes)


def overheated_bars() -> list[DailyBar]:
    closes = [100.0 * 1.001**index for index in range(109)]
    base = closes[-1]
    closes.extend(base * 1.06**step for step in range(1, 22))
    return make_bars(closes)


def gradually_overheated_bars() -> list[DailyBar]:
    closes = [100.0] * 110
    base = closes[-1]
    closes.extend(base * 1.014**step for step in range(1, 22))
    return make_bars(closes)


def structured_bars() -> list[DailyBar]:
    prefix = [100.0 + index * 0.05 for index in range(118)]
    pattern = [
        (112.0, 110.0),
        (110.0, 108.0),
        (113.0, 111.0),
        (115.0, 113.0),
        (113.0, 111.0),
        (111.0, 109.0),
        (114.0, 112.0),
        (116.0, 114.0),
        (114.0, 112.0),
        (112.0, 110.0),
        (115.0, 113.0),
        (116.0, 114.0),
    ]
    closes = prefix + [(high + low) / 2.0 for high, low in pattern]
    ranges = [(close + 0.8, close - 0.8) for close in prefix] + pattern
    return make_bars(closes, ranges=ranges)


def high_risk_bars() -> list[DailyBar]:
    closes = [100.0 + index * 0.1 for index in range(70)]
    closes.extend(145.0 if index % 2 == 0 else 75.0 for index in range(40))
    closes.extend([130.0, 120.0, 105.0, 90.0, 72.0, 65.0, 60.0, 58.0, 56.0, 55.0])
    return make_bars(closes)


def extreme_price_bars() -> list[DailyBar]:
    maximum = sys.float_info.max
    closes = [1.0] * 100 + [maximum] * 20
    start = date(2026, 1, 1)
    return [
        DailyBar(
            trade_date=start + timedelta(days=index),
            open=close,
            high=close,
            low=close,
            close=close,
            volume=0.0,
            amount=0.0,
            turnover_rate=0.0,
            adjustment_mode="qfq",
            provider_name="test",
            source_timestamp=datetime(2026, 1, 1, tzinfo=UTC)
            + timedelta(days=index),
        )
        for index, close in enumerate(closes)
    ]


def _all_scores(result):
    return (result.trend, result.balanced)


def test_score_components_and_totals_are_finite_bounded_and_weighted():
    from stock_daily_report.market_scan.scoring import score_candidate

    for code, bars in (
        ("600001", clean_rising_bars()),
        ("600002", overheated_bars()),
        ("600003", structured_bars()),
        ("600004", high_risk_bars()),
    ):
        result = score_candidate(code, bars)

        for score in _all_scores(result):
            assert score.code == code
            assert all(
                math.isfinite(value) and 0.0 <= value <= 100.0
                for value in (
                    score.total,
                    score.components.trend,
                    score.components.momentum,
                    score.components.volume,
                    score.components.structure,
                    score.components.risk,
                )
            )
        if code == "600001":
            assert "risk_measures_within_moderate_ranges" in (
                result.trend.evidence_codes
            )
            assert "risk_measures_within_moderate_ranges" in (
                result.balanced.evidence_codes
            )

        trend = result.trend
        assert trend.total == pytest.approx(
            trend.components.trend * 0.45
            + trend.components.momentum * 0.30
            + trend.components.volume * 0.15
            + trend.components.risk * 0.10
        )
        balanced = result.balanced
        assert balanced.total == pytest.approx(
            balanced.components.trend * 0.30
            + balanced.components.momentum * 0.20
            + balanced.components.volume * 0.15
            + balanced.components.structure * 0.20
            + balanced.components.risk * 0.15
        )


@pytest.mark.parametrize(
    "value",
    [0.15, math.nextafter(sys.float_info.max, 0.0), sys.float_info.max],
)
def test_scaled_saturates_finite_values_at_or_above_upper_threshold(value):
    from stock_daily_report.market_scan.scoring import _scaled

    assert _scaled(value, -0.05, 0.15) == 100.0


def test_trend_profile_favors_clean_momentum_over_overheated_spike():
    from stock_daily_report.market_scan.scoring import score_candidate

    clean = score_candidate("600001", clean_rising_bars()).trend
    overheated = score_candidate("600002", overheated_bars()).trend

    assert clean.total > overheated.total
    assert clean.components.momentum > overheated.components.momentum
    assert "overheated_short_term_momentum" in overheated.risk_codes
    assert "momentum_capped_for_overheating" in overheated.evidence_codes


def test_overheated_risk_does_not_claim_all_risk_measures_are_moderate():
    from stock_daily_report.market_scan.scoring import score_candidate

    overheated = score_candidate("600002", gradually_overheated_bars()).trend

    assert "overheated_short_term_momentum" in overheated.risk_codes
    assert "risk_measures_within_moderate_ranges" not in overheated.evidence_codes


def test_balanced_profile_rewards_confirmed_structure_with_moderate_risk():
    from stock_daily_report.market_scan.scoring import score_candidate

    structured = score_candidate("600003", structured_bars())
    unstructured = score_candidate("600001", clean_rising_bars())

    assert structured.balanced.components.structure > (
        unstructured.balanced.components.structure
    )
    assert structured.balanced.components.risk >= 50.0
    assert "confirmed_central_structure" in structured.balanced.evidence_codes
    assert structured.balanced.total - structured.trend.total > (
        unstructured.balanced.total - unstructured.trend.total
    )


def test_high_volatility_and_deep_drawdown_reduce_risk_component():
    from stock_daily_report.market_scan.scoring import score_candidate

    clean = score_candidate("600001", clean_rising_bars()).balanced
    high_risk = score_candidate("600004", high_risk_bars()).balanced

    assert high_risk.components.risk < clean.components.risk
    assert "high_realized_volatility" in high_risk.risk_codes
    assert "deep_trailing_drawdown" in high_risk.risk_codes


def test_extreme_finite_prices_remain_bounded_and_are_not_scored_as_moderate_risk():
    from stock_daily_report.indicators.technical import (
        _PERCENTAGE_RETURN_CAP,
        calculate_technical_metrics,
    )
    from stock_daily_report.market_scan.scoring import score_candidate

    bars = extreme_price_bars()
    metrics = calculate_technical_metrics(bars)
    result = score_candidate("600005", bars)

    assert metrics.return20 == _PERCENTAGE_RETURN_CAP
    assert math.isfinite(metrics.return20)
    assert metrics.realized_volatility20 is not None
    assert math.isfinite(metrics.realized_volatility20)

    for score in _all_scores(result):
        assert all(
            math.isfinite(value) and 0.0 <= value <= 100.0
            for value in (
                score.total,
                score.components.trend,
                score.components.momentum,
                score.components.volume,
                score.components.structure,
                score.components.risk,
            )
        )
        assert score.components.risk <= 60.0
        assert "high_realized_volatility" in score.risk_codes
        assert "overheated_short_term_momentum" in score.risk_codes
        assert (
            "risk_measures_within_moderate_ranges"
            not in score.evidence_codes
        )


def test_as_of_scoring_ignores_later_bars():
    from stock_daily_report.market_scan.scoring import score_candidate

    prefix = clean_rising_bars()
    later = make_bars(
        [bar.close for bar in prefix]
        + [prefix[-1].close * 0.7, prefix[-1].close * 1.4],
        volumes=[bar.volume for bar in prefix] + [5_000_000.0, 8_000_000.0],
    )

    expected = score_candidate("600001", prefix)
    actual = score_candidate("600001", later, as_of=prefix[-1].trade_date)

    assert actual == expected


def test_exact_score_ties_sort_by_code():
    from stock_daily_report.market_scan.scoring import rank_scores, score_candidate

    bars = clean_rising_bars()
    scores = [
        score_candidate("600010", bars).trend,
        score_candidate("000001", bars).trend,
        score_candidate("300001", bars).trend,
    ]

    assert [score.code for score in rank_scores(scores)] == [
        "000001",
        "300001",
        "600010",
    ]
