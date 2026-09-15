from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from stock_daily_report.market_scan.identity import build_market_state_identity
from stock_daily_report.market_scan.models import (
    MarketBreadth,
    MarketIndexState,
    MarketScanArtifact,
    MarketState,
    ProfileRankings,
)
from stock_daily_report.models import MarketStateSettings

REPORT_DATE = date(2026, 9, 11)
GENERATED_AT = datetime(2026, 9, 11, 8, 0, tzinfo=UTC)


def _index(
    code: str = "000001",
    *,
    status: str = "available",
    latest_trade_date: date | None = REPORT_DATE,
) -> MarketIndexState:
    return MarketIndexState(
        name="上证指数",
        code=code,
        status=status,
        latest_trade_date=(
            latest_trade_date if status != "unavailable" else None
        ),
        close=3_200.0 if status != "unavailable" else None,
        change_pct=1.2 if status != "unavailable" else None,
        close_vs_ma20="above" if status != "unavailable" else None,
        close_vs_ma60="above" if status != "unavailable" else None,
        trend="bullish" if status != "unavailable" else None,
        provider="fixture",
        error_code=None if status != "unavailable" else "provider_unavailable",
        error_message=None if status != "unavailable" else "not configured",
    )


def _breadth(
    *,
    status: str = "available",
    valid_count: int = 100,
    total_count: int = 100,
) -> MarketBreadth:
    return MarketBreadth(
        status=status,
        advancing_count=60 if status != "unavailable" else None,
        declining_count=30 if status != "unavailable" else None,
        unchanged_count=10 if status != "unavailable" else None,
        advancing_ratio=0.6 if status != "unavailable" else None,
        declining_ratio=0.3 if status != "unavailable" else None,
        advance_decline_ratio=2.0 if status != "unavailable" else None,
        limit_up_count=None,
        limit_down_count=None,
        valid_count=valid_count if status != "unavailable" else None,
        total_count=total_count if status != "unavailable" else None,
        provider="fixture",
        error_code="provider_unavailable" if status == "unavailable" else None,
        error_message="not configured" if status == "unavailable" else None,
    )


def _state(**changes: object) -> MarketState:
    values: dict[str, object] = {
        "report_date": REPORT_DATE,
        "generated_at": GENERATED_AT,
        "rule_version": "market-state-v1",
        "indices": (_index(),),
        "breadth": _breadth(),
        "status": "available",
        "conclusion": "市场状态可用",
    }
    values.update(changes)
    return MarketState(**values)


def _artifact(
    *,
    market_state: MarketState | None = None,
    market_state_identity: str | None = None,
) -> MarketScanArtifact:
    return MarketScanArtifact(
        rule_version="market-scan-v1",
        report_date=REPORT_DATE,
        generated_at=GENERATED_AT,
        universe_count=0,
        eligible_count=0,
        valid_count=0,
        coverage=0.0,
        exclusion_counts={},
        failure_counts={},
        rankings=ProfileRankings(),
        consensus=(),
        statuses=(),
        config_hash="0" * 64,
        input_hash="1" * 64,
        provider_names=("fixture",),
        market_state=market_state,
        market_state_identity=market_state_identity,
    )


def test_market_state_preserves_index_and_breadth_fields():
    state = _state()

    assert state.indices[0].code == "000001"
    assert state.indices[0].close == 3_200.0
    assert state.breadth.advancing_count == 60
    assert state.breadth.limit_up_count is None


def test_market_state_rejects_empty_indices():
    with pytest.raises(ValidationError, match="indices"):
        _state(indices=())


@pytest.mark.parametrize("status", ["partial", "unavailable"])
def test_market_state_supports_stable_degraded_states(status):
    state = _state(
        status=status,
        indices=(_index(status=status),),
        breadth=_breadth(status=status),
    )

    assert state.status == status


