import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from stock_daily_report.chan.strict import (
    StrictChanAnalyzer,
    StrictEvent,
    load_strict_profile,
)
from stock_daily_report.models import DailyBar


def _bars() -> list[DailyBar]:
    ranges = json.loads(
        (
            Path(__file__).parents[1] / "fixtures" / "chan" / "strict_cases.json"
        ).read_text(encoding="utf-8")
    )["alternating"]
    return _bars_from_ranges(ranges)


def _bars_from_ranges(ranges: list[list[float]]) -> list[DailyBar]:
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


def test_strict_profile_rejects_unversioned_rule_changes():
    with pytest.raises(ValueError, match="strict-v1"):
        load_strict_profile(
            Path(__file__).parents[1] / "fixtures" / "chan" / "invalid_strict.json"
        )


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


def test_strict_fractal_is_confirmed_by_right_bar_even_without_next_bar():
    result = StrictChanAnalyzer(load_strict_profile()).analyze(
        _bars_from_ranges([[10, 8], [13, 11], [11, 9]])
    )

    top = next(
        item
        for item in result.fractals
        if item.reason_code == "strict_top_fractal"
    )
    assert top.status == "confirmed"
    assert top.confirmed_at == date(2026, 1, 3)
    assert top.tradable_at is None


def test_strict_confirmed_fractal_can_form_stroke_before_tradeability():
    result = StrictChanAnalyzer(load_strict_profile()).analyze(
        _bars_from_ranges(
            [
                [10, 8],
                [15, 12],
                [13, 10],
                [12, 9],
                [8, 5],
                [10, 7],
                [11, 8],
                [16, 13],
                [14, 11],
            ]
        )
    )

    assert len(result.strokes) == 2
    assert result.strokes[-1].confirmed_at == date(2026, 1, 9)
    assert result.strokes[-1].tradable_at is None


def test_strict_fractal_confirmation_date_is_stable_after_inclusion():
    analyzer = StrictChanAnalyzer(load_strict_profile())

    initial = analyzer.analyze(
        _bars_from_ranges([[10, 8], [13, 11], [11, 9]])
    ).fractals[0]
    extended = analyzer.analyze(
        _bars_from_ranges([[10, 8], [13, 11], [11, 9], [10, 9.5]])
    ).fractals[0]

    assert initial.confirmed_at == date(2026, 1, 3)
    assert extended.confirmed_at == initial.confirmed_at
    assert extended.tradable_at == date(2026, 1, 4)


def test_strict_breakout_signal_requires_two_chronological_bars():
    area = StrictEvent(
        kind="central_area",
        formed_at=date(2026, 1, 3),
        confirmed_at=date(2026, 1, 3),
        tradable_at=date(2026, 1, 4),
        status="confirmed",
        reason_code="strict_three_stroke_overlap",
        low=5,
        high=10,
    )
    bars = _bars_from_ranges(
        [[9, 7], [11.5, 10.5], [13, 11], [14, 12], [15, 13], [16, 14]]
    )

    candidate = StrictChanAnalyzer._find_signals(bars[:4], [area])[0]
    confirmed = StrictChanAnalyzer._find_signals(bars, [area])[0]

    assert candidate.formed_at == date(2026, 1, 4)
    assert candidate.confirmed_at is None
    assert confirmed.formed_at == date(2026, 1, 4)
    assert confirmed.confirmed_at == date(2026, 1, 5)
    assert confirmed.tradable_at == date(2026, 1, 6)


def test_strict_central_candidate_does_not_crash_on_overlapping_strokes():
    result = StrictChanAnalyzer(load_strict_profile()).analyze(
        _bars_from_ranges(
            [
                [10, 8],
                [12, 10],
                [15, 13],
                [13, 11],
                [10, 8],
                [8, 6],
                [10, 8],
                [13, 11],
                [16, 14],
                [14, 12],
                [12, 10],
            ]
        )
    )

    assert result.central_candidates


def test_strict_builds_segments_and_preserves_initial_central_range():
    result = StrictChanAnalyzer(load_strict_profile()).analyze(
        _bars_from_ranges(
            [
                [10, 8],
                [15, 12],
                [13, 10],
                [12, 9],
                [8, 5],
                [10, 7],
                [11, 8],
                [16, 13],
                [14, 11],
                [13, 10],
                [7, 4],
                [9, 6],
                [11, 8],
                [17, 14],
                [15, 12],
                [14, 11],
                [6, 3],
                [8, 5],
            ]
        )
    )

    assert result.segments
    assert result.segments[0].reason_code == "strict_three_stroke_feature_sequence"
    assert result.central_areas[0].low == 5
    assert result.central_areas[0].high == 15


def test_strict_analyzer_rejects_unordered_input_without_mutating_it():
    bars = _bars()
    original = list(bars)

    with pytest.raises(ValueError, match="strictly increasing"):
        StrictChanAnalyzer(load_strict_profile()).analyze([bars[1], bars[0]])

    StrictChanAnalyzer(load_strict_profile()).analyze(bars)
    assert bars == original
