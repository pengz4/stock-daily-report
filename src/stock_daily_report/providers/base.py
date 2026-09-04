"""Provider contracts for normalized market data."""

from datetime import date
from typing import Protocol, runtime_checkable

from stock_daily_report.models import DailyBar


class ProviderError(RuntimeError):
    """An expected, provider-scoped failure that may permit fallback."""

    def __init__(self, provider: str, code: str, detail: str) -> None:
        self.provider = provider
        self.code = code
        self.detail = detail
        super().__init__(f"{provider}[{code}]: {detail}")


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


__all__ = ["DailyBar", "MarketDataProvider", "ProviderError"]
