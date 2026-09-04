"""Offline CSV market-data provider for deterministic tests."""

import csv
import re
from datetime import date, datetime
from pathlib import Path

from pydantic import ValidationError

from stock_daily_report.models import DailyBar

_CODE_PATTERN = re.compile(r"^\d{6}$")
_REQUIRED_COLUMNS = {
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "turnover_rate",
    "adjustment_mode",
    "provider_name",
    "source_timestamp",
}


class FixtureDataError(ValueError):
    """Raised when committed fixture data is missing or invalid."""


class FixtureMarketDataProvider:
    """Load normalized daily bars from CSV files below one fixture directory."""

    name = "fixture"

    def __init__(self, fixture_directory: str | Path) -> None:
        self._fixture_directory = Path(fixture_directory).resolve()

    def get_daily_bars(
        self,
        code: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[DailyBar]:
        """Return chronological fixture bars, optionally restricted by date."""

        if not isinstance(code, str) or not _CODE_PATTERN.fullmatch(code):
            raise FixtureDataError("Fixture stock code must be exactly six digits")
        self._validate_date_range(start, end)

        csv_path = self._fixture_path(code)
        try:
            with csv_path.open(encoding="utf-8", newline="") as fixture_file:
                reader = csv.DictReader(fixture_file)
                if reader.fieldnames is None or not _REQUIRED_COLUMNS.issubset(
                    reader.fieldnames
                ):
                    raise FixtureDataError(
                        f"Fixture data has missing required columns: {csv_path}"
                    )
                bars = [self._parse_bar(code, row) for row in reader]
        except FileNotFoundError as error:
            raise FixtureDataError(f"No fixture data for stock code {code}") from error
        except OSError as error:
            raise FixtureDataError(
                f"Could not read fixture data for stock code {code}: {error}"
            ) from error

        if not bars:
            raise FixtureDataError(f"No fixture data for stock code {code}")
        bars.sort(key=lambda bar: bar.trade_date)
        filtered_bars = [
            bar
            for bar in bars
            if (start is None or bar.trade_date >= start)
            and (end is None or bar.trade_date <= end)
        ]
        if not filtered_bars:
            raise FixtureDataError(f"No fixture data for stock code {code} in date range")
        return filtered_bars

    def _fixture_path(self, code: str) -> Path:
        candidate = (self._fixture_directory / f"{code}.csv").resolve()
        try:
            candidate.relative_to(self._fixture_directory)
        except ValueError as error:
            raise FixtureDataError("Fixture path escapes configured directory") from error
        return candidate

    @staticmethod
    def _validate_date_range(start: date | None, end: date | None) -> None:
        for name, value in (("start", start), ("end", end)):
            if value is not None and (
                not isinstance(value, date) or isinstance(value, datetime)
            ):
                raise FixtureDataError(f"{name} must be a date")
        if start is not None and end is not None and start > end:
            raise FixtureDataError("start date must not be after end date")

    @staticmethod
    def _parse_bar(code: str, row: dict[str, str | None]) -> DailyBar:
        try:
            return DailyBar.model_validate(row)
        except ValidationError as error:
            raise FixtureDataError(
                f"Invalid fixture bar for stock code {code}: {error}"
            ) from error
