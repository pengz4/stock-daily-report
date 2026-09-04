"""Provider contracts for normalized market data."""

from datetime import date
from typing import Protocol, runtime_checkable

from stock_daily_report.models import DailyBar


@runtime_checkable
class MarketDataProvider(Protocol):
    """A source of already-normalized daily bars for one security."""

    name: str

    def get_daily_bars(
        self,
        code: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[DailyBar]:
        """Return chronological normalized bars for ``code``."""


__all__ = ["DailyBar", "MarketDataProvider"]
