import math
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta

import pytest

from stock_daily_report.market_scan.filters import (
    EligibilityResult,
    filter_history,
    filter_universe_quote,
)
from stock_daily_report.models import DailyBar, MarketScanSettings
from stock_daily_report.providers.universe import UniverseQuote


def _settings(*, minimum_history_bars: int = 3) -> MarketScanSettings:
    return MarketScanSettings(
        rule_version="market-scan-v1",
        trend_limit=30,
        balanced_limit=30,
        minimum_history_bars=minimum_history_bars,
        minimum_latest_amount=50_000_000,
        minimum_coverage_ratio=0.8,
        max_workers=8,
        max_candidates=1_200,
    )


def _quote(**changes: object) -> UniverseQuote:
    values = {
        "code": "600519",
        "name": "贵州茅台",
        "market": "SH",
        "latest_price": 1_500.0,
        "volume": 100_000.0,
        "amount": 150_000_000.0,
        "quote_date": date(2026, 9, 11),
    }
    values.update(changes)
    return UniverseQuote(**values)


def _bar(
    trade_date: date,
    *,
    volume: float = 1_000.0,
    amount: float = 100_000.0,
) -> DailyBar:
    return DailyBar(
        trade_date=trade_date,
        open=100.0,
        high=102.0,
        low=99.0,
        close=101.0,
        volume=volume,
        amount=amount,
        turnover_rate=0.5,
        adjustment_mode="qfq",
        provider_name="test",
        source_timestamp=datetime.combine(
            trade_date, datetime.min.time(), tzinfo=UTC
        ),
    )


def test_valid_universe_quote_passes_with_immutable_empty_result():
    result = filter_universe_quote(
        _quote(),
        report_date=date(2026, 9, 11),
        settings=_settings(),
    )

    assert result == EligibilityResult(eligible=True, reason_codes=())
    with pytest.raises(FrozenInstanceError):
        result.eligible = False
    with pytest.raises(TypeError, match="reason_codes must be a tuple"):
        EligibilityResult(eligible=False, reason_codes=["mutable"])


@pytest.mark.parametrize(
    ("name", "reason_codes"),
    [
        ("ST中珠", ("special_treatment_name",)),
        ("*ST海华", ("special_treatment_name",)),
        ("退市整理证券", ("delisting_risk_warning_name",)),
        ("新纺退", ("delisting_risk_warning_name",)),
        ("风险警示股票", ("delisting_risk_warning_name",)),
        (
            "*ST退市整理",
            ("special_treatment_name", "delisting_risk_warning_name"),
        ),
    ],
)
def test_universe_filter_rejects_special_treatment_and_delisting_names(
    name, reason_codes
):
    result = filter_universe_quote(
        _quote(name=name),
        report_date=date(2026, 9, 11),
        settings=_settings(),
    )

    assert result == EligibilityResult(False, reason_codes)


@pytest.mark.parametrize(
    "quote",
    [
        _quote(code="900901"),
        _quote(code="600519", market="SZ"),
    ],
)
def test_universe_filter_rejects_unsupported_security_classes(quote):
    result = filter_universe_quote(
        quote,
        report_date=date(2026, 9, 11),
        settings=_settings(),
    )

    assert result == EligibilityResult(False, ("unsupported_security",))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("latest_price", None),
        ("latest_price", 0.0),
        ("latest_price", -1.0),
        ("latest_price", math.inf),
        ("volume", math.nan),
        ("amount", -1.0),
    ],
)
def test_universe_filter_rejects_nonpositive_or_nonfinite_quote_fields(
    field, value
):
    result = filter_universe_quote(
        _quote(**{field: value}),
        report_date=date(2026, 9, 11),
        settings=_settings(),
    )

    assert "invalid_latest_quote" in result.reason_codes
    assert result.eligible is False


