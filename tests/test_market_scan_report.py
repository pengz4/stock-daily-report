import json
from datetime import UTC, date, datetime

import pytest

from stock_daily_report.market_scan.identity import build_market_state_identity
from stock_daily_report.market_scan.models import (
    SCAN_SCHEMA_VERSION,
    ConsensusRecord,
    MarketBreadth,
    MarketIndexState,
    MarketScanArtifact,
    MarketState,
    ProfileRankings,
    RankingRecord,
    ScanStatus,
)
from stock_daily_report.market_scan.report import (
    MarketScanArtifactError,
    MarketScanConflictError,
    load_scan_artifact,
    write_scan_artifact,
)
from stock_daily_report.models import MarketStateSettings


def _ranking(profile: str, score: float) -> RankingRecord:
    return RankingRecord(
        code="600519",
        name="贵州茅台",
        profile=profile,
        rank=1,
        score=score,
        components={
            "trend": 90.0,
            "momentum": 80.0,
            "volume": 70.0,
            "structure": 60.0,
            "risk": 50.0,
        },
        evidence_codes=("close_above_ma20",),
        risk_codes=(),
        latest_trade_date=date(2026, 9, 11),
        provider_name="fixture",
    )


def _artifact(
    *,
    generated_at: datetime,
    input_hash: str = "1" * 64,
    market_state: MarketState | None = None,
    market_state_identity: str | None = None,
) -> MarketScanArtifact:
    trend = _ranking("trend", 82.0)
    balanced = _ranking("balanced", 75.0)
    return MarketScanArtifact(
        schema_version=SCAN_SCHEMA_VERSION,
        rule_version="market-scan-v1",
        report_date=date(2026, 9, 11),
        generated_at=generated_at,
        universe_count=1,
        eligible_count=1,
        valid_count=1,
        coverage=1.0,
        exclusion_counts={},
        failure_counts={},
        rankings=ProfileRankings(trend=(trend,), balanced=(balanced,)),
        consensus=(
            ConsensusRecord(
                code="600519",
                name="贵州茅台",
                trend_rank=1,
                balanced_rank=1,
                trend_score=82.0,
                balanced_score=75.0,
                latest_trade_date=date(2026, 9, 11),
                provider_name="fixture",
            ),
        ),
        statuses=(
            ScanStatus(
                code="600519",
                name="贵州茅台",
                status="valid",
                reason_codes=(),
                provider_name="fixture",
            ),
        ),
        config_hash="0" * 64,
        input_hash=input_hash,
        provider_names=("fake-universe", "fixture"),
        market_state=market_state,
        market_state_identity=market_state_identity,
    )


