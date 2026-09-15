"""Dedicated index-history providers.

Index symbols are deliberately kept separate from stock symbols.  AkShare's
index endpoint uses the bare six-digit index code and has a slightly different
schema from the stock-history endpoint.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from http.client import HTTPException
from typing import Any, Protocol, runtime_checkable

from stock_daily_report.models import DailyBar
from stock_daily_report.providers.base import (
    ProviderAvailabilityError,
    ProviderDataError,
    ProviderError,
)

_SOURCE_FIELDS = {
    "日期": "trade_date",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "volume",
    "成交额": "amount",
}
_PROVIDER_SCHEMA_ERRORS = (
    AttributeError,
    IndexError,
    KeyError,
    TypeError,
    ValueError,
)
try:
    from requests.exceptions import RequestException as _RequestsRequestException
except ImportError:
    _PROVIDER_TRANSPORT_ERRORS = (OSError, HTTPException)
else:
    _PROVIDER_TRANSPORT_ERRORS = (
        OSError,
        HTTPException,
        _RequestsRequestException,
    )


@runtime_checkable
class IndexHistoryProvider(Protocol):
    """Provider contract for normalized history of one market index."""

    name: str

    def get_daily_bars(
        self,
        code: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[DailyBar]:
        """Return chronological index bars for ``code``."""


class AkShareIndexProvider:
    """Normalize AkShare ``index_zh_a_hist`` records into :class:`DailyBar`."""

    name = "akshare"
    supports_hard_timeout = True

    def __init__(
        self,
        *,
        fetcher: Callable[..., object] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._now = now or (lambda: datetime.now(UTC))

    def get_daily_bars(
        self,
        code: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[DailyBar]:
        if not isinstance(code, str) or not code.isdigit() or len(code) != 6:
            raise ProviderDataError(
                self.name,
                "invalid_index_code",
                f"code={code!r} must be a six-digit index code",
            )
        _validate_date_range(start, end, provider=self.name)
        try:
            response = self._resolve_fetcher()(
                symbol=code,
                period="daily",
                start_date=start.strftime("%Y%m%d") if start else "",
                end_date=end.strftime("%Y%m%d") if end else "",
            )
        except ProviderError:
            raise
        except _PROVIDER_TRANSPORT_ERRORS as error:
            raise ProviderAvailabilityError(
                self.name, "network_error", str(error)
            ) from error
        except _PROVIDER_SCHEMA_ERRORS as error:
            raise ProviderDataError(
                self.name,
                "provider_schema_invalid",
                f"code={code}: {error}",
            ) from error

        try:
            response_records = _records_from_response(response)
            latest_allowed = end or self._now().date()
            bars = [
                self._map_record(record, code=code)
                for record in response_records
            ]
            return sorted(
                (
                    bar
                    for bar in bars
                    if (start is None or bar.trade_date >= start)
                    and bar.trade_date <= latest_allowed
                ),
                key=lambda bar: bar.trade_date,
            )
        except ProviderError:
            raise
        except _PROVIDER_SCHEMA_ERRORS as error:
            raise ProviderDataError(
                self.name,
                "provider_schema_invalid",
                f"code={code}: {error}",
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
        return akshare.index_zh_a_hist

    def _map_record(self, record: Mapping[str, object], *, code: str) -> DailyBar:
        missing = sorted(set(_SOURCE_FIELDS).difference(record))
        if missing:
            raise ProviderDataError(
                self.name,
                "provider_schema_invalid",
                f"code={code}: missing fields: {', '.join(missing)}",
            )
        try:
            values: dict[str, Any] = {
                target: record[source] for source, target in _SOURCE_FIELDS.items()
            }
            values.update(
                adjustment_mode="none",
                provider_name=self.name,
                source_timestamp=self._now().astimezone(UTC),
                turnover_rate=0.0,
            )
            return DailyBar.model_validate(values)
        except (TypeError, ValueError) as error:
            raise ProviderDataError(
                self.name,
                "provider_schema_invalid",
                f"code={code}: invalid row: {error}",
            ) from error


AkShareIndexHistoryProvider = AkShareIndexProvider
IndexProvider = IndexHistoryProvider


def _records_from_response(response: object) -> Sequence[Mapping[str, object]]:
    if hasattr(response, "to_dict"):
        response = response.to_dict("records")  # type: ignore[union-attr]
    if not isinstance(response, Sequence) or isinstance(response, (str, bytes)):
        raise TypeError("AkShare response is not a sequence of records")
    if not all(isinstance(item, Mapping) for item in response):
        raise ValueError("AkShare response contains a non-mapping record")
    return response  # type: ignore[return-value]


def _validate_date_range(
    start: date | None,
    end: date | None,
    *,
    provider: str,
) -> None:
    for name, value in (("start", start), ("end", end)):
        if value is not None and (
            not isinstance(value, date) or isinstance(value, datetime)
        ):
            raise ProviderDataError(
                provider,
                "invalid_date_range",
                f"{name} must be a date",
            )
    if start is not None and end is not None and start > end:
        raise ProviderDataError(
            provider,
            "invalid_date_range",
            "start date must not be after end date",
        )


__all__ = [
    "AkShareIndexHistoryProvider",
    "AkShareIndexProvider",
    "IndexHistoryProvider",
    "IndexProvider",
]
