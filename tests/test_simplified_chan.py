import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from stock_daily_report.models import DailyBar

FIXTURES = json.loads(
    (
        Path(__file__).parents[1] / "fixtures" / "chan" / "simplified_cases.json"
    ).read_text()
)


def case_bars(name: str) -> list[DailyBar]:
    start = date(2026, 1, 1)
    return [
        DailyBar(
            trade_date=start + timedelta(days=index),
            open=(high + low) / 2,
            high=high,
            low=low,
            close=(high + low) / 2,
            volume=100.0,
            amount=1000.0,
            turnover_rate=0.1,
            adjustment_mode="qfq",
            provider_name="fixture",
            source_timestamp=datetime(2026, 1, 1, tzinfo=UTC)
            + timedelta(days=index),
        )
        for index, (high, low) in enumerate(FIXTURES[name])
    ]


def test_inclusion_merges_using_upward_direction_and_equal_boundaries():
    from stock_daily_report.chan.simplified import SimplifiedChanAnalyzer

    result = SimplifiedChanAnalyzer().analyze(case_bars("inclusion_up"))

    assert len(result.processed_bars) == 2
    merged = result.processed_bars[-1]
    assert (merged.high, merged.low, merged.direction) == (13.0, 8.0, "up")
    assert merged.source_indices == (1, 2, 3)
    assert merged.source_dates == (
        date(2026, 1, 2),
        date(2026, 1, 3),
        date(2026, 1, 4),
    )


def test_simplified_analyzer_marks_bottom_after_right_bar_confirms():
    from stock_daily_report.chan.simplified import SimplifiedChanAnalyzer

    result = SimplifiedChanAnalyzer().analyze(case_bars("bottom"))

    bottom = next(item for item in result.fractals if item.kind == "bottom")
    assert (bottom.formed_at, bottom.confirmed_at, bottom.tradable_at) == (
        date(2026, 1, 2),
        date(2026, 1, 3),
        date(2026, 1, 4),
    )
    assert (bottom.status, bottom.rule_version) == ("confirmed", "simplified-v1")


def test_simplified_analyzer_marks_top_after_right_bar_confirms():
    from stock_daily_report.chan.simplified import SimplifiedChanAnalyzer

    result = SimplifiedChanAnalyzer().analyze(case_bars("top"))

    top = next(item for item in result.fractals if item.kind == "top")
    assert (top.formed_at, top.confirmed_at, top.tradable_at) == (
        date(2026, 1, 2),
        date(2026, 1, 3),
        date(2026, 1, 4),
    )
    assert top.price == 13.0


def test_final_right_bar_leaves_fractal_as_non_tradable_candidate():
    from stock_daily_report.chan.simplified import SimplifiedChanAnalyzer

    result = SimplifiedChanAnalyzer().analyze(case_bars("final_bar_bottom"))

    assert [(item.kind, item.formed_at, item.confirmed_at, item.tradable_at, item.status)
            for item in result.fractals] == [
        ("bottom", date(2026, 1, 2), None, None, "candidate")
    ]
    assert (result.state.label, result.state.status, result.state.formed_at) == (
        "candidate_upward",
        "candidate",
        date(2026, 1, 2),
    )


def test_confirmed_strokes_link_alternating_fractals_at_minimum_separation():
    from stock_daily_report.chan.simplified import SimplifiedChanAnalyzer

    result = SimplifiedChanAnalyzer().analyze(
        case_bars("strokes_and_central_extension")
    )

    assert [
        (stroke.kind, stroke.start_formed_at, stroke.end_formed_at,
         stroke.confirmed_at, stroke.tradable_at)
        for stroke in result.strokes
    ] == [
        (
            "upward",
            date(2026, 1, 2),
            date(2026, 1, 4),
            date(2026, 1, 5),
            date(2026, 1, 6),
        ),
        (
            "downward",
            date(2026, 1, 4),
            date(2026, 1, 6),
            date(2026, 1, 7),
            date(2026, 1, 8),
        ),
        (
            "upward",
            date(2026, 1, 6),
            date(2026, 1, 8),
            date(2026, 1, 9),
            date(2026, 1, 10),
        ),
        (
            "downward",
            date(2026, 1, 8),
            date(2026, 1, 10),
            date(2026, 1, 11),
            date(2026, 1, 12),
        ),
    ]
    assert all(
        stroke.end_processed_index - stroke.start_processed_index >= 2
        for stroke in result.strokes
    )


def test_fractals_one_processed_bar_apart_do_not_create_a_stroke():
    from stock_daily_report.chan.simplified import SimplifiedChanAnalyzer

    result = SimplifiedChanAnalyzer().analyze(case_bars("too_close_fractals"))

    assert [
        (item.kind, item.formed_at, item.confirmed_at, item.tradable_at)
        for item in result.fractals
    ] == [
        (
            "top",
            date(2026, 1, 2),
            date(2026, 1, 3),
            date(2026, 1, 4),
        ),
        (
            "bottom",
            date(2026, 1, 3),
            date(2026, 1, 4),
            date(2026, 1, 5),
        ),
    ]
    assert result.strokes == ()


