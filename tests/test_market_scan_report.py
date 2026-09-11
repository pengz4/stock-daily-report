import json
from datetime import UTC, date, datetime

import pytest

from stock_daily_report.market_scan.models import (
    ConsensusRecord,
    MarketScanArtifact,
    ProfileRankings,
    RankingRecord,
    ScanStatus,
)
from stock_daily_report.market_scan.report import (
    MarketScanConflictError,
    load_scan_artifact,
    write_scan_artifact,
)


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
) -> MarketScanArtifact:
    trend = _ranking("trend", 82.0)
    balanced = _ranking("balanced", 75.0)
    return MarketScanArtifact(
        schema_version=1,
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
    )


def test_scan_artifact_uses_versioned_date_path_and_round_trips(tmp_path):
    artifact = _artifact(generated_at=datetime(2026, 9, 11, 8, 0, tzinfo=UTC))

    path = write_scan_artifact(tmp_path, artifact)
    loaded = load_scan_artifact(path)
    document = json.loads(path.read_text(encoding="utf-8"))

    assert path == tmp_path / "market-scans" / "2026-09-11" / "scan.json"
    assert loaded == artifact
    assert document["schema_version"] == 1
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
