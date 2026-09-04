"""Provider contracts for normalized market data."""

from collections.abc import Mapping
from datetime import date
from typing import Protocol, runtime_checkable

from stock_daily_report.models import DailyBar


class ProviderError(RuntimeError):
    """A provider-scoped failure with stable provider and code context."""

    def __init__(self, provider: str, code: str, detail: str) -> None:
        self.provider = provider
        self.code = code
        self.detail = detail
        super().__init__(f"{provider}[{code}]: {detail}")


class ProviderAvailabilityError(ProviderError):
    """An availability, transport, or upstream-service failure eligible for fallback."""


class ProviderDataError(ProviderError):
    """A provider schema or data failure that must not use fallback."""


@runtime_checkable
class MarketDataProvider(Protocol):
    """A source of canonical daily-bar records for one security."""

    name: str

    def get_daily_bars(
        self,
        code: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[DailyBar | Mapping[str, object]]:
        """Return chronological canonical records for ``code``."""


__all__ = [
    "DailyBar",
    "MarketDataProvider",
    "ProviderAvailabilityError",
    "ProviderDataError",
    "ProviderError",
]