def test_central_area_uses_three_stroke_overlap_and_only_extends_on_available_stroke():
    from stock_daily_report.chan.simplified import SimplifiedChanAnalyzer

    result = SimplifiedChanAnalyzer().analyze(
        case_bars("strokes_and_central_extension")
    )

    assert [
        (
            area.low,
            area.high,
            area.formed_at,
            area.confirmed_at,
            area.tradable_at,
            area.start_stroke_index,
            area.end_stroke_index,
        )
        for area in result.central_areas
    ] == [
        (
            10.0,
            15.0,
            date(2026, 1, 8),
            date(2026, 1, 11),
            date(2026, 1, 12),
            0,
            3,
        )
    ]
    assert result.state.label == "neutral_consolidation"
    assert result.state.status == "confirmed"


def test_two_overlapping_strokes_are_a_non_tradable_central_candidate():
    from stock_daily_report.chan.simplified import SimplifiedChanAnalyzer

    result = SimplifiedChanAnalyzer().analyze(case_bars("central_candidate"))

    assert [
        (area.low, area.high, area.formed_at, area.confirmed_at, area.tradable_at)
        for area in result.central_candidates
    ] == [(9.0, 15.0, date(2026, 1, 6), None, None)]
    assert result.state.label == "neutral_consolidation"
    assert result.state.status == "candidate"


def test_simplified_breakout_becomes_confirmed_after_two_closes():
    from stock_daily_report.chan.common import CentralArea
    from stock_daily_report.chan.simplified import SimplifiedChanAnalyzer

    bars = case_bars("bottom")
    bars.extend(
        [
            bars[-1].model_copy(
                update={
                    "trade_date": date(2026, 1, 5),
                    "open": 16.0,
                    "high": 17.0,
                    "low": 15.0,
                    "close": 16.0,
                }
            ),
            bars[-1].model_copy(
                update={
                    "trade_date": date(2026, 1, 6),
                    "open": 17.0,
                    "high": 18.0,
                    "low": 16.0,
                    "close": 17.0,
                }
            ),
        ]
    )
    area = CentralArea(
        low=5,
        high=15,
        formed_at=date(2026, 1, 1),
        confirmed_at=date(2026, 1, 1),
        tradable_at=date(2026, 1, 2),
        status="confirmed",
        rule_version="simplified-v1",
        start_stroke_index=0,
        end_stroke_index=2,
    )

    observations = SimplifiedChanAnalyzer._find_observations(bars, [area])

    assert len(observations) == 1
    assert (
        observations[0].formed_at,
        observations[0].confirmed_at,
        observations[0].status,
    ) == (date(2026, 1, 5), date(2026, 1, 6), "confirmed")


def test_support_and_resistance_levels_have_deterministic_sources_and_strengths():
    from stock_daily_report.chan.simplified import SimplifiedChanAnalyzer

    result = SimplifiedChanAnalyzer().analyze(
        case_bars("strokes_and_central_extension")
    )

    assert [
        (level.kind, level.price, level.source, level.strength, level.formed_at)
        for level in result.support_resistance
    ] == [
        ("support", 10.0, "confirmed_central_range", 3, date(2026, 1, 8)),
        ("support", 8.0, "confirmed_bottom_fractal", 2, date(2026, 1, 2)),
        ("support", 9.0, "confirmed_bottom_fractal", 2, date(2026, 1, 6)),
        ("support", 10.0, "confirmed_bottom_fractal", 2, date(2026, 1, 10)),
        ("resistance", 15.0, "confirmed_central_range", 3, date(2026, 1, 8)),
        ("resistance", 15.0, "confirmed_top_fractal", 2, date(2026, 1, 4)),
        ("resistance", 16.0, "confirmed_top_fractal", 2, date(2026, 1, 8)),
    ]


def test_analyzer_rejects_duplicate_or_out_of_order_dates_without_mutating_input():
    from stock_daily_report.chan.simplified import SimplifiedChanAnalyzer

    bars = case_bars("bottom")
    original = list(bars)
    duplicated = [bars[0], bars[1], bars[1]]
    out_of_order = [bars[1], bars[0], bars[2]]

    with pytest.raises(ValueError, match="strictly increasing"):
        SimplifiedChanAnalyzer().analyze(duplicated)
    with pytest.raises(ValueError, match="strictly increasing"):
        SimplifiedChanAnalyzer().analyze(out_of_order)

    SimplifiedChanAnalyzer().analyze(bars)
    assert bars == original