def _market_state(generated_at: datetime) -> MarketState:
    return MarketState(
        report_date=date(2026, 9, 11),
        generated_at=generated_at,
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


def _capped_document() -> dict[str, object]:
    artifact = _artifact(generated_at=datetime(2026, 9, 11, 8, 0, tzinfo=UTC))
    document = artifact.model_dump()
    document.update(
        universe_count=2,
        eligible_count=2,
        valid_count=1,
        coverage=0.5,
        failure_counts={"candidate_limit_exceeded": 1},
        statuses=(
            artifact.statuses[0],
            ScanStatus(
                code="600520",
                name="Skipped",
                status="not_processed",
                reason_codes=("candidate_limit_exceeded",),
            ),
        ),
    )
    return document


def test_scan_artifact_schema_version_is_2():
    assert SCAN_SCHEMA_VERSION == 2


def test_scan_artifact_uses_versioned_date_path_and_round_trips(tmp_path):
    artifact = _artifact(generated_at=datetime(2026, 9, 11, 8, 0, tzinfo=UTC))

    path = write_scan_artifact(tmp_path, artifact)
    loaded = load_scan_artifact(path)
    document = json.loads(path.read_text(encoding="utf-8"))

    assert path == tmp_path / "market-scans" / "2026-09-11" / "scan.json"
    assert loaded == artifact
    assert document["schema_version"] == 2
    assert document["universe_count"] == 1
    assert document["rankings"]["trend"][0]["code"] == "600519"


def test_same_date_identical_artifact_is_reused_without_changing_bytes(tmp_path):
    first = _artifact(generated_at=datetime(2026, 9, 11, 8, 0, tzinfo=UTC))
    rerun = _artifact(generated_at=datetime(2026, 9, 11, 9, 0, tzinfo=UTC))

    path = write_scan_artifact(tmp_path, first)
    original_bytes = path.read_bytes()
    reused_path = write_scan_artifact(tmp_path, rerun)

    assert reused_path == path
    assert path.read_bytes() == original_bytes
    assert load_scan_artifact(path).generated_at == first.generated_at


def test_same_date_artifact_reuse_ignores_market_state_observation_timestamp(tmp_path):
    first = _artifact(
        generated_at=datetime(2026, 9, 11, 8, 0, tzinfo=UTC),
        market_state=_market_state(datetime(2026, 9, 11, 8, 0, tzinfo=UTC)),
    )
    rerun = _artifact(
        generated_at=datetime(2026, 9, 11, 9, 0, tzinfo=UTC),
        market_state=_market_state(datetime(2026, 9, 11, 9, 0, tzinfo=UTC)),
    )

    path = write_scan_artifact(tmp_path, first)
    reused_path = write_scan_artifact(tmp_path, rerun)

    assert reused_path == path
    assert load_scan_artifact(path).market_state == first.market_state


def test_legacy_market_state_artifact_is_reused_by_a_canonical_rerun(tmp_path):
    state = _market_state(datetime(2026, 9, 11, 8, 0, tzinfo=UTC))
    legacy = _artifact(
        generated_at=datetime(2026, 9, 11, 8, 0, tzinfo=UTC),
        market_state=state,
    )
    canonical = legacy.model_copy(
        update={
            "generated_at": datetime(2026, 9, 11, 9, 0, tzinfo=UTC),
            "market_state_identity": build_market_state_identity(
                MarketStateSettings(), state
            ),
        }
    )

    path = write_scan_artifact(tmp_path, legacy)

    assert write_scan_artifact(tmp_path, canonical) == path
    assert load_scan_artifact(path).market_state_identity is None


def test_loading_a_legacy_artifact_migrates_its_identity_in_memory(tmp_path):
    state = _market_state(datetime(2026, 9, 11, 8, 0, tzinfo=UTC))
    artifact = _artifact(
        generated_at=datetime(2026, 9, 11, 8, 0, tzinfo=UTC),
        market_state=state,
    )
    path = write_scan_artifact(tmp_path, artifact)

    loaded = load_scan_artifact(
        path,
        market_state_settings=MarketStateSettings(),
    )

    assert loaded.market_state_identity == build_market_state_identity(
        MarketStateSettings(), state
    )


def test_same_date_artifact_rejects_a_mismatched_stored_identity(tmp_path):
    state = _market_state(datetime(2026, 9, 11, 8, 0, tzinfo=UTC))
    mismatched = _artifact(
        generated_at=datetime(2026, 9, 11, 8, 0, tzinfo=UTC),
        market_state=state,
        market_state_identity="f" * 64,
    )
    canonical = mismatched.model_copy(
        update={
            "market_state_identity": build_market_state_identity(
                MarketStateSettings(), state
            )
        }
    )
    write_scan_artifact(tmp_path, mismatched)

    with pytest.raises(
        MarketScanConflictError,
        match="mismatched market-state identity",
    ):
        write_scan_artifact(tmp_path, canonical)


def test_loading_an_artifact_rejects_a_mismatched_identity(tmp_path):
    state = _market_state(datetime(2026, 9, 11, 8, 0, tzinfo=UTC))
    artifact = _artifact(
        generated_at=datetime(2026, 9, 11, 8, 0, tzinfo=UTC),
        market_state=state,
        market_state_identity="f" * 64,
    )
    path = write_scan_artifact(tmp_path, artifact)

    with pytest.raises(
        MarketScanArtifactError,
        match="Invalid market-state identity",
    ):
        load_scan_artifact(
            path,
            market_state_settings=MarketStateSettings(),
        )


def test_same_date_different_artifact_raises_explicit_conflict(tmp_path):
    first = _artifact(generated_at=datetime(2026, 9, 11, 8, 0, tzinfo=UTC))
    changed = _artifact(
        generated_at=datetime(2026, 9, 11, 9, 0, tzinfo=UTC),
        input_hash="2" * 64,
    )
    write_scan_artifact(tmp_path, first)

    with pytest.raises(
        MarketScanConflictError,
        match="Refusing to overwrite immutable market scan",
    ):
        write_scan_artifact(tmp_path, changed)


def test_scan_artifact_wraps_invalid_output_root_oserror(tmp_path):
    artifact = _artifact(generated_at=datetime(2026, 9, 11, 8, 0, tzinfo=UTC))
    invalid_root = tmp_path / "not-a-directory"
    invalid_root.write_text("occupied", encoding="utf-8")

    with pytest.raises(
        MarketScanArtifactError,
        match="Could not persist market scan artifact",
    ) as raised:
        write_scan_artifact(invalid_root, artifact)

    assert isinstance(raised.value.__cause__, OSError)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        (
            {"valid_count": 0, "coverage": 0.0},
            "valid_count must equal number of valid statuses",
        ),
        (
            {
                "eligible_count": 0,
                "valid_count": 0,
                "coverage": 0.0,
                "rankings": ProfileRankings(),
                "consensus": (),
                "failure_counts": {"network_error": 1},
                "statuses": (
                    ScanStatus(
                        code="600519",
                        name="贵州茅台",
                        status="history_failed",
                        reason_codes=("network_error",),
                        provider_name="fixture",
                    ),
                ),
            },
            "eligible_count must equal number of history-processed statuses",
        ),
        (
            {"exclusion_counts": {"insufficient_history": 1}},
            "exclusion_counts must match exclusion status reasons",
        ),
        (
            {"failure_counts": {"network_error": 1}},
            "failure_counts must match failed or unprocessed status reasons",
        ),
    ],
)
def test_scan_artifact_rejects_counts_inconsistent_with_statuses(updates, message):
    artifact = _artifact(generated_at=datetime(2026, 9, 11, 8, 0, tzinfo=UTC))
    document = artifact.model_dump()
    document.update(updates)

    with pytest.raises(ValueError, match=message):
        MarketScanArtifact.model_validate(document)


