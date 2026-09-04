from datetime import date

from stock_daily_report.backtest.replay import ReplayEvent
from stock_daily_report.backtest.report import build_report, write_report


def test_backtest_report_is_versioned_and_writes_json(tmp_path):
    event = ReplayEvent(
        analyzer="strict-v1",
        event_id="strict-1",
        kind="fractal",
        observed_at=date(2026, 1, 3),
        max_input_date=date(2026, 1, 3),
        formed_at=date(2026, 1, 2),
        confirmed_at=date(2026, 1, 3),
        tradable_at=None,
        status="confirmed",
        reason_code="strict_top_fractal",
        price=10,
        low=None,
        high=None,
        revision=0,
        first_observed_at=date(2026, 1, 3),
    )

    report = build_report(
        report_date=date(2026, 1, 3),
        snapshot_hash="a" * 64,
        events_by_analyzer={"strict-v1": (event,)},
        analyzer_profile_hashes={"strict-v1": "b" * 64},
    )
    path = write_report(report, tmp_path)

    assert path.name == "report.json"
    assert path.parent.name == "2026-01-03"
    assert path.read_text(encoding="utf-8").find('"schema_version": 1') >= 0


def test_backtest_report_detects_versioned_strict_reference():
    event = ReplayEvent(
        analyzer="strict-v2",
        event_id="strict-1",
        kind="fractal",
        observed_at=date(2026, 1, 3),
        max_input_date=date(2026, 1, 3),
        formed_at=date(2026, 1, 2),
        confirmed_at=date(2026, 1, 3),
        tradable_at=None,
        status="confirmed",
        reason_code="strict_top_fractal",
        price=10,
        low=None,
        high=None,
        revision=0,
        first_observed_at=date(2026, 1, 3),
    )

    report = build_report(
        report_date=date(2026, 1, 3),
        snapshot_hash="a" * 64,
        events_by_analyzer={"strict-v2": (event,), "simplified-v1": (event,)},
        analyzer_profile_hashes={
            "strict-v2": "b" * 64,
            "simplified-v1": "c" * 64,
        },
    )

    assert "simplified-v1" in report["comparisons"]


def test_backtest_report_serializes_non_null_split_date(tmp_path):
    report = build_report(
        report_date=date(2026, 1, 3),
        snapshot_hash="a" * 64,
        events_by_analyzer={},
        analyzer_profile_hashes={},
        backtest_assumptions={"out_of_sample_start": date(2026, 1, 2)},
    )

    path = write_report(report, tmp_path)

    assert '"out_of_sample_start": "2026-01-02"' in path.read_text(encoding="utf-8")
