from datetime import UTC, date, datetime

import pytest

from stock_daily_report.market_scan.identity import (
    MarketStateIdentityError,
    build_market_state_identity,
    resolve_market_state_identity,
)
from stock_daily_report.market_scan.models import (
    MarketBreadth,
    MarketIndexState,
    MarketState,
)
from stock_daily_report.models import MarketStateSettings

REPORT_DATE = date(2026, 9, 11)
GENERATED_AT = datetime(2026, 9, 11, 8, 0, tzinfo=UTC)


def _state() -> MarketState:
    return MarketState(
        report_date=REPORT_DATE,
        generated_at=GENERATED_AT,
        rule_version="market-state-v1",
        indices=(
            MarketIndexState(
                name="上证指数",
                code="000001",
                status="unavailable",
                error_code="offline",
                error_message="fixture",
            ),
        ),
        breadth=MarketBreadth(
            status="unavailable",
            error_code="offline",
            error_message="fixture",
        ),
        status="unavailable",
        conclusion="市场状态数据不足",
    )


def test_missing_identity_is_migrated_from_valid_market_state_payload():
    settings = MarketStateSettings()
    state = _state()

    identity = resolve_market_state_identity(settings, state, None)

    assert identity == build_market_state_identity(settings, state)


def test_stored_identity_must_match_canonical_payload_and_configuration():
    settings = MarketStateSettings()
    state = _state()
    mismatched = build_market_state_identity(settings, state.model_copy(update={
        "conclusion": "其他结论",
    }))

    with pytest.raises(MarketStateIdentityError, match="does not match"):
        resolve_market_state_identity(settings, state, mismatched)


def test_market_state_identity_rejects_unsupported_lookbacks():
    settings = MarketStateSettings.model_construct(lookback_periods=(10, 30))

    with pytest.raises(MarketStateIdentityError, match="unsupported"):
        build_market_state_identity(settings, _state())


def test_invalid_stored_identity_is_rejected():
    with pytest.raises(MarketStateIdentityError, match="invalid"):
        resolve_market_state_identity(MarketStateSettings(), _state(), "invalid")


def test_missing_market_state_cannot_have_an_identity():
    with pytest.raises(MarketStateIdentityError, match="requires"):
        resolve_market_state_identity(MarketStateSettings(), None, "a" * 64)