def test_scan_artifact_rejects_ranking_code_without_valid_status():
    artifact = _artifact(generated_at=datetime(2026, 9, 11, 8, 0, tzinfo=UTC))
    document = artifact.model_dump()
    unscanned = _ranking("trend", 82.0).model_copy(
        update={"code": "000001", "name": "Unscanned"}
    )
    document["rankings"] = ProfileRankings(trend=(unscanned,))
    document["consensus"] = ()

    with pytest.raises(
        ValueError,
        match="ranking code must reference a valid status",
    ):
        MarketScanArtifact.model_validate(document)


@pytest.mark.parametrize(
    ("location", "message"),
    [
        ("rankings", "ranking latest_trade_date must not exceed report_date"),
        ("consensus", "consensus latest_trade_date must not exceed report_date"),
    ],
)
def test_scan_artifact_rejects_future_dated_ranking_evidence(location, message):
    artifact = _artifact(generated_at=datetime(2026, 9, 11, 8, 0, tzinfo=UTC))
    document = artifact.model_dump()
    future_date = date(2026, 9, 12)
    if location == "rankings":
        document["rankings"]["trend"][0]["latest_trade_date"] = future_date
        document["rankings"]["balanced"][0]["latest_trade_date"] = future_date
    else:
        document["consensus"][0]["latest_trade_date"] = future_date

    with pytest.raises(ValueError, match=message):
        MarketScanArtifact.model_validate(document)


