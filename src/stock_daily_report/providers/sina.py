"""AkShare Sina adapter used as an independent production fallback."""

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from stock_daily_report.providers.base import (
    ProviderAvailabilityError,
    ProviderDataError,
    ProviderError,
)

_DEFAULT_HISTORY_DAYS = 250
_REQUIRED_FIELDS = {
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "turnover",
}


class SinaMarketDataProvider:
    """Fetch A-share daily history from AkShare's Sina endpoint."""

    name = "sina"

    def __init__(
        self,
        *,
        adjustment_mode: str = "qfq",
        fetcher: Callable[..., object] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._adjustment_mode = adjustment_mode
        self._fetcher = fetcher
        self._now = now or (lambda: datetime.now(UTC))

    def get_daily_bars(
        self,
        code: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[Mapping[str, object]]:
        """Fetch and normalize Sina daily records."""

        effective_start = (
            start
            if start is not None
            else end - timedelta(days=_DEFAULT_HISTORY_DAYS)
            if end is not None
            else None
        )
        try:
            response = self._resolve_fetcher()(
                symbol=_sina_symbol(code),
                start_date=(
                    effective_start.strftime("%Y%m%d")
                    if effective_start
                    else ""
                ),
                end_date=end.strftime("%Y%m%d") if end else "21000000",
                adjust=self._adjustment_mode,
            )
        except ProviderError:
            raise
        except OSError as error:
            raise ProviderAvailabilityError(
                self.name, "network_error", str(error)
            ) from error

        try:
            return [self._map_record(record) for record in _records(response)]
        except ProviderError:
            raise
        except (TypeError, ValueError) as error:
            raise ProviderDataError(
                self.name, "provider_schema_invalid", str(error)
            ) from error

    def _resolve_fetcher(self) -> Callable[..., object]:
        if self._fetcher is not None:
            return self._fetcher
        try:
            import akshare  # type: ignore[import-not-found]
        except ImportError as error:
            raise ProviderAvailabilityError(
                self.name,
                "dependency_unavailable",
                "Install stock-daily-report[akshare] to enable this provider",
            ) from error
        return akshare.stock_zh_a_daily

    def _map_record(self, record: Mapping[str, object]) -> Mapping[str, object]:
        missing = sorted(_REQUIRED_FIELDS.difference(record))
        if missing:
            raise ProviderDataError(
                self.name,
                "provider_schema_invalid",
                f"Missing required source fields: {', '.join(missing)}",
            )
        trade_date = record["date"]
        if isinstance(trade_date, datetime):
            trade_date = trade_date.date()
        normalized: dict[str, Any] = {
            field: record[field]
            for field in ("open", "high", "low", "close", "volume", "amount")
        }
        normalized.update(
            trade_date=trade_date,
            turnover_rate=record["turnover"],
            adjustment_mode=self._adjustment_mode,
            provider_name=self.name,
            source_timestamp=self._now().astimezone(UTC),
        )
        return normalized


def _sina_symbol(code: str) -> str:
    if code.startswith("6"):
        return f"sh{code}"
    if code.startswith(("0", "3")):
        return f"sz{code}"
    if code.startswith("920"):
        return f"bj{code}"
    raise ProviderDataError("sina", "unsupported_symbol", f"Unsupported code: {code}")


def _records(response: object) -> Sequence[Mapping[str, object]]:
    if hasattr(response, "to_dict"):
        response = response.to_dict("records")  # type: ignore[union-attr]
    if not isinstance(response, Sequence) or isinstance(response, (str, bytes)):
        raise TypeError("Sina response is not a sequence of records")
    if not all(isinstance(item, Mapping) for item in response):
        raise ValueError("Sina response contains a non-mapping record")
    return response  # type: ignore[return-value]


__all__ = ["SinaMarketDataProvider"]
