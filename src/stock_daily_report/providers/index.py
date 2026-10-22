"""Dedicated index-history providers.

Index symbols are deliberately kept separate from stock symbols.  AkShare's
index endpoint uses the bare six-digit index code and has a slightly different
schema from the stock-history endpoint.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from http.client import HTTPException
from pathlib import Path
from time import sleep
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

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
_INDEX_SYMBOLS = {
    "000001": "sh000001",
    "399001": "sz399001",
    "399006": "sz399006",
    "000300": "sh000300",
    "000852": "sh000852",
}


class IndexCloseBar(BaseModel):
    """Index-only price observation; no invented volume or turnover measures."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    trade_date: date
    close: float = Field(gt=0, allow_inf_nan=False)
    provider_name: str = Field(min_length=1)
    source_timestamp: AwareDatetime

    @field_validator("close", "trade_date", mode="before")
    @classmethod
    def reject_invalid_scalar_types(cls, value: object) -> object:
        if isinstance(value, (bool, datetime)):
            raise ValueError(  # noqa: TRY004
                "index prices and dates must not be booleans or datetimes"
            )
        return value


IndexBar = DailyBar | IndexCloseBar
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
    ) -> list[IndexBar]:
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


class SinaIndexProvider:
    """Bounded HTTPS index-price requests, isolated from stock bar semantics."""

    name = "sina-index"
    supports_hard_timeout = True

    def __init__(
        self,
        *,
        fetcher: Callable[[str], object] | None = None,
        cache_directory: str | Path | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = sleep,
    ) -> None:
        from stock_daily_report.providers.service import RawResponseCache

        self._fetcher = fetcher or self._fetch
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._cache = (
            RawResponseCache(cache_directory, ttl_seconds=21600, now=self._now)
            if cache_directory is not None else None
        )

    @staticmethod
    def _fetch(symbol: str) -> object:
        import requests

        response = requests.get(
            "https://quotes.sina.cn/cn/api/json_v2.php/"
            "CN_MarketDataService.getKLineData",
            params={"symbol": symbol, "scale": 240, "ma": "no", "datalen": 240},
            timeout=(5, 20),
        )
        response.raise_for_status()
        return response.json()

    def get_daily_bars(
        self, code: str, *, start: date | None = None, end: date | None = None,
    ) -> list[IndexCloseBar]:
        if code not in _INDEX_SYMBOLS:
            raise ProviderDataError(self.name, "invalid_index_code", repr(code))
        _validate_date_range(start, end, provider=self.name)
        local_now = self._now().astimezone(ZoneInfo("Asia/Shanghai"))
        cacheable = (
            end == local_now.date()
            and (local_now.hour, local_now.minute) >= (15, 30)
        )
        raw = (
            self._cache.load("sina-index-v1", code, start, end)
            if self._cache is not None and cacheable else None
        )
        from_cache = raw is not None
        if raw is None:
            for attempt in range(2):
                try:
                    raw = self._fetcher(_INDEX_SYMBOLS[code])
                    break
                except _PROVIDER_TRANSPORT_ERRORS as error:
                    if attempt:
                        raise ProviderAvailabilityError(
                            self.name, "network_error", str(error)
                        ) from error
                    self._sleep(2)
                except _PROVIDER_SCHEMA_ERRORS as error:
                    raise ProviderDataError(
                        self.name, "provider_schema_invalid", str(error)
                    ) from error
        try:
            records = _records_from_response(raw)
            bars = sorted(
                (IndexCloseBar(
                    trade_date=row["day"], close=row["close"],
                    provider_name=self.name, source_timestamp=self._now(),
                ) for row in records),
                key=lambda bar: bar.trade_date,
            )
            if len({bar.trade_date for bar in bars}) != len(bars):
                raise ValueError("duplicate index dates")
            bars = [
                bar for bar in bars
                if (start is None or bar.trade_date >= start)
                and bar.trade_date <= (end or local_now.date())
            ]
            if not bars or (end is not None and bars[-1].trade_date != end):
                raise ValueError("empty or stale index history")
        except _PROVIDER_SCHEMA_ERRORS as error:
            raise ProviderDataError(
                self.name, "provider_schema_invalid", f"code={code}: {error}"
            ) from error
        if self._cache is not None and cacheable and not from_cache:
            self._cache.store("sina-index-v1", code, start, end, raw)
        return bars


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
    "IndexBar",
    "IndexCloseBar",
    "IndexHistoryProvider",
    "IndexProvider",
    "SinaIndexProvider",
]