@pytest.mark.parametrize(
    ("location", "field", "value", "message"),
    [
        ("trend", "name", "Wrong Name", "ranking metadata must match valid status"),
        (
            "balanced",
            "provider_name",
            "wrong-provider",
            "ranking metadata must match valid status",
        ),
        (
            "balanced",
            "latest_trade_date",
            date(2026, 9, 10),
            "ranking metadata must match across profiles",
        ),
        (
            "consensus",
            "name",
            "Wrong Name",
            "consensus metadata must match rankings",
        ),
        (
            "consensus",
            "provider_name",
            "wrong-provider",
            "consensus metadata must match rankings",
        ),
        (
            "consensus",
            "latest_trade_date",
            date(2026, 9, 10),
            "consensus metadata must match rankings",
        ),
    ],
)
def test_scan_artifact_rejects_inconsistent_code_metadata(
    location,
    field,
    value,
    message,
):
    artifact = _artifact(generated_at=datetime(2026, 9, 11, 8, 0, tzinfo=UTC))
    document = artifact.model_dump()
    if location == "consensus":
        document["consensus"][0][field] = value
    else:
        document["rankings"][location][0][field] = value

    with pytest.raises(ValueError, match=message):
        MarketScanArtifact.model_validate(document)


@pytest.mark.parametrize("field", ["exclusion_counts", "failure_counts"])
def test_reason_count_mappings_cannot_be_mutated(field):
    artifact = _artifact(generated_at=datetime(2026, 9, 11, 8, 0, tzinfo=UTC))
    canonical_before = artifact.model_dump_json()

    with pytest.raises(TypeError):
        getattr(artifact, field)["new_reason"] = 1

    assert artifact.model_dump_json() == canonical_before


def test_scan_artifact_rejects_rankings_when_candidate_cap_skips_symbol():
    document = _capped_document()

    with pytest.raises(
        ValueError,
        match="rankings and consensus must be empty when any symbol is not_processed",
    ):
        MarketScanArtifact.model_validate(document)


def test_scan_artifact_rejects_legacy_schema_v1_capped_rankings():
    document = _capped_document()
    document["schema_version"] = 1

    with pytest.raises(ValueError, match="schema_version"):
        MarketScanArtifact.model_validate(document)


@pytest.mark.parametrize(
    "provider_names",
    [
        (),
        ("fake-universe",),
    ],
)
def test_scan_artifact_rejects_empty_or_incomplete_provider_names(provider_names):
    artifact = _artifact(generated_at=datetime(2026, 9, 11, 8, 0, tzinfo=UTC))
    document = artifact.model_dump()
    document["provider_names"] = provider_names

    with pytest.raises(
        ValueError,
        match="provider_names must include all referenced providers",
    ):
        MarketScanArtifact.model_validate(document)


@pytest.mark.parametrize(
    ("status", "count_field"),
    [
        ("history_failed", "failure_counts"),
        ("history_excluded", "exclusion_counts"),
    ],
)
def test_scan_artifact_rejects_missing_failed_or_excluded_status_provider(
    status,
    count_field,
):
    artifact = _artifact(generated_at=datetime(2026, 9, 11, 8, 0, tzinfo=UTC))
    document = artifact.model_dump()
    document.update(
        valid_count=0,
        coverage=0.0,
        rankings=ProfileRankings(),
        consensus=(),
        statuses=(
            ScanStatus(
                code="600519",
                name="贵州茅台",
                status=status,
                reason_codes=("provider_problem",),
                provider_name="fixture",
            ),
        ),
        provider_names=("fake-universe",),
    )
    document[count_field] = {"provider_problem": 1}

    with pytest.raises(
        ValueError,
        match="provider_names must include all referenced providers",
    ):
        MarketScanArtifact.model_validate(document)


def test_candidate_limit_exceeded_cannot_be_an_exclusion_reason():
    artifact = _artifact(generated_at=datetime(2026, 9, 11, 8, 0, tzinfo=UTC))
    document = artifact.model_dump()
    document.update(
        eligible_count=0,
        valid_count=0,
        coverage=0.0,
        exclusion_counts={"candidate_limit_exceeded": 1},
        rankings=ProfileRankings(),
        consensus=(),
        statuses=(
            ScanStatus(
                code="600519",
                name="贵州茅台",
                status="universe_excluded",
                reason_codes=("candidate_limit_exceeded",),
            ),
        ),
    )

    with pytest.raises(
        ValueError,
        match="candidate_limit_exceeded must use not_processed status",
    ):
        MarketScanArtifact.model_validate(document)
