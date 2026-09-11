"""Reason-coded eligibility filters for full-market scan candidates."""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from stock_daily_report.models import (
    A_SHARE_CODE_PATTERN,
    DailyBar,
    MarketScanSettings,
)
from stock_daily_report.providers.universe import Market, UniverseQuote
from stock_daily_report.quality.checks import (
    DataQualitySettings,
    validate_bars,
)


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    """One immutable eligibility decision with stable exclusion reasons."""

    eligible: bool
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.reason_codes, tuple):
            raise TypeError("reason_codes must be a tuple")
        if self.eligible == bool(self.reason_codes):
            raise ValueError(
                "eligible results must have no reasons and rejected results "
                "must have at least one reason"
            )
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("reason_codes must not contain duplicates")


def filter_universe_quote(
    quote: UniverseQuote,
    *,
    report_date: date,
    settings: MarketScanSettings,
) -> EligibilityResult:
    """Apply cheap name, security-class, quote, and liquidity checks."""

    _require_report_date(report_date)
    reasons: list[str] = []
    normalized_name = quote.name.upper()

    if normalized_name.startswith(("ST", "*ST")):
        _add_reason(reasons, "special_treatment_name")
    if (
        "退市" in normalized_name
        or "风险警示" in normalized_name
        or normalized_name.endswith("退")
    ):
        _add_reason(reasons, "delisting_risk_warning_name")
    if not _is_supported_security(quote):
        _add_reason(reasons, "unsupported_security")

    quote_fields = (quote.latest_price, quote.volume, quote.amount)
    if not all(_is_positive_finite(value) for value in quote_fields):
        _add_reason(reasons, "invalid_latest_quote")
    if _is_nonpositive(quote.volume) or _is_nonpositive(quote.amount):
        _add_reason(reasons, "suspended_quote")
    if (
        _is_finite_number(quote.amount)
        and quote.amount < settings.minimum_latest_amount
    ):
        _add_reason(reasons, "insufficient_latest_amount")
    if quote.quote_date < _most_recent_weekday(report_date):
        _add_reason(reasons, "stale_quote_date")
    elif quote.quote_date > report_date:
        _add_reason(reasons, "future_quote_date")

    return _result(reasons)


def filter_history(
    code: str,
    bars: Sequence[DailyBar],
    *,
    report_date: date,
    settings: MarketScanSettings,
) -> EligibilityResult:
    """Apply history quality and latest-trading-state checks."""

    _require_report_date(report_date)
    quality = validate_bars(
        code,
        bars,
        as_of=report_date,
        settings=DataQualitySettings(
            minimum_history_bars=settings.minimum_history_bars
        ),
    )
    reasons = list(quality.issue_codes)

    if bars:
        latest_bar = max(bars, key=lambda bar: bar.trade_date)
        if latest_bar.volume <= 0 or latest_bar.amount <= 0:
            _add_reason(reasons, "suspended_latest_bar")
    if not quality.analysis_allowed:
        _add_reason(reasons, "data_quality_rejected")

    return _result(reasons)


def _result(reasons: list[str]) -> EligibilityResult:
    reason_codes = tuple(reasons)
    return EligibilityResult(eligible=not reason_codes, reason_codes=reason_codes)


def _add_reason(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _is_supported_security(quote: UniverseQuote) -> bool:
    if not A_SHARE_CODE_PATTERN.fullmatch(quote.code):
        return False
    return quote.market == _expected_market(quote.code)


def _expected_market(code: str) -> Market:
    if code.startswith(("600", "601", "603", "605", "688")):
        return "SH"
    if code.startswith(("000", "001", "002", "003", "300", "301")):
        return "SZ"
    return "BJ"


def _is_positive_finite(value: object) -> bool:
    return _is_finite_number(value) and value > 0


def _is_nonpositive(value: object) -> bool:
    return _is_finite_number(value) and value <= 0


def _is_finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _require_report_date(value: date) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError("report_date must be a date, not a datetime")


def _most_recent_weekday(value: date) -> date:
    while value.weekday() >= 5:
        value -= timedelta(days=1)
    return value


__all__ = [
    "EligibilityResult",
    "filter_history",
    "filter_universe_quote",
]
