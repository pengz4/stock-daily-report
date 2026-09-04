"""Optional AkShare adapter that normalizes complete daily history records."""

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

from pydantic import ValidationError

from stock_daily_report.models import DailyBar
from stock_daily_report.providers.base import ProviderError

_SOURCE_FIELDS = {
    "日期": "trade_date",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "volume",
    "成交额": "amount",
    "换手率": "turnover_rate",
}


class AkShareMarketDataProvider:
    """Use AkShare only when installed; importing this module has no dependency."""

    name = "akshare"

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
    ) -> list[DailyBar]:
        """Fetch and normalize AkShare's daily A-share field names."""

        try:
            response = self._resolve_fetcher()(
                symbol=code,
                period="daily",
                start_date=start.strftime("%Y%m%d") if start else "",
                end_date=end.strftime("%Y%m%d") if end else "",
                adjust=self._adjustment_mode,
            )
        except ProviderError:
            raise
        except Exception as error:
            raise ProviderError("akshare", "network_error", str(error)) from error

        try:
            records = _records_from_response(response)
            return [self._normalize_record(record) for record in records]
        except ProviderError:
            raise
        except (TypeError, ValueError, ValidationError) as error:
            raise ProviderError("akshare", "malformed_response", str(error)) from error

    def _resolve_fetcher(self) -> Callable[..., object]:
        if self._fetcher is not None:
            return self._fetcher
        try:
            import akshare  # type: ignore[import-not-found]
        except ImportError as error:
            raise ProviderError(
                "akshare",
                "dependency_unavailable",
                "Install stock-daily-report[akshare] to enable this provider",
            ) from error
        return akshare.stock_zh_a_hist

    def _normalize_record(self, record: Mapping[str, object]) -> DailyBar:
        missing = sorted(set(_SOURCE_FIELDS).difference(record))
        if missing:
            raise ProviderError(
                "akshare",
                "malformed_response",
                f"Missing required source fields: {', '.join(missing)}",
            )
        normalized: dict[str, Any] = {
            target: record[source] for source, target in _SOURCE_FIELDS.items()
        }
        normalized.update(
            adjustment_mode=self._adjustment_mode,
            provider_name=self.name,
            source_timestamp=self._now().astimezone(UTC),
        )
        return DailyBar.model_validate(normalized)


def _records_from_response(response: object) -> Sequence[Mapping[str, object]]:
    if hasattr(response, "to_dict"):
        response = response.to_dict("records")  # type: ignore[union-attr]
    if not isinstance(response, Sequence) or isinstance(response, (str, bytes)):
        raise TypeError("AkShare response is not a sequence of records")
    if not all(isinstance(item, Mapping) for item in response):
        raise ValueError("AkShare response contains a non-mapping record")
    return response  # type: ignore[return-value]
