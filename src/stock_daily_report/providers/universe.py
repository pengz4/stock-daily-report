"""AkShare adapter for discovering the supported full A-share universe."""

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from stock_daily_report.models import A_SHARE_CODE_PATTERN
from stock_daily_report.providers.base import (
    ProviderAvailabilityError,
    ProviderDataError,
    ProviderError,
)

Market = Literal["SH", "SZ", "BJ"]
_SHANGHAI_TIME = ZoneInfo("Asia/Shanghai")
_REQUIRED_FIELDS = frozenset({"代码", "名称", "最新价", "成交量", "成交额"})
_MISSING_QUOTE_VALUES = frozenset({"", "-", "--"})
_PROVIDER_SCHEMA_ERRORS = (KeyError, TypeError, ValueError, ZeroDivisionError)
_DEFAULT_MINIMUM_UNIVERSE_SIZE = 4_000
_MISSING_CODE_DETAIL_LIMIT = 10
_PRIMARY_ENDPOINT = "stock_zh_a_spot_em"
_FALLBACK_ENDPOINT = "stock_zh_a_spot"


@dataclass(frozen=True, slots=True)
class UniverseQuote:
    """One immutable latest quote used by stage-one market filtering."""

    code: str
    name: str
    market: Market
    latest_price: float | None
    volume: float | None
    amount: float | None
    quote_date: date

    @property
    def exchange(self) -> Market:
        """Expose the normalized market as an exchange identifier."""

        return self.market


