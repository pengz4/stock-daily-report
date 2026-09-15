from datetime import UTC, date, datetime, timedelta

from stock_daily_report.market_scan.state import (
    calculate_breadth,
    calculate_index_state,
    calculate_market_state,
    classify_market_state,
)
from stock_daily_report.models import DailyBar
from stock_daily_report.providers.base import ProviderAvailabilityError
from stock_daily_report.providers.universe import UniverseQuote

REPORT_DATE = date(2026, 9, 11)
GENERATED_AT = datetime(2026, 9, 11, 8, tzinfo=UTC)


def _bars(values):
    start = REPORT_DATE - timedelta(days=len(values) - 1)
    return [
        DailyBar(
            trade_date=start + timedelta(days=index),
            open=value,
            high=value + 1,
            low=value - 1,
            close=value,
            volume=1_000.0,
            amount=value * 1_000.0,
            turnover_rate=0.0,
            adjustment_mode="none",
            provider_name="fixture",
            source_timestamp=GENERATED_AT,
        )
        for index, value in enumerate(values)
    ]


def _quote(change_pct):
    return UniverseQuote(
        code=f"600{abs(hash(change_pct)) % 1000:03d}",
        name="test",
        market="SH",
        latest_price=10.0,
        volume=1_000.0,
        amount=10_000.0,
        quote_date=REPORT_DATE,
        change_pct=change_pct,
    )


def test_index_state_calculates_20_and_60_day_moving_average_relations():
    state = calculate_index_state(
        name="上证指数",
        code="000001",
        bars=_bars([100.0] * 59 + [120.0]),
        report_date=REPORT_DATE,
        generated_at=GENERATED_AT,
    )

    assert state.close_vs_ma20 == "above"
    assert state.close_vs_ma60 == "above"
    assert state.trend == "bullish"


def test_breadth_counts_quotes_by_change_pct_and_ignores_missing_values():
    breadth = calculate_breadth(
        [_quote(1.2), _quote(-0.5), _quote(0.0), _quote(None)]
    )

    assert breadth.advancing_count == 1
    assert breadth.declining_count == 1
    assert breadth.unchanged_count == 1
    assert breadth.valid_count == 3
    assert breadth.total_count == 4
    assert breadth.advance_decline_ratio == 1.0


def test_breadth_treats_non_finite_changes_as_missing():
    breadth = calculate_breadth(
        [_quote(1.2), _quote(float("nan")), _quote(float("inf")), _quote(-float("inf"))]
    )

    assert breadth.advancing_count == 1
    assert breadth.declining_count == 0
    assert breadth.unchanged_count == 0
    assert breadth.valid_count == 1
    assert breadth.total_count == 4
    assert breadth.status == "partial"


def test_non_finite_changes_do_not_drive_market_classification():
    bullish = calculate_index_state(
        name="上证指数",
        code="000001",
        bars=_bars([100.0] * 59 + [120.0]),
        report_date=REPORT_DATE,
        generated_at=GENERATED_AT,
    )
    breadth = calculate_breadth(
        [_quote(float("nan")), _quote(float("inf")), _quote(-float("inf"))]
    )

    assert classify_market_state([bullish], breadth) == "insufficient"


def test_market_state_classification_identifies_aligned_directions():
    bullish = calculate_index_state(
        name="上证指数",
        code="000001",
        bars=_bars([100.0] * 59 + [120.0]),
        report_date=REPORT_DATE,
        generated_at=GENERATED_AT,
    )
    breadth = calculate_breadth([_quote(1.0), _quote(0.1), _quote(-0.1)])

    assert classify_market_state([bullish], breadth) == "aligned"


def test_market_state_classification_identifies_divergent_directions():
    bearish = calculate_index_state(
        name="上证指数",
        code="000001",
        bars=_bars([120.0] * 59 + [100.0]),
        report_date=REPORT_DATE,
        generated_at=GENERATED_AT,
    )
    breadth = calculate_breadth([_quote(1.0), _quote(0.1), _quote(-0.1)])

    assert classify_market_state([bearish], breadth) == "divergent"


def test_market_state_classification_reports_insufficient_inputs():
    state = calculate_index_state(
        name="上证指数",
        code="000001",
        bars=_bars([100.0] * 10),
        report_date=REPORT_DATE,
        generated_at=GENERATED_AT,
    )
    breadth = calculate_breadth([_quote(None)])

    assert classify_market_state([state], breadth) == "insufficient"


def test_market_state_preserves_available_indices_when_another_provider_fails():
    state = calculate_market_state(
        report_date=REPORT_DATE,
        generated_at=GENERATED_AT,
        quotes=[_quote(1.0), _quote(0.1), _quote(-0.1)],
        index_bars={
            "000001": _bars([100.0] * 59 + [120.0]),
            "399001": ProviderAvailabilityError(
                "akshare", "network_error", "offline"
            ),
        },
        index_definitions=(
            ("上证指数", "000001"),
            ("深证成指", "399001"),
        ),
    )

    assert state.status == "partial"
    assert state.indices[0].status == "available"
    assert state.indices[1].status == "unavailable"
    assert state.indices[1].error_code == "network_error"
