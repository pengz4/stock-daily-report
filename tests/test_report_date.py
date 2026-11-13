from datetime import datetime
from zoneinfo import ZoneInfo

from stock_daily_report.report_date import report_date_for_run

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_scheduled_run_before_market_open_uses_previous_weekday():
    now = datetime(2026, 11, 13, 0, 58, tzinfo=SHANGHAI)

    assert report_date_for_run(now) == datetime(2026, 11, 12).date()


def test_delayed_monday_run_before_market_open_uses_friday():
    now = datetime(2026, 11, 16, 8, 0, tzinfo=SHANGHAI)

    assert report_date_for_run(now) == datetime(2026, 11, 13).date()


def test_weekend_run_uses_the_previous_friday():
    now = datetime(2026, 11, 14, 12, 0, tzinfo=SHANGHAI)

    assert report_date_for_run(now) == datetime(2026, 11, 13).date()


def test_run_after_market_open_uses_current_date():
    now = datetime(2026, 11, 13, 9, 15, tzinfo=SHANGHAI)

    assert report_date_for_run(now) == datetime(2026, 11, 13).date()


def test_explicit_report_date_is_not_adjusted_for_run_time():
    now = datetime(2026, 11, 13, 0, 58, tzinfo=SHANGHAI)

    assert report_date_for_run(now, "2026-11-10") == datetime(2026, 11, 10).date()
