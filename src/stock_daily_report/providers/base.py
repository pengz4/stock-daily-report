"""Provider contracts for normalized market data."""

from collections.abc import Mapping
from datetime import date
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from stock_daily_report.models import DailyBar


class ProviderRequestMetric(BaseModel):
    """Safe, bounded telemetry for one provider lookup or network attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_name: str = Field(min_length=1)
    outcome: Literal["success", "failure", "data_rejected", "cache_hit"]
    duration_ms: int = Field(ge=0)
    cache_hit: bool
    retry: bool
    error_code: str | None = None


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
    "ProviderRequestMetric",
]