class AkShareUniverseProvider:
    """Discover current A-share quotes without retrieving price history."""

    name = "akshare"

    def __init__(
        self,
        *,
        fetcher: Callable[[], object] | None = None,
        fallback_fetcher: Callable[[], object] | None = None,
        expected_codes_fetcher: Callable[[], object] | None = None,
        clock: Callable[[], date | datetime] | None = None,
        use_minimum_size_fallback: bool = False,
        minimum_universe_size: int = _DEFAULT_MINIMUM_UNIVERSE_SIZE,
    ) -> None:
        if (
            isinstance(minimum_universe_size, bool)
            or not isinstance(minimum_universe_size, int)
            or minimum_universe_size < 1
        ):
            raise ValueError("minimum_universe_size must be a positive integer")
        if expected_codes_fetcher is not None and use_minimum_size_fallback:
            raise ValueError(
                "expected_codes_fetcher and use_minimum_size_fallback "
                "are mutually exclusive"
            )
        self._fetcher = fetcher
        self._fallback_fetcher = fallback_fetcher
        self._expected_codes_fetcher = expected_codes_fetcher
        self._clock = clock or (lambda: datetime.now(_SHANGHAI_TIME))
        self._use_minimum_size_fallback = use_minimum_size_fallback
        self._minimum_universe_size = minimum_universe_size

    def get_quotes(self) -> list[UniverseQuote]:
        """Fetch and normalize one bulk quote snapshot."""

        response, endpoint, primary_error = self._fetch_quote_response()
        try:
            return self._quotes_from_response(response, endpoint)
        except ProviderDataError as error:
            if primary_error is None:
                raise
            raise _fallback_data_error(primary_error, error) from error

    def _quotes_from_response(
        self, response: object, endpoint: str
    ) -> list[UniverseQuote]:
        try:
            records = _records_from_response(response)
            if (
                self._use_minimum_size_fallback
                and len(records) < self._minimum_universe_size
            ):
                raise ProviderDataError(
                    self.name,
                    "provider_snapshot_incomplete",
                    f"AkShare universe response received {len(records)} records; "
                    f"expected at least {self._minimum_universe_size}",
                )
            quote_date = _date_from_clock(self._clock())
        except ProviderError:
            raise
        except _PROVIDER_SCHEMA_ERRORS as error:
            raise ProviderDataError(
                self.name, "provider_schema_invalid", str(error)
            ) from error

        quotes: list[UniverseQuote] = []
        seen_codes: set[str] = set()
        for index, record in enumerate(records):
            quote = self._map_record(record, index, quote_date, endpoint)
            if quote.code in seen_codes:
                raise ProviderDataError(
                    self.name,
                    "duplicate_symbol",
                    f"Duplicate universe code: {quote.code}",
                )
            seen_codes.add(quote.code)
            quotes.append(quote)

        if not self._use_minimum_size_fallback:
            expected_codes = self._get_expected_codes()
            missing_codes = sorted(expected_codes.difference(seen_codes))
            if missing_codes:
                examples = ", ".join(
                    missing_codes[:_MISSING_CODE_DETAIL_LIMIT]
                )
                remaining = len(missing_codes) - _MISSING_CODE_DETAIL_LIMIT
                suffix = f" (+{remaining} more)" if remaining > 0 else ""
                raise ProviderDataError(
                    self.name,
                    "provider_snapshot_incomplete",
                    f"AkShare universe response received {len(seen_codes)} "
                    f"supported codes; expected {len(expected_codes)}; "
                    f"missing {len(missing_codes)}: {examples}{suffix}",
                )
        return quotes

    def _fetch_quote_response(
        self,
    ) -> tuple[object, str, ProviderAvailabilityError | None]:
        try:
            return self._resolve_fetcher()(), _PRIMARY_ENDPOINT, None
        except ProviderAvailabilityError as error:
            primary_error = error
        except ProviderError:
            raise
        except OSError as error:
            primary_error = ProviderAvailabilityError(
                self.name, "network_error", str(error)
            )
        except _PROVIDER_SCHEMA_ERRORS as error:
            raise ProviderDataError(
                self.name, "provider_schema_invalid", str(error)
            ) from error

        if self._fallback_fetcher is None and (
            self._fetcher is not None
            or primary_error.code == "dependency_unavailable"
        ):
            raise primary_error

        try:
            return (
                self._resolve_fallback_fetcher()(),
                _FALLBACK_ENDPOINT,
                primary_error,
            )
        except ProviderAvailabilityError as error:
            fallback_error = error
        except ProviderDataError as error:
            raise _fallback_data_error(primary_error, error) from error
        except ProviderError:
            raise
        except OSError as error:
            fallback_error = ProviderAvailabilityError(
                self.name, "network_error", str(error)
            )
        except _sina_invalid_response_errors() as error:
            fallback_error = ProviderAvailabilityError(
                self.name,
                "invalid_remote_response",
                f"{_FALLBACK_ENDPOINT} returned an invalid remote response: {error}",
            )
        except _PROVIDER_SCHEMA_ERRORS as error:
            data_error = ProviderDataError(
                self.name, "provider_schema_invalid", str(error)
            )
            raise _fallback_data_error(primary_error, data_error) from error

        detail = (
            f"{_PRIMARY_ENDPOINT} attempt failed: {primary_error}; "
            f"{_FALLBACK_ENDPOINT} attempt failed: {fallback_error}"
        )
        raise ProviderAvailabilityError(
            self.name,
            "all_endpoints_unavailable",
            detail,
        ) from fallback_error

    def _resolve_fetcher(self) -> Callable[[], object]:
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
        return akshare.stock_zh_a_spot_em

    def _resolve_fallback_fetcher(self) -> Callable[[], object]:
        if self._fallback_fetcher is not None:
            return self._fallback_fetcher
        try:
            import akshare  # type: ignore[import-not-found]
        except ImportError as error:
            raise ProviderAvailabilityError(
                self.name,
                "dependency_unavailable",
                "Install stock-daily-report[akshare] to enable this provider",
            ) from error
        return akshare.stock_zh_a_spot

    def _get_expected_codes(self) -> set[str]:
        try:
            response = self._resolve_expected_codes_fetcher()()
        except ProviderError:
            raise
        except OSError as error:
            raise ProviderAvailabilityError(
                self.name,
                "network_error",
                f"expected-code universe request failed: {error}",
            ) from error
        except _PROVIDER_SCHEMA_ERRORS as error:
            raise ProviderDataError(
                self.name,
                "provider_schema_invalid",
                f"expected-code universe fetch failed: {error}",
            ) from error

        try:
            return _expected_codes_from_response(response)
        except ProviderError:
            raise
        except _PROVIDER_SCHEMA_ERRORS as error:
            raise ProviderDataError(
                self.name,
                "provider_schema_invalid",
                f"expected-code universe response is invalid: {error}",
            ) from error

    def _resolve_expected_codes_fetcher(self) -> Callable[[], object]:
        if self._expected_codes_fetcher is not None:
            return self._expected_codes_fetcher
        try:
            import akshare  # type: ignore[import-not-found]
        except ImportError as error:
            raise ProviderAvailabilityError(
                self.name,
                "dependency_unavailable",
                "Install stock-daily-report[akshare] to enable this provider",
            ) from error
        # AkShare defines this as the combined Shanghai, Shenzhen, STAR, and
        # Beijing Stock Exchange A-share code list.
        return akshare.stock_info_a_code_name

    def _map_record(
        self,
        record: Mapping[str, object],
        index: int,
        quote_date: date,
        endpoint: str,
    ) -> UniverseQuote:
        context = _row_context(record, index)
        missing = sorted(_REQUIRED_FIELDS.difference(record))
        if missing:
            raise ProviderDataError(
                self.name,
                "provider_schema_invalid",
                f"Malformed universe row ({context}): "
                f"missing fields {', '.join(missing)}",
            )

        try:
            code = _normalize_code(
                record["代码"],
                allow_exchange_prefix=endpoint == _FALLBACK_ENDPOINT,
            )
            name = _normalize_name(record["名称"])
            return UniverseQuote(
                code=code,
                name=name,
                market=_market_for_code(code),
                latest_price=_optional_float(record["最新价"], "最新价"),
                volume=_optional_float(record["成交量"], "成交量"),
                amount=_optional_float(record["成交额"], "成交额"),
                quote_date=quote_date,
            )
        except ProviderError:
            raise
        except (TypeError, ValueError) as error:
            raise ProviderDataError(
                self.name,
                "provider_schema_invalid",
                f"Malformed universe row ({context}): {error}",
            ) from error