@pytest.mark.parametrize(
    ("status", "index_status", "breadth_status"),
    [
        ("available", "unavailable", "available"),
        ("available", "available", "unavailable"),
        ("partial", "available", "available"),
        ("unavailable", "available", "available"),
        ("unavailable", "unavailable", "partial"),
        ("partial", "unavailable", "unavailable"),
    ],
)
def test_market_state_rejects_contradictory_aggregate_status(
    status, index_status, breadth_status
):
    with pytest.raises(ValidationError, match="status"):
        _state(
            status=status,
            indices=(_index(status=index_status),),
            breadth=_breadth(status=breadth_status),
        )


@pytest.mark.parametrize("field", ["close", "change_pct"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_market_index_state_rejects_non_finite_numbers(field, value):
    with pytest.raises(ValidationError, match="finite"):
        MarketIndexState(
            **{
                **_index().model_dump(),
                field: value,
            }
        )


@pytest.mark.parametrize("value", [0, -1])
def test_market_index_state_rejects_non_positive_available_close(value):
    with pytest.raises(ValidationError, match="strictly positive"):
        MarketIndexState(
            **{
                **_index().model_dump(),
                "close": value,
            }
        )


@pytest.mark.parametrize("value", [0, -1])
def test_market_index_state_rejects_non_positive_partial_close(value):
    with pytest.raises(ValidationError, match="strictly positive"):
        MarketIndexState(
            **{
                **_index(status="partial").model_dump(),
                "close": value,
            }
        )


@pytest.mark.parametrize(
    "field",
    [
        "advancing_count",
        "declining_count",
        "unchanged_count",
        "limit_up_count",
        "limit_down_count",
        "valid_count",
        "total_count",
    ],
)
def test_market_breadth_rejects_boolean_counts(field):
    with pytest.raises(ValidationError):
        MarketBreadth(
            **{
                **_breadth().model_dump(),
                field: True,
            }
        )


def test_market_state_rejects_index_after_report_date():
    with pytest.raises(ValidationError, match="report_date"):
        _state(
            indices=(
                _index(latest_trade_date=date(2026, 9, 12)),
            )
        )


def test_market_scan_artifact_rejects_market_state_for_another_report_date():
    with pytest.raises(ValidationError, match="report_date"):
        _artifact(
            market_state=_state(report_date=date(2026, 9, 10)),
        )


@pytest.mark.parametrize("field", ["indices", "breadth"])
def test_market_scan_artifact_rejects_unlisted_market_state_provider(field):
    state = _state()
    if field == "indices":
        state = state.model_copy(
            update={
                "indices": (
                    state.indices[0].model_copy(update={"provider": "other"}),
                )
            }
        )
    else:
        state = state.model_copy(
            update={
                "breadth": state.breadth.model_copy(update={"provider": "other"})
            }
        )

    with pytest.raises(ValidationError, match="provider_names"):
        _artifact(market_state=state)


def test_market_scan_artifact_loads_without_optional_market_state():
    artifact = _artifact()

    document = artifact.model_dump(mode="json")
    document.pop("market_state")

    loaded = MarketScanArtifact.model_validate(document)

    assert loaded.market_state is None


def test_market_state_identity_ignores_generated_at_and_tracks_state():
    settings = MarketStateSettings()
    state = _state()
    identity = build_market_state_identity(settings, state)

    regenerated = state.model_copy(
        update={"generated_at": datetime(2026, 9, 11, 9, 0, tzinfo=UTC)}
    )
    changed_state = state.model_copy(update={"conclusion": "状态已变化"})

    assert identity == build_market_state_identity(settings, regenerated)
    assert identity != build_market_state_identity(settings, changed_state)


def test_market_scan_artifact_preserves_optional_market_state_identity():
    state = _state()
    identity = build_market_state_identity(MarketStateSettings(), state)
    artifact = _artifact(
        market_state=state,
        market_state_identity=identity,
    )

    document = artifact.model_dump(mode="json")
    loaded = MarketScanArtifact.model_validate(document)

    assert loaded.market_state_identity == identity

    document.pop("market_state_identity")
    legacy = MarketScanArtifact.model_validate(document)
    assert legacy.market_state_identity is None
