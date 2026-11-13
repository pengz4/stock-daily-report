"""Resolve the market date used by scheduled report and scan workflows."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MARKET_OPEN = time(9, 15)


def report_date_for_run(
    now: datetime,
    requested_date: str | None = None,
) -> date:
    """Use the prior weekday for runs delayed until before the next session."""

    if requested_date:
        return date.fromisoformat(requested_date)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")

    local_now = now.astimezone(_SHANGHAI)
    if (
        local_now.weekday() < 5
        and local_now.timetz().replace(tzinfo=None) >= _MARKET_OPEN
    ):
        return local_now.date()

    previous_weekday = local_now.date() - timedelta(days=1)
    while previous_weekday.weekday() >= 5:
        previous_weekday -= timedelta(days=1)
    return previous_weekday


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requested", default=None)
    args = parser.parse_args()
    print(report_date_for_run(datetime.now(_SHANGHAI), args.requested).isoformat())


if __name__ == "__main__":
    main()
