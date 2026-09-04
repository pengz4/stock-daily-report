import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from stock_daily_report.chan.strict import (
    StrictChanAnalyzer,
    load_strict_profile,
)
from stock_daily_report.models import DailyBar


def _bars() -> list[DailyBar]:
    ranges = json.loads(
        (
            Path(__file__).parents[1] / "fixtures" / "chan" / "strict_cases.json"
        ).read_text(encoding="utf-8")
    )["alternating"]
    start = date(2026, 1, 1)
    return [
        DailyBar(
            trade_date=start + timedelta(days=index),
            open=(high + low) / 2,
            high=high,
            low=low,
            close=(high + low) / 2,
            volume=100,
            amount=1000,
            turnover_rate=0.1,
            adjustment_mode="qfq",
            provider_name="fixture",
            source_timestamp=datetime(2026, 1, 1, tzinfo=UTC)
            + timedelta(days=index),
        )
        for index, (high, low) in enumerate(ranges)
    ]


def test_strict_profile_is_versioned_and_explicit():
    profile = load_strict_profile()

    assert profile.rule_version == "strict-v1"
    assert profile.minimum_stroke_separation == 3
    assert profile.minimum_central_strokes == 3


def test_strict_analyzer_preserves_confirmation_before_tradeability():
    profile = load_strict_profile()
    result = StrictChanAnalyzer(profile).analyze(_bars())

    assert result.rule_version == "strict-v1"
    assert result.all_items
    assert all(item.reason_code for item in result.all_items)
    assert all(
        item.tradable_at is None or item.confirmed_at is not None
        for item in result.all_items
    )
    assert all(
        item.tradable_at is None or item.tradable_at >= item.confirmed_at
        for item in result.all_items
        if item.confirmed_at is not None
    )


def test_strict_analyzer_rejects_unordered_input_without_mutating_it():
    bars = _bars()
    original = list(bars)

    with pytest.raises(ValueError, match="strictly increasing"):
        StrictChanAnalyzer(load_strict_profile()).analyze([bars[1], bars[0]])

    StrictChanAnalyzer(load_strict_profile()).analyze(bars)
    assert bars == original