def test_universe_filter_collects_multiple_reasons_in_stable_order():
    result = filter_universe_quote(
        _quote(
            name="*ST退市整理",
            latest_price=math.nan,
            volume=0.0,
            amount=0.0,
            quote_date=date(2026, 9, 8),
        ),
        report_date=date(2026, 9, 11),
        settings=_settings(),
    )

    assert result == EligibilityResult(
        False,
        (
            "special_treatment_name",
            "delisting_risk_warning_name",
            "invalid_latest_quote",
            "suspended_quote",
            "insufficient_latest_amount",
            "stale_quote_date",
        ),
    )
    assert len(result.reason_codes) == len(set(result.reason_codes))


def test_universe_filter_accepts_friday_quote_for_weekend_report_date():
    result = filter_universe_quote(
        _quote(quote_date=date(2026, 9, 11)),
        report_date=date(2026, 9, 12),
        settings=_settings(),
    )

    assert result == EligibilityResult(True, ())


def test_universe_filter_rejects_future_quote_date():
    result = filter_universe_quote(
        _quote(quote_date=date(2026, 9, 12)),
        report_date=date(2026, 9, 11),
        settings=_settings(),
    )

    assert result == EligibilityResult(False, ("future_quote_date",))


def test_valid_history_passes_with_empty_reasons():
    bars = [_bar(date(2026, 9, day)) for day in (9, 10, 11)]

    result = filter_history(
        "600519",
        bars,
        report_date=date(2026, 9, 11),
        settings=_settings(),
    )

    assert result == EligibilityResult(True, ())


@pytest.mark.parametrize(
    ("latest_trade_date", "report_date"),
    [
        (date(2026, 9, 10), date(2026, 9, 11)),
        (date(2026, 9, 11), date(2026, 9, 14)),
    ],
)
def test_history_filter_requires_latest_completed_trading_day(
    latest_trade_date, report_date
):
    bars = [
        _bar(latest_trade_date - timedelta(days=2)),
        _bar(latest_trade_date - timedelta(days=1)),
        _bar(latest_trade_date),
    ]

    result = filter_history(
        "600519",
        bars,
        report_date=report_date,
        settings=_settings(),
    )

    assert result == EligibilityResult(
        False,
        ("stale_last_trade_date", "data_quality_rejected"),
    )


def test_history_filter_collects_quality_and_suspension_reasons():
    suspended = _bar(date(2026, 9, 8), volume=0.0, amount=0.0)
    bars = [
        suspended,
        suspended.model_copy(),
        _bar(date(2026, 9, 7)),
    ]

    result = filter_history(
        "600519",
        bars,
        report_date=date(2026, 9, 11),
        settings=_settings(minimum_history_bars=4),
    )

    assert result == EligibilityResult(
        False,
        (
            "duplicate_trade_date",
            "non_chronological_trade_dates",
            "insufficient_history",
            "stale_last_trade_date",
            "suspended_latest_bar",
            "data_quality_rejected",
        ),
    )
    assert len(result.reason_codes) == len(set(result.reason_codes))


def test_history_filter_surfaces_other_data_quality_rejections():
    bars = [
        _bar(date(2026, 9, 10)),
        _bar(date(2026, 9, 11)),
        _bar(date(2026, 9, 12)),
    ]

    result = filter_history(
        "600519",
        bars,
        report_date=date(2026, 9, 11),
        settings=_settings(),
    )

    assert result == EligibilityResult(
        False,
        ("future_trade_date", "data_quality_rejected"),
    )


@pytest.mark.parametrize("report_date", [date(2026, 9, 12), date(2026, 9, 13)])
def test_history_filter_accepts_friday_latest_bar_for_weekend_report(report_date):
    bars = [
        _bar(date(2026, 9, 9)),
        _bar(date(2026, 9, 10)),
        _bar(date(2026, 9, 11)),
    ]

    result = filter_history(
        "600519",
        bars,
        report_date=report_date,
        settings=_settings(),
    )

    assert result == EligibilityResult(True, ())