def _records_from_response(response: object) -> Sequence[Mapping[str, object]]:
    if hasattr(response, "to_dict"):
        response = response.to_dict("records")  # type: ignore[union-attr]
    if not isinstance(response, Sequence) or isinstance(response, (str, bytes)):
        raise TypeError("AkShare universe response is not a sequence of records")
    if not response:
        raise ValueError("AkShare universe response is empty")
    if not all(isinstance(item, Mapping) for item in response):
        raise ValueError("AkShare universe response contains a non-mapping record")
    return response  # type: ignore[return-value]


def _sina_invalid_response_errors() -> tuple[type[BaseException], ...]:
    try:
        from akshare.utils.demjson import (  # type: ignore[import-not-found]
            JSONDecodeError,
        )
    except ImportError:
        return (IndexError,)
    return (JSONDecodeError, IndexError)


def _fallback_data_error(
    primary_error: ProviderAvailabilityError,
    fallback_error: ProviderDataError,
) -> ProviderDataError:
    return ProviderDataError(
        fallback_error.provider,
        fallback_error.code,
        f"{_PRIMARY_ENDPOINT} attempt failed: {primary_error}; "
        f"{_FALLBACK_ENDPOINT} fallback data error: {fallback_error.detail}",
    )


def _expected_codes_from_response(response: object) -> set[str]:
    records = _records_from_response(response)
    expected_codes: set[str] = set()
    for index, record in enumerate(records):
        if "code" in record:
            value = record["code"]
        elif "证券代码" in record:
            value = record["证券代码"]
        else:
            raise KeyError(
                f"expected-code row {index} is missing code or 证券代码"
            )
        if not isinstance(value, str):
            raise TypeError(f"expected-code row {index} code must be a string")
        code = value.strip()
        if A_SHARE_CODE_PATTERN.fullmatch(code):
            expected_codes.add(code)
    if not expected_codes:
        raise ValueError(
            "expected-code universe contains no supported A-share codes"
        )
    return expected_codes


def _normalize_code(
    value: object,
    *,
    allow_exchange_prefix: bool = False,
) -> str:
    if not isinstance(value, str):
        raise TypeError("代码 must be a string")
    code = value.strip()
    exchange_prefix = ""
    if allow_exchange_prefix and len(code) == 8:
        exchange_prefix = code[:2].lower()
        if exchange_prefix in {"sh", "sz", "bj"}:
            code = code[2:]
    if not A_SHARE_CODE_PATTERN.fullmatch(code):
        raise ProviderDataError(
            "akshare", "unsupported_symbol", f"Unsupported code: {code or value!s}"
        )
    if exchange_prefix and exchange_prefix.upper() != _market_for_code(code):
        raise ProviderDataError(
            "akshare", "unsupported_symbol", f"Unsupported code: {value}"
        )
    return code


def _normalize_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("名称 must be a non-empty string")
    return value.strip()


def _market_for_code(code: str) -> Market:
    if code.startswith(("600", "601", "603", "605", "688")):
        return "SH"
    if code.startswith(("000", "001", "002", "003", "300", "301", "302")):
        return "SZ"
    if code.startswith("920"):
        return "BJ"
    raise ProviderDataError(
        "akshare", "unsupported_symbol", f"Unsupported code: {code}"
    )


def _optional_float(value: object, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip() in _MISSING_QUOTE_VALUES:
        return None
    if isinstance(value, bool):
        raise TypeError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be numeric") from error
    return number if math.isfinite(number) else None


def _date_from_clock(value: date | datetime) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(_SHANGHAI_TIME)
        return value.date()
    if isinstance(value, date):
        return value
    raise TypeError("clock must return a date or datetime")


def _row_context(record: Mapping[str, object], index: int) -> str:
    code = record.get("代码")
    return f"row={index}, code={code}" if code is not None else f"row={index}"


__all__ = ["AkShareUniverseProvider", "UniverseQuote"]
